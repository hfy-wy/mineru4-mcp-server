"""F6 e2e: two users (alice/bob) run full parse flows via MCP client.

Usage: python scripts/mcp_f6_e2e.py <sample.pdf>
Env overrides: MINERU_MCP_URL / MINERU_UPLOAD_URL (default 127.0.0.1:7000).
MINERU_MCP_URL may be the bare base URL (as in deploy/env/*.env) or a full
endpoint ending in /mcp — both are accepted.
"""
import asyncio
import os
import sys
import base64

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

_mcp_raw = os.environ.get("MINERU_MCP_URL", "http://127.0.0.1:7000")
MCP = _mcp_raw if _mcp_raw.rstrip("/").endswith("/mcp") else _mcp_raw.rstrip("/") + "/mcp"
UPLOAD = os.environ.get("MINERU_UPLOAD_URL",
                        _mcp_raw.rstrip("/") + "/upload")


async def upload_via_http(path: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=60) as hx:
        with open(path, "rb") as fh:
            r = await hx.post(UPLOAD, files={"file": (path.split("/")[-1], fh)})
        r.raise_for_status()
        return r.json()["uploads"][0]["upload_id"]


async def run_for_user(user: str, upl: str) -> str:
    transport = StreamableHttpTransport(MCP, headers={"X-Mineru-User": user})
    async with Client(transport) as client:
        sub = await client.call_tool("submit_parse_job", {
            "file_sources": [upl],
            "output_formats": ["markdown"],
        })
        job_id = sub.data["job_id"]
        print(f"[{user}] submitted {job_id}: {sub.data['status']}")
        for _ in range(30):
            q = await client.call_tool("query_job_status",
                                       {"job_id": job_id, "wait_seconds": 5})
            if q.data["status"] in ("completed", "partial", "failed", "canceled"):
                break
        print(f"[{user}] final status: {q.data['status']}")
        d = await client.call_tool("download_job_result",
                                   {"job_id": job_id, "output_formats": ["markdown"]})
        print(f"[{user}] downloaded {len(d.data['results'])} artifact row(s)")
        return job_id


async def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "pdf/doc1.pdf"
    upl = await upload_via_http(path)
    print("uploaded handle:", upl)
    alice_job = await run_for_user("alice", upl)   # sha256 dedup -> second upload skips bytes
    bob_job = await run_for_user("bob", upl)
    # same handle again for bob — exercises the v1_file_id cache path
    print("DONE", alice_job, bob_job)


if __name__ == "__main__":
    asyncio.run(main())
