"""Owni BLE protocol: command builders and notification parsers.

Verified against hardware 3.1 / launch monitor firmware 1.5.8.
"""

import struct
from dataclasses import asdict, dataclass
from typing import Optional

BASE = "8660{:04x}-6b7e-439a-bdd1-489a3213e9bb"
SERVICE_UUID = BASE.format(0x1001)
DEVICE_ID_UUID = BASE.format(0x2001)
HW_VERSION_UUID = BASE.format(0x2002)
FW_VERSION_UUID = BASE.format(0x2003)
CMD_UUID = BASE.format(0x2101)   # write with response
EVT_UUID = BASE.format(0x2102)   # notify

GAP_NAME_PREFIX = "SGO"
MODEL_CODE_PREFIX = b"0300"
MPS_TO_MPH = 2.236936

# Commands
CMD_DETECT_BALL = 0x81
CMD_CLUB_SELECT = 0x82
CMD_HEARTBEAT = 0x83
CMD_QUERY = 0x86
CMD_REQUEST_CLUB = 0x87

SPIN_STANDARD, SPIN_ADVANCED = 0x10, 0x11

# Club codes: (club_number, category). Starred entries in WIRE.md §8 are inferred.
CLUBS = {
    "driver": (0x01, 0x00), "3wood": (0x03, 0x01), "5wood": (0x05, 0x01),
    "3hybrid": (0x0D, 0x01), "4hybrid": (0x0E, 0x01), "5hybrid": (0x0F, 0x01),
    "4iron": (0x04, 0x02), "5iron": (0x05, 0x02), "6iron": (0x06, 0x02),
    "7iron": (0x07, 0x02), "8iron": (0x08, 0x02), "9iron": (0x09, 0x02),
    "pw": (0x0A, 0x02), "gw": (0x0B, 0x02), "sw": (0x0C, 0x02), "lw": (0x0D, 0x02),
    "putter": (0x01, 0x03),
}

_CLUB_ALIASES = {"d": "driver", "dr": "driver", "1w": "driver", "aw": "gw", "putt": "putter",
                 "pt": "putter", "p": "pw"}
_CLUB_SUFFIXES = (("iron", "iron"), ("i", "iron"), ("wood", "wood"), ("w", "wood"),
                  ("hybrid", "hybrid"), ("hy", "hybrid"), ("h", "hybrid"))


def club_name(text: str) -> Optional[str]:
    """Canonical club key for user input like "7", "7i", "7-iron", "3w", "pw"; None if unknown."""
    t = text.strip().lower().replace("-", "").replace(" ", "")
    t = _CLUB_ALIASES.get(t, t)
    if t in CLUBS:
        return t
    if t.isdigit():
        t += "iron"
    for suffix, kind in _CLUB_SUFFIXES:
        if t.endswith(suffix) and t[:-len(suffix)].isdigit():
            key = t[:-len(suffix)] + kind
            return key if key in CLUBS else None
    return None


DEVICE_STATES = {0: "none", 1: "idle", 2: "initialising", 3: "detecting",
                 4: "ready", 5: "shot", 6: "done"}


def command(cmd_type: int, seq: int, *payload: int) -> bytes:
    """[0x00][type][seq][payload…], zero-padded to 9 bytes."""
    body = bytes([0x00, cmd_type, seq & 0xFF, *payload])
    return body.ljust(9, b"\x00")


def detect_ball(seq: int, on: bool = True, spin: int = SPIN_ADVANCED) -> bytes:
    return command(CMD_DETECT_BALL, seq, 0x01 if on else 0x00, spin)


def club_select(seq: int, club: str, left_handed: bool = False) -> bytes:
    number, category = CLUBS[club]
    return command(CMD_CLUB_SELECT, seq, number, category, 0x01 if left_handed else 0x00)


def heartbeat(seq: int) -> bytes:
    return command(CMD_HEARTBEAT, seq)


def query(seq: int) -> bytes:
    return command(CMD_QUERY, seq)


def request_club(seq: int) -> bytes:
    return command(CMD_REQUEST_CLUB, seq)


# ── Notifications ──

def _i16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


