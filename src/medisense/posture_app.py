"""MediSense Posture: a separate app for working at the computer. It watches
your back, shoulders and head (hands and legs stay free for the keyboard)
and reminds you with a macOS notification when you slump, hunch or lean in
towards the screen. (The Vairocana check lives in MediSense, for sessions.)

Runs in the background with the camera (native engine, works while the page
is closed or hidden), keeps a daily history in ~/MediSense/posture/, and
serves its own page at http://localhost:8766.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from aiohttp import WSMsgType, web

from . import notify
from .config import WEB_DIR, data_dir
from .posture import PostureTracker
from .posture_service import PostureService
from .webutil import dumps, guard, json_body

log = logging.getLogger("medisense.posture_app")

POSTURE_PORT = 8766
APP_KEY = web.AppKey("posture", object)
LOG_EVERY_S = 10.0


def posture_dir() -> Path:
    d = data_dir() / "posture"
    d.mkdir(parents=True, exist_ok=True)
    return d


class PostureApp:
    def __init__(self):
        self.clients: set[web.WebSocketResponse] = set()
        self.preview_clients: set[web.WebSocketResponse] = set()
        self.tracker = PostureTracker(store=data_dir() / "posture.json", notify=self._notify)
        self.tracker.settings.mode = "desk"  # office posture only
        # Posture changes slowly: 2 frames a second spares an old Mac's battery all day.
        self.service = PostureService(self.tracker, on_update=self._on_update, on_preview=self._on_preview, fps=2.0)
        self.paused = False
        self._acc: list[tuple[float | None, str | None]] = []
        self._next_log = time.monotonic() + LOG_EVERY_S
        self._last_push = 0.0
        self._last_lm = None
        self.started = time.time()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.service.start()
        self._task = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        self._flush_log(force=True)
        await self.service.stop()

    def _notify(self, title: str, message: str, sound: bool) -> None:
        if not self.paused:
            asyncio.get_running_loop().run_in_executor(None, notify.send, title, message, sound, "Tink")

    # -- data flow ----------------------------------------------------------

    def _on_update(self, cur: dict) -> None:
        worst = cur["checks"] and max((c for c in cur["checks"] if c["penalty"] > 0),
                                      key=lambda c: c["penalty"], default=None)
        self._acc.append((cur.get("score"), worst["key"] if worst else None))
        now = time.monotonic()
        if now - self._last_push >= 0.4:
            self._last_push = now
            lm = self.service.last_lm if self.service.source == "native" else None
            asyncio.get_running_loop().create_task(self.broadcast({"type": "posture", **cur, "lm": lm,
                                                                    "service": self.service.snapshot(),
                                                                    "paused": self.paused}))

    def _on_preview(self, jpg: str) -> None:
        for ws in list(self.preview_clients):
            asyncio.get_running_loop().create_task(self._send(ws, {"type": "preview", "jpg": jpg}))

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._flush_log()
            if time.monotonic() - self.service.last_frame > 3:
                self._on_update(self.tracker.update(None))

    def _flush_log(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_log:
            return
        self._next_log = now + LOG_EVERY_S
        acc, self._acc = self._acc, []
        if not acc:
            return
        scores = [s for s, _ in acc if s is not None]
        faults = [w for _, w in acc if w]
        row = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "score": round(sum(scores) / len(scores), 1) if scores else "",
            "visible": round(len(scores) / len(acc), 2),
            "worst": max(set(faults), key=faults.count) if faults else "",
            "alerts": self.tracker.alerts,
            "mode": self.tracker.settings.mode,
        }
        path = posture_dir() / f"{datetime.now():%Y-%m-%d}.csv"
        new = not path.exists()
        with open(path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)

    async def _send(self, ws, msg) -> None:
        try:
            await ws.send_str(dumps(msg))
        except Exception:
            self.clients.discard(ws)
            self.preview_clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.clients):
            await self._send(ws, msg)

    def state(self) -> dict:
        return {"posture": self.tracker.current, "service": self.service.snapshot(), "paused": self.paused}


def read_day(day: str) -> dict:
    path = posture_dir() / f"{day}.csv"
    out = {"day": day, "time": [], "score": [], "worst": [], "alerts": 0}
    if not path.exists() or "/" in day or ".." in day:
        return out
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out["time"].append(row["time"])
            out["score"].append(float(row["score"]) if row["score"] else None)
            out["worst"].append(row["worst"])
            out["alerts"] = max(out["alerts"], int(row.get("alerts") or 0))
    scores = [s for s in out["score"] if s is not None]
    out["minutes_tracked"] = round(len(scores) * LOG_EVERY_S / 60, 1)
    out["aligned_pct"] = round(100 * sum(1 for s in scores if s >= 80) / len(scores), 1) if scores else None
    out["average"] = round(sum(scores) / len(scores), 1) if scores else None
    faults = [w for w in out["worst"] if w]
    out["most_common_fault"] = max(set(faults), key=faults.count) if faults else None
    return out


def list_days() -> list[dict]:
    days = sorted((p.stem for p in posture_dir().glob("*.csv")), reverse=True)[:30]
    return [{k: v for k, v in read_day(d).items() if k not in ("time", "score", "worst")} for d in days]


# ------------------------------------------------------------------- server

def _app(request) -> PostureApp:
    return request.app[APP_KEY]


async def index(request):
    resp = web.FileResponse(WEB_DIR / "posture.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def ws_handler(request):
    app = _app(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app.clients.add(ws)
    await ws.send_str(dumps({"type": "posture", **app.state()["posture"], "service": app.service.snapshot(),
                             "paused": app.paused}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "pose":  # browser camera fallback
                app.service.feed(data.get("lm"), data.get("world"), float(data.get("aspect") or 4 / 3),
                                 source="browser")
            elif data.get("type") == "preview":
                on = bool(data.get("on"))
                (app.preview_clients.add if on else app.preview_clients.discard)(ws)
                await app.service.set_preview(bool(app.preview_clients))
    finally:
        app.clients.discard(ws)
        app.preview_clients.discard(ws)
        if not app.preview_clients:
            await app.service.set_preview(False)
    return ws


async def api_state(request):
    return web.json_response(_app(request).state(), dumps=dumps)


async def api_calibrate(request):
    _app(request).tracker.start_calibration(5.0)
    return web.json_response({"ok": True, "seconds": 5})


async def api_settings(request):
    d = await json_body(request)
    app = _app(request)
    if "paused" in d:
        app.paused = bool(d.pop("paused"))
    app.tracker.set_settings({**d, "mode": "desk"})
    return web.json_response(app.state(), dumps=dumps)


async def api_test(request):
    ok = await asyncio.get_running_loop().run_in_executor(
        None, notify.send, "MediSense Posture", "Test: this is how a posture reminder looks.", True, "Tink")
    return web.json_response({"ok": ok})


async def api_history(request):
    day = request.query.get("day") or datetime.now().strftime("%Y-%m-%d")
    return web.json_response(read_day(day))


async def api_days(request):
    return web.json_response(list_days())


def build(app: PostureApp) -> web.Application:
    w = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    w[APP_KEY] = app
    w.add_routes([
        web.get("/", index),
        web.get("/ws", ws_handler),
        web.get("/api/state", api_state),
        web.post("/api/calibrate", api_calibrate),
        web.post("/api/settings", api_settings),
        web.post("/api/test-notification", api_test),
        web.get("/api/history", api_history),
        web.get("/api/days", api_days),
        web.get("/vendor/mediapipe/{name:.+}", _vendor),
        web.static("/static", WEB_DIR),
    ])
    return w


async def _vendor(request):
    from .server import vendor

    return await vendor(request)
