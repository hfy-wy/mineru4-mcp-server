"""Vendored file-type tables for the MinerU V1 parse API.

Kept dependency-free on purpose: the upstream ``mineru`` package pulls in
hundreds of MB for these few tables. Extension sets and MIME mappings mirror
``mineru/filetypes.py`` (MinerU 4.0).
"""
from __future__ import annotations

MIME_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".jp2": "image/jp2",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".html": "text/html",
    ".htm": "text/html",
    ".shtml": "text/html",
    ".mhtml": "message/rfc822",
    ".mht": "message/rfc822",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".epub": "application/epub+zip",
    ".ofd": "application/ofd",
}

# MinerU parseable input extensions (PARSEABLE_EXTENSIONS in upstream filetypes.py)
PARSEABLE_EXTENSIONS: frozenset[str] = frozenset(MIME_BY_EXTENSION)

# Office/HTML/MHTML/CSV/EPUB/OFD are flash-tier-only inputs (auto-normalized
# by the server); PDF and images run on any tier.
FLASH_ONLY_EXTENSIONS: frozenset[str] = frozenset({
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".rtf", ".odt", ".ods", ".odp",
    ".html", ".htm", ".shtml", ".mhtml", ".mht",
    ".csv", ".tsv", ".epub", ".ofd",
})

PDF_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".jp2",
})

_PAGE_RANGE_RE = __import__("re").compile(
    r"^(all|r?\d+(?:-r?\d+)?(?:\s*,\s*r?\d+(?:-r?\d+)?)*)$"
)


def mime_type_for_extension(filename: str, default: str = "application/octet-stream") -> str:
    ext = _extension_of(filename)
    return MIME_BY_EXTENSION.get(ext, default)


def is_parseable(filename: str) -> bool:
    return _extension_of(filename) in PARSEABLE_EXTENSIONS


def is_flash_only(filename: str) -> bool:
    return _extension_of(filename) in FLASH_ONLY_EXTENSIONS


def is_pdf_or_image(filename: str) -> bool:
    return _extension_of(filename) in PDF_IMAGE_EXTENSIONS


def valid_page_range(page_range: str) -> bool:
    """True when page_range matches V1 grammar: all | rN | N | N-M | lists."""
    return bool(_PAGE_RANGE_RE.match(page_range.strip()))


def _extension_of(filename: str) -> str:
    name = filename.replace("\\", "/").split("/")[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()
