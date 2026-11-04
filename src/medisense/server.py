"""Local web server: the dashboard page, a live WebSocket and a small JSON API.

Listens on 127.0.0.1 only. State-changing requests must carry the
`X-MediSense: 1` header, which a web page on another site cannot add without
a CORS preflight that this server never approves, and the Host header must
be localhost (protects against DNS-rebinding tricks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys

from aiohttp import WSMsgType, web

from . import assets, insights, sessions
from .app import MediSenseApp
from .webutil import add_assets, dumps, guard, json_body as _json, page
from .config import WEB_DIR, load_wifi_password

log = logging.getLogger("medisense.server")

APP_KEY = web.AppKey("medisense", MediSenseApp)


def _app(request: web.Request) -> MediSenseApp:
    return request.app[APP_KEY]


async def index(request: web.Request) -> web.StreamResponse:
    return page(WEB_DIR / "index.html")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = _app(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app.clients.add(ws)
    await ws.send_str(dumps({"type": "status", **app.status()}))
    if app.metrics:
        await ws.send_str(dumps({"type": "metrics", **app.metrics}))
    await ws.send_str(dumps({"type": "posture", **app.posture_state(), "lm": None}))
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
            if data.get("type") == "pose" and app.camera_on:
                # the page's own camera (when the background engine isn't installed)
                try:
                    aspect = float(data.get("aspect") or 4 / 3)
                except (TypeError, ValueError):
                    aspect = 4 / 3
                app.posture_service.feed(data.get("lm"), data.get("world"), aspect, source="browser")
            elif data.get("type") == "preview":
                (app.preview_clients.add if data.get("on") else app.preview_clients.discard)(ws)
                await app.posture_service.set_preview(bool(app.preview_clients))
    finally:
        app.clients.discard(ws)
        if ws in app.preview_clients:
            app.preview_clients.discard(ws)
            await app.posture_service.set_preview(bool(app.preview_clients))
    return ws


async def api_status(request: web.Request) -> web.Response:
    return web.json_response(_app(request).status(), dumps=dumps)


async def api_setup(request: web.Request) -> web.Response:
    app = _app(request)
    d = await _json(request)
    ssid = str(d.get("ssid") or "").strip()
    password = str(d.get("password") or "")
    if d.get("use_saved"):
        saved = await asyncio.to_thread(load_wifi_password, ssid)
        if saved is None:
            raise ValueError("No saved password for that network. Please type it.")
        password = saved
    await app.device.setup(ssid, password, remember=bool(d.get("remember", True)))
    return web.json_response({"ok": True})


async def api_reset(request: web.Request) -> web.Response:
    await _app(request).device.reset_board()
    return web.json_response({"ok": True})


async def api_session_start(request: web.Request) -> web.Response:
    d = await _json(request)
    app = _app(request)
    out = app.start_session(d)
    if d.get("posture_camera") and not app.camera_on:
        await app.set_camera(True)
    return web.json_response(out)


async def api_session_stop(request: web.Request) -> web.Response:
    d = await _json(request)
    return web.json_response(_app(request).stop_session(d))


async def api_session_event(request: web.Request) -> web.Response:
    d = await _json(request)
    ev = await _app(request).add_event(str(d.get("kind")), d.get("value"), d.get("text"))
    return web.json_response(ev)


async def api_sessions(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(sessions.list_sessions))


async def api_session(request: web.Request) -> web.Response:
    s = await asyncio.to_thread(sessions.load_session, request.match_info["sid"])
    if s is None:
        raise web.HTTPNotFound()
    return web.json_response(s)


async def api_session_wave(request: web.Request) -> web.Response:
    p = sessions.session_path(request.match_info["sid"])
    if p is None:
        raise web.HTTPNotFound()
    return web.json_response(await asyncio.to_thread(sessions.load_wave, p))


async def api_session_meta(request: web.Request) -> web.Response:
    d = await _json(request)
    allowed = {k: d[k] for k in ("notes", "rating", "mood", "polar_avg_hr", "intention", "practice") if k in d}
    meta = sessions.update_meta(request.match_info["sid"], allowed)
    if meta is None:
        raise web.HTTPNotFound()
    return web.json_response(meta)


async def api_session_export(request: web.Request) -> web.StreamResponse:
    p = sessions.session_path(request.match_info["sid"])
    if p is None:
        raise web.HTTPNotFound()
    resp = web.FileResponse(p / "metrics.csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="medisense-{p.name}.csv"'
    return resp


async def api_session_open(request: web.Request) -> web.Response:
    p = sessions.session_path(request.match_info["sid"])
    if p is None:
        raise web.HTTPNotFound()
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return web.json_response({"ok": True, "path": str(p)})


async def api_posture(request: web.Request) -> web.Response:
    return web.json_response(_app(request).posture_state(), dumps=dumps)


async def api_posture_camera(request: web.Request) -> web.Response:
    d = await _json(request)
    return web.json_response(await _app(request).set_camera(bool(d.get("on"))), dumps=dumps)


async def api_posture_calibrate(request: web.Request) -> web.Response:
    app = _app(request)
    if not app.camera_on:
        raise ValueError("Turn the camera on first.")
    app.posture_tracker.start_calibration(5.0)
    return web.json_response({"ok": True, "seconds": 5})


async def api_posture_settings(request: web.Request) -> web.Response:
    d = await _json(request)
    app = _app(request)
    allowed = {k: d[k] for k in ("enabled", "threshold", "hold_s", "cooldown_s", "sound") if k in d}
    app.posture_tracker.set_settings({**allowed, "mode": "meditation"})
    return web.json_response(app.posture_state(), dumps=dumps)


async def api_profile(request: web.Request) -> web.Response:
    metas = await asyncio.to_thread(sessions.list_sessions)
    return web.json_response(insights.profile(metas), dumps=dumps)


async def api_session_delete(request: web.Request) -> web.Response:
    app = _app(request)
    sid = request.match_info["sid"]
    if app.session and app.session.id == sid:
        raise ValueError("Stop the session first.")
    if not sessions.trash_session(sid):
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def vendor(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if assets.resolve(name) is None:
        raise web.HTTPNotFound()
    try:
        await assets.ensure()
    except Exception as e:
        log.warning("posture model download failed: %s", e)
        return web.Response(status=503, text=f"Could not download the posture model: {e}")
    path = assets.resolve(name)
    resp = web.FileResponse(path)
    if name.endswith(".wasm"):
        resp.content_type = "application/wasm"
    elif name.endswith((".js", ".mjs")):
        resp.content_type = "text/javascript"
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


def build(app: MediSenseApp) -> web.Application:
    w = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    w[APP_KEY] = app
    w.add_routes([
        web.get("/", index),
        web.get("/ws", ws_handler),
        web.get("/api/status", api_status),
        web.post("/api/device/setup", api_setup),
        web.post("/api/device/reset", api_reset),
        web.post("/api/session/start", api_session_start),
        web.post("/api/session/stop", api_session_stop),
        web.post("/api/session/event", api_session_event),
        web.get("/api/posture", api_posture),
        web.post("/api/posture/camera", api_posture_camera),
        web.post("/api/posture/calibrate", api_posture_calibrate),
        web.post("/api/posture/settings", api_posture_settings),
        web.get("/api/profile", api_profile),
        web.get("/api/sessions", api_sessions),
        web.get("/api/sessions/{sid}", api_session),
        web.get("/api/sessions/{sid}/wave", api_session_wave),
        web.post("/api/sessions/{sid}/meta", api_session_meta),
        web.get("/api/sessions/{sid}/export.csv", api_session_export),
        web.post("/api/sessions/{sid}/open", api_session_open),
        web.delete("/api/sessions/{sid}", api_session_delete),
        web.get("/vendor/mediapipe/{name:.+}", vendor),
    ])
    add_assets(w, WEB_DIR)
    return w
