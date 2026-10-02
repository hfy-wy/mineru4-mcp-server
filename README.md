# mineru-selfhosted-mcp

MCP server bridging LLM agents (Claude Code, Codex, any MCP client) to a **self-hosted MinerU 4.0** parse service over its V1 HTTP API. Not the mineru.net cloud API — strict passthrough of local formats.

## 功能 / Features

- **异步任务模型** (borrowed from neosun100/mineru-mcp-server): `submit_parse_job` returns `job_id` immediately → `query_job_status` polls with MCP progress notifications → `download_job_result` fetches artifacts. `list_parse_jobs` is the task board, `cancel_parse_job` cancels. `parse_documents` is the one-shot sync path.
- **HTTP 侧信道上传**: binaries never travel inside MCP tool arguments. `POST /upload` (multipart) returns an `upl_…` handle used in `file_sources`. Works through liteLLM proxies — binaries always go direct to this host.
- **产物发布**: artifacts land in `OUTPUT_DIR/<job_id>/`, served at `/downloads/<job_id>/…`. zip outputs are saved **AND** extracted (markdown + `images/`, flat markdown with fixed image refs).
- **双传输**: stdio + streamable-http (`/mcp`). HTTP mode also serves the upload side-channel and static downloads.
- **格式严格透传**: local V1 supports `markdown / middle_json / structured_content / zip` only (html/latex/docx are API-key/cloud-only, local 403).
- **10 tools**: parse_documents, submit_parse_job, query_job_status, list_parse_jobs, cancel_parse_job, download_job_result, get_ocr_languages, mineru_health, list_uploads, list_published.
- **任务日志 + Web 看板 (F6)**: multi-user parse jobs (who, how many files, status, artifact URLs) are journaled to PostgreSQL (`MINERU_PG_DSN`); a read-only board at `/ui` shows per-user stats, job table, detail + event timeline with 5s auto-refresh. Best-effort writes — journal failure never affects parsing; no DSN configured = journal off.

## 部署拓扑 / Topology

```
Claude Code / Codex ──(MCP JSON-RPC)──► liteLLM 196.127:4000/mineru_selfhosted/mcp ─┐
        │  X-Mineru-User: <name>                                          │ (proxy)
        └───────────────(MCP JSON-RPC direct)─────────────────────────────┤
                                                                          ▼
              MCP server  192.168.210.251:7000  (this project, /mcp)
                │  ▲                    │
                │  └── POST /upload     │ V1 HTTP (upload/jobs/files)      ← binaries go HERE, always direct
                ▼                       ▼
              MinerU router 192.168.210.251:8002  ──►  api-server :8000
                │
                └── journal (best-effort) ──► PostgreSQL ──► /ui board (browser)
```

## Quickstart

```bash
pip install -e .
MINERU_BASE_URL=http://192.168.210.251:8002 \
mineru-selfhosted-mcp --transport streamable-http --host 0.0.0.0 --port 7000
```

- MCP endpoint: `http://<host>:7000/mcp`
- Upload: `curl -F file=@report.pdf http://<host>:7000/upload` → `{"upload_id": "upl_…"}`
- Downloads: `http://<host>:7000/downloads/<job_id>/…`
- Job board (F6): `http://<host>:7000/ui` (requires `MINERU_PG_DSN`; read-only)

## Configuration

All `MINERU_*` env vars are documented in `.env.example`; defaults target the LAN topology above.

| Var | Default | Purpose |
|---|---|---|
| `MINERU_BASE_URL` | `http://192.168.210.251:8002` | MinerU router (V1 gateway) |
| `MINERU_API_KEY` | unset | Bearer for router/api-server (`--api-key`) |
| `MINERU_MCP_TOKEN` | unset | Bearer guarding `/mcp` + `/upload`. **Note**: when set, the token is genuinely enforced — every `/mcp` and `/upload` request needs `Authorization: Bearer <token>` (clients, liteLLM `auth_type: bearer_token`). |
| `MINERU_UPLOAD_DIR` | `./uploads` | Side-channel spool |
| `MINERU_OUTPUT_DIR` | `~/mineru-downloads` | Artifacts (served at `/downloads`) |
| `MINERU_PUBLIC_BASE_URL` | `http://192.168.210.251:7000` | Prefix for download/upload URLs in tool responses |
| `MINERU_MAX_UPLOAD_BYTES` | 200MB | Side-channel + local-path cap |
| `MINERU_MAX_FILES_PER_JOB` | 100 | Submit guard |
| `MINERU_UPLOAD_TTL_SECONDS` | 86400 | Upload handle TTL |
| `MINERU_POLL_TIMEOUT_SECONDS` | 3600 | Sync-wait ceiling |
| `MINERU_LOG_LEVEL` | INFO | Logging |
| `MINERU_PG_DSN` | unset (journal off) | PostgreSQL DSN for the multi-user job journal (F6) |
| `MINERU_WEBUI_ENABLED` | `1` | Set `0` to remove the `/ui` board route |

