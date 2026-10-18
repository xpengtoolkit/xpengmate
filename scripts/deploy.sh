#!/usr/bin/env bash
# Deploy the current working tree to the `xpengmate-host` host and (re)build the stack.
#
#   ./scripts/deploy-xpengmate-host.sh            # sync + build + up
#   ./scripts/deploy-xpengmate-host.sh --sync     # sync only
#
# The host is expected to have /home/xpengmate/xpengmate with a .env already in
# place (see .env.example). Nothing here prints or copies secrets.
set -euo pipefail

HOST="${XPENGMATE_HOST:-xpengmate-host}"
DIR="${XPENGMATE_DIR:-/home/xpengmate/xpengmate}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== syncing $HERE -> $HOST:$DIR"
COPYFILE_DISABLE=1 tar \
  --exclude='./.git' \
  --exclude='./website/node_modules' \
  --exclude='./data' \
  -czf - -C "$HERE" . 2>/dev/null \
  | ssh "$HOST" "mkdir -p '$DIR' && tar -xzf - -C '$DIR' 2>/dev/null"

[[ "${1:-}" == "--sync" ]] && exit 0

echo "== removing macOS metadata files (they break Grafana provisioning)"
ssh "$HOST" "cd '$DIR' && find . -name '._*' -delete && find . -name '.DS_Store' -delete"

echo "== building images"
ssh "$HOST" "cd '$DIR' && DOCKER_BUILDKIT=1 docker-compose build"

echo "== starting the stack"
ssh "$HOST" "cd '$DIR' && docker-compose up -d"
ssh "$HOST" "cd '$DIR' && docker-compose ps"
