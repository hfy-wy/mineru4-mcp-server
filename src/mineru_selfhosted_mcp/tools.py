"""MCP tool registrations: async task trio + sync one-shot + board/cancel/health.

All tools return plain dicts (never raise). Sources are resolved by
_resolve_sources: ``upl_…`` side-channel handles (lazy V1 upload with sha256
dedup + file_id caching), http(s) URLs (server-side fetch passthrough), and
server-local paths (3-step upload).
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from fastmcp import Context
from pydantic import Field

from .config import Settings
from .filetypes import is_flash_only, is_parseable, is_pdf_or_image, valid_page_range
from .journal import JobJournal
from .publish import download_url as _downloads_url
from .publish import job_output_dir, publish_job_outputs
from .responses import error_entry, format_results, tool_fail, tool_ok
from .upload_store import UploadStore
from .v1_client import MinerUV1AsyncClient, MinerUV1Error

_SOURCES_FIELD = Annotated[list[Union[str, dict[str, str]]], Field(
    description=(
        "Files to parse. Each entry is either: "
        "a plain string — an upload handle (upl_...), an http(s) URL, or a path "
        "on the MCP server host; or a dict {\"source\"/\"upload_id\"/\"url\": "
        "\"...\", \"pages\": \"N-M\"}. Page range (PDF only): \"N\", \"N-M\", "
        "\"1,3,5-7\", \"all\", reverse \"r1\". Duplicates with different ranges allowed. "
        "Examples: [\"upl_ab12cd34ef56gh78\"], "
        "[{\"upload_id\": \"upl_ab12cd34ef56gh78\", \"pages\": \"1-5\"}], "
        "[\"https://example.com/doc.pdf\", \"/data/docs/local.docx\"]"
    )
)]

OCR_LANGUAGES = [
    "ch (Chinese, English, Chinese Traditional)",
    "ch_server (Chinese, English, Chinese Traditional, Japanese)",
    "en (English)",
    "korean (Korean, English)",
    "japan (Chinese, English, Chinese Traditional, Japanese)",
    "chinese_cht (Chinese, English, Chinese Traditional, Japanese)",
    "ta (Tamil, English)",
    "te (Telugu, English)",
    "ka (Kannada)",
    "el (Greek, English)",
    "th (Thai, English)",
    "latin (French, German, Italian, Spanish, Portuguese, Czech, Danish, "
    "Hungarian, Indonesian, Dutch, Norwegian, Polish, Slovak, Swedish, Turkish, "
    "Vietnamese, Finnish, Catalan, Romanian and more)",
    "arabic (Arabic, Persian, Uyghur, Urdu, Pashto, Kurdish, English)",
    "east_slavic (Russian, Belarusian, Ukrainian, English)",
    "cyrillic (Russian, Belarusian, Ukrainian, Bulgarian, Kazakh, Kyrgyz, "
    "Tajik, Macedonian, Tatar, Mongolian and more)",
    "devanagari (Hindi, Marathi, Nepali, Bihari, Sanskrit, English)",
]


def register_tools(mcp, settings: Settings,
                   client: MinerUV1AsyncClient, store: UploadStore,
                   journal: JobJournal | None = None) -> None:

    def _stdio_only_mode() -> bool:
        return os.environ.get("MINERU_TRANSPORT", "stdio") != "streamable-http"

    async def _ctx_identity(ctx: Context | None) -> tuple[str | None, str | None]:
        """Best-effort caller identity: X-Mineru-User header > fastmcp client_id."""
        user_id = None
        try:
            from fastmcp.server.dependencies import get_http_headers
            headers = get_http_headers()
            if headers:
                user_id = headers.get("x-mineru-user") or None
        except Exception:
            pass
        client_name = None
        if ctx is not None:
            try:
                client_name = getattr(ctx, "client_id", None) or None
            except Exception:
                pass
        return user_id, client_name

    async def _journal_submit(
        job: dict[str, Any], entries: list[dict[str, Any]],
        pre_errors: list[dict[str, Any]], warnings: list[str],
        *, tier: str, ocr_mode: str, output_formats: list[str],
        ctx: Context | None, source: str = "mcp",
    ) -> None:
        if journal is None or job is None:
            return
        user_id, client_name = await _ctx_identity(ctx)
        try:
            await journal.record_job(
                str(job.get("job_id")), state=str(job.get("status", "queued")),
                client_name=client_name, user_id=user_id, user_label=user_id,
                transport=os.environ.get("MINERU_TRANSPORT", "stdio"),
                source=source, tier=tier, ocr_mode=ocr_mode,
                output_formats=output_formats, file_count=len(entries),
                file_names=[str(e.get("display_name", "")) for e in entries
                            if e.get("display_name")] or None,
                pre_errors=len(pre_errors), warnings=len(warnings),
                event="submitted",
                event_message=f"{len(entries)} file(s), tier={tier}",
            )
        except Exception:
            pass

    _last_progress: dict[str, tuple] = {}

    async def _journal_progress(job_id: str, job: dict[str, Any]) -> None:
        if journal is None:
            return
        progress = job.get("progress") or {}
        snapshot = (str(job.get("status", "running")),
                    int(progress.get("completed", 0) or 0),
                    int(progress.get("failed", 0) or 0),
                    int(progress.get("total", 0) or 0))
        # skip identical writes: a long poll otherwise issues the same UPDATE
        # every cycle (~120x for a 1h job) with no information gain
        if _last_progress.get(job_id) == snapshot:
            return
        _last_progress[job_id] = snapshot
        try:
            await journal.record_progress(
                job_id, state=snapshot[0], completed=snapshot[1],
                failed=snapshot[2], total=snapshot[3],
            )
        except Exception:
            pass

    async def _journal_published(
        job_id: str, rows: list[dict[str, Any]],
    ) -> None:
        if journal is None:
            return
        urls = [r.get("download_url") for r in rows
                if r.get("download_url")]
        try:
            await journal.record_published(
                job_id, published_files=len(urls), download_urls=[str(u) for u in urls])
            if rows:
                await journal.record_event(
                    job_id, "published", f"{len(urls)} artifact download(s) served")
        except Exception:
            pass

    async def _resolve_and_submit(
        sources: list[str], ranges: dict[int, str], *,
        tier: str, ocr_mode: str, output_formats: list[str],
        ctx: Context | None,
    ):
        """Resolve sources then submit; retry once on 404 (stale cached file_id).

        Returns (job_or_None, entries, pre_errors, warnings).
        Raises MinerUV1Error only when the final submit fails.
        """
        entries, pre_errors, warnings = await _resolve_sources(
            sources, ranges, settings, client, store, ctx)
        if not entries:
            return None, entries, pre_errors, warnings
        try:
            job = await client.submit_job(
                entries, tier=tier, ocr_mode=ocr_mode,
                output_formats=output_formats)
            return job, entries, pre_errors, warnings
        except MinerUV1Error as exc:
            if exc.status_code != 404:
                raise
        # api-server purged cached files: drop v1_file_id caches, re-upload, retry
        for rec in store.list(limit=10_000):
            if rec.v1_file_id:
                store.clear_v1_file_id(rec.upload_id)
        entries, pre_errors, warnings = await _resolve_sources(
            sources, ranges, settings, client, store, ctx)
        if not entries:
            return None, entries, pre_errors, warnings
        job = await client.submit_job(
            entries, tier=tier, ocr_mode=ocr_mode,
            output_formats=output_formats)
        return job, entries, pre_errors, warnings

    # ── sync one-shot ─────────────────────────────────────────────────────

    @mcp.tool(
        title="Parse documents to Markdown (sync)",
        description=(
            "One-shot synchronous parse: resolve sources -> submit job -> poll "
            "-> download artifacts. Returns markdown content inline (capped) "
            "plus saved paths / download URLs. "
            "For long waits prefer the async trio (submit_parse_job -> "
            "query_job_status -> download_job_result)."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "openWorldHint": True},
    )
    async def parse_documents(
        file_sources: _SOURCES_FIELD,
        tier: Literal["flash", "basic", "standard", "advanced"] = "standard",
        ocr_mode: Literal["auto", "txt", "ocr"] = "auto",
        output_format: Literal["markdown", "middle_json", "structured_content", "zip"] = "markdown",
        poll_timeout_seconds: int = settings.poll_timeout_seconds,
        ctx: Context = None,
    ) -> dict[str, Any]:
        sources, ranges = _normalize_file_sources(file_sources)
        if not sources:
            return format_results([], message_parts=["No file sources given."])
        entries: list[dict[str, Any]] = []
        all_results: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            job, entries, pre_errors, warnings = await _resolve_and_submit(
                sources, ranges, tier=tier, ocr_mode=ocr_mode,
                output_formats=[output_format], ctx=ctx)
            all_results.extend(pre_errors)
            if job is not None:
                if ctx:
                    await ctx.info(f"Job submitted: {job.get('job_id')}, waiting...")
                await _journal_submit(job, entries, pre_errors, warnings,
                                      tier=tier, ocr_mode=ocr_mode,
                                      output_formats=[output_format], ctx=ctx)
                job = await _poll_loop(client, str(job.get("job_id")),
                                       poll_timeout_seconds, ctx,
                                       on_snapshot=_journal_progress)
                all_results.extend(await publish_job_outputs(
                    settings, client, job, [output_format]))
                await _journal_published(str(job.get("job_id")), all_results)
        except (MinerUV1Error, asyncio.TimeoutError) as exc:
            all_results.extend(_error_rows_for_entries(entries, str(exc)))
            warnings.append(f"parse failed: {exc}")
        urls = [r.get("download_url") for r in all_results if r.get("download_url")]
        return format_results(all_results, message_parts=warnings,
                              download_urls=[str(u) for u in urls])

    # ── async task trio + board ───────────────────────────────────────────

    @mcp.tool(
        title="Submit parse job (async)",
        description=(
            "Upload/collect sources and submit a parse job; returns job_id "
            "IMMEDIATELY without waiting. Then query_job_status(job_id) to "
            "watch progress and download_job_result(job_id) for artifacts. "
            "Batch: up to 100 files in one job."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "openWorldHint": True},
    )
    async def submit_parse_job(
        file_sources: _SOURCES_FIELD,
        tier: Literal["flash", "basic", "standard", "advanced"] = "standard",
        ocr_mode: Literal["auto", "txt", "ocr"] = "auto",
        output_formats: list[Literal[
            "markdown", "middle_json", "structured_content", "zip"]] = ["markdown"],
        ctx: Context = None,
    ) -> dict[str, Any]:
        sources, ranges = _normalize_file_sources(file_sources)
        if not sources:
            return tool_fail(status="no_valid_files",
                             error="no file sources given")
        if len(sources) > settings.max_files_per_job:
            jobs_needed = -(-len(sources) // settings.max_files_per_job)
            return tool_fail(
                status="too_many_files",
                error=f"{len(sources)} sources exceed the {settings.max_files_per_job} "
                      f"per-job limit; split into ~{jobs_needed} jobs.",
            )
        try:
            job, entries, pre_errors, warnings = await _resolve_and_submit(
                sources, ranges, tier=tier, ocr_mode=ocr_mode,
                output_formats=output_formats, ctx=ctx)
        except MinerUV1Error as exc:
            return tool_fail(status="submit_failed", error=str(exc))
        if job is None:
            return tool_fail(status="no_valid_files", failures=pre_errors,
                             warnings=warnings,
                             error="no usable file sources after validation")
        await _journal_submit(job, entries, pre_errors, warnings,
                              tier=tier, ocr_mode=ocr_mode,
                              output_formats=output_formats, ctx=ctx)
        return tool_ok(
            job_id=job.get("job_id"),
            status=job.get("status", "queued"),
            file_count=len(entries),
            failures=pre_errors,
            warnings=warnings,
            output_formats=output_formats,
            next_step="call query_job_status(job_id) to watch progress; "
                      "when completed/partial call download_job_result(job_id)",
        )

    @mcp.tool(
        title="Query job status / progress",
        description=(
            "Query a parse job by job_id. wait_seconds=0 returns one snapshot; "
            "wait_seconds>0 polls with progress notifications (backoff 2s -> "
            "30s) until terminal or timeout. Statuses: queued/running/"
            "completed/partial/failed/canceled."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def query_job_status(
        job_id: str,
        wait_seconds: int = 0,
        ctx: Context = None,
    ) -> dict[str, Any]:
        try:
            if wait_seconds > 0:
                job = await _poll_loop(client, job_id, wait_seconds, ctx,
                                       on_snapshot=_journal_progress)
            else:
                job = await client.get_job(job_id)
                await _journal_progress(job_id, job)
        except asyncio.TimeoutError:
            try:
                job = await client.get_job(job_id)
            except MinerUV1Error as exc:
                return tool_fail(job_id=job_id, error=str(exc),
                                 note="poll timed out; final snapshot failed too")
        except MinerUV1Error as exc:
            return tool_fail(job_id=job_id, error=str(exc))
        summary = _job_summary(job, job_id, settings.public_base_url)
        if summary["status"] in ("completed", "partial"):
            # Auto-publish on the polling path so the announced links are real —
            # but only once: re-publishing creates <stem>_1.md duplicates and
            # overwrites the journal's download_urls (losing zip/JSON links).
            # Guard on the FILESYSTEM (output dir already has the artifacts),
            # not the journal — journal must not be load-bearing for behavior.
            job_out_dir = Path(settings.output_dir) / job_id
            already = job_out_dir.is_dir() and any(job_out_dir.glob("*.md"))
            try:
                rows = [] if already else (
                    await publish_job_outputs(
                        settings, client, job,
                        ["markdown"]) if wait_seconds > 0 else [])
            except Exception:
                rows = []
            if rows:
                summary["download_urls_preview"] = [
                    str(r["download_url"]) for r in rows
                    if r.get("status") == "success" and r.get("download_url")]
                if summary["download_urls_preview"]:
                    summary["next_step"] = (
                        "artifacts published; markdown links above are live — "
                        "give them to the user (call download_job_result for "
                        "zip/JSON formats)")
                await _journal_published(job_id, rows)
            else:
                summary["download_urls_preview"] = _artifact_urls_from_job(
                    job, settings.public_base_url)
                if not already and wait_seconds == 0:
                    summary["download_hint"] = (
                        "links below are PREDICTED paths, not yet published — "
                        "call download_job_result (or re-query with "
                        "wait_seconds>0) to publish artifacts and get live URLs")
        return summary

    @mcp.tool(
        title="List parse jobs (task board)",
        description="List recent parse jobs (cursor-paginated, newest first). "
                    "Use after=last_id to page through history. Rows carry "
                    "job_id/status/created_at/file_count; call "
                    "query_job_status for per-file detail.",
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def list_parse_jobs(
        limit: int = 20,
        after: str | None = None,
        status: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        try:
            data = await client.list_jobs(limit=limit, after=after, status=status)
        except MinerUV1Error as exc:
            return tool_fail(error=str(exc))
        board = []
        for j in data.get("data") or []:
            board.append({
                "job_id": j.get("job_id"),
                "status": j.get("status"),
                "created_at": j.get("created_at"),
                "file_count": j.get("file_count"),
            })
        return tool_ok(
            count=len(board),
            jobs=board,
            has_more=data.get("has_more", False),
            last_id=data.get("last_id"),
            next_step=("pass this last_id as after to see older jobs"
                       if data.get("has_more")
                       else "use query_job_status(job_id) for per-file detail"),
        )

    @mcp.tool(
        title="Cancel parse job",
        description="Cancel a queued/running parse job by job_id.",
        annotations={"readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": False},
    )
    async def cancel_parse_job(job_id: str) -> dict[str, Any]:
        try:
            await client.cancel_job(job_id)
            job = await client.get_job(job_id)
            if journal is not None:
                try:
                    # update the board row too, not just the event timeline —
                    # otherwise a canceled job shows running on the board forever
                    await journal.record_job(job_id, state="canceled")
                    await journal.record_event(job_id, "canceled",
                                               f"cancel_parse_job -> {job.get('status')}")
                except Exception:
                    pass
            return tool_ok(job_id=job_id, status=job.get("status"))
        except MinerUV1Error as exc:
            return tool_fail(job_id=job_id, error=str(exc))

    @mcp.tool(
        title="Download job result",
        description=(
            "Download artifacts of a completed/partial job into the output "
            "directory. zip output is saved as .zip AND extracted (markdown + "
            "images with fixed refs); markdown output is saved and inlined "
            "(content capped at 20k chars; full text at extract_path)."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def download_job_result(
        job_id: str,
        output_formats: list[Literal[
            "markdown", "middle_json", "structured_content", "zip"]] = ["markdown"],
        ctx: Context = None,
    ) -> dict[str, Any]:
        try:
            job = await client.get_job(job_id)
            status = job.get("status")
            if status not in ("completed", "partial"):
                return tool_fail(
                    job_id=job_id, status=status,
                    error="job not ready; call query_job_status first")
            if ctx:
                await ctx.info(f"Downloading artifacts for {job_id}...")
            rows = await publish_job_outputs(settings, client, job, output_formats)
            await _journal_published(job_id, rows)
            env = format_results(rows, job_id=job_id)
            env["job_dir"] = str(job_output_dir(settings, job_id))
            env["next_step"] = "give the user the download_url / saved paths above"
            return env
        except MinerUV1Error as exc:
            return tool_fail(job_id=job_id, error=str(exc))

    # ── info tools ────────────────────────────────────────────────────────

    @mcp.tool(
        title="List OCR language codes",
        description="Supported OCR/script language codes (informational).",
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def get_ocr_languages() -> dict[str, Any]:
        return tool_ok(status="success", languages=OCR_LANGUAGES,
                       note="informational only — the V1 job API has no "
                            "language field; OCR languages are configured "
                            "server-side")

    @mcp.tool(
        title="MinerU service health",
        description="Health, advertised tiers, sources and output formats of "
                    "the self-hosted MinerU service.",
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def mineru_health() -> dict[str, Any]:
        try:
            data = await client.health()
        except Exception as exc:
            return tool_fail(ok=False, base_url=settings.base_url,
                             error=f"{type(exc).__name__}: {exc}")
        tiers = await client.tiers()
        result: dict[str, Any] = {
            "ok": True,
            "status": data.get("status"),
            "version": data.get("version"),
            "features": data.get("features"),
            "base_url": settings.base_url,
        }
        if tiers is not None:
            result["tiers"] = tiers
        return result

    @mcp.tool(
        title="List uploaded handles",
        description="List side-channel upload handles (upl_...) with state and TTL.",
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def list_uploads(limit: int = 50) -> dict[str, Any]:
        store.sweep_expired()
        rows = [{
            "upload_id": r.upload_id,
            "filename": r.filename,
            "size": r.size,
            "sha256": r.sha256,
            "state": r.state,
            "v1_file_id": r.v1_file_id,
            "expires_at": r.expires_at,
        } for r in store.list(limit)]
        return tool_ok(
            status="success", uploads=rows, count=len(rows),
            upload_hint=(
                "POST the file (multipart, field 'file') to "
                f"{settings.public_base_url}/upload — even when MCP traffic "
                "goes through a liteLLM proxy, binaries go direct to this "
                "host. Then pass the upload_id into file_sources."
            ),
        )

    @mcp.tool(
        title="List published artifacts",
        description="List files published under the output directory (per job).",
        annotations={"readOnlyHint": True, "destructiveHint": False,
                     "openWorldHint": False},
    )
    async def list_published(limit: int = 50) -> dict[str, Any]:
        out_dir = settings.output_dir
        if not out_dir.is_dir():
            return tool_ok(status="success", output_dir=str(out_dir), jobs=[])
        jobs = []
        for job_dir in sorted(out_dir.iterdir(),
                              key=lambda p: p.stat().st_mtime,
                              reverse=True)[:limit]:
            if not job_dir.is_dir():
                continue
            files = []
            for f in sorted(job_dir.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(out_dir)
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "saved_path": str(f),
                        "download_url": _downloads_url(settings, rel),
                    })
            jobs.append({"job_dir": job_dir.name, "files": files})
        return tool_ok(status="success", output_dir=str(out_dir), jobs=jobs)


# ─────────────────── helpers ───────────────────


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _display_name(source: str) -> str:
    if _is_url(source):
        return source.split("?")[0].split("/")[-1] or source
    return Path(source).name


def _normalize_file_sources(
    file_sources: list[Union[str, dict[str, str]]],
) -> tuple[list[str], dict[int, str]]:
    """Flatten entries to (source strings, per-index page ranges)."""
    sources: list[str] = []
    ranges: dict[int, str] = {}
    for entry in file_sources:
        if isinstance(entry, str):
            sources.append(entry)
        elif isinstance(entry, dict):
            value = (entry.get("upload_id") or entry.get("url")
                     or entry.get("source") or "")
            sources.append(str(value))
            pages = entry.get("pages") or entry.get("page_range")
            if pages:
                ranges[len(sources) - 1] = str(pages)
        else:
            sources.append(str(entry))
    return sources, ranges


async def _resolve_sources(
    sources: list[str],
    ranges: dict[int, str],
    settings: Settings,
    client: MinerUV1AsyncClient,
    store: UploadStore,
    ctx: Context | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Normalize all source kinds into V1 job entries.

    Returns (job_entries, pre_errors, warnings). Handles: upl_ side-channel
    (lazy V1 upload + sha256 dedup + file_id cache), URLs (passthrough),
    server-local paths (validate + upload).
    """
    stdio_only = os.environ.get("MINERU_TRANSPORT", "stdio") != "streamable-http"
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    for i, source in enumerate(sources):
        pages = (ranges.get(i) or "all").strip()
        if pages and pages != "all" and not valid_page_range(pages):
            errors.append(error_entry(
                _display_name(source), f"invalid page_range '{pages}'"))
            continue
        try:
            if source.startswith("upl_"):
                if stdio_only:
                    errors.append(error_entry(
                        _display_name(source),
                        "upload handles require the streamable-http transport "
                        "(the side-channel /upload endpoint only exists there); "
                        "use a server-local path or URL instead"))
                    continue
                rec = store.get(source)
                if rec is None:
                    errors.append(error_entry(
                        _display_name(source),
                        f"unknown or expired upload handle: {source}"))
                    continue
                if pages not in ("", "all") and not is_pdf_or_image(rec.filename):
                    warnings.append(
                        f"page_range ignored for non-PDF input {rec.filename}")
                    pages = "all"
                if rec.v1_file_id:
                    entries.append({
                        "source": {"type": "file_id", "file_id": rec.v1_file_id},
                        "page_range": pages if pages != "all" else None,
                        "display_name": rec.filename,
                    })
                    continue
                blob = store.upload_dir / f"{rec.upload_id}.bin"
                if ctx:
                    await ctx.info(f"Uploading {rec.filename} to MinerU...")
                file_id = await client.upload_file(blob, display_name=rec.filename)
                # keep the client-side filename in job rows (V1 names output by source)
                store.mark_consumed(rec.upload_id, file_id)
                entries.append({
                    "source": {"type": "file_id", "file_id": file_id},
                    "page_range": pages if pages != "all" else None,
                    "display_name": rec.filename,
                })
                continue

            if _is_url(source):
                entries.append({
                    "source": {"type": "url", "url": source},
                    "page_range": pages if pages != "all" else None,
                    "display_name": _display_name(source),
                })
                continue

            # server-local path
            p = Path(source).expanduser()
            if not p.is_file():
                errors.append(error_entry(_display_name(source),
                                          f"File not found: {source}"))
                continue
            if not is_parseable(p.name):
                errors.append(error_entry(
                    p.name, f"unsupported extension '{p.suffix}'"))
                continue
            if p.stat().st_size > settings.max_upload_bytes:
                errors.append(error_entry(
                    p.name, f"file too large: {p.stat().st_size} > "
                            f"{settings.max_upload_bytes}"))
                continue
            if pages not in ("", "all") and not is_pdf_or_image(p.name):
                warnings.append(
                    f"page_range ignored for non-PDF input {p.name}")
                pages = "all"
            if is_flash_only(p.name):
                warnings.append(
                    f"{p.name} is a flash-only input; the server will "
                    "normalize the tier to flash")
            if ctx:
                await ctx.info(f"Uploading: {p.name}")
            file_id = await client.upload_file(p)
            entries.append({
                "source": {"type": "file_id", "file_id": file_id},
                "page_range": pages if pages != "all" else None,
                "display_name": p.name,
            })
        except MinerUV1Error as exc:
            errors.append(error_entry(_display_name(source), str(exc)))
        except OSError as exc:
            errors.append(error_entry(_display_name(source),
                                      f"{type(exc).__name__}: {exc}"))
    return entries, errors, warnings


