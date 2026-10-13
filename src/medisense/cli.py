"""Command line: `medisense start`, `medisense demo`, `medisense doctor`, ..."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import signal
import socket
import sys
import time
import urllib.request
import webbrowser

from . import __version__
from .config import UDP_PORT, WEB_PORT, data_dir


def _port_free(port: int, kind=socket.SOCK_STREAM, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, kind)
    try:
        if kind == socket.SOCK_STREAM:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
            return b'"version"' in r.read()
    except Exception:
        return False


def _fmt(v, nd=1, unit=""):
    return "--" if v is None else f"{v:.{nd}f}{unit}"


class TerminalView:
    """One live status line in the Terminal window that launched the app."""

    def __init__(self, app):
        self.app = app

    async def run(self) -> None:
        tty = sys.stdout.isatty()
        last_print = 0.0
        while True:
            await asyncio.sleep(1.0)
            st = self.app.status()
            m = self.app.metrics or {}
            if st["live"]:
                state = "LIVE"
            elif st["demo"]:
                state = "DEMO"
            else:
                state = (st["device"] or {}).get("state", "?").upper()
            br = m.get("breath_rate")
            hr = m.get("heart_rate")
            parts = [
                f"[{state}]",
                f"breath {_fmt(br)}/min",
                f"heart ~{_fmt(hr, 0)}",
                f"{m.get('motion_state', '-')}",
            ]
            if m.get("posture_score") is not None:
                parts.append(f"posture {m['posture_score']:.0f}")
            if st["session"]:
                e = int(st["session"]["elapsed"])
                parts.append(f"REC {e // 60:02d}:{e % 60:02d}")
            parts.append(f"{_fmt(m.get('frames_per_s'), 0)} fps")
            line = "  ".join(parts)
            if tty:
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
            elif time.monotonic() - last_print > 30:
                print(line, flush=True)
                last_print = time.monotonic()


async def _serve(args) -> int:
    from aiohttp import web

    from .app import MediSenseApp
    from .server import build

    app = MediSenseApp(demo=args.demo, udp_port=args.udp_port)
    await app.start()
    runner = web.AppRunner(build(app), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    url = f"http://localhost:{args.port}/"
    print(f"MediSense {__version__} is running: {url}")
    print(f"Recordings: {data_dir() / 'sessions'}")
    print("Keep this window open while you use MediSense. Press Ctrl+C (or close it) to quit.\n")
    if not args.no_browser:
        webbrowser.open(url)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    view = None if args.quiet else asyncio.create_task(TerminalView(app).run())
    await stop.wait()
    print("\nStopping…")
    if view:
        view.cancel()
    await app.stop()
    await runner.cleanup()
    return 0


def cmd_start(args) -> int:
    if _already_running(args.port):
        print(f"MediSense is already running. Opening http://localhost:{args.port}/")
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{args.port}/")
        return 0
    if not _port_free(args.port):
        print(f"Port {args.port} is used by another program. Try: medisense start --port 8766")
        return 1
    if not _port_free(args.udp_port, socket.SOCK_DGRAM, "0.0.0.0"):
        print(f"UDP port {args.udp_port} (board data) is used by another program. Close it and try again.")
        return 1
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        filename=str(data_dir() / "medisense.log"),
    )
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 0


def cmd_doctor(args) -> int:
    """Check everything that can be checked without the board."""
    from . import firmware, network

    ok = True

    def line(good: bool | None, text: str) -> None:
        nonlocal ok
        mark = {True: "OK  ", False: "FAIL", None: "NOTE"}[good]
        if good is False:
            ok = False
        print(f"[{mark}] {text}")

    line(True, f"MediSense {__version__}, Python {platform.python_version()} ({platform.machine()})")
    if sys.platform == "darwin":
        line(True, f"macOS {platform.mac_ver()[0]}")
    for mod in ("numpy", "aiohttp", "serial", "esptool", "esp_idf_nvs_partition_gen"):
        try:
            __import__(mod)
            line(True, f"library {mod}")
        except Exception as e:
            line(False, f"library {mod}: {e}")
    for v in ("esp32s3-8mb", "esp32s3-4mb"):
        try:
            img = firmware.load_image(v)
            firmware.verify_image(img)
            line(True, f"firmware {v} (RuView {img.version}) checksums match")
        except Exception as e:
            line(False, f"firmware {v}: {e}")
    try:
        firmware.build_nvs(firmware.BoardSettings("test", "password1", "192.168.1.2"))
        line(True, "can build board settings")
    except Exception as e:
        line(False, f"cannot build board settings: {e}")
    ip = network.local_ip()
    line(bool(ip), f"this computer's network address: {ip or 'none (not connected?)'}")
    ssid = network.current_ssid()
    line(None if not ssid else True, f"WiFi network: {ssid or 'unknown'}")
    if ssid:
        b = network.router_bssid_24ghz(ssid)
        line(None if not b else True, f"router 2.4 GHz address: {b or 'not found (the board will listen to all traffic)'}")
    fw = network.firewall_blocks_incoming()
    if fw:
        line(False, "macOS firewall blocks ALL incoming connections; board data can't arrive. Turn that option off.")
    elif network.firewall_enabled():
        line(None, "macOS firewall is on: if macOS asks whether Python may accept incoming connections, click Allow.")
    line(_port_free(UDP_PORT, socket.SOCK_DGRAM, "0.0.0.0") or _already_running(WEB_PORT),
         f"UDP port {UDP_PORT} for board data is available")
    from .device import find_ports

    ports = find_ports()
    if ports:
        for p in ports:
            line(True, f"USB board candidate: {p.device} ({p.label()})")
    else:
        line(None, "no board plugged in right now")
    from . import assets

    line(None if not assets.ready() else True,
         "webcam posture model " + ("downloaded" if assets.ready() else "not downloaded yet (happens on first camera use)"))
    print("\nAll good." if ok else "\nSome checks failed; see above.")
    return 0 if ok else 1


def cmd_sessions(args) -> int:
    from .sessions import list_sessions

    rows = list_sessions()
    if not rows:
        print("No sessions recorded yet.")
    for s in rows:
        sm = s.get("summary") or {}
        dur = (s.get("duration_s") or 0) / 60
        print(f"{s['id']}  {dur:5.1f} min  breath {_fmt(sm.get('breath_start'))}→{_fmt(sm.get('breath_end'))}/min  "
              f"still {_fmt(sm.get('stillness_pct'), 0)}%  shifts {sm.get('shifts', 0)}  {s.get('practice') or ''}")
    return 0


def cmd_reprocess(args) -> int:
    """Run the current analysis over a recorded session's raw capture."""
    import csv

    from .protocol import parse_csi
    from .recorder import METRIC_FIELDS, read_capture
    from .sessions import session_path
    from .vitals import VitalsEngine

    p = session_path(args.session)
    if not p:
        print("No such session.")
        return 1
    eng = VitalsEngine()
    out = p / "metrics_reprocessed.csv"
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=METRIC_FIELDS, extrasaction="ignore")
        w.writeheader()
        t0 = None
        next_t = None
        for t, pkt in read_capture(p / "raw.mscap.gz"):
            f = parse_csi(pkt)
            if not f:
                continue
            t0 = t if t0 is None else t0
            next_t = t + 1 if next_t is None else next_t
            eng.add_frame(t, f)
            while t >= next_t:
                row = eng.compute(next_t).as_dict()
                row["t"] = round(next_t - t0, 3)
                w.writerow(row)
                next_t += 1
                eng.drain_wave()
    print(f"Wrote {out}")
    return 0


