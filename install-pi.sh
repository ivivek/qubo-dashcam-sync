#!/usr/bin/env bash
# Install the dashcam garage watcher on a Raspberry Pi.
# Usage (on the Pi, inside the copied dashcam folder):  sudo bash install-pi.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "== checks"
command -v nmcli >/dev/null || { echo "nmcli missing. Enable NetworkManager: sudo raspi-config -> Advanced -> Network Config -> NetworkManager"; exit 1; }
systemctl is-active --quiet NetworkManager || { echo "NetworkManager not running (see above)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 missing"; exit 1; }
for f in car_watch.py sync.py dashcam-watch.service dashcam.conf.example; do
  [ -f "$DIR/$f" ] || { echo "missing $f in $DIR"; exit 1; }
done
nmcli -t -f DEVICE,TYPE,STATE dev | grep ':wifi:' || { echo "no Wi-Fi adapter found"; exit 1; }
if ip -4 route | grep -q '^192\.168\.1\.0/24'; then
  echo "WARNING: your home LAN uses 192.168.1.0/24 - same subnet as the camera. Sync may fail."
fi

if [ ! -f "$DIR/dashcam.conf" ]; then
  cp "$DIR/dashcam.conf.example" "$DIR/dashcam.conf"
  chmod 600 "$DIR/dashcam.conf"
  [ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER:" "$DIR/dashcam.conf"   # editable without sudo
  echo "== created $DIR/dashcam.conf - edit it (password, Telegram, CAM_IFACE, DEST), then re-run this script"
  exit 0
fi
chmod 600 "$DIR/dashcam.conf"
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER:" "$DIR/dashcam.conf"

echo "== installing service"
sed "s#__DIR__#$DIR#g" "$DIR/dashcam-watch.service" > /etc/systemd/system/dashcam-watch.service
systemctl daemon-reload
systemctl enable --now dashcam-watch.service
sleep 2
systemctl --no-pager --lines=5 status dashcam-watch.service || true
echo
echo "Logs:            journalctl -u dashcam-watch -f"
echo "Test Telegram:   sudo python3 $DIR/car_watch.py --test-telegram"
