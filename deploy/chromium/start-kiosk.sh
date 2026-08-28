#!/bin/sh
set -eu

URL="${KIOSK_URL:-http://127.0.0.1:8080/}"

exec /usr/bin/chromium \
  --kiosk \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --disable-translate \
  --password-store=basic \
  --ozone-platform-hint=auto \
  "$URL"
