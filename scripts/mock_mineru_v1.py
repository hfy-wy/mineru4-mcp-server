"""Mock MinerU V1 api-server for e2e testing (dev only)."""
import asyncio
import hashlib
import json
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()
FILES = {}       # sha -> file_id
FILE_DATA = {}   # file_id -> bytes
JOBS = {}        # job_id -> dict


@app.get("/v1/health")
async def health():
    return {"status": "ok", "version": "mock-4.0",
            "features": {"sources": ["file_id", "url", "inline"],
                         "output_formats": ["markdown", "middle_json",
                                            "structured_content", "zip"]}}


@app.post("/v1/uploads")
async def create_upload(request: Request):
    body = await request.json()
    sha = body.get("sha256sum")
    if sha and sha in FILES:
        return {"id": "upload_dup", "status": "completed",
                "file": {"id": FILES[sha]}}
    uid = f"upload_{uuid.uuid4().hex[:8]}"
    return {"id": uid, "status": "pending",
            "upload_url": f"/v1/uploads/{uid}/content",
            "upload_headers": {"Content-Type": body.get("mime_type", "application/octet-stream")}}


@app.put("/v1/uploads/{uid}/content")
async def put_content(uid: str, request: Request):
    data = await request.body()
    FILE_DATA[uid] = data
    FILE_DATA.setdefault("_sha_" + uid, hashlib.sha256(data).hexdigest())
    return Response(status_code=200)


@app.post("/v1/uploads/{uid}/complete")
async def complete_upload(uid: str, request: Request):
    sha = FILE_DATA.get("_sha_" + uid, "deadbeef")
    fid = f"file_{uuid.uuid4().hex[:8]}"
    FILES[sha] = fid
    FILE_DATA[fid] = FILE_DATA.get(uid, b"")
    return {"id": uid, "status": "completed", "file": {"id": fid}}


@app.post("/v1/parse/jobs")
async def create_job(request: Request):
    body = await request.json()
    jid = f"job_{uuid.uuid4().hex[:8]}"
    files = []
    for i, f in enumerate(body.get("files", [])):
        files.append({"name": f"sample-{i}.pdf", "status": "queued",
                      "page_range": f.get("page_range")})
    from datetime import datetime, timezone
    JOBS[jid] = {"job_id": jid, "status": "queued", "tier": body.get("tier"),
                 "created_at": datetime.now(timezone.utc).isoformat()
                 .replace("+00:00", "Z"),
                 "output_formats": body.get("output_formats", ["markdown"]),
                 "progress": {"completed": 0, "failed": 0, "total": len(files)},
                 "files": files, "requested_formats": body.get("output_formats")}
    asyncio.get_event_loop().call_later(2.0, lambda: _finish(jid))
    return JSONResponse(JOBS[jid], status_code=202)


def _finish(jid: str):
    job = JOBS[jid]
    for f in job["files"]:
        f["status"] = "completed"
        out = {}
        for fmt in job["requested_formats"]:
            if fmt == "markdown":
                payload = f"# Parsed {f['name']}\n\nHello from mock.\n![img](images/p1.jpg)\n"
            elif fmt == "zip":
                import io
                import zipfile
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr(f"{f['name'].rsplit('.', 1)[0]}.md",
                                f"# Zip {f['name']}\n\n![x](images/a.png)\n")
                    zf.writestr("images/a.png", b"\x89PNG fake")
                payload = buf.getvalue()
            elif fmt == "middle_json":
                payload = json.dumps({"pages": []}).encode()
            else:
                payload = b"{}"
            fid = f"out_{uuid.uuid4().hex[:8]}"
            FILE_DATA[fid] = payload
            out[fmt] = {"file_id": fid, "bytes": len(payload)}
        f["output_files"] = out
    job["progress"] = {"completed": len(job["files"]), "failed": 0,
                       "total": len(job["files"])}
    job["status"] = "completed"


@app.get("/v1/parse/jobs")
async def list_jobs(limit: int = 20, after: str | None = None,
                    status: str | None = None, order: str = "desc"):
    jobs = [JOBS[k] for k in sorted(JOBS, key=lambda k: JOBS[k]["created_at"],
                                    reverse=(order == "desc"))]
    if status:
        wanted = {s.strip() for s in status.split(",")}
        jobs = [j for j in jobs if j["status"] in wanted]
    if after:
        idx = next((i for i, j in enumerate(jobs) if j["job_id"] == after), None)
        if idx is not None:
            jobs = jobs[idx + 1:]
    rows = [{"job_id": j["job_id"], "status": j["status"],
             "created_at": j["created_at"],
             "file_count": len(j["files"])} for j in jobs[:limit]]
    has_more = len(jobs) > limit
    return {"object": "list", "data": rows,
            "first_id": rows[0]["job_id"] if rows else None,
            "last_id": rows[-1]["job_id"] if rows else None,
            "has_more": has_more}


@app.get("/v1/parse/jobs/{jid}")
async def get_job(jid: str):
    if jid not in JOBS:
        return JSONResponse({"error": {"message": "job not found"}}, status_code=404)
    return JOBS[jid]


@app.delete("/v1/parse/jobs/{jid}")
async def cancel_job(jid: str):
    if jid not in JOBS:
        return JSONResponse({"error": {"message": "job not found"}}, status_code=404)
    JOBS[jid]["status"] = "canceled"
    return {"job_id": jid, "status": "canceled"}


@app.get("/v1/files/{fid}/content")
async def file_content(fid: str):
    data = FILE_DATA.get(fid)
    if data is None:
        return JSONResponse({"error": {"message": "file not found"}}, status_code=404)
    return Response(content=data, media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9999)
