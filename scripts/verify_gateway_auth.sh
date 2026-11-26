#!/usr/bin/env bash
# Verify the MCP Gateway rejects anonymous callers (AC1).
#
# Sends an UNSIGNED MCP `tools/list` request. Passes only on 401/403; any 2xx means the gateway
# is anonymously reachable and must be fixed before anything else. Read-only: it calls no tool.
#
# Usage: scripts/verify_gateway_auth.sh [gateway-endpoint-url]
#   default URL: AGENTCORE_GATEWAY_URL from .env
set -euo pipefail

URL="${1:-}"
if [[ -z "$URL" && -f .env ]]; then
  URL="$(grep -E '^AGENTCORE_GATEWAY_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"')"
fi
[[ -n "$URL" ]] || { echo "usage: $0 <gateway-endpoint-url> (or set AGENTCORE_GATEWAY_URL in .env)" >&2; exit 2; }

BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT
CODE="$(curl -sS -o "$OUT" -D - -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  --data "$BODY" | tee "$OUT.headers" | tail -1)" || CODE="000"   # network error → inconclusive

echo "Anonymous tools/list → HTTP $CODE"
grep -i -E '^(x-amzn-errortype|www-authenticate|x-amzn-requestid):' "$OUT.headers" 2>/dev/null || true
head -c 300 "$OUT" 2>/dev/null; echo
rm -f "$OUT.headers"

case "$CODE" in
  401|403) echo "PASS: anonymous call denied."; exit 0 ;;
  2*)      echo "FAIL: the gateway answered an unsigned call. It is anonymously reachable." >&2; exit 1 ;;
  *)       echo "INCONCLUSIVE: expected 401/403 (wrong URL or gateway not deployed?)." >&2; exit 3 ;;
esac
