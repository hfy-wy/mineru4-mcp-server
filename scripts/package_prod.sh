#!/usr/bin/env bash
# DEV SIDE — package the project for a production deployment.
#
# Usage (from repo root):
#   bash scripts/package_prod.sh            # -> mineru-prod-pkg.tgz in repo root
#   bash scripts/package_prod.sh -o /tmp/x.tgz
#
# Renders the prod environment profile first (single source of endpoint IPs),
# then packs source + systemd unit + rendered env into one tarball.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${2:-$ROOT/mineru-prod-pkg.tgz}"

# 1. render prod profile artifacts (env file for systemd, snippets, skills…)
bash "$ROOT/scripts/render_env.sh" "$ROOT/deploy/env/prod.env" >/dev/null
echo "[pkg] rendered prod artifacts in deploy/generated/"

# 2. pack source + deploy assets + install/verify scripts (no caches)
tar --exclude='__pycache__' --exclude='*.pyc' \
    --transform 's,^,mineru-mcp/,' \
    -czf "$OUT" -C "$ROOT" \
    src pyproject.toml README.md CLAUDE.md .env.example \
    deploy/systemd \
    deploy/docker/Dockerfile \
    deploy/prod_install.sh \
    scripts/prod_verify.sh \
    deploy/generated/mineru-mcp.env.prod.generated \
    deploy/generated/docker-compose.prod.generated.yml

echo "[pkg] wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "[pkg] next: scp it to the server, then run deploy/prod_install.sh there"
