"""Walk the DeviceManager through a board's life with USB, esptool and the
network faked out: new board -> setup -> WiFi -> data -> IP change -> unplug."""

import asyncio
import time

import pytest

from medisense import device, firmware
from medisense.config import DeviceRegistry
from medisense.device import ChipInfo, DeviceManager, UsbPort

PORT = UsbPort("/dev/cu.usbmodem1101", 0x303A, 0x1001, "USB JTAG/serial debug unit", "F4:12")
CHIP = ChipInfo("ESP32-S3 (QFN56) (revision v0.2)", "f4:12:fa:4b:1c:08", "16MB")


class FakeMonitor:
    instances: list["FakeMonitor"] = []

    def __init__(self, port, on_line):
        self.port, self.on_line = port, on_line
        self.resets = 0
        self.stopped = False
        FakeMonitor.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def reset_board(self):
        self.resets += 1
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDISENSE_HOME", str(tmp_path / "home"))
    FakeMonitor.instances = []
    state = {"ports": [], "ip": "192.168.1.23", "flashes": [], "passwords": {}, "data_at": -1e9}

    async def fake_identify(port):
        return CHIP

    async def fake_flash(port, image, nvs, on_progress, baud=460800):
        on_progress(50.0, "Writing")
        state["flashes"].append({"port": port, "full": image is not None, "nvs": nvs})

    monkeypatch.setattr(device, "find_ports", lambda: list(state["ports"]))
    monkeypatch.setattr(device, "identify", fake_identify)
    monkeypatch.setattr(device, "flash_board", fake_flash)
    monkeypatch.setattr(device, "SerialMonitor", FakeMonitor)
    monkeypatch.setattr(device.network, "local_ip", lambda towards="8.8.8.8": state["ip"])
    monkeypatch.setattr(device.network, "current_ssid", lambda: "Home")
    monkeypatch.setattr(device.network, "router_bssid_24ghz", lambda ssid: "a4:2b:b0:01:02:0f")
    monkeypatch.setattr(device.network, "firewall_blocks_incoming", lambda: False)
    monkeypatch.setattr(device, "save_wifi_password", lambda s, p: state["passwords"].__setitem__(s, p))
    monkeypatch.setattr(device, "load_wifi_password", lambda s: state["passwords"].get(s))
    monkeypatch.setattr(device, "UNPLUG_AFTER_S", 0.2)
    return state


async def wait_for(cond, timeout=20.0):
    """Building the settings image starts a separate Python, which takes a
    second or two on slower Macs; wait for the outcome instead of guessing."""
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


def feed(mgr, *lines):
    for line in lines:
        mgr._on_log_line(line)


BOOT = [
    "I (312) main: ESP32-S3 CSI Node (ADR-018 / ADR-110) - v0.8.8 - Node ID: 1",
    "I (500) main: WiFi STA initialized, connecting to SSID: Home",
    "I (4000) main: Got IP: 192.168.1.77",
    "I (4100) main: CSI streaming active -> 192.168.1.23:5005 (edge_tier=0)",
]


