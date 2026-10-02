#!/usr/bin/env bash
# End-to-end smoke test against a running mineru-selfhosted-mcp + MinerU router.
# Usage:
#   ./smoke_test.sh [file.pdf]                                  # uses MINERU_MCP_URL from deploy/env/*.env if sourced
#   MCP=http://192.168.196.10:7000 ./smoke_test.sh sample.pdf   # explicit override
#   source deploy/env/prod.env && ./smoke_test.sh sample.pdf    # prod profile
set -euo pipefail

MCP="${MCP:-${MINERU_MCP_URL:-http://192.168.196.10:7000}}"
FILE="${1:-}"

say() { printf '\n=== %s ===\n' "$*"; }

say "info"
curl -sf "$MCP/" || true; echo

say "healthz"
curl -sf "$MCP/healthz"; echo

say "job board (F6)"
curl -sf "$MCP/ui" >/dev/null && echo "/ui reachable" || echo "/ui not reachable"
curl -sf "$MCP/ui/api/summary" || true; echo

if [[ -n "$FILE" ]]; then
  say "upload $FILE"
  UPLOAD_JSON=$(curl -sf -F "file=@${FILE}" "$MCP/upload")
  echo "$UPLOAD_JSON"
  UPL_ID=$(echo "$UPLOAD_JSON" | python -c "import sys,json;print(json.load(sys.stdin)['uploads'][0]['upload_id'])")
  echo "handle: $UPL_ID"

  say "handle status"
  curl -sf "$MCP/upload/$UPL_ID/status"; echo

  say "MCP tools"
  echo "Now drive the MCP endpoint ($MCP/mcp) with an MCP client:"
  echo "  submit_parse_job {\"file_sources\": [\"$UPL_ID\"], \"output_formats\": [\"markdown\",\"zip\"]}"
  echo "  query_job_status {\"job_id\": ..., \"wait_seconds\": 30}"
  echo "  download_job_result {\"job_id\": ..., \"output_formats\": [\"markdown\",\"zip\"]}"
  echo "  list_parse_jobs {} ; list_published {}"
else
  say "skip upload (no file given)"
  echo "Usage: MCP=http://host:7000 $0 /path/to/sample.pdf"
fi
