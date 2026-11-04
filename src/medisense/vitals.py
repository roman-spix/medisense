"""Turns a stream of CSI frames into breathing, heart rate and movement.

Version 2, rebuilt after the first real-world test (one board, router 2 m
behind the meditator, board 1 m in front). What that test exposed, and the
fix for each:

* Movement read 0.0 all session. Frames arrived at ~10-15 per second, the old
  code padded its 20 Hz timeline with copies, and its "quiet level" became 0.
  Movement is now measured on real frames only, against a baseline that can't
  be zero.
* Because movement never registered, breathing/heart estimates were never
  paused while the person moved (e.g. 13-16 /min when they got up). Estimates
  now restart cleanly after movement.
* Short WiFi hiccups (1-3 s) wiped the whole analysis. Gaps up to 10 s are now
  bridged; only long outages start a new segment.
* Slow breathing has strong harmonics (inhale, longer exhale, a pause), so a
  plain spectral peak can report 2x or 3x the true rate. The rate now comes
  from the waveform's own repetition period (autocorrelation), checked against
  the spectrum with explicit harmonic handling, and tracked over time.
* Heart rate jumped between 55 and 100 (a Polar strap read 74-80). WiFi
  heart rate is kept as an experimental, logged number only; a trusted heart
  rate needs a pulse sensor (MediSense Pro).

Pipeline per frame: pick the cleanest LTF block (802.11n HT-LTF when present)
and compute log-amplitude per subcarrier, minus the frame mean (removes the
radio's gain jumps). Phase is not used: after removing the radio's random
phase and timing slope, what's left is mostly noise, and frames the router
sends to other devices (beamformed) differ mainly in phase, which in
simulation made breathing lock onto slow drifts. Frames from the less common
frame type (e.g. beacons among ping replies) and frames whose amplitude
pattern doesn't look like the recent ones are set aside.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import dsp
from .protocol import HTLTF_INDEX, HTLTF_SUBCARRIER, LLTF_INDEX, LLTF_SUBCARRIER, CsiFrame

FS = 20.0  # analysis grid (Hz)
HISTORY_S = 240.0
BREATH_BAND = (0.05, 0.8)  # 3 .. 48 breaths/min
BREATH_MIN_LAG_S = 1.25
HEART_BAND = (0.7, 2.6)  # fundamental 45 .. 150 beats/min plus some harmonic content
MAX_BRIDGE_GAP_S = 10.0  # longer gaps start a new analysis segment
PAUSED_AFTER_S = 2.5  # "signal paused" in the UI
NO_SIGNAL_S = 10.0
FORGET_AFTER_S = 60.0

# Movement thresholds, in multiples of the quiet baseline (robust spread of
# the subcarrier pattern over 1 s of real frames).
RESTLESS_AT = 1.8
MOVING_AT = 3.5
SHIFT_MIN_S = 0.5
SHIFT_END_BELOW = 2.2
SHIFT_END_HOLD_S = 2.0
SETTLE_AFTER_MOVE_S = 4.0

BREATH_SHOW_CONF = 0.3
HEART_SHOW_CONF = 0.35

# Fixed subcarrier sets per frame class (L-LTF drops subcarrier 1: the radio
# sometimes marks the first CSI word invalid).
_L_KEEP = LLTF_INDEX != 1
CLASS_SETS = {
    "L": (1, 0, LLTF_INDEX[_L_KEEP], LLTF_SUBCARRIER[_L_KEEP]),  # (min blocks, block, index, subcarrier)
    "H": (2, 1, HTLTF_INDEX, HTLTF_SUBCARRIER),
    "S": (3, 1, HTLTF_INDEX, HTLTF_SUBCARRIER),
}


@dataclass
class Metrics:
    """One snapshot, produced once per second."""

    t: float
    signal: str  # "ok", "settling", "paused", "no_signal"
    frames_per_s: float
    loss_pct: float
    rssi: float | None
    channel: int | None
    breath_rate: float | None = None  # breaths per minute
    breath_conf: float = 0.0
    breath_regularity: float | None = None  # 0..1 (1 = perfectly even breaths)
    heart_rate: float | None = None  # beats per minute: only when very consistent
    heart_conf: float = 0.0
    heart_state: str = "off"  # ok / faint / moving / off
    heart_rate_experimental: float | None = None  # always logged, never trusted on screen
    motion: float = 0.0  # multiple of quiet baseline
    motion_state: str = "unknown"  # still / restless / moving / unknown
    presence: bool = False
    settled_s: float = 0.0  # seconds since the last big movement
    gap_s: float = 0.0  # seconds since the last frame
    data_quality: float = 0.0  # 0..1: frame rate x completeness
    frame_class: str | None = None

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("breath_rate", "heart_rate", "rssi", "breath_regularity", "heart_rate_experimental"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 2)
        for k in ("breath_conf", "heart_conf", "motion", "frames_per_s", "loss_pct", "settled_s", "gap_s", "data_quality"):
            d[k] = round(float(d[k]), 3)
        d["t"] = round(self.t, 3)
        d["presence"] = bool(self.presence)
        if d["channel"] is not None:
            d["channel"] = int(d["channel"])
        return d


@dataclass
class MotionEvent:
    t_start: float
    t_end: float | None = None
    peak: float = 0.0


@dataclass
class _Track:
    """Keeps a rate estimate continuous.

    Small changes are followed at once. A big change is followed when most
    recent estimates agree on the new value (consecutive or not), so one
    wrong early lock can't get stuck, and a few stray estimates can't move it.
    """

    max_rel_jump: float
    confirm: int = 6  # of the last `window` offered estimates
    window: int = 10
    first_confirm: int = 4  # of the last 6, before a first value is shown
    values: deque = field(default_factory=lambda: deque(maxlen=5))
    offered: deque = field(default_factory=lambda: deque(maxlen=10))
    last: float | None = None
    last_t: float = -1e9
    streak: int = 0  # consecutive accepted estimates

    def _cluster(self, v: float, n: int) -> list[float]:
        recent = list(self.offered)[-n:]
        return [x for x in recent if abs(x - v) / v <= 0.15]

    def offer(self, v: float, conf: float, t: float, strong: float = 0.75) -> bool:
        if self.last is not None and t - self.last_t > 60.0:
            self.clear()  # a minute without estimates: start fresh
        self.offered.append(v)
        if self.last is None:
            agree = self._cluster(v, 6)
            if len(agree) < self.first_confirm:
                return False
            return self._set(float(np.median(agree)), t, reset=True)
        if abs(v - self.last) / self.last <= self.max_rel_jump or conf >= strong:
            self.values.append(v)
            self.last = float(np.median(self.values))
            self.last_t = t
            self.streak += 1
            return True
        agree = self._cluster(v, self.window)
        if len(agree) >= self.confirm:
            return self._set(float(np.median(agree)), t, reset=True)
        return False

    def _set(self, v: float, t: float, reset: bool) -> bool:
        if reset:
            self.values.clear()
            self.streak = 0
        self.values.append(v)
        self.last = float(np.median(self.values))
        self.last_t = t
        self.streak += 1
        return True

    def clear(self) -> None:
        self.values.clear()
        self.offered.clear()
        self.last = None
        self.streak = 0


class _ClockMap:
    """Maps the board's receive timestamps (µs, 32-bit) onto this computer's clock."""

    def __init__(self):
        self.wraps = 0
        self.last_raw: int | None = None
        self.offsets: deque = deque()  # (host t, offset)

    def map(self, host_t: float, dev_us: int) -> float:
        if self.last_raw is not None and dev_us < self.last_raw - (1 << 31):
            self.wraps += 1
        self.last_raw = dev_us
        dev_s = (dev_us + self.wraps * (1 << 32)) / 1e6
        off = host_t - dev_s
        self.offsets.append((host_t, off))
        while self.offsets and host_t - self.offsets[0][0] > 30.0:
            self.offsets.popleft()
        best = min(o for _, o in self.offsets)  # least delayed arrival = true offset
        return dev_s + best

    def reset(self) -> None:
        self.__init__()


class VitalsEngine:
    def __init__(self, fs: float = FS):
        self.fs = fs
        self._clock = _ClockMap()
        self._reset_buffers()
        # Link statistics (kept across segments)
        self._arrivals: deque[float] = deque()
        self._class_times: deque[tuple[float, str, bool | None]] = deque()
        self._last_seq: int | None = None
        self._loss_window: deque[tuple[float, int, int]] = deque()
        self._received = 0
        self._lost = 0
        self._rssi_ema: float | None = None
        self._channel: int | None = None
        self._last_arrival = -1e9
        self.dominant: str | None = None
        self.rejected = 0
        self.events_out: list[MotionEvent] = []
        self.wave_out: list[tuple[float, float | None, float]] = []
        self._tick = 0

    def _reset_buffers(self) -> None:
        self._t: deque[float] = deque()
        self._f: deque[np.ndarray] = deque()
        self._profile: deque[np.ndarray] = deque(maxlen=60)
        self._recent_reject: deque[tuple[float, bool]] = deque()
        self._k_amp = 0
        self._dim: int | None = None
        self._segment_start: float | None = None
        self._last_t = -1e9
        # movement
        self._recent: deque[tuple[float, np.ndarray, int]] = deque()
        self._rssi_hist: deque[float] = deque(maxlen=120)
        self._rssi_base: float | None = None
        self._stat_hist: deque[tuple[float, float]] = deque()
        # 30 s of per-subcarrier spreads: a new posture changes which
        # subcarriers are noisy, so quiet levels must re-learn within seconds
        self._spread_hist: deque[np.ndarray] = deque(maxlen=120)
        self._spread_base: np.ndarray | None = None
        self._next_base_t = -1e9
        self._next_stat_t = -1e9
        self._motion_index = 0.0
        self._motion_state = "unknown"
        self._last_move_t = -1e9
        self._event: MotionEvent | None = None
        self._calm_since: float | None = None
        # estimators
        # Estimates come from heavily overlapping windows, so a big jump must
        # persist for several seconds before the display follows it.
        self._breath = _Track(max_rel_jump=0.2)
        self._heart = _Track(max_rel_jump=0.12)
        self._breath_conf = 0.0
        self._heart_conf = 0.0
        self._regularity: float | None = None
        self._last_breath_seen = -1e9
        self._heart_state = "off"
        # live wave
        self._weights: np.ndarray | None = None
        self._wave_scale: float | None = None
        self._grid_next: float | None = None
        self._wave_filter: dsp.StreamingSos | None = None
        self._bp = dsp.butter_bandpass(BREATH_BAND[0], BREATH_BAND[1], self.fs, order=2)

    # ------------------------------------------------------------------ input

    def add_frame(self, t: float, frame: CsiFrame) -> None:
        """Feed one frame; `t` is this computer's monotonic arrival time (s)."""
        self._link_stats(t, frame)
        cls = frame.frame_class
        if cls not in CLASS_SETS or frame.n_blocks < CLASS_SETS[cls][0]:
            return
        if not self._accept_class(cls, frame.to_me):
            return
        ts = self._clock.map(t, frame.dev_ts_us) if frame.dev_ts_us is not None else t
        if ts <= self._last_t:
            ts = self._last_t + 1e-3
        if ts - self._last_t > MAX_BRIDGE_GAP_S and self._last_t > 0:
            self._new_segment()
        feat, prof = self._features(frame, cls)
        if feat is None:
            return
        if self._dim != len(feat):
            self._new_segment()
            self._dim = len(feat)
        # Frames unlike the recent ones (another device's beamformed traffic,
        # corrupt captures) are kept out of the breathing/heart analysis.
        # (Amplitude pattern: neighbour-phase differences are too noisy frame
        # to frame to compare.) If nearly everything is "unlike", the channel
        # itself changed (new posture): start again from here.
        self._recent_reject.append((ts, False))
        if len(self._profile) >= 20:
            med = np.median(np.asarray(self._profile), axis=0)
            k = self._k_amp
            c_amp = np.corrcoef(prof[:k], med[:k])[0, 1]
            if c_amp < 0.6:
                self.rejected += 1
                self._recent_reject[-1] = (ts, True)
                while self._recent_reject and ts - self._recent_reject[0][0] > 2.0:
                    self._recent_reject.popleft()
                share = sum(r for _, r in self._recent_reject) / len(self._recent_reject)
                if share > 0.5 and len(self._recent_reject) >= 8:
                    # most frames suddenly unlike the recent ones: the
                    # channel itself is changing, i.e. the person moves
                    self._force_motion(ts)
                if share > 0.7 and len(self._recent_reject) >= 10:
                    self._profile.clear()
                    self._recent_reject.clear()
                return
        while self._recent_reject and ts - self._recent_reject[0][0] > 2.0:
            self._recent_reject.popleft()
        self._update_motion(ts, feat, frame.rssi)
        self._profile.append(prof)
        if self._segment_start is None:
            self._segment_start = ts
        self._t.append(ts)
        self._f.append(feat)
        self._last_t = ts
        while self._t and ts - self._t[0] > HISTORY_S:
            self._t.popleft()
            self._f.popleft()

    def _link_stats(self, t: float, frame: CsiFrame) -> None:
        self._arrivals.append(t)
        while self._arrivals and t - self._arrivals[0] > 5.0:
            self._arrivals.popleft()
        if self._last_seq is not None:
            gap = (frame.seq - self._last_seq) & 0xFFFFFFFF
            if 1 < gap < 1000:
                self._lost += gap - 1
        self._last_seq = frame.seq
        self._received += 1
        self._loss_window.append((t, self._received, self._lost))
        while self._loss_window and t - self._loss_window[0][0] > 10.0:
            self._loss_window.popleft()
        self._rssi_ema = frame.rssi if self._rssi_ema is None else self._rssi_ema + 0.05 * (frame.rssi - self._rssi_ema)
        self._channel = frame.channel
        self._last_arrival = t

    def _accept_class(self, cls: str, to_me: bool | None) -> bool:
        """Use only the dominant frame type, and frames addressed to the board when known."""
        now = self._last_arrival
        self._class_times.append((now, cls, to_me))
        while self._class_times and now - self._class_times[0][0] > 10.0:
            self._class_times.popleft()
        counts: dict[str, int] = {}
        any_to_me = False
        for _, c, me in self._class_times:
            counts[c] = counts.get(c, 0) + 1
            any_to_me |= bool(me)
        best = max(counts, key=counts.get)
        if self.dominant is None or (best != self.dominant and counts[best] > 2 * counts.get(self.dominant, 0)):
            self.dominant = best
        if cls != self.dominant:
            return False
        if any_to_me and to_me is False:
            return False
        return True

    def _features(self, frame: CsiFrame, cls: str) -> tuple[np.ndarray | None, np.ndarray | None]:
        _, b, idx, _sub = CLASS_SETS[cls]
        h = frame.block(b)[idx]
        a = np.abs(h)
        if a.mean() < 1.0:
            return None, None
        la = 20.0 * np.log10(a + 0.5)
        la -= la.mean()
        self._k_amp = len(la)
        feat = la.astype(np.float32)
        return feat, feat

    def _new_segment(self) -> None:
        keep_stats = (self._stat_hist, self._last_move_t)
        self._reset_buffers()
        self._stat_hist, self._last_move_t = keep_stats

    # --------------------------------------------------------------- movement

    def _update_motion(self, t: float, feat: np.ndarray, rssi: int = 0) -> None:
        self._recent.append((t, feat, rssi))
        while self._recent and t - self._recent[0][0] > 3.0:
            self._recent.popleft()
        if t < self._next_stat_t:
            return
        self._next_stat_t = t + 0.25
        # The last second, stretched to the last 12 frames when frames are
        # sparse (spread estimates from a handful of frames are unstable).
        span_start = min(t - 1.0, self._recent[-12][0] if len(self._recent) >= 12 else self._recent[0][0])
        window = [r for r in self._recent if r[0] >= span_start]
        if len(window) < 8:
            return
        # amplitude features only: neighbour-phase differences are mostly noise
        X = np.asarray([r[1][: self._k_amp or None] for r in window], dtype=np.float64)
        X = dsp.detrend(X, axis=0)
        # robust spread of each subcarrier feature over the last second (MAD)
        spread = np.median(np.abs(X - np.median(X, axis=0)), axis=0) * 1.4826 + 1e-6
        if self._spread_base is not None and len(self._spread_base) != len(spread):
            self._spread_hist.clear()
            self._spread_base = None
        self._spread_hist.append(spread)
        # signal strength wobble: the body shadowing the direct path changes
        r_sd = float(np.std([r[2] for r in window]))
        self._rssi_hist.append(r_sd)
        if self._spread_base is None or t >= self._next_base_t:
            # each feature's own quiet level: its 25th percentile over 30 s
            self._spread_base = np.percentile(np.asarray(self._spread_hist), 25, axis=0)
            self._rssi_base = float(np.percentile(np.asarray(self._rssi_hist), 25))
            self._next_base_t = t + 2.0
        ratio = spread / np.maximum(self._spread_base, 1e-3)
        # movement changes the channel broadly: look at the strongly affected 20 %
        stat = float(np.percentile(ratio, 80))
        rssi_ratio = r_sd / max(self._rssi_base or 0.0, 0.7)
        stat = max(stat, rssi_ratio)
        self._stat_hist.append((t, stat))
        while self._stat_hist and t - self._stat_hist[0][0] > 120.0:
            self._stat_hist.popleft()
        idx = max(stat, 0.0)
        self._motion_index = idx if self._motion_index == 0 else 0.5 * self._motion_index + 0.5 * idx
        warm = len(self._spread_hist) >= 12
        m = self._motion_index
        if not warm:
            self._motion_state = "unknown"
        elif m >= MOVING_AT:
            self._motion_state = "moving"
        elif m >= RESTLESS_AT:
            self._motion_state = "restless"
        else:
            self._motion_state = "still"
        if warm:
            self._shift_events(t, m)

    def _force_motion(self, t: float) -> None:
        if len(self._spread_hist) < 12:
            return
        self._motion_index = max(self._motion_index, MOVING_AT + 1.0)
        self._motion_state = "moving"
        self._shift_events(t, self._motion_index)

    def _shift_events(self, t: float, m: float) -> None:
        if m >= MOVING_AT:
            self._last_move_t = t
            self._calm_since = None
            if self._event is None:
                self._event = MotionEvent(t_start=t, peak=m)
            else:
                self._event.peak = max(self._event.peak, m)
        elif self._event is not None:
            if m < SHIFT_END_BELOW:
                if self._calm_since is None:
                    self._calm_since = t
                elif t - self._calm_since >= SHIFT_END_HOLD_S:
                    ev = self._event
                    ev.t_end = self._calm_since
                    if self._last_move_t - ev.t_start + 0.25 >= SHIFT_MIN_S:
                        self.events_out.append(ev)
                        self._weights = None  # new position: relearn which subcarriers breathe
                    self._event = None
                    self._calm_since = None
            else:
                self._calm_since = None

    # -------------------------------------------------------------- live wave

    def advance(self, now: float) -> None:
        """Emit uniform-grid samples (live waveform + movement) up to the newest frame."""
        if len(self._t) < 2:
            return
        t_end = self._t[-1] - 0.05
        if self._grid_next is None or self._grid_next < self._t[0]:
            self._grid_next = math.ceil(max(self._t[0], t_end - 1.0) * self.fs) / self.fs
            self._wave_filter = dsp.StreamingSos(self._bp, self._dim or 1)
        if self._grid_next > t_end:
            return
        n_new = int((t_end - self._grid_next) * self.fs) + 1
        grid = self._grid_next + np.arange(n_new) / self.fs
        # interpolate only from the last few seconds of frames
        k0 = max(0, len(self._t) - int(6 * 60))
        ts = np.fromiter((self._t[i] for i in range(k0, len(self._t))), dtype=np.float64)
        F = np.asarray([self._f[i] for i in range(k0, len(self._f))], dtype=np.float64)
        G = _interp_rows(grid, ts, F)
        for g, row in zip(grid, G):
            y = self._wave_filter.step(row)
            wave = None
            if self._weights is not None and self._wave_scale and len(self._weights) == len(y):
                wave = float(y @ self._weights) / self._wave_scale * 0.5
            self.wave_out.append((float(g), wave, self._motion_index))
        self._grid_next = grid[-1] + 1.0 / self.fs

    def drain_wave(self) -> list[tuple[float, float | None, float]]:
        self.advance(0.0)
        out, self.wave_out = self.wave_out, []
        return out

    def drain_events(self) -> list[MotionEvent]:
        out, self.events_out = self.events_out, []
        return out

    # ---------------------------------------------------------------- analysis

    def compute(self, now: float) -> Metrics:
        """Run the slower analyses. Call about once per second."""
        fps = len(self._arrivals) / 5.0 if self._arrivals else 0.0
        gap = now - self._last_arrival
        if gap > 5.0:
            fps = 0.0
        loss = 0.0
        if len(self._loss_window) >= 2:
            _, r0, l0 = self._loss_window[0]
            _, r1, l1 = self._loss_window[-1]
            got, lost = r1 - r0, l1 - l0
            if got + lost > 0:
                loss = 100.0 * lost / (got + lost)
        if gap > FORGET_AFTER_S and self._t:
            self._new_segment()
        signal = "no_signal" if gap > NO_SIGNAL_S else ("paused" if gap > PAUSED_AFTER_S else "settling")
        m = Metrics(
            t=now,
            signal=signal,
            frames_per_s=fps,
            loss_pct=loss,
            rssi=self._rssi_ema,
            channel=self._channel,
            motion=self._motion_index if gap <= PAUSED_AFTER_S else 0.0,
            motion_state=self._motion_state if gap <= PAUSED_AFTER_S else "unknown",
            gap_s=max(0.0, gap) if self._last_arrival > 0 else 0.0,
            frame_class=self.dominant,
        )
        if len(self._t) < self.fs * 4 or signal == "no_signal":
            return m

        t_end = self._t[-1]
        settled_from = self._last_move_t + SETTLE_AFTER_MOVE_S
        m.settled_s = max(0.0, t_end - max(settled_from, self._segment_start or t_end))
        m.data_quality = min(1.0, fps / 30.0) * (1.0 if gap < PAUSED_AFTER_S else 0.5)
        moving_now = self._motion_state == "moving"
        self._tick += 1
        if not moving_now:
            # The slow analyses alternate between seconds: keeps an old Mac responsive.
            if self._tick % 2 == 0 or self._breath.last is None:
                self._estimate_breath(t_end, settled_from)
            else:
                self._estimate_heart(t_end, settled_from)
        else:
            self._heart_state = "moving"

        fresh = self._breath.last_t > self._last_move_t  # nothing stale after you move
        if self._breath.last is not None and t_end - self._breath.last_t < 30 and fresh:
            m.breath_rate = self._breath.last
            m.breath_conf = self._breath_conf
            m.breath_regularity = self._regularity
        if self._heart.last is not None and t_end - self._heart.last_t < 15:
            m.heart_rate_experimental = self._heart.last
            m.heart_conf = self._heart_conf
            # Never shown as trusted: in testing, one WiFi board's heart rate was
            # too often wrong. It is logged for comparison with a watch; a heart
            # sensor (MediSense Pro) provides the real number.
        m.heart_state = self._heart_state if m.heart_rate is None else "ok"
        m.presence = (t_end - self._last_breath_seen < 45) or (t_end - self._last_move_t < 30)
        span = t_end - (self._segment_start or t_end)
        if signal != "paused":
            m.signal = "ok" if (m.breath_rate is not None or span >= 40) else "settling"
        return m

    def _window(self, t_end: float, length: float, start_min: float):
        """Uniform grid of features over [t_end-length, t_end] (and its gap fraction)."""
        start = max(t_end - length, start_min, self._segment_start or t_end)
        if t_end - start < 8:
            return None, None, 1.0
        ts = np.fromiter(self._t, dtype=np.float64)
        i0 = max(0, int(np.searchsorted(ts, start)) - 1)
        ts = ts[i0:]
        F = np.asarray([self._f[i] for i in range(i0, len(self._f))], dtype=np.float64)
        grid = np.arange(math.ceil(start * self.fs), math.floor(t_end * self.fs) + 1) / self.fs
        if len(grid) < 16 or len(ts) < 8:
            return None, None, 1.0
        G = _interp_rows(grid, ts, F)
        # gap fraction: grid points more than 0.75 s from a real frame
        j = np.searchsorted(ts, grid)
        prev = ts[np.clip(j - 1, 0, len(ts) - 1)]
        nxt = ts[np.clip(j, 0, len(ts) - 1)]
        near = np.minimum(np.abs(grid - prev), np.abs(nxt - grid))
        self._gap_mask = near > 0.75
        gap_frac = float(np.mean(self._gap_mask))
        return grid, G, gap_frac

    @staticmethod
    def _clean(X: np.ndarray) -> np.ndarray:
        X = dsp.detrend(X, axis=0)
        mad = np.median(np.abs(X), axis=0) * 1.4826 + 1e-9
        return np.clip(X, -5 * mad, 5 * mad)

    def _estimate_breath(self, t_end: float, settled_from: float) -> None:
        fs = self.fs
        prev = self._breath.last
        length = float(np.clip(6.0 * 60.0 / prev, 45.0, 150.0)) if prev else 120.0
        grid, G, gap_frac = self._window(t_end, length, settled_from)
        need = 25 if prev else 40  # a first reading needs a longer look
        if grid is None or len(grid) < need * fs or gap_frac > 0.35:
            return
        X = dsp.fft_bandpass(self._clean(G), fs, *BREATH_BAND)
        sd = X.std(axis=0)
        ok = sd > 1e-6
        if ok.sum() < 4:
            return
        Z = np.zeros_like(X)
        Z[:, ok] = X[:, ok] / sd[ok]
        n = len(grid)
        min_lag = int(BREATH_MIN_LAG_S * fs)
        max_lag = int(min(30.0, n / fs / 2.5) * fs)  # at least 2.5 breaths per window
        if max_lag <= min_lag + 2:
            return
        gaps = self._gap_mask
        Z[gaps] = 0.0  # dropout filler carries no information: blank it
        # Period: the autocorrelation averaged over all subcarriers, sign kept.
        # At the true period every breathing subcarrier adds; at half of it the
        # normally-responding ones subtract (out of step), so doubling near a
        # multipath null can't win; at twice it the biased estimate is damped;
        # noise averages towards zero.
        R = dsp.autocorr(Z[:, ok], max_lag) - dsp.bandpass_noise_acf(fs, *BREATH_BAND, n, max_lag)[:, None]
        # Weight subcarriers by how strongly they repeat at all in the
        # breathing range, so the few that carry the breath aren't drowned.
        h = R[min_lag:max_lag + 1].max(axis=0)
        w = np.clip(h - 0.15, 0.0, None) ** 2
        if w.sum() <= 1e-9:
            self._breath_conf = 0.0
            return
        agg = R @ w / w.sum()
        period_lag, agg_h = _aggregate_period(agg, min_lag, max_lag, floor=0.05, min_score=0.15)
        if period_lag is None:
            self._breath_conf = 0.0
            return
        # Waveform: the subcarrier mix that repeats best at that period.
        found = _periodic_component(Z[:, ok], gaps, period_lag)
        if found is None:
            self._breath_conf = 0.0
            return
        lam, comp, w_ok = found
        support = float(np.clip(agg_h / 0.4, 0.0, 1.0))
        weights = np.zeros(X.shape[1])  # live-wave weights apply to raw features
        weights[ok] = w_ok / sd[ok]
        if self._weights is not None and len(self._weights) == len(weights) and float(weights @ self._weights) < 0:
            weights = -weights
            comp = -comp
        self._weights = weights
        amp = float(np.std(X @ weights)) * math.sqrt(2)
        if amp > 0:
            self._wave_scale = amp if not self._wave_scale else 0.7 * self._wave_scale + 0.3 * amp

        # refine the period on the composite (parabola through its ACF peak)
        r = dsp.autocorr(comp[:, None], max_lag)[:, 0]
        lo, hi = max(min_lag, int(period_lag * 0.85)), min(max_lag, int(period_lag * 1.15) + 1)
        lag = lo + int(np.argmax(r[lo:hi + 1]))
        frac = 0.0
        if 0 < lag < len(r) - 1:
            a, b, c = r[lag - 1], r[lag], r[lag + 1]
            d = a - 2 * b + c
            frac = 0.5 * (a - c) / d if d != 0 else 0.0
        f0 = fs / (lag + frac)
        acf_h = float(r[lag])
        f_sp = _spectral_peak(comp, fs, *BREATH_BAND)
        agree = 0.7
        if f_sp is not None:
            for k in (1, 2, 3, 4):
                if abs(f_sp - k * f0) / (k * f0) < 0.1:
                    agree = 1.0
                    if k == 1:
                        f0 = 0.5 * (f0 + f_sp)
                    break
        # breath-by-breath intervals from the composite waveform
        period = fs / f0
        pk = dsp.find_peaks_simple(comp, max(2, int(0.6 * period)), 0.3 * float(np.std(comp)))
        reg = None
        if len(pk) >= 4:
            iv = np.diff(pk) / fs
            f_td = 1.0 / float(np.median(iv))
            if abs(f_td - f0) / f0 > 0.2:
                agree *= 0.7
            cv = float(np.std(iv) / max(np.mean(iv), 1e-6))
            reg = max(0.0, min(1.0, 1.0 - cv))
        cycles = f0 * n / fs
        conf = float(np.clip(acf_h, 0, 1)) ** 0.5 * support * agree \
            * (1.0 - gap_frac) * min(1.0, cycles / 3.0)
        self._breath_conf = conf
        if conf >= BREATH_SHOW_CONF and self._breath.offer(f0 * 60.0, conf, t_end):
            self._regularity = reg
            self._last_breath_seen = t_end

    def _estimate_heart(self, t_end: float, settled_from: float) -> None:
        """Heart rate the same way as breathing: the period at which the
        subcarriers repeat (and are out of step at half of it).

        A sharp heartbeat mixed non-linearly with breathing scatters its
        energy over many spectral lines, so spectral peak-picking fails; the
        repetition period survives. Reported only when it holds for a while.
        """
        fs = self.fs
        if t_end - settled_from < 20:
            self._heart_state = "moving"
            return
        grid, G, gap_frac = self._window(t_end, 30.0, settled_from)
        if grid is None or len(grid) < 20 * fs or gap_frac > 0.15:
            self._heart_state = "faint"
            return
        X = dsp.fft_bandpass(self._clean(G), fs, *HEART_BAND)
        sd = X.std(axis=0)
        ok = sd > 1e-6
        if ok.sum() < 4:
            self._heart_state = "faint"
            return
        Z = X[:, ok] / sd[ok]
        Z[self._gap_mask] = 0.0
        n = len(grid)
        min_lag, max_lag = int(fs * 60 / 150), int(fs * 60 / 45)
        R = dsp.autocorr(Z, 2 * max_lag + 2)
        agg = R.mean(axis=1) - dsp.bandpass_noise_acf(fs, *HEART_BAND, n, 2 * max_lag + 2)
        lag, score = _aggregate_period(agg, min_lag, max_lag, floor=0.01, min_score=0.03)
        if lag is None:
            self._heart_state = "faint"
            self._heart_conf = 0.0
            return
        r = agg
        frac = 0.0
        if 0 < lag < len(r) - 1:
            a, b, c = r[lag - 1], r[lag], r[lag + 1]
            d = a - 2 * b + c
            frac = 0.5 * (a - c) / d if d != 0 else 0.0
        f_h = fs / (lag + frac)
        conf = float(np.clip(score / 0.08, 0, 1)) * (1 - gap_frac)
        self._heart_conf = conf
        if conf >= HEART_SHOW_CONF and self._heart.offer(f_h * 60.0, conf, t_end, strong=0.95):
            self._heart_state = "ok"
        else:
            self._heart_state = "faint"


def _interp_rows(grid: np.ndarray, ts: np.ndarray, F: np.ndarray) -> np.ndarray:
    """Linear interpolation of each column of F (rows at times ts) onto `grid`."""
    j = np.clip(np.searchsorted(ts, grid), 1, len(ts) - 1)
    t0, t1 = ts[j - 1], ts[j]
    w = np.clip((grid - t0) / np.maximum(t1 - t0, 1e-9), 0.0, 1.0)[:, None]
    return F[j - 1] * (1 - w) + F[j] * w


def _parabolic(freqs: np.ndarray, p: np.ndarray, i: int) -> float:
    if 0 < i < len(p) - 1 and p[i - 1] > 0 and p[i] > 0 and p[i + 1] > 0:
        a, b, c = np.log(p[i - 1]), np.log(p[i]), np.log(p[i + 1])
        d = a - 2 * b + c
        if d != 0:
            return float(freqs[i] + 0.5 * (a - c) / d * (freqs[1] - freqs[0]))
    return float(freqs[i])


def _spectral_peak(x: np.ndarray, fs: float, lo: float, hi: float) -> float | None:
    n = len(x)
    nfft = max(8192, 1 << (n - 1).bit_length())
    p = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n), nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1 / fs)
    band = np.flatnonzero((f >= lo) & (f <= hi))
    if not len(band):
        return None
    i = band[p[band].argmax()]
    return _parabolic(f, p, i)


