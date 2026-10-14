#!/usr/bin/env python3
"""Status light, power button and fan for a Pi in a Vizy enclosure.

The Vizy power board (I2C bus 1, address 0x14) has an RGB light, a button and
a fan, but nothing on a plain Raspberry Pi OS install talks to it. This does:

  light   green = Linux running; flashing red = shutting down;
          off = halted, safe to unplug (set by vizy-power-shutdown, the last
          thing before the Pi halts). Dim white is the board's own "has power".
  button  hold ~5 s for a clean shutdown (the board flags the long press).
  fan     off below FAN_START, ramping to full at FAN_FULL.

Register layout per Charmed Labs' vizypowerboard.py (hardware 3.0).

Usage: sudo python3 vizy_power.py [--once]   (--once: print board status and exit)
"""

import argparse
import logging
import signal
import subprocess
import time

import smbus

log = logging.getLogger("vizy-power")

ADDR = 0x14
REG_HW_VERSION = 1
REG_POWER = 38           # reads 0x0f after a long button press; [0x1f, t/100ms] = power off after t
REG_POWER_ON_SOURCE = 40
REG_LED = 49             # [mode, r, g, b, on/10ms, off/10ms, flashes, pause/10ms, atten]
REG_LED_BACKGROUND = 58  # [r, g, b], shown whenever the LED isn't running a pattern
REG_FAN = 75             # speed 0-4
EXEC = 0xC0              # command register (semaphore 0xff)
EXEC_AD = 2
CHANNEL_VIN, CHANNEL_5V = 4, 5

POWER_OFF_REQUESTED = 0x0f
POWER_ON_SOURCES = {1: "alarm", 2: "button", 3: "12V input", 4: "Pi USB-C"}

GREEN = (0, 32, 0)
RED = (255, 0, 0)

FAN_START = 60.0  # °C, speed 1
FAN_FULL = 75.0   # °C, speed 4 (the Pi 4 throttles at 80)
FAN_HOLD = 30.0   # seconds before slowing the fan down again
POLL = 1.0


def cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000


def fan_speed_for(temp: float) -> int:
    if temp < FAN_START:
        return 0
    if temp >= FAN_FULL:
        return 4
    return 1 + int((temp - FAN_START) / (FAN_FULL - FAN_START) * 3)


class Board:
    def __init__(self, bus: int = 1):
        self.bus = smbus.SMBus(bus)

    def read(self, reg: int, n: int) -> list:
        return self.bus.read_i2c_block_data(ADDR, reg, n)

    def write(self, reg: int, data: list):
        self.bus.write_i2c_block_data(ADDR, reg, data)

    def hw_version(self) -> list:
        return self.read(REG_HW_VERSION, 2)

    def power_off_requested(self) -> bool:
        return self.read(REG_POWER, 1)[0] == POWER_OFF_REQUESTED

    def power_on_source(self) -> str:
        source = self.read(REG_POWER_ON_SOURCE, 1)[0]
        return POWER_ON_SOURCES.get(source, f"unknown ({source})")

    def background(self, rgb: tuple):
        self.write(REG_LED_BACKGROUND, list(rgb))

    def led(self, rgb: tuple, flashes: int = 0, repeat: bool = False,
            on_ms: int = 100, off_ms: int = 100, pause_ms: int = 200):
        mode = 0x02 if repeat else 0x01
        self.write(REG_LED, [mode, *rgb, on_ms // 10, off_ms // 10, flashes, pause_ms // 10, 255])

    def fan(self, speed: int):
        self.write(REG_FAN, [speed])

    def measure(self, channel: int) -> float:
        for _ in range(1000):
            if self.read(EXEC, 1)[0] == 0xFF:
                break
            time.sleep(0.001)
        self.write(EXEC, [EXEC_AD, channel])
        while self.read(0, 1)[0] & 0x80:  # busy
            time.sleep(0.001)
        lo, hi = self.read(EXEC + 2, 2)
        self.write(EXEC, [0xFF])  # release the semaphore
        return (hi * 256 + lo) / 1000


def status(board: Board) -> str:
    return (f"power board {'.'.join(map(str, board.hw_version()))}, "
            f"powered on by {board.power_on_source()}, "
            f"5V rail {board.measure(CHANNEL_5V):.2f} V, 12V input {board.measure(CHANNEL_VIN):.2f} V, "
            f"CPU {cpu_temp():.1f} °C, fan {board.read(REG_FAN, 1)[0]}")


def run(board: Board):
    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log.info(status(board))
    board.background(GREEN)
    board.led((0, 0, 0))
    fan, fan_since = None, 0.0
    while not stop:
        try:
            if board.power_off_requested():
                log.info("power button held: shutting down")
                board.background((0, 0, 0))
                board.led(RED, flashes=1, repeat=True, on_ms=500, off_ms=500, pause_ms=0)
                subprocess.run(["systemctl", "poweroff"])
                return
            speed, now = fan_speed_for(cpu_temp()), time.monotonic()
            # Speed up right away; slow down only after holding a speed for FAN_HOLD.
            if fan is None or speed > fan or (speed < fan and now - fan_since > FAN_HOLD):
                if speed != fan:
                    log.info("CPU %.1f °C: fan %d", cpu_temp(), speed)
                board.fan(speed)
                fan, fan_since = speed, now
        except OSError as e:  # a missed I2C transfer; try again next poll
            log.warning("power board: %s", e)
        time.sleep(POLL)

    # Stopped by systemd: the system is shutting down (or the service is being
    # restarted). Flash red; vizy-power-shutdown turns the light off at halt.
    board.background((0, 0, 0))
    board.led(RED, flashes=1, repeat=True, on_ms=500, off_ms=500, pause_ms=0)
    board.fan(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="print board status and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    board = Board()
    if board.hw_version() != [3, 0]:
        log.warning("untested power board hardware version %s", board.hw_version())
    if args.once:
        print(status(board))
        return
    run(board)


if __name__ == "__main__":
    main()
