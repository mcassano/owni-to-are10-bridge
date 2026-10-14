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
                s = owni_logger.Session(link, "driver", path, [0])
                for p in packets:
                    s.handle(p)
                    await asyncio.sleep(0)  # let the 0x87 request task run
                if s.pending_ball:
                    s.record(s.pending_ball, None)
                shots = [json.loads(l) for l in open(path)] if os.path.exists(path) else []
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


if __name__ == "__main__":
    unittest.main()
