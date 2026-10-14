"""Run the emulator over stdin/stdout instead of BLE (for tools/client-harness).

Input lines:  R <hex>  host wrote MultiLink 2810 (register/control)
              W <hex>  host wrote 2820 (GFDI data)
              S        push a test shot
Output lines: N <hex>  notification on 2810
Logs go to stderr.
"""

import logging
import sys

from .device import Are10Device, Identity
from .shots import from_gspro

TEST_SHOT = {"BallData": {"Speed": 150.0, "VLA": 12.5, "HLA": -1.5,
                          "TotalSpin": 2600, "SpinAxis": 4.0},
             "ClubData": {"Speed": 102.0, "AngleOfAttack": -3.0, "Path": 1.5,
                          "FaceToTarget": 0.5}}


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if "-v" in sys.argv else logging.INFO,
                        format="emu %(levelname)s %(message)s")

    def notify(_channel, data):
        sys.stdout.write(f"N {data.hex()}\n")
        sys.stdout.flush()

    dev = Are10Device(notify, Identity.load())
    for line in sys.stdin:
        cmd, _, arg = line.strip().partition(" ")
        if cmd in ("R", "W"):
            dev.on_write(0, bytes.fromhex(arg))
        elif cmd == "S":
            dev.push_shot(from_gspro(TEST_SHOT))


if __name__ == "__main__":
    main()
