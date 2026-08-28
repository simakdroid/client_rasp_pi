#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo sh ./deploy/install.sh [desktop-user]" >&2
  exit 1
fi

DEPLOY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname "$DEPLOY_DIR")
DESKTOP_USER=${1:-${SUDO_USER:-pi}}

apt-get update
apt-get install -y chromium gettext-base python3-venv rtl-sdr

getent group plugdev >/dev/null || groupadd --system plugdev
getent group rtl-airband >/dev/null || groupadd --system rtl-airband
getent group adsb-vhf >/dev/null || groupadd --system adsb-vhf
id readsb >/dev/null 2>&1 || useradd --system --home /nonexistent --shell /usr/sbin/nologin readsb
id rtl-airband >/dev/null 2>&1 || useradd --system --gid rtl-airband --home /nonexistent --shell /usr/sbin/nologin rtl-airband
id adsb-vhf >/dev/null 2>&1 || useradd --system --gid adsb-vhf --home /opt/adsb-vhf --shell /usr/sbin/nologin adsb-vhf
usermod -a -G plugdev readsb
usermod -a -G plugdev rtl-airband

install -Dm0644 "$DEPLOY_DIR/udev/99-adsb-vhf-rtl-sdr.rules" /etc/udev/rules.d/99-adsb-vhf-rtl-sdr.rules
install -Dm0644 "$DEPLOY_DIR/modprobe/blacklist-rtl-sdr.conf" /etc/modprobe.d/blacklist-rtl-sdr.conf
install -Dm0644 "$DEPLOY_DIR/readsb/readsb.default" /etc/default/readsb-adsb
install -Dm0644 "$DEPLOY_DIR/rtl-airband/rtl_airband.conf.in" /etc/rtl_airband.conf.in
install -Dm0644 "$DEPLOY_DIR/systemd/readsb-adsb.service" /etc/systemd/system/readsb-adsb.service
install -Dm0644 "$DEPLOY_DIR/systemd/rtl-airband.service" /etc/systemd/system/rtl-airband.service
install -Dm0644 "$DEPLOY_DIR/systemd/adsb-vhf-backend.service" /etc/systemd/system/adsb-vhf-backend.service
install -Dm0644 "$DEPLOY_DIR/systemd/adsb-kiosk.service" /etc/systemd/user/adsb-kiosk.service
install -Dm0755 "$DEPLOY_DIR/chromium/start-kiosk.sh" /usr/local/lib/adsb-vhf/start-kiosk.sh
install -Dm0755 "$DEPLOY_DIR/scripts/rtl-device-mode.sh" /usr/local/lib/adsb-vhf/rtl-device-mode.sh
install -Dm0755 "$DEPLOY_DIR/scripts/start-readsb.sh" /usr/local/lib/adsb-vhf/start-readsb.sh

# Deploy the Python package and static assets without copying local venv/cache files.
install -d -m0755 -o root -g adsb-vhf /opt/adsb-vhf
cp -a "$PROJECT_DIR/app" "$PROJECT_DIR/data" /opt/adsb-vhf/
install -m0644 "$PROJECT_DIR/pyproject.toml" "$PROJECT_DIR/README.md" /opt/adsb-vhf/
python3 -m venv /opt/adsb-vhf/.venv
/opt/adsb-vhf/.venv/bin/pip install --no-cache-dir /opt/adsb-vhf
chown -R root:adsb-vhf /opt/adsb-vhf
find /opt/adsb-vhf -type d -exec chmod 0755 {} +
find /opt/adsb-vhf -type f -exec chmod u=rw,go=r {} +
find /opt/adsb-vhf/.venv/bin -type f -exec chmod 0755 {} +

install -d -m0755 -o root -g root /etc/adsb-vhf
if [ ! -e /etc/adsb-vhf/rtl-airband.env ]; then
  install -m0640 -o root -g rtl-airband "$DEPLOY_DIR/env/rtl-airband.env.example" /etc/adsb-vhf/rtl-airband.env
fi
if [ ! -e /etc/adsb-vhf/backend.env ]; then
  install -m0640 -o root -g adsb-vhf "$DEPLOY_DIR/env/backend.env.example" /etc/adsb-vhf/backend.env
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
systemctl daemon-reload

if [ -x /usr/bin/readsb ] && [ -x /usr/bin/rtl_airband ]; then
  echo "Binaries found. Edit coordinates, frequencies and credentials, then enable services."
else
  echo "readsb and/or rtl_airband is not installed; follow docs/raspberry-pi-setup.md." >&2
fi

if id "$DESKTOP_USER" >/dev/null 2>&1; then
  HOME_DIR=$(getent passwd "$DESKTOP_USER" | cut -d: -f6)
  PRIMARY_GROUP=$(id -gn "$DESKTOP_USER")
  install -d -m0755 -o "$DESKTOP_USER" -g "$PRIMARY_GROUP" "$HOME_DIR/.config/systemd/user"
  ln -sfn /etc/systemd/user/adsb-kiosk.service "$HOME_DIR/.config/systemd/user/adsb-kiosk.service"
  chown -h "$DESKTOP_USER:$PRIMARY_GROUP" "$HOME_DIR/.config/systemd/user/adsb-kiosk.service"
  echo "For Wayfire autostart, merge deploy/chromium/wayfire-autostart.ini into $HOME_DIR/.config/wayfire.ini."
fi

echo "Reboot before testing the dongles so blacklisted DVB modules are released."
