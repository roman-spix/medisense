"""Turns a stream of CSI frames into breathing, heart rate and movement.

Pipeline (all on the Mac, nothing on the board):

1. Each frame -> amplitude of the 64 LLTF subcarriers, divided by the frame's
   mean amplitude. That removes the radio's automatic gain changes and makes
   beacons and data frames comparable.
2. Frames arrive irregularly (~25-50 per second). They are averaged into a
   uniform 20 Hz timeline; short gaps are filled, long gaps restart the
   analysis.
3. Movement: how much the subcarrier pattern changes from one sample to the
   next, relative to the quiet baseline of the last two minutes.
4. Breathing: band-pass 0.07-0.75 Hz (4-45 breaths/min), the subcarriers'
   common component (PCA), then the dominant frequency, cross-checked by
   counting breaths in the waveform. A live waveform is produced causally, so
   it lags reality by well under a second.
5. Heart rate: same idea in 0.8-2.2 Hz with breathing harmonics removed. With
   one board this is a rough estimate and is reported with its confidence.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import dsp
from .protocol import LLTF_SUBCARRIERS, CsiFrame

FS = 20.0  # analysis sample rate (Hz)

BREATH_BAND = (0.07, 0.75)  # Hz -> 4.2 .. 45 breaths/min
HEART_BAND = (0.8, 2.2)  # Hz -> 48 .. 132 beats/min
BREATH_WINDOW_S = 45.0
BREATH_MIN_S = 20.0
HEART_WINDOW_S = 24.0
HEART_MIN_S = 16.0
HISTORY_S = 90.0
MAX_FILL_GAP_S = 1.0  # longer gaps restart the analysis
NO_SIGNAL_S = 3.0

# Movement thresholds, in multiples of the quiet baseline.
RESTLESS_AT = 2.2
MOVING_AT = 5.0
SHIFT_MIN_S = 0.5
SHIFT_END_BELOW = 2.5
SHIFT_END_HOLD_S = 2.0

BREATH_SHOW_CONF = 0.25
HEART_SHOW_CONF = 0.12


@dataclass
class Metrics:
    """One snapshot, produced once per second."""

    t: float
    signal: str  # "ok", "settling", "no_signal"
    frames_per_s: float
    loss_pct: float
    rssi: float | None
    channel: int | None
    breath_rate: float | None = None  # breaths per minute
    breath_conf: float = 0.0
    breath_regularity: float | None = None  # 0..1 (1 = perfectly even breaths)
    heart_rate: float | None = None  # beats per minute (rough)
    heart_conf: float = 0.0
    motion: float = 0.0  # multiple of quiet baseline
    motion_state: str = "unknown"  # still / restless / moving / unknown
    presence: bool = False
    settled_s: float = 0.0  # seconds since the last big movement

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for k in ("breath_rate", "heart_rate", "rssi", "breath_regularity"):
            if d[k] is not None:
                d[k] = round(float(d[k]), 2)
        for k in ("breath_conf", "heart_conf", "motion", "frames_per_s", "loss_pct", "settled_s"):
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
class _Tracker:
    """Median-smooths accepted estimates and remembers the last good value."""

    size: int
    values: deque = field(default_factory=deque)
    last: float | None = None
    last_t: float = -1e9

    def push(self, v: float, t: float) -> float:
        self.values.append(v)
        while len(self.values) > self.size:
            self.values.popleft()
        self.last = float(np.median(self.values))
        self.last_t = t
        return self.last

    def clear(self) -> None:
        self.values.clear()
        self.last = None


class VitalsEngine:
    def __init__(self, fs: float = FS):
        self.fs = fs
        n_hist = int(HISTORY_S * fs)
        self._x: deque[np.ndarray] = deque(maxlen=n_hist)  # normalised amplitudes
        self._t: deque[float] = deque(maxlen=n_hist)
        self._moving: deque[bool] = deque(maxlen=n_hist)
        self._diff_hist: deque[float] = deque(maxlen=int(120 * fs))

        self._bin_idx: int | None = None
        self._bin_sum = np.zeros(LLTF_SUBCARRIERS)
        self._bin_n = 0
        self._raw_ema: np.ndarray | None = None
        self.mask = np.ones(LLTF_SUBCARRIERS, dtype=bool)

        self._bp = dsp.butter_bandpass(*BREATH_BAND, fs, order=2)
        self._hp = dsp.butter_bandpass(*HEART_BAND, fs, order=2)
        self._wave_filter = dsp.StreamingSos(self._bp, LLTF_SUBCARRIERS)
        self._breath_w: np.ndarray | None = None
        self._wave_ref: np.ndarray | None = None
        self._wave_scale: float | None = None

        self._motion_smooth = 0.0
        self._motion_index = 0.0
        self._motion_state = "unknown"
        self._last_move_t = -1e9
        self._event: MotionEvent | None = None
        self._calm_since: float | None = None
        self.events_out: list[MotionEvent] = []
        # (t, breath wave or None while it is still being learned, motion)
        self.wave_out: list[tuple[float, float | None, float]] = []

        self._breath = _Tracker(5)
        self._heart = _Tracker(7)
        self._breath_conf = 0.0
        self._heart_conf = 0.0
        self._regularity: float | None = None
        self._last_breath_seen = -1e9

        # Link statistics
        self._frame_times: deque[float] = deque()
        self._last_seq: int | None = None
        self._lost = 0
        self._received = 0
        self._loss_window: deque[tuple[float, int, int]] = deque()
        self._rssi_ema: float | None = None
        self._channel: int | None = None
        self._last_frame_t = -1e9
        self._segment_start: float | None = None

    # ------------------------------------------------------------------ input

    def add_frame(self, t: float, frame: CsiFrame) -> None:
        """Feed one frame; `t` is the host's monotonic arrival time in seconds."""
        amp = frame.amplitudes(LLTF_SUBCARRIERS)
        if len(amp) < LLTF_SUBCARRIERS:
            return
        self._link_stats(t, frame)
        if self._raw_ema is None:
            self._raw_ema = amp.astype(np.float64).copy()
        else:
            self._raw_ema += 0.01 * (amp - self._raw_ema)
        # Normalise by the mean over all subcarriers (not just the masked
        # ones) so the scale never jumps when the mask is refined.
        level = float(amp.mean())
        if level <= 0:
            return
        x = amp / level

        k = int(math.floor(t * self.fs))
        if self._bin_idx is None or (k - self._bin_idx) > MAX_FILL_GAP_S * self.fs:
            if self._bin_idx is not None and self._bin_n:
                self._emit(self._bin_idx)
            self._restart()
            self._bin_idx = k
        elif k > self._bin_idx:
            self._flush_until(k)
        self._bin_sum += x
        self._bin_n += 1
        self._last_frame_t = t

    def _link_stats(self, t: float, frame: CsiFrame) -> None:
        self._frame_times.append(t)
        while self._frame_times and t - self._frame_times[0] > 5.0:
            self._frame_times.popleft()
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

    def _flush_until(self, k: int) -> None:
        assert self._bin_idx is not None
        if self._bin_n:
            self._emit(self._bin_idx)
            last = self._x[-1]
            for j in range(self._bin_idx + 1, k):  # fill short gaps
                self._emit(j, fill=last)
        self._bin_idx = k

    def _restart(self) -> None:
        """Start a fresh analysis segment (first frame or after a long gap)."""
        self._x.clear()
        self._t.clear()
        self._moving.clear()
        self._bin_sum[:] = 0
        self._bin_n = 0
        self._wave_filter.reset()
        self._wave_ref = None
        self._breath.clear()
        self._heart.clear()
        self._breath_conf = self._heart_conf = 0.0
        self._regularity = None
        self._segment_start = None

    def _emit(self, k: int, fill: np.ndarray | None = None) -> None:
        if fill is None:
            x = self._bin_sum / max(1, self._bin_n)
            self._bin_sum = np.zeros(LLTF_SUBCARRIERS)
            self._bin_n = 0
        else:
            x = fill
        t = k / self.fs
        if self._segment_start is None:
            self._segment_start = t
        prev = self._x[-1] if self._x else None
        self._x.append(x)
        self._t.append(t)

        # Movement
        if prev is not None:
            d = float(np.mean(np.abs(x - prev)[self.mask]))
            self._diff_hist.append(d)
            self._motion_smooth += 0.15 * (d - self._motion_smooth)
            base = self._motion_baseline()
            self._motion_index = self._motion_smooth / base if base > 0 else 0.0
            self._update_motion_state(t)
        self._moving.append(self._motion_state == "moving")

        # Live breathing waveform (causal). Filtering the change from the
        # segment's first sample avoids a start-up jolt from the high-pass.
        if self._wave_ref is None:
            self._wave_ref = x.copy()
        y = self._wave_filter.step(x - self._wave_ref)
        wave = None
        if self._breath_w is not None and self._wave_scale:
            wave = float(y[self.mask] @ self._breath_w) / self._wave_scale * 0.5
        self.wave_out.append((t, wave, self._motion_index))

    def _motion_baseline(self) -> float:
        if len(self._diff_hist) < self.fs * 5:
            return float(np.median(self._diff_hist)) if self._diff_hist else 0.0
        arr = np.fromiter(self._diff_hist, dtype=np.float64)
        return float(np.percentile(arr, 25))

    def _update_motion_state(self, t: float) -> None:
        m = self._motion_index
        if len(self._diff_hist) < self.fs * 5:
            self._motion_state = "unknown"
            return
        if m >= MOVING_AT:
            self._motion_state = "moving"
        elif m >= RESTLESS_AT:
            self._motion_state = "restless"
        else:
            self._motion_state = "still"

        # Shift events: big movement lasting at least SHIFT_MIN_S.
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
                    if self._last_move_t - ev.t_start + 1 / self.fs >= SHIFT_MIN_S:
                        self.events_out.append(ev)
                    self._event = None
                    self._calm_since = None
            else:
                self._calm_since = None

    # ---------------------------------------------------------------- output

    def drain_wave(self) -> list[tuple[float, float | None, float]]:
        out, self.wave_out = self.wave_out, []
        return out

    def drain_events(self) -> list[MotionEvent]:
        out, self.events_out = self.events_out, []
        return out

    def compute(self, now: float) -> Metrics:
        """Run the slower analyses. Call about once per second."""
        fps = len(self._frame_times) / 5.0 if self._frame_times else 0.0
        if self._frame_times and now - self._frame_times[-1] > 5.0:
            fps = 0.0
        loss = 0.0
        if len(self._loss_window) >= 2:
            _, r0, l0 = self._loss_window[0]
            _, r1, l1 = self._loss_window[-1]
            got, lost = r1 - r0, l1 - l0
            if got + lost > 0:
                loss = 100.0 * lost / (got + lost)

        no_signal = now - self._last_frame_t > NO_SIGNAL_S
        if no_signal and self._bin_idx is not None:
            if self._bin_n:
                self._emit(self._bin_idx)
            self._bin_idx = None
            self._restart()

        m = Metrics(
            t=now,
            signal="no_signal" if no_signal else "settling",
            frames_per_s=fps,
            loss_pct=loss,
            rssi=self._rssi_ema,
            channel=self._channel,
            motion=self._motion_index if not no_signal else 0.0,
            motion_state=self._motion_state if not no_signal else "unknown",
        )
        if no_signal or len(self._x) < self.fs * 4:
            return m

        self._update_mask()
        X = np.asarray(self._x)[:, self.mask]
        T = np.asarray(self._t)
        moving = np.asarray(self._moving)
        t_now = T[-1]
        settled_from = self._last_move_t + 3.0
        m.settled_s = max(0.0, t_now - max(settled_from, T[0]))

        self._estimate_breath(X, T, moving, t_now, settled_from)
        self._estimate_heart(X, T, t_now, settled_from)

        if self._breath.last is not None and t_now - self._breath.last_t < 30:
            m.breath_rate = self._breath.last
            m.breath_conf = self._breath_conf
            m.breath_regularity = self._regularity
        if self._heart.last is not None and t_now - self._heart.last_t < 30:
            m.heart_rate = self._heart.last
            m.heart_conf = self._heart_conf
        m.presence = (t_now - self._last_breath_seen < 30) or (t_now - self._last_move_t < 30)
        span = t_now - T[0]
        m.signal = "ok" if (m.breath_rate is not None or span >= BREATH_MIN_S + 5) else "settling"
        return m

    def _update_mask(self) -> None:
        """Ignore null/guard subcarriers (they carry no energy)."""
        if self._raw_ema is None:
            return
        med = float(np.median(self._raw_ema))
        if med <= 0:
            return
        mask = self._raw_ema > 0.2 * med
        if mask.sum() >= 8 and not np.array_equal(mask, self.mask):
            self.mask = mask
            self._breath_w = None  # weights were for the old subcarrier set
            self._wave_scale = None

    def _window(self, T: np.ndarray, t_now: float, length: float, settled_from: float) -> slice | None:
        start_t = max(t_now - length, settled_from, T[0])
        i0 = int(np.searchsorted(T, start_t))
        return slice(i0, len(T))

    def _estimate_breath(self, X, T, moving, t_now, settled_from) -> None:
        # Slow breathing needs a longer window: aim for ~6 breaths.
        length = BREATH_WINDOW_S
        if self._breath.last:
            length = min(HISTORY_S - 5, max(BREATH_WINDOW_S, 6 * 60.0 / self._breath.last))
        sl = self._window(T, t_now, length, settled_from)
        n = len(T[sl])
        if n < BREATH_MIN_S * self.fs:
            return
        seg = dsp.filtfilt(self._bp, X[sl], axis=0, pad=int(3 * self.fs))
        sig, w, _frac = dsp.principal_component(seg)
        if self._breath_w is not None and len(self._breath_w) == len(w) and float(w @ self._breath_w) < 0:
            w, sig = -w, -sig
        self._breath_w = w
        amp = float(np.std(sig)) * math.sqrt(2)  # ~ peak amplitude of the breath wave
        if amp > 0:
            self._wave_scale = amp if not self._wave_scale else 0.8 * self._wave_scale + 0.2 * amp

        prefer = self._breath.last / 60.0 if self._breath.last else None
        peak = dsp.spectral_peak(sig, self.fs, *BREATH_BAND, prefer=prefer, prefer_width=0.05)
        if peak is None:
            return
        rate_f = peak.freq * 60.0
        conf = peak.confidence
        cycles = rate_f * (n / self.fs) / 60.0
        if cycles < 2.5:  # not enough breaths in the window to trust the rate
            conf *= 0.3

        # Cross-check by counting breaths in the waveform.
        min_dist = max(2, int(0.55 * self.fs * 60.0 / max(rate_f, 1.0)))
        prom = 0.35 * float(np.std(sig))
        pk = dsp.find_peaks_simple(sig, min_dist, prom)
        reg = None
        if len(pk) >= 3:
            intervals = np.diff(pk) / self.fs
            rate_t = 60.0 / float(np.median(intervals))
            agreement = abs(rate_t - rate_f) / max(rate_f, 1e-6)
            conf *= 1.0 if agreement < 0.15 else (0.6 if agreement < 0.3 else 0.3)
            cv = float(np.std(intervals) / max(np.mean(intervals), 1e-6))
            reg = max(0.0, min(1.0, 1.0 - cv))
        else:
            conf *= 0.3
        self._breath_conf = conf
        if conf >= BREATH_SHOW_CONF:
            self._breath.push(rate_f, t_now)
            self._regularity = reg
            self._last_breath_seen = t_now

    def _estimate_heart(self, X, T, t_now, settled_from) -> None:
        sl = self._window(T, t_now, HEART_WINDOW_S, settled_from)
        n = len(T[sl])
        if n < HEART_MIN_S * self.fs:
            return
        seg = dsp.filtfilt(self._hp, X[sl], axis=0, pad=int(2 * self.fs))
        # Keep the subcarriers whose spectrum is most peaked in the heart band.
        spec = np.abs(np.fft.rfft(seg * np.hanning(n)[:, None], axis=0)) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / self.fs)
        band = (freqs >= HEART_BAND[0]) & (freqs <= HEART_BAND[1])
        sb = spec[band]
        peakiness = sb.max(axis=0) / (sb.mean(axis=0) + 1e-12)
        k = min(12, seg.shape[1])
        top = np.argsort(-peakiness)[:k]
        sig, _w, _ = dsp.principal_component(seg[:, top])

        exclude = []
        fb = self._breath.last / 60.0 if self._breath.last else None
        if fb:
            for h in range(2, 12):
                exclude.append((h * fb - 0.04, h * fb + 0.04))
        prefer = self._heart.last / 60.0 if self._heart.last else None
        peak = dsp.spectral_peak(sig, self.fs, *HEART_BAND, exclude=exclude, prefer=prefer, prefer_width=0.1)
        if peak is None:
            return
        self._heart_conf = peak.confidence
        if peak.confidence >= HEART_SHOW_CONF:
            self._heart.push(peak.freq * 60.0, t_now)
