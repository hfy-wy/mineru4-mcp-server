"""MinerU 4.0 V1 API async client (self-hosted api-server / router protocol).

Protocol surface (verified against mineru/parser/api_server.py, MinerU 4.0):
- 3-step upload with sha256 dedup: POST /v1/uploads -> PUT {upload_url}
  -> POST /v1/uploads/{id}/complete; a dedup hit returns status "completed"
  with an embedded file object (skip PUT/complete).
- Jobs: POST /v1/parse/jobs (202) -> GET /v1/parse/jobs/{id} -> GET /v1/files/{id}/content
- Job board: GET /v1/parse/jobs (list), DELETE /v1/parse/jobs/{id} (cancel)
- GET /v1/health, GET /v1/tiers (unauthenticated)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

_UPLOAD_CHUNK = 1024 * 1024  # 1 MB streaming chunks


class MinerUV1Error(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"API HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message


class MinerUV1AsyncClient:
    """One shared httpx.AsyncClient per app; methods return parsed dicts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    # -- lifecycle -----------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(180, connect=30),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- headers -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if self.settings.api_key:
            return {"Authorization": f"Bearer {self.settings.api_key}"}
        return {}

    # -- error envelope ------------------------------------------------------

    @staticmethod
    def _check(resp: httpx.Response) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            loaded = resp.json()
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
        if resp.status_code >= 400:
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err)
            else:
                msg = data.get("message") or resp.text[:300]
            raise MinerUV1Error(resp.status_code, msg)
        if isinstance(data.get("error"), dict):
            raise MinerUV1Error(resp.status_code, str(data["error"]))
        return data

    # -- uploads (3-step + dedup) ---------------------------------------------

    async def init_upload(self, filename: str, size: int, mime: str, sha256: str) -> dict[str, Any]:
        r = await self._http().post(
            f"{self.settings.base_url}/v1/uploads",
            headers=self._headers(),
            json={
                "filename": filename,
                "bytes": size,
                "mime_type": mime,
                "purpose": "parse",
                "sha256sum": sha256,
            },
        )
        return self._check(r)

    async def put_content(self, upload_url: str, headers: dict[str, str], src: Path) -> None:
        """Streaming PUT — chunked reads, never loads the whole file into memory."""
        req = self._http().build_request(
            "PUT", upload_url,
            headers={**self._headers(), **headers},
            content=_aiter_file(src),
        )
        resp = await self._http().send(req)
        if resp.status_code >= 400:
            raise MinerUV1Error(resp.status_code, f"upload PUT failed: {resp.text[:300]}")

    async def complete_upload(self, upload_id: str, sha256: str) -> dict[str, Any]:
        r = await self._http().post(
            f"{self.settings.base_url}/v1/uploads/{upload_id}/complete",
            headers=self._headers(),
            json={"sha256sum": sha256},
        )
        return self._check(r)

    async def upload_file(self, src: Path, display_name: str | None = None) -> str:
        """Run the 3-step upload (with sha256 dedup) and return the file_id.

        display_name: the client-side filename to advertise to the api-server
        (it names job files by it and validates the extension). Defaults to
        the local path's name — which for side-channel spool blobs is
        ``upl_<id>.bin`` and would be rejected by a strict api-server.
        """
        name = display_name or src.name
        sha = hashlib.sha256()
        size = 0
        with src.open("rb") as fh:
            while chunk := fh.read(_UPLOAD_CHUNK):
                sha.update(chunk)
                size += len(chunk)
        mime = _mime_for(name)
        init = await self.init_upload(name, size, mime, sha.hexdigest())
        if init.get("status") == "completed" and isinstance(init.get("file"), dict):
            return str(init["file"]["id"])
        upload_url = init.get("upload_url") or ""
        if upload_url.startswith("/"):
            upload_url = f"{self.settings.base_url}{upload_url}"
        if not upload_url:
            raise MinerUV1Error(500, "no upload_url in upload init response")
        await self.put_content(upload_url, init.get("upload_headers") or {}, src)
        done = await self.complete_upload(str(init["id"]), sha.hexdigest())
        file_obj = done.get("file") if isinstance(done.get("file"), dict) else None
        if file_obj and file_obj.get("id"):
            return str(file_obj["id"])
        raise MinerUV1Error(500, "upload complete returned no file id")

    # -- jobs -----------------------------------------------------------------

    async def submit_job(
        self,
        file_entries: list[dict[str, Any]],
        *,
        tier: str | None = None,
        ocr_mode: str | None = None,
        output_formats: list[str] | None = None,
    ) -> dict[str, Any]:
        # project to protocol keys only — entries may carry internal metadata
        files = [
            {"source": e["source"], **({"page_range": e["page_range"]}
                                       if e.get("page_range") else {})}
            for e in file_entries if isinstance(e.get("source"), dict)
        ]
        payload: dict[str, Any] = {
            "files": files,
            "output_formats": output_formats or ["markdown"],
        }
        if tier and tier != "auto":
            payload["tier"] = tier
        if ocr_mode and ocr_mode != "auto":
            payload["ocr_mode"] = ocr_mode
        r = await self._http().post(
            f"{self.settings.base_url}/v1/parse/jobs",
            headers=self._headers(),
            json=payload,
        )
        return self._check(r)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        r = await self._http().get(
            f"{self.settings.base_url}/v1/parse/jobs/{job_id}",
            headers=self._headers(),
        )
        return self._check(r)

    async def list_jobs(self, limit: int = 20, after: str | None = None,
                        status: str | None = None, order: str = "desc") -> dict[str, Any]:
        """Cursor-paginated job list: {object, data[], first_id, last_id, has_more}."""
        params: dict[str, Any] = {"limit": limit, "order": order}
        if after:
            params["after"] = after
        if status:
            params["status"] = status
        r = await self._http().get(
            f"{self.settings.base_url}/v1/parse/jobs",
            headers=self._headers(),
            params=params,
        )
        return self._check(r)

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        r = await self._http().delete(
            f"{self.settings.base_url}/v1/parse/jobs/{job_id}",
            headers=self._headers(),
        )
        return self._check(r)

    # -- file download ----------------------------------------------------------

    async def download_file_to(self, file_id: str, dest: Path) -> Path:
        """Stream GET /v1/files/{id}/content straight to disk."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with self._http().stream(
            "GET",
            f"{self.settings.base_url}/v1/files/{file_id}/content",
            headers=self._headers(),
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                raise MinerUV1Error(resp.status_code, f"download failed: {body}")
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    fh.write(chunk)
        return dest

    async def download_file(self, file_id: str) -> bytes:
        r = await self._http().get(
            f"{self.settings.base_url}/v1/files/{file_id}/content",
            headers=self._headers(),
        )
        if r.status_code >= 400:
            raise MinerUV1Error(r.status_code, f"download failed: {r.text[:300]}")
        return r.content

    # -- service info ----------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        r = await self._http().get(f"{self.settings.base_url}/v1/health", timeout=10)
        return self._check(r)

    async def tiers(self) -> dict[str, Any] | None:
        try:
            r = await self._http().get(
                f"{self.settings.base_url}/v1/tiers",
                headers=self._headers(),
                timeout=10,
            )
            return self._check(r)
        except Exception:
            return None


async def _aiter_file(src: Path):
    """Async byte iterator over a local file — httpx streams it chunked."""
    with src.open("rb") as fh:
        while chunk := fh.read(_UPLOAD_CHUNK):
            yield chunk


def _mime_for(filename: str) -> str:
    from .filetypes import mime_type_for_extension
    return mime_type_for_extension(filename)
