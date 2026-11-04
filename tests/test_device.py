import struct

import pytest

from medisense import firmware
from medisense.device import BoardLog, FlashProgress, explain_esptool_failure, parse_chip_info
from medisense.network import parse_airport_scan


def read_nvs(data: bytes) -> dict:
    """Minimal ESP-IDF NVS reader (page format v2) for checking our images."""
    out, namespaces = {}, {}
    page_size, entry = 4096, 32
    types = {0x01: "u8", 0x02: "u16", 0x04: "u32", 0x21: "str", 0x42: "blob_data", 0x48: "blob_idx"}
    for p in range(0, len(data), page_size):
        page = data[p:p + page_size]
        if page[:4] == b"\xff\xff\xff\xff":
            continue
        bitmap = page[32:64]
        i = 0
        while i < 126:
            state = (bitmap[i // 4] >> ((i % 4) * 2)) & 3
            e = page[64 + i * entry: 64 + (i + 1) * entry]
            if state != 2:  # 2 = written
                i += 1
                continue
            ns, typ, span = e[0], e[1], e[2]
            key = e[8:24].split(b"\0")[0].decode()
            payload = e[24:32]
            t = types.get(typ, hex(typ))
            if ns == 0:
                namespaces[payload[0]] = key
                val = None
            elif t == "u8":
                val = payload[0]
            elif t == "u16":
                val = struct.unpack("<H", payload[:2])[0]
            elif t in ("str", "blob_data"):
                size = struct.unpack("<H", payload[:2])[0]
                raw = page[64 + (i + 1) * entry: 64 + (i + 1) * entry + size]
                val = raw.rstrip(b"\0").decode() if t == "str" else raw
            else:
                val = payload
            if ns:
                out[(ns, key, t)] = val
            i += max(1, span)
    return {(namespaces.get(ns), k, t): v for (ns, k, t), v in out.items()}


def test_nvs_image_has_firmware_keys_and_types():
    s = firmware.BoardSettings(ssid="Home, Sweet \"Home\"", password="p@ss,word;1", target_ip="192.168.1.23",
                               filter_mac="a4:2b:b0:01:02:0f")
    data = firmware.build_nvs(s)
    assert len(data) == firmware.NVS_SIZE
    kv = read_nvs(data)
    assert kv[("csi_cfg", "ssid", "str")] == 'Home, Sweet "Home"'
    assert kv[("csi_cfg", "password", "str")] == "p@ss,word;1"
    assert kv[("csi_cfg", "target_ip", "str")] == "192.168.1.23"
    assert kv[("csi_cfg", "target_port", "u16")] == 5005
    assert kv[("csi_cfg", "node_id", "u8")] == 1
    assert kv[("csi_cfg", "edge_tier", "u8")] == 0
    assert kv[("csi_cfg", "filter_mac", "blob_data")] == bytes.fromhex("a42bb001020f")


def test_nvs_without_filter_or_password():
    data = firmware.build_nvs(firmware.BoardSettings(ssid="Cafe", password="", target_ip="10.0.0.5"))
    kv = read_nvs(data)
    assert kv[("csi_cfg", "password", "str")] == ""
    assert not any(k[1] == "filter_mac" for k in kv)


@pytest.mark.parametrize("ssid,pw,ok", [("x", "short", False), ("", "longenough", False), ("a" * 33, "longenough", False),
                                         ("Net", "longenough", True), ("Net", "", True)])
def test_settings_validation(ssid, pw, ok):
    assert (firmware.validate(firmware.BoardSettings(ssid, pw, "1.2.3.4")) is None) == ok


def test_shipped_firmware_images_verify():
    for variant, size in (("esp32s3-8mb", "8MB"), ("esp32s3-4mb", "4MB")):
        img = firmware.load_image(variant)
        firmware.verify_image(img)
        assert img.flash_size == size
        assert [o for o, _ in img.files] == [0x0, 0x8000, 0xF000, 0x20000]


def test_image_choice_by_flash_size():
    assert firmware.pick_image("16MB").variant == "esp32s3-8mb"
    assert firmware.pick_image("8MB").variant == "esp32s3-8mb"
    assert firmware.pick_image("4MB").variant == "esp32s3-4mb"
    with pytest.raises(ValueError):
        firmware.pick_image("2MB")


ESPTOOL_OUT = """esptool.py v4.8.1
Serial port /dev/cu.usbmodem14101
Connecting...
Detecting chip type... ESP32-S3
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: WiFi, BLE, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: f4:12:fa:4b:1c:08
Uploading stub...
Detected flash size: 16MB
Hard resetting via RTS pin...
"""


def test_parse_chip_info():
    c = parse_chip_info(ESPTOOL_OUT)
    assert c.is_s3 and c.mac == "f4:12:fa:4b:1c:08" and c.flash_size == "16MB"
    old = parse_chip_info(ESPTOOL_OUT.replace("ESP32-S3 (QFN56)", "ESP32-D0WD-V3").replace("ESP32-S3", "ESP32"))
    assert not old.is_s3


def test_esptool_failures_explained():
    assert "BOOT" in explain_esptool_failure("A fatal error occurred: Failed to connect to ESP32-S3: No serial data received.")
    assert "in use" in explain_esptool_failure("could not open port: [Errno 16] Resource busy")


def test_flash_progress():
    fp = FlashProgress([(0x0, 0x5000), (0x9000, 0x6000), (0x20000, 0x10000)])
    assert fp.percent("Writing at 0x00000000... (0 %)") == 0
    assert fp.percent("Writing at 0x0000c000... (50 %)") == pytest.approx(100 * (0x5000 + 0x3000) / 0x1B000)
    assert fp.percent("Writing at 0x00030000... (100 %)") == 100
    assert fp.percent("Compressed 1234 bytes") is None


BOOT_LOG = """\x1b[0;32mI (312) main: ESP32-S3 CSI Node (ADR-018 / ADR-110) — v0.8.8 — Node ID: 1\x1b[0m
I (320) nvs_config: NVS override: target_ip=192.168.1.23
I (321) nvs_config: NVS override: filter_mac=a4:2b:b0:01:02:0f
I (500) main: WiFi STA initialized, connecting to SSID: Home
W (2600) main: WiFi disconnected, reason=15 rssi=-40
I (2601) main: Retrying WiFi connection (1/10)
I (4000) main: Got IP: 192.168.1.77
I (4100) main: CSI streaming active → 192.168.1.23:5005 (edge_tier=0, OTA=on)
I (4200) csi_collector: CSI cb #100: len=384 rssi=-41 ch=6
"""


def test_board_log_happy_path():
    b = BoardLog()
    for line in BOOT_LOG.splitlines():
        b.feed(line)
    assert b.firmware_version == "0.8.8"
    assert b.target_ip_nvs == "192.168.1.23" and b.filter_mac_nvs == "a4:2b:b0:01:02:0f"
    assert b.wifi == "connected" and b.board_ip == "192.168.1.77"
    assert b.streaming and b.target == "192.168.1.23:5005"
    assert b.csi_callbacks == 100
    assert b.wifi_problem() is None


def test_board_log_wrong_password():
    b = BoardLog()
    b.feed("I (312) main: ESP32-S3 CSI Node (ADR-018 / ADR-110) - v0.8.8 - Node ID: 1")
    b.feed("I (500) main: WiFi STA initialized, connecting to SSID: Home")
    for _ in range(4):
        b.feed("W (2600) main: WiFi disconnected, reason=15 rssi=-40")
    assert b.wrong_password
    assert "password" in b.wifi_problem()
    b.feed("E (9000) main: Failed to connect to WiFi after 10 retries")
    assert b.wifi == "failed"


def test_airport_scan_parsing():
    out = """                            SSID BSSID             RSSI CHANNEL HT CC SECURITY (auth/unicast/group)
                       Home Net a4:2b:b0:1:2:f    -48  6       Y  -- WPA2(PSK/AES/AES)
                       Home Net a4:2b:b0:1:2:10   -55  149,+1  Y  -- WPA2(PSK/AES/AES)
                    Neighbour 00:11:22:33:44:55 -80  11      Y  -- WPA2(PSK/AES/AES)
"""
    aps = parse_airport_scan(out)
    assert [(a.ssid, a.bssid, a.channel) for a in aps] == [
        ("Home Net", "a4:2b:b0:01:02:0f", 6), ("Home Net", "a4:2b:b0:01:02:10", 149), ("Neighbour", "00:11:22:33:44:55", 11)]


# ------------------------------------------------------------ firmware update

@pytest.mark.parametrize("saved_password", [True, False])
async def test_known_board_with_old_firmware_is_updated(tmp_path, monkeypatch, saved_password):
    """A board set up with an older MediSense firmware is updated in place
    (same WiFi settings) when its WiFi password is in the Keychain."""
    from medisense import device
    from medisense.config import DeviceRegistry

    reg = DeviceRegistry(tmp_path / "devices.json")
    mac = "aa:bb:cc:dd:ee:ff"
    reg.put(mac, {"firmware_sha": "old", "settings": {"ssid": "Home", "target_ip": "192.168.1.20",
                                                    "target_port": 5005, "node_id": 1, "filter_mac": None}})
    chip = device.ChipInfo("ESP32-S3 (QFN56)", mac, "8MB")

    async def fake_identify(port):
        return chip

    monkeypatch.setattr(device, "identify", fake_identify)
    monkeypatch.setattr(device, "load_wifi_password", lambda ssid: "secret123" if saved_password else None)
    monkeypatch.setattr(device.network, "lan_ip", lambda *a, **k: "192.168.1.21")
    monkeypatch.setattr(device.network, "current_ssid", lambda: "Home")
    calls = []

    async def fake_provision(self, settings, full):
        calls.append((settings, full))

    monkeypatch.setattr(device.DeviceManager, "_provision", fake_provision)
    dm = device.DeviceManager(reg, lambda: None, lambda: -1e9)
    await dm._plugged(device.UsbPort("/dev/cu.usbmodem1", 0x303A, 0x1001, "USB JTAG", "x"))
    if saved_password:
        assert len(calls) == 1 and calls[0][1] is True
        s = calls[0][0]
        assert s.ssid == "Home" and s.password == "secret123" and s.target_ip == "192.168.1.21"
    else:
        assert not calls and dm.state == "needs_setup" and "update" in dm.message


def test_board_log_medisense_stats(monkeypatch):
    from medisense import device

    clock = iter([100.0, 110.0])
    monkeypatch.setattr(device.time, "monotonic", lambda: next(clock))
    b = BoardLog()
    b.feed("I (312) main: ESP32-S3 CSI Node (ADR-018 / ADR-110) - v0.8.8 - Node ID: 1")
    b.feed("I (5000) csi_collector: MediSense firmware 3: frames from the router, MGMT+DATA capture")
    assert b.medisense_fw == "3" and b.hears() is None and not b.sending
    b.feed("I (15000) csi_collector: medisense stats: heard=1200 used=400 to_board=390 other=10 foreign=800 rate_drop=5 any_sender=0")
    assert b.hears()["used"] == 400 and b.hears()["heard_per_s"] is None and b.sending
    b.feed("I (25000) csi_collector: medisense stats: heard=2400 used=900 to_board=880 other=20 foreign=1500 rate_drop=9 any_sender=1")
    h = b.hears()
    assert h["heard_per_s"] == 120.0 and h["used_per_s"] == 50.0 and h["any_sender"]
    b.feed("I (312) main: ESP32-S3 CSI Node (ADR-018 / ADR-110) - v0.8.8 - Node ID: 1")  # rebooted
    assert b.hears() is None and b.medisense_fw is None


def test_lan_address_is_the_one_the_board_can_reach():
    """With a VPN on, the route to the internet leads into the tunnel; the
    board must be given the Mac's address on its own network."""
    from medisense.network import parse_ifconfig, parse_ip_addr, pick_lan_ip

    ifconfig = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 8c:85:90:aa:bb:cc
\tinet 192.168.1.5 netmask 0xffffff00 broadcast 192.168.1.255
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.0.0.7 netmask 0xffffff00 broadcast 10.0.0.255
utun2: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffffff
""".replace("\\t", "\t")
    addrs = parse_ifconfig(ifconfig)
    assert addrs == [("en0", "192.168.1.5/24"), ("en5", "10.0.0.7/24")]
    assert pick_lan_ip(addrs, "192.168.1.7", "en0") == "192.168.1.5"
    assert pick_lan_ip(addrs, "10.0.0.9", "en0") == "10.0.0.7"
    assert pick_lan_ip(addrs, None, "en0") == "192.168.1.5"
    assert pick_lan_ip(addrs, "172.16.0.9", "en9") is None
    linux = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
3: wlan0    inet 192.168.1.5/24 brd 192.168.1.255 scope global dynamic wlan0\\       valid_lft 85000sec
7: tun0    inet 10.8.0.2 peer 10.8.0.1/32 scope global tun0\\       valid_lft forever
"""
    assert parse_ip_addr(linux) == [("wlan0", "192.168.1.5/24")]
