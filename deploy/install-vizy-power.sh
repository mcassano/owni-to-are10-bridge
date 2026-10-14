#!/usr/bin/env bash
# Install the Vizy enclosure service (light, button, fan) on the Pi.
# Run from the copied repo directory:  sudo ./deploy/install-vizy-power.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"

if ! dpkg -s i2c-tools python3-smbus >/dev/null 2>&1; then
    apt-get update
    apt-get install -y i2c-tools python3-smbus
    apt-get clean
fi

# I2C on GPIO 2/3, where the power board sits. Takes effect now and on every boot.
CONFIG=/boot/firmware/config.txt
grep -q "^dtparam=i2c_arm=on" "$CONFIG" || echo "dtparam=i2c_arm=on" >> "$CONFIG"
echo i2c-dev > /etc/modules-load.d/i2c-dev.conf
[ -e /dev/i2c-1 ] || { dtparam i2c_arm=on; modprobe i2c-dev; }

mkdir -p /opt/vizy-power
install -m 755 "$SRC/vizy_power.py" /opt/vizy-power/
install -m 755 "$SRC/deploy/vizy-power-shutdown" /usr/lib/systemd/system-shutdown/vizy-power
cp "$SRC/deploy/vizy-power.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable vizy-power
systemctl restart vizy-power
python3 /opt/vizy-power/vizy_power.py --once
echo "Installed. Light: green = running, flashing red = shutting down, off = safe to unplug."
echo "Hold the power button ~5 s to shut down. Logs: journalctl -u vizy-power -f"
