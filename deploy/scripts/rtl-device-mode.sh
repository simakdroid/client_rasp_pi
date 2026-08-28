#!/bin/sh
set -eu

SYSFS_ROOT=${RTL_SYSFS_ROOT:-/sys/bus/usb/devices}
PREFERRED_ADSB_SERIAL=${ADSB_PREFERRED_SERIAL:-1090}
VHF_SERIAL=${VHF_SERIAL:-0118}

# USB iSerial and librtlsdr EEPROM serial are not the same string.
# readsb --device matches librtlsdr. With one dongle always use index 0.

list_rtl_devices() {
  for device in "$SYSFS_ROOT"/*; do
    [ -r "$device/idVendor" ] || continue
    [ -r "$device/idProduct" ] || continue
    [ "$(tr -d '\r\n' < "$device/idVendor")" = "0bda" ] || continue
    product=$(tr -d '\r\n' < "$device/idProduct")
    case "$product" in
      2832|2838) ;;
      *) continue ;;
    esac
    if [ -r "$device/serial" ]; then
      serial=$(tr -d '\r\n' < "$device/serial")
    else
      serial=""
    fi
    printf '%s\n' "${serial:-"-"}"
  done
}

devices=$(list_rtl_devices)
if [ -n "$devices" ]; then
  count=$(printf '%s\n' "$devices" | awk 'NF { count++ } END { print count + 0 }')
else
  count=0
fi

has_serial() {
  wanted=$1
  printf '%s\n' "$devices" | awk -v wanted="$wanted" '$0 == wanted { found=1 } END { exit !found }'
}

case "${1:-}" in
  adsb-device)
    if [ "$count" -eq 1 ]; then
      printf '0\n'
      exit 0
    fi
    if [ "$count" -gt 1 ] && has_serial "$PREFERRED_ADSB_SERIAL"; then
      printf '%s\n' "$PREFERRED_ADSB_SERIAL"
      exit 0
    fi
    echo "No unambiguous ADS-B RTL-SDR found (count=$count, preferred=$PREFERRED_ADSB_SERIAL)" >&2
    exit 1
    ;;
  adsb-serial)
    # Kept for diagnostics; not used to open the dongle when only one is present.
    if [ "$count" -eq 1 ]; then
      printf '%s\n' "$devices" | awk '{ print; exit }'
      exit 0
    fi
    if [ "$count" -gt 1 ] && has_serial "$PREFERRED_ADSB_SERIAL"; then
      printf '%s\n' "$PREFERRED_ADSB_SERIAL"
      exit 0
    fi
    echo "No unambiguous ADS-B RTL-SDR found (count=$count, preferred=$PREFERRED_ADSB_SERIAL)" >&2
    exit 1
    ;;
  vhf-available)
    [ "$count" -ge 2 ] || exit 1
    has_serial "$VHF_SERIAL"
    ;;
  count)
    printf '%s\n' "$count"
    ;;
  *)
    echo "Usage: $0 {adsb-device|adsb-serial|vhf-available|count}" >&2
    exit 2
    ;;
esac
