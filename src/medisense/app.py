"""The running app: receives board data, analyses it, records sessions and
pushes everything live to the browser."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from typing import Any

from aiohttp import web

from . import __version__, insights, network, notify, sessions
from .config import UDP_PORT, DeviceRegistry, data_dir
from .device import DeviceManager
from .posture import AlertSettings, PostureTracker
from .posture_service import PostureService
from .protocol import CSI_MAGIC, packet_kind, parse_csi
from .recorder import SessionRecorder
from .vitals import VitalsEngine
from .webutil import dumps

log = logging.getLogger("medisense.app")

SLUMP_EVENT_AT = 0.15  # head 15 % lower than in your calibrated posture...
SLUMP_EVENT_HOLD_S = 15  # ...for this long: a "slump" event
POSTURE_PUSH_S = 0.4  # posture updates to the page at most this often
POSTURE_LOG_S = 0.5  # posture.csv rows at most this often
FAULT_MIN_PENALTY = 5.0  # posture points that count as a fault in the records
# During meditation: a gentler reminder than the all-day posture app's.
SESSION_ALERTS = AlertSettings(threshold=80.0, hold_s=30.0, cooldown_s=300.0, sound=True, mode="meditation")
LIVE_S = 2.5  # data within this long: live
PAUSED_S = 10.0  # up to this long: "signal paused", not a disconnection
PACER_HZ = 50
PACER_PORT = 9  # discard port: the board doesn't listen, it only needs to receive frames

# What the board's log says about an unexpected interruption (diagnostics).
BOARD_EVENTS = [
    (re.compile(r"Brownout detector was triggered|rst:0xf \(BROWNOUT"), "Power dip: the board restarted (try another USB cable or port)."),
    (re.compile(r"Guru Meditation Error|abort\(\) was called|Task watchdog got triggered"), "The board's firmware crashed and restarted."),
    (re.compile(r"rst:0x[0-9a-f]+ \(([A-Z_]+)"), "Board restarted ({0})."),
    (re.compile(r"WiFi disconnected, reason=(\d+)"), "Board lost the WiFi connection (reason {0})."),
    (re.compile(r"Got IP: (\S+)"), "Board (re)joined the WiFi at {0}."),
]


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
        self.registry = DeviceRegistry()
        self.clients: set[web.WebSocketResponse] = set()
        self.session: SessionRecorder | None = None
        self.last_packet = -1e9
        self.packet_source: str | None = None
        self.metrics: dict = {}
        self.posture: dict | None = None
        self.posture_at = -1e9
        self._slump_since: float | None = None
        self._slump_open = False
        self._posture_push = 0.0
        self._posture_log = 0.0
        self.preview_clients: set[web.WebSocketResponse] = set()
        self.posture_tracker = PostureTracker(store=data_dir() / "posture.json", notify=self._posture_notify,
                                              settings=AlertSettings(**SESSION_ALERTS.__dict__),
                                              settings_key="session_settings")
        self.posture_tracker.settings.mode = "meditation"
        self.posture_service = PostureService(self.posture_tracker, on_update=self._on_posture,
                                              on_preview=self._on_preview)
        self.camera_on = False
        self.practice = insights.PracticeState()
        self.device = DeviceManager(self.registry, self._status_changed, lambda: self.last_packet)
        self.device.line_listeners.append(self._on_board_line)
        self.board_events: list[dict] = []
        self._pacer_enabled = os.environ.get("MEDISENSE_PACER", "1") != "0"
        self.pacer_sent = 0
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
            if self._pacer_enabled:
                self._tasks.append(asyncio.create_task(self._pacer()))
        self._tasks.append(asyncio.create_task(self._fast_loop()))
        self._tasks.append(asyncio.create_task(self._slow_loop()))

    async def stop(self) -> None:
        self.demo_stop.set()
        if self.session:
            self.stop_session({})
        for t in self._tasks:
            t.cancel()
        await self.posture_service.stop()
        await self.device.stop()
        if self._transport:
            self._transport.close()

    # ---------------------------------------------------------------- data

    def on_packet(self, data: bytes, addr) -> None:
        now = time.monotonic()
        kind = packet_kind(data)
        if kind != CSI_MAGIC:
            if self.session and kind is not None and (kind >> 16) == 0xC511:
                self.session.raw(time.time(), data)  # sensor packets: keep for later analysis
            self.on_other_packet(kind, data, now)
            return
        frame = parse_csi(data)
        if frame is None:
            return
        if now - self.last_packet > LIVE_S:
            self._status_dirty = True
        self.last_packet = now
        self.packet_source = addr[0] if addr else None
        self.engine.add_frame(now, frame)
        if self.session:
            self.session.raw(time.time(), data)

    def on_other_packet(self, kind: int | None, data: bytes, now: float) -> None:
        """Sensor packets (radar, pulse sensor): handled by MediSense Pro."""

    async def _pacer(self) -> None:
        """Send tiny packets to the board so the router transmits frames to it.

        Every frame the router delivers to the board is a CSI sample. The
        board pings the router itself, but routers treat pings differently;
        this keeps a steady supply that doesn't depend on that.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        period = 1.0 / PACER_HZ
        nxt = time.monotonic()
        try:
            while True:
                nxt += period
                await asyncio.sleep(max(0.0, nxt - time.monotonic()))
                if nxt < time.monotonic() - 1.0:
                    nxt = time.monotonic()
                target = self.packet_source or self.device.log.board_ip
                if not target or time.monotonic() - self.last_packet > PAUSED_S:
                    continue
                try:
                    sock.sendto(b"MS", (target, PACER_PORT))
                    self.pacer_sent += 1
                except OSError:
                    pass
        finally:
            sock.close()

    def _on_board_line(self, line: str) -> None:
        """Board log line (USB): keep it with the session, note interruptions."""
        now = time.monotonic()
        if self.session:
            self.session.board_line(now, line)
        for rx, text in BOARD_EVENTS:
            m = rx.search(line)
            if m:
                msg = text.format(*m.groups())
                ev = {"t": round(now - self.started, 1), "text": msg}
                self.board_events.append(ev)
                del self.board_events[:-30]
                if self.session:
                    asyncio.get_running_loop().create_task(self._emit_event(now, "board", text=msg))
                break

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
        rel = self._rel(now)
        d["state"] = self.practice.step(rel, d)
        d.update({k: v for k, v in self.practice.live(rel).items() if k != "state"})
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

    def _rel(self, t_mono: float) -> float:
        """Time on the practice clock: session time while recording, else app time."""
        return self.session.rel(t_mono) if self.session else round(t_mono - self.started, 3)

    # -------------------------------------------------------------- posture

    async def set_camera(self, on: bool) -> dict:
        """Posture camera on/off (the background engine, or the page's camera as a fallback)."""
        self.camera_on = bool(on)
        if self.camera_on:
            await self.posture_service.start()
        else:
            await self.posture_service.stop()
            self.posture = None
            self.posture_tracker.update(None)
        await self._push_posture(force=True)
        return self.posture_state()

    def posture_state(self) -> dict:
        return {**self.posture_tracker.current, "camera_on": self.camera_on,
                "service": self.posture_service.snapshot()}

    def _posture_notify(self, title: str, message: str, sound: bool) -> None:
        if not self.session:  # reminders only while you meditate (the Posture app covers the rest of the day)
            return
        asyncio.get_running_loop().run_in_executor(None, notify.send, f"MediSense · {title}", message, sound, "Tink")

    def _on_preview(self, jpg: str) -> None:
        for ws in list(self.preview_clients):
            asyncio.get_running_loop().create_task(self._send_one(ws, {"type": "preview", "jpg": jpg}))

    def _on_posture(self, cur: dict) -> None:
        now = time.monotonic()
        if cur.get("score") is not None:
            self.posture = cur
            self.posture_at = now
        for t, kind, data in self.posture_tracker.drain_events():
            self.practice.note_event(self._rel(t), kind)
            if self.session:
                asyncio.get_running_loop().create_task(self._emit_event(t, kind, **data))
        checks = {c["key"]: c for c in cur.get("checks") or []}
        if self.session and cur.get("score") is not None:
            if now - self._posture_log >= POSTURE_LOG_S:
                self._posture_log = now
                row = {"score": cur["score"], "state": cur.get("state"), "faults": self._faults(cur), "visible": 1}
                for k, c in checks.items():
                    if c.get("value") is not None:
                        row[k] = c["value"]
                self.session.posture(now, row)
            drop = (checks.get("head_drop") or {}).get("value")
            if drop is not None and drop >= SLUMP_EVENT_AT:
                if self._slump_since is None:
                    self._slump_since = now
                elif not self._slump_open and now - self._slump_since >= SLUMP_EVENT_HOLD_S:
                    self._slump_open = True
                    asyncio.get_running_loop().create_task(self._emit_event(self._slump_since, "slump", value=round(drop, 2)))
            elif drop is None or drop < SLUMP_EVENT_AT * 0.6:
                self._slump_since = None
                self._slump_open = False
        asyncio.get_running_loop().create_task(self._push_posture())

    async def _push_posture(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._posture_push < POSTURE_PUSH_S:
            return
        self._posture_push = now
        lm = self.posture_service.last_lm if self.posture_service.source == "native" else None
        await self.broadcast({"type": "posture", **self.posture_state(), "lm": lm})

    @staticmethod
    def _faults(cur: dict) -> str:
        bad = sorted((c for c in cur.get("checks") or [] if c.get("penalty", 0) >= FAULT_MIN_PENALTY),
                     key=lambda c: -c["penalty"])
        return ";".join(c["key"] for c in bad)

    def _posture_fields(self, now: float) -> dict:
        if not self.posture or now - self.posture_at > 3:
            return {}
        p = self.posture
        checks = {c["key"]: c.get("value") for c in p.get("checks") or []}
        return {
            "posture_score": p.get("score"),
            "posture_faults": self._faults(p),
            "slump": checks.get("head_drop"),
            "head_tilt": checks.get("head_level"),
            "shoulder_tilt": checks.get("shoulders_level"),
            "lean": checks.get("lean"),
        }

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
        self.practice = insights.PracticeState()  # the session's own clock
        self._status_dirty = True
        return {"id": self.session.id}

    def stop_session(self, fields: dict) -> dict:
        if not self.session:
            raise RuntimeError("No session is running.")
        s = self.session
        self.session = None
        s.close()
        self.practice = insights.PracticeState()
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
        if kind == "polar_hr":
            for k in ("heart_rate", "heart_rate_experimental", "sensor_hr"):
                if self.metrics.get(k):
                    data[f"app_{k}"] = self.metrics[k]
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
        live = now - self.last_packet < LIVE_S
        paused = not live and now - self.last_packet < PAUSED_S
        dev = {"state": "demo", "message": "Demo mode: a simulated meditator, no board needed."} if self.demo else self.device.snapshot()
        return {
            "version": __version__,
            "demo": self.demo,
            "live": live,
            "paused": paused,
            "packet_source": self.packet_source,
            "pacer": self._pacer_enabled and not self.demo,
            "board_events": self.board_events[-8:],
            "camera_on": self.camera_on,
            "seconds_since_data": None if self.last_packet < 0 else round(now - self.last_packet, 1),
            "mac_ip": network.local_ip() if not self.demo else "127.0.0.1",
            "device": dev,
            "session": None if not self.session else {
                "id": self.session.id,
                "elapsed": round(now - self.session.t0_mono, 1),
                **{k: self.session.meta.get(k) for k in ("practice", "intention", "planned_min")},
            },
        }

    async def _send_one(self, ws, msg: dict) -> None:
        try:
            await ws.send_str(dumps(msg))
        except Exception:
            self.clients.discard(ws)
            self.preview_clients.discard(ws)

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
