#!/usr/bin/env bash
# Deploy the current working tree to a remote host and (re)build the stack.
#
#   ./scripts/deploy.sh            # sync + build + up
#   ./scripts/deploy.sh --sync     # sync only
#
# The host is expected to have the stack directory with a .env already in
# place (see .env.example). Nothing here prints or copies secrets.
set -euo pipefail

HOST="${XPENGMATE_HOST:?set XPENGMATE_HOST to the ssh target}"
DIR="${XPENGMATE_DIR:-/opt/xpengmate}"
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
