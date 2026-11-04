"""MediSense Pro: the WiFi board plus a 60 GHz radar and a pulse sensor.

WiFi sensing is good for stillness and (with a decent signal) breathing, but
not for heart rate. Two cheap modules on the same ESP32-S3 fix that:

* a 60 GHz vital-signs radar (HLK-LD6002, or the Seeed MR60BHA2 module) on
  UART: it measures the chest's movement directly, millimetre-precise, and
  reports breathing and heart rate plus the breathing/heartbeat phase;
* an optical pulse sensor (MAX30102 or MAX30101) on I2C, on a fingertip or
  earlobe: every heartbeat, so beat-to-beat intervals and heart-rate
  variability (HRV), the best-studied bodily marker of a calm, regulated state.

The board's firmware (v2) forwards both raw to this app. Here:

  radar bytes  -> TinyFrame frames -> breathing rate, heart rate, presence
  pulse samples -> beats -> heart rate, RMSSD, SDNN, coherence, breathing (RSA)

and the best source wins for each number:

  heart rate:  pulse sensor (finger on, clean beats) > radar > nothing (never WiFi)
  breathing:   radar > WiFi > pulse sensor (from the heart's breathing rhythm)

Run it with `medisense pro` (or `medisense pro --demo` without hardware).
"""

from __future__ import annotations

import math
import struct
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from . import dsp
from .app import MediSenseApp
from .protocol import PPG_MAGIC, RADAR_MAGIC, parse_ppg, parse_radar

# ------------------------------------------------------------------- radar

TF_SOF = 0x01
TYPE_PHASE = 0x0A13
TYPE_BREATH = 0x0A14
TYPE_HEART = 0x0A15
TYPE_DISTANCE = 0x0A16
TYPE_PRESENCE = 0x0F09
TF_MAX_PAYLOAD = 512


