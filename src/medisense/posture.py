"""Posture scoring, in two flavours.

* **Meditation** (MediSense sessions): the seven-point posture of Vairocana
  as Lama Yonten Gyaltso teaches it in "Nectar of Liberation" (Нектар
  освобождения, 2018), in his order:

  1. Legs       vajra (lotus) posture, or at least simply cross-legged
  2. Hands      palms four finger-widths below the navel, right on left,
                thumb tips joined at the navel
  3. Back       fully straight
  4. Shoulders  open and level; no stooping
  5. Head       chin lowered, lightly drawn in towards the throat
  6. Tongue     touching the palate                     (not visible to a camera)
  7. Gaze       down along the line of the nose, eyes half-closed, steady,
                not looking around                      (head turning is checked)

  plus his list of errors: leaning right, left, forward or back.

* **Office** (MediSense Posture, all day at the computer): hands and legs are
  free for the keyboard; it watches only a healthy working posture: back
  upright (no slumping, no leaning in), shoulders relaxed and level, head
  over the shoulders (not pushed towards the screen, not hanging), sitting
  centred.

Both work on MediaPipe Pose landmarks (33 points, image-normalised, plus
"world" coordinates in metres), from the background camera engine or the
browser. Level checks work without setup; the rest compare with *your* good
posture, captured once with "Calibrate" (separately for each flavour).
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
    point: str  # which point of the posture (see POINTS)
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
    mode: str = "meditation"

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
            "points": summarize_points(self.checks, self.mode),
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


# The points each flavour shows, in order: (key, name, what it asks for).
POINTS = {
    "meditation": [  # Lama Yonten Gyaltso, "Nectar of Liberation": the seven-point posture of Vairocana
        ("legs", "Legs", "Vajra (lotus) posture, or at least simply cross-legged"),
        ("hands", "Hands", "Palms four finger-widths below the navel, right on left, thumb tips joined at the navel"),
        ("back", "Back", "Fully straight"),
        ("shoulders", "Shoulders", "Open and level; no stooping"),
        ("head", "Head", "Chin lowered, lightly drawn in towards the throat"),
        ("tongue", "Tongue", "Touching the palate"),
        ("gaze", "Gaze", "Down along the line of the nose, eyes half-closed, steady on one point"),
    ],
    "desk": [  # a healthy working posture at the computer
        ("back", "Back upright", "Sit tall, supported; no slumping and no leaning in towards the screen"),
        ("shoulders", "Shoulders relaxed and level", "Down and back, not hunched up or dropped to one side"),
        ("head", "Head over the shoulders", "Ears above the shoulders, not pushed towards the screen or hanging down"),
        ("centre", "Sitting centred", "Not leaning to one side or propped on an elbow; head level"),
    ],
}
STATUS_RANK = {"major": 6, "minor": 5, "needs_calibration": 4, "ok": 3, "not_visible": 2, "not_checked": 1, "off": 0}


def summarize_points(checks: list[Check], mode: str) -> list[dict]:
    """One line per point of the posture: its worst check, for the pages."""
    out = []
    for key, name, how in POINTS.get(mode, POINTS["meditation"]):
        mine = [c for c in checks if c.point == key]
        worst = max(mine, key=lambda c: (STATUS_RANK.get(c.status, 0), c.penalty), default=None)
        out.append({
            "key": key, "name": name, "how": how,
            "status": worst.status if worst else "not_visible",
            "advice": worst.advice if worst is not None and worst.penalty > 0 else "",
        })
    return out


def assess(m: dict, calibration: dict | None, mode: str = "meditation") -> Assessment:
    """Score one (smoothed) set of measures: Vairocana ("meditation") or office ("desk")."""
    cal = calibration or {}
    desk = mode == "desk"
    if not m.get("visible"):
        return Assessment(None, [], False, bool(cal), mode)
    checks: list[Check] = []
    m = dict(m)
    for k in LEVEL_KEYS:
        if m.get(k) is not None and cal.get(k) is not None:
            m[k] = m[k] - cal[k]
    # absolute shifts since calibration, in shoulder widths (+ = moved down)
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

    BACK = "back"
    SIDE = "centre" if desk else "back"  # leaning sideways: its own point at the desk, the back in meditation

    # -- leaning sideways (both); Lama Yonten Gyaltso: right stirs anger, left desire
    if m.get("lean") is not None:
        p = _pen(m["lean"], 3.0, 3.0, 25)
        side = "left" if m["lean"] > 0 else "right"
        adv = (f"Sit centred: you're leaning to your {side}." if desk else
               f"You're leaning to your {side}: come back to the centre. "
               + ("(Leaning left stirs desire and attachment.)" if side == "left" else "(Leaning right stirs anger.)"))
        checks.append(Check("lean", SIDE, _status(p), p, round(m["lean"], 1), adv))
    else:
        p = _pen(m["head_offset"], 0.07, 150.0, 15)
        side = "left" if m["head_offset"] > 0 else "right"
        checks.append(Check("head_over_spine", SIDE, _status(p), p, round(m["head_offset"], 3),
                            f"Bring your head back over the middle of your body (it's drifting to your {side})."))

    # -- slouching
    v, st = rel("torso_height")
    if v is not None:
        drop = 1 - v / cal["torso_height"]
        p = _pen(drop * 100, 4.0, 2.0, 30) if drop > 0 else 0.0
        checks.append(Check("slouch", BACK, _status(p), p, round(drop, 3),
                            "Sit up tall: you're slumping." if desk else "Straighten your back fully: you're slouching."))
    elif shoulders_up is not None and desk:
        # hips out of view (usual at a desk): sliding down in the chair lowers
        # both the shoulders and the head in the picture
        sink = min(-shoulders_up, nose_down)
        p = _pen(sink * 100, 5.0, 2.5, 30) if sink > 0 else 0.0
        checks.append(Check("slouch", BACK, _status(p), p, round(sink, 3), "Sit up tall: you're slumping in the chair."))
    elif st == "needs_calibration" or (desk and shoulders_up is None):
        checks.append(Check("slouch", BACK, "needs_calibration"))

    # -- tipped forward / leaning back. With depth (MediaPipe world landmarks,
    # hips in view): how far the shoulders are in front of the hips (smaller z
    # = closer to the camera). Without (Apple Vision gives 2-D points): the
    # upper body's size in the picture, which grows when you lean towards the
    # camera and shrinks when you lean back.
    fwd_adv = ("Sit back in your chair: you're leaning in towards the screen." if desk else
               "You're leaning forward: sit upright. (Leaning forward stirs ignorance, even when the mind seems quiet.)")
    back_adv = "You're leaning back: bring the body upright. (Leaning back stirs pride.)"
    fwd = back = None
    v, st = rel("torso_forward")
    if v is not None:
        d = v - cal["torso_forward"]  # + = the shoulders came towards the camera
        fwd = _pen(max(0.0, d), 0.03, 400.0, 25)
        back = _pen(max(0.0, -d), 0.04, 300.0, 20)
        value = round(d, 3)
    elif m.get("shoulder_width") and cal.get("shoulder_width"):
        scale = m["shoulder_width"] / cal["shoulder_width"] - 1
        fwd = _pen(max(0.0, scale) * 100, 8.0 if desk else 6.0, 2.5, 25)
        back = _pen(max(0.0, -scale) * 100, 8.0, 2.0, 20)
        value = round(scale, 3)
    if fwd is not None:
        if desk:
            back = 0.0  # reclining a little at a desk is fine
        if fwd >= back:
            checks.append(Check("tipped_forward", BACK, _status(fwd), fwd, value, fwd_adv))
        else:
            checks.append(Check("leaning_back", BACK, _status(back), back, value, back_adv))

    # -- shoulders: level, relaxed (open)
    p = _pen(m["shoulder_roll"], 2.5, 3.0, 20)
    side = "left" if m["shoulder_roll"] > 0 else "right"
    checks.append(Check("shoulders_level", "shoulders", _status(p), p, round(m["shoulder_roll"], 1),
                        f"Level your shoulders: your {side} shoulder is lower."
                        + (" Leaning on one elbow?" if desk else "")))
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
                            "Let your shoulders drop: they're creeping up towards your ears." if desk else
                            "Open your shoulders and let them settle; don't hunch."))
    else:
        checks.append(Check("shoulders_relaxed", "shoulders", "needs_calibration"))

    # -- head
    if head_drop is not None:
        p = _pen(head_drop * 100, 10.0 if desk else 6.0, 2.2, 35) if head_drop > 0 else 0.0
        checks.append(Check("head_drop", "head", _status(p), p, round(head_drop, 3),
                            "Lift your head: looking down a lot strains the neck. If it keeps happening, raise the screen to eye level."
                            if desk else
                            "Your head is sinking: lift through the crown. (A sinking head often comes with dullness: brighten your attention.)"))
    else:
        checks.append(Check("head_drop", "head", "needs_calibration"))
    v, st = rel("head_pitch")
    if v is not None:
        d = v - cal["head_pitch"]
        if d > 0:
            p = _pen(d, 0.15 if desk else 0.08, 120.0, 25)
            adv = ("Chin down a lot: bring your head up to neutral." if desk else
                   "Chin too far down: lower it only slightly, lightly drawn in.")
        else:
            p = _pen(d, 0.10 if desk else 0.06, 150.0, 25)
            adv = ("Your chin is up: bring the head back to neutral." if desk else
                   "Lower your chin, drawing it lightly in towards the throat.")
        checks.append(Check("chin", "head", _status(p), p, round(d, 3), adv))
    fwd_adv = ("Bring your head back over your shoulders: it's drifting towards the screen." if desk else
               "Bring your head back over your shoulders.")
    if m.get("head_forward") is not None and cal.get("head_forward") is not None:
        d = m["head_forward"] - cal["head_forward"]
        p = _pen(max(0.0, d), 0.04 if desk else 0.03, 300.0, 25)
        checks.append(Check("head_forward", "head", _status(p), p, round(d, 3), fwd_adv))
    elif m.get("face_size") is not None and cal.get("face_size"):
        d = m["face_size"] / cal["face_size"] - 1
        p = _pen(max(0.0, d) * 100, 10.0 if desk else 6.0, 2.0, 25)
        checks.append(Check("head_forward", "head", _status(p), p, round(d, 3), fwd_adv))
    if m.get("head_roll") is not None:
        p = _pen(m["head_roll"], 3.0, 2.5, 20)
        side = "left" if m["head_roll"] > 0 else "right"
        checks.append(Check("head_level", SIDE if desk else "head", _status(p), p, round(m["head_roll"], 1),
                            f"Your head is tilted to your {side}: bring it level."))

    if not desk:
        # gaze: the eyes can't be seen well enough, but looking around turns the head
        if m.get("head_yaw") is not None:
            p = _pen(m["head_yaw"], 0.12, 60.0, 10)
            checks.append(Check("face_forward", "gaze", _status(p), p, round(m["head_yaw"], 3),
                                "Keep the gaze steady, down along the line of the nose; don't look around."))
        else:
            checks.append(Check("face_forward", "gaze", "not_visible"))
        # hands: four finger-widths below the navel, right on left, thumbs joined
        if m.get("wrist_gap") is not None:
            p = 0.0
            p += 4.0 if m["wrist_gap"] > 0.8 else 0.0
            p += 3.0 if m["wrist_low"] < 1.2 else 0.0
            p += 3.0 if m["wrist_centre"] > 0.35 else 0.0
            checks.append(Check("hands", "hands", _status(p), p, round(m["wrist_gap"], 2),
                                "Rest the palms four finger-widths below the navel, right on left, thumb tips joined."))
        else:
            checks.append(Check("hands", "hands", "not_visible"))
        # legs: information only
        if m.get("knee_spread") is not None and m.get("hip_width"):
            crossed = m["knee_spread"] > 1.4 * m["hip_width"]
            checks.append(Check("legs", "legs", "ok" if crossed else "minor", 0.0, round(m["knee_spread"], 2),
                                "" if crossed else "Sit in the vajra posture, or at least cross your legs."))
        else:
            checks.append(Check("legs", "legs", "not_visible"))
        checks.append(Check("tongue", "tongue", "not_checked"))

    total = min(100.0, sum(c.penalty for c in checks))
    return Assessment(100.0 - total, checks, True, bool(cal), mode)


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
                              "checks": [], "advice": [], "state": "waiting",
                              "points": summarize_points([], self.settings.mode), "mode": self.settings.mode}
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
                            "advice": [], "state": "away" if self._last_seen > 0 else "waiting",
                            "points": summarize_points([], self.settings.mode), "mode": self.settings.mode}
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
