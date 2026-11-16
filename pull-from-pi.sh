#!/usr/bin/env bash
# Copy new dashcam data from the Pi to this machine (incremental; safe to run often).
# Usage: bash pull-from-pi.sh user@pi-host [remote_dir] [local_dir]
#   remote_dir default: dev/dashcam/footage (relative to the Pi user's home)
#   local_dir  default: <this repo>/footage
# Hourly via cron:  0 * * * * bash /path/to/pull-from-pi.sh user@pi-host >> /path/to/pull.log 2>&1
set -euo pipefail
PI="${1:?usage: bash pull-from-pi.sh user@pi-host [remote_dir] [local_dir]}"
REMOTE="${2:-dev/dashcam/footage}"
LOCAL="${3:-$(cd "$(dirname "$0")" && pwd)/footage}"
mkdir -p "$LOCAL"

exec 9>"$LOCAL/.pull.lock"
flock -n 9 || { echo "$(date '+%F %T') previous pull still running - skipped"; exit 0; }

# BatchMode: never hang waiting for a password (cron has no terminal)
# .sync_state.json belongs to whichever machine ran sync.py; *.part are unfinished downloads
out=$(rsync -a --partial --timeout=60 --stats \
        -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
        --exclude=.sync_state.json --exclude='*.part' --exclude=.pull.lock \
        "$PI:$REMOTE/" "$LOCAL/" 2>&1) || { echo "$(date '+%F %T') FAILED: $(echo "$out" | tail -2 | tr '\n' ' ')"; exit 1; }

files=$(echo "$out" | sed -n 's/^Number of regular files transferred: //p')
bytes=$(echo "$out" | sed -n 's/^Total transferred file size: \([0-9,]*\).*/\1/p')
echo "$(date '+%F %T') ok: ${files:-0} files, ${bytes:-0} bytes"
