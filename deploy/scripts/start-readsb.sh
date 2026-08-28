#!/bin/sh
set -eu

. /etc/default/readsb-adsb

export ADSB_PREFERRED_SERIAL
device=$(/usr/local/lib/adsb-vhf/rtl-device-mode.sh adsb-device)
echo "Starting readsb with RTL-SDR selector $device" >&2

# Option groups are administrator-controlled shell words from /etc/default.
# shellcheck disable=SC2086
exec /usr/bin/readsb \
  --device-type rtlsdr \
  --device "$device" \
  $RECEIVER_OPTIONS $DECODER_OPTIONS $NET_OPTIONS $JSON_OPTIONS
