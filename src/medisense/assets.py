"""Webcam posture model (Google MediaPipe Pose, runs inside the browser).

Downloaded once into ~/MediSense/cache and served to the page from there, so
the camera works offline afterwards. Every file is checked against a pinned
hash. Video never leaves the browser; only posture angles reach the app.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import ssl
import tarfile
import urllib.request
from pathlib import Path

from .config import cache_dir

log = logging.getLogger("medisense.assets")

MP_VERSION = "0.10.14"
MP_TARBALL = f"https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-{MP_VERSION}.tgz"
MP_INTEGRITY = "sha512-vOifgZhkndgybdvoRITzRkIueWWSiCKuEUXXK6Q4FaJsFvRJuwgg++vqFUMlL0Uox62U5aEXFhHxlhV7Ja5e3Q=="
MP_FILES = [
    "vision_bundle.mjs",
    "wasm/vision_wasm_internal.js",
    "wasm/vision_wasm_internal.wasm",
    "wasm/vision_wasm_nosimd_internal.js",
    "wasm/vision_wasm_nosimd_internal.wasm",
]
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
MODEL_SHA256 = "59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a"
MODEL_NAME = "pose_landmarker_lite.task"

_lock = asyncio.Lock()


def asset_dir() -> Path:
    d = cache_dir() / f"mediapipe-{MP_VERSION}"
    (d / "wasm").mkdir(parents=True, exist_ok=True)
    return d


def ready() -> bool:
    d = asset_dir()
    return all((d / f).exists() for f in MP_FILES) and (d / MODEL_NAME).exists()


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "MediSense"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
        return r.read()


def fetch_sync() -> None:
    d = asset_dir()
    if not all((d / f).exists() for f in MP_FILES):
        log.info("downloading MediaPipe %s", MP_VERSION)
        blob = _download(MP_TARBALL)
        algo, b64 = MP_INTEGRITY.split("-", 1)
        if base64.b64encode(hashlib.new(algo, blob).digest()).decode() != b64:
            raise RuntimeError("MediaPipe download failed its integrity check")
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            for name in MP_FILES:
                member = tar.getmember(f"package/{name}")
                data = tar.extractfile(member).read()
                (d / name).write_bytes(data)
    if not (d / MODEL_NAME).exists():
        log.info("downloading pose model")
        blob = _download(MODEL_URL)
        if hashlib.sha256(blob).hexdigest() != MODEL_SHA256:
            raise RuntimeError("Pose model download failed its integrity check")
        (d / MODEL_NAME).write_bytes(blob)


async def ensure() -> Path:
    async with _lock:
        if not ready():
            await asyncio.to_thread(fetch_sync)
    return asset_dir()


def resolve(name: str) -> Path | None:
    """Map a requested file name to a cached file (only known files)."""
    if name == MODEL_NAME or name in MP_FILES:
        return asset_dir() / name
    return None
