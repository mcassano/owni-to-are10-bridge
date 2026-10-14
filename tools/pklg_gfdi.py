#!/usr/bin/env python3
"""Decode MultiLink/GFDI frames from tools/pklg_att.py output.

Usage: python tools/pklg_att.py trace.pklg | python tools/pklg_gfdi.py
Reassembles each MultiLink handle's byte stream per direction, then prints
GFDI frames with protobuf payloads decoded generically.
"""

import os
import re
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from are10emu import pb, wire  # noqa: E402

LINE = re.compile(r"^\s*([\d.]+) (ph→are10|are10→ph) (WRITE_CMD|WRITE_REQ|NOTIFY)\s+0x[0-9a-f]+\s+(28[12]\d)\s+([0-9a-f]+)$")


def pb_dump(data: bytes, depth=0) -> str:
    """Best-effort generic protobuf rendering."""
    try:
        fields = pb.decode(data)
    except Exception:
        return data.hex()
    parts = []
    for num, values in fields.items():
        for wt, v in values:
            if wt == pb.WT_LEN:
                inner = pb_dump(v, depth + 1) if v and depth < 8 else f"'{v.hex()}'"
                if v and all(32 <= b < 127 for b in v) and len(v) > 2:
                    inner = repr(v.decode())
                parts.append(f"{num}:{{{inner}}}")
            elif wt == pb.WT_I32:
                parts.append(f"{num}:{struct.unpack('<f', v)[0]:.4g}f")
            else:
                parts.append(f"{num}:{v}")
    return " ".join(parts)


def describe(frame: wire.GfdiFrame) -> str:
    t, p = frame.msg_type, frame.payload
    txn = f" txn={frame.txn_id}" if frame.txn_id is not None else ""
    if t == wire.MSG_ACK and len(p) >= 3:
        orig, status = struct.unpack_from("<HB", p)
        return f"ACK({orig}) status={status}{txn} rest={p[3:].hex()}"
    if t in (wire.MSG_PROTOBUF_REQUEST, wire.MSG_PROTOBUF_RESPONSE) and len(p) >= 14:
        c = wire.parse_proto_payload(p)
        return f"{t} #{c.request_id} off={c.offset} total={c.total_length}{txn}  {pb_dump(c.data)}"
    if t == wire.MSG_CONFIGURATION:
        return f"5050 caps={wire.parse_capabilities(p)}{txn}"
    text = p.decode(errors="replace") if p and sum(32 <= b < 127 for b in p) > len(p) * 0.8 else p.hex()
    return f"{t}{txn} ({len(p)}B) {text[:300]}"


def main():
    streams = defaultdict(wire.StreamBuffer)
    services = {}  # MultiLink handle → service id, learned from REGISTER responses
    for line in sys.stdin:
        m = LINE.match(line)
        if not m:
            if "===" in line or "SMP" in line:
                print(line.rstrip())
            continue
        ts, direction, _, _char, hexdata = m.groups()
        data = bytes.fromhex(hexdata)
        handle = data[0]
        if handle == 0:
            print(f"{ts:>9} {direction} ML-CTRL {data.hex()}")
            if len(data) >= 14 and data[1] == 1 and data[12] == 0:  # REGISTER_RESP success
                services[data[13]] = struct.unpack_from("<H", data, 10)[0]
                streams.pop((data[13], "ph→are10"), None)
                streams.pop((data[13], "are10→ph"), None)
            continue
        if services.get(handle) != 1:  # non-GFDI service: raw bytes
            print(f"{ts:>9} {direction} SVC{services.get(handle, '?')} h{handle} {data[1:].hex()}")
            continue
        sb = streams[(handle, direction)]
        sb.extend(data[1:])
        try:
            raws = list(sb.frames())
        except ValueError as e:
            print(f"{ts:>9} {direction} h{handle} COBS error {e}; resetting stream")
            sb.buf.clear()
            raws = []
        for raw in raws:
            try:
                frame = wire.parse_frame(raw)
            except ValueError as e:
                print(f"{ts:>9} {direction} h{handle} BAD {raw.hex()} ({e})")
                continue
            print(f"{ts:>9} {direction} h{handle} {describe(frame)}")


if __name__ == "__main__":
    main()
