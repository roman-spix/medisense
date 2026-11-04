"""Meditation insights: what the measurements say about the practice.

Heart and breathing numbers alone don't answer "how is my practice going?".
What the sensors *can* see is the body's side of meditation: whether you sat
still, how long it took to settle, whether you fidgeted (a bodily sign of
agitation) or your head dropped and nodded (a bodily sign of dullness), and
how your breathing slowed once you were settled. These are the two classic
obstacles of calm-abiding practice, excitation and laxity, seen from outside.

Every second gets a *practice state*:

    steady    still for at least a minute
    settling  still, but less than a minute since you last moved (or since the start)
    restless  moving: fidgeting or shifting position
    drowsy    the head dropping or nodding (needs the camera)
    away      nobody there
    ""        no data

From those, each session gets a few numbers (time to settle, steady minutes,
longest still stretch, interruptions, breathing while steady...) and across
sessions a "where you are now" profile.

Honest limits: sensors see the body, not the mind. Steady here means the
conditions for a calm mind (stillness, even breathing, upright posture), not
proof of it; your own notes and ratings stay the real record.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, timedelta
from statistics import median

STEADY_AFTER_S = 60.0  # still this long after moving: steady
SETTLED_RUN_S = 180.0  # the first still stretch this long marks "settled"
DROWSY_NOD_WINDOW_S = 60.0  # a nod keeps the state drowsy this long
DROWSY_DROP_S = 10.0  # head dropped this long: drowsy
AWAY_AFTER_S = 20.0  # no sign of anyone this long: away
PAUSE_HOLD_S = 10.0  # brief data gaps keep the previous state

STATES = ("steady", "settling", "restless", "drowsy", "away")
STATE_LABEL = {
    "steady": "Steady",
    "settling": "Settling",
    "restless": "Restless",
    "drowsy": "Drowsy",
    "away": "Away",
    "": "No data",
}


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class PracticeState:
    """Turns one row of measurements per second into a practice state.

    Used live (MediSense) and to re-analyse recorded sessions, so both agree.
    """

    def __init__(self):
        self.state = ""
        self.since: float | None = None  # when the current state began
        self._last_nod = -1e9
        self._drop_run = 0.0
        self._absent_run = 0.0
        self._last_ok_t: float | None = None
        self._last_t: float | None = None
        # running session numbers
        self.counts: Counter = Counter()
        self.steady_s = 0.0
        self.settled_at: float | None = None
        self._still_start: float | None = None

    def note_event(self, t: float, kind: str) -> None:
        if kind in ("nod", "slump"):
            self._last_nod = t

    def step(self, t: float, row: dict) -> str:
        dt = 1.0 if self._last_t is None else max(0.0, min(5.0, t - self._last_t))
        self._last_t = t
        signal = row.get("signal") or ""
        motion = row.get("motion_state") or "unknown"
        faults = row.get("posture_faults") or ""
        camera_sees = _num(row.get("posture_score")) is not None

        if "head_drop" in faults.split(";"):
            self._drop_run += dt
        else:
            self._drop_run = 0.0

        if signal == "no_signal":
            new = ""
        elif signal == "paused":
            # a short dropout: keep what we knew, briefly
            held = self._last_ok_t is not None and t - self._last_ok_t <= PAUSE_HOLD_S
            new = self.state if held else ""
        else:
            self._last_ok_t = t
            present = _truthy(row.get("presence")) or camera_sees or motion in ("restless", "moving")
            self._absent_run = 0.0 if present else self._absent_run + dt
            settled = _num(row.get("settled_s")) or 0.0
            if self._absent_run >= AWAY_AFTER_S:
                new = "away"
            elif motion in ("restless", "moving"):
                new = "restless"
            elif t - self._last_nod <= DROWSY_NOD_WINDOW_S or self._drop_run >= DROWSY_DROP_S:
                new = "drowsy"
            elif motion == "still" and settled >= STEADY_AFTER_S:
                new = "steady"
            else:
                new = "settling"

        if new != self.state:
            self.state = new
            self.since = t
        self.counts[new] += dt
        if new == "steady":
            self.steady_s += dt
        # the first long still stretch: "settled"
        if new in ("steady", "settling", "drowsy"):
            if self._still_start is None:
                self._still_start = t
            if self.settled_at is None and t - self._still_start >= SETTLED_RUN_S:
                self.settled_at = self._still_start
        elif new in ("restless", "away"):
            self._still_start = None
        return new

    def live(self, t: float) -> dict:
        present = sum(v for k, v in self.counts.items() if k in ("steady", "settling", "restless", "drowsy"))
        return {
            "state": self.state,
            "state_for_s": round(t - self.since, 1) if self.since is not None else 0.0,
            "steady_s": round(self.steady_s, 1),
            "steady_pct": round(100.0 * self.steady_s / present, 1) if present >= 30 else None,
            "settled_at": self.settled_at,
        }


def states_for(metrics: dict[str, list], events: list[dict]) -> list[str]:
    """Practice state per recorded second (re-computed, so old sessions work too)."""
    t = metrics.get("t") or []
    keys = [k for k in ("signal", "motion_state", "presence", "settled_s", "posture_score", "posture_faults")
            if k in metrics]
    nods = sorted(e["t"] for e in events if e.get("kind") in ("nod", "slump") and isinstance(e.get("t"), (int, float)))
    ps = PracticeState()
    out = []
    j = 0
    for i, ti in enumerate(t):
        if ti is None:
            out.append("")
            continue
        while j < len(nods) and nods[j] <= ti:
            ps.note_event(nods[j], "nod")
            j += 1
        out.append(ps.step(ti, {k: metrics[k][i] for k in keys}))
    return out


def _runs(t: list, states: list[str]) -> list[tuple[float, float, str]]:
    runs: list[list] = []
    for ti, s in zip(t, states):
        if ti is None:
            continue
        if runs and runs[-1][2] == s and ti - runs[-1][1] <= 3.0:
            runs[-1][1] = ti + 1.0
        else:
            runs.append([ti, ti + 1.0, s])
    return [(round(a, 1), round(b, 1), s) for a, b, s in runs]


def _episodes(runs: list[tuple[float, float, str]], state: str, merge_s: float) -> int:
    """Separate occurrences of a state; ones closer together than merge_s count once."""
    n, last_end = 0, None
    for a, b, s in runs:
        if s != state:
            continue
        if last_end is None or a - last_end > merge_s:
            n += 1
        last_end = b
    return n


def analyze(metrics: dict[str, list], events: list[dict]) -> dict:
    """Numbers that describe the practice in one session."""
    t = metrics.get("t") or []
    if not t:
        return {}
    states = states_for(metrics, events)
    n = len(states)
    c = Counter(states)
    present = sum(c[s] for s in ("steady", "settling", "restless", "drowsy"))
    out: dict = {"coverage_pct": round(100.0 * (n - c[""]) / n, 1) if n else None}
    if present < 30:
        return out
    runs = _runs(t, states)
    still_runs = []  # still stretches: settling/steady/drowsy back to back
    cur = None
    for a, b, s in runs:
        if s in ("settling", "steady", "drowsy"):
            cur = [a, b] if cur is None else [cur[0], b]
        elif s in ("restless", "away"):
            if cur:
                still_runs.append(cur)
            cur = None
        # "" (a data gap): the still stretch stays open
    if cur:
        still_runs.append(cur)
    t0 = next((x for x in t if x is not None), 0.0)
    settled = next((a for a, b in still_runs if b - a >= SETTLED_RUN_S), None)
    br = metrics.get("breath_rate") or [None] * n
    steady_br = [b for b, s in zip(br, states) if s == "steady" and b is not None]
    ps = [p for p in (metrics.get("posture_score") or []) if p is not None]
    out.update({
        "steady_min": round(c["steady"] / 60.0, 1),
        "steady_pct": round(100.0 * c["steady"] / present, 1),
        "settle_s": None if settled is None else round(settled - t0, 0),
        "longest_still_s": round(max((b - a for a, b in still_runs), default=0.0), 0),
        "restless_n": _episodes(runs, "restless", 10.0),
        "restless_min": round(c["restless"] / 60.0, 1),
        "drowsy_n": _episodes(runs, "drowsy", 30.0),
        "drowsy_min": round(c["drowsy"] / 60.0, 1),
        "away_min": round(c["away"] / 60.0, 1),
        "present_min": round(present / 60.0, 1),
        "steady_breath": round(median(steady_br), 2) if len(steady_br) >= 30 else None,
        "posture_aligned_pct": round(100.0 * sum(1 for p in ps if p >= 80) / len(ps), 1) if len(ps) >= 30 else None,
    })
    return out


def timeline(metrics: dict[str, list], events: list[dict]) -> list[tuple[float, float, str]]:
    """Run-length practice states for the session's timeline strip."""
    return _runs(metrics.get("t") or [], states_for(metrics, events))


