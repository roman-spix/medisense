"""Plug-and-play handling of the ESP32-S3 board over USB.

Lifecycle, driven by DeviceManager:

    no board -> board on USB -> identify (chip, MAC, flash size)
      -> known board with current firmware/settings: just watch its log
      -> known board, Mac's address changed: rewrite settings only (seconds)
      -> new board: ask the person (setup wizard), then flash firmware + settings
    -> watch the board's log: joined WiFi? streaming? -> data arrives over WiFi

esptool runs as a separate process so a stuck flash can never freeze the app.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from serial.tools import list_ports

from . import firmware, network
from .config import UDP_PORT, DeviceRegistry, load_wifi_password, save_wifi_password

log = logging.getLogger("medisense.device")

ESPRESSIF_VID = 0x303A  # native USB (USB-Serial/JTAG) on ESP32-S3
BRIDGE_VIDS = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH34x/CH343/CH9102",
    0x0403: "FTDI",
}

# ESP-IDF WiFi disconnect reasons -> what to tell the person.
WIFI_REASONS = {
    2: "the router rejected the board (authentication expired). Check the password.",
    15: "the WiFi password seems to be wrong.",
    201: "the board can't find that WiFi network. It must be a 2.4 GHz network within range.",
    202: "the WiFi password seems to be wrong (authentication failed).",
    203: "the router refused the connection (association failed).",
    204: "the WiFi password seems to be wrong (handshake timed out).",
    205: "the connection to the router failed.",
    210: "the router's security mode isn't supported (WPA3-only?). Enable WPA2/WPA3 mixed mode on the router.",
    211: "the router's security mode isn't supported. Enable WPA2 on the router.",
}
WRONG_PASSWORD_REASONS = {15, 202, 204}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# A reset over the native USB port makes macOS drop and re-add the device for
# a moment. Only treat the board as unplugged after it's been gone this long.
UNPLUG_AFTER_S = 4.0


@dataclass
class UsbPort:
    device: str
    vid: int | None
    pid: int | None
    description: str
    serial_number: str | None

    @property
    def native(self) -> bool:
        return self.vid == ESPRESSIF_VID

    def label(self) -> str:
        if self.native:
            return "ESP32-S3 (native USB port)"
        return BRIDGE_VIDS.get(self.vid or -1, self.description or "USB serial")


def find_ports() -> list[UsbPort]:
    """USB serial ports that could be an ESP32 board, best candidates first."""
    found = []
    for p in list_ports.comports():
        dev = p.device
        if sys.platform == "darwin" and dev.startswith("/dev/tty."):
            dev = dev.replace("/dev/tty.", "/dev/cu.", 1)
        if "Bluetooth" in dev or "debug-console" in dev:
            continue
        likely = p.vid == ESPRESSIF_VID or p.vid in BRIDGE_VIDS
        if not likely:
            desc = f"{p.description} {p.manufacturer or ''}".lower()
            likely = any(k in desc for k in ("cp210", "ch34", "ch910", "usb jtag", "esp32", "usbserial", "usbmodem"))
        if likely:
            found.append(UsbPort(dev, p.vid, p.pid, p.description or "", p.serial_number))
    found.sort(key=lambda u: (not u.native, u.device))
    return found


# ------------------------------------------------------------------ esptool

@dataclass
class ChipInfo:
    chip: str
    mac: str
    flash_size: str | None
    features: str = ""

    @property
    def is_s3(self) -> bool:
        return "ESP32-S3" in self.chip.upper()


class EsptoolError(RuntimeError):
    pass


def explain_esptool_failure(output: str) -> str:
    o = output.lower()
    if "port is busy" in o or "resource busy" in o or "could not exclusively lock" in o:
        return "The USB port is in use by another program (Arduino IDE serial monitor, another MediSense window?). Close it and unplug/replug the board."
    if "permission denied" in o:
        return "No permission to use the USB port."
    if "failed to connect" in o or "no serial data received" in o or "wrong boot mode" in o:
        return ("The board didn't answer. Hold the BOOT button, press and release RESET, release BOOT, then try again. "
                "If it keeps failing, try the board's other USB port or a different cable (some cables are charge-only).")
    if "this chip is esp32" in o or "chip is esp32 " in o:
        return "This is an older ESP32, not an ESP32-S3."
    return "esptool failed. Details: " + output.strip().splitlines()[-1] if output.strip() else "esptool failed."


async def run_esptool(args: list[str], on_line: Callable[[str], None] | None = None, timeout: float = 240) -> str:
    cmd = [sys.executable, "-m", "esptool", *args]
    log.info("esptool %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    lines: list[str] = []
    buf = b""

    async def pump():
        nonlocal buf
        assert proc.stdout
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            # esptool updates progress with \r on a terminal and \n otherwise.
            while True:
                idx = min([i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0], default=-1)
                if idx < 0:
                    break
                line = buf[:idx].decode(errors="replace").strip()
                buf = buf[idx + 1:]
                if line:
                    lines.append(line)
                    if on_line:
                        on_line(line)

    try:
        await asyncio.wait_for(pump(), timeout)
        rc = await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise EsptoolError("esptool took too long and was stopped. Unplug and replug the board, then try again.") from None
    out = "\n".join(lines)
    if rc != 0:
        raise EsptoolError(explain_esptool_failure(out))
    return out


def parse_chip_info(out: str) -> ChipInfo:
    chip = re.search(r"Chip is (.+)", out)
    if not chip:
        det = re.search(r"Detecting chip type\.\.\. (\S+)", out)
        chip_name = det.group(1) if det else "unknown"
    else:
        chip_name = chip.group(1).strip()
    mac = re.search(r"MAC: ([0-9a-fA-F:]{17})", out)
    flash = re.search(r"Detected flash size: (\S+)", out)
    feats = re.search(r"Features: (.+)", out)
    if not mac:
        raise EsptoolError("Couldn't read the board's MAC address.")
    return ChipInfo(chip_name, mac.group(1).lower(), flash.group(1) if flash else None, feats.group(1) if feats else "")


async def identify(port: str) -> ChipInfo:
    out = await run_esptool(["--port", port, "--after", "hard_reset", "flash_id"], timeout=60)
    return parse_chip_info(out)


class FlashProgress:
    """Turns esptool's 'Writing at 0x...' lines into overall percent."""

    def __init__(self, regions: list[tuple[int, int]]):
        self.regions = sorted(regions)  # (offset, size)
        self.total = sum(s for _, s in regions) or 1

    def percent(self, line: str) -> float | None:
        m = re.search(r"Writing at 0x([0-9a-fA-F]+)", line)
        if not m:
            return None
        addr = int(m.group(1), 16)
        done = 0
        for off, size in self.regions:
            if addr >= off + size:
                done += size
            elif addr >= off:
                done += addr - off
        return min(100.0, 100.0 * done / self.total)


