"""The firmware images shipped in firmware/ and the board settings (NVS) image."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import FIRMWARE_DIR

NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
EDGE_TIER_RAW = 0  # the mode RuView qualified on the ESP32-S3


@dataclass
class FirmwareImage:
    variant: str  # "esp32s3-8mb" or "esp32s3-4mb"
    path: Path
    flash_size: str
    version: str
    files: list[tuple[int, Path]]  # (offset, file), sorted by offset

    @property
    def app_sha256(self) -> str:
        return sha256(self.path / "esp32-csi-node.bin")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def flash_mb(text: str | None) -> int | None:
    if not text:
        return None
    t = text.upper().replace(" ", "")
    try:
        if t.endswith("MB"):
            return int(float(t[:-2]))
        if t.endswith("KB"):
            return int(float(t[:-2])) // 1024
    except ValueError:
        return None
    return None


def pick_image(detected_flash: str | None) -> FirmwareImage:
    """8 MB image for boards with >= 8 MB flash, otherwise the 4 MB image."""
    mb = flash_mb(detected_flash) or 8
    if mb >= 8:
        return load_image("esp32s3-8mb")
    if mb >= 4:
        return load_image("esp32s3-4mb")
    raise ValueError(f"This board has only {detected_flash} of flash; MediSense needs at least 4 MB.")


def load_image(variant: str) -> FirmwareImage:
    d = FIRMWARE_DIR / variant
    info = json.loads((d / "BUILDINFO.json").read_text())
    args = json.loads((d / "flasher_args.json").read_text())
    files = []
    for off, rel in args["flash_files"].items():
        files.append((int(off, 16), d / Path(rel).name))
    files.sort()
    return FirmwareImage(variant, d, info["flash_size"], info.get("firmware_version", "?"), files)


def verify_image(img: FirmwareImage) -> None:
    """Check every file against SHA256SUMS before it goes anywhere near a board."""
    sums = {}
    for line in (img.path / "SHA256SUMS").read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip("*")] = parts[0]
    for _, f in img.files:
        want = sums.get(f.name)
        if want is None:
            raise ValueError(f"{f.name} is not listed in SHA256SUMS")
        if sha256(f) != want:
            raise ValueError(f"{f.name} does not match its checksum; the download may be damaged")


@dataclass
class BoardSettings:
    ssid: str
    password: str
    target_ip: str
    target_port: int = 5005
    node_id: int = 1
    filter_mac: str | None = None  # router's 2.4 GHz BSSID, aa:bb:cc:dd:ee:ff

    def summary(self) -> dict:
        return {
            "ssid": self.ssid,
            "target_ip": self.target_ip,
            "target_port": self.target_port,
            "node_id": self.node_id,
            "filter_mac": self.filter_mac,
        }


def validate(s: BoardSettings) -> str | None:
    """A human-readable problem with the settings, or None."""
    if not s.ssid or len(s.ssid.encode()) > 32:
        return "The WiFi name must be 1 to 32 characters."
    if s.password and not (8 <= len(s.password.encode()) <= 63):
        return "WiFi passwords are 8 to 63 characters long. Leave it empty only for open networks."
    if s.filter_mac:
        parts = s.filter_mac.split(":")
        if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdefABCDEF" for c in p) for p in parts):
            return "Router address must look like aa:bb:cc:dd:ee:ff."
    return None


def nvs_csv(s: BoardSettings) -> str:
    """CSV for ESP-IDF's NVS generator; keys and types match RuView's nvs_config.c."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", "type", "encoding", "value"])
    w.writerow(["csi_cfg", "namespace", "", ""])
    w.writerow(["ssid", "data", "string", s.ssid])
    w.writerow(["password", "data", "string", s.password])
    w.writerow(["target_ip", "data", "string", s.target_ip])
    w.writerow(["target_port", "data", "u16", str(s.target_port)])
    w.writerow(["node_id", "data", "u8", str(s.node_id)])
    w.writerow(["edge_tier", "data", "u8", str(EDGE_TIER_RAW)])
    if s.filter_mac:
        w.writerow(["filter_mac", "data", "hex2bin", s.filter_mac.replace(":", "").lower()])
    return buf.getvalue()


def build_nvs(s: BoardSettings) -> bytes:
    problem = validate(s)
    if problem:
        raise ValueError(problem)
    with tempfile.TemporaryDirectory(prefix="medisense-nvs-") as td:
        csv_path = Path(td) / "nvs.csv"
        bin_path = Path(td) / "nvs.bin"
        csv_path.write_text(nvs_csv(s), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate", "--outdir", td,
             str(csv_path), str(bin_path), hex(NVS_SIZE)],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or not bin_path.exists():
            raise RuntimeError(f"Could not build the settings image: {r.stderr.strip() or r.stdout.strip()}")
        data = bin_path.read_bytes()
    if len(data) != NVS_SIZE:
        raise RuntimeError(f"Settings image has the wrong size ({len(data)} bytes)")
    return data
