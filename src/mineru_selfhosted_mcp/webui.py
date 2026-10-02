"""Web UI job board: read-only views over the PostgreSQL journal.

Serves GET /ui (single-file HTML+JS board, no frontend dependencies) and
read-only JSON APIs under /ui/api/. All data comes from the journal tables;
the UI never triggers parse work.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .journal import JobJournal

log = logging.getLogger("mineru-selfhosted-mcp.webui")


def register_webui(app, journal: JobJournal) -> None:
    router = APIRouter()

    @router.get("/ui")
    async def ui_page() -> Any:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(_PAGE_HTML)

    @router.get("/ui/api/summary")
    async def summary() -> Any:
        if not journal.enabled:
            return {"enabled": False}
        pool = await journal.ensure_pool()
        if pool is None:
            raise HTTPException(503, "journal unavailable")
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("""
                SELECT state, COUNT(*) FROM mineru_jobs GROUP BY state
            """)
            by_state = {r[0]: r[1] for r in await cur.fetchall()}
            await cur.execute("""
                SELECT COUNT(*), COALESCE(SUM(file_count),0),
                       COALESCE(SUM(published_files),0)
                  FROM mineru_jobs
            """)
            total_jobs, total_files, published = (await cur.fetchone())
            await cur.execute("""
                SELECT user_label, COUNT(*) AS jobs
                  FROM mineru_jobs GROUP BY user_label
                 ORDER BY jobs DESC LIMIT 10
            """)
            users = [{"user": r[0] or "anonymous", "jobs": r[1]}
                     for r in await cur.fetchall()]
        return {"enabled": True, "total_jobs": total_jobs,
                "total_files": total_files, "published_files": published,
                "by_state": by_state, "users": users}

    @router.get("/ui/api/jobs")
    async def jobs(
        request: Request,
        limit: int = Query(50, le=200),
        offset: int = Query(0, ge=0),
        state: str | None = None,
        user: str | None = None,
    ) -> Any:
        if not journal.enabled:
            return {"enabled": False, "jobs": [], "count": 0}
        pool = await journal.ensure_pool()
        if pool is None:
            raise HTTPException(503, "journal unavailable")
        where, where_params = [], []
        if state:
            where.append("state = %s")
            where_params.append(state)
        if user:
            where.append("(user_label = %s OR user_id = %s)")
            where_params.extend([user, user])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params = where_params + [limit, offset]
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"""
                SELECT job_id, client_name, user_label, state, tier, ocr_mode,
                       output_formats, file_count, pre_errors, warnings,
                       completed_files, failed_files, total_files,
                       published_files, download_urls, created_at, updated_at
                  FROM mineru_jobs {clause}
                 ORDER BY updated_at DESC LIMIT %s OFFSET %s
            """, tuple(params))
            rows = [dict(zip([
                "job_id", "client_name", "user_label", "state", "tier",
                "ocr_mode", "output_formats", "file_count", "pre_errors",
                "warnings", "completed_files", "failed_files", "total_files",
                "published_files", "download_urls", "created_at", "updated_at",
            ], r)) for r in await cur.fetchall()]
            await cur.execute(f"""
                SELECT COUNT(*) FROM mineru_jobs {clause}
            """, tuple(where_params))
            total = (await cur.fetchone())[0]
        return {"enabled": True, "total": total, "count": len(rows), "jobs": rows}

    @router.get("/ui/api/jobs/{job_id}")
    async def job_detail(job_id: str) -> Any:
        if not journal.enabled:
            raise HTTPException(404, "journal disabled")
        pool = await journal.ensure_pool()
        if pool is None:
            raise HTTPException(503, "journal unavailable")
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("""
                SELECT job_id, client_name, user_id, user_label, transport,
                       source, state, tier, ocr_mode, output_formats,
                       file_count, file_names, pre_errors, warnings,
                       completed_files, failed_files, total_files,
                       published_files, download_urls, detail,
                       created_at, updated_at
                  FROM mineru_jobs WHERE job_id = %s
            """, (job_id,))
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(404, f"unknown job '{job_id}'")
            cols = ["job_id", "client_name", "user_id", "user_label",
                    "transport", "source", "state", "tier", "ocr_mode",
                    "output_formats", "file_count", "file_names",
                    "pre_errors", "warnings", "completed_files",
                    "failed_files", "total_files", "published_files",
                    "download_urls", "detail", "created_at", "updated_at"]
            job = dict(zip(cols, row))
            await cur.execute("""
                SELECT ts, event, message FROM mineru_job_events
                 WHERE job_id = %s ORDER BY id
            """, (job_id,))
            job["events"] = [{"ts": str(r[0]), "event": r[1], "message": r[2]}
                             for r in await cur.fetchall()]
        return job

    app.include_router(router)


_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>MinerU MCP 任务看板</title>
<style>
 :root { --bg:#0f1420; --panel:#171e2e; --line:#28324a; --fg:#dbe2f0;
         --dim:#8792ad; --accent:#4f8cff; --ok:#37c988; --warn:#e8b34b;
         --err:#e85d5d; }
 * { box-sizing:border-box; }
 body { margin:0; background:var(--bg); color:var(--fg);
        font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; }
 header { display:flex; align-items:center; gap:12px; padding:14px 22px;
          border-bottom:1px solid var(--line); }
 header h1 { font-size:17px; margin:0; }
 header .sub { color:var(--dim); font-size:12px; }
 header .sp { flex:1; }
 main { padding:18px 22px; max-width:1400px; margin:0 auto; }
 .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
          gap:12px; margin-bottom:18px; }
 .card { background:var(--panel); border:1px solid var(--line);
         border-radius:10px; padding:12px 16px; }
 .card .num { font-size:24px; font-weight:600; }
 .card .lbl { color:var(--dim); font-size:12px; margin-top:2px; }
 .num.ok { color:var(--ok); } .num.warn { color:var(--warn); }
 .num.err { color:var(--err); } .num.acc { color:var(--accent); }
 .toolbar { display:flex; gap:8px; align-items:center; margin-bottom:12px;
            flex-wrap:wrap; }
 select, input, button { background:var(--panel); color:var(--fg);
        border:1px solid var(--line); border-radius:8px; padding:6px 10px;
        font:inherit; }
 button { cursor:pointer; }
 table { width:100%; border-collapse:collapse; background:var(--panel);
         border:1px solid var(--line); border-radius:10px; overflow:hidden; }
 th, td { padding:8px 10px; border-bottom:1px solid var(--line);
          text-align:left; font-size:13px; }
 th { color:var(--dim); font-weight:500; background:#131a29; white-space:nowrap; }
 tr:hover td { background:#1b2438; }
 .st { display:inline-block; padding:1px 9px; border-radius:99px;
       font-size:12px; border:1px solid; }
 .st.completed,.st.success { color:var(--ok); border-color:var(--ok); }
 .st.partial,.st.running,.st.queued { color:var(--warn); border-color:var(--warn); }
 .st.failed,.st.error,.st.canceled { color:var(--err); border-color:var(--err); }
 a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
 .muted { color:var(--dim); }
 .mono { font-family:Consolas,monospace; font-size:12px; }
 dialog { background:var(--panel); color:var(--fg); border:1px solid var(--line);
          border-radius:12px; max-width:860px; width:92%; }
 dialog::backdrop { background:rgba(0,0,0,.55); }
 dl { display:grid; grid-template-columns:130px 1fr; gap:4px 12px; margin:10px 0; }
 dt { color:var(--dim); } dd { margin:0; word-break:break-all; }
 #events { max-height:260px; overflow:auto; border:1px solid var(--line);
           border-radius:8px; padding:8px 12px; }
 #events div { padding:2px 0; border-bottom:1px dashed var(--line); font-size:12px; }
 .err-banner { background:#3a1f24; border:1px solid var(--err); color:var(--err);
               padding:10px 14px; border-radius:10px; margin-bottom:14px; display:none; }
</style>
</head>
<body>
<header>
  <h1>MinerU MCP 任务看板</h1>
  <span class="sub">192.168.210.251:7000 · liteLLM multi-user journal</span>
  <span class="sp"></span>
  <span class="sub" id="refresh-in"></span>
  <button id="btn-refresh">刷新</button>
  <label class="sub"><input type="checkbox" id="auto" checked> 自动</label>
</header>
<main>
  <div id="banner" class="err-banner"></div>
  <div class="cards" id="cards"></div>
  <div class="toolbar">
    <select id="f-state">
      <option value="">全部状态</option>
      <option value="queued">queued</option>
      <option value="running">running</option>
      <option value="completed">completed</option>
      <option value="partial">partial</option>
      <option value="failed">failed</option>
      <option value="canceled">canceled</option>
    </select>
    <input id="f-user" placeholder="按用户过滤 (user_label/user_id)" size="28">
    <button id="btn-apply">应用</button>
    <span class="sub" id="rowinfo"></span>
  </div>
  <table>
    <thead><tr>
      <th>job_id</th><th>用户</th><th>客户端</th><th>状态</th>
      <th>文件</th><th>完成/失败</th><th>产物</th><th>tier</th>
      <th>输出格式</th><th>更新时间</th><th></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</main>
<dialog id="dlg">
  <h3 style="margin:6px 0 2px" id="d-title"></h3>
  <div id="d-body"></div>
  <div style="text-align:right; margin:8px 0">
    <button id="d-close">关闭</button>
  </div>
</dialog>
<script>
const $ = s => document.querySelector(s);
let timer = null, secs = 5;

function esc(s){ return String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function fmtTime(s){ return s ? String(s).replace('T',' ').slice(0,19) : '—'; }

function stBadge(s){ return `<span class="st ${esc(s)}">${esc(s||'—')}</span>`; }

function showError(m){
  const b = $('#banner'); b.textContent = m; b.style.display = 'block';
}
function hideError(){ $('#banner').style.display = 'none'; }

async function jget(url){
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.json();
}

async function loadSummary(){
  try {
    const d = await jget('/ui/api/summary');
    hideError();
    if (d.enabled === false) {
      $('#cards').innerHTML =
        `<div class="card"><div class="num warn">—</div>
         <div class="lbl">journal 未启用 (MINERU_PG_DSN 未配置)</div></div>`;
      return;
    }
    const bs = d.by_state || {};
    const card = (n,l,c) =>
      `<div class="card"><div class="num ${c||''}">${n}</div>
       <div class="lbl">${l}</div></div>`;
    $('#cards').innerHTML =
      card(d.total_jobs ?? 0, '任务总数', 'acc') +
      card(bs.completed ?? 0, 'completed', 'ok') +
      card((bs.running ?? 0) + (bs.queued ?? 0), '运行/排队', 'warn') +
      card(bs.failed ?? 0, 'failed', 'err') +
      card(d.total_files ?? 0, '文件总数') +
      card(d.published_files ?? 0, '已发布产物', 'ok');
  } catch (e) { showError('统计加载失败: ' + e.message); }
}

async function loadJobs(){
  try {
    const state = encodeURIComponent($('#f-state').value);
    const user = encodeURIComponent($('#f-user').value.trim());
    const d = await jget(`/ui/api/jobs?limit=100&state=${state}&user=${user}`);
    if (d.enabled === false) { $('#rows').innerHTML = ''; return; }
    $('#rowinfo').textContent = `${d.count} / ${d.total} 条`;
    $('#rows').innerHTML = (d.jobs || []).map(j => `
      <tr>
        <td class="mono">${esc(j.job_id)}</td>
        <td>${esc(j.user_label || 'anonymous')}</td>
        <td class="muted">${esc(j.client_name || '—')}</td>
        <td>${stBadge(j.state)}</td>
        <td>${j.file_count ?? 0}</td>
        <td class="muted">${j.completed_files ?? 0} / ${j.failed_files ?? 0}</td>
        <td>${j.published_files ?? 0}</td>
        <td class="muted">${esc(j.tier || '—')}</td>
        <td class="muted">${esc((j.output_formats||[]).join(',')) || '—'}</td>
        <td class="muted">${fmtTime(j.updated_at)}</td>
        <td><a href="#" onclick="openDetail('${esc(j.job_id)}');return false;">详情</a></td>
      </tr>`).join('') ||
      `<tr><td colspan="11" class="muted" style="text-align:center">暂无任务记录</td></tr>`;
  } catch (e) { showError('任务列表加载失败: ' + e.message); }
}

async function openDetail(jobId){
  try {
    const j = await jget(`/ui/api/jobs/${encodeURIComponent(jobId)}`);
    $('#d-title').textContent = '任务 ' + j.job_id;
    const urls = (j.download_urls || []).slice(0, 8);
    $('#d-body').innerHTML = `
      <dl>
        <dt>状态</dt><dd>${stBadge(j.state)}</dd>
        <dt>用户</dt><dd>${esc(j.user_label || 'anonymous')} ${j.user_id ? `(id: ${esc(j.user_id)})` : ''}</dd>
        <dt>客户端</dt><dd>${esc(j.client_name || '—')} · ${esc(j.transport || '—')} · ${esc(j.source || 'mcp')}</dd>
        <dt>解析参数</dt><dd>tier=${esc(j.tier || '—')} · ocr=${esc(j.ocr_mode || '—')} · [${esc((j.output_formats||[]).join(', '))}]</dd>
        <dt>文件</dt><dd>${j.file_count ?? 0} 个 · 完成 ${j.completed_files ?? 0} / 失败 ${j.failed_files ?? 0} / 总 ${j.total_files ?? 0}</dd>
        <dt>文件名</dt><dd class="mono">${esc((j.file_names||[]).join(', ')) || '—'}</dd>
        <dt>预校验</dt><dd>errors=${j.pre_errors ?? 0} · warnings=${j.warnings ?? 0}</dd>
        <dt>产物</dt><dd>${j.published_files ?? 0} 个已发布${urls.length ? `：<br>` + urls.map(u => `<a href="${esc(u)}" target="_blank">${esc(u)}</a>`).join('<br>') : ''}</dd>
        <dt>时间</dt><dd>创建 ${fmtTime(j.created_at)} · 更新 ${fmtTime(j.updated_at)}</dd>
      </dl>
      <div class="sub" style="margin:8px 0 4px">事件时间线</div>
      <div id="events">${(j.events||[]).map(e =>
        `<div><span class="muted mono">${fmtTime(e.ts)}</span>
         <b>${esc(e.event)}</b> ${esc(e.message||'')}</div>`).join('') ||
        '<div class="muted">无事件</div>'}</div>`;
    $('#dlg').showModal();
  } catch (e) { showError('详情加载失败: ' + e.message); }
}

async function refresh(){
  await Promise.all([loadSummary(), loadJobs()]);
}

$('#btn-refresh').onclick = refresh;
$('#btn-apply').onclick = () => { hideError(); loadJobs(); };
$('#f-state').onchange = loadJobs;
$('#d-close').onclick = () => $('#dlg').close();

$('#auto').onchange = () => {
  clearInterval(timer); timer = null;
  if ($('#auto').checked) timer = setInterval(tick, 1000);
  else $('#refresh-in').textContent = '';
};
function tick(){
  secs -= 1;
  $('#refresh-in').textContent = `${secs}s 后刷新`;
  if (secs <= 0) { secs = 5; refresh(); }
}
timer = setInterval(tick, 1000);

refresh();
</script>
</body>
</html>
"""
