#!/bin/sh
set -eu

export ADSB_PREFERRED_SERIAL VHF_SERIAL
device=$(/usr/local/lib/adsb-vhf/rtl-device-mode.sh vhf-device)
echo "Starting acarsdec on RTL-SDR $device" >&2

host=${ACARS_UDP_HOST:-127.0.0.1}
port=${ACARS_UDP_PORT:-5550}
gain=${ACARS_GAIN:-400}
# Space-separated MHz list that fits in one RTL-SDR tuner (~2.4 MHz).
frequencies=${ACARS_FREQUENCIES:-131.525 131.550 131.725 131.825}

set -f
# shellcheck disable=SC2086
exec /usr/bin/acarsdec -N "${host}:${port}" -g "$gain" -r "$device" $frequencies
