"""Shot model in are10 units, plus conversion from GSPro OpenConnect JSON.

GSPro's OpenConnect shot format (mph / degrees / rpm) is the input format so a
Mac-side bridge can forward whatever the Owni produces with minimal glue.

Sign conventions (HLA, spin axis, face, path) are passed through unchanged;
verify them against a real are10 shot before trusting curvature.
"""

import math
from dataclasses import dataclass
from typing import Optional

MPH_TO_MS = 0.44704

# BallMetrics.SpinCalculationType / GolfBallType
SPIN_RATIO = 0
SPIN_MEASURED = 3
BALL_CONVENTIONAL = 1
BALL_MARKED = 2


@dataclass
class Shot:
    ball_speed: float        # m/s
    launch_angle: float      # degrees (VLA)
    launch_direction: float  # degrees (HLA)
    total_spin: float        # rpm
    spin_axis: float         # degrees
    club_speed: Optional[float] = None    # m/s
    face_angle: Optional[float] = None    # degrees
    club_path: Optional[float] = None     # degrees
    attack_angle: Optional[float] = None  # degrees
    # The Owni measures spin directly, which on a real are10 means an RCT ball.
    spin_calc_type: int = SPIN_MEASURED
    golf_ball_type: int = BALL_MARKED
    shot_id: int = 0  # assigned by the device when sent


def from_gspro(msg: dict) -> Shot:
    """Build a Shot from a GSPro OpenConnect shot message."""
    ball = msg.get("BallData") or {}
    club = msg.get("ClubData") or {}
    opts = msg.get("ShotDataOptions") or {}
    if not ball or opts.get("ContainsBallData") is False:
        raise ValueError("shot has no BallData")

    total_spin = ball.get("TotalSpin")
    spin_axis = ball.get("SpinAxis")
    if total_spin is None or spin_axis is None:
        back, side = ball.get("BackSpin", 0.0), ball.get("SideSpin", 0.0)
        total_spin = math.hypot(back, side)
        spin_axis = math.degrees(math.atan2(side, back))

    has_club = bool(club) and opts.get("ContainsClubData", True) and "Speed" in club
    return Shot(
        ball_speed=ball["Speed"] * MPH_TO_MS,
        launch_angle=ball["VLA"],
        launch_direction=ball["HLA"],
        total_spin=total_spin,
        spin_axis=spin_axis,
        club_speed=club["Speed"] * MPH_TO_MS if has_club else None,
        face_angle=club.get("FaceToTarget") if has_club else None,
        club_path=club.get("Path") if has_club else None,
        attack_angle=club.get("AngleOfAttack") if has_club else None,
    )
