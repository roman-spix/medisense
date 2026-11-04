"""Shared web helpers for the local apps (MediSense, MediSense Posture)."""

from __future__ import annotations

import json

from aiohttp import web


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
