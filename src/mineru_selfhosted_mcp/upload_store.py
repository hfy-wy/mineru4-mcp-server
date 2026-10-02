"""Side-channel upload store: streaming spool + sha256 + metadata sidecar + TTL.

The side channel only persists files locally. The 3-step V1 upload happens
lazily inside tools.resolve_sources when a handle (``upl_…``) is consumed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .filetypes import is_parseable, mime_type_for_extension

log = logging.getLogger("mineru-selfhosted-mcp.upload")


class UploadTooLarge(RuntimeError):
    pass


class UploadRejected(RuntimeError):
    pass


def repair_mojibake(filename: str) -> str:
    """Repair non-UTF8 client filenames decoded as latin-1 by the multipart parser.

    Clients on Windows code pages (e.g. GBK curl) send raw local bytes in the
    multipart ``filename`` header; Starlette decodes headers as latin-1, so
    "年度" arrives as "Äê¶È". latin-1 is a byte↔char bijection, so re-encoding
    recovers the original bytes losslessly; then try UTF-8, then GBK.

    Byte-level GBK-vs-cp1252 is genuinely ambiguous (cp1252 high bytes form
    valid GBK pairs and vice versa), so this optimizes for the dominant client
    population (Chinese-locale Windows). ASCII, already-Unicode, and
    undecodable byte sequences return as-is.
    """
    try:
        raw = filename.encode("latin-1")
    except UnicodeEncodeError:
        return filename  # contains chars outside latin-1 → already real Unicode
    if all(b < 0x80 for b in raw):
        return filename  # pure ASCII, nothing to fix
    for enc in ("utf-8", "gbk"):
        try:
            decoded = raw.decode(enc)
            # adopt only if something actually changed
            return decoded if decoded != filename else filename
        except UnicodeDecodeError:
            continue
    return filename


@dataclass
class UploadRecord:
    upload_id: str
    filename: str
    size: int
    sha256: str
    mime_type: str
    created_at: float
    ttl_seconds: int = 86400
    state: str = "stored"            # stored | consumed
    v1_file_id: str | None = None    # cached after first successful V1 upload

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UploadStore:
    def __init__(self, upload_dir: Path, ttl_seconds: int, max_bytes: int) -> None:
        self.upload_dir = upload_dir
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        upload_dir.mkdir(parents=True, exist_ok=True)

    # -- write path ------------------------------------------------------------

    async def save(
        self,
        filename: str,
        stream: AsyncIterator[bytes],
        ttl_seconds: int | None = None,
    ) -> UploadRecord:
        filename = Path(repair_mojibake(filename)).name
        if not filename:
            raise UploadRejected("empty filename")
        ext = Path(filename).suffix.lower()
        if ext and not is_parseable(filename):
            raise UploadRejected(f"unsupported extension '{ext}'")

        upload_id = f"upl_{uuid.uuid4().hex[:16]}"
        blob_path = self.upload_dir / f"{upload_id}.bin"
        sha = hashlib.sha256()
        size = 0
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        fh = blob_path.open("wb")
        try:
            async for chunk in stream:
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.max_bytes:
                    raise UploadTooLarge(
                        f"file exceeds limit {self.max_bytes} bytes"
                    )
                sha.update(chunk)
                fh.write(chunk)
        except BaseException:
            fh.close()
            blob_path.unlink(missing_ok=True)
            raise
        fh.close()
        if size == 0:
            blob_path.unlink(missing_ok=True)
            raise UploadRejected("empty file body")

        record = UploadRecord(
            upload_id=upload_id,
            filename=filename,
            size=size,
            sha256=sha.hexdigest(),
            mime_type=mime_type_for_extension(filename),
            created_at=time.time(),
            ttl_seconds=self.ttl_seconds if ttl_seconds is None else ttl_seconds,
        )
        self._write_meta(record)
        log.info("stored upload %s %s (%d bytes)", upload_id, filename, size)
        return record

    # -- read path -------------------------------------------------------------

    def get(self, upload_id: str) -> UploadRecord | None:
        if not self._safe_id(upload_id):
            return None
        meta_path = self.upload_dir / f"{upload_id}.json"
        if not meta_path.is_file():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if time.time() > float(data.get("created_at", 0)) + self.ttl_seconds:
            self.delete(upload_id)
            return None
        return UploadRecord(
            upload_id=data["upload_id"],
            filename=data["filename"],
            size=int(data["size"]),
            sha256=data["sha256"],
            mime_type=data.get("mime_type", "application/octet-stream"),
            created_at=float(data.get("created_at", 0)),
            ttl_seconds=int(data.get("ttl_seconds", self.ttl_seconds)),
            state=data.get("state", "stored"),
            v1_file_id=data.get("v1_file_id"),
        )

    def list(self, limit: int = 50) -> list[UploadRecord]:
        records: list[UploadRecord] = []
        for meta_path in sorted(self.upload_dir.glob("upl_*.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True):
            rec = self.get(meta_path.stem)
            if rec:
                records.append(rec)
            if len(records) >= limit:
                break
        return records

    # -- mutations -------------------------------------------------------------

    def mark_consumed(self, upload_id: str, v1_file_id: str) -> None:
        rec = self.get(upload_id)
        if rec:
            rec.state = "consumed"
            rec.v1_file_id = v1_file_id
            self._write_meta(rec)

    def clear_v1_file_id(self, upload_id: str) -> None:
        rec = self.get(upload_id)
        if rec and rec.v1_file_id:
            rec.v1_file_id = None
            rec.state = "stored"
            self._write_meta(rec)

    def delete(self, upload_id: str) -> bool:
        if not self._safe_id(upload_id):
            return False
        blob = self.upload_dir / f"{upload_id}.bin"
        meta = self.upload_dir / f"{upload_id}.json"
        existed = blob.exists() or meta.exists()
        blob.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return existed

    def sweep_expired(self) -> int:
        """Delete blobs+meta past TTL. Call periodically and opportunistically."""
        removed = 0
        now = time.time()
        for meta_path in self.upload_dir.glob("upl_*.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if now > float(data.get("created_at", 0)) + self.ttl_seconds:
                    self.delete(meta_path.stem)
                    removed += 1
            except Exception:
                continue
        # blobs without meta (interrupted saves) older than TTL
        for blob in self.upload_dir.glob("upl_*.bin"):
            if not blob.with_suffix(".json").exists() and \
                    now - blob.stat().st_mtime > self.ttl_seconds:
                blob.unlink(missing_ok=True)
                removed += 1
        return removed

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _safe_id(upload_id: str) -> bool:
        return upload_id.startswith("upl_") and "/" not in upload_id and \
            "\\" not in upload_id and ".." not in upload_id

    def _write_meta(self, rec: UploadRecord) -> None:
        meta_path = self.upload_dir / f"{rec.upload_id}.json"
        meta_path.write_text(
            json.dumps(rec.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
