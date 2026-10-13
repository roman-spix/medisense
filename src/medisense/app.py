"""The running app: receives board data, analyses it, records sessions and
pushes everything live to the browser."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from typing import Any

from aiohttp import web

from . import __version__, network, sessions
from .config import UDP_PORT, DeviceRegistry, Settings
from .device import DeviceManager
from .protocol import CSI_MAGIC, packet_kind, parse_csi
from .recorder import SessionRecorder
from .vitals import VitalsEngine

log = logging.getLogger("medisense.app")

POSTURE_KEYS = ("score", "slump", "head_drop", "head_tilt", "shoulder_tilt", "lean", "visible", "camera_motion")
SLUMP_EVENT_AT = 0.2  # 20 % head drop vs your upright baseline
SLUMP_EVENT_HOLD_S = 15


def _jsonable(o):
    """json.dumps fallback for numpy scalars and similar."""
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=_jsonable)


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, app: "MediSenseApp"):
        self.app = app

    def datagram_received(self, data: bytes, addr) -> None:
        self.app.on_packet(data, addr)


class MediSenseApp:
    def __init__(self, demo: bool = False, udp_port: int = UDP_PORT):
        self.demo = demo
        self.udp_port = udp_port
        self.engine = VitalsEngine()
        self.settings = Settings()
        self.registry = DeviceRegistry()
        self.clients: set[web.WebSocketResponse] = set()
        self.session: SessionRecorder | None = None
        self.last_packet = -1e9
        self.packet_source: str | None = None
        self.packets = 0
        self.metrics: dict = {}
        self.posture: dict | None = None
        self.posture_at = -1e9
        self._slump_since: float | None = None
        self._slump_open = False
        self.device = DeviceManager(self.registry, self._status_changed, lambda: self.last_packet)
        self._tasks: list[asyncio.Task] = []
        self._transport = None
        self._status_dirty = True
        self.started = time.monotonic()
        self.demo_stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Udp(self), local_addr=("0.0.0.0", self.udp_port), family=socket.AF_INET
        )
        if self.demo:
            from .simulator import Scenario, run_udp

            self._tasks.append(asyncio.create_task(run_udp("127.0.0.1", self.udp_port, Scenario(), self.demo_stop)))
        else:
            self.device.start()
        self._tasks.append(asyncio.create_task(self._fast_loop()))
        self._tasks.append(asyncio.create_task(self._slow_loop()))

    async def stop(self) -> None:
        self.demo_stop.set()
        if self.session:
            self.stop_session({})
        for t in self._tasks:
            t.cancel()
        await self.device.stop()
        if self._transport:
            self._transport.close()

    # ---------------------------------------------------------------- data

    def on_packet(self, data: bytes, addr) -> None:
        now = time.monotonic()
        if packet_kind(data) != CSI_MAGIC:
            return
        frame = parse_csi(data)
        if frame is None:
            return
        self.packets += 1
        if now - self.last_packet > 3:
            self._status_dirty = True
        self.last_packet = now
        self.packet_source = addr[0] if addr else None
        self.engine.add_frame(now, frame)
        if self.session:
            self.session.raw(time.time(), data)

    async def _fast_loop(self) -> None:
        """10x per second: live waveform to the browser and the recording."""
        while True:
            await asyncio.sleep(0.1)
            try:
                await self._fast_tick()
            except Exception:
                log.exception("fast loop")

    async def _fast_tick(self) -> None:
        wave = self.engine.drain_wave()
        if not wave:
            return
        if self.session:
            self.session.wave(wave)
        # Times relative to app start (not the session) so the scrolling
        # chart never jumps backwards when a session starts or ends.
        await self.broadcast({
            "type": "wave",
            "samples": [[round(t - self.started, 3), None if b is None else round(b, 4), round(m, 2)] for t, b, m in wave],
        })

    async def _slow_loop(self) -> None:
        """Once per second: vital signs, events, status."""
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            tick += 1
            try:
                await self._slow_tick(tick)
            except Exception:
                log.exception("slow loop")

    async def _slow_tick(self, tick: int) -> None:
        now = time.monotonic()
        try:
            # In the event-loop thread on purpose: packets mutate the same
            # buffers, and a few tens of ms per second is fine.
            m = self.engine.compute(now)
        except Exception:
            log.exception("analysis failed")
            return
        d = m.as_dict()
        d.update(self._posture_fields(now))
        if self.session:
            self.session.metrics(now, d)
            d["session_t"] = self.session.rel(now)
        self.metrics = d
        await self.broadcast({"type": "metrics", **d})
        for ev in self.engine.drain_events():
            await self._emit_event(ev.t_start, "shift", peak=round(ev.peak, 1),
                                   duration=round((ev.t_end or ev.t_start) - ev.t_start, 1))
        if self._status_dirty or tick % 2 == 0:
            self._status_dirty = False
            await self.broadcast({"type": "status", **self.status()})

    def _posture_fields(self, now: float) -> dict:
        if not self.posture or now - self.posture_at > 3:
            return {}
        p = self.posture
        return {
            "posture_score": p.get("score"),
            "slump": p.get("slump"),
            "head_tilt": p.get("head_tilt"),
            "shoulder_tilt": p.get("shoulder_tilt"),
            "lean": p.get("lean"),
        }

    def on_posture(self, p: dict) -> None:
        now = time.monotonic()
        clean = {}
        for k in POSTURE_KEYS:
            v = p.get(k)
            if isinstance(v, bool):
                clean[k] = int(v)
            elif isinstance(v, (int, float)):
                clean[k] = round(float(v), 4)
        self.posture = clean
        self.posture_at = now
        if self.session:
            self.session.posture(now, clean)
            slump = clean.get("slump")
            if slump is not None and slump >= SLUMP_EVENT_AT:
                if self._slump_since is None:
                    self._slump_since = now
                elif not self._slump_open and now - self._slump_since >= SLUMP_EVENT_HOLD_S:
                    self._slump_open = True
                    asyncio.create_task(self._emit_event(self._slump_since, "slump", value=round(slump, 2)))
            elif slump is not None and slump < SLUMP_EVENT_AT * 0.6:
                self._slump_since = None
                self._slump_open = False

    async def _emit_event(self, t_mono: float, kind: str, **data) -> dict:
        if self.session:
            ev = self.session.event(t_mono, kind, **data)
        else:
            ev = {"t": round(t_mono - self.started, 3), "kind": kind, **data}
        await self.broadcast({"type": "event", **ev})
        return ev

    # ------------------------------------------------------------- sessions

    def start_session(self, meta: dict) -> dict:
        if self.session:
            raise RuntimeError("A session is already running.")
        dev = self.device.snapshot()
        allowed = {k: meta.get(k) for k in ("practice", "intention", "planned_min", "posture_camera")}
        self.session = SessionRecorder({
            **allowed,
            "demo": self.demo,
            "board": {"mac": dev.get("mac"), "chip": dev.get("chip"), "firmware": dev.get("board_firmware")},
        })
        self._slump_since = None
        self._slump_open = False
        self._status_dirty = True
        return {"id": self.session.id}

    def stop_session(self, fields: dict) -> dict:
        if not self.session:
            raise RuntimeError("No session is running.")
        s = self.session
        self.session = None
        s.close()
        summary = sessions.summarize(s.path)
        meta = sessions.update_meta(s.id, {"summary": summary})
        self._status_dirty = True
        return meta or {}

    async def add_event(self, kind: str, value: Any = None, text: str | None = None) -> dict:
        if kind not in ("mark", "polar_hr", "breath_count", "note"):
            raise ValueError("unknown event kind")
        data: dict = {}
        if value is not None:
            data["value"] = float(value)
        if text:
            data["text"] = str(text)[:1000]
        if kind == "breath_count" and self.metrics.get("breath_rate"):
            data["app_breath_rate"] = self.metrics["breath_rate"]
        if kind == "polar_hr" and self.metrics.get("heart_rate"):
            data["app_heart_rate"] = self.metrics["heart_rate"]
        return await self._emit_event(time.monotonic(), kind, **data)

    # --------------------------------------------------------------- status

    def _status_changed(self) -> None:
        self._status_dirty = True
        try:
            asyncio.get_running_loop().create_task(self.broadcast({"type": "status", **self.status()}))
        except RuntimeError:
            pass

    def status(self) -> dict:
        now = time.monotonic()
        live = now - self.last_packet < 3
        dev = {"state": "demo", "message": "Demo mode: a simulated meditator, no board needed."} if self.demo else self.device.snapshot()
        return {
            "version": __version__,
            "demo": self.demo,
            "live": live,
            "packet_source": self.packet_source,
            "seconds_since_data": None if self.last_packet < 0 else round(now - self.last_packet, 1),
            "mac_ip": network.local_ip() if not self.demo else "127.0.0.1",
            "device": dev,
            "session": None if not self.session else {
                "id": self.session.id,
                "elapsed": round(now - self.session.t0_mono, 1),
                **{k: self.session.meta.get(k) for k in ("practice", "intention", "planned_min")},
            },
        }

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = dumps(msg)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)
