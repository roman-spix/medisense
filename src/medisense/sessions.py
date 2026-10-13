"""Reading recorded sessions back, and the summary numbers for each one."""

from __future__ import annotations

import csv
import json
import math
import shutil
from pathlib import Path

import numpy as np

from .config import data_dir, sessions_dir


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def load_metrics(path: Path) -> dict[str, list]:
    cols: dict[str, list] = {}
    f = path / "metrics.csv"
    if not f.exists():
        return cols
    with open(f, newline="") as fh:
        for row in csv.DictReader(fh):
            for k, v in row.items():
                if k in ("signal", "motion_state", "presence"):
                    cols.setdefault(k, []).append(v)
                else:
                    cols.setdefault(k, []).append(_f(v))
    return cols


def load_events(path: Path) -> list[dict]:
    out = []
    f = path / "events.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def load_wave(path: Path, max_points: int = 20000) -> dict[str, list]:
    t, b, m = [], [], []
    f = path / "wave.csv"
    if f.exists():
        with open(f, newline="") as fh:
            r = csv.reader(fh)
            next(r, None)
            for row in r:
                if len(row) == 3:
                    t.append(_f(row[0]))
                    b.append(_f(row[1]))
                    m.append(_f(row[2]))
    step = max(1, len(t) // max_points)
    return {"t": t[::step], "breath": b[::step], "motion": m[::step]}


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(float(np.mean(xs)), 2) if xs else None


def summarize(path: Path) -> dict:
    """Numbers that describe a session, for the summary card and progress charts."""
    m = load_metrics(path)
    events = load_events(path)
    t = m.get("t", [])
    n = len(t)
    s: dict = {"seconds_recorded": n}
    if not n:
        return s
    br = m.get("breath_rate", [None] * n)
    hr = m.get("heart_rate", [None] * n)
    states = m.get("motion_state", [""] * n)
    br_valid = [(ti, b) for ti, b in zip(t, br) if b is not None]
    s["breath_coverage_pct"] = round(100.0 * len(br_valid) / n, 1)
    s["breath_avg"] = _mean([b for _, b in br_valid])
    if br_valid:
        # First vs last 3 minutes; for short sessions, first vs last third.
        span = br_valid[-1][0] - br_valid[0][0]
        win = min(180.0, max(20.0, span / 3))
        first = [b for ti, b in br_valid if ti <= br_valid[0][0] + win]
        last = [b for ti, b in br_valid if ti >= br_valid[-1][0] - win]
        s["breath_start"] = _mean(first)
        s["breath_end"] = _mean(last)
        arr = np.array([b for _, b in br_valid])
        if len(arr) >= 60:
            roll = np.convolve(arr, np.ones(60) / 60, mode="valid")
            s["breath_lowest_minute"] = round(float(roll.min()), 2)
        else:
            s["breath_lowest_minute"] = round(float(arr.min()), 2)
    s["breath_regularity"] = _mean(m.get("breath_regularity", []))
    hr_valid = [h for h in hr if h is not None]
    s["heart_avg"] = _mean(hr_valid)
    known = [st for st in states if st in ("still", "restless", "moving")]
    if known:
        s["stillness_pct"] = round(100.0 * known.count("still") / len(known), 1)
    s["shifts"] = sum(1 for e in events if e.get("kind") == "shift")
    s["marks"] = sum(1 for e in events if e.get("kind") == "mark")
    polar = [e.get("value") for e in events if e.get("kind") == "polar_hr" and isinstance(e.get("value"), (int, float))]
    if polar:
        s["polar_hr_avg"] = round(float(np.mean(polar)), 1)
    ps = [p for p in m.get("posture_score", []) if p is not None]
    if ps:
        s["posture_avg"] = round(float(np.mean(ps)), 1)
        s["posture_upright_pct"] = round(100.0 * sum(1 for p in ps if p >= 80) / len(ps), 1)
    s["slump_events"] = sum(1 for e in events if e.get("kind") == "slump")
    return s


def list_sessions(root: Path | None = None) -> list[dict]:
    root = root or sessions_dir()
    out = []
    for d in sorted(root.iterdir(), reverse=True) if root.exists() else []:
        meta_f = d / "session.json"
        if not meta_f.exists():
            continue
        try:
            meta = json.loads(meta_f.read_text())
        except ValueError:
            continue
        if "summary" not in meta and meta.get("ended_at"):
            meta["summary"] = summarize(d)
        out.append(meta)
    return out


def session_path(sid: str, root: Path | None = None) -> Path | None:
    root = root or sessions_dir()
    if not sid or "/" in sid or "\\" in sid or sid.startswith("."):
        return None
    p = root / sid
    return p if (p / "session.json").exists() else None


def load_session(sid: str) -> dict | None:
    p = session_path(sid)
    if not p:
        return None
    meta = json.loads((p / "session.json").read_text())
    if "summary" not in meta:
        meta["summary"] = summarize(p)
    posture: dict[str, list] = {}
    pf = p / "posture.csv"
    if pf.exists():
        with open(pf, newline="") as fh:
            for row in csv.DictReader(fh):
                for k, v in row.items():
                    posture.setdefault(k, []).append(_f(v))
    return {"meta": meta, "metrics": load_metrics(p), "events": load_events(p), "posture": posture}


def update_meta(sid: str, fields: dict) -> dict | None:
    p = session_path(sid)
    if not p:
        return None
    meta = json.loads((p / "session.json").read_text())
    meta.update(fields)
    tmp = p / "session.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(p / "session.json")
    return meta


def trash_session(sid: str) -> bool:
    """Move a session to ~/MediSense/trash (not deleted, just out of the way)."""
    p = session_path(sid)
    if not p:
        return False
    trash = data_dir() / "trash"
    trash.mkdir(exist_ok=True)
    shutil.move(str(p), str(trash / p.name))
    return True
