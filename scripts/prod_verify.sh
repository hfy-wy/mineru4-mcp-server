#!/usr/bin/env bash
# Post-deploy verification for mineru-selfhosted-mcp.
#
# Usage:
#   bash scripts/prod_verify.sh [MCP_URL] [sample.pdf]
#   bash scripts/prod_verify.sh http://192.168.210.251:7000
#   bash scripts/prod_verify.sh http://127.0.0.1:7000 ./sample.pdf
set -euo pipefail

MCP="${1:-${MINERU_MCP_URL:-http://192.168.210.251:7000}}"
FILE="${2:-}"

say()  { printf '\n=== %s ===\n' "$*"; }
pass() { printf '  PASS %s\n' "$*"; }
fail() { printf '  FAIL %s\n' "$*"; FAILURES=$((FAILURES+1)); }
FAILURES=0

say "1/5 healthz"
if curl -sf "$MCP/healthz" | grep -q '"ok":true'; then pass "/healthz ok"; else fail "/healthz"; fi

say "2/5 info page"
if curl -sf "$MCP/" >/dev/null; then pass "GET / reachable"; else fail "GET /"; fi

say "3/5 job board (F6 journal)"
if curl -sf "$MCP/ui" >/dev/null; then pass "/ui reachable"; else fail "/ui"; fi
if curl -sf "$MCP/ui/api/summary"; then echo; pass "/ui/api/summary"; else fail "/ui/api/summary (journal off or PG down)"; fi

if [[ -n "$FILE" ]]; then
  say "4/5 side-channel upload: $FILE"
  if UPLOAD_JSON=$(curl -sf -F "file=@${FILE}" "$MCP/upload"); then
    echo "$UPLOAD_JSON"
    UPL_ID=$(echo "$UPLOAD_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['uploads'][0]['upload_id'])")
    pass "upload handle: $UPL_ID"
    curl -sf "$MCP/upload/$UPL_ID/status" >/dev/null && pass "handle status endpoint"
  else
    fail "POST /upload"
  fi
else
  say "4/5 upload (skipped — no file given)"
fi

say "5/5 MCP client checks (manual, unprefixed tool names on direct :7000/mcp)"
if [[ -n "${UPL_ID:-}" ]]; then
  cat <<EOF
  Open an MCP client against $MCP/mcp and run:
    submit_parse_job  {"file_sources": ["$UPL_ID"], "output_formats": ["markdown","zip"]}
    query_job_status  {"job_id": "<from submit>", "wait_seconds": 30}
      -> completed returns live download_urls_preview (task-1 feature)
    download_job_result {"job_id": "<…>", "output_formats": ["markdown","zip"]}
      -> download_url must GET 200 via curl
    list_parse_jobs   {}          # board view
  Then confirm the job shows up in the board UI: $MCP/ui
EOF
else
  echo "  (upload a file first to get an upl_ handle for these checks)"
fi

printf '\n=== RESULT: %s ===\n' "$([[ $FAILURES -eq 0 ]] && echo ALL PASS || echo "$FAILURES FAILURE(S)")"
exit $FAILURES