def _aggregate_period(agg: np.ndarray, min_lag: int, max_lag: int, floor: float = 0.02,
                      min_score: float = 0.05) -> tuple[int | None, float]:
    """Breathing period from the subcarrier-averaged autocorrelation (filter ripple removed).

    A real breathing period P shows a peak at P and a clear dip at P/2,
    where the breath is out of step with itself. A noise bump has no dip at
    its half; a multiple 2T has a peak (T), not a dip, at its half; a
    doubling artefact at T/2 has nothing at T/4. So each candidate is scored
    by peak minus half-period dip, and the earliest strong one wins.
    """
    cands = [i for i in range(min_lag + 1, max_lag) if agg[i] > agg[i - 1] and agg[i] >= agg[i + 1] and agg[i] >= floor]
    scores = {}
    for p in cands:
        lo, hi = max(1, int(0.4 * p)), max(2, int(0.6 * p))
        trough = float(agg[lo:hi + 1].min())
        if trough > -0.01:
            continue
        scores[p] = float(agg[p]) - trough
    if not scores:
        return None, 0.0
    g = max(scores.values())
    if g < min_score:
        return None, g
    lag = min(p for p, sc in scores.items() if sc >= 0.8 * g)
    h = float(agg[lag])
    two = 2 * lag
    if two <= max_lag:
        lo, hi = int(two * 0.95), min(max_lag, int(two * 1.05) + 1)
        if agg[lo:hi + 1].max() < 0.3 * h:  # must repeat a second time
            return None, h
    return lag, scores[lag]


def _periodic_component(Z: np.ndarray, gaps: np.ndarray, lag: int, k: int = 10):
    """The subcarrier mix that repeats best at `lag` (periodic component analysis).

    Largest eigenvector of the symmetrised lag covariance of the whitened
    top-k principal components. Returns (correlation, composite, weights on
    Z's columns) or None.
    """
    n, D = Z.shape
    rows = ~gaps
    if rows.sum() < 0.6 * n or D < 3 or lag >= n - 1:
        return None
    _, S, Vt = np.linalg.svd(Z[rows] - Z[rows].mean(axis=0), full_matrices=False)
    k = min(k, len(S))
    if S[0] <= 0:
        return None
    proj = Vt[:k].T / S[:k] * math.sqrt(rows.sum())  # whitening projection
    Y = Z @ proj
    Y[gaps] = 0.0
    a, b = Y[:-lag], Y[lag:]
    C = a.T @ b / max(1, len(a))
    Cs = 0.5 * (C + C.T)
    vals, vecs = np.linalg.eigh(Cs)
    v = vecs[:, -1]
    comp = Y @ v
    comp = comp / (np.std(comp[rows]) + 1e-12)
    return float(vals[-1]), comp, proj @ v
