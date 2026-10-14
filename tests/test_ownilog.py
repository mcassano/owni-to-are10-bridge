"""Tests for the Owni protocol codec and the logger's shot handling.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ownilog import protocol  # noqa: E402
from ownilog.forward import Forwarder, gspro_shot  # noqa: E402
import owni_logger  # noqa: E402


def ball_packet(speed_mps=67.0, vla=12.5, hla=-1.5, spin=2600, axis_raw=-4.0, back=2594, side_raw=-181):
    return bytes([0x11, 0x02, 0x37]) + struct.pack(
        "<hhhhhhh", round(speed_mps * 100), round(vla * 100), round(hla * 100), spin,
        round(axis_raw * 100), back, side_raw)


def club_packet(mask=0xFF, values=(1.5, 0.5, -3.0, 15.0, 2.0, -1.0, 45.6, 1.47)):
    fields = [round(v * 100) if mask & (1 << i) else -1 for i, v in enumerate(values)]
    return bytes([0x11, 0x07, mask]) + struct.pack("<8h", *fields)


class ProtocolTests(unittest.TestCase):
    def test_commands_are_nine_bytes(self):
        self.assertEqual(protocol.detect_ball(3), bytes.fromhex("008103011100000000"))
        self.assertEqual(protocol.club_select(4, "7iron"), bytes.fromhex("008204070200000000"))
        self.assertEqual(protocol.heartbeat(255), bytes.fromhex("0083ff000000000000"))
        self.assertEqual(protocol.request_club(256), bytes.fromhex("008700000000000000"))

    def test_ball_metrics_and_sign_convention(self):
        b = protocol.parse(ball_packet())
        self.assertAlmostEqual(b.ball_speed_mph, 149.87, places=1)
        self.assertEqual((b.launch_angle, b.direction, b.total_spin), (12.5, -1.5, 2600))
        # Owni reports a fade as a NEGATIVE axis; we expose GSPro convention.
        self.assertEqual((b.spin_axis_raw, b.spin_axis, b.sidespin), (-4.0, 4.0, 181))

    def test_club_metrics_mask_and_sentinel(self):
        c = protocol.parse(club_packet())
        self.assertEqual((c.path, c.face, c.club_speed_mps, c.smash_factor), (1.5, 0.5, 45.6, 1.47))
        untracked = protocol.parse(club_packet(mask=0))
        self.assertEqual(untracked.mask, 0)
        self.assertIsNone(untracked.club_speed_mps)
        partial = protocol.parse(club_packet(mask=0b10000000))  # only smash
        self.assertEqual((partial.path, partial.smash_factor), (None, 1.47))

    def test_other_families(self):
        self.assertIsNone(protocol.parse(b""))
        self.assertEqual(protocol.parse(bytes([0x91, 80, 2])), protocol.Battery(80, 2))
        hb = protocol.parse(bytes.fromhex("110305040000000000"))
        self.assertEqual(hb.state_name, "ready")
        sensor = protocol.parse(bytes([0x11, 0x01, 9, 1, 1]) + struct.pack("<iii", 1, 2, 3))
        self.assertTrue(sensor.ready and sensor.detected)
        self.assertIsInstance(protocol.parse(bytes.fromhex("710000000000000123")), protocol.Other)


class FakeLink:
    connected = True

    def __init__(self):
        self.writes = []
        self.on_notify = None

    async def write(self, data):
        self.writes.append(data)


class SessionTests(unittest.TestCase):
    def run_handle(self, packets):
        async def go():
            link = FakeLink()
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "shots.jsonl")
                s = owni_logger.Session(link, owni_logger.Logger(path))
                for p in packets:
                    s.handle(p)
                    await asyncio.sleep(0)  # let the 0x87 request task run
                if s.pending_ball:
                    s.record(s.pending_ball, None)
                shots = []
                if os.path.exists(path):
                    with open(path) as f:
                        shots = [json.loads(line) for line in f]
                return link, shots
        return asyncio.run(go())

    def test_duplicates_ignored_and_club_correlated(self):
        ball, club = ball_packet(), club_packet()
        link, shots = self.run_handle([ball, ball, club, club])
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["spin_axis"], 4.0)
        self.assertAlmostEqual(shots[0]["club"]["club_speed_mph"], 102.0, places=0)
        self.assertEqual([w[1] for w in link.writes], [protocol.CMD_REQUEST_CLUB])

    def test_stale_club_data_without_ball_is_dropped(self):
        _, shots = self.run_handle([club_packet()])
        self.assertEqual(shots, [])

    def test_untracked_club_still_logs_ball(self):
        _, shots = self.run_handle([ball_packet(), club_packet(mask=0)])
        self.assertEqual(len(shots), 1)
        self.assertNotIn("club", shots[0])


def record(shot=1, club=True):
    rec = {"shot": shot, **protocol.shot_record(protocol.parse(ball_packet()),
                                                  protocol.parse(club_packet()) if club else None)}
    return rec


class ForwardTests(unittest.TestCase):
    def test_gspro_shot_fields(self):
        msg = gspro_shot(record())
        self.assertEqual(msg["BallData"]["SpinAxis"], 4.0)  # GSPro sign, from the raw -4.0
        self.assertEqual(msg["BallData"]["HLA"], -1.5)
        self.assertAlmostEqual(msg["ClubData"]["Speed"], 102.0, places=0)
        self.assertEqual((msg["ClubData"]["Path"], msg["ClubData"]["FaceToTarget"]), (1.5, 0.5))
        self.assertTrue(msg["ShotDataOptions"]["ContainsClubData"])
        no_club = gspro_shot(record(club=False))
        self.assertNotIn("ClubData", no_club)
        self.assertFalse(no_club["ShotDataOptions"]["ContainsClubData"])

    def test_forwarder_delivers_and_reconnects(self):
        async def go():
            received = []

            async def emulator(reader, writer):
                data = await reader.read(4096)
                received.append(json.loads(data))
                writer.write(json.dumps({"Code": 200, "Message": "Shot received successfully"}).encode())
                await writer.drain()
                writer.close()  # the next shot has to reconnect

            server = await asyncio.start_server(emulator, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            fwd = Forwarder("127.0.0.1", port)
            task = asyncio.create_task(fwd.run())
            with self.assertLogs("ownilog.forward", "INFO") as logs:
                fwd.submit(record(1))
                fwd.submit(record(2))
                for _ in range(100):
                    if len(received) == 2 and len(logs.records) == 2:
                        break
                    await asyncio.sleep(0.02)
            task.cancel()
            fwd._close()
            server.close()
            await server.wait_closed()
            return received, logs.output
        received, output = asyncio.run(go())
        self.assertEqual([m["ShotNumber"] for m in received], [1, 2])
        self.assertTrue(all("forwarded" in line for line in output), output)

    def test_unreachable_emulator_drops_shot(self):
        async def go():
            fwd = Forwarder("127.0.0.1", 1)  # nothing listens on port 1
            task = asyncio.create_task(fwd.run())
            with self.assertLogs("ownilog.forward", "WARNING") as logs:
                fwd.submit(record(7))
                for _ in range(100):
                    if logs.records:
                        break
                    await asyncio.sleep(0.02)
            task.cancel()
            return logs.output
        self.assertIn("shot 7 not forwarded", asyncio.run(go())[0])


class LoggerTests(unittest.TestCase):
    def test_numbering_continues(self):
        with tempfile.TemporaryDirectory() as d:
            shots = os.path.join(d, "shots.jsonl")
            with open(shots, "w") as f:
                f.write(json.dumps({"shot": 41}) + "\n" + json.dumps({"shot": 42}) + "\n")
            self.assertEqual(owni_logger.Logger(shots).next_shot_number(), 43)

    def test_arms_with_the_driver(self):
        async def go():
            lg = owni_logger.Logger(os.devnull)
            link = FakeLink()
            session = owni_logger.Session(link, lg)
            task = asyncio.create_task(session.run())
            await asyncio.sleep(0.05)
            task.cancel()
            return link.writes
        writes = asyncio.run(go())
        self.assertIn(protocol.club_select(2, "driver"), writes)
        self.assertEqual(writes[-1][1], protocol.CMD_DETECT_BALL)

    def test_control_socket_status(self):
        async def go(d):
            lg = owni_logger.Logger(os.path.join(d, "shots.jsonl"))
            sock = os.path.join(d, "ctl", "control.sock")
            server = await lg.serve_control(sock)

            async def ask(line):
                r, w = await asyncio.open_unix_connection(sock)
                w.write((line + "\n").encode())
                reply = (await r.readline()).decode().strip()
                w.close()
                return reply

            replies = [await ask("status"), await ask("club 7i")]
            server.close()
            return replies
        with tempfile.TemporaryDirectory() as d:
            status, club = asyncio.run(go(d))
        self.assertEqual(json.loads(status), {"connected": False, "state": None, "battery": None,
                                              "shots": 0})
        self.assertTrue(club.startswith("error"))


if __name__ == "__main__":
    unittest.main()
