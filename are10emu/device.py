"""Transport-agnostic are10 device state machine.

Plays the device side of the session sequence:
MultiLink REGISTER → 5024 device info → 5050 capabilities → protobuf session
(alert subscribe, wake-up, status/tilt/shot-config) → shot alerts.

The BLE layer calls on_write()/on_disconnect() and supplies a notify callback;
everything else happens here.
"""

import json
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import pb, wire
from .shots import Shot

log = logging.getLogger("are10emu.device")

NotifyFn = Callable[[int, bytes], None]  # (channel index 0-2, payload)


# The real device's names and description file stay out of the repo, in local/.
DEFAULT_IDENTITY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "local")


@dataclass
class Identity:
    """What the device reports about itself.

    Identity.load() reads local/identity.json (the values captured from your own
    are10) and local/device.xml. The defaults are placeholders for tests.
    """
    local_name: str = "are10"            # advertised name and adapter alias
    manufacturer: str = "are10"          # DIS 2A29
    model: str = "are10"                 # DIS 2A24
    serial: str = "1"                    # DIS 2A25
    firmware: str = "1.00"               # DIS 2A28
    unit_id: int = 1                     # same number as serial
    software_version: int = 100          # firmware x 100
    product_number: int = 1
    protocol_version: int = 150
    max_packet_size: int = 0x0200
    friendly_name: str = "are10"
    device_name: str = "are10"
    model_name: str = ""
    device_info_trailer: bytes = b"\x00\x00"  # two bytes the real 5024 has after model_name
    # Raw 5050 payload: 13-byte bitmap with bits 1,3,4,5,29,71,76,81.
    capabilities: bytes = bytes.fromhex("0d3a000020000000008010020000")
    # ACK body for 5031 (supported file types): one entry, FIT type 0x80/0x24 "FIT_TYPE_36".
    file_types: bytes = bytes.fromhex("0180240b4649545f545950455f3336")
    device_xml: bytes = b'<?xml version="1.0" encoding="UTF-8"?><Device/>'  # file 0xFFFD

    @classmethod
    def load(cls, directory: str = DEFAULT_IDENTITY_DIR) -> "Identity":
        with open(os.path.join(directory, "identity.json")) as f:
            values = json.load(f)
        with open(os.path.join(directory, "device.xml"), "rb") as f:
            values["device_xml"] = f.read()
        return cls(**values)


DIRECTORY_FILE = bytes.fromhex("00000100") + bytes(28)  # file 0: directory listing, as captured
FILE_CHUNK = 494  # data bytes per 5004 chunk, as the are10 sends them

MSG_DOWNLOAD_REQUEST = 5002
MSG_FILE_TRANSFER_DATA = 5004
MSG_CREATE_FILE = 5005
MSG_5007 = 5007
MSG_SYSTEM_EVENT = 5030
MSG_SUPPORTED_FILE_TYPES = 5031
MSG_SYNCHRONIZATION = 5037
MSG_CURRENT_TIME_REQUEST = 5052

# System events (5030) the phone sends, per Gadgetbridge's naming.
EVENT_SYNC_COMPLETE = 0
EVENT_PAIR_COMPLETE = 4
EVENT_SYNC_READY = 8
# 5037 payloads the are10 sent in dtn_are10.pklg.
SYNC_AFTER_READY = bytes.fromhex("010400010000")
SYNC_AFTER_PAIR = bytes.fromhex("020402020000")


