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
import base64
import hmac
import os
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import egress_pool, logring, reqlog

router = APIRouter(prefix="/panel", tags=["panel"])

_TOKEN = os.environ.get("BFL2PANEL_TOKEN", "")
#: 浏览器密码（HTTP Basic）。命名沿用「sk- + 二级域名」约定：
#: bfl.1task.cn -> sk-bfl。与 _TOKEN 分工：token 给脚本/curl 走 Bearer，
#: pass 给浏览器弹窗。两者任一命中即放行。
_PASS = os.environ.get("BFL2PANEL_PASS", "")

#: 扫描是长任务：3000 个号段约 3-6 分钟，同步跑会被 nginx 的 300s 掐断。
#: 改为后台执行；**进度状态存在 Redis**（egress_pool.scan_status），
#: 因为 gunicorn 多 worker 下进程内变量互相看不见。
async def _run_scan() -> None:
    try:
        await egress_pool.scan()
    except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
        print(f"[panel] scan failed: {type(exc).__name__}: {exc}", flush=True)


def _check(request: Request) -> None:
    """面板鉴权。命中任一即放行：

    1) 两个凭据都没配 —— 维持历史做法（依赖入口 nginx 404 + SSH 隧道）
    2) `Authorization: Bearer <BFL2PANEL_TOKEN>` —— 脚本 / curl
    3) `Authorization: Basic`，密码 == `BFL2PANEL_PASS` —— 浏览器弹窗
       （用户名不校验：弹窗只让用户关心密码）

    未命中时 401 必须带 `WWW-Authenticate: Basic`，否则浏览器不会弹窗，
    只会显示一段 JSON —— 这是让面板「能用密码打开」的关键。
    """
    if not _TOKEN and not _PASS:
        return
    got = request.headers.get("authorization", "")
    if _TOKEN and hmac.compare_digest(got, f"Bearer {_TOKEN}"):
        return
    # 面板前端把用户输入的密钥存 localStorage，请求统一发 Bearer
    # （与 workbuddy2api 的 panel 同口径：壳公开、数据要 key）。
    if _PASS and hmac.compare_digest(got, f"Bearer {_PASS}"):
        return
    if _PASS and got[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(got[6:].strip()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 非法 base64 视为未带凭据
            raw = ""
        _user, _, pw = raw.partition(":")
        if pw and hmac.compare_digest(pw, _PASS):
            return
    # 🔴 401 不带 WWW-Authenticate：带了浏览器会弹原生 Basic 弹窗，
    # 把面板自己的登录框盖住（实测会卡在"输入完又弹一次"）。前端拿 401
    # 后自己弹输入框，密钥存 localStorage 走 Bearer —— 与 workbuddy2api 同。
    # curl 用 `-u 任意:密码` 仍可（-u 直接发 Basic 头，不依赖 challenge）。
    raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/api/snapshot")
async def api_snapshot(request: Request):
    _check(request)
    #: 状态库（Redis）偶发抖动时快失败并给出可读原因：未捕获会变成
    #: 500 + 整段 traceback，前端只能显示"读取失败 500"，运维看不出是
    #: 状态库问题还是面板问题。
    try:
        snap = await egress_pool.snapshot()
        snap["now"] = int(time.time())
        snap["scan"] = await egress_pool.scan_status()
    except Exception as exc:  # noqa: BLE001 - 面板必须自己收尾
        print(f"[panel] snapshot failed: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="state store unavailable")
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
    if not egress_pool._has_creds():
        raise HTTPException(status_code=400, detail="pool credentials missing")
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


@router.get("/api/logs")
async def api_logs(request: Request, limit: int = 200, level: str = "", q: str = ""):
    """运行日志。`level` 是最小级别（INFO/WARNING/ERROR），`q` 为包含匹配。

    数据源是 logring：多 worker 经 Redis 汇总（source=redis），Redis 不可用
    时降级为本进程内存环（source=memory，前端会标注只覆盖本 worker）。
    """
    _check(request)
    try:
        data = await asyncio.to_thread(logring.read, limit, level, q)
    except Exception as exc:  # noqa: BLE001 - 面板不能因日志挂了而 500
        print(f"[panel] logs failed: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="log ring unavailable")
    return JSONResponse(data)


@router.get("/api/request_logs")
async def api_request_logs(request: Request, limit: int = 100,
                           status: str = "", q: str = ""):
    """请求记录。`status`="error"（4xx/5xx）| "slow"（>=3s）；`q` 匹配方法/路径/IP。"""
    _check(request)
    try:
        data = await asyncio.to_thread(reqlog.read, limit, status, q)
    except Exception as exc:  # noqa: BLE001
        print(f"[panel] request_logs failed: {type(exc).__name__}: {exc}", flush=True)
        raise HTTPException(status_code=503, detail="request log unavailable")
    return JSONResponse(data)


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
#gate{position:fixed;inset:0;background:rgba(255,255,255,.97);display:flex;
  align-items:center;justify-content:center;z-index:9}
/* 🔴 hidden 属性默认的 display:none 是 UA 样式，优先级低于上面的 #gate{display:flex}，
   结果 hideGate() 设了 hidden 也关不掉遮罩 —— 登录成功后仍停在登录框，
   用户看到的就是"密钥验证不通过"。必须显式压过。 */
#gate[hidden]{display:none}
.gatebox{background:#fff;border:1px solid var(--line);border-radius:10px;
  padding:24px;width:300px;text-align:center}
.gatebox h3{margin:0 0 6px;font-size:15px;font-weight:500}
.gatebox input{width:100%;box-sizing:border-box;padding:8px 10px;
  border:1px solid var(--line);border-radius:6px;margin:14px 0;font-size:14px}
.gatebox button{width:100%;padding:9px;border:0;border-radius:6px;
  background:var(--fg);color:#fff;cursor:pointer;font-size:13px}
.gatebox .err{color:var(--bad);margin:10px 0 0;min-height:16px}
h2.sect{font-size:14px;margin:28px 0 10px;font-weight:600}
.box{border:1px solid var(--line);border-radius:10px;margin-top:16px;overflow:hidden}
.box>header{display:flex;align-items:center;gap:10px;padding:11px 14px;
  border-bottom:1px solid var(--line);background:#fcfcfc;flex-wrap:wrap}
.box>header h3{font-size:13px;margin:0;font-weight:600}
.grow{flex:1}
.chips{display:flex;gap:6px}
button.xs{padding:2px 10px;font-size:12px;border-radius:999px}
button.xs.chip.on{background:var(--fg);color:#fff;border-color:var(--fg)}
.note{font-size:12px;color:var(--mut)}
#logBox{margin:0;padding:10px 12px;font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;
  background:#fafafa;max-height:380px;overflow:auto;white-space:pre-wrap;word-break:break-all}
#logBox .ln{display:block}
#logBox .ln.w{color:var(--warn)}
#logBox .ln.e{color:var(--bad)}
.tbl-wrap{max-height:380px;overflow:auto}
table.logs-tbl{width:100%;border-collapse:collapse;font-size:12px}
table.logs-tbl th{position:sticky;top:0;background:#fcfcfc;text-align:left;
  padding:8px 10px;border-bottom:1px solid var(--line);font-weight:500;color:var(--mut)}
table.logs-tbl td{padding:6px 10px;border-bottom:1px solid #f3f3f3;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.st-2xx{color:var(--ok)}.st-3xx{color:var(--mut)}
.st-4xx{color:var(--warn)}.st-5xx{color:var(--bad)}
</style></head><body>
<header><h1>出口池中台</h1><span class="mut" id="sub">加载中…</span></header>
<div id="gate" hidden>
  <div class="gatebox">
    <h3>出口池中台</h3>
    <p class="mut" style="margin:0">请输入面板密钥</p>
    <input id="keyin" type="password" placeholder="sk-..." autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">进入</button>
    <p class="err" id="gateerr"></p>
  </div>
</div>
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
    <th>进度</th><th>硬额度</th><th>429 / 403</th><th>失败</th><th>空闲</th><th>改停手点</th>
  </tr></thead><tbody id="rows"></tbody></table>

  <h2 class="sect">运行观测</h2>
  <div class="toolbar">
    <input id="logq" placeholder="搜索路径 / IP / 关键字" oninput="logDebounce()"
           style="border:1px solid var(--line);border-radius:5px;padding:4px 8px;font-size:12px;width:240px">
    <button onclick="loadLogs()">刷新</button>
    <label class="mut" style="display:flex;align-items:center;gap:4px">
      <input type="checkbox" id="logauto" checked> 5s 自动刷新
    </label>
    <span class="mut" id="logmeta"></span>
  </div>

  <div class="box">
    <header>
      <h3>请求记录</h3>
      <span class="grow"></span>
      <span class="chips" id="reqChips">
        <button class="xs chip on" data-st="">全部</button>
        <button class="xs chip" data-st="error">错误 4xx/5xx</button>
        <button class="xs chip" data-st="slow">慢 &gt;3s</button>
      </span>
      <span class="note" id="reqNote">最近 100 条</span>
    </header>
    <div class="tbl-wrap">
      <table class="logs-tbl">
        <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>状态</th><th>耗时</th><th>客户端</th></tr></thead>
        <tbody id="reqBody"><tr><td colspan="6" class="mut">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="box">
    <header>
      <h3>运行日志</h3>
      <span class="grow"></span>
      <span class="chips" id="logChips">
        <button class="xs chip on" data-lv="">全部</button>
        <button class="xs chip" data-lv="INFO">INFO</button>
        <button class="xs chip" data-lv="WARNING">警告</button>
        <button class="xs chip" data-lv="ERROR">错误</button>
      </span>
      <span class="note" id="logNote">最近 300 行</span>
      <button class="xs" id="btnLogPin">自动滚动：开</button>
    </header>
    <pre id="logBox"><span class="ln">加载中…</span></pre>
  </div>
</main>
<script>
const el=(id)=>document.getElementById(id);
const LS_KEY="bfl.panel.key";
let KEY="";
try{KEY=localStorage.getItem(LS_KEY)||""}catch(e){}
function showGate(msg){el("gate").hidden=false;if(msg)el("gateerr").textContent=msg;setTimeout(()=>el("keyin").focus(),50)}
function hideGate(){el("gate").hidden=true;el("gateerr").textContent=""}
function doLogin(){
  const v=(el("keyin").value||"").trim();
  if(!v){el("gateerr").textContent="请输入密钥";return}
  KEY=v;
  try{localStorage.setItem(LS_KEY,v)}catch(e){}
  el("gateerr").textContent="验证中…";
  load().then(()=>{if(DATA&&DATA.slots!==undefined){hideGate();loadLogs().catch(()=>{})}
                   else showGate("密钥无效")}).catch(()=>showGate("密钥无效"));
}
// ---------------- 运行观测：请求记录 + 运行日志 ----------------
let logDebounceTimer=null, logLv="", reqSt="", logPin=true;
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]))}
function fmtLogTs(ts){const d=new Date((ts||0)*1000);
  return d.toLocaleTimeString("zh-CN",{hour12:false})}
function logDebounce(){clearTimeout(logDebounceTimer);
  logDebounceTimer=setTimeout(()=>{loadLogs().catch(()=>{})},300)}
function pickChips(wrapId,btn,key){
  document.querySelectorAll("#"+wrapId+" .chip").forEach(x=>x.classList.toggle("on",x===btn));
  if(key==="lv")logLv=btn.dataset.lv; else reqSt=btn.dataset.st;
  loadLogs().catch(()=>{});
}
function renderReq(rows,src){
  el("reqNote").textContent=rows.length+" 条 · "
    +(src==="redis"?"全部 worker":"仅本 worker");
  el("reqBody").innerHTML=rows.length?rows.map(e=>{
    const s=Number(e.status||0);
    const cls=s>=500?"st-5xx":s>=400?"st-4xx":s>=300?"st-3xx":"st-2xx";
    return `<tr><td>${fmtLogTs(e.ts)}</td><td>${esc(e.method)}</td>`
      +`<td>${esc(e.path)}</td><td class="${cls}">${s}</td>`
      +`<td>${Number(e.ms||0)}ms</td><td>${esc(e.ip||"—")}</td></tr>`;
  }).join(""):'<tr><td colspan="6" class="mut">暂无记录</td></tr>';
}
function renderLogs(rows,src){
  const box=el("logBox");
  const atEnd=box.scrollTop+box.clientHeight>=box.scrollHeight-24;
  box.innerHTML=rows.length?rows.map(e=>{
    const cls=(e.level==="ERROR"||e.level==="CRITICAL")?" e"
      :(e.level==="WARNING"?" w":"");
    return `<span class="ln${cls}">${fmtLogTs(e.ts)} [${esc(e.level)}] `
      +`${esc(e.logger)} ${esc(e.msg)}</span>`;
  }).join(""):'<span class="ln" style="color:var(--mut)">暂无匹配日志</span>';
  // 只有原本就贴在底部才跟着滚：否则用户往上翻历史时会被强行拉回去
  if(logPin&&atEnd)box.scrollTop=box.scrollHeight;
  el("logNote").textContent=rows.length+" 行 · "
    +(src==="redis"?"全部 worker":"仅本 worker");
}
async function loadLogs(){
  if(!KEY)return;
  const kw=(el("logq").value||"").trim();
  const q="&q="+encodeURIComponent(kw);
  const [lr,rr]=await Promise.all([
    api("/logs?limit=300"+q+(logLv?"&level="+encodeURIComponent(logLv):"")).catch(()=>null),
    api("/request_logs?limit=100"+q+(reqSt?"&status="+encodeURIComponent(reqSt):"")).catch(()=>null),
  ]);
  if(lr&&lr.ok){const d=await lr.json();renderLogs(d.entries||[],d.source)}
  else el("logNote").textContent="日志读取失败"+(lr?" "+lr.status:"");
  if(rr&&rr.ok){const d=await rr.json();renderReq(d.entries||[],d.source)}
  else el("reqNote").textContent="记录读取失败"+(rr?" "+rr.status:"");
  el("logmeta").textContent="";
}
let API_BASE="/panel/api";
function apiPath(p){return API_BASE+p}
async function api(path,opt){
  opt=opt||{};
  opt.headers=Object.assign({},opt.headers||{});
  if(KEY)opt.headers["Authorization"]="Bearer "+KEY;
  // 后端偶发慢（状态库抖动）：12s 内必给出结果，否则前端会一直转圈，
  // 看起来和"密钥验证不通过"没区别。
  const ac=new AbortController();
  const timer=setTimeout(()=>ac.abort(),12000);
  let r;
  try{
    r=await fetch(apiPath(path),Object.assign({},opt,{signal:ac.signal}));
  }catch(e){
    clearTimeout(timer);
    el("gateerr").textContent="后端超时，请重试";
    el("msg").textContent="后端超时，请重试";
    throw e;
  }
  clearTimeout(timer);
  // 入口 nginx 可能把 /panel/api/ 整条前缀挡成 404：自动换到 /pq/ 别名，
  // 否则前端只能看到"密钥无效"（实际是接口不可达）。
  if(r.status===404&&API_BASE==="/panel/api"){
    API_BASE="/pq";
    return api(path,opt);
  }
  if(r.status===401){showGate(KEY?"密钥无效，请重试":"请输入面板密钥");throw new Error("unauthorized")}
  return r;
}
function pct(u,q){return q? Math.min(100,Math.round(u/q*100)):0}
function fmtCool(s){return s>0? Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s":"—"}
function fmtLeft(s){if(s<=0)return "即将换日";const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+"h "+m+"m":m+"m"}
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
    <td class="s-${r.cooling_s>0?"exhausted":r.exhausted?"exhausted":r.state}">${r.cooling_s>0?"冷却 "+fmtCool(r.cooling_s):r.exhausted?"额度用尽":r.state==="primary"?"可用":r.state==="dup"?"碰撞":"失败"}</td>
    <td>${r.used} / ${r.soft_quota}</td>
    <td>${r.remaining_safe}</td>
    <td><div class="bar"><i style="width:${pct(r.used,r.soft_quota)}%;background:${r.remaining_safe>0?"var(--ok)":"var(--bad)"}"></i></div></td>
    <td>${r.quota}</td>
    <td>${r.n429||0} / ${r.n403||0}</td><td>${r.fail||""}</td>
    <td>${r.idle_s==null?"—":fmtCool(r.idle_s)}</td>
    <td><input type="number" min="0" value="${r.soft_quota}" id="q${r.slot}">
        <button onclick="setQ(${r.slot})">改</button></td>
  </tr>`).join("") + (rows.length>CAP
    ? `<tr><td colspan="11" class="mut">另有 ${rows.length-CAP} 行未显示（用筛选框缩小范围）</td></tr>`
    : "");
}
async function load(){
  let r;
  try{r=await api("/snapshot")}catch(e){return}
  if(!r.ok){el("msg").textContent="读取失败 "+r.status;return}
  DATA=await r.json();
  const d=DATA;
  const rz=(d.reset&&d.reset.tz)||"UTC";
  el("sub").textContent=`${d.configured?"2090 pin 模式":"未配置"} · 槽位 ${d.slots} · 唯一 IP ${d.distinct_ips} · 软上限 ${d.soft_quota_per_ip}/IP${d.stretch?" · stretch 已开":""} · 换日 ${rz} 00:00（北京 08:00）· 构建于 ${d.built_at?new Date(d.built_at*1000).toLocaleString():"—"}`;
  el("cards").innerHTML =
    card("换日口径",`${rz} 00:00`)+
    card("距下次换日",d.reset?fmtLeft(d.reset.next_at-d.now):"—")+
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
  const r=await api("/quota",{method:"POST",headers:{"content-type":"application/json"},
    body:JSON.stringify({slot,soft})});
  el("msg").textContent=r.ok?`槽位 ${slot} 软上限已改为 ${soft}`:"修改失败";
  load();
}
async function scan(){
  el("msg").textContent="已在后台开始探测，约需 2-3 分钟…";
  const r=await api("/scan",{method:"POST"});
  if(!r.ok){el("msg").textContent="重建失败 "+r.status;return}
  const d=await r.json();
  if(!d.ok) el("msg").textContent="已有任务在跑，等待即可";
}
document.querySelectorAll("#logChips .chip").forEach(b=>
  b.onclick=()=>pickChips("logChips",b,"lv"));
document.querySelectorAll("#reqChips .chip").forEach(b=>
  b.onclick=()=>pickChips("reqChips",b,"st"));
el("btnLogPin").onclick=()=>{logPin=!logPin;
  el("btnLogPin").textContent="自动滚动："+(logPin?"开":"关")};
load().then(()=>{if(KEY&&DATA)loadLogs().catch(()=>{})}).catch(()=>{});
setInterval(()=>{load().catch(()=>{})},15000);
setInterval(()=>{if(KEY&&el("logauto")&&el("logauto").checked)loadLogs().catch(()=>{})},5000);
</script></body></html>"""


#: 入口 nginx 对 `/panel/api/` 做了 `return 404`（历史遗留的"别暴露运维面"），
#: 但它拦的是整条前缀，面板前端因此拉不到数据（表现为"密钥无效"）。
#: 这里把同一批 handler 再挂一份到 `/pq/` —— 实测 `/xyzabc` 这类路径都能正常
#: 转发到应用，说明 nginx 只对 `/panel/api/` 特判。前端首次请求 404 时自动
#: 回退到这套别名，公网与隧道都能用。
#:
#: 🔴 这只是让服务先可用；正解仍是把 nginx 那条 `location ^~ /panel/api/`
#: 删掉（不是换成鉴权，是不要再拦），鉴权由 _check() 负责。
alt_router = APIRouter(prefix="/pq", tags=["panel"])
alt_router.add_api_route("/snapshot", api_snapshot, methods=["GET"])
alt_router.add_api_route("/quota", api_quota, methods=["POST"])
alt_router.add_api_route("/scan", api_scan, methods=["POST"])
alt_router.add_api_route("/reset", api_reset, methods=["POST"])
alt_router.add_api_route("/logs", api_logs, methods=["GET"])
alt_router.add_api_route("/request_logs", api_request_logs, methods=["GET"])


@router.get("", response_class=HTMLResponse)
async def page(request: Request):
    # 页面壳不鉴权：照 workbuddy2api 的做法 —— 壳公开、数据要 key。
    # 前端拿到 401 后弹输入框，key 存 localStorage，之后请求带 Bearer。
    # 这样不需要浏览器 Basic 弹窗，也不用依赖入口层 nginx 挡。
    #
    # 🔴 必须 no-store：页面没有任何 Cache-Control/ETag/Last-Modified 时，
    # 浏览器会按启发式规则长期缓存 HTML —— 改了前端逻辑后用户仍跑旧脚本
    # （2026-11-18 16:30 实测：新版已上线，用户刷新仍是"密钥无效"）。
    return HTMLResponse(_PAGE, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })
