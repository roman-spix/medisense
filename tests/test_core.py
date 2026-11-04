import math

import numpy as np
import pytest

from medisense import dsp
from medisense.protocol import (
    CSI_MAGIC, FLAG_TO_ME, SYNC_MAGIC, build_csi, build_ppg, build_radar, packet_kind, parse_csi, parse_ppg,
    parse_radar,
)
from medisense.simulator import Scenario, SyntheticBody
from medisense.vitals import VitalsEngine

# ------------------------------------------------------------------ protocol


def test_parse_matches_firmware_layout():
    iq = bytes([3, 4] * 64)  # imag=3, real=4 -> amplitude 5
    pkt = build_csi(iq, seq=42, rssi=-50, noise_floor=-90, channel=11)
    assert packet_kind(pkt) == CSI_MAGIC
    f = parse_csi(pkt)
    assert (f.seq, f.rssi, f.noise_floor, f.n_subcarriers, f.freq_mhz, f.channel) == (42, -50, -90, 64, 2462, 11)
    assert np.allclose(f.amplitudes(), 5.0)
    assert f.frame_class == "L" and f.dev_ts_us is None and f.to_me is None


def test_medisense_firmware_trailer_and_flags():
    iq = bytes([1, 2] * 192)  # L-LTF + HT-LTF + STBC
    pkt = build_csi(iq, seq=7, flags=FLAG_TO_ME, dev_ts_us=123456789)
    f = parse_csi(pkt)
    assert f.frame_class == "S" and f.n_blocks == 3
    assert f.dev_ts_us == 123456789 and f.to_me is True
    h, sub, idx = f.best_block()
    assert len(h) == 56 and sub.min() == -28 and sub.max() == 28


def test_other_packets_ignored():
    sync = SYNC_MAGIC.to_bytes(4, "little") + bytes(28)
    assert parse_csi(sync) is None
    assert parse_csi(b"\x01\x02") is None


def test_sensor_packets_roundtrip():
    raw = bytes(range(40))
    r = parse_radar(build_radar(raw, seq=5, dev_ts_us=1000, baud=115200))
    assert r.data == raw and r.seq == 5 and r.baud == 115200 and r.radar_type == 1
    red = np.arange(10) + 100000
    ir = np.arange(10) + 200000
    p = parse_ppg(build_ppg(red, ir, seq=9, dev_ts_us=5000))
    assert p.rate_hz == 100 and np.array_equal(p.red, red) and np.array_equal(p.ir, ir) and not p.overflow


# ----------------------------------------------------------------------- dsp


def test_bandpass_passes_band_and_rejects_outside():
    fs = 20.0
    sos = dsp.butter_bandpass(0.1, 0.7, fs)
    t = np.arange(0, 120, 1 / fs)
    inband = np.sin(2 * math.pi * 0.25 * t)
    y = dsp.filtfilt(sos, inband + np.sin(2 * math.pi * 0.01 * t) + np.sin(2 * math.pi * 3.0 * t))
    core = slice(200, -200)
    assert np.corrcoef(y[core], inband[core])[0, 1] > 0.98
    y2 = dsp.fft_bandpass(inband + 0.3 * t, fs, 0.05, 0.8)
    assert np.corrcoef(y2[core], inband[core])[0, 1] > 0.99


def test_streaming_filter_matches_offline():
    sos = dsp.butter_bandpass(0.1, 0.7, 20.0)
    x = np.random.default_rng(1).normal(size=(400, 3))
    st = dsp.StreamingSos(sos, 3)
    assert np.allclose(dsp.sosfilt(sos, x), np.array([st.step(row) for row in x]))


def test_autocorr_and_noise_ripple():
    fs = 20.0
    t = np.arange(0, 60, 1 / fs)
    r = dsp.autocorr(np.sin(2 * math.pi * 0.1 * t)[:, None], 300)
    assert abs(int(np.argmax(r[50:, 0])) + 50 - 200) <= 2
    # the filter ripple of white noise is what averaging over subcarriers leaves
    noise = np.random.default_rng(0).normal(size=(len(t), 200))
    agg = dsp.autocorr(dsp.fft_bandpass(noise, fs, 0.05, 0.8), 100).mean(axis=1)
    expected = dsp.bandpass_noise_acf(fs, 0.05, 0.8, len(t), 100)
    assert np.max(np.abs(agg - expected)) < 0.05


