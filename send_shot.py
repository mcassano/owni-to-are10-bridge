#!/usr/bin/env python3
"""Send a test shot (GSPro OpenConnect JSON) to the are10 emulator.

Usage: send_shot.py [preset] [--host 127.0.0.1] [--port 921]
                    [--speed MPH] [--vla DEG] [--hla DEG] [--spin RPM] [--axis DEG] [--club MPH]

Presets: driver (default), 7iron, wedge. Any --option overrides the preset's value.
HLA negative = left; spin axis positive = fade/slice (right), negative = draw/hook.
"""

import argparse
import json
import socket

PRESETS = {
    #        ball mph, VLA, HLA, spin rpm, axis, club mph
    "driver": dict(speed=150.0, vla=12.5, hla=-1.5, spin=2600, axis=4.0, club=102.0),
    "7iron":  dict(speed=118.0, vla=17.0, hla=0.5, spin=6500, axis=-2.0, club=85.0),
    "wedge":  dict(speed=90.0, vla=26.0, hla=0.0, spin=9000, axis=0.0, club=72.0),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", nargs="?", default="driver", choices=PRESETS)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=921)
    ap.add_argument("--speed", type=float, help="ball speed, mph")
    ap.add_argument("--vla", type=float, help="vertical launch angle, degrees")
    ap.add_argument("--hla", type=float, help="horizontal launch angle, degrees (negative = left)")
    ap.add_argument("--spin", type=float, help="total spin, rpm")
    ap.add_argument("--axis", type=float, help="spin axis, degrees (positive = fade)")
    ap.add_argument("--club", type=float, help="club speed, mph (0 to omit club data)")
    args = ap.parse_args()

    v = dict(PRESETS[args.preset])
    v.update({k: getattr(args, k) for k in v if getattr(args, k) is not None})

    shot = {
        "DeviceID": "are10emu-test", "Units": "Yards", "ShotNumber": 1, "APIversion": "1",
        "BallData": {"Speed": v["speed"], "VLA": v["vla"], "HLA": v["hla"],
                     "TotalSpin": v["spin"], "SpinAxis": v["axis"]},
        "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": v["club"] > 0},
    }
    if v["club"] > 0:
        shot["ClubData"] = {"Speed": v["club"]}

    with socket.create_connection((args.host, args.port), timeout=5) as s:
        s.sendall(json.dumps(shot).encode())
        reply = json.loads(s.recv(4096).decode())
    print(f"{args.preset}: {v['speed']:.0f} mph, VLA {v['vla']}, HLA {v['hla']}, "
          f"spin {v['spin']:.0f} @ {v['axis']} → {reply.get('Message')}")


if __name__ == "__main__":
    main()
