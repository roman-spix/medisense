"""A pretend board: produces CSI packets like the real firmware would.

Used by demo mode (try the whole app before the hardware arrives) and by the
tests (check that the analysis recovers known breathing and heart rates).

The model is deliberately unkind: irregular frame timing, automatic gain jumps,
beacons mixed with data frames, int8 quantisation, sensor noise, and a few
posture shifts.
"""

from __future__ import annotations

import asyncio
import math
import random
import socket
import time
from dataclasses import dataclass, field

import numpy as np

from .protocol import build_csi

N_SUB = 64
# LLTF ordering used by ESP-IDF: subcarriers 0..31 then -32..-1. DC (index 0)
# and the guard band (27..37 in this ordering) carry nothing.
NULL_SUBCARRIERS = [0] + list(range(27, 38))


@dataclass
class Scenario:
    """What the pretend meditator does. Rates can drift over the session."""

    breath_start_bpm: float = 13.0
    breath_end_bpm: float = 6.5
    ramp_s: float = 600.0
    heart_bpm: float = 64.0
    breath_depth: float = 0.035  # relative amplitude modulation
    heart_depth: float = 0.004
    noise: float = 0.02
    frame_rate: float = 34.0
    shifts_at: list[float] = field(default_factory=lambda: [95.0, 400.0, 760.0])
    shift_len_s: float = 2.5
    loss: float = 0.02
    seed: int = 7

    def breath_bpm(self, t: float) -> float:
        a = min(1.0, max(0.0, t / self.ramp_s))
        return self.breath_start_bpm + (self.breath_end_bpm - self.breath_start_bpm) * a


class SyntheticBody:
    def __init__(self, scenario: Scenario | None = None):
        self.sc = scenario or Scenario()
        self.rng = np.random.default_rng(self.sc.seed)
        r = self.rng
        base = 18 + 10 * np.abs(np.sin(np.linspace(0, 3 * math.pi, N_SUB) + r.uniform(0, math.pi)))
        base[NULL_SUBCARRIERS] = 0.3
        self.base = base
        self.breath_gain = r.normal(0, 1, N_SUB)
        self.heart_gain = r.normal(0, 1, N_SUB)
        self.phase = r.uniform(0, 2 * math.pi, N_SUB)
        self.posture = np.zeros(N_SUB)  # static change after a shift
        self.seq = 0
        self.breath_phase = 0.0
        self.heart_phase = 0.0
        self._last_t = 0.0

    def frame(self, t: float) -> bytes | None:
        """The packet the board would send at session time `t` (None = lost)."""
        sc, r = self.sc, self.rng
        dt = max(0.0, t - self._last_t)
        self._last_t = t
        self.breath_phase += 2 * math.pi * sc.breath_bpm(t) / 60.0 * dt
        hr = sc.heart_bpm + 2.0 * math.sin(2 * math.pi * t / 47.0)
        self.heart_phase += 2 * math.pi * hr / 60.0 * dt

        breath = math.sin(self.breath_phase) + 0.25 * math.sin(2 * self.breath_phase + 0.6)
        heart = math.sin(self.heart_phase) + 0.4 * math.sin(2 * self.heart_phase)
        mod = 1.0 + sc.breath_depth * breath * self.breath_gain + sc.heart_depth * heart * self.heart_gain

        moving = any(s <= t < s + sc.shift_len_s for s in sc.shifts_at)
        if moving:
            self.posture += r.normal(0, 0.02, N_SUB)
            mod = mod * (1.0 + r.normal(0, 0.15, N_SUB))
        amp = self.base * mod * (1.0 + self.posture) * (1.0 + r.normal(0, sc.noise, N_SUB))
        amp = np.clip(amp, 0, None)

        self.seq += 1
        if r.random() < sc.loss:
            return None

        is_beacon = r.random() < 0.25
        agc = r.choice([0.8, 1.0, 1.0, 1.0, 1.25]) * (0.9 if is_beacon else 1.0)
        amp = amp * agc
        ph = self.phase + r.normal(0, 0.3, N_SUB) + r.uniform(0, 2 * math.pi)
        re = np.clip(np.round(amp * np.cos(ph)), -127, 127).astype(np.int8)
        im = np.clip(np.round(amp * np.sin(ph)), -127, 127).astype(np.int8)
        iq = np.empty(2 * N_SUB, dtype=np.int8)
        iq[0::2] = im
        iq[1::2] = re
        payload = iq.tobytes()
        if not is_beacon:
            # HT data frames carry HT-LTF (and STBC) after the LLTF block.
            payload += (iq.tobytes() * 2)
        rssi = int(-42 + r.normal(0, 1.5))
        return build_csi(payload, self.seq, rssi=rssi)

    def frame_times(self, duration: float, start: float = 0.0):
        """Irregular arrival times, like real CSI callbacks."""
        r = random.Random(self.sc.seed)
        t = start
        while t < start + duration:
            t += r.expovariate(self.sc.frame_rate) * 0.6 + 0.4 / self.sc.frame_rate
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
