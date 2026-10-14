#!/usr/bin/env bash
# Install the are10 emulator on a Raspberry Pi (Raspberry Pi OS Bookworm or later).
# Run from the copied repo directory:  sudo ./deploy/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/are10emu

for f in identity.json device.xml; do
    [ -f "$SRC/local/$f" ] || { echo "missing local/$f: the device identity (see README)" >&2; exit 1; }
done

if ! dpkg -s bluez python3-venv >/dev/null 2>&1; then
    apt-get update
    apt-get install -y bluez python3-venv
fi

mkdir -p "$DEST"
cp -R "$SRC/are10emu" "$SRC/are10_emulator.py" "$DEST/"
mkdir -p "$DEST/local"
cp "$SRC/local/identity.json" "$SRC/local/device.xml" "$DEST/local/"
install -m 755 "$SRC/send_shot.py" /usr/local/bin/are10shot   # test-shot command
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet dbus-fast

# LE-only (the are10 has no Classic Bluetooth), allow the phone to re-pair
# without manual cleanup if it forgets the bond, and drop BlueZ's built-in
# Device Information service so ours (with the are10's values) is the only one.
CONF=/etc/bluetooth/main.conf
[ -f "$CONF.are10emu-orig" ] || cp "$CONF" "$CONF.are10emu-orig"
set_conf() {  # key value
    sed -i -E "s/^#?\s*$1\s*=.*/$1 = $2/" "$CONF"
    grep -q "^$1 = $2" "$CONF" || sed -i "/^\[General\]/a $1 = $2" "$CONF"
}
set_conf ControllerMode le
set_conf JustWorksRepairing always
set_conf DeviceID false
systemctl restart bluetooth

cp "$SRC/deploy/are10emu.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now are10emu
echo "Installed. Logs: journalctl -u are10emu -f"
