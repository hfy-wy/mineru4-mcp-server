"""Response envelope builders (official mineru-open-mcp style).

Standard envelope for parse/publish flows:
  {status: success|partial_success|error, results[], summary{...}, message}
Plus tool_ok/tool_fail helpers so every tool returns a dict, never raises.
"""
from __future__ import annotations

from typing import Any

CONTENT_MAX_PER_FILE = 20_000
CONTENT_MAX_TOTAL = 60_000


def error_entry(filename: str, message: str) -> dict[str, Any]:
    return {"filename": filename, "status": "error", "error": message}


def apply_content_caps(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate inline markdown content under 20k/file, 60k total budget.

    Full markdown is always on disk at extract_path; truncated rows get a
    ``truncated: true`` flag.
    """
    with_content = [r for r in results
                    if r.get("status") == "success" and "content" in r]
    cap = min(CONTENT_MAX_PER_FILE,
              CONTENT_MAX_TOTAL // max(len(with_content), 1))
    out: list[dict[str, Any]] = []
    for r in results:
        if r.get("status") == "success" and "content" in r:
            r = dict(r)
            full = r["content"]
            r["content_chars"] = len(full)
            if len(full) > cap:
                r["content"] = full[:cap]
                r["truncated"] = True
            else:
                r["truncated"] = False
        out.append(r)
    return out


def format_results(
    results: list[dict[str, Any]],
    *,
    job_id: str | None = None,
    message_parts: list[str] | None = None,
    download_urls: list[str] | None = None,
) -> dict[str, Any]:
    success = sum(1 for r in results if r.get("status") == "success")
    errors = len(results) - success
    response: dict[str, Any] = {
        "status": "error" if success == 0 else
                  "partial_success" if errors else "success",
        "results": apply_content_caps(results),
        "summary": {
            "total_files": len(results),
            "success_count": success,
            "error_count": errors,
        },
    }
    if job_id:
        response["summary"]["job_id"] = job_id
    if success:
        saved = [(r.get("filename", ""), r.get("extract_path"))
                 for r in results
                 if r.get("status") == "success" and r.get("extract_path")]
        lines: list[str] = []
        if message_parts:
            lines.extend(message_parts)
        if len(saved) == 1:
            lines.append(f"Saved to: {saved[0][1]}")
        elif saved:
            lines.append("Files saved to:")
            for i, (name, path) in enumerate(saved, 1):
                lines.append(f"  [{i}] {name} -> {path}")
        if download_urls:
            lines.append("Download links (give these to the user):")
            for url in download_urls:
                lines.append(f"  {url}")
        response["message"] = "\n".join(["Parsing complete!", *lines])
    elif message_parts:
        response["message"] = "\n".join(message_parts)
    return response


def tool_ok(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("ok", True)
    return kwargs


def tool_fail(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("ok", False)
    return kwargs