async def flash_board(
    port: str,
    image: firmware.FirmwareImage | None,
    nvs: bytes,
    on_progress: Callable[[float, str], None],
    baud: int = 460800,
) -> None:
    """Write firmware (if `image`) and the settings partition, then reset the board."""
    with tempfile.TemporaryDirectory(prefix="medisense-flash-") as td:
        nvs_path = Path(td) / "nvs.bin"
        nvs_path.write_bytes(nvs)
        files: list[tuple[int, Path]] = list(image.files) if image else []
        files.append((firmware.NVS_OFFSET, nvs_path))
        files.sort()
        regions = [(off, f.stat().st_size) for off, f in files]
        prog = FlashProgress(regions)
        args = ["--chip", "esp32s3", "--port", port, "--baud", str(baud),
                "--before", "default_reset", "--after", "hard_reset", "write_flash"]
        if image:
            args += ["--flash_mode", "dio", "--flash_freq", "80m", "--flash_size", image.flash_size]
        for off, f in files:
            args += [hex(off), str(f)]

        def on_line(line: str) -> None:
            p = prog.percent(line)
            if p is not None:
                on_progress(p, "Writing")
            elif "Connecting" in line:
                on_progress(0.0, "Connecting to the board")
            elif "Hash of data verified" in line:
                on_progress(-1, "Verified")

        try:
            await run_esptool(args, on_line, timeout=300)
        except EsptoolError:
            if baud != 115200:
                on_progress(0.0, "Retrying at a slower speed")
                await flash_board(port, image, nvs, on_progress, baud=115200)
            else:
                raise


# --------------------------------------------------------------- board log

STATS_RE = re.compile(r"medisense stats: heard=(\d+) used=(\d+) to_board=(\d+) other=(\d+) "
                      r"foreign=(\d+) rate_drop=(\d+) any_sender=(\d)")


