"""PostgreSQL job journal: multi-user task records for the web board.

Every write is best-effort: journal failures never fail MCP tool calls or the
side-channel. When MINERU_PG_DSN is unset the journal is a no-op (disabled),
so stdio/local runs without a database behave exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .config import Settings

log = logging.getLogger("mineru-selfhosted-mcp.journal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mineru_jobs (
    id             BIGSERIAL PRIMARY KEY,
    job_id         TEXT UNIQUE NOT NULL,
    client_name    TEXT,
    user_id        TEXT,
    user_label     TEXT,
    transport      TEXT,
    source         TEXT NOT NULL DEFAULT 'mcp',
    state          TEXT NOT NULL DEFAULT 'queued',
    tier           TEXT,
    ocr_mode       TEXT,
    output_formats TEXT[] DEFAULT '{}',
    file_count     INTEGER NOT NULL DEFAULT 0,
    file_names     TEXT[],
    pre_errors     INTEGER NOT NULL DEFAULT 0,
    warnings       INTEGER NOT NULL DEFAULT 0,
    completed_files INTEGER NOT NULL DEFAULT 0,
    failed_files   INTEGER NOT NULL DEFAULT 0,
    total_files    INTEGER NOT NULL DEFAULT 0,
    published_files INTEGER NOT NULL DEFAULT 0,
    download_urls  TEXT[],
    detail         JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mineru_job_events (
    id         BIGSERIAL PRIMARY KEY,
    job_id     TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    event      TEXT NOT NULL,
    message    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON mineru_jobs (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_state   ON mineru_jobs (state);
CREATE INDEX IF NOT EXISTS idx_events_job   ON mineru_job_events (job_id, id);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z"


class JobJournal:
    """Best-effort PostgreSQL journal; disabled when no DSN configured."""

    def __init__(self, settings: Settings) -> None:
        self.dsn = settings.pg_dsn
        self.pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._broken_until = 0.0  # backoff after connection failures

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)

    async def ensure_pool(self) -> Any | None:
        # Respect the failure backoff here too: webui calls this directly, and
        # without this check every board request would block ~30s on a dead PG.
        if time.monotonic() < self._broken_until or not self.dsn:
            return None
        if self.pool is not None:
            return self.pool
        async with self._pool_lock:
            if self.pool is not None:
                return self.pool
            try:
                from psycopg_pool import AsyncConnectionPool

                pool = AsyncConnectionPool(
                    self.dsn, min_size=0, max_size=4, open=False,
                )
                await pool.open()
                if not self._schema_ready:
                    async with pool.connection() as conn:
                        await conn.execute(_SCHEMA)
                    self._schema_ready = True
                self.pool = pool
                log.info("journal connected to PostgreSQL")
                return self.pool
            except Exception as exc:
                log.warning("journal disabled until backoff passes: %s", exc)
                self._broken_until = time.monotonic() + 30.0
                # open() may leave a half-started pool with live workers behind;
                # close it so retries start clean instead of leaking workers.
                try:
                    await pool.close(timeout=1.0)
                except Exception:
                    pass
                return None

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        if not self.enabled or time.monotonic() < self._broken_until:
            return
        try:
            pool = await self.ensure_pool()
            if pool is None:
                return
            async with pool.connection() as conn:
                await conn.execute(sql, params)
        except Exception as exc:
            log.warning("journal write failed (ignored): %s", exc)
            self._broken_until = time.monotonic() + 30.0
            # a failed write usually means the pool itself is broken; drop it
            # (close workers) so the next ensure_pool attempt starts clean.
            if self.pool is not None:
                pool, self.pool = self.pool, None
                try:
                    await pool.close(timeout=1.0)
                except Exception:
                    pass

    # -- writes ----------------------------------------------------------------

    async def record_job(
        self,
        job_id: str,
        *,
        state: str,
        client_name: str | None = None,
        user_id: str | None = None,
        user_label: str | None = None,
        transport: str | None = None,
        source: str = "mcp",
        tier: str | None = None,
        ocr_mode: str | None = None,
        output_formats: list[str] | None = None,
        file_count: int = 0,
        file_names: list[str] | None = None,
        pre_errors: int = 0,
        warnings: int = 0,
        detail: dict[str, Any] | None = None,
        event: str | None = None,
        event_message: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO mineru_jobs
                (job_id, client_name, user_id, user_label, transport, source,
                 state, tier, ocr_mode, output_formats, file_count, file_names,
                 pre_errors, warnings, detail, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
            ON CONFLICT (job_id) DO UPDATE SET
                state = EXCLUDED.state,
                client_name = COALESCE(EXCLUDED.client_name, mineru_jobs.client_name),
                user_id = COALESCE(EXCLUDED.user_id, mineru_jobs.user_id),
                user_label = COALESCE(EXCLUDED.user_label, mineru_jobs.user_label),
                transport = COALESCE(EXCLUDED.transport, mineru_jobs.transport),
                tier = COALESCE(EXCLUDED.tier, mineru_jobs.tier),
                ocr_mode = COALESCE(EXCLUDED.ocr_mode, mineru_jobs.ocr_mode),
                output_formats = COALESCE(EXCLUDED.output_formats, mineru_jobs.output_formats),
                file_count = GREATEST(EXCLUDED.file_count, mineru_jobs.file_count),
                file_names = COALESCE(EXCLUDED.file_names, mineru_jobs.file_names),
                pre_errors = EXCLUDED.pre_errors,
                warnings = EXCLUDED.warnings,
                detail = mineru_jobs.detail || EXCLUDED.detail,
                updated_at = now()
            """,
            (
                job_id, client_name, user_id, user_label, transport, source,
                state, tier, ocr_mode,
                output_formats or [],
                file_count, file_names or [],
                pre_errors, warnings,
                json.dumps(detail or {}),
            ),
        )
        if event:
            await self.record_event(job_id, event, event_message)

    async def record_progress(
        self, job_id: str, *, state: str,
        completed: int = 0, failed: int = 0, total: int = 0,
    ) -> None:
        await self._execute(
            """
            UPDATE mineru_jobs
               SET state = %s,
                   completed_files = GREATEST(%s, mineru_jobs.completed_files),
                   failed_files = GREATEST(%s, mineru_jobs.failed_files),
                   total_files = GREATEST(%s, mineru_jobs.total_files),
                   updated_at = now()
             WHERE job_id = %s
            """,
            (state, completed, failed, total, job_id),
        )

    async def record_published(
        self, job_id: str, *, published_files: int,
        download_urls: list[str],
    ) -> None:
        await self._execute(
            """
            UPDATE mineru_jobs
               SET published_files = %s,
                   download_urls = %s,
                   updated_at = now()
             WHERE job_id = %s
            """,
            (published_files, download_urls, job_id),
        )

    async def record_event(self, job_id: str, event: str,
                           message: str | None = None) -> None:
        await self._execute(
            "INSERT INTO mineru_job_events (job_id, event, message) VALUES (%s,%s,%s)",
            (job_id, event, message),
        )

    async def get_job_row(self, job_id: str) -> dict[str, Any] | None:
        """Best-effort read of one journal row (None on failure/disabled)."""
        if not self.enabled or time.monotonic() < self._broken_until:
            return None
        try:
            pool = await self.ensure_pool()
            if pool is None:
                return None
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT * FROM mineru_jobs WHERE job_id = %s", (job_id,))
                row = await cur.fetchone()
                return dict(row) if row else None
        except Exception:
            return None


def new_journal(settings: Settings) -> JobJournal:
    return JobJournal(settings)


def anon_user_id() -> str:
    """Fallback identity when no header / client id is available."""
    return "anonymous"
