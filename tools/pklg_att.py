#!/usr/bin/env python3
"""Decode ATT/SMP traffic for one peer from an Apple PacketLogger (.pklg) trace.

Usage: python tools/pklg_att.py TRACE.pklg PEER_ADDRESS   (the real device's BLE address)

Characteristic handles are labelled from the trace's own GATT discovery
(Read By Type responses for characteristic declarations).
"""

import struct
import sys
from collections import defaultdict

ATT_OPS = {
    0x01: "ERROR", 0x02: "MTU_REQ", 0x03: "MTU_RSP", 0x04: "FIND_INFO_REQ", 0x05: "FIND_INFO_RSP",
    0x06: "FIND_BY_TYPE_REQ", 0x07: "FIND_BY_TYPE_RSP", 0x08: "READ_BY_TYPE_REQ",
    0x09: "READ_BY_TYPE_RSP", 0x0A: "READ_REQ", 0x0B: "READ_RSP", 0x0C: "READ_BLOB_REQ",
    0x0D: "READ_BLOB_RSP", 0x10: "READ_BY_GROUP_REQ", 0x11: "READ_BY_GROUP_RSP",
    0x12: "WRITE_REQ", 0x13: "WRITE_RSP", 0x52: "WRITE_CMD", 0x1B: "NOTIFY", 0x1D: "INDICATE",
    0x1E: "CONFIRM",
}
SMP_OPS = {1: "PAIRING_REQ", 2: "PAIRING_RSP", 3: "CONFIRM", 4: "RANDOM", 5: "FAILED",
           6: "ENC_INFO", 7: "CENTRAL_ID", 8: "ID_INFO", 9: "ID_ADDR_INFO", 0x0A: "SIGN_INFO",
           0x0B: "SECURITY_REQ", 0x0C: "PUBLIC_KEY", 0x0D: "DHKEY_CHECK"}


def records(path):
    data = open(path, "rb").read()
    i = 0
    while i + 4 <= len(data):
        (length,) = struct.unpack_from("<I", data, i)
        if length < 9 or i + 4 + length > len(data):
            break
        secs, usecs, ptype = struct.unpack_from("<IIB", data, i + 4)
        yield secs + usecs / 1e6, ptype, data[i + 13:i + 4 + length]
        i += 4 + length


def short_uuid(raw: bytes) -> str:
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    u = raw[::-1].hex()
    return u[4:8] if u.endswith("667b11e3949a0800200c9a66") or u.endswith("00001000800000805f9b34fb") else u


def main(path, peer):
    peer_bytes = bytes.fromhex(peer.replace(":", ""))[::-1]
    conn_handles = set()
    names = {}            # attribute handle → label
    pending_find = {}     # direction-less: last READ_BY_TYPE was for 0x2803?
    reasm = defaultdict(bytes)
    t0 = None
    out = []

    for ts, ptype, pkt in records(path):
        if ptype == 0xFC or ptype == 0xFB:      # text notes from PacketLogger
            continue
        if ptype == 0x01 and len(pkt) > 2 and pkt[0] == 0x3E:   # LE meta event
            sub = pkt[2]
            if sub in (0x01, 0x0A) and len(pkt) >= 13:  # (enhanced) connection complete
                status, handle = pkt[3], struct.unpack_from("<H", pkt, 4)[0]
                addr = pkt[8:14]
                if status == 0 and addr == peer_bytes:
                    conn_handles.add(handle)
                    t0 = t0 or ts
                    out.append((ts, f"=== connected to {peer}, conn handle {handle:#x}"))
            continue
        if ptype == 0x01 and pkt and pkt[0] == 0x05 and len(pkt) >= 6:  # disconnection complete
            handle = struct.unpack_from("<H", pkt, 3)[0]
            if handle in conn_handles:
                out.append((ts, f"=== disconnected, reason {pkt[5]:#04x}"))
                conn_handles.discard(handle)
            continue
        if ptype == 0x01 and pkt and pkt[0] == 0x08 and len(pkt) >= 6:  # encryption change
            handle = struct.unpack_from("<H", pkt, 3)[0]
            if handle in conn_handles:
                out.append((ts, f"=== encryption change status={pkt[2]} enabled={pkt[5]}"))
            continue
        if ptype not in (0x02, 0x03) or len(pkt) < 4:
            continue
        hdr, alen = struct.unpack_from("<HH", pkt)
        handle, pb = hdr & 0x0FFF, (hdr >> 12) & 3
        if handle not in conn_handles:
            continue
        direction = "ph→are10" if ptype == 0x02 else "are10→ph"
        key = (handle, direction)
        reasm[key] = (reasm[key] if pb == 1 else b"") + pkt[4:4 + alen]
        buf = reasm[key]
        if len(buf) < 4:
            continue
        l2len, cid = struct.unpack_from("<HH", buf)
        if len(buf) - 4 < l2len:
            continue
        payload = buf[4:4 + l2len]
        reasm[key] = b""

        if cid == 6:
            op = payload[0]
            out.append((ts, f"{direction} SMP {SMP_OPS.get(op, hex(op))} {payload[1:].hex()}"))
            continue
        if cid != 4 or not payload:
            continue
        op = payload[0]
        name = ATT_OPS.get(op, f"op{op:#04x}")

        if op == 0x08 and len(payload) >= 7:
            pending_find[direction] = short_uuid(payload[5:])
        if op == 0x09 and len(payload) >= 2:
            item = payload[1]
            for j in range(2, len(payload) - item + 1, item):
                rec = payload[j:j + item]
                if item in (7, 21):  # characteristic declaration
                    value_handle = struct.unpack_from("<H", rec, 3)[0]
                    label = short_uuid(rec[5:])
                    names[value_handle] = label
                    names[value_handle + 1] = label + ".cccd"
        if op == 0x05 and len(payload) >= 2:  # find information: descriptors
            fmt = payload[1]
            step = 4 if fmt == 1 else 18
            for j in range(2, len(payload) - step + 1, step):
                h = struct.unpack_from("<H", payload, j)[0]
                u = short_uuid(payload[j + 2:j + step])
                if u == "2902" and h - 1 in names:
                    names[h] = names[h - 1].split(".")[0] + ".cccd"

        if op in (0x12, 0x52, 0x0A, 0x0C, 0x1B, 0x1D) and len(payload) >= 3:
            ah = struct.unpack_from("<H", payload, 1)[0]
            detail = f"{ah:#06x} {names.get(ah, '?'):>10} {payload[3:].hex()}"
        elif op == 0x01 and len(payload) >= 5:
            ah = struct.unpack_from("<H", payload, 2)[0]
            detail = (f"for {ATT_OPS.get(payload[1], hex(payload[1]))} {ah:#06x} "
                      f"{names.get(ah, '?')} err={payload[4]:#04x}")
        else:
            detail = payload[1:].hex()
        out.append((ts, f"{direction} {name:17} {detail}"))

    t0 = t0 or (out[0][0] if out else 0)
    for ts, line in out:
        print(f"{ts - t0:9.3f} {line}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().splitlines()[2])
    main(*sys.argv[1:])