# ------------------------------------------------------------ across sessions

def _session_date(meta: dict) -> date | None:
    s = meta.get("started_at")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _avg(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def _med(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(float(median(xs)), 1) if xs else None


def _per10(sessions: list[dict], key: str) -> float | None:
    mins = sum((s.get("summary") or {}).get("present_min") or 0.0 for s in sessions)
    if mins < 5:
        return None
    n = sum((s.get("summary") or {}).get(key) or 0 for s in sessions)
    return round(10.0 * n / mins, 2)


TIPS = {
    "habit": "Short daily sessions do more than long occasional ones. Aim for 10 to 20 minutes, at the same time each day.",
    "settle": "Give the body the first minutes on purpose: a few long out-breaths and a quick body scan, then decide to stop adjusting.",
    "excitation": "Restlessness shows in the body as fidgeting. Relax the body, lower your gaze a little, let each out-breath be long and soft, and let itches and thoughts come and go without following them.",
    "laxity": "Dullness shows as the head dropping or nodding. Sit up through the crown, raise your gaze slightly, brighten your attention, and try a cooler room, an earlier time of day or shorter sessions.",
    "posture": "Your most frequent posture drift is {fault}. Check that point when you sit down, then let it go.",
    "deepen": "Your sitting is stable. Keep the length that feels nourishing and let the steadiness carry into the day.",
    "start": "Each session teaches MediSense your usual pattern. After three sessions it shows where you are and what to work on.",
}
FAULT_NAMES = {
    "head_drop": "the head dropping", "chin": "the chin angle", "head_forward": "the head pushing forward",
    "head_level": "the head tilting", "shoulders_level": "uneven shoulders", "shoulders_relaxed": "raised shoulders",
    "lean": "leaning sideways", "slouch": "slouching", "tipped_forward": "tipping forward", "hands": "the hands",
    "head_over_spine": "the head drifting sideways", "face_forward": "turning the face",
}


def profile(metas: list[dict], today: date | None = None) -> dict:
    """Where you are now: the last sessions compared with the ones before."""
    today = today or date.today()
    done = [m for m in metas if m.get("ended_at") and not m.get("demo") and (m.get("summary") or {}).get("present_min")]
    done.sort(key=lambda m: m.get("started_at") or "")
    out: dict = {"sessions": len(done)}
    if not done:
        out.update({"stage": "Getting started", "advice": [TIPS["start"]]})
        return out
    recent, before = done[-7:], done[-14:-7]
    days = [d for d in (_session_date(m) for m in done) if d]
    last28 = [m for m in done if (_session_date(m) or date.min) > today - timedelta(days=28)]
    streak = 0
    day = today
    dayset = set(days)
    if day not in dayset:
        day -= timedelta(days=1)  # today not done yet: the streak still counts
    while day in dayset:
        streak += 1
        day -= timedelta(days=1)
    # over the last 4 weeks, or since your first session if that's sooner (at least a week)
    span_days = max(7, min(28, (today - min(days)).days + 1)) if days else 28
    per_week = round(len(last28) * 7.0 / span_days, 1)
    min_week = round(sum((m.get("duration_s") or 0) for m in last28) / 60.0 * 7.0 / span_days, 0)

    def stat(key, fn=_avg, group=None):
        return fn([(m.get("summary") or {}).get(key) for m in (group if group is not None else recent)])

    out.update({
        "sessions_per_week": per_week,
        "minutes_per_week": min_week,
        "streak_days": streak,
        "steady_pct": stat("steady_pct"),
        "steady_pct_before": stat("steady_pct", group=before) if before else None,
        "settle_s": stat("settle_s", _med),
        "settle_s_before": stat("settle_s", _med, before) if before else None,
        "longest_still_s": stat("longest_still_s", _med),
        "steady_breath": stat("steady_breath", _med),
        "steady_breath_before": stat("steady_breath", _med, before) if before else None,
        "restless_per10": _per10(recent, "restless_n"),
        "drowsy_per10": _per10(recent, "drowsy_n"),
        "posture_aligned_pct": stat("posture_aligned_pct"),
        "recent_n": len(recent),
        "before_n": len(before),
    })
    # which obstacle shows more: agitation (fidgeting) or dullness (nodding)
    r, d = out["restless_per10"] or 0.0, out["drowsy_per10"] or 0.0
    camera = any((m.get("summary") or {}).get("posture_aligned_pct") is not None for m in recent)
    if r >= 1.0 and r >= 1.5 * d:
        lean = "excitation"
    elif d >= 0.5 and d > r:
        lean = "laxity"
    else:
        lean = "balanced"
    out["leaning"] = lean
    out["drowsiness_measured"] = camera

    sp = out["steady_pct"] or 0.0
    if len(done) < 3:
        stage = "Getting started"
    elif per_week < 3:
        stage = "Building the habit"
    elif sp < 40:
        stage = "Settling the body"
    elif sp < 70:
        stage = "Steadying"
    else:
        stage = "Stable sitting"
    out["stage"] = stage

    advice = []
    if len(done) < 3:
        advice.append(TIPS["start"])
    if per_week < 3:
        advice.append(TIPS["habit"])
    if lean == "excitation":
        advice.append(TIPS["excitation"])
    elif lean == "laxity":
        advice.append(TIPS["laxity"])
    if (out["settle_s"] or 0) > 300:
        advice.append(TIPS["settle"])
    faults = Counter()
    for m in recent:
        f = (m.get("summary") or {}).get("posture_main_fault")
        if f:
            faults[f] += 1
    if faults:
        f = faults.most_common(1)[0][0]
        out["posture_main_fault"] = f
        advice.append(TIPS["posture"].format(fault=FAULT_NAMES.get(f, f.replace("_", " "))))
    if stage == "Stable sitting" and lean == "balanced":
        advice.append(TIPS["deepen"])
    out["advice"] = advice[:3]
    return out
