#!/usr/bin/env python3
"""are10 emulator for Raspberry Pi: advertises as an are10 over BLE and
relays shots received on a GSPro OpenConnect-compatible TCP port.

Usage: sudo .venv/bin/python are10_emulator.py [--port 921] [--adapter hci0|ADDRESS] [-v]
Send a test shot from another machine: python send_shot.py <pi-host>
"""

import argparse
import asyncio
import json
import logging

from are10emu.bluez import Are10Peripheral
from are10emu.device import DEFAULT_IDENTITY_DIR, Are10Device, Identity
from are10emu.shots import from_gspro

log = logging.getLogger("are10emu.shots")


def gspro_reply(writer, code: int, message: str):
    writer.write(json.dumps({"Code": code, "Message": message}).encode())


def handle_message(device: Are10Device, msg: dict, writer):
    opts = msg.get("ShotDataOptions") or {}
    if opts.get("IsHeartBeat") or not opts.get("ContainsBallData", True) or not msg.get("BallData"):
        gspro_reply(writer, 200, "OK")
        return
    try:
        shot = from_gspro(msg)
    except (KeyError, TypeError, ValueError) as e:
        log.warning("bad shot %s: %s", msg, e)
        gspro_reply(writer, 501, f"bad shot: {e}")
        return
    if device.push_shot(shot):
        gspro_reply(writer, 200, "Shot received successfully")
    else:
        gspro_reply(writer, 501, "are10 emulator has no armed app connected")


async def serve_shots(device: Are10Device, port: int):
    decoder = json.JSONDecoder()

    async def client(reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("shot source connected: %s", peer)
        buf = ""
        try:
            while data := await reader.read(4096):
                buf += data.decode(errors="replace")
                # GSPro connectors send back-to-back JSON objects, not always newline-separated.
                while (buf := buf.lstrip()):
                    try:
                        msg, end = decoder.raw_decode(buf)
                    except json.JSONDecodeError:
                        break
                    buf = buf[end:]
                    handle_message(device, msg, writer)
                await writer.drain()
        finally:
            log.info("shot source disconnected: %s", peer)
            writer.close()

    server = await asyncio.start_server(client, "0.0.0.0", port)
    log.info("listening for GSPro OpenConnect shots on port %d", port)
    async with server:
        await server.serve_forever()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=921, help="GSPro OpenConnect port (default 921)")
    ap.add_argument("--adapter", default="hci0",
                    help="hciN or the controller's Bluetooth address (stable across reboots)")
    ap.add_argument("--standard-headers", action="store_true",
                    help="send 4-byte GFDI headers for all messages instead of compressed ones")
    ap.add_argument("--identity", default=DEFAULT_IDENTITY_DIR,
                    help="directory with identity.json and device.xml (default: local/)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every frame")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    device = Are10Device(notify=lambda ch, data: None, identity=Identity.load(args.identity),
                         compressed_headers=not args.standard_headers)
    peripheral = Are10Peripheral(device, adapter=args.adapter)
    await peripheral.start()
    await serve_shots(device, args.port)


if __name__ == "__main__":
    asyncio.run(main())
