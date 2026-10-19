#!/usr/bin/env bash
# Pull the newest XPENG export from the EVConduit host and run the importer.
#
#   ./scripts/sync-history.sh
#
# Runs where SSH access exists to BOTH the EVConduit host and the XpengMate
# host (normally this workstation), because the export zips live on the EVConduit
# host's disk, not in its API. Then it triggers the one-shot importer there.
#
# Environment:
#   EVCONDUIT_HOST   ssh target of the EVConduit host        (default 77.42.77.249)
#   EVCONDUIT_DIR    export root on that host     (default /mnt/volume-ssd/xpeng-exports)
#   EVCONDUIT_USER   user id directory to read                     (required)
#   XPENGMATE_HOST   ssh target of the XpengMate host        (default xpengmate-host)
#   XPENGMATE_DIR    stack directory (default /home/xpengmate/xpengmate)
set -euo pipefail

EVCONDUIT_HOST="${EVCONDUIT_HOST:-77.42.77.249}"
EVCONDUIT_DIR="${EVCONDUIT_DIR:-/mnt/volume-ssd/xpeng-exports}"
EVCONDUIT_USER="${EVCONDUIT_USER:?set EVCONDUIT_USER to the EVConduit account id}"
XPENGMATE_HOST="${XPENGMATE_HOST:-xpengmate-host}"
XPENGMATE_DIR="${XPENGMATE_DIR:-/home/xpengmate/xpengmate}"

echo "== newest export for user $EVCONDUIT_USER"
LATEST="$(ssh "$EVCONDUIT_HOST" "find '$EVCONDUIT_DIR/$EVCONDUIT_USER' -name 'DA*.zip' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-")"
[[ -n "$LATEST" ]] || { echo "no export zip found" >&2; exit 1; }
echo "   $LATEST"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
scp -q "$EVCONDUIT_HOST:$LATEST" "$TMP/export.zip"
ssh "$XPENGMATE_HOST" "mkdir -p '$XPENGMATE_DIR/data/exports'"
scp -q "$TMP/export.zip" "$XPENGMATE_HOST:$XPENGMATE_DIR/data/exports/$(basename "$LATEST")"

echo "== running the importer on $XPENGMATE_HOST"
ssh "$XPENGMATE_HOST" "cd '$XPENGMATE_DIR' && docker-compose run --rm history"
echo "== done"
