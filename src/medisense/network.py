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
import time
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


_wifi_dev: tuple[float, str | None] = (-1e9, None)


def wifi_device() -> str | None:
    global _wifi_dev
    if sys.platform != "darwin":
        return None
    if time.monotonic() - _wifi_dev[0] < 300:
        return _wifi_dev[1]
    out = _run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
    _wifi_dev = (time.monotonic(), m.group(1) if m else "en0")
    return _wifi_dev[1]


def parse_ifconfig(out: str) -> list[tuple[str, str]]:
    """(interface, "ip/prefix") for each IPv4 address in macOS `ifconfig` output.

    Point-to-point tunnels (VPNs: "inet 10.8.0.2 --> 10.8.0.1") and loopback
    are left out: nothing on the WiFi can send to those.
    """
    addrs, dev = [], None
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+):\s", line)
        if m:
            dev = m.group(1)
            continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]{8})", line)
        if m and dev and not m.group(1).startswith("127."):
            addrs.append((dev, f"{m.group(1)}/{bin(int(m.group(2), 16)).count('1')}"))
    return addrs


def parse_ip_addr(out: str) -> list[tuple[str, str]]:
    """The same from Linux `ip -4 -o addr show`."""
    addrs = []
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
        if m and not m.group(2).startswith("127.") and " peer " not in line:
            addrs.append((m.group(1), m.group(2)))
    return addrs


def interface_addresses() -> list[tuple[str, str]]:
    if sys.platform == "darwin":
        return parse_ifconfig(_run(["/sbin/ifconfig"]))
    if shutil.which("ip"):
        return parse_ip_addr(_run(["ip", "-4", "-o", "addr", "show"]))
    return []


def pick_lan_ip(addrs: list[tuple[str, str]], peer: str | None, wifi: str | None) -> str | None:
    """The address on the same subnet as `peer`, else the WiFi interface's."""
    if peer:
        try:
            p = ipaddress.ip_address(peer)
        except ValueError:
            p = None
        for _, a in addrs:
            iface = ipaddress.ip_interface(a)
            if p is not None and iface.network.prefixlen < 32 and p in iface.network:
                return str(iface.ip)
    for dev, a in addrs:
        if dev == wifi:
            return a.split("/")[0]
    return None


_lan_cache: dict = {}


def lan_ip(peer: str | None = None, max_age: float = 0.0) -> str | None:
    """This computer's address as seen by a device on the local network.

    Not the route to the internet (`local_ip()`): with a VPN on, that is the
    tunnel's address, and a board sending there never reaches this Mac. With
    `peer` (the board's address) the address on the board's subnet wins, then
    the WiFi interface's, then the default route's.
    """
    hit = _lan_cache.get(peer)
    if max_age and hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    ip = pick_lan_ip(interface_addresses(), peer, wifi_device())
    ip = ip or (local_ip(peer) if peer else None) or local_ip()
    _lan_cache[peer] = (time.monotonic(), ip)
    return ip


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


def firewall_app_blocked(path: str) -> bool | None:
    """True if the firewall blocks incoming connections for this program."""
    if sys.platform != "darwin":
        return None
    out = _run(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getappblocked", path]).lower()
    if "block" in out:
        return True
    if "permit" in out or "allow" in out:
        return False
    return None


def firewall_state(program: str) -> str | None:
    """"block_all", "app_blocked", "on" (can't tell for this program) or None (not in the way)."""
    if firewall_blocks_incoming():
        return "block_all"
    if not firewall_enabled():
        return None
    blocked = firewall_app_blocked(program)
    if blocked:
        return "app_blocked"
    return None if blocked is False else "on"