class GfdiSession:
    """One registered GFDI handle (one per MultiLink registration)."""

    def __init__(self, device: "Are10Device", channel: int, handle: int):
        self.device = device
        self.channel = channel
        self.handle = handle
        self.client_id = 0
        self.stream = wire.StreamBuffer()
        self.host_protocol_version = 0
        self.got_host_config = False
        self.subscribed = False
        self._txn = 0
        self._partial: Dict[int, bytearray] = {}
        self._download: Optional[bytes] = None
        self._seen_events: set = set()
        self._declined_upload = False

    # ── outbound ──

    def _use_compressed(self) -> bool:
        return self.device.compressed_headers and self.host_protocol_version >= 150

    def _next_txn(self) -> Optional[int]:
        if not self._use_compressed():
            return None
        txn = self._txn
        self._txn = (self._txn + 1) & 0x7F
        return txn

    def send_frame(self, frame: bytes, label: str):
        log.debug("[h%d] → %s %s", self.handle, label, frame.hex())
        self.device.send_data(self.channel, self.handle, wire.wrap_cobs(frame))

    def send_ack(self, incoming: wire.GfdiFrame, status=wire.STATUS_ACK, payload=b""):
        frame = wire.build_ack(incoming.msg_type, status, payload, incoming.txn_id)
        self.send_frame(frame, f"ACK({incoming.msg_type},{status})")

    def send_proto(self, pb_bytes: bytes, msg_type=wire.MSG_PROTOBUF_REQUEST,
                   request_id: Optional[int] = None, label="proto"):
        if request_id is None:
            request_id = self.device.next_alert_id()
        payload = wire.build_proto_payload(request_id, pb_bytes)
        self.send_frame(wire.build_frame(msg_type, payload, self._next_txn()),
                        f"{msg_type} #{request_id} {label}")

    def send_alert(self, pb_bytes: bytes, label: str):
        if self.subscribed:
            self.send_proto(pb_bytes, label=label)

    def start(self):
        info = self.device.identity
        payload = wire.build_device_info(
            info.protocol_version, info.product_number, info.unit_id,
            info.software_version, info.max_packet_size,
            info.friendly_name, info.device_name, info.model_name) + info.device_info_trailer
        self.send_frame(wire.build_frame(wire.MSG_DEVICE_INFO, payload), "5024 device info")

    # ── inbound ──

    def feed(self, data: bytes):
        try:
            self.stream.extend(data)
        except ValueError as e:
            log.warning("[h%d] %s", self.handle, e)
            return
        for raw in self.stream.frames():
            try:
                frame = wire.parse_frame(raw)
            except ValueError as e:
                log.warning("[h%d] dropping bad frame %s: %s", self.handle, raw.hex(), e)
                continue
            log.debug("[h%d] ← %d txn=%s %s", self.handle, frame.msg_type, frame.txn_id,
                      frame.payload.hex())
            self.handle_frame(frame)

    def handle_frame(self, frame: wire.GfdiFrame):
        t = frame.msg_type
        if t == wire.MSG_ACK:
            self.handle_ack(frame.payload)
        elif t == wire.MSG_CONFIGURATION:
            caps = wire.parse_capabilities(frame.payload)
            log.info("[h%d] host capabilities: %s", self.handle, caps)
            self.send_ack(frame)
            self.got_host_config = True
            log.info("[h%d] GFDI handshake complete", self.handle)
        elif t in (wire.MSG_PROTOBUF_REQUEST, wire.MSG_PROTOBUF_RESPONSE):
            self.handle_proto(frame)
        elif t == MSG_SYSTEM_EVENT:
            self.send_ack(frame, payload=b"\x00")
            self.handle_system_event(frame.payload[0] if frame.payload else -1)
        elif t == MSG_5007:
            log.debug("[h%d] 5007 %s", self.handle, frame.payload.hex())
            self.send_ack(frame, payload=b"\x00")
        elif t == MSG_SUPPORTED_FILE_TYPES:
            self.send_ack(frame, payload=self.device.identity.file_types)
        elif t == MSG_DOWNLOAD_REQUEST:
            self.handle_download(frame)
        elif t == MSG_CREATE_FILE:
            self.decline_create_file(frame)
        else:
            log.warning("[h%d] unsupported message %d: %s", self.handle, t, frame.payload.hex())
            self.send_ack(frame, wire.STATUS_UNSUPPORTED)

    def handle_ack(self, payload: bytes):
        if len(payload) < 3:
            log.warning("[h%d] short ACK %s", self.handle, payload.hex())
            return
        orig, status = struct.unpack_from("<HB", payload)
        rest = payload[3:]
        if status != wire.STATUS_ACK:
            log.warning("[h%d] host NAK'd %d with status %d (%s)", self.handle, orig, status,
                        rest.hex())
            return
        if orig == wire.MSG_DEVICE_INFO:
            if len(rest) >= 2:
                self.host_protocol_version = struct.unpack_from("<H", rest)[0]
            log.info("[h%d] host info: protocol=%d raw=%s", self.handle,
                     self.host_protocol_version, rest.hex())
            caps = self.device.identity.capabilities
            self.send_frame(wire.build_frame(wire.MSG_CONFIGURATION, caps), "5050 capabilities")
        elif orig == wire.MSG_CONFIGURATION:
            log.debug("[h%d] host ACK'd our capabilities", self.handle)
        elif orig == MSG_FILE_TRANSFER_DATA and self._download is not None and len(rest) >= 5:
            next_offset = struct.unpack_from("<I", rest, 1)[0]
            if rest[0] == 0 and next_offset < len(self._download):
                self.send_file_chunk(next_offset)
            else:
                self._download = None

    def decline_create_file(self, frame: wire.GfdiFrame):
        """Refuse uploads to the device (the app pushes a firmware update after sync).

        ACK body per Gadgetbridge's CreateFileStatusMessage:
        [create_status][file_index u16][data_type][subtype][file_number u16];
        create_status 3 = UNSUPPORTED.
        """
        p = frame.payload
        data_type, subtype = (p[4], p[5]) if len(p) >= 6 else (0, 0)
        if not self._declined_upload:
            log.info("[h%d] declining file upload (type %#04x/%#04x, %d bytes)", self.handle,
                     data_type, subtype, struct.unpack_from("<I", p)[0] if len(p) >= 4 else 0)
            self._declined_upload = True
        self.send_ack(frame, payload=struct.pack("<BHBBH", 3, 0, data_type, subtype, 0))

    def handle_system_event(self, event: int):
        """Follow-ups the real are10 sends after certain phone events (dtn_are10.pklg)."""
        first = event not in self._seen_events
        self._seen_events.add(event)
        (log.info if first else log.debug)("[h%d] system event %d", self.handle, event)
        if event == EVENT_SYNC_READY and first:
            self.send_time_request()
            self.send_sync(SYNC_AFTER_READY)
        elif event == EVENT_PAIR_COMPLETE:
            self.send_time_request()
            self.send_sync(SYNC_AFTER_PAIR)
        elif event == EVENT_SYNC_COMPLETE and EVENT_PAIR_COMPLETE in self._seen_events:
            self.send_sync(SYNC_AFTER_READY)

    def send_time_request(self):
        reference = struct.pack("<I", int.from_bytes(os.urandom(2), "little"))
        self.send_frame(wire.build_frame(MSG_CURRENT_TIME_REQUEST, reference), "5052 time request")

    def send_sync(self, payload: bytes):
        self.send_frame(wire.build_frame(MSG_SYNCHRONIZATION, payload), f"5037 sync {payload.hex()}")

    def handle_download(self, frame: wire.GfdiFrame):
        index, offset = struct.unpack_from("<HI", frame.payload)
        data = self.device.files.get(index)
        log.debug("[h%d] download file %#06x from %d (%s)", self.handle, index, offset,
                 "unknown" if data is None else f"{len(data)} bytes")
        if data is None:
            self.send_ack(frame, payload=b"\x01" + struct.pack("<I", 0))
            return
        self.send_ack(frame, payload=b"\x00" + struct.pack("<I", len(data)))
        self._download = data
        self.send_file_chunk(offset)

    def send_file_chunk(self, offset: int):
        data = self._download
        chunk = data[offset:offset + FILE_CHUNK]
        crc = wire.crc16(data[:offset + len(chunk)])  # running CRC over the file so far
        payload = struct.pack("<BHI", 0, crc, offset) + chunk
        self.send_frame(wire.build_frame(MSG_FILE_TRANSFER_DATA, payload),
                        f"5004 file @{offset} ({len(chunk)}B)")

    def handle_proto(self, frame: wire.GfdiFrame):
        try:
            chunk = wire.parse_proto_payload(frame.payload)
        except ValueError as e:
            log.warning("[h%d] %s", self.handle, e)
            self.send_ack(frame, wire.STATUS_LENGTH_ERROR)
            return
        self.send_ack(frame, payload=wire.proto_ack_payload(chunk.request_id, chunk.offset))

        data = chunk.data
        if chunk.offset or len(data) < chunk.total_length:  # multi-part message
            buf = self._partial.setdefault(chunk.request_id, bytearray())
            buf += data
            if len(buf) < chunk.total_length:
                return
            data = bytes(self._partial.pop(chunk.request_id))

        if frame.msg_type == wire.MSG_PROTOBUF_REQUEST:
            self.handle_smart(chunk.request_id, data)
        else:
            log.info("[h%d] host proto response #%d: %s", self.handle, chunk.request_id,
                     data.hex())

    def handle_smart(self, request_id: int, data: bytes):
        try:
            smart = pb.decode(data)
        except ValueError as e:
            log.warning("[h%d] undecodable protobuf %s: %s", self.handle, data.hex(), e)
            return

        def reply(pb_bytes, label):
            self.send_proto(pb_bytes, wire.MSG_PROTOBUF_RESPONSE, request_id, label)

        handled = False
        es = pb.get_msg(smart, pb.SMART_EVENT_SHARING)
        if es is not None:
            sub = pb.get_msg(es, 1)
            if sub is not None:
                handled = True
                types = [pb.get_varint(pb.decode(v), 1) or 0 for _, v in sub.get(1, [])]
                log.info("[h%d] subscribe alerts %s", self.handle, types)
                self.subscribed = pb.ALERT_LAUNCH_MONITOR in types
                reply(pb.subscribe_response(types), "subscribe response")
            if pb.get_msg(es, 4) is not None:
                handled = True
                reply(pb.alert_support_response([pb.ALERT_LAUNCH_MONITOR]), "alert support")

        status = pb.get_msg(smart, 8)
        if status is not None and pb.get_msg(status, 2) is not None:
            handled = True
            # Real are10: 8:{3:{1:1 2:<battery %> 3:1}}
            body = pb.f_varint(1, 1) + pb.f_varint(2, self.device.battery) + pb.f_varint(3, 1)
            reply(pb.f_msg(8, pb.f_msg(3, body)), "battery status")

        lm = pb.get_msg(smart, pb.SMART_LAUNCH_MONITOR)
        if lm is not None:
            handled = self.handle_launch_monitor(lm, reply) or handled

        if not handled:
            log.warning("[h%d] unhandled protobuf request #%d: %s", self.handle, request_id,
                        data.hex())

    def handle_launch_monitor(self, lm: pb.Fields, reply) -> bool:
        dev = self.device
        handled = False
        if pb.get_msg(lm, pb.LM_STATUS) is not None:
            handled = True
            reply(pb.lm_response(pb.LM_STATUS, pb.f_msg(1, pb.state_msg(dev.state))), "status")
        if pb.get_msg(lm, pb.LM_WAKE_UP) is not None:
            handled = True
            awake = dev.state != pb.STATE_STANDBY
            status = pb.WAKE_UP_ALREADY_AWAKE if awake else pb.WAKE_UP_SUCCESS
            log.info("[h%d] wake-up (%s)", self.handle, "already awake" if awake else "arming")
            reply(pb.lm_response(pb.LM_WAKE_UP, pb.f_varint(1, status)), "wake-up response")
            if not awake:
                dev.set_state(pb.STATE_INTERFERENCE_TEST)
                dev.set_state(pb.STATE_WAITING)
        if pb.get_msg(lm, pb.LM_TILT) is not None:
            handled = True
            reply(pb.lm_response(pb.LM_TILT, pb.f_msg(1, pb.tilt_msg(0.0, 0.0))), "tilt")
        if pb.get_msg(lm, pb.LM_START_TILT_CAL) is not None:
            handled = True
            reply(pb.lm_response(pb.LM_START_TILT_CAL, pb.f_varint(1, 0)), "tilt cal started")
            # CalibrationStatus{status: IN_BOUNDS, result: SUCCESS}
            self.send_alert(pb.alert_notification(pb.f_msg(4, pb.f_varint(1, 1) + pb.f_varint(2, 0))),
                            "tilt cal done")
        reset = pb.get_msg(lm, pb.LM_RESET_TILT_CAL)
        if reset is not None:
            handled = True
            reply(pb.lm_response(pb.LM_RESET_TILT_CAL, pb.f_varint(1, 3)), "tilt cal reset")
        cfg = pb.get_msg(lm, pb.LM_SHOT_CONFIG)
        if cfg is not None:
            handled = True
            names = ("temperature", "humidity", "altitude", "air_density", "tee_range")
            values = {n: pb.get_float(cfg, i + 1) for i, n in enumerate(names)}
            log.info("[h%d] shot config %s", self.handle, values)
            reply(pb.lm_response(pb.LM_SHOT_CONFIG, pb.f_varint(1, 1)), "shot config ok")
        return handled