def tf_checksum(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return (~x) & 0xFF


def tf_frame(frame_type: int, payload: bytes, frame_id: int = 0) -> bytes:
    """Build one TinyFrame frame (used by the simulator and tests)."""
    head = struct.pack(">BHHH", TF_SOF, frame_id & 0xFFFF, len(payload), frame_type)
    out = head + bytes([tf_checksum(head)]) + payload
    if payload:
        out += bytes([tf_checksum(payload)])
    else:
        out += bytes([tf_checksum(b"")])
    return out


class TinyFrameParser:
    """Byte stream -> (type, payload) frames. Header and data checksums = ~XOR."""

    def __init__(self):
        self.buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(bytes([TF_SOF]))
            if i < 0:
                self.buf.clear()
                return out
            if i:
                del self.buf[:i]
            if len(self.buf) < 8:
                return out
            head = bytes(self.buf[:8])
            if tf_checksum(head[:7]) != head[7]:
                self.errors += 1
                del self.buf[:1]
                continue
            _, _, length, ftype = struct.unpack(">BHHH", head[:7])
            if length > TF_MAX_PAYLOAD:
                self.errors += 1
                del self.buf[:1]
                continue
            total = 8 + length + 1
            if len(self.buf) < total:
                return out
            payload = bytes(self.buf[8:8 + length])
            if tf_checksum(payload) != self.buf[8 + length]:
                self.errors += 1
                del self.buf[:1]
                continue
            out.append((ftype, payload))
            del self.buf[:total]


class RadarState:
    """Latest readings from a TinyFrame vital-signs radar."""

    VALID_S = 5.0

    def __init__(self):
        self.parser = TinyFrameParser()
        self.breath: float | None = None
        self.breath_t = -1e9
        self.heart: float | None = None
        self.heart_t = -1e9
        self.presence: bool | None = None
        self.distance_cm: float | None = None
        self.last_t = -1e9
        self.frames = 0
        self.kind: int | None = None
        self.baud: int | None = None
        self.phase: deque = deque(maxlen=600)  # (t, breath phase, heart phase)

    def feed(self, t: float, raw: bytes) -> None:
        for ftype, p in self.parser.feed(raw):
            self.frames += 1
            self.last_t = t
            if ftype == TYPE_BREATH and len(p) >= 4:
                v = struct.unpack_from("<f", p)[0]
                if math.isfinite(v) and 0.0 <= v <= 60.0:
                    self.breath, self.breath_t = v, t
            elif ftype == TYPE_HEART and len(p) >= 4:
                v = struct.unpack_from("<f", p)[0]
                if math.isfinite(v) and 0.0 <= v <= 250.0:
                    self.heart, self.heart_t = v, t
            elif ftype == TYPE_DISTANCE and len(p) >= 8:
                flag, dist = struct.unpack_from("<If", p)
                self.distance_cm = (dist * 100.0 if dist < 5.0 else dist) if flag and math.isfinite(dist) else None
            elif ftype == TYPE_PRESENCE and len(p) >= 1:
                self.presence = p[0] != 0
            elif ftype == TYPE_PHASE and len(p) >= 12:
                _total, br_ph, hr_ph = struct.unpack_from("<fff", p)
                self.phase.append((t, br_ph, hr_ph))

    def connected(self, now: float) -> bool:
        return now - self.last_t < 3.0

    def breath_rate(self, now: float) -> float | None:
        ok = self.breath is not None and 3.0 <= self.breath <= 40.0 and now - self.breath_t < self.VALID_S
        return self.breath if ok and self.presence is not False else None

    def heart_rate(self, now: float) -> float | None:
        ok = self.heart is not None and 40.0 <= self.heart <= 150.0 and now - self.heart_t < self.VALID_S
        return self.heart if ok and self.presence is not False else None


# ------------------------------------------------------------- pulse sensor

FINGER_DC = 30000.0  # IR level (18-bit counts at ~7 mA) when a finger covers the sensor


@dataclass
class HrvSnapshot:
    heart_rate: float | None = None
    rmssd_ms: float | None = None
    sdnn_ms: float | None = None
    coherence: float | None = None  # 0..1: share of HRV power in one slow rhythm
    resonance_bpm: float | None = None  # that rhythm, per minute (usually the breathing)
    quality: float = 0.0  # 0..1: share of recent beats accepted
    beats: int = 0


class PpgProcessor:
    """Red/IR samples -> heartbeats -> heart rate and heart-rate variability.

    Timing comes from the board's own sample clock (exact 100 Hz spacing),
    so beat-to-beat intervals don't suffer from WiFi jitter. A beat's moment
    is the steepest point of the pulse's rise (the most precise fiducial),
    refined between samples.
    """

    def __init__(self, fs: float = 100.0):
        self.fs = fs
        self._bp = dsp.StreamingSos(dsp.butter_bandpass(0.6, 4.0, fs, order=2), 1)
        self._ir_dc: float | None = None
        self._y_prev: float | None = None
        self._d1: float | None = None  # slope one sample ago
        self._d2: float | None = None  # slope two samples ago
        self._hist: deque = deque()  # (t, slope) over the last 3 s: the adaptive threshold
        self._last_beat_t: float | None = None
        self._last_raw_beat_t: float | None = None
        self.beats: deque = deque()  # (t, ibi_s, accepted)
        self.finger = False
        self._t_base: float | None = None
        self._last_dev = None
        self._wraps = 0
        self.samples = 0
        self.wave: deque = deque(maxlen=400)  # (t, filtered value) for the page
        self._skip_until = 0.0

    # -- input -------------------------------------------------------------

    def _dev_time(self, ts_us: int) -> float:
        if self._last_dev is not None and ts_us < self._last_dev and self._last_dev - ts_us > 2 ** 31:
            self._wraps += 1
        self._last_dev = ts_us
        return (ts_us + self._wraps * 2 ** 32) / 1e6

    def feed_batch(self, dev_ts_us: int, red, ir, rate: float | None = None) -> None:
        fs = rate or self.fs
        t_last = self._dev_time(dev_ts_us)
        n = len(ir)
        for i in range(n):
            self.feed_sample(t_last - (n - 1 - i) / fs, float(red[i]), float(ir[i]))

    def feed_sample(self, t: float, red: float, ir: float) -> None:
        self.samples += 1
        self._ir_dc = ir if self._ir_dc is None else self._ir_dc + 0.02 * (ir - self._ir_dc)
        finger = self._ir_dc > FINGER_DC
        if finger != self.finger:
            # a finger just landed (or left): restart the filter and beat search
            self._bp.reset(1)
            self._y_prev = self._d1 = self._d2 = None
            self._last_raw_beat_t = None
            self._skip_until = t + 2.0
        self.finger = finger
        if not finger:
            return
        # more blood -> more light absorbed -> lower IR reading: invert it
        y = float(self._bp.step(np.array([-ir]))[0])
        self.wave.append((t, y))
        if self._y_prev is None:
            self._y_prev = y
            return
        d = (y - self._y_prev) * self.fs  # slope of the pulse wave
        self._y_prev = y
        self._hist.append((t, d))
        while self._hist and t - self._hist[0][0] > 3.0:
            self._hist.popleft()
        d1, d2 = self._d1, self._d2
        self._d2, self._d1 = d1, d
        if t < self._skip_until or d1 is None or d2 is None:
            return
        # the steepest point of the rise (a local maximum of the slope), if
        # it is a real upstroke: well above the recent slopes
        if d1 > d2 and d1 >= d and d1 > 0:
            if d1 > 0.45 * max(v for _, v in self._hist):
                denom = d2 - 2 * d1 + d
                frac = 0.5 * (d2 - d) / denom if denom != 0 else 0.0
                self._on_beat(t - 1.0 / self.fs + max(-0.5, min(0.5, frac)) / self.fs)

    def _on_beat(self, tb: float) -> None:
        if self._last_raw_beat_t is not None and tb - self._last_raw_beat_t < 0.33:
            return  # refractory: at most 180 per minute
        last = self._last_raw_beat_t
        self._last_raw_beat_t = tb
        if last is None:
            return
        ibi = tb - last
        recent = [b[1] for b in self.beats if b[2]][-8:]
        ok = 0.33 <= ibi <= 1.6
        if ok and len(recent) >= 4:
            med = float(np.median(recent))
            ok = abs(ibi - med) <= 0.3 * med
        self.beats.append((tb, ibi, ok))
        while self.beats and tb - self.beats[0][0] > 120.0:
            self.beats.popleft()

    # -- output ------------------------------------------------------------

    def snapshot(self, window_s: float = 60.0) -> HrvSnapshot:
        snap = HrvSnapshot()
        if not self.finger or not self.beats:
            return snap
        t_end = self.beats[-1][0]
        recent = [b for b in self.beats if t_end - b[0] <= window_s]
        acc = [b for b in recent if b[2]]
        snap.beats = len(acc)
        snap.quality = len(acc) / len(recent) if recent else 0.0
        last8 = [b[1] for b in acc[-8:]]
        if len(last8) >= 4:
            snap.heart_rate = 60.0 / float(np.median(last8))
        # successive differences only between consecutive accepted beats
        ibis = [b[1] for b in acc]
        if len(ibis) >= 20:
            pairs = [(a, b) for a, b in zip(recent, recent[1:]) if a[2] and b[2]]
            diffs = np.array([b[1] - a[1] for a, b in pairs])
            if len(diffs) >= 15:
                snap.rmssd_ms = float(np.sqrt(np.mean(diffs ** 2)) * 1000.0)
            snap.sdnn_ms = float(np.std(ibis) * 1000.0)
        coh = self._coherence(acc)
        if coh:
            snap.coherence, snap.resonance_bpm = coh
        return snap

    @staticmethod
    def _coherence(acc) -> tuple[float, float] | None:
        """Share of the heart-rhythm variability in one slow rhythm (0.04-0.26 Hz).

        A tachogram (beat-to-beat intervals over time) resampled at 4 Hz, last
        64 s. Slow, even breathing makes the heart speed up and slow down with
        each breath (respiratory sinus arrhythmia): one strong peak, i.e. high
        coherence, at the breathing frequency.
        """
        if len(acc) < 40 or acc[-1][0] - acc[0][0] < 50:
            return None
        t = np.array([b[0] for b in acc])
        v = np.array([b[1] for b in acc])
        grid = np.arange(t[-1] - 64.0, t[-1], 0.25)
        grid = grid[grid >= t[0]]
        if len(grid) < 160:
            return None
        x = np.interp(grid, t, v)
        x = x - np.polyval(np.polyfit(np.arange(len(x)), x, 1), np.arange(len(x)))
        x = x * np.hanning(len(x))
        spec = np.abs(np.fft.rfft(x)) ** 2
        f = np.fft.rfftfreq(len(x), 0.25)
        band = (f >= 0.04) & (f <= 0.26)
        total_band = (f >= 0.0033) & (f <= 0.4)
        if not band.any() or spec[total_band].sum() <= 0:
            return None
        k = int(np.flatnonzero(band)[np.argmax(spec[band])])
        f_peak = f[k]
        near = (f >= f_peak - 0.015) & (f <= f_peak + 0.015)
        coh = float(spec[near].sum() / spec[total_band].sum())
        return min(1.0, coh), float(f_peak * 60.0)


# ------------------------------------------------------------------ the app

class ProApp(MediSenseApp):
    """MediSense with the radar and pulse sensor (same page, more cards)."""

    pro = True

    def __init__(self, demo: bool = False, **kw):
        super().__init__(demo=demo, **kw)
        self.radar = RadarState()
        self.ppg = PpgProcessor()
        self.ppg_seen = -1e9
        self._ppg_push = 0.0
        self._ppg_offset: float | None = None
        self._ppg_sent = -1e9
        self._hrv = HrvSnapshot()
        self._hrv_t = -1e9

    async def start(self) -> None:
        await super().start()
        if self.demo:
            import asyncio

            from .simulator import run_sensors_udp

            self._tasks.append(asyncio.create_task(run_sensors_udp("127.0.0.1", self.udp_port, stop=self.demo_stop)))

    def on_other_packet(self, kind: int | None, data: bytes, now: float) -> None:
        if kind == RADAR_MAGIC:
            r = parse_radar(data)
            if r:
                self.radar.kind, self.radar.baud = r.radar_type, r.baud
                self.radar.feed(now, r.data)
        elif kind == PPG_MAGIC:
            p = parse_ppg(data)
            if p:
                self.ppg_seen = now
                self.ppg.feed_batch(p.dev_ts_us, p.red, p.ir, p.rate_hz)
                if now - self._ppg_push >= 0.1 and self.clients and self.ppg.wave:
                    self._ppg_push = now
                    import asyncio

                    # the board's sample clock, placed once on the app's clock (re-placed if it drifts)
                    t_dev = self.ppg.wave[-1][0]
                    if self._ppg_offset is None or abs(now - self.started - (t_dev + self._ppg_offset)) > 1.0:
                        self._ppg_offset = now - self.started - t_dev
                    new = [(t, v) for t, v in self.ppg.wave if t > self._ppg_sent][::2]  # 50 Hz is plenty to draw
                    if new:
                        self._ppg_sent = new[-1][0]
                        asyncio.get_running_loop().create_task(self.broadcast({
                            "type": "ppg", "finger": self.ppg.finger,
                            "samples": [[round(t + self._ppg_offset, 3), round(v, 2), 0] for t, v in new],
                        }))

    def extra_metrics(self, now: float, d: dict) -> None:
        if now - self._hrv_t >= 1.0:
            self._hrv_t = now
            self._hrv = self.ppg.snapshot() if now - self.ppg_seen < 3 else HrvSnapshot()
        h = self._hrv
        hr, src = None, None
        if h.heart_rate is not None and h.quality >= 0.6:
            hr, src = h.heart_rate, "pulse sensor"
        elif self.radar.heart_rate(now) is not None:
            hr, src = self.radar.heart_rate(now), "radar"
        wifi_br = d.get("breath_rate")
        br, bsrc = None, None
        if self.radar.breath_rate(now) is not None:
            br, bsrc = self.radar.breath_rate(now), "radar"
        elif wifi_br is not None:
            br, bsrc = wifi_br, "wifi"
        elif h.resonance_bpm is not None and (h.coherence or 0) >= 0.3:
            br, bsrc = h.resonance_bpm, "pulse"
        d.update({
            "sensor_hr": None if hr is None else round(hr, 1),
            "sensor_hr_source": src,
            "sensor_br": self.radar.breath_rate(now),
            "breath_rate_wifi": wifi_br,
            "breath_rate": None if br is None else round(br, 2),
            "breath_source": bsrc,
            "hrv_rmssd": None if h.rmssd_ms is None else round(h.rmssd_ms, 1),
            "hrv_sdnn": None if h.sdnn_ms is None else round(h.sdnn_ms, 1),
            "coherence": None if h.coherence is None else round(h.coherence, 3),
            "pulse_quality": round(h.quality, 2),
            "finger_on": self.ppg.finger and now - self.ppg_seen < 3,
            "radar_connected": self.radar.connected(now),
            "radar_present": self.radar.presence,
            "radar_distance_cm": None if self.radar.distance_cm is None else round(self.radar.distance_cm, 0),
        })
        if br is not None and wifi_br is None:
            d["breath_conf"] = max(d.get("breath_conf") or 0.0, 0.8)

    def status(self) -> dict:
        st = super().status()
        now = time.monotonic()
        st["pro"] = {
            "radar": {"connected": self.radar.connected(now), "frames": self.radar.frames,
                      "type": {1: "TinyFrame (LD6002 / MR60BHA2)", 2: "LD2410"}.get(self.radar.kind, None),
                      "baud": self.radar.baud, "errors": self.radar.parser.errors},
            "pulse": {"connected": now - self.ppg_seen < 3, "finger": self.ppg.finger, "samples": self.ppg.samples},
        }
        return st