@dataclass
class BallMetrics:
    ball_speed_mps: float
    launch_angle: float      # degrees, vertical
    direction: float         # degrees, positive = right
    total_spin: int          # rpm
    spin_axis_raw: float     # degrees, Owni convention: NEGATIVE curves right
    backspin: int            # rpm
    sidespin_raw: int        # rpm, Owni convention: NEGATIVE curves right
    shot_type: int

    @property
    def ball_speed_mph(self) -> float:
        return self.ball_speed_mps * MPS_TO_MPH

    @property
    def spin_axis(self) -> float:
        """Common/GSPro convention: positive = fade (curves right)."""
        return -self.spin_axis_raw + 0.0  # + 0.0 turns -0.0 into 0.0

    @property
    def sidespin(self) -> int:
        """Common/GSPro convention: positive curves right."""
        return -self.sidespin_raw + 0


CLUB_FIELDS = ("path", "face", "attack_angle", "dynamic_loft", "impact_h_mm", "impact_v_mm",
               "club_speed_mps", "smash_factor")


@dataclass
class ClubMetrics:
    mask: int
    path: Optional[float] = None
    face: Optional[float] = None
    attack_angle: Optional[float] = None
    dynamic_loft: Optional[float] = None
    impact_h_mm: Optional[float] = None
    impact_v_mm: Optional[float] = None
    club_speed_mps: Optional[float] = None
    smash_factor: Optional[float] = None

    @property
    def club_speed_mph(self) -> Optional[float]:
        return None if self.club_speed_mps is None else self.club_speed_mps * MPS_TO_MPH


@dataclass
class Sensor:
    ready: bool
    detected: bool
    position: tuple


@dataclass
class HeartbeatAck:
    state: int

    @property
    def state_name(self) -> str:
        return DEVICE_STATES.get(self.state, f"unknown({self.state})")


@dataclass
class Battery:
    level: int
    charging_state: int


@dataclass
class Other:
    family: int
    type: Optional[int]
    raw: bytes


def parse(data: bytes):
    """Decode one EVT notification. Returns None for a zero-length (disconnect) one."""
    if not data:
        return None
    family = data[0]
    if family == 0x91 and len(data) >= 3:
        return Battery(data[1], data[2])
    if family != 0x11 or len(data) < 2:
        return Other(family, data[1] if len(data) > 1 else None, data)
    kind = data[1]
    if kind == 0x02 and len(data) >= 17:
        return BallMetrics(
            ball_speed_mps=_i16(data, 3) / 100, launch_angle=_i16(data, 5) / 100,
            direction=_i16(data, 7) / 100, total_spin=_i16(data, 9),
            spin_axis_raw=_i16(data, 11) / 100, backspin=_i16(data, 13),
            sidespin_raw=_i16(data, 15), shot_type=data[2])
    if kind == 0x07 and len(data) >= 19:
        mask = data[2]
        values = {}
        for n, name in enumerate(CLUB_FIELDS):
            raw = _i16(data, 3 + 2 * n)
            if mask & (1 << n) and raw != -1:
                values[name] = raw / 100
        return ClubMetrics(mask=mask, **values)
    if kind == 0x01 and len(data) >= 17:
        return Sensor(ready=data[3] in (1, 2), detected=data[4] == 1,
                      position=struct.unpack_from("<iii", data, 5))
    if kind == 0x03 and len(data) >= 4:
        return HeartbeatAck(data[3])
    return Other(family, kind, data)


def shot_record(ball: BallMetrics, club: Optional[ClubMetrics]) -> dict:
    """Flat record for logging: Owni raw values plus GSPro-convention fields."""
    rec = {
        "ball_speed_mph": round(ball.ball_speed_mph, 2),
        "vla": ball.launch_angle,
        "hla": ball.direction,
        "total_spin": ball.total_spin,
        "spin_axis": ball.spin_axis,          # GSPro convention (+ = fade)
        "backspin": ball.backspin,
        "sidespin": ball.sidespin,            # GSPro convention (+ = right)
        "raw": asdict(ball),
    }
    if club is not None and club.mask:
        rec["club"] = {k: v for k, v in asdict(club).items() if v is not None and k != "mask"}
        if club.club_speed_mph is not None:
            rec["club"]["club_speed_mph"] = round(club.club_speed_mph, 2)
    return rec
