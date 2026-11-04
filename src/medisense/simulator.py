"""A pretend board: produces CSI packets like the real firmware would.

Used by demo mode (try the app before the hardware arrives) and by the
tests (check that the analysis recovers known breathing and heart rates).

The model is physical rather than cosmetic, because the first real-world
test showed how kind a cosmetic model is:

* the channel is a sum of propagation paths (router → board directly, a few
  wall reflections, and one path that bounces off the chest); breathing and
  the heartbeat change the chest path's length by millimetres, so every
  subcarrier's amplitude *and* phase moves differently, sometimes
  non-linearly;
* breathing is shaped like real slow breathing (inhale, longer exhale, a
  pause), which puts strong harmonics at 2x and 3x the true rate;
* frames arrive irregularly; with the original firmware every lost ping
  reply stalls the stream for a full second; there are longer outages;
* beacons (a different transmit chain, lower quality) are mixed with the
  ping replies; the original firmware also lets through router frames meant
  for other devices;
* the radio adds automatic gain jumps, a random phase per frame, a timing
  slope across subcarriers, noise and 8-bit quantisation;
* the person shifts position now and then, and can get up and leave.
"""

from __future__ import annotations

import asyncio
import math
import socket
import struct
import time
from dataclasses import dataclass, field

import numpy as np

from .protocol import FLAG_TO_ME, HTLTF_INDEX, LLTF_INDEX, build_csi

C = 3e8
FC = 2.437e9  # channel 6
DF = 312.5e3


@dataclass
class Scenario:
    """What the pretend meditator (and the radio) does."""

    breath_start_bpm: float = 13.0
    breath_end_bpm: float = 6.0
    ramp_s: float = 600.0
    heart_bpm: float = 66.0
    hrv_bpm: float = 3.0  # respiratory sinus arrhythmia amplitude
    breath_mm: float = 5.0  # chest displacement (peak)
    heart_mm: float = 0.25
    body_gain: float = 0.35  # strength of the chest reflection vs the direct path
    snr_db: float = 24.0
    frame_rate: float = 45.0  # frames/s offered by the radio
    firmware: str = "new"  # "new" (MediSense patches) or "old" (RuView 0.8.8)
    loss: float = 0.03  # lost ping replies (old firmware: each stalls ~1 s)
    beacon_share: float = 0.15
    foreign_share: float = 0.10  # old firmware only: router frames meant for others
    outages: list[tuple[float, float]] = field(default_factory=list)  # (start, length)
    shifts_at: list[float] = field(default_factory=lambda: [95.0, 400.0, 760.0])
    shift_len_s: float = 2.5
    away: list[tuple[float, float]] = field(default_factory=list)  # (start, length) out of the seat
    seed: int = 7

    def breath_bpm(self, t: float) -> float:
        a = min(1.0, max(0.0, t / self.ramp_s)) if self.ramp_s > 0 else 1.0
        return self.breath_start_bpm + (self.breath_end_bpm - self.breath_start_bpm) * a

    def moving(self, t: float) -> bool:
        return any(s <= t < s + self.shift_len_s for s in self.shifts_at) or \
            any(s <= t < s + 2.0 or s + d - 2.0 <= t < s + d for s, d in self.away)

    def present(self, t: float) -> bool:
        return not any(s + 2.0 <= t < s + d - 2.0 for s, d in self.away)

    def outage(self, t: float) -> bool:
        return any(s <= t < s + d for s, d in self.outages)


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def breath_shape(phase: float) -> float:
    """0..1 chest position over one breath: inhale 35 %, exhale 45 %, pause 20 %."""
    p = phase % 1.0
    if p < 0.35:
        return float(_smoothstep(p / 0.35))
    if p < 0.80:
        return float(1.0 - _smoothstep((p - 0.35) / 0.45))
    return 0.0


def heart_shape(phase: float) -> float:
    """Sharp systolic push followed by a smaller rebound."""
    p = phase % 1.0
    return math.exp(-((p - 0.15) / 0.06) ** 2) + 0.35 * math.exp(-((p - 0.45) / 0.10) ** 2)