def _client_name(client_id: int) -> str:
    """MultiLink client ids are sometimes ASCII (Dome Tee Nero's own client uses one)."""
    raw = client_id.to_bytes(8, "little")
    text = raw.rstrip(b"\x00")
    return f'"{text.decode()}"' if text and all(32 <= b < 127 for b in text) and len(text) > 2 \
        else str(client_id)


class RawSession:
    """A registered MultiLink service we don't understand yet: log everything."""

    subscribed = False
    client_id = 0

    def __init__(self, device: "Are10Device", channel: int, handle: int, service: int):
        self.device, self.channel, self.handle, self.service = device, channel, handle, service

    def start(self):
        pass

    def feed(self, data: bytes):
        log.info("[h%d svc%d] ← %s", self.handle, self.service, data.hex())

    def send_alert(self, pb_bytes: bytes, label: str):
        pass


class RegistrationSession(RawSession):
    """MultiLink service 4. Replies are the real are10's, from dtn_are10.pklg."""

    REPLIES = {bytes.fromhex("00"): bytes.fromhex("00120040"),
               bytes.fromhex("050200"): bytes.fromhex("ff05")}

    def feed(self, data: bytes):
        reply = self.REPLIES.get(data)
        log.info("[h%d svc4] ← %s → %s", self.handle, data.hex(), reply.hex() if reply else "?")
        if reply is not None:
            self.device.notify(self.channel, bytes([self.handle]) + reply)


