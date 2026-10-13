import asyncio

import pytest

from medisense import sessions
from medisense.protocol import parse_csi
from medisense.recorder import SessionRecorder, read_capture
from medisense.simulator import Scenario, SyntheticBody


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDISENSE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_recorder_roundtrip(home):
    rec = SessionRecorder({"practice": "Breath focus"})
    body = SyntheticBody(Scenario(shifts_at=[]))
    pkts = [p for p in (body.frame(t / 30) for t in range(1, 200)) if p]
    for i, p in enumerate(pkts):
        rec.raw(1000.0 + i, p)
    t0 = rec.t0_mono
    for i in range(120):
        rec.metrics(t0 + i, {"breath_rate": 12 - i / 30, "motion_state": "still" if i % 10 else "restless",
                             "heart_rate": 60.0, "posture_score": 90.0})
    rec.wave([(t0 + i / 20, 0.1, 1.0) for i in range(40)] + [(t0 + 3, None, 1.0)])
    rec.event(t0 + 30, "shift", peak=7.0)
    rec.event(t0 + 40, "polar_hr", value=61)
    rec.close()

    got = list(read_capture(rec.path / "raw.mscap.gz"))
    assert [p for _, p in got] == pkts
    assert parse_csi(got[0][1]) is not None

    s = sessions.summarize(rec.path)
    assert s["shifts"] == 1
    assert s["polar_hr_avg"] == 61
    assert s["stillness_pct"] == 90.0
    assert s["breath_start"] > s["breath_end"]
    assert s["posture_avg"] == 90.0

    listed = sessions.list_sessions()
    assert listed[0]["id"] == rec.id and listed[0]["practice"] == "Breath focus"
    wave = sessions.load_wave(rec.path)
    assert len(wave["t"]) == 21 and wave["breath"][-1] is None


def test_truncated_capture_is_readable(home):
    rec = SessionRecorder({})
    for i in range(50):
        rec.raw(float(i), bytes([1, 0, 17, 197]) + bytes(40))
    rec.close()
    data = (rec.path / "raw.mscap.gz").read_bytes()
    (rec.path / "raw.mscap.gz").write_bytes(data[: len(data) - 7])
    assert len(list(read_capture(rec.path / "raw.mscap.gz"))) <= 50


def test_session_path_rejects_traversal(home):
    assert sessions.session_path("../etc") is None
    assert sessions.session_path(".hidden") is None


# ------------------------------------------------------------------ server

@pytest.fixture
async def client(aiohttp_client):
    from medisense.app import MediSenseApp
    from medisense.server import build

    app = MediSenseApp(demo=True)
    c = await aiohttp_client(build(app))
    c.ms = app
    return c


H = {"X-MediSense": "1"}


async def test_guards(client):
    r = await client.post("/api/session/start", json={})
    assert r.status == 403
    r = await client.get("/api/status", headers={"Host": "attacker.example"})
    assert r.status == 403
    r = await client.get("/api/status")
    assert r.status == 200 and (await r.json())["demo"] is True


async def test_session_flow(client):
    app = client.ms
    r = await client.post("/api/session/start", json={"practice": "Body scan", "planned_min": 10}, headers=H)
    sid = (await r.json())["id"]
    r = await client.post("/api/session/start", json={}, headers=H)
    assert r.status == 400

    body = SyntheticBody(Scenario(shifts_at=[]))
    for ts in body.frame_times(3):
        p = body.frame(ts)
        if p:
            app.on_packet(p, ("192.168.1.50", 5005))
    await app._slow_tick(1)
    await app._fast_tick()
    assert app.status()["live"]

    r = await client.post("/api/session/event", json={"kind": "polar_hr", "value": 63}, headers=H)
    assert (await r.json())["kind"] == "polar_hr"
    r = await client.post("/api/session/event", json={"kind": "rm -rf"}, headers=H)
    assert r.status == 400

    async with client.ws_connect("/ws") as ws:
        first = await ws.receive_json()
        assert first["type"] == "status"
        await ws.send_json({"type": "posture", "visible": 1, "score": 88.5, "slump": 0.05, "head_tilt": 2})
        for _ in range(100):
            if app.posture:
                break
            await asyncio.sleep(0.02)
    assert app.posture["score"] == 88.5

    r = await client.post("/api/session/stop", json={}, headers=H)
    meta = await r.json()
    assert meta["id"] == sid and "summary" in meta and meta["practice"] == "Body scan"

    r = await client.post(f"/api/sessions/{sid}/meta", json={"notes": "calm", "rating": 4, "evil": 1}, headers=H)
    m2 = await r.json()
    assert m2["notes"] == "calm" and m2["rating"] == 4 and "evil" not in m2

    r = await client.get("/api/sessions")
    assert (await r.json())[0]["id"] == sid
    r = await client.get(f"/api/sessions/{sid}")
    d = await r.json()
    assert d["meta"]["rating"] == 4 and any(e["kind"] == "polar_hr" for e in d["events"])
    r = await client.get(f"/api/sessions/{sid}/export.csv")
    assert r.status == 200 and "breath_rate" in (await r.text()).splitlines()[0]
    r = await client.get("/api/sessions/nope")
    assert r.status == 404

    r = await client.delete(f"/api/sessions/{sid}", headers=H)
    assert r.status == 200
    r = await client.get("/api/sessions")
    assert await r.json() == []


async def test_dashboard_files_served(client):
    for path in ("/", "/static/app.js", "/static/charts.js", "/static/posture.js", "/static/style.css"):
        r = await client.get(path)
        assert r.status == 200, path
    r = await client.get("/vendor/mediapipe/../../etc/passwd")
    assert r.status == 404
