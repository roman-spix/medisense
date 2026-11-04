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
    # the seven points as Lama Yonten Gyaltso lists them, in his order
    assert [p["key"] for p in cur["points"]] == ["legs", "hands", "back", "shoulders", "head", "tongue", "gaze"]
    assert next(p for p in cur["points"] if p["key"] == "tongue")["status"] == "not_checked"


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
    assert statuses["tongue"] == "not_checked"
    desk = assess(measure(LM, None, ASPECT), None, "desk").as_dict()
    assert [p["key"] for p in desk["points"]] == ["back", "shoulders", "head", "centre"]
    assert not any(c["key"] in ("hands", "legs", "tongue", "face_forward") for c in desk["checks"])


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


# ------------------------------------------------------------- office (desk)

def desk_tracker(tmp_path, lm=LM):
    tr = PostureTracker(store=tmp_path / "posture.json")
    tr.set_settings({"mode": "desk"})
    tr.start_calibration(1.0)
    t0 = tr._calib_until - 1.0
    for i in range(12):
        tr.update(lm, None, ASPECT, t=t0 + i * 0.25)
    assert tr.calibrated
    return tr, t0 + 3.0


def without_hips(lm):
    out = lm.copy()
    out[[23, 24, 25, 26, 27, 28], 3] = 0.0  # below the desk edge, out of view
    return out


def test_desk_ignores_hands(tmp_path):
    tr, t = desk_tracker(tmp_path)
    typing = moved([15], dx=0.3, dy=-0.2)  # one hand off on the mouse, the other high
    cur, _ = feed(tr, typing, t, 3)
    assert cur["score"] >= 90 and worst(cur) is None


def test_desk_slump_without_hips_in_view(tmp_path):
    lm = without_hips(LM)
    tr, t = desk_tracker(tmp_path, lm)
    slumped = moved(FACE + SH, dy=0.15 * SW, lm=lm)  # sliding down in the chair
    cur, _ = feed(tr, slumped, t, 3)
    assert worst(cur) == "slouch" and cur["score"] < 85
    back = next(p for p in cur["points"] if p["key"] == "back")
    assert back["status"] in ("minor", "major") and "slump" in back["advice"]


def test_desk_leaning_in_towards_the_screen_without_depth(tmp_path):
    lm = without_hips(LM)
    tr, t = desk_tracker(tmp_path, lm)
    centre = lm[[11, 12], :2].mean(axis=0)
    closer = lm.copy()
    closer[:, :2] = centre + (lm[:, :2] - centre) * 1.2  # the whole upper body 20 % bigger
    cur, _ = feed(tr, closer, t, 3)
    assert any(c["key"] == "tipped_forward" and c["penalty"] > 0 for c in cur["checks"])
    assert "screen" in " ".join(cur["advice"])


@pytest.mark.parametrize("dz,expect", [(-0.10, "tipped_forward"), (0.10, "leaning_back")])
def test_meditation_leaning_forward_or_back(tmp_path, dz, expect):
    world = np.array(POSE["world"], dtype=float)
    cal = measure(LM, world, ASPECT)
    w2 = world.copy()
    w2[[11, 12], 2] += dz  # smaller z = closer to the camera
    a = assess(measure(LM, w2, ASPECT), {k: v for k, v in cal.items() if k != "visible"}, "meditation")
    bad = {c.key for c in a.checks if c.penalty > 0}
    assert expect in bad
    advice = " ".join(c.advice for c in a.checks if c.key == expect)
    assert ("ignorance" if expect == "tipped_forward" else "pride") in advice  # the Lama's list of errors


def test_no_reminders_before_calibration(tmp_path):
    sent = []
    tr = PostureTracker(store=tmp_path / "posture.json", notify=lambda *a: sent.append(a))
    tr.set_settings({"threshold": 95, "hold_s": 3, "cooldown_s": 30})
    tilted = moved([7], dy=0.10 * SW)  # a tilted head scores under 95 even uncalibrated
    feed(tr, tilted, 0.0, 10)
    assert not sent and not tr.calibrated


async def test_tracking_can_be_stopped_and_started(papp):
    app = papp.pa
    sent = []
    app.tracker.notify = lambda *a: sent.append(a)
    r = await papp.post("/api/tracking", json={"on": False}, headers=H)
    st = await r.json()
    assert st["tracking"] is False and st["posture"]["state"] == "stopped"
    async with papp.ws_connect("/ws") as ws:
        first = await ws.receive_json()
        assert first["tracking"] is False
        for _ in range(6):  # frames sent while stopped are ignored
            await ws.send_json({"type": "pose", "lm": POSE["lm"], "world": POSE["world"], "aspect": ASPECT})
        await asyncio.sleep(0.2)
    assert app.tracker.current["score"] is None and app.tracker.current["state"] == "stopped"
    app._flush_log(force=True)
    from medisense.posture_app import posture_dir
    assert not list(posture_dir().glob("*.csv"))  # nothing recorded while stopped
    r = await papp.post("/api/tracking", json={"on": True}, headers=H)
    st = await r.json()
    assert st["tracking"] is True and st["tracking_since"]
    async with papp.ws_connect("/ws") as ws:
        await ws.receive_json()
        for _ in range(6):
            await ws.send_json({"type": "pose", "lm": POSE["lm"], "world": POSE["world"], "aspect": ASPECT})
        for _ in range(100):
            if app.tracker.current.get("score") is not None:
                break
            await asyncio.sleep(0.02)
    assert app.tracker.current["score"] is not None


def test_day_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDISENSE_HOME", str(tmp_path / "home"))
    from medisense import posture_app

    rows = ["time,score,visible,worst,alerts,mode"]
    t = 9 * 3600
    def add(n, score, worst="", alerts=0):
        nonlocal t
        for _ in range(n):
            rows.append(f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d},{score},1.0,{worst},{alerts},desk")
            t += 10
    add(30, 92)                       # 5 min upright
    add(12, 70, "slouch", 1)          # 2 min slumping
    add(6, 74, "head_forward", 2)     # 1 min head forward
    add(18, 90, "", 2)                # 3 min upright
    t += 3600                         # an hour away (gap)
    add(24, 88, "", 1)                # 4 min upright after a restart (its own alert count)
    rows.append(f"{t // 3600:02d}:{t % 3600 // 60:02d}:00,,0.0,,1,desk")  # away: not counted
    (posture_app.posture_dir() / "2026-11-04.csv").write_text("\n".join(rows) + "\n")
    d = posture_app.read_day("2026-11-04", threshold=80)
    assert d["minutes_tracked"] == 15.0
    assert d["aligned_pct"] == round(100 * 72 / 90, 1)
    assert d["longest_upright_min"] == 5.0
    assert d["drifting_min"] == 3.0
    assert [(i["key"], i["minutes"]) for i in d["issues"]] == [("slouch", 2.0), ("head_forward", 1.0)]
    assert d["issues"][0]["name"] == "Slumping in the chair"
    assert d["alerts"] == 3  # 2 before the restart + 1 after
    assert d["first"] == "09:00" and d["last"] == "10:14"  # the away row at 10:15 has no score
    stricter = posture_app.read_day("2026-11-04", threshold=91)
    assert stricter["aligned_pct"] < d["aligned_pct"]
    assert posture_app.read_day("2026-11-05")["minutes_tracked"] == 0
