"""Runs the background camera engine and feeds the posture tracker.

Used by MediSense (meditation sessions) and MediSense Posture (all-day
posture reminders). The engine is a separate process using the posture
environment (.venv-posture); if that isn't installed, pages fall back to the
browser camera and send landmarks here instead (same scoring either way).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

from . import assets
from .config import REPO_ROOT
from .posture import PostureTracker

log = logging.getLogger("medisense.posture")

ENGINE = Path(__file__).with_name("posture_engine.py")


def posture_python() -> Path | None:
    """The interpreter of the posture environment, if installed."""
    for name in (".venv-posture/bin/python", ".venv-posture/Scripts/python.exe"):
        p = REPO_ROOT / name
        if p.exists():
            return p
    return None


def native_available() -> bool:
    if os.environ.get("MEDISENSE_POSTURE_NATIVE") == "0":  # tests; or to force the browser camera
        return False
    return posture_python() is not None


class PostureService:
    def __init__(self, tracker: PostureTracker, on_update: Callable[[dict], None] | None = None,
                 on_preview: Callable[[str], None] | None = None, fps: float = 4.0):
        self.tracker = tracker
        self.on_update = on_update
        self.on_preview = on_preview
        self.fps = fps
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.state = "off"  # off / starting / running / error / browser
        self.error: str | None = None
        self.source: str | None = None  # "native" or "browser"
        self.last_frame = -1e9
        self.last_lm = None
        self._want = False
        self._preview = False

    # -- control ------------------------------------------------------------

    async def start(self) -> None:
        if self._want:
            return
        self._want = True
        if not native_available():
            self.state, self.source = "browser", "browser"
            self.error = None
            return
        self.state, self.source, self.error = "starting", "native", None
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._want = False
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write(b"quit\n")
                await self.proc.stdin.drain()
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                self.proc.kill()
        if self.task:
            self.task.cancel()
        self.proc = None
        self.state = "off"
        self.source = None

    async def set_preview(self, on: bool) -> None:
        self._preview = on
        await self._send(f"preview {'on' if on else 'off'}")

    async def _send(self, line: str) -> None:
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            try:
                self.proc.stdin.write((line + "\n").encode())
                await self.proc.stdin.drain()
            except (ConnectionError, RuntimeError):
                pass

    # -- engine process -----------------------------------------------------

    async def _run(self) -> None:
        backoff = 2.0
        while self._want:
            self.state, self.source, self.error = "starting", "native", None
            try:
                model = await assets.ensure_pose_model()
            except Exception as e:
                self.state, self.error = "error", f"Could not download the posture model: {e}"
                await asyncio.sleep(30)
                continue
            py = posture_python()
            extra = []
            test_image = os.environ.get("MEDISENSE_POSTURE_TEST_IMAGE")  # no camera: tests, CI
            if test_image:
                extra = ["--image", test_image]
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    str(py), "-I", str(ENGINE), "--model", str(model), "--fps", str(self.fps), *extra,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=1 << 20,
                )
            except OSError as e:
                self.state, self.error = "error", f"Could not start the posture engine: {e}"
                await asyncio.sleep(30)
                continue
            if self._preview:
                await self._send("preview on")
            stderr_task = asyncio.create_task(self._drain(self.proc.stderr))
            fatal = await self._read(self.proc.stdout)
            await self.proc.wait()
            stderr_task.cancel()
            if not self._want:
                break
            if fatal:
                self.state, self.error = "error", fatal
                await asyncio.sleep(15)  # e.g. camera permission: retry slowly
            else:
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def _drain(self, stream) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return

    async def _read(self, stream) -> str | None:
        fatal = None
        while True:
            line = await stream.readline()
            if not line:
                return fatal
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "ready":
                self.state = "running"
            elif kind == "pose":
                self.feed(msg.get("lm"), msg.get("world"), msg.get("aspect", 4 / 3))
            elif kind == "nopose":
                self.feed(None)
            elif kind == "preview" and self.on_preview:
                self.on_preview(msg.get("jpg", ""))
            elif kind == "error":
                fatal = msg.get("message")
                log.warning("posture engine: %s", fatal)

    # -- frames (native engine or browser) ----------------------------------

    def feed(self, lm, world=None, aspect: float = 4 / 3, source: str | None = None) -> dict:
        if source == "browser" and self.source == "native" and self.state == "running":
            return self.tracker.current  # the native engine already watches
        if source:
            self.source = source
        self.last_frame = time.monotonic()
        self.last_lm = lm
        cur = self.tracker.update(lm, world, aspect)
        if self.on_update:
            self.on_update(cur)
        return cur

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "source": self.source,
            "error": self.error,
            "native": native_available(),
            "receiving": time.monotonic() - self.last_frame < 3,
            "settings": self.tracker.settings.__dict__,
            "calibrated": self.tracker.calibrated,
            "alerts": self.tracker.alerts,
            "platform": sys.platform,
        }
