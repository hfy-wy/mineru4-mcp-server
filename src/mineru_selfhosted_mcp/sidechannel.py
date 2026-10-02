"""HTTP side-channel routes: multipart upload, handle status, info, healthz.

These are plain FastAPI routes (NOT MCP tools) — binaries never travel through
MCP tool arguments or the liteLLM proxy; clients POST files straight to this
server's /upload and pass the returned ``upl_…`` handle to the MCP tools.
"""
from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from . import __version__
from .config import Settings
from .upload_store import UploadRejected, UploadStore, UploadTooLarge

log = logging.getLogger("mineru-selfhosted-mcp.sidechannel")


def register_sidechannel(app, settings: Settings, store: UploadStore) -> None:
    @app.get("/")
    async def info() -> dict[str, Any]:
        store.sweep_expired()
        return {
            "name": "mineru-selfhosted-mcp",
            "version": __version__,
            "mcp_endpoint": f"{settings.public_base_url}/mcp",
            "upload_endpoint": f"{settings.public_base_url}/upload",
            "downloads_prefix": f"{settings.public_base_url}/downloads",
            "mineru_base_url": settings.base_url,
            "upload_hint": (
                "POST the file (multipart, field 'file') to "
                f"{settings.public_base_url}/upload, then pass the returned "
                "upload_id (upl_…) into file_sources of submit_parse_job / "
                "parse_documents."
            ),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/upload")
    async def upload(
        request: Request,
        file: list[UploadFile] = File(...),
        purpose: str = Form("parse"),
    ) -> dict[str, Any]:
        if purpose not in ("parse", "input_image"):
            raise HTTPException(400, f"unsupported purpose '{purpose}'")
        if not file:
            raise HTTPException(400, "no file parts under field 'file'")
        uploads = []
        for part in file:
            try:
                rec = await store.save(part.filename or "unnamed",
                                       _file_stream(part))
            except UploadTooLarge as exc:
                raise HTTPException(413, str(exc))
            except UploadRejected as exc:
                raise HTTPException(415, str(exc))
            uploads.append({
                "upload_id": rec.upload_id,
                "filename": rec.filename,
                "size": rec.size,
                "sha256": rec.sha256,
                "mime_type": rec.mime_type,
                "state": rec.state,
                "expires_at": rec.expires_at,
                "next_step": "pass this upload_id into file_sources of "
                             "submit_parse_job or parse_documents",
            })
        return {"status": "success", "uploads": uploads, "count": len(uploads)}

    @app.get("/upload/{upload_id}/status")
    async def upload_status(upload_id: str) -> dict[str, Any]:
        rec = store.get(upload_id)
        if rec is None:
            raise HTTPException(404, f"unknown upload_id '{upload_id}'")
        d = rec.to_dict()
        d["expires_at"] = rec.expires_at
        return d

    @app.delete("/upload/{upload_id}")
    async def upload_delete(upload_id: str) -> dict[str, Any]:
        return {"deleted": store.delete(upload_id)}


async def _file_stream(part: UploadFile) -> AsyncIterator[bytes]:
    spool = part.file
    if spool is not None and hasattr(spool, "read"):
        # Starlette already spooled to disk; re-chunk so sha256 streams.
        while chunk := spool.read(1024 * 1024):
            yield chunk
    else:
        data = await part.read()
        yield data
