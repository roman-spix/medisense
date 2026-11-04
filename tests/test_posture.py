"""Vairocana posture scoring, the tracker (calibration, alerts, nods) and the Posture app API."""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from medisense.posture import PostureTracker, assess, measure

POSE = json.loads((Path(__file__).parent / "data" / "pose_upright.json").read_text())
LM = np.array(POSE["lm"], dtype=float)
ASPECT = POSE["aspect"]
FACE = list(range(0, 11))  # nose, eyes, ears, mouth
SH = [11, 12]


def shoulder_width(lm):
    return float(np.hypot((lm[11, 0] - lm[12, 0]) * ASPECT, lm[11, 1] - lm[12, 1]))


SW = shoulder_width(LM)
ED = float(np.hypot((LM[7, 0] - LM[8, 0]) * ASPECT, LM[7, 1] - LM[8, 1]))  # ear to ear


def moved(idx, dy=0.0, dx=0.0, lm=LM):
    out = lm.copy()
    out[idx, 1] += dy
    out[idx, 0] += dx
    return out


def calibrated_tracker(tmp_path, **kw):
    tr = PostureTracker(store=tmp_path / "posture.json", **kw)
    tr.start_calibration(1.0)
    t0 = tr._calib_until - 1.0
    for i in range(12):
        tr.update(LM, None, ASPECT, t=t0 + i * 0.25)
    assert tr.calibrated
    return tr, t0 + 3.0


def feed(tr, lm, t0, seconds, fps=4):
    cur = None
    for i in range(int(seconds * fps)):
        cur = tr.update(lm, None, ASPECT, t=t0 + i / fps)
    return cur, t0 + seconds


def worst(cur):
    bad = [c for c in cur["checks"] if c["penalty"] > 0]
    return max(bad, key=lambda c: c["penalty"])["key"] if bad else None


def test_upright_scores_high_once_calibrated(tmp_path):
    tr, t = calibrated_tracker(tmp_path)
    cur, _ = feed(tr, LM, t, 3)
    assert cur["score"] >= 90
    assert cur["state"] in ("aligned", "ok")
    points = {c["point"] for c in cur["checks"]}
    assert {"spine", "shoulders", "head", "hands", "eyes", "mouth"} <= points


@pytest.mark.parametrize("name,lm,expect", [
    ("head drop", moved(FACE, dy=0.25 * SW), "head_drop"),
    ("shrug", moved(SH, dy=-0.12 * SW), "shoulders_relaxed"),
    ("uneven shoulders", moved([11], dy=0.15 * SW), "shoulders_level"),
    ("head tilted", moved([7], dy=0.10 * SW), "head_level"),
])
def test_each_fault_is_named(tmp_path, name, lm, expect):
    tr, t = calibrated_tracker(tmp_path)
    cur, _ = feed(tr, lm, t, 3)
    assert cur["score"] < 90, name
    assert worst(cur) == expect, (name, cur["checks"])
    assert cur["advice"], name


def test_shrug_is_not_mistaken_for_a_head_drop(tmp_path):
    tr, t = calibrated_tracker(tmp_path)
    cur, _ = feed(tr, moved(SH, dy=-0.12 * SW), t, 3)
    drop = next(c for c in cur["checks"] if c["key"] == "head_drop")
    assert drop["penalty"] == 0


def test_uncalibrated_runs_level_checks_only():
    a = assess(measure(LM, None, ASPECT), None, "meditation")
    statuses = {c.key: c.status for c in a.checks}
    assert statuses["head_drop"] == "needs_calibration"
    assert statuses["eyes"] == "not_checked"
    desk = {c.key: c.status for c in assess(measure(LM, None, ASPECT), None, "desk").checks}
    assert desk["hands"] == "off" and desk["legs"] == "off"


def test_nobody_in_view():
    assert measure(np.zeros((33, 4)), None, ASPECT)["visible"] is False


def test_alert_after_hold_then_cooldown(tmp_path):
    sent = []
    tr, t = calibrated_tracker(tmp_path, notify=lambda title, msg, sound: sent.append((title, msg)))
    tr.set_settings({"threshold": 80, "hold_s": 5, "cooldown_s": 60})
    bad = moved(FACE, dy=0.3 * SW)
    _, t = feed(tr, bad, t, 4)
    assert not sent  # not yet: shorter than the hold time
    _, t = feed(tr, bad, t, 4)
    assert len(sent) == 1 and "head" in sent[0][1].lower()
    _, t = feed(tr, bad, t, 20)
    assert len(sent) == 1  # cooldown
    kinds = [k for _, k, _ in tr.drain_events()]
    assert kinds.count("posture_alert") == 1


def test_nod_is_detected(tmp_path):
    tr, t = calibrated_tracker(tmp_path)
    _, t = feed(tr, LM, t, 2)
    _, t = feed(tr, moved([0], dy=0.35 * ED), t, 1)  # the chin drops for a second...
    _, t = feed(tr, LM, t, 1.5)  # ...and comes back up
    assert any(k == "nod" for _, k, _ in tr.drain_events())


def test_calibration_is_shared_between_apps(tmp_path):
    a, _ = calibrated_tracker(tmp_path)
    b = PostureTracker(store=tmp_path / "posture.json", settings_key="session_settings")
    assert b.calibrated
    b.set_settings({"threshold": 60})
    a2 = PostureTracker(store=tmp_path / "posture.json")
    assert a2.settings.threshold != 60  # each app keeps its own settings


# ------------------------------------------------------------- posture app

@pytest.fixture
async def papp(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDISENSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEDISENSE_POSTURE_NATIVE", "0")
    from medisense import notify
    from medisense.posture_app import PostureApp, build

    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    app = PostureApp()
    await app.start()
    c = await aiohttp_client(build(app))
    c.pa = app
    yield c
    await app.stop()


H = {"X-MediSense": "1"}


async def test_posture_app_api(papp):
    app = papp.pa
    r = await papp.get("/api/state")
    st = await r.json()
    assert st["service"]["state"] == "browser"
    async with papp.ws_connect("/ws") as ws:
        first = await ws.receive_json()
        assert first["type"] == "posture"
        for _ in range(6):
            await ws.send_json({"type": "pose", "lm": POSE["lm"], "world": POSE["world"], "aspect": ASPECT})
        for _ in range(100):
            if app.tracker.current.get("score") is not None:
                break
            await asyncio.sleep(0.02)
    assert app.tracker.current["score"] is not None
    r = await papp.post("/api/settings", json={"threshold": 70, "mode": "desk", "paused": True}, headers=H)
    st = await r.json()
    assert st["paused"] and st["service"]["settings"]["mode"] == "desk"
    r = await papp.post("/api/calibrate", json={}, headers=H)
    assert (await r.json())["ok"]
    r = await papp.post("/api/test-notification", json={}, headers=H)
    assert (await r.json())["ok"]
    app._flush_log(force=True)
    r = await papp.get("/api/history")
    h = await r.json()
    assert h["minutes_tracked"] >= 0 and h["score"]
    r = await papp.get("/api/days")
    assert len(await r.json()) == 1
    r = await papp.get("/api/history?day=../../etc")
    assert (await r.json())["score"] == []
    r = await papp.post("/api/settings", json={"threshold": 70})
    assert r.status == 403  # no header: refused
    for path in ("/", "/static/posture-app.js", "/static/posture.js"):
        assert (await papp.get(path)).status == 200
