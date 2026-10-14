"""Unit tests: framing primitives and a full scripted host session.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from are10emu import pb, wire  # noqa: E402
from are10emu.device import Are10Device  # noqa: E402
from are10emu.shots import from_gspro  # noqa: E402


class WireTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(wire.crc16(b"123456789"), 0xBB3D)

    def test_cobs_roundtrip(self):
        for data in [b"", b"\x00", b"\x00\x00", b"\x11\x00\x22", bytes(range(256)) * 3,
                     bytes([1] * 254), bytes([1] * 255)]:
            enc = wire.cobs_encode(data)
            self.assertNotIn(0, enc)
            self.assertEqual(wire.cobs_decode(enc), data)

    def test_frame_roundtrip_both_headers(self):
        f = wire.parse_frame(wire.build_frame(5043, b"abc"))
        self.assertEqual((f.msg_type, f.payload, f.txn_id), (5043, b"abc", None))
        f = wire.parse_frame(wire.build_frame(5043, b"abc", txn_id=5))
        self.assertEqual((f.msg_type, f.payload, f.txn_id), (5043, b"abc", 5))

    def test_register_response_matches_real_are10(self):
        # Real are10 response to a MultiLink register.
        real = bytes([0x00, 0x01, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00])
        self.assertEqual(wire.build_register_resp(1, 1, 0, 1), real)

    def test_gspro_conversion(self):
        shot = from_gspro({"BallData": {"Speed": 150.0, "VLA": 12.0, "HLA": -1.5,
                                        "TotalSpin": 2500, "SpinAxis": 3.0},
                           "ClubData": {"Speed": 100.0, "AngleOfAttack": -2, "Path": 1,
                                        "FaceToTarget": 0.5}})
        self.assertAlmostEqual(shot.ball_speed, 67.056, places=3)
        self.assertAlmostEqual(shot.club_speed, 44.704, places=3)
        shot = from_gspro({"BallData": {"Speed": 100, "VLA": 20, "HLA": 0,
                                        "BackSpin": 3000, "SideSpin": 0}})
        self.assertEqual((shot.total_spin, shot.spin_axis, shot.club_speed), (3000, 0, None))


class Host:
    """Minimal host following the app's session sequence."""

    def __init__(self, test):
        self.test = test
        self.notifications = []  # (channel, bytes)
        self.dev = Are10Device(lambda ch, data: self.notifications.append((ch, data)))
        self.handle = None
        self.stream = wire.StreamBuffer()
        self.frames = []

    def pump(self):
        """Collect GFDI frames from pending notifications."""
        for _, data in self.notifications:
            if data[0] == 0:
                continue
            self.test.assertEqual(data[0], self.handle)
            self.test.assertLessEqual(len(data), self.dev.mtu - 3)
            self.stream.extend(data[1:])
        self.notifications.clear()
        for raw in self.stream.frames():
            self.frames.append(wire.parse_frame(raw))

    def take(self, msg_type):
        self.pump()
        self.test.assertTrue(self.frames, f"expected {msg_type}, got nothing")
        f = self.frames.pop(0)
        self.test.assertEqual(f.msg_type, msg_type, f"payload {f.payload.hex()}")
        return f

    def send(self, frame, mtu=20):
        data = wire.wrap_cobs(frame)
        for i in range(0, len(data), mtu - 1):
            self.dev.on_write(0, bytes([self.handle]) + data[i:i + mtu - 1])

    def take_proto(self, msg_type=wire.MSG_PROTOBUF_REQUEST):
        f = self.take(msg_type)
        chunk = wire.parse_proto_payload(f.payload)
        self.send(wire.build_ack(msg_type, 0))
        return chunk, pb.decode(chunk.data)

    def connect(self):
        self.dev.on_write(0, struct.pack("<BBQHB", 0, 0, 1, 1, 0))
        ch, resp = self.notifications.pop(0)
        self.test.assertEqual(len(resp), 16)
        self.handle = resp[13]

        info = self.take(wire.MSG_DEVICE_INFO)
        proto_ver, product, unit_id, sw = struct.unpack_from("<HHIH", info.payload)
        self.test.assertEqual((proto_ver, product, unit_id, sw), (150, 1, 1, 100))
        host_info = struct.pack("<HHIHH", 150, 0xFFFF, 0xFFFFFFFF, 100, 0xFFFF) + b"\x00\x00\x00\x01"
        self.send(wire.build_ack(wire.MSG_DEVICE_INFO, 0, host_info))

        caps = self.take(wire.MSG_CONFIGURATION)
        self.test.assertEqual(wire.parse_capabilities(caps.payload), [1, 3, 4, 5, 29, 71, 76, 81])
        self.send(wire.build_ack(wire.MSG_CONFIGURATION, 0))
        self.send(wire.build_frame(wire.MSG_CONFIGURATION, wire.capability_bitmap([30], 13)))
        ack = self.take(wire.MSG_ACK)
        self.test.assertEqual(struct.unpack_from("<HB", ack.payload), (wire.MSG_CONFIGURATION, 0))

    def request(self, request_id, smart):
        self.send(wire.build_frame(wire.MSG_PROTOBUF_REQUEST,
                                   wire.build_proto_payload(request_id, smart)))
        ack = self.take(wire.MSG_ACK)
        self.test.assertEqual(ack.payload,
                              struct.pack("<HB", wire.MSG_PROTOBUF_REQUEST, 0)
                              + wire.proto_ack_payload(request_id))
        chunk, msg = self.take_proto(wire.MSG_PROTOBUF_RESPONSE)
        self.test.assertEqual(chunk.request_id, request_id)
        return msg


