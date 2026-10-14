#!/usr/bin/env python3
"""Connect to the Owni whenever it's on, log every shot, and forward it to the emulator.

Shots go to the journal and, as JSON lines, to --shots-file. Ball fields are
logged in GSPro convention (HLA negative = left, spin axis positive = fade)
alongside the Owni's raw values. Each shot is also sent to the are10 emulator's
GSPro OpenConnect port (--forward, default 127.0.0.1:921; "off" to disable).

The Owni won't detect a ball until it's told a club, so it's always armed with
the driver: Dome Tee Nero never learns the club, and the Owni only uses it for
putting mode and vertical face impact, neither of which reaches the app.
ownistatus shows the connection, battery and shot count (control socket).

Usage: sudo .venv/bin/python owni_logger.py [--shots-file PATH] [-v]
"""

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Optional

from ownilog import protocol
from ownilog.central import LinkLost, OwniLink
from ownilog.forward import Forwarder

log = logging.getLogger("ownilog")

HEARTBEAT_EVERY = 5.0
SILENCE_LIMIT = 15.0       # three missed heartbeats → treat link as lost
CLUB_REPLY_WAIT = 3.0      # how long to wait for 0x07 after a ball packet
RETRY_DELAY = 3.0
ARM_CLUB = "driver"
QUICK_RETRIES = 4          # reconnect attempts on a known device before rescanning


