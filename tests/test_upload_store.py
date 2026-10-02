"""Upload store tests: streaming save, caps, TTL, dedup metadata."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mineru_selfhosted_mcp.upload_store import (
    UploadRejected,
    UploadStore,
    UploadTooLarge,
    repair_mojibake,
)


async def _gen(*chunks: bytes):
    for c in chunks:
        yield c


def make_store(tmp_path: Path, ttl: int = 86400, max_bytes: int = 1024 * 1024) -> UploadStore:
    return UploadStore(tmp_path / "uploads", ttl_seconds=ttl, max_bytes=max_bytes)


def test_save_and_get(tmp_path):
    store = make_store(tmp_path)
    rec = asyncio.run(store.save("report.pdf", _gen(b"a" * 100, b"b" * 50)))
    assert rec.upload_id.startswith("upl_")
    assert rec.size == 150
    assert rec.filename == "report.pdf"
    assert rec.mime_type == "application/pdf"
    got = store.get(rec.upload_id)
    assert got is not None and got.sha256 == rec.sha256
    blob = store.upload_dir / f"{rec.upload_id}.bin"
    assert blob.read_bytes() == b"a" * 100 + b"b" * 50


def test_reject_unsupported_extension(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(UploadRejected):
        asyncio.run(store.save("virus.exe", _gen(b"xx")))


def test_reject_empty_body(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(UploadRejected):
        asyncio.run(store.save("empty.pdf", _gen()))


def test_size_cap_aborts(tmp_path):
    store = make_store(tmp_path, max_bytes=100)
    with pytest.raises(UploadTooLarge):
        asyncio.run(store.save("big.pdf", _gen(b"x" * 60, b"x" * 60)))
    # partial blob must be removed
    leftovers = list(store.upload_dir.glob("upl_*.bin"))
    assert leftovers == []


def test_ttl_expiry(tmp_path):
    store = make_store(tmp_path, ttl=1)
    rec = asyncio.run(store.save("doc.pdf", _gen(b"data")))
    # backdate the record
    import json
    meta = store.upload_dir / f"{rec.upload_id}.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["created_at"] = time.time() - 10
    meta.write_text(json.dumps(data), encoding="utf-8")
    assert store.get(rec.upload_id) is None


def test_mark_consumed_and_clear(tmp_path):
    store = make_store(tmp_path)
    rec = asyncio.run(store.save("doc.pdf", _gen(b"data")))
    store.mark_consumed(rec.upload_id, "file_abc")
    got = store.get(rec.upload_id)
    assert got.state == "consumed" and got.v1_file_id == "file_abc"
    store.clear_v1_file_id(rec.upload_id)
    got = store.get(rec.upload_id)
    assert got.state == "stored" and got.v1_file_id is None


def test_list_and_delete(tmp_path):
    store = make_store(tmp_path)
    r1 = asyncio.run(store.save("a.pdf", _gen(b"1")))
    r2 = asyncio.run(store.save("b.pdf", _gen(b"2")))
    ids = [r.upload_id for r in store.list()]
    assert r1.upload_id in ids and r2.upload_id in ids
    assert store.delete(r1.upload_id) is True
    assert store.get(r1.upload_id) is None
    assert store.delete(r1.upload_id) is False


def test_unsafe_id_rejected(tmp_path):
    store = make_store(tmp_path)
    assert store.get("../etc/passwd") is None
    assert store.get("upl_../../x") is None
    assert store.delete("../junk") is False


# -- mojibake repair (GBK/latin-1 filenames from Windows clients) -----------

def test_repair_mojibake_gbk():
    # Real case seen on the board: GBK bytes of a Chinese filename decoded
    # as latin-1 by the multipart header parser.
    mojibake = "2025Äê¶ÈÐÅÏ¢»¯ÆÀ¹À£¨ÑÐ¾¿Ëù£©_Äþ²¨²ÄÁÏ.pdf"
    assert repair_mojibake(mojibake) == "2025年度信息化评估（研究所）_宁波材料.pdf"


def test_repair_mojibake_utf8_passthrough():
    # UTF-8 bytes mis-decoded as latin-1 (common for modern clients)
    raw = "报告.pdf".encode("utf-8").decode("latin-1")
    assert repair_mojibake(raw) == "报告.pdf"


def test_repair_mojibake_ascii_untouched():
    name = "plain-report-2026.pdf"
    assert repair_mojibake(name) == name


def test_repair_mojibake_real_unicode_untouched():
    # Already-proper Unicode (server received it via a correct parser)
    name = "真·已解码.pdf"
    assert repair_mojibake(name) == name


def test_repair_mojibake_cp1252_tradeoff_documented():
    # Byte-level GBK-vs-cp1252 is genuinely ambiguous: cp1252 high bytes form
    # valid GBK pairs. The repair optimizes for Chinese-locale clients, so a
    # genuine cp1252 name IS corrupted (audit finding) — accepted trade-off,
    # documented here so it is a known decision, not an accident.
    assert repair_mojibake("Müller.pdf") == "M黮ler.pdf"
    # UTF-8 repair is unambiguous and still wins:
    raw = "报告.pdf".encode("utf-8").decode("latin-1")
    assert repair_mojibake(raw) == "报告.pdf"


def test_repair_mojibake_undecodable_kept():
    # Random high bytes that are neither valid UTF-8 nor GBK → keep original
    junk = bytes([0xC3, 0x28, 0xA0]).decode("latin-1")
    assert repair_mojibake(junk) == junk


async def test_save_repairs_gbk_filename(tmp_path):
    store = make_store(tmp_path)
    rec = await store.save(
        "ÖÐ±êÍ¨ÖªÊé.pdf", _gen(b"%PDF-1.4 tiny")
    )
    assert rec.filename == "中标通知书.pdf"
