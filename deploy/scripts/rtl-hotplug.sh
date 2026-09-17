#!/bin/sh
# Re-select ADS-B vs VHF roles after an RTL-SDR is plugged or unplugged.
# SYSTEMD_WANTS starts a unit if it is inactive; it does not restart readsb
# when a second dongle appears, so this oneshot always re-evaluates.
set -eu

MODE=/usr/local/lib/adsb-vhf/rtl-device-mode.sh

systemctl reset-failed readsb-adsb.service rtl-airband.service >/dev/null 2>&1 || true

if "$MODE" adsb-device >/dev/null 2>&1; then
  systemctl restart readsb-adsb.service || systemctl start readsb-adsb.service || true
else
  systemctl stop readsb-adsb.service >/dev/null 2>&1 || true
fi

if "$MODE" vhf-available; then
  systemctl restart rtl-airband.service || systemctl start rtl-airband.service || true
else
  systemctl stop rtl-airband.service >/dev/null 2>&1 || true
fi
