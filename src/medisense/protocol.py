"""Decoding of the UDP packets RuView's esp32-csi-node firmware sends.

Every packet starts with a 4-byte little-endian magic number. MediSense runs
the board in raw mode (edge_tier 0), so almost everything that arrives is an
ADR-018 raw CSI frame (see RuView firmware/esp32-csi-node/main/csi_collector.c):

    [0..3]   magic 0xC5110001
    [4]      node id
    [5]      number of antennas
    [6..7]   number of subcarriers (u16)
    [8..11]  frequency in MHz (u32)
    [12..15] sequence number (u32)
    [16]     RSSI (i8)
    [17]     noise floor (i8)
    [18..19] PPDU type / flags (zero unless HE tagging is compiled in)
    [20..]   CSI bytes as delivered by ESP-IDF: int8 pairs, imaginary first

Other packet kinds (the ~0.5 Hz time-sync packet, vitals packets if edge
processing is ever switched on) are recognised and ignored.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

CSI_MAGIC = 0xC5110001
SYNC_MAGIC = 0xC511A110
VITALS_MAGIC = 0xC5110002
HEADER_SIZE = 20

# The legacy long training field (LLTF) is always first in the CSI buffer and
# is present for every frame type (beacons, HT data, ...): 64 subcarriers.
LLTF_SUBCARRIERS = 64

_HEADER = struct.Struct("<IBBHIIbbBB")


@dataclass(frozen=True)
class CsiFrame:
    node_id: int
    n_antennas: int
    n_subcarriers: int
    freq_mhz: int
    seq: int
    rssi: int
    noise_floor: int
    iq: bytes  # raw int8 pairs (imag, real)

    @property
    def channel(self) -> int:
        f = self.freq_mhz
        if f == 2484:
            return 14
        if 2412 <= f <= 2472:
            return (f - 2412) // 5 + 1
        if f >= 5000:
            return (f - 5000) // 5
        return 0

    def amplitudes(self, max_subcarriers: int = LLTF_SUBCARRIERS) -> np.ndarray:
        """Amplitude per subcarrier for the first `max_subcarriers` subcarriers."""
        raw = np.frombuffer(self.iq, dtype=np.int8)
        n = min(len(raw) // 2, max_subcarriers)
        pairs = raw[: 2 * n].astype(np.float32).reshape(n, 2)
        return np.hypot(pairs[:, 0], pairs[:, 1])


def packet_kind(data: bytes) -> int | None:
    if len(data) < 4:
        return None
    return struct.unpack_from("<I", data, 0)[0]


def parse_csi(data: bytes) -> CsiFrame | None:
    """Parse one raw CSI packet. Returns None for anything that isn't one."""
    if len(data) < HEADER_SIZE + 2:
        return None
    (magic, node_id, n_ant, n_sub, freq, seq, rssi, noise, _ppdu, _flags) = _HEADER.unpack_from(data, 0)
    if magic != CSI_MAGIC:
        return None
    iq = bytes(data[HEADER_SIZE:])
    if len(iq) % 2:
        iq = iq[:-1]
    return CsiFrame(
        node_id=node_id,
        n_antennas=max(1, n_ant),
        n_subcarriers=n_sub,
        freq_mhz=freq,
        seq=seq,
        rssi=rssi,
        noise_floor=noise,
        iq=iq,
    )


def build_csi(
    iq: bytes,
    seq: int,
    rssi: int = -45,
    noise_floor: int = -92,
    node_id: int = 1,
    channel: int = 6,
) -> bytes:
    """Build a packet exactly like the firmware does (used by the simulator and tests)."""
    freq = 2412 + (channel - 1) * 5
    n_sub = len(iq) // 2
    header = _HEADER.pack(CSI_MAGIC, node_id, 1, n_sub, freq, seq & 0xFFFFFFFF, rssi, noise_floor, 0, 0)
    return header + iq
