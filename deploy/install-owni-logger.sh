#!/usr/bin/env bash
# Install the Owni shot logger on the Pi (separate from the are10 emulator).
# Run from the copied repo directory:  sudo ./deploy/install-owni-logger.sh [--enable]
# --enable also starts it now and on boot. It needs its own USB Bluetooth adapter
# (ADAPTER in owni-logger.service): the Pi 4's chip can't be a central (Owni) while the
# iPhone is connected to it as a peripheral (are10).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/owni-logger

mkdir -p "$DEST"
cp -R "$SRC/ownilog" "$SRC/owni_logger.py" "$DEST/"
[ -x "$DEST/.venv/bin/python" ] || python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet dbus-fast

cp "$SRC/deploy/owni-logger.service" /etc/systemd/system/
systemctl daemon-reload
if [ "${1:-}" = "--enable" ]; then
    systemctl enable --now owni-logger
    echo "Installed and started. Logs: journalctl -u owni-logger -f"
else
    echo "Installed (not enabled). Start with: sudo systemctl enable --now owni-logger"
fi
echo "Shots are appended to /var/lib/owni-logger/shots.jsonl"
