"""Posture scoring against the seven-point posture of Vairocana.

Works on MediaPipe Pose landmarks (33 points, image-normalised x/y/z with
visibility, plus "world" coordinates in metres), wherever they come from: the
native background camera engine or the browser.

The seven points and what a camera in front of you can check:

1. Legs crossed (vajra / half-lotus)        - only if the knees are in view; info only
2. Hands in the meditation mudra             - wrists together, low, centred (if in view)
3. Spine straight "like an arrow"            - no sideways lean, no slouch, not tipped forward
4. Shoulders level and open                  - level, not hunched up towards the ears
5. Neck slightly bent, chin a little tucked  - head neither dropping nor raised, not pushed forward, level
6. Eyes gazing down past the nose            - not checked by the camera
7. Tongue to the palate, jaw relaxed         - not checked by the camera

Absolute rules (level shoulders, level head, no sideways lean) need no
setup. Relative rules (head height, chin, shoulders up, slouch, head forward)
compare with *your* best posture, captured once with "Calibrate".
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np

# MediaPipe Pose landmark indices
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 2, 5, 7, 8
L_SH, R_SH, L_WR, R_WR, L_HIP, R_HIP, L_KNEE, R_KNEE = 11, 12, 15, 16, 23, 24, 25, 26
VIS = 0.5

MODES = ("meditation", "desk")


@dataclass
class Check:
    key: str
    point: str  # which of the seven points
    status: str  # ok / minor / major / not_visible / needs_calibration / not_checked / off
    penalty: float = 0.0
    value: float | None = None
    advice: str = ""


@dataclass
class Assessment:
    score: float | None  # 0..100, None when nobody is visible
    checks: list[Check]
    visible: bool
    calibrated: bool

    @property
    def worst(self) -> list[Check]:
        bad = [c for c in self.checks if c.penalty > 0]
        return sorted(bad, key=lambda c: -c.penalty)

    def as_dict(self) -> dict:
        return {
            "score": None if self.score is None else round(self.score, 1),
            "visible": self.visible,
            "calibrated": self.calibrated,
            "checks": [asdict(c) for c in self.checks],
            "advice": [c.advice for c in self.worst[:2]],
        }


def _pt(lm: np.ndarray, i: int, aspect: float) -> tuple[float, float, float]:
    return float(lm[i, 0]) * aspect, float(lm[i, 1]), float(lm[i, 3]) if lm.shape[1] > 3 else 1.0


def _vis(lm: np.ndarray, *idx: int) -> bool:
    return lm.shape[1] < 4 or all(float(lm[i, 3]) >= VIS for i in idx)


def _deg(dy: float, dx: float) -> float:
    a = math.degrees(math.atan2(dy, dx))
    # a left-to-right line can come out near ±180 depending on orientation
    if a > 90:
        a -= 180
    elif a < -90:
        a += 180
    return a


def measure(lm, world=None, aspect: float = 4 / 3) -> dict:
    """Raw posture measures from one frame (None where the body part isn't visible).

    `lm`: 33 x 4 (x, y, z, visibility), image-normalised. `world`: 33 x 3 metres.
    Image y points down; the person's left side is on the image's right.
    """
    lm = np.asarray(lm, dtype=np.float64)
    wl = None if world is None else np.asarray(world, dtype=np.float64)
    m: dict = {"visible": False}
    if lm.shape[0] < 33 or not _vis(lm, L_SH, R_SH, NOSE):
        return m
    m["visible"] = True
    lsx, lsy, _ = _pt(lm, L_SH, aspect)
    rsx, rsy, _ = _pt(lm, R_SH, aspect)
    nx, ny, _ = _pt(lm, NOSE, aspect)
    smx, smy = (lsx + rsx) / 2, (lsy + rsy) / 2
    sw = math.hypot(lsx - rsx, lsy - rsy) or 1e-6
    m["shoulder_width"] = sw
    m["shoulder_y"] = smy  # absolute: the camera doesn't move during a sit
    m["nose_y"] = ny
    m["shoulder_roll"] = _deg(lsy - rsy, lsx - rsx)  # + = left shoulder lower
    m["head_height"] = (smy - ny) / sw  # smaller = head lower relative to shoulders
    m["head_offset"] = (nx - smx) / sw  # + = head shifted to the person's left
    if _vis(lm, L_EAR, R_EAR):
        a, b = L_EAR, R_EAR
    elif _vis(lm, L_EYE, R_EYE):
        a, b = L_EYE, R_EYE
    else:
        a = b = None
    if a is not None:
        ax, ay, _ = _pt(lm, a, aspect)
        bx, by, _ = _pt(lm, b, aspect)
        emx, emy = (ax + bx) / 2, (ay + by) / 2
        ed = math.hypot(ax - bx, ay - by) or 1e-6
        m["head_roll"] = _deg(ay - by, ax - bx)  # + = head tipped towards the person's left
        m["head_pitch"] = (ny - emy) / ed  # larger = chin lower
        m["head_yaw"] = (nx - emx) / ed
        m["face_size"] = ed / sw  # grows when the head moves towards the camera
        m["ear_height"] = (smy - emy) / sw  # smaller = shoulders up (or head down)
    if _vis(lm, L_HIP, R_HIP):
        lhx, lhy, _ = _pt(lm, L_HIP, aspect)
        rhx, rhy, _ = _pt(lm, R_HIP, aspect)
        hmx, hmy = (lhx + rhx) / 2, (lhy + rhy) / 2
        m["lean"] = math.degrees(math.atan2(smx - hmx, hmy - smy))  # + = leaning to the person's left
        m["torso_height"] = (hmy - smy) / sw
        m["hip_width"] = math.hypot(lhx - rhx, lhy - rhy) / sw
        if wl is not None:
            m["torso_forward"] = float((wl[L_HIP, 2] + wl[R_HIP, 2]) / 2 - (wl[L_SH, 2] + wl[R_SH, 2]) / 2)
    if wl is not None and _vis(lm, L_EAR, R_EAR):
        m["head_forward"] = float((wl[L_SH, 2] + wl[R_SH, 2]) / 2 - (wl[L_EAR, 2] + wl[R_EAR, 2]) / 2)
    if _vis(lm, L_WR, R_WR):
        lwx, lwy, _ = _pt(lm, L_WR, aspect)
        rwx, rwy, _ = _pt(lm, R_WR, aspect)
        m["wrist_gap"] = math.hypot(lwx - rwx, lwy - rwy) / sw
        m["wrist_low"] = ((lwy + rwy) / 2 - smy) / sw
        m["wrist_centre"] = abs((lwx + rwx) / 2 - smx) / sw
    if _vis(lm, L_KNEE, R_KNEE, L_HIP, R_HIP):
        lkx, lky, _ = _pt(lm, L_KNEE, aspect)
        rkx, rky, _ = _pt(lm, R_KNEE, aspect)
        m["knee_spread"] = math.hypot(lkx - rkx, lky - rky) / sw
    return m


RELATIVE_KEYS = ("head_height", "head_pitch", "face_size", "ear_height", "torso_height",
                 "torso_forward", "head_forward", "shoulder_y", "nose_y", "shoulder_width",
                 "shoulder_roll", "head_roll", "lean", "head_offset")
# Once calibrated, "level" checks measure drift from your own posture (bodies
# and cameras are rarely perfectly level); before that, from the horizontal.
LEVEL_KEYS = ("shoulder_roll", "head_roll", "lean", "head_offset")


def _pen(dev: float, tol: float, slope: float, cap: float) -> float:
    p = float(min(cap, max(0.0, abs(dev) - tol) * slope))
    return p if p >= 1.0 else 0.0  # under a point: within normal sway, not worth a remark


def _status(p: float) -> str:
    return "ok" if p <= 0 else ("minor" if p < 10 else "major")


def assess(m: dict, calibration: dict | None, mode: str = "meditation") -> Assessment:
    """Score one (smoothed) set of measures against the Vairocana points."""
    cal = calibration or {}
    if not m.get("visible"):
        return Assessment(None, [], False, bool(cal))
    checks: list[Check] = []
    desk = mode == "desk"
    m = dict(m)
    for k in LEVEL_KEYS:
        if m.get(k) is not None and cal.get(k) is not None:
            m[k] = m[k] - cal[k]
    # absolute shifts since calibration, in shoulder widths
    sw0 = cal.get("shoulder_width") or m.get("shoulder_width") or 1.0
    shoulders_up = nose_down = None
    if cal.get("shoulder_y") is not None and m.get("shoulder_y") is not None:
        shoulders_up = (cal["shoulder_y"] - m["shoulder_y"]) / sw0
        nose_down = (m["nose_y"] - cal["nose_y"]) / sw0

    def rel(key):
        if m.get(key) is None:
            return None, "not_visible"
        if cal.get(key) in (None, 0):
            return None, "needs_calibration"
        return m[key], "ok"

    # 3. Spine straight like an arrow
    if m.get("lean") is not None:
        p = _pen(m["lean"], 3.0, 3.0, 25)
        side = "left" if m["lean"] > 0 else "right"
        checks.append(Check("lean", "spine", _status(p), p, round(m["lean"], 1),
                            f"Straighten your spine like an arrow: you're leaning to your {side}."))
    else:
        p = _pen(m["head_offset"], 0.07, 150.0, 15)
        side = "left" if m["head_offset"] > 0 else "right"
        checks.append(Check("head_over_spine", "spine", _status(p), p, round(m["head_offset"], 3),
                            f"Bring your head back over the centre of your spine (it's drifting {side})."))
    v, st = rel("torso_height")
    if v is not None:
        drop = 1 - v / cal["torso_height"]
        p = _pen(drop * 100, 4.0, 2.0, 30) if drop > 0 else 0.0
        checks.append(Check("slouch", "spine", _status(p), p, round(drop, 3),
                            "Lift through the spine, like a stack of coins: you're slouching."))
    elif st == "needs_calibration":
        checks.append(Check("slouch", "spine", st))
    v, st = rel("torso_forward")
    if v is not None:
        d = v - cal["torso_forward"]  # negative = shoulders came towards the camera
        p = _pen(min(0.0, d), 0.03, 400.0, 25)
        checks.append(Check("tipped_forward", "spine", _status(p), p, round(d, 3),
                            "Sit back over your sitting bones: your upper body is tipping forward."))

    # 4. Shoulders level and open
    p = _pen(m["shoulder_roll"], 2.5, 3.0, 20)
    side = "left" if m["shoulder_roll"] > 0 else "right"
    checks.append(Check("shoulders_level", "shoulders", _status(p), p, round(m["shoulder_roll"], 1),
                        f"Level your shoulders: your {side} shoulder is lower."))
    head_drop = None
    if m.get("head_height") is not None and cal.get("head_height"):
        head_drop = 1 - m["head_height"] / cal["head_height"]
    if shoulders_up is not None:
        # A shrug lifts the shoulders while the head stays put; a dropping head
        # sinks while the shoulders stay. Both shrink the nose-to-shoulder gap,
        # so tell them apart by what actually moved.
        shrug = shoulders_up if shoulders_up > 0 and nose_down < 0.5 * shoulders_up else 0.0
        if shrug > 0 and head_drop is not None:
            head_drop = max(0.0, nose_down) * 0.8
        p = _pen(shrug * 100, 4.0, 2.5, 20)
        checks.append(Check("shoulders_relaxed", "shoulders", _status(p), p, round(shrug, 3),
                            "Let your shoulders drop and open, like a vulture's wings."))
    else:
        checks.append(Check("shoulders_relaxed", "shoulders", "needs_calibration"))

    # 5. Neck slightly bent, chin a little tucked
    if head_drop is not None:
        p = _pen(head_drop * 100, 6.0 if not desk else 10.0, 2.2, 35) if head_drop > 0 else 0.0
        checks.append(Check("head_drop", "head", _status(p), p, round(head_drop, 3),
                            "Your head is dropping: lift through the crown. (Often a sign of dullness: brighten your attention.)"))
    else:
        checks.append(Check("head_drop", "head", "needs_calibration"))
    v, st = rel("head_pitch")
    if v is not None:
        d = v - cal["head_pitch"]
        if d > 0:
            p = _pen(d, 0.08 if not desk else 0.15, 120.0, 25)
            adv = "Chin too far down: keep it only slightly tucked."
        else:
            p = _pen(d, 0.06, 150.0, 25)
            adv = "Tuck your chin slightly: your head is tilted back."
        checks.append(Check("chin", "head", _status(p), p, round(d, 3), adv))
    if m.get("head_forward") is not None and cal.get("head_forward") is not None:
        d = m["head_forward"] - cal["head_forward"]
        p = _pen(max(0.0, d), 0.03, 300.0, 20)
        checks.append(Check("head_forward", "head", _status(p), p, round(d, 3),
                            "Bring your head back over your shoulders."))
    elif m.get("face_size") is not None and cal.get("face_size"):
        d = m["face_size"] / cal["face_size"] - 1
        p = _pen(max(0.0, d) * 100, 6.0, 2.0, 20)
        checks.append(Check("head_forward", "head", _status(p), p, round(d, 3),
                            "Bring your head back over your shoulders."))
    if m.get("head_roll") is not None:
        p = _pen(m["head_roll"], 3.0, 2.5, 20)
        side = "left" if m["head_roll"] > 0 else "right"
        checks.append(Check("head_level", "head", _status(p), p, round(m["head_roll"], 1),
                            f"Your head is tilted to your {side}: bring it level."))
    if m.get("head_yaw") is not None:
        p = _pen(m["head_yaw"], 0.12, 60.0, 10)
        checks.append(Check("face_forward", "head", _status(p), p, round(m["head_yaw"], 3), "Face straight ahead."))

    # 2. Hands in the meditation mudra (meditation mode, when the hands are in view)
    if desk:
        checks.append(Check("hands", "hands", "off"))
        checks.append(Check("legs", "legs", "off"))
    else:
        if m.get("wrist_gap") is not None:
            p = 0.0
            p += 4.0 if m["wrist_gap"] > 0.8 else 0.0
            p += 3.0 if m["wrist_low"] < 1.2 else 0.0
            p += 3.0 if m["wrist_centre"] > 0.35 else 0.0
            checks.append(Check("hands", "hands", _status(p), p, round(m["wrist_gap"], 2),
                                "Rest your hands in your lap, right on left, thumbs lightly touching."))
        else:
            checks.append(Check("hands", "hands", "not_visible"))
        # 1. Legs: information only
        if m.get("knee_spread") is not None and m.get("hip_width"):
            crossed = m["knee_spread"] > 1.4 * m["hip_width"]
            checks.append(Check("legs", "legs", "ok" if crossed else "minor", 0.0, round(m["knee_spread"], 2),
                                "" if crossed else "Cross your legs, knees resting low and wide."))
        else:
            checks.append(Check("legs", "legs", "not_visible"))
    checks.append(Check("eyes", "eyes", "not_checked"))
    checks.append(Check("jaw", "mouth", "not_checked"))

    total = min(100.0, sum(c.penalty for c in checks))
    return Assessment(100.0 - total, checks, True, bool(cal))


# --------------------------------------------------------------- live tracker

@dataclass
class AlertSettings:
    enabled: bool = True
    threshold: float = 80.0  # alert when the score stays below this
    hold_s: float = 20.0  # ... for this long
    cooldown_s: float = 180.0  # at most one alert per this long
    sound: bool = True
    mode: str = "meditation"

    @classmethod
    def from_dict(cls, d: dict) -> "AlertSettings":
        a = cls()
        for k, v in (d or {}).items():
            if hasattr(a, k) and v is not None:
                setattr(a, k, type(getattr(a, k))(v))
        a.threshold = float(min(98.0, max(30.0, a.threshold)))
        a.hold_s = float(min(600.0, max(3.0, a.hold_s)))
        a.cooldown_s = float(min(3600.0, max(30.0, a.cooldown_s)))
        if a.mode not in MODES:
            a.mode = "meditation"
        return a


class PostureTracker:
    """Smooths frames, scores them, captures calibration, raises alerts and events.

    Feed it frames with `update()`; read `.current` (dict for the UI).
    `notify(title, message, sound)` is called when an alert fires.
    """

    def __init__(self, store: Path | None = None, notify: Callable[[str, str, bool], None] | None = None,
                 settings: AlertSettings | None = None, settings_key: str = "settings"):
        self.store = store
        self.notify = notify
        self.settings_key = settings_key  # MediSense Posture and MediSense keep their own settings
        self.settings = settings or AlertSettings()
        self.calibrations: dict = {}
        self._store_mtime = 0.0
        self._next_store_check = 0.0
        self._dirty_modes: set[str] = set()
        self._load()
        self._hist: deque = deque()  # (t, measures)
        self._calib: list | None = None
        self._calib_until = 0.0
        self.current: dict = {"score": None, "visible": False, "calibrated": self.calibrated,
                              "checks": [], "advice": [], "state": "waiting"}
        self._bad_since: float | None = None
        self._good_since: float | None = None
        self._last_alert = -1e9
        self.alerts = 0
        self.events: list[tuple[float, str, dict]] = []  # (t, kind, data) for sessions
        self._pitch_hist: deque = deque()
        self._last_seen = -1e9
        self.score_ema: float | None = None

    # -- persistence -------------------------------------------------------

    @property
    def calibration(self) -> dict | None:
        return self.calibrations.get(self.settings.mode)

    @property
    def calibrated(self) -> bool:
        return bool(self.calibration)

    def _read_store(self) -> dict:
        try:
            data = json.loads(self.store.read_text()) if self.store and self.store.exists() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load(self, settings: bool = True) -> None:
        data = self._read_store()
        self.calibrations = data.get("calibrations", {}) or {}
        if settings and isinstance(data.get(self.settings_key), dict):
            self.settings = AlertSettings.from_dict({**asdict(self.settings), **data[self.settings_key]})
        try:
            self._store_mtime = self.store.stat().st_mtime if self.store else 0.0
        except OSError:
            self._store_mtime = 0.0

    def _maybe_reload(self, t: float) -> None:
        """Pick up a calibration made in the other app (they share the file)."""
        if not self.store or t < self._next_store_check:
            return
        self._next_store_check = t + 5.0
        try:
            mtime = self.store.stat().st_mtime
        except OSError:
            return
        if mtime != self._store_mtime:
            self._load(settings=False)

    def save(self) -> None:
        if self.store:
            data = self._read_store()  # keep the other app's settings and calibrations
            cals = data.get("calibrations") or {}
            for mode in self._dirty_modes:
                if mode in self.calibrations:
                    cals[mode] = self.calibrations[mode]
            self._dirty_modes.clear()
            self.calibrations = cals
            data["calibrations"] = cals
            data[self.settings_key] = asdict(self.settings)
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(f".{self.settings_key}.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.store)
            try:
                self._store_mtime = self.store.stat().st_mtime
            except OSError:
                pass

    def set_settings(self, d: dict) -> None:
        self.settings = AlertSettings.from_dict({**asdict(self.settings), **(d or {})})
        self.save()

    def start_calibration(self, seconds: float = 5.0) -> None:
        self._calib = []
        self._calib_until = time.monotonic() + seconds

    # -- frames ------------------------------------------------------------

    def update(self, lm, world=None, aspect: float = 4 / 3, t: float | None = None) -> dict:
        t = time.monotonic() if t is None else t
        self._maybe_reload(t)
        m = measure(lm, world, aspect) if lm is not None else {"visible": False}
        if m.get("visible"):
            self._last_seen = t
            self._hist.append((t, m))
            self._detect_nod(t, m)
            if self._calib is not None:
                self._calib.append(m)
        while self._hist and t - self._hist[0][0] > 2.0:
            self._hist.popleft()
        if self._calib is not None and t >= self._calib_until:
            self._finish_calibration()
        if not self._hist or t - self._last_seen > 3.0:
            self.current = {"score": None, "visible": False, "calibrated": self.calibrated, "checks": [],
                            "advice": [], "state": "away" if self._last_seen > 0 else "waiting"}
            self._bad_since = None
            return self.current
        smooth = _median_measures([h for _, h in self._hist])
        a = assess(smooth, self.calibration, self.settings.mode)
        self.score_ema = a.score if self.score_ema is None else 0.6 * self.score_ema + 0.4 * a.score
        cur = a.as_dict()
        cur["score"] = round(self.score_ema, 1)
        cur["state"] = "calibrating" if self._calib is not None else self._alert_state(t, self.score_ema, a)
        cur["mode"] = self.settings.mode
        self.current = cur
        return cur

    def _finish_calibration(self) -> None:
        samples = self._calib or []
        self._calib = None
        if len(samples) < 3:
            return
        cal = {}
        for k in RELATIVE_KEYS:
            vals = [s[k] for s in samples if s.get(k) is not None]
            if vals:
                cal[k] = float(np.median(vals))
        if cal:
            self.calibrations[self.settings.mode] = cal
            self._dirty_modes.add(self.settings.mode)
            self.save()

    def _detect_nod(self, t: float, m: dict) -> None:
        """A nod: the chin drops quickly, then comes back up (a sign of drowsiness)."""
        if m.get("head_pitch") is None:
            return
        self._pitch_hist.append((t, m["head_pitch"]))
        while self._pitch_hist and t - self._pitch_hist[0][0] > 4.0:
            self._pitch_hist.popleft()
        if len(self._pitch_hist) < 6:
            return
        ps = np.array([p for _, p in self._pitch_hist])
        base = float(np.median(ps[: len(ps) // 3]))
        peak_i = int(np.argmax(ps))
        if ps[peak_i] - base > 0.18 and ps[-1] - base < 0.06 and 0 < peak_i < len(ps) - 1:
            self.events.append((t, "nod", {"depth": round(float(ps[peak_i] - base), 3)}))
            self._pitch_hist.clear()

    def _alert_state(self, t: float, score: float, a: Assessment) -> str:
        s = self.settings
        if score < s.threshold:
            self._good_since = None
            if self._bad_since is None:
                self._bad_since = t
            elif t - self._bad_since >= s.hold_s and t - self._last_alert >= s.cooldown_s:
                self._last_alert = t
                self.alerts += 1
                tips = [c.advice for c in a.worst[:2] if c.advice]
                msg = " ".join(tips) or "Your posture has drifted."
                self.events.append((t, "posture_alert", {"score": round(score), "advice": tips}))
                if s.enabled and self.notify:
                    self.notify(f"Posture {round(score)}/100", msg, s.sound)
            return "drifting"
        if self._bad_since is not None:
            if self._good_since is None:
                self._good_since = t
            elif t - self._good_since >= 5.0 or score >= s.threshold + 5:
                self._bad_since = None
        return "aligned" if score >= 90 else "ok"

    def drain_events(self) -> list[tuple[float, str, dict]]:
        out, self.events = self.events, []
        return out


def _median_measures(ms: list[dict]) -> dict:
    out: dict = {"visible": True}
    keys = set().union(*ms) - {"visible"}
    for k in keys:
        vals = [m[k] for m in ms if m.get(k) is not None]
        # a body part counts as visible if it was seen in most recent frames
        if vals and len(vals) >= max(1, len(ms) // 2):
            out[k] = float(np.median(vals))
    return out
