"""Practice states, session insights and the "where you are now" profile."""

from datetime import date, timedelta

from medisense import insights


def rows(spec):
    """spec: list of (seconds, motion_state, extra) -> per-second metrics columns."""
    cols = {"t": [], "signal": [], "motion_state": [], "presence": [], "settled_s": [], "breath_rate": [],
            "posture_score": [], "posture_faults": []}
    t = 0.0
    settled = 0.0
    for secs, motion, extra in spec:
        for _ in range(int(secs)):
            settled = 0.0 if motion in ("moving", "restless") else settled + 1
            cols["t"].append(t)
            cols["signal"].append(extra.get("signal", "ok"))
            cols["motion_state"].append(motion)
            cols["presence"].append(str(extra.get("presence", True)))
            cols["settled_s"].append(settled)
            cols["breath_rate"].append(extra.get("breath"))
            cols["posture_score"].append(extra.get("posture"))
            cols["posture_faults"].append(extra.get("faults", ""))
            t += 1
    return cols


def test_states_follow_stillness_movement_and_dullness():
    m = rows([
        (30, "restless", {}),                       # sitting down
        (300, "still", {"breath": 6.0}),            # settles: settling 60 s, then steady
        (5, "moving", {}),                          # a shift
        (120, "still", {"breath": 5.5, "posture": 60, "faults": "head_drop;chin"}),  # head dropping
        (40, "still", {"presence": False}),         # nobody (no breathing, no motion)
    ])
    st = insights.states_for(m, events=[])
    assert st[10] == "restless"
    assert st[40] == "settling" and st[95] == "steady" and st[320] == "steady"
    assert st[332] == "restless"
    # head down for more than 10 s: drowsy
    assert st[335 + 5] == "settling" and st[335 + 20] == "drowsy"
    assert st[-1] == "away"


def test_nod_events_mark_drowsiness():
    m = rows([(200, "still", {"breath": 6.0})])
    st = insights.states_for(m, events=[{"t": 150, "kind": "nod"}])
    assert st[140] == "steady" and st[160] == "drowsy" and st[199] == "drowsy"


def test_short_dropouts_keep_the_state():
    m = rows([(100, "still", {}), (5, "still", {"signal": "paused"}), (20, "still", {"signal": "no_signal"})])
    st = insights.states_for(m, events=[])
    assert st[102] == "steady"  # paused: held
    assert st[-1] == ""  # no data


def test_session_analysis_numbers():
    m = rows([
        (60, "restless", {}),
        (240, "still", {"breath": 7.0}),   # still from 60 s; steady from 120 s
        (4, "moving", {}),
        (600, "still", {"breath": 5.0}),   # the long still stretch
    ])
    a = insights.analyze(m, events=[])
    assert a["settle_s"] == 60  # the first 3 still minutes began at 1:00
    assert a["restless_n"] == 2
    assert a["longest_still_s"] == 600
    assert 70 < a["steady_pct"] < 85
    assert a["steady_breath"] == 5.0  # median over the steady seconds, mostly the long stretch
    assert a["coverage_pct"] == 100.0


def _meta(day, steady, restless=2, drowsy=0, minutes=20, settle=120):
    return {"started_at": f"{day.isoformat()}T07:00:00+00:00", "ended_at": "x", "duration_s": minutes * 60,
            "summary": {"present_min": minutes, "steady_pct": steady, "restless_n": restless, "drowsy_n": drowsy,
                        "settle_s": settle, "longest_still_s": 600, "steady_breath": 6.0}}


def test_profile_stage_trend_and_leaning():
    today = date(2026, 11, 4)
    metas = [_meta(today - timedelta(days=i), steady=40 if i >= 7 else 75, restless=1 if i < 7 else 4)
             for i in range(14)]
    p = insights.profile(metas, today=today)
    assert p["sessions"] == 14 and p["streak_days"] == 14
    assert p["stage"] == "Stable sitting"
    assert p["steady_pct"] == 75 and p["steady_pct_before"] == 40
    assert p["leaning"] == "balanced"

    restless = [_meta(today - timedelta(days=i), steady=30, restless=8) for i in range(5)]
    p = insights.profile(restless, today=today)
    assert p["stage"] == "Settling the body" and p["leaning"] == "excitation"
    assert any("Restlessness" in a for a in p["advice"])

    sleepy = [_meta(today - timedelta(days=i), steady=50, restless=0, drowsy=4) for i in range(5)]
    p = insights.profile(sleepy, today=today)
    assert p["leaning"] == "laxity" and any("Dullness" in a for a in p["advice"])

    sparse = [_meta(today - timedelta(days=7 * i), steady=60) for i in range(4)]
    assert insights.profile(sparse, today=today)["stage"] == "Building the habit"
    assert insights.profile([], today=today)["stage"] == "Getting started"
