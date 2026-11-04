"""Shared web helpers for the local apps (MediSense, MediSense Posture)."""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from . import __version__

# The page code is served under a path that changes with every version, so a
# browser can never mix a cached old script with a new page after an update
# (that left the pages stuck at "Starting…"). /static stays for old links.
ASSET_PREFIX = f"/v/{__version__}"


def page(path: Path) -> web.Response:
    """An HTML page whose script and style links point at this version's files."""
    html = path.read_text(encoding="utf-8").replace('"/static/', f'"{ASSET_PREFIX}/')
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def no_store_assets(request: web.Request, response: web.StreamResponse) -> None:
    """on_response_prepare: always revalidate page code (cheap on localhost)."""
    if request.path.startswith(("/v/", "/static/")):
        response.headers["Cache-Control"] = "no-cache"


def add_assets(app: web.Application, web_dir: Path) -> None:
    app.router.add_static(ASSET_PREFIX, web_dir)
    app.router.add_static("/static", web_dir)
    app.on_response_prepare.append(no_store_assets)


def _jsonable(o):
    """json.dumps fallback for numpy scalars and similar."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=_jsonable)


@web.middleware
async def guard(request: web.Request, handler):
    """Localhost only; state changes need the X-MediSense header.

    A page on another site can't add that header without a CORS preflight,
    which these servers never approve (CSRF), and the Host check stops
    DNS-rebinding tricks.
    """
    host = (request.host or "").split(":")[0]
    if host not in ("localhost", "127.0.0.1", "[::1]", "::1"):
        return web.Response(status=403, text="MediSense only answers on localhost.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-MediSense") != "1":
        return web.Response(status=403, text="missing X-MediSense header")
    try:
        return await handler(request)
    except (ValueError, RuntimeError) as e:
        return web.json_response({"error": str(e)}, status=400)


async def json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("bad JSON") from None
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    return data
