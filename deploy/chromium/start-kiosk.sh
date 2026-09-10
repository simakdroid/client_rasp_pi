#!/bin/sh
set -eu

URL="${KIOSK_URL:-http://127.0.0.1:8080/}"
PROFILE="${KIOSK_PROFILE_DIR:-$HOME/.config/adsb-vhf-chromium}"
mkdir -p "$PROFILE"

wait_for_http() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required to wait for the backend at $URL" >&2
    return 1
  fi
  python3 - "$URL" <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        urllib.request.urlopen(url, timeout=2)
        raise SystemExit(0)
    except (OSError, urllib.error.URLError):
        time.sleep(1)
raise SystemExit(1)
PY
}

wait_for_http || {
  echo "Backend not reachable at $URL after 60s; kiosk will retry" >&2
  exit 1
}

exec /usr/bin/chromium \
  --kiosk \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-infobars \
  --disable-translate \
  --password-store=basic \
  --user-data-dir="$PROFILE" \
  --ozone-platform-hint=auto \
  --force-device-scale-factor=1 \
  --disable-features=OverlayScrollbar \
  "$URL"