@dataclass
class BoardLog:
    """What we've learned from the board's serial log since its last boot."""

    firmware_version: str | None = None
    ssid: str | None = None
    target: str | None = None
    target_ip_nvs: str | None = None
    filter_mac_nvs: str | None = None
    wifi: str = "unknown"  # unknown / connecting / connected / failed
    board_ip: str | None = None
    last_reason: int | None = None
    reason_counts: dict = field(default_factory=dict)
    csi_callbacks: int = 0
    streaming: bool = False
    boots: int = 0
    medisense_fw: str | None = None  # "3": the board's own start-up line (MediSense firmware 3+)
    stats: list = field(default_factory=list)  # last two (t, heard, used, to_board, other, foreign)
    any_sender: bool = False  # the firmware gave up on the router filter
    send_errors: int = 0
    recent: list = field(default_factory=list)

    def feed(self, line: str) -> None:
        line = ANSI_RE.sub("", line).strip()
        if not line:
            return
        self.recent.append(line)
        del self.recent[:-200]
        if m := re.search(r"CSI Node .*? v(\d+\.\d+\.\d+)", line):
            self.boots += 1
            self.firmware_version = m.group(1)
            self.wifi = "unknown"
            self.board_ip = None
            self.streaming = False
            self.csi_callbacks = 0
            self.reason_counts = {}
            self.last_reason = None
            self.medisense_fw = None
            self.stats = []
            self.any_sender = False
            self.send_errors = 0
        if m := re.search(r"NVS override: target_ip=(\S+)", line):
            self.target_ip_nvs = m.group(1)
        if m := re.search(r"NVS override: filter_mac=([0-9a-f:]{17})", line):
            self.filter_mac_nvs = m.group(1)
        if m := re.search(r"connecting to SSID: (.*)$", line):
            self.ssid = m.group(1)
            self.wifi = "connecting"
        if m := re.search(r"WiFi disconnected, reason=(\d+)", line):
            r = int(m.group(1))
            self.last_reason = r
            self.reason_counts[r] = self.reason_counts.get(r, 0) + 1
            if self.wifi != "failed":
                self.wifi = "connecting"
        if m := re.search(r"Got IP: (\d+\.\d+\.\d+\.\d+)", line):
            self.board_ip = m.group(1)
            self.wifi = "connected"
        if "Failed to connect to WiFi" in line:
            self.wifi = "failed"
        if m := re.search(r"CSI streaming active\W+(\S+:\d+)", line):
            self.streaming = True
            self.target = m.group(1)
        if m := re.search(r"CSI cb #(\d+)", line):
            self.csi_callbacks = max(self.csi_callbacks, int(m.group(1)))
        if m := re.search(r"MediSense firmware (\d+)", line):
            self.medisense_fw = m.group(1)
        if m := STATS_RE.search(line):
            self.stats.append((time.monotonic(), *(int(g) for g in m.groups()[:5])))
            del self.stats[:-2]
            self.any_sender = m.group(7) == "1"
        if "sendto failed" in line:
            self.send_errors += 1

    def hears(self) -> dict | None:
        """What the board heard, from its 10-second stats line (MediSense firmware 3+)."""
        if not self.stats:
            return None
        t1, heard, used, to_board, other, foreign = self.stats[-1]
        out = {"heard": heard, "used": used, "to_board": to_board, "foreign": foreign,
               "heard_per_s": None, "used_per_s": None, "any_sender": self.any_sender}
        if len(self.stats) == 2:
            t0, h0, u0 = self.stats[0][:3]
            if t1 - t0 > 1 and heard >= h0 and used >= u0:
                out["heard_per_s"] = round((heard - h0) / (t1 - t0), 1)
                out["used_per_s"] = round((used - u0) / (t1 - t0), 1)
        return out

    @property
    def sending(self) -> bool:
        """The board measures frames, so it is sending them (to `target`)."""
        h = self.hears()
        return (h is not None and h["used"] > 0) or self.csi_callbacks > 0

    def wifi_problem(self) -> str | None:
        if self.wifi == "connected":
            return None
        if self.last_reason is None:
            return "The board couldn't join the WiFi." if self.wifi == "failed" else None
        if self.wifi != "failed" and sum(self.reason_counts.values()) < 3:
            return None
        return "The board couldn't join the WiFi: " + WIFI_REASONS.get(self.last_reason, f"error code {self.last_reason}.")

    @property
    def wrong_password(self) -> bool:
        return self.last_reason in WRONG_PASSWORD_REASONS and sum(self.reason_counts.values()) >= 3


