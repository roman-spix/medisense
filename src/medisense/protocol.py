"""Decoding of the UDP packets the board sends.

Every packet starts with a 4-byte little-endian magic number.

Raw CSI frame (RuView ADR-018, see firmware/esp32-csi-node/main/csi_collector.c):

    [0..3]   magic 0xC5110001
    [4]      node id
    [5]      number of antennas
    [6..7]   number of subcarriers (u16)
    [8..11]  frequency in MHz (u32)
    [12..15] sequence number (u32)
    [16]     RSSI (i8)
    [17]     noise floor (i8)
    [18]     PPDU type
    [19]     flags: bit0 40 MHz, bit2 STBC, bit4 cross-node sync valid,
             MediSense firmware adds: bit5 frame was addressed to the board,
             bit6 first 4 CSI bytes invalid, bit7 a 4-byte receive timestamp
             (microseconds, board clock) follows the CSI bytes
    [20..]   CSI bytes from ESP-IDF: int8 pairs, imaginary first, in blocks
             of 64 subcarriers: L-LTF (all frames), then HT-LTF (802.11n
             frames; with ltf_merge_en it is the average of L-LTF and HT-LTF,
             the cleanest estimate), then STBC HT-LTF2 when STBC is used.

MediSense sensor packets (MediSense firmware only):

    radar  0xC511D0A1  raw bytes from a 60 GHz radar on UART1
    ppg    0xC511D0B1  red/IR samples from a MAX3010x pulse sensor

Other RuView packet kinds (time sync, edge vitals) are ignored.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

CSI_MAGIC = 0xC5110001
SYNC_MAGIC = 0xC511A110
RADAR_MAGIC = 0xC511D0A1
PPG_MAGIC = 0xC511D0B1
HEADER_SIZE = 20

BLOCK = 64  # subcarriers per LTF block (HT20)

FLAG_TO_ME = 0x20
FLAG_FIRST_WORD_INVALID = 0x40
FLAG_TIMESTAMP = 0x80

# Index sets inside a 64-entry block (ESP ordering: subcarriers 0..31 then
# -32..-1), listed in increasing subcarrier frequency for phase unwrapping.
# L-LTF carries subcarriers -26..-1, 1..26; HT-LTF (HT20) -28..-1, 1..28.
LLTF_INDEX = np.array(list(range(38, 64)) + list(range(1, 27)))
LLTF_SUBCARRIER = np.array(list(range(-26, 0)) + list(range(1, 27)))
HTLTF_INDEX = np.array(list(range(36, 64)) + list(range(1, 29)))
HTLTF_SUBCARRIER = np.array(list(range(-28, 0)) + list(range(1, 29)))

# Kept for older callers: the L-LTF block is always first.
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
    ppdu: int = 0
    flags: int = 0
    dev_ts_us: int | None = None  # board receive time (MediSense firmware)

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

    @property
    def n_blocks(self) -> int:
        return len(self.iq) // (2 * BLOCK)

    @property
    def frame_class(self) -> str:
        """'L' legacy (L-LTF only), 'H' 802.11n, 'S' 802.11n with STBC, 'X…' other."""
        n = len(self.iq)
        return {128: "L", 256: "H", 384: "S"}.get(n, f"X{n}")

    @property
    def to_me(self) -> bool | None:
        """True/False with MediSense firmware, None when the firmware doesn't say."""
        if not self.flags & FLAG_TIMESTAMP:
            return None
        return bool(self.flags & FLAG_TO_ME)

    def block(self, b: int) -> np.ndarray:
        """Complex CSI of one 64-subcarrier block (0 = L-LTF, 1 = HT-LTF, ...)."""
        raw = np.frombuffer(self.iq, dtype=np.int8, count=2 * BLOCK, offset=2 * BLOCK * b)
        pairs = raw.astype(np.float32).reshape(BLOCK, 2)
        return pairs[:, 1] + 1j * pairs[:, 0]

    def best_block(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(complex values, subcarrier numbers) of the cleanest estimate in this frame.

        802.11n frames: the HT-LTF block (merged with L-LTF by the radio).
        Legacy frames: the L-LTF block, minus subcarrier 1 when the radio
        flags the first CSI word as invalid.
        """
        if self.n_blocks >= 2:
            h = self.block(1)
            return h[HTLTF_INDEX], HTLTF_SUBCARRIER, HTLTF_INDEX
        h = self.block(0)
        idx, sub = LLTF_INDEX, LLTF_SUBCARRIER
        if self.flags & FLAG_FIRST_WORD_INVALID:
            keep = idx != 1
            idx, sub = idx[keep], sub[keep]
        return h[idx], sub, idx

    def amplitudes(self, max_subcarriers: int = LLTF_SUBCARRIERS) -> np.ndarray:
        """Amplitude per subcarrier of the first `max_subcarriers` entries (L-LTF)."""
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
    (magic, node_id, n_ant, n_sub, freq, seq, rssi, noise, ppdu, flags) = _HEADER.unpack_from(data, 0)
    if magic != CSI_MAGIC:
        return None
    dev_ts = None
    end = len(data)
    if flags & FLAG_TIMESTAMP and end >= HEADER_SIZE + 4 + 2:
        dev_ts = struct.unpack_from("<I", data, end - 4)[0]
        end -= 4
    iq = bytes(data[HEADER_SIZE:end])
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
        ppdu=ppdu,
        flags=flags,
        dev_ts_us=dev_ts,
    )


def build_csi(
    iq: bytes,
    seq: int,
    rssi: int = -45,
    noise_floor: int = -92,
    node_id: int = 1,
    channel: int = 6,
    flags: int = 0,
    dev_ts_us: int | None = None,
) -> bytes:
    """Build a packet exactly like the firmware does (used by the simulator and tests)."""
    freq = 2412 + (channel - 1) * 5
    n_sub = len(iq) // 2
    if dev_ts_us is not None:
        flags |= FLAG_TIMESTAMP
    header = _HEADER.pack(CSI_MAGIC, node_id, 1, n_sub, freq, seq & 0xFFFFFFFF, rssi, noise_floor, 0, flags)
    tail = struct.pack("<I", dev_ts_us & 0xFFFFFFFF) if dev_ts_us is not None else b""
    return header + iq + tail


# ------------------------------------------------------------ sensor packets

_SENSOR_HEADER = struct.Struct("<IBBHII")  # magic, node, kind, len/n, seq, timestamp


@dataclass(frozen=True)
class RadarChunk:
    node_id: int
    radar_type: int  # 1 = TinyFrame (MR60BHA2 / HLK-LD6002), 2 = HLK-LD2410
    seq: int
    dev_ts_us: int
    baud: int
    data: bytes


@dataclass(frozen=True)
class PpgBatch:
    node_id: int
    sensor_type: int  # 1 = MAX3010x
    seq: int
    dev_ts_us: int  # time of the first sample
    rate_hz: int
    overflow: bool
    red: np.ndarray
    ir: np.ndarray


def parse_radar(data: bytes) -> RadarChunk | None:
    """[magic][node][type][len u16][seq u32][ts u32][baud u32][bytes...]"""
    if len(data) < 20 or packet_kind(data) != RADAR_MAGIC:
        return None
    _, node, kind, n, seq, ts = _SENSOR_HEADER.unpack_from(data, 0)
    baud = struct.unpack_from("<I", data, 16)[0]
    payload = bytes(data[20 : 20 + n])
    if len(payload) != n:
        return None
    return RadarChunk(node, kind, seq, ts, baud, payload)


def build_radar(payload: bytes, seq: int, dev_ts_us: int, node_id: int = 1, radar_type: int = 1,
                baud: int = 1_382_400) -> bytes:
    return _SENSOR_HEADER.pack(RADAR_MAGIC, node_id, radar_type, len(payload), seq & 0xFFFFFFFF,
                               dev_ts_us & 0xFFFFFFFF) + struct.pack("<I", baud) + payload


def parse_ppg(data: bytes) -> PpgBatch | None:
    """[magic][node][type][n u8][channels u8][seq u32][ts u32][rate u16][flags u16][n*channels u32]"""
    if len(data) < 20 or packet_kind(data) != PPG_MAGIC:
        return None
    magic, node, kind, n, ch, seq, ts, rate, flags = struct.unpack_from("<IBBBBIIHH", data, 0)
    need = 20 + 4 * n * ch
    if ch < 2 or len(data) < need or n == 0:
        return None
    vals = np.frombuffer(data, dtype="<u4", count=n * ch, offset=20).reshape(n, ch)
    return PpgBatch(node, kind, seq, ts, rate or 100, bool(flags & 1),
                    vals[:, 0].astype(np.float64), vals[:, 1].astype(np.float64))


def build_ppg(red, ir, seq: int, dev_ts_us: int, node_id: int = 1, rate_hz: int = 100,
              overflow: bool = False) -> bytes:
    n = len(red)
    vals = np.empty((n, 2), dtype="<u4")
    vals[:, 0] = np.asarray(red, dtype=np.uint32)
    vals[:, 1] = np.asarray(ir, dtype=np.uint32)
    head = struct.pack("<IBBBBIIHH", PPG_MAGIC, node_id, 1, n, 2, seq & 0xFFFFFFFF,
                       dev_ts_us & 0xFFFFFFFF, rate_hz, 1 if overflow else 0)
    return head + vals.tobytes()
