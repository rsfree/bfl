"""管理中台：按出口 IP 看用量、改配额、重建映射。

访问控制（沿用既有运维规矩）
----------------------------
- 入口层 nginx 对 `/panel` 直接 `return 404`（stage-1 与 node-10 都要配）
- 只从 SSH 隧道访问：`ssh -L 8000:127.0.0.1:39100 node-10`
- 可选二次校验：设置 `BFL2PANEL_TOKEN` 后，请求需带
  `Authorization: Bearer <token>`。未设置则不校验（依赖上面两道）

不做写死的 IP 列表：槽位→IP 的映射是**探测出来的**（池子会变），
面板只读注册表，不维护第二份真相。
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import egress_pool

router = APIRouter(prefix="/panel", tags=["panel"])

_TOKEN = os.environ.get("BFL2PANEL_TOKEN", "")

#: 扫描是长任务：3000 个号段约 3-6 分钟，同步跑会被 nginx 的 300s 掐断。
#: 改为后台执行；**进度状态存在 Redis**（egress_pool.scan_status），
#: 因为 gunicorn 多 worker 下进程内变量互相看不见。
async def _run_scan() -> None:
    try:
        await egress_pool.scan()
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
        print(f"[panel] scan failed: {type(exc).__name__}: {exc}", flush=True)


def _check(request: Request) -> None:
    if not _TOKEN:
        return
    got = request.headers.get("authorization", "")
    want = f"Bearer {_TOKEN}"
    if not hmac.compare_digest(got, want):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/api/snapshot")
async def api_snapshot(request: Request):
    _check(request)
    snap = await egress_pool.snapshot()
    snap["now"] = int(time.time())
    snap["scan"] = await egress_pool.scan_status()
    return JSONResponse(snap)


@router.post("/api/quota")
async def api_quota(request: Request):
    """改配额。`quota` = 硬额度（上游真实上限），`soft` = 软上限（调度停手点）。"""
    _check(request)
    body = await request.json()
    try:
        slot = int(body["slot"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="slot required")
    if body.get("soft") is not None:
        await egress_pool.set_soft_quota(slot, int(body["soft"]))
        return {"ok": True, "slot": slot, "soft": int(body["soft"])}
    if body.get("quota") is not None:
        await egress_pool.set_quota(slot, int(body["quota"]))
        return {"ok": True, "slot": slot, "quota": int(body["quota"])}
    raise HTTPException(status_code=400, detail="quota or soft required")


@router.post("/api/scan")
async def api_scan(request: Request):
    """重建槽位→IP 映射（后台执行，就地消除碰撞，不清零既有用量）。"""
    _check(request)
    if not egress_pool.configured():
        raise HTTPException(status_code=400, detail="pool not configured")
    st = await egress_pool.scan_status()
    if st["running"]:
        return {"ok": False, "running": True, "started": st["started"]}
    asyncio.create_task(_run_scan())
    return {"ok": True, "running": True}


@router.post("/api/reset")
async def api_reset(request: Request):
    """手工清零当日用量。正常情况不该用到——只在上游配额提前重置时用。"""
    _check(request)
    return {"ok": True, "reset": await egress_pool.reset_day()}


_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>出口池中台 · bfl</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b7280;--line:#e5e7eb;--ok:#0f7b3f;--warn:#b45309;--bad:#b91c1c;--chip:#f3f4f6}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC",sans-serif;background:var(--bg);color:var(--fg)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px}
h1{font-size:16px;margin:0;font-weight:600}
.mut{color:var(--mut);font-size:12px}
main{padding:20px 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.card{border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card b{display:block;font-size:20px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--mut);font-weight:500;font-size:12px}
tr:hover td{background:#fafafa}
.bar{height:6px;background:var(--chip);border-radius:3px;overflow:hidden;width:110px}
.bar>i{display:block;height:100%;background:var(--ok)}
.chip{display:inline-block;padding:1px 7px;border-radius:999px;background:var(--chip);font-size:11px}
.s-dup{color:var(--warn)}.s-error{color:var(--bad)}.s-primary{color:var(--ok)}
.s-exhausted{color:var(--mut)}
button{border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px}
button:hover{background:var(--chip)}
input[type=number]{width:64px;border:1px solid var(--line);border-radius:5px;padding:3px 6px}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px}
</style></head><body>
<header><h1>出口池中台</h1><span class="mut" id="sub">加载中…</span></header>
<main>
  <div class="cards" id="cards"></div>
  <div class="toolbar">
    <button onclick="scan()">重建映射</button>
    <button onclick="load()">刷新</button>
    <input id="filter" placeholder="筛选 IP / 网段 / 槽位" oninput="render()"
           style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:12px;width:200px">
    <span class="mut" id="msg"></span>
  </div>
  <table><thead><tr>
    <th>槽位</th><th>出口 IP</th><th>状态</th><th>已用 / 软上限</th><th>安全余量</th>
    <th>进度</th><th>硬额度</th><th>429</th><th>失败</th><th>空闲</th><th>改软上限</th>
  </tr></thead><tbody id="rows"></tbody></table>
</main>
<script>
const el=(id)=>document.getElementById(id);
function pct(u,q){return q? Math.min(100,Math.round(u/q*100)):0}
function fmtCool(s){return s>0? Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s":"—"}
function card(label,v){return `<div class="card"><span class="mut">${label}</span><b>${v}</b></div>`}
let DATA=null;
const CAP=300;   // 槽位可达上千，一次全渲染会拖垮 DOM；默认只画前 300 行
function render(){
  if(!DATA)return;
  const q=(el("filter").value||"").trim().toLowerCase();
  let rows=DATA.rows;
  if(q) rows=rows.filter(r=>(r.ip||"").includes(q)||(r.prefix||"").includes(q)
                             ||String(r.slot).includes(q));
  const shown=rows.slice(0,CAP);
  el("rows").innerHTML=shown.map(r=>`<tr>
    <td>#${String(r.slot).padStart(3,"0")}</td>
    <td style="font-family:ui-monospace,monospace">${r.ip||"—"}<span class="mut" style="margin-left:6px">${r.prefix?r.prefix+".0/24":""}</span></td>
    <td class="s-${r.exhausted?"exhausted":r.state}">${r.exhausted?"额度用尽":r.state==="primary"?"可用":r.state==="dup"?"碰撞":"失败"}</td>
    <td>${r.used} / ${r.soft_quota}</td>
    <td>${r.remaining_safe}</td>
    <td><div class="bar"><i style="width:${pct(r.used,r.soft_quota)}%;background:${r.remaining_safe>0?"var(--ok)":"var(--bad)"}"></i></div></td>
    <td>${r.quota}</td>
    <td>${r.n429||""}</td><td>${r.fail||""}</td>
    <td>${r.idle_s==null?"—":fmtCool(r.idle_s)}</td>
    <td><input type="number" min="0" value="${r.soft_quota}" id="q${r.slot}">
        <button onclick="setQ(${r.slot})">改</button></td>
  </tr>`).join("") + (rows.length>CAP
    ? `<tr><td colspan="11" class="mut">另有 ${rows.length-CAP} 行未显示（用筛选框缩小范围）</td></tr>`
    : "");
}
async function load(){
  const r=await fetch("/panel/api/snapshot");
  if(!r.ok){el("msg").textContent="读取失败 "+r.status;return}
  DATA=await r.json();
  const d=DATA;
  el("sub").textContent=`${d.configured?"2090 pin 模式":"未配置"} · 槽位 ${d.slots} · 唯一 IP ${d.distinct_ips} · 软上限 ${d.soft_quota_per_ip}/IP${d.stretch?" · stretch 已开":""} · 构建于 ${d.built_at?new Date(d.built_at*1000).toLocaleString():"—"}`;
  el("cards").innerHTML =
    card("可用槽位",`${d.usable}/${d.slots}`)+
    card("安全余量",`${d.capacity_left} 张`)+
    card("今日已用",`${d.used_today} 张`)+
    card("软上限 / IP",`${d.soft_quota_per_ip} 张`)+
    card("唯一 IP / 网段",`${d.distinct_ips} / ${d.prefixes||0}`)+
    card("头部网段",`${d.top_prefix&&d.top_prefix.length?d.top_prefix[0][0]+" ("+d.top_prefix[0][1]+")":"—"}`)+
    card("碰撞剔除",`${d.dups}`)+
    card("探测失败",`${d.errors}`);
  render();
}
async function setQ(slot){
  const soft=Number(el("q"+slot).value);
  const r=await fetch("/panel/api/quota",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({slot,soft})});
  el("msg").textContent=r.ok?`槽位 ${slot} 软上限已改为 ${soft}`:"修改失败";
  load();
}
async function scan(){
  el("msg").textContent="已在后台开始探测，约需 2-3 分钟…";
  const r=await fetch("/panel/api/scan",{method:"POST"});
  if(!r.ok){el("msg").textContent="重建失败 "+r.status;return}
  const d=await r.json();
  if(!d.ok) el("msg").textContent="已有任务在跑，等待即可";
}
load();setInterval(load,15000);
</script></body></html>"""


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    _check(request)
    return HTMLResponse(_PAGE)
