#!/bin/sh
set -eu

export ADSB_PREFERRED_SERIAL
device=$(/usr/local/lib/adsb-vhf/rtl-device-mode.sh adsb-device)
echo "Starting readsb with RTL-SDR selector $device" >&2

set -f
# Option groups are administrator-controlled shell words from EnvironmentFile.
# /etc/default/readsb-adsb is systemd EnvironmentFile syntax only (not sourced).
# globbing is disabled so an accidental * in an option cannot expand.
# shellcheck disable=SC2086
exec /usr/bin/readsb \
  --device-type rtlsdr \
  --device "$device" \
  $RECEIVER_OPTIONS $DECODER_OPTIONS $NET_OPTIONS $JSON_OPTIONS
