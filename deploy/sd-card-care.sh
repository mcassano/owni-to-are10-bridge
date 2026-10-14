#!/usr/bin/env bash
# Cut SD card writes and free space on the Pi, so it lasts. Safe to run again.
# Run from the copied repo directory:  sudo ./deploy/sd-card-care.sh
set -euo pipefail

# Swap file: the Pi 4 here has 8 GB of RAM and never swaps; the file is ~460 MB of
# card space that gets written whenever anything does swap.
if dpkg -s dphys-swapfile >/dev/null 2>&1; then
    dphys-swapfile swapoff || true
    dphys-swapfile uninstall || true
    apt-get purge -y dphys-swapfile
fi
rm -f /var/swap

# /tmp in RAM.
if ! grep -q "^tmpfs[[:space:]]\+/tmp[[:space:]]" /etc/fstab; then
    echo "tmpfs /tmp tmpfs defaults,nosuid,nodev,size=256M 0 0" >> /etc/fstab
fi
mountpoint -q /tmp || mount /tmp

# Journal: keep it across reboots (useful after a crash) but small, and flushed
# to the card at most every 5 minutes.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/sd-card.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=32M
SyncIntervalSec=5m
EOF
systemctl restart systemd-journald
journalctl --vacuum-size=32M >/dev/null

# Scheduled jobs that mostly write to the card: package list refreshes (there are no
# automatic upgrades; run "sudo apt update" by hand) and the man page index.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer 2>/dev/null || true

# Services nothing here uses: cellular modems, keyboard hotkeys.
systemctl disable --now ModemManager.service triggerhappy.service triggerhappy.socket 2>/dev/null || true

# A motion-detection camera daemon left from an earlier project; no camera is attached.
if dpkg -s motion >/dev/null 2>&1; then
    systemctl disable --now motion.service || true
    apt-get purge -y motion
fi
rm -rf /var/log/motion /var/lib/motion

apt-get autoremove --purge -y
apt-get clean
df -h /