async def test_board_lifecycle(env):
    reg = DeviceRegistry()
    changes = []
    mgr = DeviceManager(reg, lambda: changes.append(1), lambda: env["data_at"])
    mgr._loop = asyncio.get_running_loop()

    await mgr._poll()
    assert mgr.state == "no_board"

    # New board: identified, then the wizard is needed.
    env["ports"] = [PORT]
    await mgr._poll()
    assert mgr.state == "needs_setup" and mgr.chip.mac == CHIP.mac
    assert mgr.snapshot()["suggested_ssid"] == "Home"

    with pytest.raises(ValueError):
        await mgr.setup("Home", "short")

    await mgr.setup("Home", "correct horse battery")
    await wait_for(lambda: mgr.state == "starting")
    assert len(env["flashes"]) == 1 and env["flashes"][0]["full"]
    assert env["passwords"] == {"Home": "correct horse battery"}
    saved = reg.get(CHIP.mac)
    assert saved["settings"]["target_ip"] == "192.168.1.23"
    assert saved["settings"]["filter_mac"] == "a4:2b:b0:01:02:0f"
    assert saved["firmware_sha"] == firmware.load_image("esp32s3-8mb").app_sha256
    assert mgr.state == "starting"

    # The board boots and joins WiFi; then packets arrive.
    feed(mgr, *BOOT)
    assert mgr.state == "starting" and "192.168.1.77" in mgr.message
    env["data_at"] = time.monotonic()
    await mgr._poll()
    assert mgr.state == "connected" and mgr.message.startswith("Live")

    # A reset makes the native USB port vanish briefly: not an unplug.
    env["ports"] = []
    await mgr._poll()
    assert mgr.port is not None
    env["ports"] = [PORT]
    await mgr._poll()
    assert mgr.port is not None and len(env["flashes"]) == 1

    # Really unplugged.
    env["ports"] = []
    env["data_at"] = -1e9
    await mgr._poll()
    await asyncio.sleep(0.25)
    await mgr._poll()
    assert mgr.state == "no_board" and mgr.port is None

    # Plugged in again after the Mac got a new address: settings-only update.
    env["ip"] = "192.168.1.40"
    env["ports"] = [PORT]
    await mgr._poll()
    await wait_for(lambda: mgr.state == "starting")
    assert len(env["flashes"]) == 2 and env["flashes"][1]["full"] is False
    assert reg.get(CHIP.mac)["settings"]["target_ip"] == "192.168.1.40"

    # Plugged in again, nothing changed: no flashing, just watch and reset.
    env["ports"] = []
    await mgr._poll(); await asyncio.sleep(0.25); await mgr._poll()
    env["ports"] = [PORT]
    await mgr._poll()
    await asyncio.sleep(0.7)
    assert len(env["flashes"]) == 2
    assert FakeMonitor.instances[-1].resets == 1


async def test_wrong_password_then_fixed(env):
    reg = DeviceRegistry()
    mgr = DeviceManager(reg, lambda: None, lambda: env["data_at"])
    mgr._loop = asyncio.get_running_loop()
    env["ports"] = [PORT]
    await mgr._poll()
    await mgr.setup("Home", "wrong password")
    await wait_for(lambda: mgr.state == "starting")
    feed(mgr, BOOT[0], BOOT[1], *["W (2600) main: WiFi disconnected, reason=15 rssi=-40"] * 4)
    assert mgr.state == "wifi_failed" and "password" in mgr.message

    await mgr.setup("Home", "right password")
    await wait_for(lambda: mgr.state == "starting")
    assert len(env["flashes"]) == 2 and env["flashes"][1]["full"] is False  # firmware already there
    feed(mgr, *BOOT)
    assert mgr.state == "starting"


async def test_wrong_router_filter_is_dropped(env, monkeypatch):
    reg = DeviceRegistry()
    mgr = DeviceManager(reg, lambda: None, lambda: env["data_at"])
    mgr._loop = asyncio.get_running_loop()
    env["ports"] = [PORT]
    await mgr._poll()
    await mgr.setup("Home", "correct horse battery")
    await wait_for(lambda: mgr.state == "starting")
    feed(mgr, *BOOT)  # joined WiFi, but no CSI callbacks and no data
    mgr._started_at -= 30
    mgr._check_health()
    await wait_for(lambda: len(env["flashes"]) == 2 and mgr.state == "starting")
    assert len(env["flashes"]) == 2
    assert reg.get(CHIP.mac)["settings"]["filter_mac"] is None
    assert mgr.filter_fallback_done


async def test_old_esp32_rejected(env, monkeypatch):
    async def old_chip(port):
        return ChipInfo("ESP32-D0WD-V3 (revision v3.1)", "24:0a:c4:00:00:01", "4MB")

    monkeypatch.setattr(device, "identify", old_chip)
    mgr = DeviceManager(DeviceRegistry(), lambda: None, lambda: -1e9)
    mgr._loop = asyncio.get_running_loop()
    env["ports"] = [PORT]
    await mgr._poll()
    assert mgr.state == "unsupported" and "ESP32-S3" in mgr.message
