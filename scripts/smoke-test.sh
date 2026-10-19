#!/usr/bin/env bash
# End-to-end smoke test for a running XpengMate stack.
#
#   ./scripts/smoke-test.sh [base-url] [engine-url] [provider-token]
#
# Defaults match docker-compose.yml. Exits non-zero on the first failure, so it
# is usable as a deployment gate.
set -euo pipefail

BASE="${1:-http://localhost:4100}"
ENGINE="${2:-http://localhost:8201}"
TOKEN="${3:-${XPENG_PROVIDER_TOKEN:-}}"

say() { printf '\033[1;36m== %s\033[0m\n' "$*"; }
ok() { printf '   \033[32mok\033[0m %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

say "engine health"
health="$(curl -fsS "$ENGINE/healthz")" || fail "engine /healthz unreachable"
echo "   $health"

say "engine requires the provider token"
code="$(curl -s -o /dev/null -w '%{http_code}' "$ENGINE/api/1/products")"
[[ "$code" == "401" ]] || fail "expected 401 without a token, got $code"
ok "401 without token"

if [[ -n "$TOKEN" ]]; then
  say "engine provider contract"
  curl -fsS -H "Authorization: Bearer $TOKEN" "$ENGINE/api/1/products" >/dev/null \
    || fail "/api/1/products"
  ok "products"
  curl -fsS -H "Authorization: Bearer $TOKEN" \
    -X POST -H 'Content-Type: application/json' \
    -d '{"grant_type":"refresh_token","refresh_token":null}' \
    "$ENGINE/oauth2/v3/token" >/dev/null || fail "/oauth2/v3/token"
  ok "token endpoint"
else
  echo "   (skipping authed engine checks: no provider token given)"
fi

say "settings page"
scode="$(curl -s -o /tmp/xpengmate-settings.html -w '%{http_code}' "$BASE/xpengmate")"
[[ "$scode" == "200" ]] || fail "settings page returned $scode"
grep -q "XPENG credentials" /tmp/xpengmate-settings.html || fail "settings page rendered without its sections"
ok "settings page (200)"

say "history service"
if [[ -n "$TOKEN" ]]; then
  h="$(curl -fsS -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8100/status")" || fail "history /status"
  python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") else 1)' <<< "$h" \
    || fail "history /status not ok"
  ok "history status"
fi

say "app"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")"
[[ "$code" == "200" || "$code" == "302" ]] || fail "app root returned $code"
ok "app responds ($code)"

if curl -fsS "$BASE/sign_in" -o /dev/null 2>/dev/null; then
  ok "sign-in page reachable"
fi

say "grafana"
gcode="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:3003/api/health" || true)"
[[ "$gcode" == "200" ]] && ok "grafana healthy" || echo "   (grafana not checked: $gcode)"

say "all smoke checks passed"
