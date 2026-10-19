#!/usr/bin/env bash
# Install the three XPENG credential files into the engine's data directory.
#
#   ./scripts/install-xpeng-credentials.sh <source-dir>
#
# <source-dir> must contain:
#   ca-chain.crt client-cert-0.pem client-key.pem       (or a certs/ subdirectory)
#   xpeng_tenant.json
#   xpeng_tokens.json
#
# Copy on the machine that runs the stack (or run this there):
#   scp -r xpengtoolkit/custom_components/xpeng/certs xpengmate-host:/tmp/xpeng-certs
#   scp /config/xpeng-secrets/xpeng_{tenant,tokens}.json xpengmate-host:/tmp/
#   ./scripts/install-xpeng-credentials.sh /tmp
#
# Files land 0600 in data/engine/{certs,secrets}. The engine must be restarted
# afterwards:  docker-compose restart engine  (and set XPENG_ALLOW_OFFLINE=false)
set -euo pipefail

SRC="${1:?usage: $0 <source-dir>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERTS="$ROOT/data/engine/certs"
SECRETS="$ROOT/data/engine/secrets"

mkdir -p "$CERTS" "$SECRETS"

install -m 600 "$(find "$SRC" -name 'ca-chain.crt' | head -1)" "$CERTS/ca-chain.crt"
install -m 600 "$(find "$SRC" -name 'client-cert-0.pem' | head -1)" "$CERTS/client-cert-0.pem"
install -m 600 "$(find "$SRC" -name 'client-key.pem' | head -1)" "$CERTS/client-key.pem"
install -m 600 "$(find "$SRC" -name 'xpeng_tenant.json' | head -1)" "$SECRETS/xpeng_tenant.json"
install -m 600 "$(find "$SRC" -name 'xpeng_tokens.json' | head -1)" "$SECRETS/xpeng_tokens.json"

echo "installed:"
ls -l "$CERTS" "$SECRETS"
echo
echo "next: set XPENG_ALLOW_OFFLINE=false in .env, then docker-compose up -d engine"
