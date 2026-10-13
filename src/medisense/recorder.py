"""Session recording.

Each session is a folder in ~/MediSense/sessions/<date_time>/:

    session.json     what, when, your notes and rating, summary numbers
    metrics.csv      one row per second (breathing, heart, movement, posture)
    wave.csv         breathing waveform and movement, 10 rows per second
    posture.csv      webcam posture measurements (if the camera was on)
    events.jsonl     shifts, marks, watch heart-rate entries, breath counts
    raw.mscap.gz     every packet the board sent, with arrival times, so the
                     session can be re-analysed later with better algorithms

Everything is plain text except the raw capture, which `read_capture` reads.
"""

from __future__ import annotations

import csv
import gzip
import json
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import __version__
from .config import sessions_dir

CAPTURE_MAGIC = b"MSCAP1\n"
_REC = struct.Struct("<dH")

METRIC_FIELDS = [
    "t", "signal", "frames_per_s", "loss_pct", "rssi", "breath_rate", "breath_conf", "breath_regularity",
    "heart_rate", "heart_conf", "motion", "motion_state", "presence", "settled_s",
    "posture_score", "slump", "head_tilt", "shoulder_tilt", "lean",
]
POSTURE_FIELDS = ["t", "score", "slump", "head_drop", "head_tilt", "shoulder_tilt", "lean", "visible", "camera_motion"]


def new_session_id(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")


class SessionRecorder:
    def __init__(self, meta: dict, root: Path | None = None):
        root = root or sessions_dir()
        sid = new_session_id()
        path = root / sid
        n = 1
        while path.exists():
            n += 1
            path = root / f"{sid}-{n}"
        path.mkdir(parents=True)
        self.id = path.name
        self.path = path
        self.t0_mono = time.monotonic()
        self.meta = {
            "id": self.id,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_version": __version__,
            **meta,
        }
        self._write_meta()
        self._metrics_f = open(path / "metrics.csv", "w", newline="")
        self._metrics = csv.DictWriter(self._metrics_f, fieldnames=METRIC_FIELDS, extrasaction="ignore")
        self._metrics.writeheader()
        self._wave_f = open(path / "wave.csv", "w", newline="")
        self._wave = csv.writer(self._wave_f)
        self._wave.writerow(["t", "breath", "motion"])
        self._posture_f = None
        self._posture = None
        self._events_f = open(path / "events.jsonl", "w")
        self._raw = gzip.open(path / "raw.mscap.gz", "wb", compresslevel=5)
        self._raw.write(CAPTURE_MAGIC)
        self._last_flush = time.monotonic()
        self._wave_skip = 0
        self.closed = False

    def rel(self, t_mono: float) -> float:
        return round(t_mono - self.t0_mono, 3)

    def _write_meta(self) -> None:
        tmp = self.path / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2))
        tmp.replace(self.path / "session.json")

    def raw(self, t_wall: float, packet: bytes) -> None:
        if self.closed:
            return
        self._raw.write(_REC.pack(t_wall, len(packet)))
        self._raw.write(packet)

    def metrics(self, t_mono: float, row: dict) -> None:
        if self.closed:
            return
        self._metrics.writerow({**row, "t": self.rel(t_mono)})
        self._maybe_flush()

    def wave(self, samples: list[tuple[float, float | None, float]]) -> None:
        if self.closed:
            return
        for t, b, m in samples:
            self._wave_skip ^= 1
            if self._wave_skip:  # 20 Hz -> 10 Hz on disk
                self._wave.writerow([self.rel(t), "" if b is None else f"{b:.4f}", f"{m:.3f}"])

    def posture(self, t_mono: float, p: dict) -> None:
        if self.closed:
            return
        if self._posture is None:
            self._posture_f = open(self.path / "posture.csv", "w", newline="")
            self._posture = csv.DictWriter(self._posture_f, fieldnames=POSTURE_FIELDS, extrasaction="ignore")
            self._posture.writeheader()
        self._posture.writerow({**p, "t": self.rel(t_mono)})

    def event(self, t_mono: float, kind: str, **data) -> dict:
        ev = {"t": self.rel(t_mono), "kind": kind, **data}
        if not self.closed:
            self._events_f.write(json.dumps(ev) + "\n")
            self._events_f.flush()
        return ev

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= 10:
            self._last_flush = now
            for f in (self._metrics_f, self._wave_f, self._posture_f, self._raw):
                if f is not None:
                    f.flush()

    def update_meta(self, **fields) -> None:
        self.meta.update(fields)
        self._write_meta()

    def close(self, **fields) -> None:
        if self.closed:
            return
        self.closed = True
        self.meta["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self.meta["duration_s"] = round(time.monotonic() - self.t0_mono, 1)
        self.meta.update(fields)
        for f in (self._metrics_f, self._wave_f, self._posture_f, self._events_f, self._raw):
            if f is not None:
                f.close()
        self._write_meta()


def read_capture(path: Path) -> Iterator[tuple[float, bytes]]:
    """Yield (wall time, packet) from a raw capture, tolerating a cut-off end."""
    with gzip.open(path, "rb") as f:
        if f.read(len(CAPTURE_MAGIC)) != CAPTURE_MAGIC:
            raise ValueError(f"{path} is not a MediSense capture")
        while True:
            try:
                head = f.read(_REC.size)
                if len(head) < _REC.size:
                    return
                t, n = _REC.unpack(head)
                pkt = f.read(n)
                if len(pkt) < n:
                    return
            except (EOFError, OSError):
                return
            yield t, pkt
