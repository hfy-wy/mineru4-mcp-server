"""V1 protocol client tests with httpx MockTransport (no network)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from mineru_selfhosted_mcp.config import Settings
from mineru_selfhosted_mcp.v1_client import MinerUV1AsyncClient, MinerUV1Error


def make_settings(tmp_path: Path, base: str = "http://v1.test") -> Settings:
    return Settings(
        base_url=base,
        upload_dir=tmp_path / "uploads",
        output_dir=tmp_path / "output",
    )


def make_client(settings: Settings, handler) -> MinerUV1AsyncClient:
    client = MinerUV1AsyncClient(settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=10)
    return client


def write_pdf(tmp_path: Path, name: str = "doc.pdf", size: int = 2048) -> Path:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4 " + b"x" * size)
    return p


def test_full_3step_upload(tmp_path):
    """Dedup miss: init -> PUT -> complete returns file id."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if url == "/v1/uploads" and request.method == "POST":
            body = json.loads(request.content)
            assert body["filename"] == "doc.pdf"
            assert body["purpose"] == "parse"
            assert len(body["sha256sum"]) == 64
            return httpx.Response(200, json={
                "id": "upload_1", "status": "pending",
                "upload_url": "/v1/uploads/upload_1/content",
                "upload_headers": {"Content-Type": "application/pdf"},
            })
        if url == "/v1/uploads/upload_1/content" and request.method == "PUT":
            return httpx.Response(200)
        if url == "/v1/uploads/upload_1/complete" and request.method == "POST":
            return httpx.Response(200, json={
                "id": "upload_1", "status": "completed",
                "file": {"id": "file_abc", "object": "file"},
            })
        return httpx.Response(404, json={"error": {"message": f"no route {url}"}})

    settings = make_settings(tmp_path)
    pdf = write_pdf(tmp_path)
    client = make_client(settings, handler)
    file_id = asyncio.run(client.upload_file(pdf))
    assert file_id == "file_abc"


def test_dedup_hit(tmp_path):
    """sha256 dedup: init returns completed + embedded file -> skip PUT/complete."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if url == "/v1/uploads" and request.method == "POST":
            return httpx.Response(200, json={
                "id": "upload_1", "status": "completed",
                "file": {"id": "file_dedup", "object": "file"},
            })
        raise AssertionError(f"unexpected call {request.method} {url}")

    settings = make_settings(tmp_path)
    pdf = write_pdf(tmp_path)
    client = make_client(settings, handler)
    file_id = asyncio.run(client.upload_file(pdf))
    assert file_id == "file_dedup"


def test_api_error_raises_mineru_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"code": "page_range_invalid", "message": "bad range"}})

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    with pytest.raises(MinerUV1Error) as exc_info:
        asyncio.run(client.get_job("job_x"))
    assert exc_info.value.status_code == 400
    assert "bad range" in exc_info.value.message


def test_submit_job_payload(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"job_id": "job_1", "status": "queued"})

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    job = asyncio.run(client.submit_job(
        [{"source": {"type": "file_id", "file_id": "file_1"}, "page_range": "1-3"}],
        tier="standard", ocr_mode="auto", output_formats=["markdown"]))
    assert job["job_id"] == "job_1"
    # "auto" ocr_mode is omitted; tier passed; page_range "1-3" kept
    assert captured["body"]["tier"] == "standard"
    assert "ocr_mode" not in captured["body"]
    assert captured["body"]["files"][0]["page_range"] == "1-3"
    assert captured["body"]["output_formats"] == ["markdown"]


def test_submit_job_auto_omits_tier(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"job_id": "job_1", "status": "queued"})

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    asyncio.run(client.submit_job(
        [{"source": {"type": "url", "url": "https://x/y.pdf"}}],
        tier=None, ocr_mode=None, output_formats=None))
    assert "tier" not in captured["body"]
    assert "ocr_mode" not in captured["body"]
    assert captured["body"]["output_formats"] == ["markdown"]


def test_streaming_download(tmp_path):
    payload = b"M" * (3 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    dest = tmp_path / "out" / "result.md"
    got = asyncio.run(client.download_file_to("file_1", dest))
    assert got.read_bytes() == payload


def test_download_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={
            "error": {"message": "feature_requires_api_key"}})

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    with pytest.raises(MinerUV1Error) as exc_info:
        asyncio.run(client.download_file("file_1"))
    assert exc_info.value.status_code == 403


def test_list_jobs_params(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "object": "list", "data": [], "first_id": None,
            "last_id": None, "has_more": False})

    settings = make_settings(tmp_path)
    client = make_client(settings, handler)
    data = asyncio.run(client.list_jobs(limit=5, after="job_last", status="running"))
    assert captured["params"]["limit"] == "5"
    assert captured["params"]["after"] == "job_last"
    assert captured["params"]["status"] == "running"
    assert data["has_more"] is False
