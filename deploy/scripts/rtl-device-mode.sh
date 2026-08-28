#!/bin/sh
set -eu

SYSFS_ROOT=${RTL_SYSFS_ROOT:-/sys/bus/usb/devices}
PREFERRED_ADSB_SERIAL=${ADSB_PREFERRED_SERIAL:-1090}
VHF_SERIAL=${VHF_SERIAL:-0118}

list_rtl_serials() {
  for device in "$SYSFS_ROOT"/*; do
    [ -r "$device/idVendor" ] || continue
    [ -r "$device/idProduct" ] || continue
    [ -r "$device/serial" ] || continue
    [ "$(tr -d '\r\n' < "$device/idVendor")" = "0bda" ] || continue
    product=$(tr -d '\r\n' < "$device/idProduct")
    case "$product" in
      2832|2838) ;;
      *) continue ;;
    esac
    tr -d '\r\n' < "$device/serial"
    printf '\n'
  done
}

serials=$(list_rtl_serials)
count=$(printf '%s\n' "$serials" | awk 'NF { count++ } END { print count + 0 }')

case "${1:-}" in
  adsb-serial)
    if [ "$count" -eq 1 ]; then
      printf '%s\n' "$serials" | awk 'NF { print; exit }'
      exit 0
    fi
    if [ "$count" -gt 1 ] && printf '%s\n' "$serials" | awk -v wanted="$PREFERRED_ADSB_SERIAL" '$0 == wanted { found=1 } END { exit !found }'; then
      printf '%s\n' "$PREFERRED_ADSB_SERIAL"
      exit 0
    fi
    echo "No unambiguous ADS-B RTL-SDR found (count=$count, preferred=$PREFERRED_ADSB_SERIAL)" >&2
    exit 1
    ;;
  vhf-available)
    [ "$count" -ge 2 ] || exit 1
    printf '%s\n' "$serials" | awk -v wanted="$VHF_SERIAL" '$0 == wanted { found=1 } END { exit !found }'
    ;;
  count)
    printf '%s\n' "$count"
    ;;
  *)
    echo "Usage: $0 {adsb-serial|vhf-available|count}" >&2
    exit 2
    ;;
esac