def alert_details(msg):
    notification = pb.get_msg(pb.get_msg(msg, 30), 3)
    assert pb.get_repeated_varint(notification, 1) == [pb.ALERT_LAUNCH_MONITOR]
    return pb.get_msg(notification, 1001)


def alert_state(msg):
    return pb.get_varint(pb.get_msg(alert_details(msg), 1), 1)


class SessionTests(unittest.TestCase):
    def armed_host(self):
        host = Host(self)
        host.connect()
        sub = pb.f_msg(30, pb.f_msg(1, pb.f_msg(1, pb.f_varint(1, pb.ALERT_LAUNCH_MONITOR))))
        resp = host.request(1, sub)
        status = pb.get_msg(pb.get_msg(pb.get_msg(resp, 30), 2), 1)
        self.assertEqual(pb.get_varint(status, 1), 0)

        resp = host.request(2, pb.f_msg(38, pb.f_msg(3, b"")))
        self.assertEqual(pb.get_varint(pb.get_msg(pb.get_msg(resp, 38), 4), 1), pb.WAKE_UP_SUCCESS)
        self.assertEqual(alert_state(host.take_proto()[1]), pb.STATE_INTERFERENCE_TEST)
        self.assertEqual(alert_state(host.take_proto()[1]), pb.STATE_WAITING)
        return host

    def test_full_session_and_shot(self):
        host = self.armed_host()
        self.assertTrue(host.dev.ready)
        shot = from_gspro({"BallData": {"Speed": 150.0, "VLA": 12.0, "HLA": -1.5,
                                        "TotalSpin": 2500, "SpinAxis": 3.0},
                           "ClubData": {"Speed": 100.0, "AngleOfAttack": -2.0, "Path": 1.0,
                                        "FaceToTarget": 0.5}})
        self.assertTrue(host.dev.push_shot(shot))

        self.assertEqual(alert_state(host.take_proto()[1]), pb.STATE_RECORDING)
        self.assertEqual(alert_state(host.take_proto()[1]), pb.STATE_PROCESSING)
        metrics = pb.get_msg(alert_details(host.take_proto()[1]), 2)
        self.assertEqual(pb.get_varint(metrics, 1), 1)
        ball = pb.get_msg(metrics, 3)
        self.assertAlmostEqual(pb.get_float(ball, 3), 67.056, places=3)
        self.assertAlmostEqual(pb.get_float(ball, 2), -1.5, places=5)
        self.assertAlmostEqual(pb.get_float(ball, 5), 2500.0)
        club = pb.get_msg(metrics, 4)
        self.assertAlmostEqual(pb.get_float(club, 1), 44.704, places=3)
        self.assertEqual(alert_state(host.take_proto()[1]), pb.STATE_WAITING)

    def test_file_upload_declined_as_unsupported(self):
        host = self.armed_host()
        create = bytes.fromhex("47b22000ff0800000000ffff1200") + b"Vendor/UPDATE0.BIN" + bytes(8)
        host.send(wire.build_frame(5005, create))
        ack = host.take(wire.MSG_ACK)
        self.assertEqual(ack.payload, struct.pack("<HB", 5005, 0) + struct.pack("<BHBBH", 3, 0, 0xFF, 0x08, 0))

    def test_wakeup_twice_reports_already_awake(self):
        host = self.armed_host()
        resp = host.request(3, pb.f_msg(38, pb.f_msg(3, b"")))
        self.assertEqual(pb.get_varint(pb.get_msg(pb.get_msg(resp, 38), 4), 1),
                         pb.WAKE_UP_ALREADY_AWAKE)

    def test_status_request(self):
        host = self.armed_host()
        resp = host.request(3, pb.f_msg(38, pb.f_msg(1, b"")))
        state = pb.get_msg(pb.get_msg(pb.get_msg(resp, 38), 2), 1)
        self.assertEqual(pb.get_varint(state, 1), pb.STATE_WAITING)

    def test_shot_dropped_when_not_armed(self):
        host = Host(self)
        host.connect()
        self.assertFalse(host.dev.push_shot(from_gspro(
            {"BallData": {"Speed": 100, "VLA": 10, "HLA": 0, "TotalSpin": 2000, "SpinAxis": 0}})))

    def test_gadgetbridge_style_close_all_then_register_on_send_char(self):
        notes = []
        dev = Are10Device(lambda ch, d: notes.append((ch, d)))
        dev.on_write(1, struct.pack("<BBQH", 0, wire.ML_CLOSE_ALL_REQ, 0x2C, 0))
        self.assertEqual(notes.pop(0)[1][1], wire.ML_CLOSE_ALL_RESP)
        dev.on_write(1, struct.pack("<BBQHB", 0, wire.ML_REGISTER_REQ, 0x2C, 1, 2))
        ch, resp = notes.pop(0)
        self.assertEqual((ch, resp[1], resp[12], resp[14]), (1, wire.ML_REGISTER_RESP, 0, 0))
        self.assertTrue(notes and notes[0][1][0] == resp[13])  # 5024 follows on that handle

    def test_registration_service_accepted_and_logged(self):
        notes = []
        dev = Are10Device(lambda ch, d: notes.append((ch, d)))
        dev.on_write(1, bytes.fromhex("00000400000000000000040000"))  # captured from Dome Tee Nero
        ch, resp = notes.pop(0)
        self.assertEqual((ch, resp[1], resp[12]), (1, wire.ML_REGISTER_RESP, 0))
        with self.assertLogs("are10emu.device", "INFO") as logs:
            dev.on_write(1, bytes([resp[13]]) + b"\x12\x34")
        self.assertIn("svc4", logs.output[0])

    def test_close_all_only_closes_requesting_client(self):
        host = self.armed_host()  # client id 1 on channel 0
        # Dome Tee Nero's own client (ASCII id) connects on channel 2 and closes all.
        host.dev.on_write(2, bytes.fromhex("00054845524f20694f530000"))
        self.assertIn(host.handle, host.dev.sessions)
        self.assertTrue(host.dev.ready)

    def test_unsupported_service_rejected(self):
        notes = []
        dev = Are10Device(lambda ch, d: notes.append((ch, d)))
        dev.on_write(0, struct.pack("<BBQHB", 0, 0, 1, 7, 0))
        self.assertEqual(notes[0][1][12], wire.ML_STATUS_INVALID_SERVICE)
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
