"""Frozen settings loaded once from environment variables.

All MINERU_* env vars are read into a single immutable dataclass, replacing
the mutable module-level globals of the reference implementation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    base_url: str = "http://192.168.210.251:8002"   # MinerU router (V1 gateway)
    api_key: str | None = None                       # Bearer for router/api-server
    mcp_token: str | None = None                     # Bearer guarding /mcp + /upload
    upload_dir: Path = Path("./uploads")
    output_dir: Path = Path.home() / "mineru-downloads"
    public_base_url: str = "http://192.168.210.251:7000"
    max_upload_bytes: int = 200 * 1024 * 1024
    max_files_per_job: int = 100
    upload_ttl_seconds: int = 86400
    poll_timeout_seconds: int = 3600
    log_level: str = "INFO"
    pg_dsn: str | None = None                        # PostgreSQL job journal; unset = disabled
    webui_enabled: bool = True                       # /ui job board (needs pg_dsn for data)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = env if env is not None else os.environ
        return cls(
            base_url=env.get("MINERU_BASE_URL", "http://192.168.210.251:8002").rstrip("/"),
            api_key=env.get("MINERU_API_KEY") or None,
            mcp_token=env.get("MINERU_MCP_TOKEN") or None,
            upload_dir=Path(env.get("MINERU_UPLOAD_DIR", "./uploads")).expanduser(),
            output_dir=Path(
                env.get("MINERU_OUTPUT_DIR", str(Path.home() / "mineru-downloads"))
            ).expanduser(),
            public_base_url=env.get("MINERU_PUBLIC_BASE_URL", "http://192.168.210.251:7000").rstrip("/"),
            max_upload_bytes=int(env.get("MINERU_MAX_UPLOAD_BYTES", 200 * 1024 * 1024)),
            max_files_per_job=int(env.get("MINERU_MAX_FILES_PER_JOB", 100)),
            upload_ttl_seconds=int(env.get("MINERU_UPLOAD_TTL_SECONDS", 86400)),
            poll_timeout_seconds=int(env.get("MINERU_POLL_TIMEOUT_SECONDS", 3600)),
            log_level=env.get("MINERU_LOG_LEVEL", "INFO").upper(),
            pg_dsn=env.get("MINERU_PG_DSN") or None,
            webui_enabled=(env.get("MINERU_WEBUI_ENABLED", "1").lower()
                           not in ("0", "false", "no", "off")),
        )

    def with_overrides(self, **overrides: object) -> "Settings":
        """Return a new frozen Settings with the given fields replaced."""
        return replace(self, **overrides)
