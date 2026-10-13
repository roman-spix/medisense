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
        raise EsptoolError("esptool took too long and was stopped. Unplug and replug the board, then try again.")
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
    last_line_at: float = 0.0
    recent: list = field(default_factory=list)

    def feed(self, line: str) -> None:
        line = ANSI_RE.sub("", line).strip()
        if not line:
            return
        self.last_line_at = time.monotonic()
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
            "csi_callbacks": self.log.csi_callbacks,
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
        ip = None
        if self.log.board_ip:
            ip = network.local_ip(self.log.board_ip)
        ip = ip or network.local_ip()
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
            await self._unplugged()
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
        if time.monotonic() - self.data_seen() < 5:
            self._set("no_board", "Board unplugged from USB, but data is still arriving over WiFi.")
        else:
            self._set("no_board", "Board unplugged. Plug it back in to continue.")

    async def _plugged(self, port: UsbPort) -> None:
        self.port = port
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
            want_ip = await asyncio.to_thread(network.local_ip)
            if want_ip and want_ip != reg["settings"]["target_ip"] and password is not None:
                settings = firmware.BoardSettings(**{**reg["settings"], "password": password, "target_ip": want_ip})
                self._set("flashing", f"This Mac's network address changed ({want_ip}). Updating the board…", 0)
                await self._provision(settings, full=False)
                return
            self._start_monitor(reset=True)
            self._set("starting", "Board ready. Waiting for it to join the WiFi…")
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

    def _on_log_line(self, line: str) -> None:
        before = (self.log.wifi, self.log.board_ip, self.log.streaming)
        self.log.feed(line)
        if (self.log.wifi, self.log.board_ip, self.log.streaming) != before:
            self._check_health()

    def _check_health(self) -> None:
        """Turn log + data flow into a state the person can act on."""
        if self.busy or self.state in ("needs_setup", "unsupported", "identifying", "flashing"):
            return
        now = time.monotonic()
        receiving = now - self.data_seen() < 3
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
            if since > 25 and not self.filter_fallback_done and self.chip:
                reg = self.registry.get(self.chip.mac) or {}
                s = reg.get("settings") or {}
                if s.get("filter_mac") and self.log.csi_callbacks == 0:
                    # The router address we gave the board may be wrong
                    # (mesh units, band steering). Listen to everything instead.
                    self.filter_fallback_done = True
                    asyncio.create_task(self._drop_filter(s))
                    return
            if since > 20:
                fw = network.firewall_blocks_incoming()
                hint = (" The macOS firewall is blocking incoming connections: System Preferences → Security & Privacy → Firewall."
                        if fw else " Is the Mac on the same WiFi network as the board?")
                self._set("connected", f"The board is on WiFi ({self.log.board_ip}) but no data reaches this Mac.{hint}")
            else:
                self._set("starting", f"Board joined WiFi ({self.log.board_ip}). Waiting for data…")
            return
        if self.state not in ("starting", "error"):
            self._set("starting", "Waiting for the board to join the WiFi…")

    async def _drop_filter(self, s: dict) -> None:
        password = load_wifi_password(s["ssid"])
        if password is None:
            return
        settings = firmware.BoardSettings(**{**s, "password": password, "filter_mac": None})
        self._set("flashing", "Adjusting the board to your router…", 0)
        await self._provision(settings, full=False)
        self.filter_fallback_done = True
