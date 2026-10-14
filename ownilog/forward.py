"""Forward logged shots to the are10 emulator as GSPro OpenConnect JSON.

The emulator listens on TCP 921 (the same port are10shot uses). One connection
is kept open and re-opened as needed; a shot that can't be delivered is logged
and dropped, never queued for later, so a stale shot can't land on a new hole.
"""

import asyncio
import json
import logging
from typing import Optional

log = logging.getLogger("ownilog.forward")

REPLY_WAIT = 5.0


def gspro_shot(rec: dict) -> dict:
    """GSPro OpenConnect shot message from a logger record (already GSPro signs)."""
    msg = {
        "DeviceID": "owni-logger", "Units": "Yards", "ShotNumber": rec["shot"], "APIversion": "1",
        "BallData": {"Speed": rec["ball_speed_mph"], "VLA": rec["vla"], "HLA": rec["hla"],
                     "TotalSpin": rec["total_spin"], "SpinAxis": rec["spin_axis"],
                     "BackSpin": rec["backspin"], "SideSpin": rec["sidespin"]},
        "ShotDataOptions": {"ContainsBallData": True, "ContainsClubData": False},
    }
    club = rec.get("club") or {}
    if "club_speed_mph" in club:
        # Face and path go through with the Owni's signs, which aren't documented;
        # check them against a shot with a known open or closed face.
        data = {"Speed": club["club_speed_mph"]}
        for ours, theirs in (("path", "Path"), ("face", "FaceToTarget"),
                             ("attack_angle", "AngleOfAttack"), ("dynamic_loft", "Loft")):
            if ours in club:
                data[theirs] = club[ours]
        msg["ClubData"] = data
        msg["ShotDataOptions"]["ContainsClubData"] = True
    return msg


class Forwarder:
    def __init__(self, host: str = "127.0.0.1", port: int = 921):
        self.host, self.port = host, port
        self.queue: asyncio.Queue = asyncio.Queue()
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.decoder = json.JSONDecoder()

    def submit(self, rec: dict):
        self.queue.put_nowait(rec)

    async def run(self):
        while True:
            rec = await self.queue.get()
            reply = None
            for attempt in (1, 2):  # a dropped connection is noticed on first use; retry once
                try:
                    reply = await self._send(gspro_shot(rec))
                    break
                except (OSError, asyncio.TimeoutError, ValueError) as e:
                    self._close()
                    if attempt == 2:
                        log.warning("shot %d not forwarded to %s:%d: %s",
                                    rec["shot"], self.host, self.port, e or type(e).__name__)
            if reply is None:
                continue
            if reply.get("Code") == 200:
                log.info("shot %d forwarded: %s", rec["shot"], reply.get("Message"))
            else:
                log.warning("shot %d not accepted: %s", rec["shot"], reply.get("Message"))

    async def _send(self, msg: dict) -> dict:
        if self.writer is None:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), REPLY_WAIT)
        self.writer.write(json.dumps(msg).encode())
        await self.writer.drain()
        buf = ""
        while True:
            data = await asyncio.wait_for(self.reader.read(4096), REPLY_WAIT)
            if not data:
                raise ConnectionResetError("emulator closed the connection")
            buf += data.decode(errors="replace")
            try:
                reply, _ = self.decoder.raw_decode(buf.lstrip())
                return reply
            except json.JSONDecodeError:
                continue

    def _close(self):
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None
