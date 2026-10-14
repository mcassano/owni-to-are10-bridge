#!/usr/bin/env python3
"""Replay Dome Tee Nero's opening moves against the REAL are10 and log its answers.

Runs on the Pi (BlueZ). The are10 must already be paired with the Pi
(or be in pairing mode, solid blue LED). Usage: sudo .venv/bin/python probe_are10.py DEVICE_ADDRESS
"""

import asyncio
import sys
import time

from bleak import BleakClient

BASE = "6a4e{:04x}-667b-11e3-949a-0800200c9a66"
CONFIG = BASE.format(0x2803)
T0 = time.monotonic()


def stamp(msg):
    print(f"{time.monotonic() - T0:7.3f} {msg}", flush=True)


async def main(address):
    async with BleakClient(address, timeout=20) as c:
        stamp(f"connected, MTU {c.mtu_size}")
        # No pairing up front: replay Dome Tee Nero's sequence and log each result,
        # since the are10 rejects pairing that isn't preceded by it.

        for short in (0x2810, 0x2811, 0x2812, 0x3401, 0x3403, 0x3402):
            uuid = BASE.format(short)

            def on_notify(_char, data, short=short):
                stamp(f"NOTIFY {short:04x}: {bytes(data).hex()}")
            try:
                await c.start_notify(uuid, on_notify)
                stamp(f"subscribed {short:04x}")
            except Exception as e:
                stamp(f"subscribe {short:04x} failed: {e}")

        async def read_config(label):
            try:
                stamp(f"read 2803 ({label}) → {(await c.read_gatt_char(CONFIG)).hex()}")
            except Exception as e:
                stamp(f"read 2803 ({label}) FAILED: {e}")

        async def write(short, hexdata, label, response=True):
            stamp(f"write {short:04x} ← {hexdata}  ({label})")
            try:
                await c.write_gatt_char(BASE.format(short), bytes.fromhex(hexdata), response=response)
            except Exception as e:
                stamp(f"  FAILED: {e}")
            await asyncio.sleep(1.0)

        await read_config("initial")

        # Dome Tee Nero's sequence (client id 4) on MultiLink channel 1. Its
        # management writes landed on channel 1; try the send char (2821) first.
        await write(0x2821, "000504000000000000000000", "close all")
        await write(0x2821, "00000400000000000000040000", "register service 4")

        await read_config("after register")
        await write(0x2803, "050200", "config, as Dome Tee Nero does")
        for i in range(4):
            await asyncio.sleep(0.5)
            await read_config(f"poll {i}")

        stamp("waiting 5 s for anything else on service 4...")
        await asyncio.sleep(5)

        # For comparison: register GFDI on the same channel and capture the handshake start.
        await write(0x2821, "00000400000000000000010000", "register GFDI")
        await asyncio.sleep(3)
        stamp(f"done (connected={c.is_connected})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: probe_are10.py DEVICE_ADDRESS")
    asyncio.run(main(sys.argv[1]))