class SyntheticBody:
    def __init__(self, scenario: Scenario | None = None):
        self.sc = scenario or Scenario()
        r = self.rng = np.random.default_rng(self.sc.seed)
        k = np.arange(-32, 32)
        self.freqs = FC + k * DF  # all 64 subcarrier frequencies, index = k + 32
        # Static paths: direct + 4 reflections (delays in metres of extra path)
        self.static_len = np.array([3.0, 4.6, 6.1, 7.9, 10.4]) + r.uniform(0, 0.5, 5)
        self.static_amp = np.array([1.0, 0.55, 0.4, 0.3, 0.2]) * r.uniform(0.8, 1.2, 5)
        self.static_phase = r.uniform(0, 2 * np.pi, 5)
        self.body_len0 = 4.2 + r.uniform(0, 0.4)
        self.body_phase = r.uniform(0, 2 * np.pi)
        self.body_amp = self.sc.body_gain
        # Beacons use a different transmit chain: a fixed smooth complex response.
        self.beacon_resp = (1 + 0.25 * np.cos(np.linspace(0, 2 * np.pi, 64) + r.uniform(0, 6))) * \
            np.exp(1j * 0.6 * np.sin(np.linspace(0, np.pi, 64) + r.uniform(0, 6)))
        # Frames to other devices are beamformed: another response, drifting.
        self.foreign_resp = np.exp(1j * r.uniform(-1, 1, 64))
        self.seq = 0
        self._p_ref = float(np.mean(np.abs(self._static_channel()) ** 2))
        self.breath_phase = 0.0
        self.heart_phase = 0.0
        self.posture_offset = 0.0
        self._last_t = 0.0
        self._stall_until = -1.0

    # ------------------------------------------------------------------ truth

    def heart_bpm(self, t: float) -> float:
        rsa = self.sc.hrv_bpm * math.sin(2 * math.pi * self.breath_phase)
        return self.sc.heart_bpm + rsa + 1.5 * math.sin(2 * math.pi * t / 47.0)

    # --------------------------------------------------------------- channel

    def _static_channel(self) -> np.ndarray:
        lam = C / self.freqs
        h = np.zeros(64, dtype=np.complex128)
        for L, a, ph in zip(self.static_len, self.static_amp, self.static_phase):
            h += a * np.exp(-2j * np.pi * L / lam + 1j * ph)
        return h

    def _channel(self, t: float) -> np.ndarray:
        sc, r = self.sc, self.rng
        lam = C / self.freqs
        h = np.zeros(64, dtype=np.complex128)
        for L, a, ph in zip(self.static_len, self.static_amp, self.static_phase):
            h += a * np.exp(-2j * np.pi * L / lam + 1j * ph)
        if sc.present(t):
            disp = sc.breath_mm * 1e-3 * breath_shape(self.breath_phase) + \
                sc.heart_mm * 1e-3 * heart_shape(self.heart_phase)
            L = self.body_len0 + 2 * disp + self.posture_offset
            amp = self.body_amp
            shadow = 0.35  # the seated body blocks part of the direct path
            if sc.moving(t):
                self._move_walk = getattr(self, "_move_walk", 0.0) + r.normal(0, 0.01)
                L += self._move_walk
                amp *= float(np.clip(1 + r.normal(0, 0.4), 0.3, 2.0))
                shadow *= float(np.clip(1 + r.normal(0, 0.5), 0.0, 2.0))
            h += amp * np.exp(-2j * np.pi * L / lam + 1j * self.body_phase)
            h -= shadow * self.static_amp[0] * np.exp(-2j * np.pi * self.static_len[0] / lam + 1j * self.static_phase[0])
        if sc.moving(t):
            # A moving body is a big scatterer: arms and torso add strong,
            # fast-changing reflections; the resting position drifts.
            self.posture_offset += r.normal(0, 0.01)
            if not hasattr(self, "_limb"):
                self._limb = np.array([4.8, 5.6, 6.3])
            self._limb = self._limb + r.normal(0, 0.03, 3)
            for L, a in zip(self._limb, (0.5, 0.35, 0.25)):
                h += a * np.exp(-2j * np.pi * L / lam + 1j * r.uniform(0, 0.3))
        return h

    def _radio(self, h: np.ndarray, kind: str, n_est: int = 1) -> list[np.ndarray]:
        """What the ESP32 reports: gain, random phase, timing slope, noise.

        Returns `n_est` estimates of the same packet (L-LTF and HT-LTF share
        the packet's phase and gain; only their noise differs).
        """
        r, sc = self.rng, self.sc
        if kind == "beacon":
            h = h * self.beacon_resp
            snr = sc.snr_db - 6
        elif kind == "foreign":
            self.foreign_resp *= np.exp(1j * r.normal(0, 0.05, 64))
            h = h * self.foreign_resp * r.uniform(0.6, 1.6)
            snr = sc.snr_db
        else:
            snr = sc.snr_db
        k = np.arange(-32, 32)
        h = h * np.exp(1j * (r.uniform(0, 2 * np.pi) + 2 * np.pi * k * r.normal(0, 0.004)))
        p = np.mean(np.abs(h) ** 2)
        sigma = math.sqrt(p / 2 / 10 ** (snr / 10))
        agc = r.choice([0.8, 1.0, 1.0, 1.0, 1.25])
        scale = 22 * agc / math.sqrt(p)
        out = []
        for _ in range(n_est):
            noise = (r.normal(0, 1, 64) + 1j * r.normal(0, 1, 64)) * sigma
            out.append((h + noise) * scale)
        return out

    @staticmethod
    def _pack(h_lltf: np.ndarray, h_htltf: np.ndarray | None) -> bytes:
        def block(h, idx):
            out = np.zeros(64, dtype=np.complex128)
            out[idx] = h[idx]
            im = np.clip(np.round(out.imag), -127, 127).astype(np.int8)
            re = np.clip(np.round(out.real), -127, 127).astype(np.int8)
            b = np.empty(128, dtype=np.int8)
            b[0::2] = im
            b[1::2] = re
            return b.tobytes()

        # h arrays are indexed k+32 (subcarrier k); the firmware block uses ESP order.
        def esp_order(h):
            esp = np.zeros(64, dtype=np.complex128)
            esp[0:32] = h[32:64]   # subcarriers 0..31
            esp[32:64] = h[0:32]   # subcarriers -32..-1
            return esp

        data = block(esp_order(h_lltf), LLTF_INDEX)
        if h_htltf is not None:
            data += block(esp_order(h_htltf), HTLTF_INDEX)
        return data

    # ------------------------------------------------------------------ frames

    def frame(self, t: float) -> bytes | None:
        """The packet the board would send at session time `t` (None = nothing)."""
        sc, r = self.sc, self.rng
        dt = max(0.0, t - self._last_t)
        self._last_t = t
        self.breath_phase += sc.breath_bpm(t) / 60.0 * dt
        self.heart_phase += self.heart_bpm(t) / 60.0 * dt
        if sc.outage(t) or t < self._stall_until:
            return None
        self.seq += 1
        u = r.random()
        if u < sc.beacon_share:
            kind = "beacon"
        elif sc.firmware == "old" and u < sc.beacon_share + sc.foreign_share:
            kind = "foreign"
        else:
            kind = "reply"
            if r.random() < sc.loss:
                if sc.firmware == "old":
                    self._stall_until = t + 1.0  # esp_ping waits its 1 s timeout
                return None
        h = self._channel(t)
        if kind == "beacon":
            (hl,) = self._radio(h, kind)
            payload = self._pack(hl, None)
        else:
            # The radio's merged HT-LTF: L-LTF averaged with the HT-LTF estimate.
            hl, h2 = self._radio(h, kind, n_est=2)
            payload = self._pack(hl, 0.5 * (hl + h2))
        # RSSI follows the channel's power (the body shadowing it, moving)
        rssi = int(round(-42 + 10 * math.log10(float(np.mean(np.abs(h) ** 2)) / self._p_ref + 1e-9) + r.normal(0, 0.7)))
        if sc.firmware == "new":
            flags = FLAG_TO_ME if kind == "reply" else 0
            return build_csi(payload, self.seq, rssi=rssi, flags=flags, dev_ts_us=int(t * 1e6))
        return build_csi(payload, self.seq, rssi=rssi)

    def frame_times(self, duration: float, start: float = 0.0):
        """Irregular arrival times, like real CSI callbacks (capped at 50 Hz by the firmware)."""
        r = np.random.default_rng(self.sc.seed + 1)
        t = start
        rate = self.sc.frame_rate
        while t < start + duration:
            t += max(0.02, r.exponential(1.0 / rate) * 0.5 + 0.5 / rate)
            yield t


