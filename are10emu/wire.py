"""are10 wire framing: CRC-16/ARC, COBS, GFDI frames, MultiLink control messages.

MultiLink close commands and the protobuf ACK payload come from Gadgetbridge.
"""

import struct
from dataclasses import dataclass
from typing import List, Optional

# ── GFDI message types ──
MSG_ACK = 5000
MSG_FIT_DEFINITION = 5011
MSG_FIT_DATA = 5012
MSG_DEVICE_INFO = 5024
MSG_PROTOBUF_REQUEST = 5043
MSG_PROTOBUF_RESPONSE = 5044
MSG_CONFIGURATION = 5050

# ACK status codes (WIRE.md §5.4)
STATUS_ACK = 0
STATUS_NAK = 1
STATUS_UNSUPPORTED = 2
STATUS_COBS_ERROR = 3
STATUS_CRC_ERROR = 4
STATUS_LENGTH_ERROR = 5

# ── CRC-16/ARC ──

_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        tmp = ((crc >> 4) & 0x0FFF) ^ _CRC_TABLE[crc & 0xF] ^ _CRC_TABLE[b & 0xF]
        crc = ((tmp >> 4) & 0x0FFF) ^ _CRC_TABLE[tmp & 0xF] ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc


# ── COBS ──

def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    block = bytearray()
    for b in data:
        if b == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(b)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        if code == 0 or i + code > len(data):
            raise ValueError(f"bad COBS code {code} at {i}")
        out += data[i + 1:i + code]
        i += code
        if code < 255 and i < len(data):
            out.append(0)
    return bytes(out)


def wrap_cobs(frame: bytes) -> bytes:
    return b"\x00" + cobs_encode(frame) + b"\x00"


# ── GFDI frames ──

@dataclass
class GfdiFrame:
    msg_type: int
    payload: bytes
    txn_id: Optional[int] = None  # set when the frame used the compressed header


def build_frame(msg_type: int, payload: bytes, txn_id: Optional[int] = None) -> bytes:
    """Build a GFDI frame. With txn_id, uses the compressed 2-byte header."""
    if txn_id is None:
        header = struct.pack("<H", msg_type)
    else:
        header = bytes([msg_type - 5000, (txn_id & 0x7F) | 0x80])
    length = 2 + len(header) + len(payload) + 2
    body = struct.pack("<H", length) + header + payload
    return body + struct.pack("<H", crc16(body))


def build_ack(orig_type: int, status: int = STATUS_ACK, payload: bytes = b"",
              txn_id: Optional[int] = None) -> bytes:
    return build_frame(MSG_ACK, struct.pack("<HB", orig_type, status) + payload, txn_id)


def parse_frame(frame: bytes) -> GfdiFrame:
    if len(frame) < 6:
        raise ValueError(f"frame too short ({len(frame)})")
    crc_recv = struct.unpack_from("<H", frame, len(frame) - 2)[0]
    crc_calc = crc16(frame[:-2])
    if crc_recv != crc_calc:
        raise ValueError(f"CRC mismatch: got {crc_recv:04x}, want {crc_calc:04x}")
    if frame[3] & 0x80:
        return GfdiFrame(frame[2] + 5000, bytes(frame[4:-2]), frame[3] & 0x7F)
    return GfdiFrame(struct.unpack_from("<H", frame, 2)[0], bytes(frame[4:-2]))


class StreamBuffer:
    """Reassembles 0x00-delimited COBS frames from BLE write chunks."""

    MAX = 64 * 1024

    def __init__(self):
        self.buf = bytearray()

    def extend(self, data: bytes):
        if len(self.buf) + len(data) > self.MAX:
            self.buf.clear()
            raise ValueError("stream buffer overflow")
        self.buf += data

    def frames(self):
        """Yield decoded frame bytes (still CRC'd) for each complete COBS frame."""
        while True:
            try:
                start = self.buf.index(0)
                end = self.buf.index(0, start + 1)
            except ValueError:
                return
            encoded = bytes(self.buf[start + 1:end])
            # The closing delimiter may be shared as the next frame's opening one,
            # so keep it in the buffer.
            del self.buf[:end]
            if encoded:
                yield cobs_decode(encoded)