# Services we accept registration for besides GFDI.
LOGGED_SERVICES = {4: "REGISTRATION"}


class Are10Device:
    """The emulated are10: MultiLink routing, GFDI sessions, and device state."""

    def __init__(self, notify: NotifyFn, identity: Optional[Identity] = None,
                 compressed_headers: bool = False):
        self.notify = notify
        self.identity = identity or Identity()
        # Files served through 5002 DOWNLOAD_REQUEST / 5004 FILE_TRANSFER_DATA.
        self.files = {0xFFFD: self.identity.device_xml, 0x0000: DIRECTORY_FILE}
        self.compressed_headers = compressed_headers
        self.mtu = 23
        self.state = pb.STATE_STANDBY
        self.sessions: Dict[int, "GfdiSession | RawSession"] = {}
        self.next_handle = 1
        self.shot_count = 0
        self._alert_id = 1  # the are10's first alert is #1; it keeps counting across connections
        self.battery = 100

    # ── transport callbacks ──

    def set_mtu(self, mtu: int):
        if mtu != self.mtu:
            log.info("MTU %d", mtu)
            self.mtu = mtu

    def on_write(self, channel: int, data: bytes):
        """Bytes the host wrote to 281x/282x of MultiLink channel `channel`."""
        if not data:
            return
        if data[0] == 0:
            self.handle_management(channel, data)
            return
        session = self.sessions.get(data[0])
        if session is None:
            log.warning("write for unknown handle %d: %s", data[0], data.hex())
            return
        session.feed(data[1:])

    def next_alert_id(self) -> int:
        rid = self._alert_id
        self._alert_id = (self._alert_id + 1) & 0xFFFF
        return rid

    def on_disconnect(self):
        # Handle numbers and alert ids keep counting, as on the real are10.
        log.info("host disconnected; resetting sessions")
        self.sessions.clear()
        self.state = pb.STATE_STANDBY

    # ── MultiLink control plane ──

    def handle_management(self, channel: int, data: bytes):
        req = wire.parse_ml_request(data)
        if req is None:
            log.warning("bad MultiLink control message %s", data.hex())
            return
        if req.type == wire.ML_REGISTER_REQ:
            if req.arg:
                log.info("host asked for reliable (MLR) mode %d; granting plain mode", req.arg)
            if req.service != wire.ML_SERVICE_GFDI and req.service not in LOGGED_SERVICES:
                log.warning("register for unsupported service %d; rejecting", req.service)
                self.notify(channel, wire.build_register_resp(
                    req.client_id, req.service, wire.ML_STATUS_INVALID_SERVICE, 0))
                return
            handle = self.next_handle
            self.next_handle = self.next_handle % 15 + 1
            if req.service == wire.ML_SERVICE_GFDI:
                session = GfdiSession(self, channel, handle)
            elif req.service == 4:
                session = RegistrationSession(self, channel, handle, req.service)
            else:
                session = RawSession(self, channel, handle, req.service)
            session.client_id = req.client_id
            self.sessions[handle] = session
            log.info("registered service %d (%s) on channel %d, handle %d (client %s)",
                     req.service, LOGGED_SERVICES.get(req.service, "GFDI"), channel, handle,
                     _client_name(req.client_id))
            # The are10 ends its service 4 response with 0x01, GFDI's with 0x00.
            self.notify(channel, wire.build_register_resp(
                req.client_id, req.service, wire.ML_STATUS_SUCCESS, handle,
                last=1 if req.service == 4 else 0))
            session.start()
        elif req.type == wire.ML_CLOSE_HANDLE_REQ:
            log.info("close handle %d (service %d)", req.arg, req.service)
            self.sessions.pop(req.arg, None)
            self.notify(channel, wire.build_close_handle_resp(req.client_id, req.service, req.arg))
        elif req.type == wire.ML_CLOSE_ALL_REQ:
            # Several apps share the are10 (e.g. client 4 and the app's own client on different
            # channels); close-all only closes the requesting client's handles.
            mine = [h for h, sess in self.sessions.items() if sess.client_id == req.client_id]
            log.info("close all handles for client %s: %s", _client_name(req.client_id), mine)
            for h in mine:
                del self.sessions[h]
            self.notify(channel, wire.build_close_all_resp(req.client_id, req.service))
        else:
            log.warning("unhandled MultiLink request type %d: %s", req.type, data.hex())

    # The real are10 notifies in 20-byte chunks (MTU 23) even when the link allows more.
    MAX_NOTIFY = 20

    def send_data(self, channel: int, handle: int, data: bytes):
        """Send GFDI bytes as notifications, prefixing every chunk with the handle."""
        step = max(min(self.mtu - 3, self.MAX_NOTIFY) - 1, 1)
        for i in range(0, len(data), step):
            self.notify(channel, bytes([handle]) + data[i:i + step])

    # ── device state and shots ──

    def set_state(self, state: int):
        self.state = state
        for s in list(self.sessions.values()):
            s.send_alert(pb.state_alert(state), f"state {state}")

    @property
    def ready(self) -> bool:
        return any(s.subscribed for s in self.sessions.values()) and self.state == pb.STATE_WAITING

    def push_shot(self, shot: Shot) -> bool:
        """Deliver a shot the way the are10 does. Returns False if no app is armed."""
        if not self.ready:
            log.warning("shot dropped: no subscribed host in WAITING state")
            return False
        self.shot_count += 1
        shot.shot_id = self.shot_count
        self.set_state(pb.STATE_RECORDING)
        self.set_state(pb.STATE_PROCESSING)
        for s in list(self.sessions.values()):
            s.send_alert(pb.metrics_alert(shot), f"shot {shot.shot_id}")
        self.set_state(pb.STATE_WAITING)
        log.info("shot %d sent: %.1f mph, VLA %.1f, HLA %.1f, spin %.0f @ %.1f", shot.shot_id,
                 shot.ball_speed / 0.44704, shot.launch_angle, shot.launch_direction,
                 shot.total_spin, shot.spin_axis)
        return True