async def _poll_loop(
    client: MinerUV1AsyncClient,
    job_id: str,
    timeout_seconds: int,
    ctx: Context | None,
    on_snapshot=None,
) -> dict[str, Any]:
    """Poll until terminal state; backoff 2s -> 30s; report progress."""
    deadline = time.monotonic() + timeout_seconds
    interval = 2.0
    while True:
        job = await client.get_job(job_id)
        status = job.get("status")
        if on_snapshot is not None:
            try:
                await on_snapshot(job_id, job)
            except Exception:
                pass
        if status in ("completed", "partial", "failed", "canceled"):
            return job
        if time.monotonic() > deadline:
            raise asyncio.TimeoutError(
                f"job {job_id} did not finish within {timeout_seconds}s")
        if ctx:
            progress = job.get("progress") or {}
            try:
                await ctx.report_progress(
                    progress.get("completed", 0), progress.get("total", 0))
            except Exception:
                pass
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 30.0)


def _job_summary(job: dict[str, Any], job_id: str,
                 base_url: str = "") -> dict[str, Any]:
    progress = job.get("progress") or {}
    completed = progress.get("completed", 0)
    failed = progress.get("failed", 0)
    total = progress.get("total", 0)
    status = job.get("status")
    summary: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "status": status,
        "tier": job.get("tier"),
        "progress": {"completed": completed, "failed": failed, "total": total},
        "percent": round(100 * completed / total, 1) if total else None,
        "files": [{
            "name": f.get("name"),
            "status": f.get("status"),
            "output_formats": list((f.get("output_files") or {}).keys()),
            "error": (f.get("error") or {}).get("message"),
        } for f in (job.get("files") or [])],
    }
    if status in ("completed", "partial"):
        summary["next_step"] = "call download_job_result(job_id) to fetch artifacts"
        summary["download_hint"] = (
            "artifacts are ready — download_urls_preview (if present) lists "
            "live published links; when absent, call download_job_result(job_id) "
            "to publish artifacts server-side and get authoritative URLs")
    elif status == "failed":
        summary["next_step"] = "job failed; inspect files[].error for the reason"
    else:
        summary["next_step"] = ("still running; call query_job_status again "
                                "(wait_seconds>0 to wait for completion)")
    return summary


