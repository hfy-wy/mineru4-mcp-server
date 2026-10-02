"""Artifact persistence: per-job output dirs, zip save+extract, image-ref fixer.

Layout under OUTPUT_DIR:
  <job_id>/<stem>.md / .json / .structured.json / .zip   (flat convenience copies)
  <job_id>/<stem>/                                        (zip extracted, original layout)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Settings
from .v1_client import MinerUV1AsyncClient, MinerUV1Error

log = logging.getLogger("mineru-selfhosted-mcp.publish")

_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)

_IMAGE_REF_RE = re.compile(
    r"(!\[[^\]]*\]\()\s*(images/[^)\s]+)\s*\)", re.IGNORECASE
)


def job_output_dir(settings: Settings, job_id: str) -> Path:
    d = settings.output_dir / _safe_dirname(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def unique_path(directory: Path, name: str) -> Path:
    """Return a non-existing path for name, suffixing _1.._9999 on collision."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if not target.exists():
        return target
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(1, 10000):
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many duplicates for {name}")


def download_url(settings: Settings, rel_path: Path) -> str | None:
    """PUBLIC_BASE_URL + /downloads/<rel>, URL-quoted. None when unset."""
    if not settings.public_base_url:
        return None
    rel_posix = rel_path.as_posix()
    quoted = quote(rel_posix)
    return f"{settings.public_base_url}/downloads/{quoted}"


async def publish_job_outputs(
    settings: Settings,
    client: MinerUV1AsyncClient,
    job: dict[str, Any],
    output_formats: list[str],
) -> list[dict[str, Any]]:
    """Download every completed file x requested format; return result rows.

    Rows: {filename, output_format, status, saved_path, download_url,
           extracted_dir?, content? (markdown only), content_chars?, truncated?}
    """
    out_dir = job_output_dir(settings, str(job.get("job_id", "job")))
    rows: list[dict[str, Any]] = []
    for f in job.get("files") or []:
        name = f.get("name", "file")
        if f.get("status") == "failed":
            err = f.get("error") or {}
            rows.append({
                "filename": name,
                "status": "error",
                "error": err.get("message", "server-side parse failed"),
            })
            continue
        if f.get("status") != "completed":
            continue
        out_files = f.get("output_files") or {}
        for fmt in output_formats:
            meta = out_files.get(fmt)
            if not isinstance(meta, dict) or not meta.get("file_id"):
                rows.append({
                    "filename": name,
                    "output_format": fmt,
                    "status": "error",
                    "error": f"no {fmt} output",
                })
                continue
            rows.append(await _download_one(settings, client, out_dir, name, fmt, meta))

    for row in rows:
        if row.get("status") == "success" and row.get("saved_path"):
            rel = Path(row["saved_path"]).relative_to(settings.output_dir)
            row["download_url"] = download_url(settings, rel)

    return rows


async def _download_one(
    settings: Settings,
    client: MinerUV1AsyncClient,
    out_dir: Path,
    name: str,
    fmt: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    async with _DOWNLOAD_SEMAPHORE:
        stem = Path(name).stem or "output"
        try:
            if fmt == "markdown":
                md_path = unique_path(out_dir, f"{stem}.md")
                await client.download_file_to(str(meta["file_id"]), md_path)
                content = md_path.read_text(encoding="utf-8", errors="replace")
                row = {
                    "filename": name,
                    "output_format": fmt,
                    "status": "success",
                    "saved_path": str(md_path),
                    "extract_path": str(md_path),
                    "content": content,
                }
            elif fmt == "zip":
                zip_path = unique_path(out_dir, f"{stem}.zip")
                await client.download_file_to(str(meta["file_id"]), zip_path)
                extracted = _extract_zip(zip_path, out_dir / stem)
                flat_md = _flat_markdown(extracted, out_dir, stem)
                row = {
                    "filename": name,
                    "output_format": fmt,
                    "status": "success",
                    "saved_path": str(zip_path),
                    "extract_path": str(flat_md) if flat_md else str(zip_path),
                    "extracted_dir": str(extracted) if extracted else None,
                }
            else:
                ext = _FORMAT_EXT.get(fmt, f".{fmt}")
                data_path = unique_path(out_dir, f"{stem}{ext}")
                await client.download_file_to(str(meta["file_id"]), data_path)
                row = {
                    "filename": name,
                    "output_format": fmt,
                    "status": "success",
                    "saved_path": str(data_path),
                    "extract_path": str(data_path),
                }
            return row
        except MinerUV1Error as exc:
            return {
                "filename": name,
                "output_format": fmt,
                "status": "error",
                "error": str(exc),
            }


_FORMAT_EXT = {
    "middle_json": ".json",
    "structured_content": ".structured.json",
}


def _extract_zip(zip_path: Path, dest: Path) -> Path | None:
    try:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                _safe_extract_member(zf, member, dest)
        return dest
    except Exception as exc:
        log.warning("zip extract failed for %s: %s", zip_path, exc)
        return None


def _safe_extract_member(zf: zipfile.ZipFile, member: str, dest: Path) -> None:
    """Extract one member, refusing path traversal outside dest."""
    dest_resolved = str(dest.resolve())
    target = (dest / member).resolve()
    if not (target == dest.resolve() or str(target).startswith(dest_resolved + os.sep)):
        log.warning("skipping zip member outside dest: %s", member)
        return
    if member.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(member) as src, target.open("wb") as out:
        while chunk := src.read(1024 * 1024):
            out.write(chunk)


def _flat_markdown(extracted: Path | None, out_dir: Path, stem: str) -> Path | None:
    """Write a flat <stem>.md whose image refs point into <stem>/images/."""
    if extracted is None:
        return None
    md_files = sorted(extracted.rglob("*.md"))
    if not md_files:
        return None
    source_md = md_files[0]
    try:
        text = source_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    fixed = _IMAGE_REF_RE.sub(lambda m: f"{m.group(1)}{stem}/{m.group(2)})", text)
    flat = out_dir / f"{stem}.md"
    flat.write_text(fixed, encoding="utf-8")
    return flat


def _safe_dirname(job_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", job_id.strip())
    return name or "job"
