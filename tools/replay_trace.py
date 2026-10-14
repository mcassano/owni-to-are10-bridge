#!/usr/bin/env python3
"""Replay the phone's writes from a decoded trace into the emulator and diff its
replies against the real are10's.

Usage: python tools/pklg_att.py trace.pklg > att.txt
       python tools/replay_trace.py att.txt [--until 15.8]
"""

import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from are10emu import wire  # noqa: E402
from are10emu.device import Are10Device, Identity  # noqa: E402

LINE = re.compile(r"^\s*([\d.]+) (ph→are10|are10→ph) (WRITE_CMD|WRITE_REQ|NOTIFY)\s+0x[0-9a-f]+\s+(28[12])(\d)\s+([0-9a-f]+)$")
# Device-initiated messages the emulator doesn't send (time/sync requests).
SKIP_REAL = {5052}  # time request carries a random reference id


def messages(chunks):
    """Turn [(bytes)] notifications into comparable items: control/svc4 raw, GFDI frames."""
    out, streams = [], defaultdict(wire.StreamBuffer)
    for data in chunks:
        if data[0] in (0, 1):
            out.append(("raw", data.hex()))
            continue
        sb = streams[data[0]]
        sb.extend(data[1:])
        try:
            raws = list(sb.frames())
        except ValueError:  # partial frame at the start of the window
            sb.buf.clear()
            continue
        for raw in raws:
            try:
                f = wire.parse_frame(raw)
            except ValueError:
                continue
            if f.msg_type not in SKIP_REAL:
                out.append(("gfdi", f"{f.msg_type} txn={f.txn_id} {f.payload.hex()}"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("att")
    ap.add_argument("--until", type=float, default=15.8)
    ap.add_argument("--since", type=float, default=0.0, help="only compare replies after this time")
    args = ap.parse_args()

    real, events = [], []
    for line in open(args.att):
        if "=== disconnected" in line:
            events.append(("disconnect", float(line.split()[0]), None))
            continue
        m = LINE.match(line)
        if not m or float(m.group(1)) > args.until:
            continue
        ts, data = float(m.group(1)), bytes.fromhex(m.group(6))
        if m.group(2) == "ph→are10":
            events.append(("write", ts, (int(m.group(5)), data)))
        elif ts >= args.since:
            real.append((0, data))

    emitted = []
    dev = Are10Device(lambda ch, d: emitted.append((current_ts, d)), Identity.load())
    current_ts = 0.0
    for kind, ts, arg in events:
        current_ts = ts
        if kind == "disconnect":
            dev.on_disconnect()
        else:
            dev.on_write(*arg)
    emitted = [d for ts, d in emitted if ts >= args.since]

    ours, theirs = messages(emitted), messages([d for _, d in real])
    width = max(len(ours), len(theirs))
    mismatches = 0
    for i in range(width):
        a = ours[i] if i < len(ours) else ("-", "")
        b = theirs[i] if i < len(theirs) else ("-", "")
        same = a == b
        mismatches += not same
        print(f"{'  ' if same else '!!'} emu  {a[1][:110]}")
        if not same:
            print(f"   real {b[1][:110]}")
    print(f"\n{width - mismatches}/{width} device messages identical")


if __name__ == "__main__":
    main()