def _artifact_urls_from_job(job: dict[str, Any], base_url: str = "") -> list[str]:
    """Direct /downloads URLs for completed files, built from job metadata.

    Only markdown predictions are exact (publish writes <stem>.md at the job
    dir root); other formats are noted generically. The authoritative links
    come from download_job_result. Predictions for duplicate stems are wrong
    (publish de-dupes as <stem>_1.md) so those are skipped; non-ASCII stems
    are URL-quoted to match publish.py.
    """
    from urllib.parse import quote

    job_id = str(job.get("job_id", ""))
    urls: list[str] = []
    seen_stems: set[str] = set()
    for f in job.get("files") or []:
        if f.get("status") != "completed":
            continue
        out_files = f.get("output_files") or {}
        if not any(isinstance(v, dict) for v in out_files.values()):
            continue
        stem = Path(str(f.get("name") or "file")).stem or "file"
        if stem in seen_stems:
            continue  # duplicate stem -> publish would write <stem>_1.md; skip
        seen_stems.add(stem)
        urls.append(
            f"{base_url}/downloads/{job_id}/{quote(stem)}.md")
    return urls


def _error_rows_for_entries(
    entries: list[dict[str, Any]], message: str,
) -> list[dict[str, Any]]:
    rows = []
    for e in entries:
        src = e.get("source", {})
        name = src.get("url") or src.get("file_id", "file")
        rows.append(error_entry(Path(str(name)).name, message))
    return rows
