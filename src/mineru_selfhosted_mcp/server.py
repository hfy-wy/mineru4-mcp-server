"""Server assembly: FastMCP instance, FastAPI app, transports, run()."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .journal import JobJournal
from .security import BearerTokenMiddleware
from .sidechannel import register_sidechannel
from .upload_store import UploadStore
from .v1_client import MinerUV1AsyncClient

log = logging.getLogger("mineru-selfhosted-mcp.server")

_SWEEP_INTERVAL = 600  # 10 min


def build_mcp(settings: Settings):
    """Create the FastMCP instance with tools registered; return (mcp, client, store, journal)."""
    from fastmcp import FastMCP

    from .tools import register_tools

    mcp = FastMCP(
        name="MinerU Self-Hosted Document Parser",
        instructions=_instructions(settings.public_base_url),
    )
    client = MinerUV1AsyncClient(settings)
    store = UploadStore(settings.upload_dir, settings.upload_ttl_seconds,
                        settings.max_upload_bytes)
    journal = JobJournal(settings)
    register_tools(mcp, settings, client, store, journal=journal)
    return mcp, client, store, journal


def _instructions(base_url: str) -> str:
    return f"""You are connected to a SELF-HOSTED MinerU 4.0 parsing service \
(not mineru.net cloud). Capabilities: PDF/document extraction, formula \
recognition (LaTeX), table extraction (HTML tables in Markdown), \
full-document parsing, scanned-document OCR, multi-column layout handling.

## Tiers
Use `tier`: "flash" (fastest) / "basic" (small models) /
"standard" (VLM, default and recommended) / "advanced". Office/HTML/EPUB/CSV \
inputs are flash-only (server auto-normalizes); PDF/images run on any tier.

## Output formats (self-hosted V1 API)
Local api-server serves markdown / middle_json / structured_content / zip \
only. Requesting html/latex/docx locally fails with 403 \
("requires an API key" — API-key/cloud only). \
Request zip when the user needs images; request middle_json or \
structured_content for structured JSON.

## Uploading client files — the side-channel (IMPORTANT)
Binaries never go through MCP tool arguments. When the user's file is NOT on \
the MCP server host, POST it (multipart, field "file") to the MCP server's \
upload endpoint: curl -F file=@report.pdf {base_url}/upload \
-> returns upload_id (upl_...). Pass upload_id directly inside file_sources. \
If the MCP traffic goes through a liteLLM proxy, the binary \
upload STILL goes direct to this same /upload — never through the proxy.

## When to call parse tools (call immediately, no confirmation)
- The user gives any local file path, an upload_id, or a document URL
- The user says parse / convert / read / extract / summarize a file
file_sources entries: plain string or {{"source"|"upload_id"|"url": "...", \
"pages": "N", "N-M", "1,3,5-7", "all", "r1"...}}. page_range is PDF-only; \
duplicates with different ranges are allowed.

## Async task model (submit -> query -> download)
- submit_parse_job uploads/collects sources and returns job_id IMMEDIATELY.
- query_job_status(job_id, wait_seconds=N) polls with progress notifications.
- download_job_result(job_id) fetches artifacts (zip is saved AND extracted; \
extract_path points at a flat markdown with image refs fixed).
- list_parse_jobs is the task board; cancel_parse_job cancels.
- parse_documents = one-shot sync (submit+poll+download in one call).
- Batch: <=100 files -> ONE submit_parse_job call; >100 -> split into multiple \
jobs (the tool tells you how many). Same file, many ranges -> duplicate entries.

## After calling
Always relay the response `message` verbatim; always give the user \
`download_url` / `extract_path` from results.
"""


def build_app(settings: Settings) -> FastAPI:
    mcp, client, store, journal = build_mcp(settings)
    settings.output_dir.mkdir(parents=True, exist_ok=True)

    mcp_app = _mcp_asgi_app(mcp)
    mcp_lifespan = getattr(mcp_app, "lifespan", None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        sweeper = asyncio.create_task(_sweep_loop(store, stop))
        try:
            if mcp_lifespan is not None:
                async with mcp_lifespan(app):
                    log.info("MCP session manager lifespan started")
                    yield
            else:
                log.info("MCP app has no lifespan; running without it")
                yield
        finally:
            stop.set()
            sweeper.cancel()
            await client.aclose()

    app = FastAPI(title="MinerU Self-Hosted MCP", version=__version__,
                  lifespan=lifespan)

    register_sidechannel(app, settings, store)
    if settings.webui_enabled:
        from .webui import register_webui
        register_webui(app, journal)
    app.mount("/downloads", StaticFiles(directory=str(settings.output_dir)),
              name="downloads")
    app.mount("/", mcp_app)   # LAST: parent routes match first, MCP at /mcp
    app.add_middleware(BearerTokenMiddleware, token=settings.mcp_token)
    return app


def _mcp_asgi_app(mcp):
    """fastmcp version shim: http_app() (2.11+/3.x) or streamable_http_app()."""
    if hasattr(mcp, "http_app"):
        return mcp.http_app(path="/mcp")
    return mcp.streamable_http_app()


async def _sweep_loop(store: UploadStore, stop: asyncio.Event) -> None:
    try:
        while True:
            removed = store.sweep_expired()
            if removed:
                log.info("swept %d expired uploads", removed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=_SWEEP_INTERVAL)
                break  # stop event set -> exit cleanly
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass


def run(settings: Settings, transport: str, host: str, port: int) -> None:
    os.environ["MINERU_TRANSPORT"] = transport
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if transport == "streamable-http":
        import uvicorn

        app = build_app(settings)
        log.info("HTTP: MCP=%s/mcp  downloads=%s/downloads  board=%s/ui",
                 settings.public_base_url, settings.public_base_url,
                 settings.public_base_url)
        uvicorn_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # psycopg async pool requires SelectorEventLoop; uvicorn's own
            # factory forces ProactorEventLoop on Windows unless loop="none"
            # (loop="none" -> new_event_loop() -> honours the policy).
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            uvicorn_kwargs["loop"] = "none"
        uvicorn.run(app, host=host, port=port,
                    log_level=settings.log_level.lower(), **uvicorn_kwargs)
    else:
        mcp, _client, _store, _journal = build_mcp(settings)
        mcp.run("stdio", show_banner=False)
