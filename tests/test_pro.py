"""MediSense Pro: radar frames, pulse-sensor beats and HRV, and which source wins."""

import struct

import pytest

from medisense.pro import PpgProcessor, ProApp, RadarState, TinyFrameParser, tf_frame
from medisense.protocol import PPG_MAGIC, RADAR_MAGIC, packet_kind, parse_ppg, parse_radar
from medisense.simulator import Scenario, SensorSim


def test_tinyframe_roundtrip_and_resync():
    frames = tf_frame(0x0A14, struct.pack("<f", 6.5), 1) + tf_frame(0x0F09, b"\x01", 2)
    p = TinyFrameParser()
    # garbage first, split in odd places, a corrupted frame in the middle
    bad = bytearray(tf_frame(0x0A15, struct.pack("<f", 70.0), 3))
    bad[-1] ^= 0xFF
    stream = b"\x55\x01\x02" + frames[:5] + frames[5:] + bytes(bad) + tf_frame(0x0A15, struct.pack("<f", 64.0), 4)
    got = []
    for i in range(0, len(stream), 7):
        got += p.feed(stream[i:i + 7])
    types = [t for t, _ in got]
    assert types == [0x0A14, 0x0F09, 0x0A15]
    assert struct.unpack("<f", got[2][1])[0] == pytest.approx(64.0)
    assert p.errors >= 1


def test_radar_state_validity():
    r = RadarState()
    r.feed(10.0, tf_frame(0x0F09, b"\x01") + tf_frame(0x0A14, struct.pack("<f", 6.0)) +
           tf_frame(0x0A15, struct.pack("<f", 61.0)) + tf_frame(0x0A16, struct.pack("<If", 1, 0.9)))
    assert r.breath_rate(11.0) == pytest.approx(6.0) and r.heart_rate(11.0) == pytest.approx(61.0)
    assert r.distance_cm == pytest.approx(90.0)
    assert r.heart_rate(20.0) is None  # stale
    r.feed(12.0, tf_frame(0x0F09, b"\x00"))
    assert r.breath_rate(12.5) is None  # nobody there


def _run_sim(seconds, scenario=None, finger_on=(2.0, 1e9)):
    sim = SensorSim(scenario or Scenario(heart_bpm=62.0, hrv_bpm=5.0, breath_start_bpm=6.0, breath_end_bpm=6.0,
                                         shifts_at=[]), finger_on=finger_on)
    ppg, radar = PpgProcessor(), RadarState()
    for k in range(1, int(seconds * 100)):
        t = round(k * 0.01, 2)
        for pkt in sim.step(t):
            if packet_kind(pkt) == PPG_MAGIC:
                b = parse_ppg(pkt)
                ppg.feed_batch(b.dev_ts_us, b.red, b.ir, b.rate_hz)
            elif packet_kind(pkt) == RADAR_MAGIC:
                radar.feed(t, parse_radar(pkt).data)
    return ppg, radar, sim


def test_pulse_sensor_heart_rate_and_hrv():
    ppg, _, _ = _run_sim(90)
    snap = ppg.snapshot()
    assert ppg.finger
    assert snap.heart_rate == pytest.approx(62.0, abs=4.0)
    assert snap.quality > 0.9
    # slow breathing (6 /min) with 5 bpm of RSA: large, rhythmic variability
    assert snap.rmssd_ms is not None and snap.rmssd_ms > 20
    assert snap.coherence is not None and snap.coherence > 0.4
    assert snap.resonance_bpm == pytest.approx(6.0, abs=1.0)


def test_no_finger_no_numbers():
    ppg, _, _ = _run_sim(20, finger_on=(1e9, 1e9))
    assert not ppg.finger and ppg.snapshot().heart_rate is None


def test_device_clock_wrap():
    p = PpgProcessor()
    assert p._dev_time(2 ** 32 - 1000) < p._dev_time(500)  # wrapped, still increasing


async def test_pro_app_fuses_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDISENSE_HOME", str(tmp_path))
    monkeypatch.setenv("MEDISENSE_POSTURE_NATIVE", "0")
    app = ProApp(demo=True)
    sim = SensorSim(Scenario(heart_bpm=58.0, hrv_bpm=4.0, breath_start_bpm=7.0, breath_end_bpm=7.0, shifts_at=[]),
                    finger_on=(1.0, 1e9))
    import time as _t

    base = _t.monotonic() - 60.0
    for k in range(1, 6000):
        t = round(k * 0.01, 2)
        for pkt in sim.step(t):
            app.on_other_packet(packet_kind(pkt), pkt, base + t)
    d = {"breath_rate": None}
    app.extra_metrics(base + 60.0, d)
    assert d["sensor_hr_source"] == "pulse sensor" and d["sensor_hr"] == pytest.approx(58.0, abs=4.0)
    assert d["breath_source"] == "radar" and d["breath_rate"] == pytest.approx(7.0, abs=0.8)
    assert d["finger_on"] and d["radar_connected"]
    st = app.status()
    assert st["pro"]["radar"]["connected"] and st["pro"]["pulse"]["finger"]


def test_header_checksum_variant_still_parsed():
    """Some LD6002 firmware may compute the header checksum differently:
    known type + expected length + valid data checksum is enough."""
    good = bytearray(tf_frame(0x0A15, struct.pack("<f", 66.0), 9))
    good[7] ^= 0x5A  # header checksum off
    p = TinyFrameParser()
    got = p.feed(bytes(good))
    assert [t for t, _ in got] == [0x0A15]
    junk = bytearray(tf_frame(0x0B01, b"\x00" * 20, 9))
    junk[7] ^= 0x5A  # unknown type with a bad header: rejected
    assert p.feed(bytes(junk)) == []
