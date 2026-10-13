"""Small, dependency-free signal processing helpers (numpy only).

SciPy would do all of this, but it is a large download on an old Intel Mac
and MediSense needs only Butterworth band-pass filters and a spectral peak.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def _butter_q(order: int) -> list[float]:
    """Q factors of the 2nd-order sections of a Butterworth filter (even order)."""
    return [1.0 / (2.0 * math.cos(math.pi * (2 * k + 1) / (2 * order))) for k in range(order // 2)]


def _lowpass(fc: float, fs: float, q: float) -> np.ndarray:
    w0 = 2 * math.pi * fc / fs
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
    a = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array([b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]])


def _highpass(fc: float, fs: float, q: float) -> np.ndarray:
    w0 = 2 * math.pi * fc / fs
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
    a = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array([b[0] / a[0], b[1] / a[0], b[2] / a[0], 1.0, a[1] / a[0], a[2] / a[0]])


def butter_bandpass(lo: float, hi: float, fs: float, order: int = 2) -> np.ndarray:
    """Second-order sections (rows of b0 b1 b2 a0 a1 a2) for a band-pass filter.

    `order` is the order of each of the high-pass and low-pass halves.
    """
    sos = [_highpass(lo, fs, q) for q in _butter_q(order)]
    sos += [_lowpass(hi, fs, q) for q in _butter_q(order)]
    return np.vstack(sos)


def sosfilt(sos: np.ndarray, x: np.ndarray, axis: int = 0) -> np.ndarray:
    """Filter along `axis` (vectorised over the other axis). Direct form II transposed."""
    y = np.moveaxis(np.asarray(x, dtype=np.float64), axis, 0).copy()
    for b0, b1, b2, _a0, a1, a2 in sos:
        z1 = np.zeros(y.shape[1:])
        z2 = np.zeros(y.shape[1:])
        for n in range(y.shape[0]):
            xn = y[n]
            yn = b0 * xn + z1
            z1 = b1 * xn - a1 * yn + z2
            z2 = b2 * xn - a2 * yn
            y[n] = yn
    return np.moveaxis(y, 0, axis)


def filtfilt(sos: np.ndarray, x: np.ndarray, axis: int = 0, pad: int | None = None) -> np.ndarray:
    """Zero-phase filtering (forward and backward) with odd-reflection padding."""
    x = np.moveaxis(np.asarray(x, dtype=np.float64), axis, 0)
    n = x.shape[0]
    if pad is None:
        pad = min(n - 1, 3 * 20)
    pad = max(0, min(pad, n - 1))
    if pad:
        head = 2 * x[0] - x[pad:0:-1]
        tail = 2 * x[-1] - x[-2 : -pad - 2 : -1]
        xp = np.concatenate([head, x, tail], axis=0)
    else:
        xp = x
    y = sosfilt(sos, xp)
    y = sosfilt(sos, y[::-1])[::-1]
    if pad:
        y = y[pad:-pad]
    return np.moveaxis(y, 0, axis)


class StreamingSos:
    """Causal filter that keeps state between calls (one channel per column)."""

    def __init__(self, sos: np.ndarray, n_channels: int):
        self.sos = sos
        self.n_channels = n_channels
        self.z = np.zeros((len(sos), 2, n_channels))

    def reset(self, n_channels: int | None = None) -> None:
        if n_channels is not None:
            self.n_channels = n_channels
        self.z = np.zeros((len(self.sos), 2, self.n_channels))

    def step(self, x: np.ndarray) -> np.ndarray:
        """Filter one sample vector of shape (n_channels,)."""
        y = np.asarray(x, dtype=np.float64)
        for i, (b0, b1, b2, _a0, a1, a2) in enumerate(self.sos):
            z1, z2 = self.z[i]
            yn = b0 * y + z1
            self.z[i, 0] = b1 * y - a1 * yn + z2
            self.z[i, 1] = b2 * y - a2 * yn
            y = yn
        return y


@dataclass
class Peak:
    freq: float  # Hz
    power: float
    confidence: float  # 0..1: share of in-band power near the peak


def spectral_peak(
    x: np.ndarray,
    fs: float,
    lo: float,
    hi: float,
    exclude: list[tuple[float, float]] | None = None,
    prefer: float | None = None,
    prefer_width: float = 0.15,
) -> Peak | None:
    """Dominant frequency of `x` within [lo, hi] Hz.

    `exclude` removes frequency ranges (e.g. breathing harmonics when looking
    for the heartbeat). `prefer` gently favours peaks near a previous estimate
    so the reading doesn't jump between two similar peaks.
    """
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n < 16:
        return None
    x = x - x.mean()
    win = np.hanning(n)
    nfft = max(4096, 1 << (n - 1).bit_length())
    spec = np.abs(np.fft.rfft(x * win, nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    band = (freqs >= lo) & (freqs <= hi)
    if exclude:
        for a, b in exclude:
            band &= ~((freqs >= a) & (freqs <= b))
    if not band.any():
        return None
    total = spec[band].sum()
    if total <= 0:
        return None
    score = spec.copy()
    if prefer is not None:
        score = score * (1.0 + 0.5 * np.exp(-0.5 * ((freqs - prefer) / prefer_width) ** 2))
    idx_band = np.flatnonzero(band)
    i = idx_band[np.argmax(score[idx_band])]
    # Parabolic interpolation on the log spectrum for sub-bin accuracy.
    f = freqs[i]
    if 0 < i < len(spec) - 1 and spec[i - 1] > 0 and spec[i + 1] > 0 and spec[i] > 0:
        a, b, c = np.log(spec[i - 1]), np.log(spec[i]), np.log(spec[i + 1])
        denom = a - 2 * b + c
        if denom != 0:
            f = f + 0.5 * (a - c) / denom * (freqs[1] - freqs[0])
    # Confidence: power within +-1.5 resolution bins of the peak vs whole band.
    res = fs / n
    near = band & (np.abs(freqs - freqs[i]) <= 1.5 * res)
    conf = float(spec[near].sum() / total)
    return Peak(freq=float(f), power=float(spec[i]), confidence=min(1.0, conf))


def principal_component(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """First principal component of x (time x channels).

    Returns (signal, weights, explained_fraction).
    """
    xc = x - x.mean(axis=0)
    cov = xc.T @ xc / max(1, len(xc) - 1)
    vals, vecs = np.linalg.eigh(cov)
    w = vecs[:, -1]
    total = vals.sum()
    frac = float(vals[-1] / total) if total > 0 else 0.0
    return xc @ w, w, frac


def find_peaks_simple(x: np.ndarray, min_distance: int, min_prominence: float) -> np.ndarray:
    """Indices of local maxima at least `min_distance` apart with a minimum prominence."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 3:
        return np.array([], dtype=int)
    cand = np.flatnonzero((x[1:-1] > x[:-2]) & (x[1:-1] >= x[2:])) + 1
    keep: list[int] = []
    for i in cand[np.argsort(-x[cand])]:
        if all(abs(i - k) >= min_distance for k in keep):
            lo = max(0, i - min_distance)
            hi = min(len(x), i + min_distance + 1)
            if x[i] - min(x[lo:i + 1].min(), x[i:hi].min()) >= min_prominence:
                keep.append(int(i))
    return np.array(sorted(keep), dtype=int)
