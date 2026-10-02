"""JobJournal unit tests — SQL logic against a fake pool (no real PostgreSQL)."""
from __future__ import annotations

import asyncio

import pytest

from mineru_selfhosted_mcp.config import Settings
from mineru_selfhosted_mcp.journal import JobJournal, new_journal


class FakeCursor:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def fetchall(self):
        return []

    async def fetchone(self):
        return (0,)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeCursor(self.log)

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self):
        self.log = []

    def connection(self):
        return FakeConn(self.log)

    async def open(self):
        pass


@pytest.fixture
def settings_with_dsn():
    return Settings.from_env({"MINERU_PG_DSN": "postgresql://u:p@127.0.0.1:5432/db"})


def test_disabled_without_dsn(settings_with_dsn):
    j = JobJournal(Settings.from_env({}))
    assert j.enabled is False
    # writes are silent no-ops, never raise
    asyncio.get_event_loop().run_until_complete(
        j.record_job("job_x", state="queued"))


def test_record_job_upsert(settings_with_dsn):
    j = new_journal(settings_with_dsn)
    pool = FakePool()
    j.pool = pool
    j._schema_ready = True
    asyncio.get_event_loop().run_until_complete(
        j.record_job(
            "job_abc", state="queued", client_name="claude-code",
            user_id="alice", user_label="alice", tier="standard",
            ocr_mode="auto", output_formats=["markdown"], file_count=2,
            file_names=["a.pdf", "b.pdf"], pre_errors=1, warnings=2,
            event="submitted", event_message="2 file(s), tier=standard",
        ))
    upsert, event = pool.log
    assert upsert[0].startswith("INSERT INTO mineru_jobs")
    assert "ON CONFLICT (job_id) DO UPDATE" in upsert[0]
    p = upsert[1]
    assert p[0] == "job_abc"
    assert p[1] == "claude-code"
    assert p[2] == "alice"
    assert p[6] == "queued"
    assert p[7] == "standard"
    assert p[11] == ["a.pdf", "b.pdf"]
    assert p[12] == 1  # pre_errors
    assert p[13] == 2  # warnings
    assert event[0].startswith("INSERT INTO mineru_job_events")
    assert event[1] == ("job_abc", "submitted", "2 file(s), tier=standard")


def test_record_progress_and_published(settings_with_dsn):
    j = new_journal(settings_with_dsn)
    pool = FakePool()
    j.pool = pool
    j._schema_ready = True
    asyncio.get_event_loop().run_until_complete(
        j.record_progress("job_abc", state="running",
                          completed=3, failed=1, total=4))
    asyncio.get_event_loop().run_until_complete(
        j.record_published("job_abc", published_files=2,
                           download_urls=["http://x/1.md", "http://x/1.zip"]))
    upd_progress, upd_pub = pool.log
    assert upd_progress[0].startswith("UPDATE mineru_jobs")
    assert upd_progress[1] == ("running", 3, 1, 4, "job_abc")
    assert upd_pub[1] == (2, ["http://x/1.md", "http://x/1.zip"], "job_abc")


def test_write_failure_never_raises(settings_with_dsn, monkeypatch):
    j = new_journal(settings_with_dsn)
    j._schema_ready = True

    class BoomPool:
        def connection(self):
            raise RuntimeError("db down")

    j.pool = BoomPool()
    # must not raise despite the pool blowing up
    asyncio.get_event_loop().run_until_complete(
        j.record_job("job_x", state="queued", event="submitted"))
    assert j._broken_until > 0  # backoff engaged
