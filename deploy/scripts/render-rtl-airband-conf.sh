#!/bin/sh
# Render rtl_airband.conf from AIRMON_RADIO_CHANNELS_JSON in backend.env.
# Runs as root (ExecStartPre=+) so it can read 0640 backend.env without
# loading AIRMON_ADMIN_TOKEN into the rtl-airband process environment.
set -eu

if [ -x /opt/adsb-vhf/.venv/bin/python ]; then
  exec /opt/adsb-vhf/.venv/bin/python -m app.rtl_airband_conf
fi
PYTHONPATH=/opt/adsb-vhf
export PYTHONPATH
exec /usr/bin/python3 -m app.rtl_airband_conf