## Client configuration

### Claude Code

```bash
claude mcp add --transport http mineru http://192.168.210.251:7000/mcp
# or via liteLLM:
claude mcp add --transport http mineru_selfhosted http://192.168.196.127:4000/mineru_selfhosted/mcp --header "Authorization: Bearer sk-…"
# NOTE: through liteLLM, tool names are prefixed with the server alias
# (mineru_selfhosted-parse_documents, …); direct :7000/mcp has no prefix.
```

or project `.mcp.json` — see `deploy/clients/claude-code.mcp.json.template`
(render per-environment configs with `scripts/render_env.sh`, below).

### Codex

Add to `~/.codex/config.toml` — see `deploy/clients/codex.config.toml.template`:

```toml
[mcp_servers.mineru]
url = "http://192.168.210.251:7000/mcp"
```

### liteLLM (192.168.196.127:4000)

Merge `deploy/litellm/mcp-servers-snippet.yaml.template` (rendered:
`deploy/generated/mcp-servers-snippet.<mode>.generated.yaml`) into `config.yaml`:

```yaml
mcp_servers:
  mineru_selfhosted:
    url: "http://192.168.196.10:7000/mcp"   # test; prod: http://192.168.210.251:7000/mcp
    transport: "http"
    extra_headers: ["X-Mineru-User"]        # forward client identity to the journal
```

Per-server route: `http://192.168.196.127:4000/mineru_selfhosted/mcp`
(clients authenticate with a liteLLM virtual key:
`Authorization: Bearer sk-…`).

**Tool name prefix**: through the gateway tools appear as
`mineru_selfhosted-parse_documents`, … — direct `:7000/mcp` shows the
unprefixed names.

**Multi-user journal**: `X-Mineru-User` headers only pass through the proxy
when listed in `extra_headers` (verified 2026-09-28); otherwise have each
client connect direct or inject the header gateway-side via `static_headers`.

## Job journal & board (F6)

1. Provision PostgreSQL (same host recommended): `CREATE DATABASE mineru_mcp;`
   — tables auto-create on server startup.
2. Set `MINERU_PG_DSN=postgresql://mineru:mineru@127.0.0.1:5432/mineru_mcp`.
3. Open `http://192.168.210.251:7000/ui`: totals, per-state cards, top users,
   filterable job table, per-job detail with artifact links and an event
   timeline (auto-refresh 5s). `/ui/api/summary|jobs|jobs/{id}` are read-only
   JSON endpoints.
4. Writes are best-effort: DB down ⇒ warnings in logs, parsing unaffected;
   no DSN ⇒ journal disabled and the board says so.

## Skills

- `skills/mineru-parse/` — production skill **templates** (`*.template`).
  Endpoints are NOT hardcoded — render with the environment profile:
  `bash scripts/render_env.sh deploy/env/test.env` (or `prod.env`), then
  copy the rendered skill: `cp -r deploy/generated/mineru-parse.<mode>
  ~/.claude/skills/mineru-parse/` (Claude Code) or
  `.agents/skills/mineru-parse/` (Codex).
- `skills/mineru-parse-test/` — test skill pinned to the WSL test server
  (192.168.196.10:7000), including the multi-user journal notes
  (`X-Mineru-User` header) and the `/ui` board APIs. Copy to
  `.claude/skills/mineru-parse-test/` or `.agents/skills/mineru-parse-test/`.
  Remove it when the production deployment goes live.

## Deployment & environment profiles

**One source of truth for endpoints**: `deploy/env/test.env`
(开发测试 192.168.196.10) and `deploy/env/prod.env` (正式 192.168.210.251).
All deploy/client/skill artifacts are rendered from `{{VAR}}` templates —
never edit IPs in more than one place; to switch environments:

```bash
bash scripts/render_env.sh deploy/env/prod.env   # renders everything into deploy/generated/
# then follow the printed deploy hints (systemd / litellm / skills / clients)
```

- systemd: `deploy/systemd/` (unit + `mineru-mcp.env.template`)
- Docker: `deploy/docker/` (host networking, `/data` volume)
- liteLLM: `deploy/litellm/mcp-servers-snippet.yaml.template`
- Clients: `deploy/clients/*.template`
- Smoke test: `scripts/smoke_test.sh` (`MCP=<url> ./smoke_test.sh [file.pdf]`;
  + `scripts/mock_mineru_v1.py` for offline e2e)

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```

## License

MIT
