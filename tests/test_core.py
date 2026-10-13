import math

import numpy as np
import pytest

from medisense import dsp
from medisense.protocol import CSI_MAGIC, SYNC_MAGIC, build_csi, packet_kind, parse_csi
from medisense.simulator import Scenario, SyntheticBody
from medisense.vitals import VitalsEngine


def test_parse_matches_firmware_layout():
    iq = bytes([3, 4] * 64)  # imag=3, real=4 -> amplitude 5
    pkt = build_csi(iq, seq=42, rssi=-50, noise_floor=-90, channel=11)
    assert packet_kind(pkt) == CSI_MAGIC
    f = parse_csi(pkt)
    assert f is not None
    assert (f.seq, f.rssi, f.noise_floor, f.n_subcarriers, f.freq_mhz, f.channel) == (42, -50, -90, 64, 2462, 11)
    assert np.allclose(f.amplitudes(), 5.0)


def test_other_packets_ignored():
    sync = SYNC_MAGIC.to_bytes(4, "little") + bytes(28)
    assert parse_csi(sync) is None
    assert parse_csi(b"\x01\x02") is None


def test_ht_frames_use_lltf_block_only():
    lltf = bytes([0, 10] * 64)
    ht = bytes([0, 99] * 128)
    f = parse_csi(build_csi(lltf + ht, seq=1))
    assert np.allclose(f.amplitudes(), 10.0)


def test_bandpass_passes_band_and_rejects_outside():
    fs = 20.0
    sos = dsp.butter_bandpass(0.1, 0.7, fs)
    t = np.arange(0, 120, 1 / fs)
    inband = np.sin(2 * math.pi * 0.25 * t)
    slow = np.sin(2 * math.pi * 0.01 * t)
    fast = np.sin(2 * math.pi * 3.0 * t)
    y = dsp.filtfilt(sos, inband + slow + fast)
    core = slice(200, -200)
    assert np.corrcoef(y[core], inband[core])[0, 1] > 0.98


def test_streaming_filter_matches_offline():
    fs = 20.0
    sos = dsp.butter_bandpass(0.1, 0.7, fs)
    x = np.random.default_rng(1).normal(size=(400, 3))
    offline = dsp.sosfilt(sos, x)
    st = dsp.StreamingSos(sos, 3)
    online = np.array([st.step(row) for row in x])
    assert np.allclose(offline, online)


def test_spectral_peak_accuracy():
    fs = 20.0
    t = np.arange(0, 40, 1 / fs)
    x = np.sin(2 * math.pi * 0.2 * t) + 0.3 * np.random.default_rng(0).normal(size=len(t))
    p = dsp.spectral_peak(x, fs, 0.07, 0.75)
    assert abs(p.freq * 60 - 12.0) < 0.3
    assert p.confidence > 0.5


def run_engine(scenario, duration):
    body = SyntheticBody(scenario)
    eng = VitalsEngine()
    out, events = [], []
    next_t = 1.0
    for ts in body.frame_times(duration):
        pkt = body.frame(ts)
        if pkt:
            eng.add_frame(ts, parse_csi(pkt))
        if ts >= next_t:
            out.append(eng.compute(ts))
            next_t += 1.0
        events += eng.drain_events()
        eng.drain_wave()
    return out, events


@pytest.mark.parametrize("bpm", [5.0, 9.0, 16.0])
def test_breathing_rate_recovered(bpm):
    sc = Scenario(breath_start_bpm=bpm, breath_end_bpm=bpm, shifts_at=[], seed=3)
    out, _ = run_engine(sc, 150)
    est = [m.breath_rate for m in out[-30:] if m.breath_rate]
    assert est, "no breathing estimate"
    assert abs(np.median(est) - bpm) < 0.6


def test_heart_rate_recovered_on_clean_signal():
    sc = Scenario(breath_start_bpm=8, breath_end_bpm=8, heart_bpm=66, shifts_at=[], seed=5)
    out, _ = run_engine(sc, 120)
    est = [m.heart_rate for m in out[-30:] if m.heart_rate]
    assert est
    assert abs(np.median(est) - 66) < 4


def test_shifts_detected_and_still_otherwise():
    sc = Scenario(shifts_at=[40.0, 90.0], seed=11)
    out, events = run_engine(sc, 130)
    starts = sorted(e.t_start for e in events)
    assert len(starts) == 2
    assert abs(starts[0] - 40) < 2 and abs(starts[1] - 90) < 2
    states = [m.motion_state for m in out[10:35]]
    assert states.count("still") >= 20


def test_signal_loss_and_recovery():
    body = SyntheticBody(Scenario(shifts_at=[]))
    eng = VitalsEngine()
    for ts in body.frame_times(30):
        eng.add_frame(ts, parse_csi(body.frame(ts) or build_csi(bytes(128), 0)))
    assert eng.compute(30.0).signal in ("ok", "settling")
    assert eng.compute(40.0).signal == "no_signal"
    for ts in body.frame_times(30, start=50):
        pkt = body.frame(ts)
        if pkt:
            eng.add_frame(ts, parse_csi(pkt))
    m = eng.compute(80.0)
    assert m.signal != "no_signal"
    assert m.frames_per_s > 20