# ── Device information (5024) ──

def _lp_string(s: str) -> bytes:
    raw = s.encode()
    return bytes([len(raw)]) + raw


def build_device_info(protocol_version: int, product_number: int, unit_id: int,
                      software_version: int, max_packet_size: int,
                      friendly_name: str, device_name: str, model_name: str) -> bytes:
    return (struct.pack("<HHIHH", protocol_version, product_number, unit_id,
                        software_version, max_packet_size)
            + _lp_string(friendly_name) + _lp_string(device_name) + _lp_string(model_name))


def capability_bitmap(bits, size: int) -> bytes:
    bitmap = bytearray(size)
    for bit in bits:
        bitmap[bit // 8] |= 1 << (bit % 8)
    return bytes([size]) + bytes(bitmap)


def parse_capabilities(payload: bytes) -> List[int]:
    if not payload:
        return []
    bitmap = payload[1:1 + payload[0]]
    return [i * 8 + b for i, byte in enumerate(bitmap) for b in range(8) if byte & (1 << b)]


# ── Protobuf fragmentation header (5043/5044) ──

@dataclass
class ProtoChunk:
    request_id: int
    offset: int
    total_length: int
    data: bytes


def build_proto_payload(request_id: int, pb: bytes) -> bytes:
    return struct.pack("<HIII", request_id, 0, len(pb), len(pb)) + pb


def parse_proto_payload(payload: bytes) -> ProtoChunk:
    if len(payload) < 14:
        raise ValueError("protobuf payload shorter than 14-byte header")
    request_id, offset, total, chunk = struct.unpack_from("<HIII", payload)
    return ProtoChunk(request_id, offset, total, payload[14:14 + chunk])


def proto_ack_payload(request_id: int, offset: int = 0) -> bytes:
    """ACK body for 5043/5044: request_id, data_offset, chunk KEPT, NO_ERROR."""
    return struct.pack("<HIBB", request_id, offset, 0, 0)


# ── MultiLink control plane (handle 0) ──

ML_REGISTER_REQ = 0
ML_REGISTER_RESP = 1
ML_CLOSE_HANDLE_REQ = 2
ML_CLOSE_HANDLE_RESP = 3
ML_CLOSE_ALL_REQ = 5
ML_CLOSE_ALL_RESP = 6

ML_SERVICE_GFDI = 1

ML_STATUS_SUCCESS = 0
ML_STATUS_INVALID_SERVICE = 1


@dataclass
class MlRequest:
    type: int
    client_id: int
    service: int
    arg: int  # REGISTER: reliable flag; CLOSE_HANDLE: handle


def parse_ml_request(data: bytes) -> Optional[MlRequest]:
    """Parse a 13-byte control message: [0x00][type][client u64][service u16][arg]."""
    if len(data) < 12 or data[0] != 0:
        return None
    mtype = data[1]
    client_id, service = struct.unpack_from("<QH", data, 2)
    arg = data[12] if len(data) > 12 else 0
    return MlRequest(mtype, client_id, service, arg)


def build_register_resp(client_id: int, service: int, status: int, handle: int,
                        last: int = 0) -> bytes:
    # 16 bytes like the real are10: ... status, handle, reliable=0, then `last`.
    return struct.pack("<BBQHBBBB", 0, ML_REGISTER_RESP, client_id, service, status, handle, 0, last)


def build_close_handle_resp(client_id: int, service: int, handle: int, status: int = 0) -> bytes:
    return struct.pack("<BBQHBB", 0, ML_CLOSE_HANDLE_RESP, client_id, service, handle, status)


def build_close_all_resp(client_id: int, service: int = 0, last: int = 1) -> bytes:
    # Real are10: 00 06 <client> 0000 01
    return struct.pack("<BBQHB", 0, ML_CLOSE_ALL_RESP, client_id, service, last)