# -------------------------------------------------------------------- engine


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
            out.append((ts, eng.compute(ts)))
            next_t += 1.0
        events += eng.drain_events()
        eng.drain_wave()
    return out, events, body


@pytest.mark.parametrize("bpm,dur", [(5.0, 150), (9.0, 120), (16.0, 100), (3.0, 220)])
def test_breathing_rate_recovered_despite_harmonics(bpm, dur):
    """Slow breathing (inhale, long exhale, pause) has strong harmonics and
    some subcarriers 'double'; the rate must still be the true one."""
    out, _, _ = run_engine(Scenario(breath_start_bpm=bpm, breath_end_bpm=bpm, shifts_at=[], seed=3), dur)
    est = [m.breath_rate for _, m in out[-30:] if m.breath_rate]
    assert len(est) >= 20, "too few breathing estimates"
    assert abs(np.median(est) - bpm) < 0.5


def test_movement_registers_at_low_frame_rate():
    """Regression: at ~12 frames/s the old engine's quiet level became 0 and
    movement read 0.0 all session."""
    sc = Scenario(firmware="old", frame_rate=14, shifts_at=[50.0, 100.0], seed=11)
    out, events, _ = run_engine(sc, 140)
    starts = sorted(e.t_start for e in events)
    assert any(abs(s - 50) < 3 for s in starts) and any(abs(s - 100) < 3 for s in starts)
    motion = [m.motion for t, m in out if t > 20]
    assert max(motion) > 3.5 and np.median(motion) < 2.5


def test_getting_up_is_movement_and_absence_stops_breathing():
    sc = Scenario(breath_start_bpm=6, breath_end_bpm=6, shifts_at=[], away=[(80.0, 40.0)], seed=5)
    out, events, _ = run_engine(sc, 140)
    assert any(78 < e.t_start < 84 for e in events)
    away = [m.breath_rate for t, m in out if 90 < t < 118]
    assert sum(b is not None for b in away) < len(away) / 3  # no breathing while nobody is there


def test_short_dropout_does_not_reset_the_analysis():
    # (seed 9 is a room where breathing never shows: that one honestly reports nothing)
    sc = Scenario(breath_start_bpm=7, breath_end_bpm=7, shifts_at=[], outages=[(90.0, 5.0)], seed=4)
    out, _, _ = run_engine(sc, 130)
    before = [m.breath_rate for t, m in out if 80 < t < 90]
    assert all(b is not None and abs(b - 7) < 0.7 for b in before)
    during = [m for t, m in out if 90.5 < t < 95]
    assert all(m.signal != "no_signal" for m in during)
    assert any(m.signal == "paused" for m in during)
    after = [m.breath_rate for t, m in out if 97 < t < 107]
    assert all(b is not None and abs(b - 7) < 0.7 for b in after)


def test_no_wild_values_with_original_firmware():
    """The original firmware (1 s stalls, foreign frames): fewer readings, but
    every reading shown must be close to the truth."""
    sc = Scenario(firmware="old", frame_rate=30, breath_start_bpm=5, breath_end_bpm=5,
                  shifts_at=[120], away=[(200, 30)], outages=[(160, 3)], seed=7)
    out, _, _ = run_engine(sc, 300)
    shown = [m.breath_rate for _, m in out if m.breath_rate is not None]
    assert shown, "expected at least some readings"
    assert all(abs(b - 5) / 5 < 0.25 for b in shown)


def test_wifi_heart_rate_is_never_presented_as_trusted():
    out, _, _ = run_engine(Scenario(breath_start_bpm=8, breath_end_bpm=8, shifts_at=[], seed=3), 120)
    assert all(m.heart_rate is None for _, m in out)
    assert "heart_rate_experimental" in out[-1][1].as_dict()


def test_signal_loss_and_recovery():
    body = SyntheticBody(Scenario(shifts_at=[]))
    eng = VitalsEngine()
    for ts in body.frame_times(30):
        pkt = body.frame(ts)
        if pkt:
            eng.add_frame(ts, parse_csi(pkt))
    assert eng.compute(30.0).signal in ("ok", "settling")
    assert eng.compute(31.0 + 3).signal == "paused"
    assert eng.compute(45.0).signal == "no_signal"
    for ts in body.frame_times(30, start=50):
        pkt = body.frame(ts)
        if pkt:
            eng.add_frame(ts, parse_csi(pkt))
    m = eng.compute(80.0)
    assert m.signal != "no_signal" and m.frames_per_s > 20
