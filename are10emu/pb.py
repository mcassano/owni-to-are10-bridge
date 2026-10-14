"""Minimal proto2 codec for the are10's GDI Smart messages.

Field numbers come from the published .proto definitions. Only the handful of
messages the are10 exchanges are implemented, so there's no protoc dependency.
"""

import struct
from typing import Dict, List, Optional, Tuple

# Smart container extension fields
SMART_EVENT_SHARING = 30
SMART_LAUNCH_MONITOR = 38

ALERT_LAUNCH_MONITOR = 8

# LaunchMonitor State.StateType
STATE_STANDBY = 0
STATE_INTERFERENCE_TEST = 1
STATE_WAITING = 2
STATE_RECORDING = 3
STATE_PROCESSING = 4
STATE_ERROR = 5

# LaunchMonitor.Service field numbers (requests are odd, responses even)
LM_STATUS = 1
LM_WAKE_UP = 3
LM_TILT = 5
LM_START_TILT_CAL = 7
LM_RESET_TILT_CAL = 9
LM_SHOT_CONFIG = 11

WAKE_UP_SUCCESS = 0
WAKE_UP_ALREADY_AWAKE = 1

# ── Encoding ──

WT_VARINT, WT_I64, WT_LEN, WT_I32 = 0, 1, 2, 5


def _varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wt: int) -> bytes:
    return _varint((field << 3) | wt)


def f_varint(field: int, value: int) -> bytes:
    return _key(field, WT_VARINT) + _varint(int(value))


def f_float(field: int, value: float) -> bytes:
    return _key(field, WT_I32) + struct.pack("<f", value)


def f_msg(field: int, body: bytes) -> bytes:
    return _key(field, WT_LEN) + _varint(len(body)) + body


# ── Decoding ──

Fields = Dict[int, List[Tuple[int, object]]]


def decode(data: bytes) -> Fields:
    """Decode one message level into {field: [(wire_type, value), ...]}."""
    fields: Fields = {}
    i = 0

    def read_varint():
        nonlocal i
        shift = result = 0
        while True:
            if i >= len(data):
                raise ValueError("truncated varint")
            b = data[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while i < len(data):
        key = read_varint()
        field, wt = key >> 3, key & 7
        if wt == WT_VARINT:
            value = read_varint()
        elif wt == WT_I64:
            value = data[i:i + 8]
            i += 8
        elif wt == WT_LEN:
            n = read_varint()
            value = data[i:i + n]
            i += n
        elif wt == WT_I32:
            value = data[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        fields.setdefault(field, []).append((wt, value))
    return fields


def get_msg(fields: Fields, field: int) -> Optional[Fields]:
    """Sub-message at `field` (None if absent). Empty messages decode to {}."""
    for wt, v in fields.get(field, []):
        if wt == WT_LEN:
            return decode(v)
    return None


def get_varint(fields: Fields, field: int) -> Optional[int]:
    for wt, v in fields.get(field, []):
        if wt == WT_VARINT:
            return v
    return None


def get_float(fields: Fields, field: int) -> Optional[float]:
    for wt, v in fields.get(field, []):
        if wt == WT_I32:
            return struct.unpack("<f", v)[0]
    return None


def get_repeated_varint(fields: Fields, field: int) -> List[int]:
    """Repeated enum/int field, accepting both packed and unpacked encodings."""
    out = []
    for wt, v in fields.get(field, []):
        if wt == WT_VARINT:
            out.append(v)
        elif wt == WT_LEN:
            out.extend(_decode_packed(v))
    return out


def _decode_packed(data: bytes) -> List[int]:
    out, shift, cur = [], 0, 0
    for b in data:
        cur |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
        else:
            out.append(cur)
            shift = cur = 0
    return out


# ── Device → host builders ──

def smart_event_sharing(body: bytes) -> bytes:
    return f_msg(SMART_EVENT_SHARING, body)


def smart_launch_monitor(body: bytes) -> bytes:
    return f_msg(SMART_LAUNCH_MONITOR, body)


def subscribe_response(alert_types: List[int]) -> bytes:
    statuses = b"".join(
        f_msg(1, f_varint(1, 0) + f_msg(2, f_varint(1, t)))  # SUCCESS + echoed AlertMessage
        for t in alert_types)
    return smart_event_sharing(f_msg(2, statuses))


def alert_support_response(alert_types: List[int]) -> bytes:
    return smart_event_sharing(f_msg(5, b"".join(f_varint(1, t) for t in alert_types)))


def lm_response(request_field: int, body: bytes) -> bytes:
    """Wrap a LaunchMonitor response body at the field after its request."""
    return smart_launch_monitor(f_msg(request_field + 1, body))


def state_msg(state: int) -> bytes:
    return f_varint(1, state)


def tilt_msg(roll: float, pitch: float) -> bytes:
    return f_float(1, roll) + f_float(2, pitch)


def alert_notification(details: bytes) -> bytes:
    """AlertNotification{alert_type: [LAUNCH_MONITOR] (packed, as the are10 sends it),
    details(1001): AlertDetails}."""
    return smart_event_sharing(f_msg(3, f_msg(1, _varint(ALERT_LAUNCH_MONITOR)) + f_msg(1001, details)))


def state_alert(state: int) -> bytes:
    return alert_notification(f_msg(1, state_msg(state)))


def metrics_alert(shot) -> bytes:
    """Shot-data alert from a shots.Shot."""
    ball = (f_float(1, shot.launch_angle) + f_float(2, shot.launch_direction)
            + f_float(3, shot.ball_speed) + f_float(4, shot.spin_axis)
            + f_float(5, shot.total_spin) + f_varint(6, shot.spin_calc_type)
            + f_varint(7, shot.golf_ball_type))
    metrics = f_varint(1, shot.shot_id) + f_varint(2, 1) + f_msg(3, ball)  # shot_type NORMAL
    if shot.club_speed is not None:
        club = (f_float(1, shot.club_speed) + f_float(2, shot.face_angle or 0.0)
                + f_float(3, shot.club_path or 0.0) + f_float(4, shot.attack_angle or 0.0))
        metrics += f_msg(4, club)
    # SwingMetrics (tempo timestamps) are omitted: the Owni doesn't measure them.
    return alert_notification(f_msg(2, metrics))
