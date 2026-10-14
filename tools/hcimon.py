#!/usr/bin/env python3
"""Minimal HCI monitor for Linux (btmon replacement): writes a PacketLogger
(.pklg, little-endian) file readable by tools/pklg_att.py.

Usage: sudo python3 tools/hcimon.py out.pklg [seconds]
"""

import ctypes
import socket
import struct
import sys
import time

AF_BLUETOOTH, BTPROTO_HCI = 31, 1
HCI_DEV_NONE, HCI_CHANNEL_MONITOR = 0xFFFF, 2
# monitor opcode → pklg packet type
PKLG_TYPE = {2: 0x00, 3: 0x01, 4: 0x02, 5: 0x03}  # cmd, event, ACL tx, ACL rx


def open_monitor() -> socket.socket:
    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    addr = struct.pack("<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(sock.fileno(), ctypes.c_char_p(addr), len(addr)) != 0:
        raise OSError(ctypes.get_errno(), "bind to HCI monitor channel failed")
    return sock


def main(path: str, seconds: float = 600):
    sock = open_monitor()
    sock.settimeout(1.0)
    end = time.time() + seconds
    count = 0
    with open(path, "wb") as out:
        while time.time() < end:
            try:
                frame = sock.recv(65536)
            except socket.timeout:
                continue
            opcode, _index, length = struct.unpack_from("<HHH", frame)
            ptype = PKLG_TYPE.get(opcode)
            if ptype is None:
                continue
            data = frame[6:6 + length]
            now = time.time()
            out.write(struct.pack("<IIIB", 9 + len(data), int(now), int(now % 1 * 1e6), ptype) + data)
            out.flush()
            count += 1
    print(f"captured {count} packets")


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 600)
