#!/usr/bin/env bash
# Production install for mineru-selfhosted-mcp on the MinerU host (192.168.210.251).
#
# Two mutually-exclusive modes (matching the two deployment styles of that host):
#
#   sudo bash prod_install.sh --docker     # docker compose (RECOMMENDED — same
#                                          # style as the router/api/webui stack)
#     Requires: docker + docker compose v2, /tmp/mineru-prod-pkg.tgz transferred.
#     Brings up mineru-selfhosted-mcp (:7000) + postgres:16 (journal DB) with
#     volumes for data; tables auto-create on first start.
#
#   sudo bash prod_install.sh --systemd [--with-pg]
#     systemd + venv deployment. --with-pg additionally installs local
#     PostgreSQL (apt/dnf) and creates role mineru + db mineru_mcp.
#
# Both modes are idempotent. Env values come from the rendered
# deploy/generated/mineru-mcp.env.prod.generated / docker-compose.prod.generated.yml
# inside the package (rendered from deploy/env/prod.env — single source of IPs).
set -euo pipefail

PKG="${PKG:-/tmp/mineru-prod-pkg.tgz}"
APP=/opt/mineru-selfhosted-mcp
DATA=/var/lib/mineru-mcp
SVC=mineru-selfhosted-mcp
MODE="${1:-}"
WITH_PG=0

log() { echo "[install] $*"; }
die() { echo "[install] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -f "$PKG" ]] || die "package $PKG not found — transfer it first (see scripts/package_prod.sh)"

case "$MODE" in
  --docker)  log "mode: docker compose (matches the MinerU router/api/webui stack)" ;;
  --systemd) MODE_ARG="${2:-}"; [[ "$MODE_ARG" == "--with-pg" ]] && WITH_PG=1
             log "mode: systemd + venv" ;;
  *) die "usage: sudo bash prod_install.sh --docker | --systemd [--with-pg]" ;;
esac

# ------------------------------------------------------------- unpack
log "unpacking $PKG -> $APP"
mkdir -p "$APP"
tar -xzf "$PKG" -C "$APP" --strip-components=1

# ============================================================ DOCKER MODE
if [[ "$MODE" == "--docker" ]]; then
  command -v docker >/dev/null 2>&1 || die "docker not found on this host"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
  log "compose build + up (mineru-selfhosted-mcp + postgres:16)"
  cd "$APP"
  cp -f deploy/generated/docker-compose.prod.generated.yml docker-compose.yml
  docker compose up -d --build
  log "waiting for /healthz (container boot + first DB table creation)"
  for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:7000/healthz >/dev/null 2>&1; then
      log "healthz OK"
      docker compose ps
      log "DONE (docker). Next:"
      echo "  1. verify:  bash scripts/prod_verify.sh http://127.0.0.1:7000 [sample.pdf]"
      echo "  2. journal DB is the postgre16 container; tables auto-created on start"
      echo "  3. firewall: allow TCP 7000 (agents) — 5432 published only for DBA access"
      echo "  4. liteLLM switch (Admin UI): mcp_servers.mineru_selfhosted"
      echo "     url -> http://192.168.210.251:7000/mcp ; extra_headers: [X-Mineru-User]"
      echo "  5. rollback: point the UI entry back to http://192.168.196.10:7000/mcp"
      exit 0
    fi
    [[ $i -eq 20 ]] && {
      echo "[install] container not healthy; recent logs:" >&2
      docker compose logs --tail 50 mineru-mcp >&2 || true
      exit 1
    }
    sleep 3
  done
fi

# =========================================================== SYSTEMD MODE
log "creating system user + data dirs"
if ! id -u mineru >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin mineru
else
  log "user mineru already exists"
fi
mkdir -p "$DATA/uploads" "$DATA/output"
chown -R mineru:mineru "$DATA"

cd "$APP"
if [[ ! -x .venv/bin/python ]]; then
  log "creating venv"
  python3 --version
  python3 -m venv .venv
fi
log "pip install . (needs internet; offline: pip wheel on dev box, then .venv/bin/pip install <wheel>)"
.venv/bin/pip install --quiet .

log "placing environment file"
if [[ -f deploy/generated/mineru-mcp.env.prod.generated ]]; then
  cp -f deploy/generated/mineru-mcp.env.prod.generated deploy/systemd/mineru-mcp.env
else
  die "rendered env missing from package"
fi
grep -q "MINERU_MCP_TOKEN" deploy/systemd/mineru-mcp.env || cat >> deploy/systemd/mineru-mcp.env <<'EOF'

# PRODUCTION: uncomment to protect /mcp + /upload with a Bearer token.
# If set, direct clients must send Authorization: Bearer <token>, and the
# liteLLM mcp_servers entry needs auth_type: bearer_token + auth_value.
#MINERU_MCP_TOKEN=change-me
EOF

log "installing systemd unit"
cp -f deploy/systemd/mineru-selfhosted-mcp.service /etc/systemd/system/
systemctl daemon-reload

if [[ $WITH_PG -eq 1 ]]; then
  log "installing local PostgreSQL (skip if already provisioned)"
  if command -v psql >/dev/null 2>&1; then
    log "psql already present; provisioning roles/db only"
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq postgresql
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q postgresql-server && postgresql-setup --initdb
  else
    die "no apt/dnf found; install PostgreSQL manually"
  fi
  systemctl enable --now postgresql
  sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='mineru'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER mineru WITH PASSWORD 'mineru';"
  sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='mineru_mcp'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE mineru_mcp OWNER mineru;"
  log "PG ready: role mineru + db mineru_mcp (matches MINERU_PG_DSN)"
fi

log "starting $SVC"
systemctl enable --now "$SVC"
log "waiting for /healthz"
for i in $(seq 1 15); do
  if curl -sf http://127.0.0.1:7000/healthz >/dev/null 2>&1; then
    log "healthz OK"
    break
  fi
  [[ $i -eq 15 ]] && {
    echo "[install] service did not become healthy; recent log:" >&2
    journalctl -u "$SVC" -n 50 --no-pager >&2 || true
    exit 1
  }
  sleep 2
done

log "DONE (systemd). Next steps:"
echo "  1. verify:  bash scripts/prod_verify.sh http://127.0.0.1:7000 [sample.pdf]"
echo "  2. firewall: allow TCP 7000 from the agent subnet (196.x) if enabled"
echo "  3. liteLLM switch (Admin UI, DB-config mode): mcp_servers.mineru_selfhosted"
echo "     url -> http://192.168.210.251:7000/mcp ; add extra_headers: [X-Mineru-User]"
echo "  4. rollback = point the UI entry back to http://192.168.196.10:7000/mcp"