async def run_udp(host: str, port: int, scenario: Scenario | None = None, stop: asyncio.Event | None = None) -> None:
    """Send simulated packets to host:port in real time until `stop` is set."""
    body = SyntheticBody(scenario)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.monotonic()
    try:
        for ts in body.frame_times(10 ** 9):
            if stop is not None and stop.is_set():
                return
            delay = t0 + ts - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            pkt = body.frame(ts)
            if pkt is not None:
                try:
                    sock.sendto(pkt, (host, port))
                except OSError:
                    pass
    finally:
        sock.close()


# ------------------------------------------------------- Pro sensors (demo)

def ppg_shape(p: float) -> float:
    """Blood volume through one heartbeat: a systolic wave, then the dicrotic one."""
    p %= 1.0
    return math.exp(-((p - 0.22) / 0.07) ** 2) + 0.35 * math.exp(-((p - 0.52) / 0.09) ** 2)


class SensorSim:
    """A pretend 60 GHz radar and fingertip pulse sensor on the same body.

    Produces the packets the v2 firmware sends: raw TinyFrame bytes from the
    radar (rates once a second, phases 20x a second) and red/IR batches from
    the pulse sensor (100 Hz, 5 samples per packet). The heartbeat follows the
    body's heart rate including the breathing rhythm in it (RSA), so heart-
    rate variability and coherence come out as they would from a person.
    """

    def __init__(self, scenario: Scenario | None = None, finger_on=(10.0, 1e9), seed: int = 7):
        self.sc = scenario or Scenario()
        self.body = SyntheticBody(self.sc)
        self.rng = np.random.default_rng(seed)
        self.finger_on = finger_on
        self.t = 0.0
        self.seq_ppg = 0
        self.seq_radar = 0
        self._radar_buf = bytearray()
        self._next_rates = 1.0
        self._next_phase = 0.0
        self._fid = 0
        self._ppg: list[tuple[int, int]] = []

    def _advance(self, t: float) -> None:
        dt = max(0.0, t - self.body._last_t)
        self.body._last_t = t
        self.body.breath_phase += self.sc.breath_bpm(t) / 60.0 * dt
        self.body.heart_phase += self.body.heart_bpm(t) / 60.0 * dt

    def _tf(self, ftype: int, payload: bytes) -> bytes:
        from .pro import tf_frame

        self._fid += 1
        return tf_frame(ftype, payload, self._fid)

    def step(self, t: float) -> list[bytes]:
        """Advance to time t (call every 10 ms); returns packets due now."""
        from .protocol import build_ppg, build_radar

        out = []
        self._advance(t)
        self.t = t
        # pulse sensor: one sample now; a packet every 5 samples
        on = self.finger_on[0] <= t < self.finger_on[1] and self.sc.present(t)
        ir_dc, red_dc = (82000.0, 61000.0) if on else (1200.0, 900.0)
        wander = 140.0 * breath_shape(self.body.breath_phase)
        pulse = ppg_shape(self.body.heart_phase) if on else 0.0
        move = 1500.0 * self.rng.normal() if (on and self.sc.moving(t)) else 0.0
        ir = ir_dc - 650.0 * pulse - wander + move + self.rng.normal(0, 18)
        red = red_dc - 420.0 * pulse - 0.7 * wander + move + self.rng.normal(0, 18)
        self._ppg.append((max(0, int(red)), max(0, int(ir))))
        if len(self._ppg) == 5:
            r, i = zip(*self._ppg)
            out.append(build_ppg(list(r), list(i), self.seq_ppg, int(t * 1e6)))
            self.seq_ppg += 1
            self._ppg = []
        # radar
        if self.sc.present(t) and t >= self._next_phase:
            self._next_phase = t + 0.05
            br_ph = breath_shape(self.body.breath_phase) - 0.5
            hr_ph = 0.1 * heart_shape(self.body.heart_phase)
            self._radar_buf += self._tf(0x0A13, struct.pack("<fff", br_ph + hr_ph, br_ph, hr_ph))
        if t >= self._next_rates:
            self._next_rates = t + 1.0
            present = self.sc.present(t)
            self._radar_buf += self._tf(0x0F09, bytes([1 if present else 0]))
            if present and not self.sc.moving(t):
                self._radar_buf += self._tf(0x0A14, struct.pack("<f", self.sc.breath_bpm(t) + self.rng.normal(0, 0.2)))
                self._radar_buf += self._tf(0x0A15, struct.pack("<f", self.sc.heart_bpm + self.rng.normal(0, 1.2)))
                self._radar_buf += self._tf(0x0A16, struct.pack("<If", 1, 0.9))
        if len(self._radar_buf) >= 200 or (self._radar_buf and abs((t * 10) - round(t * 10)) < 1e-6):
            out.append(build_radar(bytes(self._radar_buf), self.seq_radar, int(t * 1e6)))
            self.seq_radar += 1
            self._radar_buf.clear()
        return out


async def run_sensors_udp(host: str, port: int, scenario: Scenario | None = None,
                          stop: asyncio.Event | None = None) -> None:
    """MediSense Pro demo: send simulated radar and pulse packets in real time."""
    sim = SensorSim(scenario)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.monotonic()
    k = 0
    try:
        while stop is None or not stop.is_set():
            k += 1
            t = k * 0.01
            delay = t0 + t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            for pkt in sim.step(round(t, 2)):
                try:
                    sock.sendto(pkt, (host, port))
                except OSError:
                    pass
    finally:
        sock.close()