class SerialMonitor:
    """Reads the board's log in a background thread without resetting it."""

    def __init__(self, port: str, on_line: Callable[[str], None]):
        self.port = port
        self.on_line = on_line
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ser = None
        self.error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="serial-monitor", daemon=True)
        self._thread.start()

    def _open(self):
        import serial

        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = 115200
        ser.timeout = 0.5
        ser.dtr = False  # opening must not reset the board or put it in download mode
        ser.rts = False
        ser.open()
        return ser

    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                if self._ser is None:
                    self._ser = self._open()
                chunk = self._ser.read(512)
            except Exception as e:  # unplugged, busy, ...
                self.error = str(e)
                self._close()
                if self._stop.wait(1.0):
                    break
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.on_line(line.decode(errors="replace"))
            if len(buf) > 4096:
                buf = b""
        self._close()

    def reset_board(self) -> bool:
        """Pulse RTS (EN) to reboot the board into its firmware; works for both USB ports."""
        ser = self._ser
        if ser is None:
            return False
        try:
            ser.dtr = False
            ser.rts = True
            time.sleep(0.15)
            ser.rts = False
            return True
        except Exception:
            return False

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


# ----------------------------------------------------------- the manager

class DeviceManager:
    """Owns the USB board: detection, setup, settings refresh, log watching.

    `state` (shown in the app) is one of:
      no_board, identifying, unsupported, needs_setup, flashing, starting,
      wifi_failed, connected, error
    """

    def __init__(self, registry: DeviceRegistry, on_change: Callable[[], None], data_seen: Callable[[], float]):
        self.registry = registry
        self.on_change = on_change
        self.data_seen = data_seen  # monotonic time of the last CSI packet
        self.state = "no_board"
        self.message = "Plug the ESP32-S3 board into a USB port."
        self.port: UsbPort | None = None
        self.chip: ChipInfo | None = None
        self.progress: float | None = None
        self.log = BoardLog()
        self.monitor: SerialMonitor | None = None
        self.busy = False
        self.suggested_ssid: str | None = None
        self.filter_fallback_done = False
        self._started_at = 0.0
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ignored: set[str] = set()
        self._saved_pw = False
        self._missing_since: float | None = None
        self.line_listeners: list[Callable[[str], None]] = []
        self._fw_checked = (-1e9, None)
        self._target_checked_boot = -1  # board boot whose data address was checked
        self._target_fixes = 0  # address corrections since the board was plugged in

    async def _refresh_ssid(self) -> None:
        ssid = await asyncio.to_thread(network.current_ssid)
        self.suggested_ssid = ssid or self.suggested_ssid
        if self.suggested_ssid:
            self._saved_pw = bool(await asyncio.to_thread(load_wifi_password, self.suggested_ssid))

    # -- public ------------------------------------------------------------

    def snapshot(self) -> dict:
        reg = self.registry.get(self.chip.mac) if self.chip else None
        return {
            "state": self.state,
            "message": self.message,
            "progress": self.progress,
            "port": self.port.device if self.port else None,
            "port_label": self.port.label() if self.port else None,
            "chip": self.chip.chip if self.chip else None,
            "mac": self.chip.mac if self.chip else None,
            "flash_size": self.chip.flash_size if self.chip else None,
            "board_ip": self.log.board_ip,
            "board_wifi": self.log.wifi,
            "board_firmware": self.log.firmware_version,
            "firmware_label": reg.get("firmware_version") if reg else None,  # what MediSense installed
            "csi_callbacks": self.log.csi_callbacks,
            "board_medisense": self.log.medisense_fw,
            "board_target": self.log.target,
            "board_hears": self.log.hears(),
            "configured": reg.get("settings") if reg else None,
            "suggested_ssid": self.suggested_ssid,
            "saved_password": self._saved_pw,
            "log_tail": self.log.recent[-25:],
        }

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.monitor:
            await asyncio.to_thread(self.monitor.stop)

    async def setup(self, ssid: str, password: str, remember: bool = True) -> None:
        """Install firmware + settings on the connected board (from the wizard)."""
        if not self.port or not self.chip:
            raise RuntimeError("No board is connected.")
        if self.busy:
            raise RuntimeError("The board is busy, please wait.")
        problem = firmware.validate(firmware.BoardSettings(ssid=ssid, password=password, target_ip="0.0.0.0"))
        if problem:
            raise ValueError(problem)
        self.busy = True  # claim the board while we look up the network
        try:
            self._set("flashing", "Preparing…", 0)
            settings = await asyncio.to_thread(self._desired_settings, ssid, password)
            if remember:
                await asyncio.to_thread(save_wifi_password, ssid, password)
            image = firmware.pick_image(self.chip.flash_size)
            reg = self.registry.get(self.chip.mac)
            full = not (reg and reg.get("firmware_sha") == image.app_sha256)
        except Exception as e:
            self.busy = False
            self._set("needs_setup" if not self.registry.get(self.chip.mac) else "wifi_failed", str(e))
            raise
        self.busy = False
        asyncio.create_task(self._provision(settings, full=full))

    async def reset_board(self) -> None:
        if self.monitor:
            await asyncio.to_thread(self.monitor.reset_board)

    # -- internals ----------------------------------------------------------

    def _set(self, state: str, message: str, progress: float | None = None) -> None:
        self.state, self.message, self.progress = state, message, progress
        self.on_change()

    def _desired_settings(self, ssid: str, password: str, use_filter: bool = True) -> firmware.BoardSettings:
        ip = network.lan_ip(self.log.board_ip)
        if not ip:
            raise RuntimeError("This Mac doesn't seem to be on a network. Connect it to your WiFi first.")
        bssid = network.router_bssid_24ghz(ssid) if use_filter else None
        return firmware.BoardSettings(ssid=ssid, password=password, target_ip=ip, target_port=UDP_PORT, filter_mac=bssid)

    async def _watch(self) -> None:
        await self._refresh_ssid()
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the watcher die
                log.exception("device watcher")
                self._set("error", f"Unexpected problem: {e}")
            await asyncio.sleep(2.0)

    async def _poll(self) -> None:
        if self.busy:
            return
        ports = await asyncio.to_thread(find_ports)
        devices = {p.device for p in ports}
        self._ignored &= devices  # a replugged board gets a fresh try
        if self.port and self.port.device not in devices:
            now = time.monotonic()
            if self._missing_since is None:
                self._missing_since = now
            if now - self._missing_since < UNPLUG_AFTER_S:
                return  # probably just re-enumerating after a reset
            await self._unplugged()
        self._missing_since = None
        if not self.port:
            fresh = [p for p in ports if p.device not in self._ignored]
            if fresh:
                await self._plugged(fresh[0])
            elif self.state != "no_board":
                self._set("no_board", "Plug the ESP32-S3 board into a USB port.")
            return
        self._check_health()

    async def _unplugged(self) -> None:
        if self.monitor:
            await asyncio.to_thread(self.monitor.stop)
        self.monitor = None
        self.port = None
        self.chip = None
        self.log = BoardLog()
        self._ignored.clear()
        self._target_fixes = 0
        if time.monotonic() - self.data_seen() < 5:
            self._set("no_board", "Board unplugged from USB, but data is still arriving over WiFi.")
        else:
            self._set("no_board", "Board unplugged. Plug it back in to continue.")

    async def _plugged(self, port: UsbPort) -> None:
        self.port = port
        self._target_fixes = 0
        self.busy = True
        self._set("identifying", f"Board found ({port.label()}). Checking what it is…")
        try:
            self.chip = await identify(port.device)
        except EsptoolError as e:
            self.busy = False
            self._ignored.add(port.device)  # don't retry in a loop; replugging retries
            self.port = None
            self._set("error", str(e))
            return
        finally:
            self.busy = False
        if not self.chip.is_s3:
            self._set("unsupported", f"This board is an {self.chip.chip}. MediSense needs an ESP32-S3 board.")
            return

        await self._refresh_ssid()
        reg = self.registry.get(self.chip.mac)
        try:
            image = firmware.pick_image(self.chip.flash_size)
        except ValueError as e:
            self._set("unsupported", str(e))
            return
        if reg and reg.get("firmware_sha") == image.app_sha256:
            ssid = reg["settings"]["ssid"]
            password = load_wifi_password(ssid)
            want_ip = await asyncio.to_thread(network.lan_ip)
            if want_ip and want_ip != reg["settings"]["target_ip"] and password is not None:
                settings = firmware.BoardSettings(**{**reg["settings"], "password": password, "target_ip": want_ip})
                self._set("flashing", f"This Mac's network address changed ({want_ip}). Updating the board…", 0)
                await self._provision(settings, full=False)
                return
            self._start_monitor(reset=True)
            self._set("starting", "Board ready. Waiting for it to join the WiFi…")
            return
        if reg and reg.get("settings", {}).get("ssid"):
            # A board set up earlier, with older firmware: update it in place
            # (same WiFi settings) when the password is in the Keychain.
            ssid = reg["settings"]["ssid"]
            password = await asyncio.to_thread(load_wifi_password, ssid)
            if password is not None:
                want_ip = await asyncio.to_thread(network.lan_ip) or reg["settings"]["target_ip"]
                settings = firmware.BoardSettings(**{**reg["settings"], "password": password, "target_ip": want_ip})
                self._set("flashing", f"Updating the board to MediSense firmware {image.version}…", 0)
                await self._provision(settings, full=True)
                return
            self._set("needs_setup", "A firmware update is ready for this board. Enter your WiFi password once to install it.")
            return
        self._set("needs_setup", "New board. Set it up once: MediSense installs its firmware and your WiFi details.")

    async def _provision(self, settings: firmware.BoardSettings, full: bool) -> None:
        assert self.port and self.chip
        self.busy = True
        if self.monitor:
            await asyncio.to_thread(self.monitor.stop)
            self.monitor = None
        try:
            image = firmware.pick_image(self.chip.flash_size) if full else None
            if image:
                firmware.verify_image(image)
            nvs = await asyncio.to_thread(firmware.build_nvs, settings)
            what = "Installing MediSense firmware" if full else "Updating board settings"

            def on_progress(p: float, stage: str) -> None:
                if p >= 0:
                    self.progress = p
                self.message = f"{what}… {stage}" + (f" {p:.0f}%" if p > 0 else "")
                self.on_change()

            self._set("flashing", f"{what}…", 0)
            await flash_board(self.port.device, image, nvs, on_progress)
            reg = self.registry.get(self.chip.mac) or {}
            self.registry.put(self.chip.mac, {
                **reg,
                "chip": self.chip.chip,
                "flash_size": self.chip.flash_size,
                "variant": (image.variant if image else reg.get("variant")),
                "firmware_sha": (image.app_sha256 if image else reg.get("firmware_sha")),
                "firmware_version": (image.version if image else reg.get("firmware_version")),
                "settings": settings.summary(),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            self.log = BoardLog()
            self.filter_fallback_done = False
            self._start_monitor(reset=False)  # esptool already reset it
            self._set("starting", "Done. The board is restarting and joining your WiFi…")
        except (EsptoolError, ValueError, RuntimeError) as e:
            self._set("error", str(e))
            self._start_monitor(reset=False)
        finally:
            self.busy = False
            self.progress = None

    def _start_monitor(self, reset: bool) -> None:
        assert self.port
        if self.monitor:
            return
        self._started_at = time.monotonic()
        loop = self._loop

        def on_line(line: str) -> None:
            if loop:
                loop.call_soon_threadsafe(self._on_log_line, line)

        self.monitor = SerialMonitor(self.port.device, on_line)
        self.monitor.start()
        if reset:
            async def later():
                await asyncio.sleep(0.6)
                await self.reset_board()
            asyncio.create_task(later())

    def _log_key(self) -> tuple:
        b = self.log
        return (b.wifi, b.board_ip, b.streaming, b.boots, sum(b.reason_counts.values()))

    def _on_log_line(self, line: str) -> None:
        before = self._log_key()
        self.log.feed(line)
        for listener in self.line_listeners:
            try:
                listener(line)
            except Exception:
                log.exception("board log listener")
        if self._log_key() != before:
            self._check_health()

    def _check_health(self) -> None:
        """Turn log + data flow into a state the person can act on."""
        if self.busy or self.state in ("needs_setup", "unsupported", "identifying", "flashing"):
            return
        now = time.monotonic()
        receiving = now - self.data_seen() < 10  # short WiFi hiccups aren't a disconnection
        if receiving:
            if self.state != "connected" or "Live" not in self.message:
                self._set("connected", f"Live. Board on WiFi{f' at {self.log.board_ip}' if self.log.board_ip else ''}.")
            return
        problem = self.log.wifi_problem()
        if problem:
            self._set("wifi_failed", problem)
            return
        if self.log.wifi == "connected":
            since = now - self._started_at
            if self.log.target and self.log.board_ip and self._target_checked_boot != self.log.boots:
                # The board says where it sends its data: make sure that's this Mac.
                self._target_checked_boot = self.log.boots
                asyncio.create_task(self._verify_target())
            if since > 25 and not self.filter_fallback_done and self.chip:
                reg = self.registry.get(self.chip.mac) or {}
                s = reg.get("settings") or {}
                if s.get("filter_mac") and (self.log.csi_callbacks == 0 or self.log.any_sender):
                    # The router address we gave the board may be wrong
                    # (mesh units, band steering). Listen to everything instead.
                    self.filter_fallback_done = True
                    asyncio.create_task(self._drop_filter(s))
                    return
            if since > 20:
                self._set("connected", self._no_data_message())
            else:
                self._set("starting", f"Board joined WiFi ({self.log.board_ip}). Waiting for data…")
            return
        if self.state not in ("starting", "error"):
            self._set("starting", "Waiting for the board to join the WiFi…")

    def _firewall_state(self) -> str | None:
        at, value = self._fw_checked
        if time.monotonic() - at > 60:
            value = network.firewall_state(os.path.realpath(sys.executable))
            self._fw_checked = (time.monotonic(), value)
        return value

    def _no_data_message(self) -> str:
        """The board is on WiFi but no data arrives: say which side it is."""
        b = self.log
        where = f"The board is on WiFi ({b.board_ip})"
        h = b.hears()
        if h is not None and h["heard"] == 0:
            return (f"{where} but doesn't pick up any WiFi frames to measure. Click “Restart board”; "
                    "if that doesn't help, unplug it and plug it back in.")
        if not b.sending and b.streaming and b.csi_callbacks == 0 and h is None and b.medisense_fw is None:
            return (f"{where} but isn't measuring anything yet. Unplug the board and plug it back in: "
                    "MediSense then checks its firmware and updates it if needed.")
        fw = self._firewall_state()
        if fw == "block_all":
            hint = (" This Mac's firewall blocks all incoming connections: System Preferences → Security & Privacy"
                    " → Firewall → Firewall Options…, untick “Block all incoming connections”.")
        elif fw in ("app_blocked", "on"):
            hint = (" This Mac's firewall may be stopping it: System Preferences → Security & Privacy → Firewall"
                    " → Firewall Options…, find “python3.12” in the list and choose “Allow incoming connections”.")
        else:
            hint = (" Check that the router doesn't keep WiFi devices apart (“AP isolation” or “client"
                    " isolation” in its settings), and switch off any VPN on this Mac.")
        if b.sending:
            to = f" to {b.target.split(':')[0]}" if b.target else ""
            return f"{where} and sends its data{to}, but nothing arrives on this Mac.{hint}"
        return f"{where} but no data reaches this Mac.{hint}"

    async def _verify_target(self) -> None:
        """Re-point the board if it sends its data somewhere other than this Mac
        (the Mac's address changed, or a VPN was on when it was set up)."""
        b = self.log
        if not (b.target and b.board_ip and self.chip) or self.busy or self._target_fixes >= 1:
            return
        ip, _, port = b.target.rpartition(":")
        want = await asyncio.to_thread(network.lan_ip, b.board_ip)
        if not want or (ip == want and port == str(UDP_PORT)):
            return
        reg = self.registry.get(self.chip.mac) or {}
        s = reg.get("settings") or {}
        if not s.get("ssid") or self.busy:
            return
        password = await asyncio.to_thread(load_wifi_password, s["ssid"])
        if password is None:
            self._set("connected", f"The board sends its data to {ip}, but this Mac is {want}. "
                      "Click “Change WiFi” and enter the WiFi password once to fix it.")
            return
        self._target_fixes += 1
        settings = firmware.BoardSettings(**{**s, "password": password, "target_ip": want, "target_port": UDP_PORT})
        self._set("flashing", f"The board was sending its data to {ip}, but this Mac is {want}. Updating the board…", 0)
        await self._provision(settings, full=False)

    async def _drop_filter(self, s: dict) -> None:
        password = load_wifi_password(s["ssid"])
        if password is None:
            return
        settings = firmware.BoardSettings(**{**s, "password": password, "filter_mac": None})
        self._set("flashing", "Adjusting the board to your router…", 0)
        await self._provision(settings, full=False)
        self.filter_fallback_done = True
