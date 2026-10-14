#!/usr/bin/env python3
"""Show the Owni logger's status: connection, battery and shot count.

Usage: ownistatus      (installed on the Pi as /usr/local/bin/ownistatus)
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
    if len(sys.argv) > 1:
        sys.exit(__doc__.strip())
    try:
        reply = ask("status")
    except OSError as e:
        sys.exit(f"owni-logger isn't reachable at {CONTROL} ({e}); is the service running?")
    if reply.startswith("error"):
        sys.exit(reply)
    st = json.loads(reply)
    link = f"connected ({st['state']})" if st["connected"] else "not connected"
    battery = f", battery {st['battery']}%" if st["battery"] is not None else ""
    print(f"Owni: {link}{battery}\nshots logged: {st['shots']}")


if __name__ == "__main__":
    main()
