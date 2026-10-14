#!/usr/bin/env python3
"""Change the Owni's club, or show the logger's status.

Usage: owniclub              show club, connection, battery and shot count
       owniclub 7iron        select a club (also: 7, 7i, 3w, 4h, pw, sw, driver, putter)

Selecting the putter is the Owni's putting mode. The choice is remembered
across restarts. Installed on the Pi as /usr/local/bin/owniclub.
"""

import json
import socket
import sys

CONTROL = "/run/owni-logger/control.sock"


def ask(line: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(CONTROL)
        s.sendall((line + "\n").encode())
        reply = b""
        while chunk := s.recv(4096):
            reply += chunk
    return reply.decode().strip()


def main():
    if len(sys.argv) > 2 or sys.argv[1:2] in (["-h"], ["--help"]):
        sys.exit(__doc__.strip())
    try:
        reply = ask(f"club {sys.argv[1]}" if len(sys.argv) == 2 else "status")
    except OSError as e:
        sys.exit(f"owni-logger isn't reachable at {CONTROL} ({e}); is the service running?")
    if reply.startswith("error"):
        sys.exit(reply)
    if len(sys.argv) == 1:
        st = json.loads(reply)
        link = f"connected ({st['state']})" if st["connected"] else "not connected"
        battery = f", battery {st['battery']}%" if st["battery"] is not None else ""
        reply = f"club: {st['club']}\nOwni: {link}{battery}\nshots logged: {st['shots']}"
    print(reply)


if __name__ == "__main__":
    main()
