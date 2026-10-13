"""Where MediSense keeps things, and small persistent settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

UDP_PORT = 5005
WEB_PORT = 8765
KEYCHAIN_SERVICE = "MediSense WiFi"

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_DIR = Path(os.environ.get("MEDISENSE_FIRMWARE_DIR", REPO_ROOT / "firmware"))
WEB_DIR = Path(__file__).resolve().parent / "web"


def data_dir() -> Path:
    """~/MediSense: recordings and settings. Never inside the app folder."""
    d = Path(os.environ.get("MEDISENSE_HOME", Path.home() / "MediSense"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    d = data_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.replace(tmp, path)


class Settings:
    """Small JSON settings file (~/MediSense/settings.json). No secrets in it."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "settings.json"
        self.data: dict = _read_json(self.path, {})

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        _write_json(self.path, self.data)


class DeviceRegistry:
    """Boards MediSense has set up, keyed by MAC address (~/MediSense/devices.json)."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "devices.json"
        self.data: dict = _read_json(self.path, {})

    def get(self, mac: str) -> dict | None:
        return self.data.get(mac.lower())

    def put(self, mac: str, info: dict) -> None:
        self.data[mac.lower()] = info
        _write_json(self.path, self.data)

    def forget(self, mac: str) -> None:
        self.data.pop(mac.lower(), None)
        _write_json(self.path, self.data)


# ---------------------------------------------------------------- passwords

def _secret_file() -> Path:
    return data_dir() / ".wifi-secrets.json"


def save_wifi_password(ssid: str, password: str) -> None:
    """macOS Keychain on a Mac; a private (0600) file elsewhere."""
    if sys.platform == "darwin":
        r = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", ssid, "-s", KEYCHAIN_SERVICE, "-w", password],
            capture_output=True,
        )
        if r.returncode == 0:
            return
    path = _secret_file()
    data = _read_json(path, {})
    data[ssid] = password
    _write_json(path, data)
    os.chmod(path, 0o600)


def load_wifi_password(ssid: str) -> str | None:
    if sys.platform == "darwin":
        r = subprocess.run(
            ["security", "find-generic-password", "-a", ssid, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return r.stdout.rstrip("\n")
    return _read_json(_secret_file(), {}).get(ssid)
