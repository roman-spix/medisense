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

from . import assets, sessions
from .app import MediSenseApp, dumps
from .config import WEB_DIR, load_wifi_password

log = logging.getLogger("medisense.server")

APP_KEY = web.AppKey("medisense", MediSenseApp)


@web.middleware
async def guard(request: web.Request, handler):
    host = (request.host or "").split(":")[0]
    if host not in ("localhost", "127.0.0.1", "[::1]", "::1"):
        return web.Response(status=403, text="MediSense only answers on localhost.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-MediSense") != "1":
        return web.Response(status=403, text="missing X-MediSense header")
    try:
        return await handler(request)
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)


def _app(request: web.Request) -> MediSenseApp:
    return request.app[APP_KEY]


async def _json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("bad JSON") from None
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    return data


async def index(request: web.Request) -> web.StreamResponse:
    resp = web.FileResponse(WEB_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = _app(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    app.clients.add(ws)
    await ws.send_str(dumps({"type": "status", **app.status()}))
    if app.metrics:
        await ws.send_str(dumps({"type": "metrics", **app.metrics}))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("type") == "posture":
                app.on_posture(data)
    finally:
        app.clients.discard(ws)
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
    return web.json_response(_app(request).start_session(d))


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
        web.get("/api/sessions", api_sessions),
        web.get("/api/sessions/{sid}", api_session),
        web.get("/api/sessions/{sid}/wave", api_session_wave),
        web.post("/api/sessions/{sid}/meta", api_session_meta),
        web.get("/api/sessions/{sid}/export.csv", api_session_export),
        web.post("/api/sessions/{sid}/open", api_session_open),
        web.delete("/api/sessions/{sid}", api_session_delete),
        web.get("/vendor/mediapipe/{name:.+}", vendor),
        web.static("/static", WEB_DIR),
    ])
    return w
