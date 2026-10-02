#!/usr/bin/env bash
# Render all deploy/client artifacts from an environment profile.
#
# Usage:
#   ./scripts/render_env.sh deploy/env/test.env   # render TEST artifacts
#   ./scripts/render_env.sh deploy/env/prod.env   # render PROD artifacts
#
# The profile (test.env / prod.env) is the ONLY place endpoint IPs live.
# Generated files are suffixed .generated (gitignored) — commit templates,
# never generated output.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <profile.env>" >&2
  echo "  e.g. $0 deploy/env/test.env" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"

# shellcheck disable=SC1090
source "$PROFILE"

: "${MINERU_MCP_HOST:?MINERU_MCP_HOST not set in profile}"
: "${MINERU_MCP_URL:?MINERU_MCP_URL not set in profile}"
: "${MINERU_UPLOAD_URL:?MINERU_UPLOAD_URL not set in profile}"
: "${LITELLM_MCP_URL:?LITELLM_MCP_URL not set in profile}"

MODE="$(basename "$PROFILE" .env)"   # test | prod
GEN="$ROOT/deploy/generated"
mkdir -p "$GEN"

tmpl() { # tmpl <template-file> <output-file>
  sed -e "s|{{MINERU_MCP_HOST}}|${MINERU_MCP_HOST}|g" \
      -e "s|{{MINERU_MCP_PORT}}|${MINERU_MCP_PORT:-7000}|g" \
      -e "s|{{MINERU_MCP_URL}}|${MINERU_MCP_URL}|g" \
      -e "s|{{MINERU_UPLOAD_URL}}|${MINERU_UPLOAD_URL}|g" \
      -e "s|{{MINERU_DOWNLOADS_URL}}|${MINERU_DOWNLOADS_URL}|g" \
      -e "s|{{MINERU_BOARD_URL}}|${MINERU_BOARD_URL}|g" \
      -e "s|{{MINERU_BASE_URL}}|${MINERU_BASE_URL}|g" \
      -e "s|{{MINERU_PUBLIC_BASE_URL}}|${MINERU_PUBLIC_BASE_URL}|g" \
      -e "s|{{MINERU_PG_DSN}}|${MINERU_PG_DSN}|g" \
      -e "s|{{LITELLM_MCP_URL}}|${LITELLM_MCP_URL}|g" \
      "$1" > "$2"
}

# 1. systemd environment file
tmpl "$ROOT/deploy/systemd/mineru-mcp.env.template" \
     "$GEN/mineru-mcp.env.$MODE.generated"

# 2. docker-compose
tmpl "$ROOT/deploy/docker/docker-compose.yml.template" \
     "$GEN/docker-compose.$MODE.generated.yml"

# 3. liteLLM mcp_servers snippet
tmpl "$ROOT/deploy/litellm/mcp-servers-snippet.yaml.template" \
     "$GEN/mcp-servers-snippet.$MODE.generated.yaml"

# 4. Claude Code client config
tmpl "$ROOT/deploy/clients/claude-code.mcp.json.template" \
     "$GEN/claude-code.mcp.$MODE.generated.json"

# 5. Codex client config
tmpl "$ROOT/deploy/clients/codex.config.toml.template" \
     "$GEN/codex.config.$MODE.generated.toml"

# 6. Skills (whole directory; SKILL.md + reference.md)
SKILL_SRC="$ROOT/skills/mineru-parse"
SKILL_DST="$GEN/mineru-parse.$MODE"
rm -rf "$SKILL_DST"
mkdir -p "$SKILL_DST"
export SKILL_DST
for f in SKILL.md reference.md; do
  tmpl "$SKILL_SRC/$f.template" "$SKILL_DST/$f"
done

echo "Rendered $MODE artifacts into deploy/generated/:"
ls -1 "$GEN" | sed 's/^/  /'
echo
echo "Deploy hints:"
echo "  systemd:  scp deploy/generated/mineru-mcp.env.$MODE.generated <host>:/etc/mineru-mcp.env && systemctl restart mineru-mcp"
echo "  litellm:  merge deploy/generated/mcp-servers-snippet.$MODE.generated.yaml into config.yaml and reload"
echo "  skills:   cp -r deploy/generated/mineru-parse.$MODE ~/.claude/skills/mineru-parse"
echo "  clients:  use deploy/generated/claude-code.mcp.$MODE.generated.json / codex.config.$MODE.generated.toml"