def cmd_fetch_assets(args) -> int:
    from . import assets

    try:
        assets.fetch_sync()
        print(f"Webcam posture model ready in {assets.asset_dir()}")
        return 0
    except Exception as e:
        print(f"Could not download the posture model now ({e}). It will be retried when you turn the camera on.")
        return 1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="medisense", description="Contactless meditation tracking (ESP32-S3 + RuView).")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    def add_start(p, demo: bool):
        p.add_argument("--port", type=int, default=WEB_PORT, help="web page port (default 8765)")
        p.add_argument("--udp-port", type=int, default=UDP_PORT, help=argparse.SUPPRESS)
        p.add_argument("--no-browser", action="store_true", help="don't open the browser")
        p.add_argument("--quiet", action="store_true", help="no live line in the terminal")
        p.add_argument("--verbose", action="store_true", help="detailed log in ~/MediSense/medisense.log")
        p.set_defaults(func=cmd_start, demo=demo)

    add_start(sub.add_parser("start", help="run MediSense (default)"), demo=False)
    add_start(sub.add_parser("demo", help="run with a simulated meditator, no board needed"), demo=True)
    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)
    sub.add_parser("sessions", help="list recorded sessions").set_defaults(func=cmd_sessions)
    rp = sub.add_parser("reprocess", help="re-analyse a session's raw capture")
    rp.add_argument("session")
    rp.set_defaults(func=cmd_reprocess)
    sub.add_parser("fetch-assets", help="download the webcam posture model now").set_defaults(func=cmd_fetch_assets)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        args = ap.parse_args(["start", *(argv or sys.argv[1:])])
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(args.func(args))