class Logger:
    """What outlives a connection: shot numbering and forwarding."""

    def __init__(self, shots_file: str, forwarder: Optional[Forwarder] = None):
        self.shots_file = shots_file
        self.forwarder = forwarder
        self.shot_count = self._last_shot_number()
        self.session: Optional["Session"] = None

    def _last_shot_number(self) -> int:
        """Continue numbering from the shots file instead of restarting at 1."""
        try:
            with open(self.shots_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4096))
                last = f.read().splitlines()[-1]
            return int(json.loads(last)["shot"])
        except (OSError, IndexError, ValueError, KeyError, TypeError):
            return 0

    def status(self) -> dict:
        s = self.session
        return {"connected": s is not None and s.armed,
                "state": s.state_name if s else None,
                "battery": s.battery[0] if s and s.battery else None,
                "shots": self.shot_count}

    def next_shot_number(self) -> int:
        self.shot_count += 1
        return self.shot_count

    async def serve_control(self, path: str):
        """Line command: "status"."""
        async def client(reader, writer):
            try:
                line = (await asyncio.wait_for(reader.readline(), 5)).decode().strip()
                if line == "status":
                    reply = json.dumps(self.status())
                else:
                    reply = "error: the only command is 'status'"
            except Exception as e:  # keep the logger alive whatever a client sends
                reply = f"error: {e}"
            writer.write((reply + "\n").encode())
            await writer.drain()
            writer.close()

        if os.path.exists(path):
            os.unlink(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        server = await asyncio.start_unix_server(client, path)
        os.chmod(path, 0o666)  # ownistatus works without sudo
        log.info("control socket: %s", path)
        return server


class Session:
    def __init__(self, link: OwniLink, logger: Logger):
        self.link = link
        self.logger = logger
        self.armed = False
        self.queue: asyncio.Queue = asyncio.Queue()
        self.seq = 0
        self.last_payload: Optional[bytes] = None
        self.last_rx = time.monotonic()
        self.pending_ball: Optional[protocol.BallMetrics] = None
        self.pending_since = 0.0
        self.state: Optional[int] = None
        self.battery: Optional[tuple] = None
        link.on_notify = self.queue.put_nowait

    async def send(self, builder, *args):
        data = builder(self.seq, *args)
        self.seq = (self.seq + 1) & 0xFF
        log.debug("→ %s", data.hex())
        await self.link.write(data)

    async def run(self):
        for label, uuid in (("device id", protocol.DEVICE_ID_UUID),
                            ("hardware", protocol.HW_VERSION_UUID),
                            ("firmware", protocol.FW_VERSION_UUID)):
            try:
                log.info("%s: %s", label, await self.link.read_text(uuid))
            except Exception as e:
                log.debug("read %s failed: %s", label, e)

        await self.send(protocol.query)
        await self.send(protocol.heartbeat)
        await self.send(protocol.club_select, ARM_CLUB)
        await self.send(protocol.detect_ball, True)
        self.armed = True
        log.info("armed; waiting for shots")

        heartbeat = asyncio.create_task(self.heartbeats())
        try:
            while True:
                if not self.link.connected:
                    raise LinkLost("disconnected")
                try:
                    data = await asyncio.wait_for(self.queue.get(), 1.0)
                except asyncio.TimeoutError:
                    data = None
                now = time.monotonic()
                if data is not None:
                    self.last_rx = now
                    self.handle(data)
                if now - self.last_rx > SILENCE_LIMIT:
                    raise LinkLost(f"no data for {SILENCE_LIMIT:.0f}s")
                if self.pending_ball and now - self.pending_since > CLUB_REPLY_WAIT:
                    self.record(self.pending_ball, None)
        finally:
            heartbeat.cancel()

    @property
    def state_name(self) -> Optional[str]:
        return None if self.state is None else protocol.DEVICE_STATES.get(self.state, str(self.state))

    async def heartbeats(self):
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY)
            try:
                await self.send(protocol.heartbeat)
            except Exception as e:
                log.warning("heartbeat failed: %s", e)

    def handle(self, data: bytes):
        if data == self.last_payload:  # every notification arrives twice
            return
        self.last_payload = data
        msg = protocol.parse(data)
        log.debug("← %s %s", data.hex(), type(msg).__name__)
        if msg is None:  # zero-length notification: device tearing the link down
            self.link.connected = False
        elif isinstance(msg, protocol.BallMetrics):
            if self.pending_ball:  # previous shot never got club data
                self.record(self.pending_ball, None)
            self.pending_ball, self.pending_since = msg, time.monotonic()
            asyncio.get_running_loop().create_task(self.send(protocol.request_club))
        elif isinstance(msg, protocol.ClubMetrics):
            if self.pending_ball:  # otherwise it's a stale shot from before connecting
                self.record(self.pending_ball, msg)
        elif isinstance(msg, protocol.HeartbeatAck):
            if msg.state != self.state:
                log.info("device state: %s", msg.state_name)
                self.state = msg.state
        elif isinstance(msg, protocol.Battery):
            if (msg.level, msg.charging_state) != self.battery:
                log.info("battery %d%% (charging state %d)", msg.level, msg.charging_state)
                self.battery = (msg.level, msg.charging_state)

    def record(self, ball: protocol.BallMetrics, club: Optional[protocol.ClubMetrics]):
        self.pending_ball = None
        rec = {"shot": self.logger.next_shot_number(), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
               **protocol.shot_record(ball, club)}
        side = "L" if ball.direction < 0 else "R"
        curve = "fade" if rec["spin_axis"] > 0 else "draw" if rec["spin_axis"] < 0 else "straight"
        club_txt = ""
        if "club" in rec and "club_speed_mph" in rec["club"]:
            c = rec["club"]
            club_txt = (f" | club {c['club_speed_mph']:.1f} mph, path {c.get('path')}, "
                        f"face {c.get('face')}, AoA {c.get('attack_angle')}, smash {c.get('smash_factor')}")
        log.info("SHOT %d: %.1f mph, VLA %.1f, HLA %.1f %s, spin %d @ %.1f (%s)%s",
                 rec["shot"], rec["ball_speed_mph"], ball.launch_angle, abs(ball.direction), side,
                 ball.total_spin, rec["spin_axis"], curve, club_txt)
        try:
            with open(self.logger.shots_file, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", self.logger.shots_file, e)
        if self.logger.forwarder is not None:
            self.logger.forwarder.submit(rec)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="hci0",
                    help="hciN or the controller's Bluetooth address (stable across reboots)")
    ap.add_argument("--shots-file", default="/var/lib/owni-logger/shots.jsonl")
    ap.add_argument("--control", default="/run/owni-logger/control.sock",
                    help="UNIX socket for ownistatus")
    ap.add_argument("--forward", default="127.0.0.1:921",
                    help='are10 emulator shot port, HOST:PORT, or "off"')
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    os.makedirs(os.path.dirname(args.shots_file), exist_ok=True)

    forwarder = None
    if args.forward != "off":
        host, _, port = args.forward.rpartition(":")
        forwarder = Forwarder(host or "127.0.0.1", int(port))
        asyncio.get_running_loop().create_task(forwarder.run())
        log.info("forwarding shots to %s:%s", host or "127.0.0.1", port)
    logger = Logger(args.shots_file, forwarder)
    await logger.serve_control(args.control)

    link = OwniLink(args.adapter)
    await link.start()
    log.info("looking for the Owni (turn it on)")
    searching_logged = False
    while True:
        path = await link.find()
        if path is None:
            if not searching_logged:
                log.info("Owni not advertising (off, or its Bluetooth timed out); still looking")
                searching_logged = True
            continue  # keep scanning in 10 s windows
        searching_logged = False
        # The link setup is flaky on the Pi 4, so retry a few times right away
        # instead of rescanning (the Owni may switch its Bluetooth off if idle).
        for attempt in range(1, QUICK_RETRIES + 1):
            try:
                await link.connect(path)
                logger.session = Session(link, logger)
                await logger.session.run()
                break
            except (LinkLost, RuntimeError, asyncio.TimeoutError) as e:
                log.info("attempt %d: %s", attempt, e)
            finally:
                logger.session = None
                await link.disconnect()
            await asyncio.sleep(1.0)
        await asyncio.sleep(RETRY_DELAY)


if __name__ == "__main__":
    asyncio.run(main())
