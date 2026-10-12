#!/bin/sh
# Run one pass now, then hand over to cron for the configured schedule.
set -eu
SCHEDULE=$(python3 -c 'import json; print(json.load(open("/data/options.json")).get("schedule") or "*/30 * * * *")')
# cron jobs start with an empty environment, so pass on what the script needs.
umask 077
printf 'export SUPERVISOR_TOKEN=%s\nexport TZ=%s\n' "${SUPERVISOR_TOKEN:-}" "${TZ:-UTC}" > /run/fb_reader.env
echo "$SCHEDULE . /run/fb_reader.env && python3 -u /fb_reader.py > /proc/1/fd/1 2>&1" > /etc/crontabs/root
echo "fb-reader: schedule '$SCHEDULE'"
python3 -u /fb_reader.py
exec crond -f -l 8
