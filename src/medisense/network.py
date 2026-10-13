"""What the Mac knows about the local network: its own address, the WiFi
network it's on, and the router's 2.4 GHz radio address (BSSID).
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

AIRPORT = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
_MAC_RE = r"[0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5}"


@dataclass
class AccessPoint:
    ssid: str
    bssid: str
    rssi: int
    channel: int


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def normalize_mac(mac: str) -> str:
    """'a:b:c:d:e:f' -> '0a:0b:0c:0d:0e:0f' (macOS drops leading zeros)."""
    return ":".join(p.zfill(2) for p in mac.lower().split(":"))


def local_ip(towards: str = "8.8.8.8") -> str | None:
    """The address other devices on the LAN reach this computer at."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((towards, 9))  # no packet is sent for UDP connect
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


def same_subnet(a: str, b: str, prefix: int = 24) -> bool:
    try:
        return ipaddress.ip_network(f"{a}/{prefix}", strict=False) == ipaddress.ip_network(f"{b}/{prefix}", strict=False)
    except ValueError:
        return False


def wifi_device() -> str | None:
    if sys.platform != "darwin":
        return None
    out = _run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
    return m.group(1) if m else "en0"


def current_ssid() -> str | None:
    if sys.platform == "darwin":
        out = _run([AIRPORT, "-I"])
        m = re.search(r"^\s*SSID: (.+)$", out, re.M)
        if m:
            return m.group(1).strip()
        dev = wifi_device() or "en0"
        out = _run(["networksetup", "-getairportnetwork", dev])
        m = re.search(r"Current Wi-Fi Network: (.+)$", out, re.M)
        return m.group(1).strip() if m else None
    if shutil.which("nmcli"):
        for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
            if line.startswith("yes:"):
                return line[4:]
    return None


def current_link() -> AccessPoint | None:
    """The access point the Mac is connected to right now."""
    if sys.platform != "darwin":
        return None
    out = _run([AIRPORT, "-I"])
    ssid = re.search(r"^\s*SSID: (.+)$", out, re.M)
    bssid = re.search(rf"^\s*BSSID: ({_MAC_RE})", out, re.M)
    ch = re.search(r"^\s*channel: (\d+)", out, re.M)
    rssi = re.search(r"^\s*agrCtlRSSI: (-?\d+)", out, re.M)
    if not (ssid and bssid and ch):
        return None
    return AccessPoint(ssid.group(1).strip(), normalize_mac(bssid.group(1)), int(rssi.group(1)) if rssi else 0, int(ch.group(1)))


def parse_airport_scan(out: str) -> list[AccessPoint]:
    aps = []
    for line in out.splitlines():
        m = re.match(rf"^\s*(.*?)\s+({_MAC_RE})\s+(-?\d+)\s+(\d+)(?:,[+-]?\d+)?\s", line)
        if m:
            aps.append(AccessPoint(m.group(1), normalize_mac(m.group(2)), int(m.group(3)), int(m.group(4))))
    return aps


def scan() -> list[AccessPoint]:
    if sys.platform == "darwin":
        return parse_airport_scan(_run([AIRPORT, "-s"], timeout=15))
    if shutil.which("nmcli"):
        aps = []
        for line in _run(["nmcli", "-t", "-f", "ssid,bssid,signal,chan", "dev", "wifi", "list"]).splitlines():
            parts = re.split(r"(?<!\\):", line)
            if len(parts) >= 9:
                ssid = parts[0].replace("\\:", ":")
                bssid = ":".join(parts[1:7]).replace("\\", "")
                try:
                    aps.append(AccessPoint(ssid, normalize_mac(bssid), int(parts[7]) // 2 - 100, int(parts[8])))
                except ValueError:
                    pass
        return aps
    return []


def router_bssid_24ghz(ssid: str) -> str | None:
    """The 2.4 GHz radio address of the router broadcasting `ssid`.

    The board only ever talks to the 2.4 GHz radio. If the Mac is on 5 GHz
    (different address) we look the 2.4 GHz one up in a scan. Returns None
    when unsure; the board then simply listens to all nearby traffic.
    """
    link = current_link()
    if link and link.ssid == ssid and 1 <= link.channel <= 14:
        return link.bssid
    candidates = [ap for ap in scan() if ap.ssid == ssid and 1 <= ap.channel <= 14]
    if not candidates:
        return None
    # Mesh systems broadcast the same name from several units; pick the one
    # the Mac hears best (the board sits next to the Mac).
    return max(candidates, key=lambda ap: ap.rssi).bssid


def firewall_blocks_incoming() -> bool | None:
    """True if the macOS application firewall is set to block all incoming connections."""
    if sys.platform != "darwin":
        return None
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getblockall"])
    if "DISABLED" in out.upper():
        return False
    if "ENABLED" in out.upper() or "BLOCK ALL" in out.upper():
        return True
    return None


def firewall_enabled() -> bool | None:
    if sys.platform != "darwin":
        return None
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"])
    if "enabled" in out.lower():
        return True
    if "disabled" in out.lower():
        return False
    return None
