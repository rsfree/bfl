"""Playwright 浏览器出口：真实 Chromium 环境内的 fetch 代理。

上游 flux-tools 对 curl_cffi 的 HTTP/2 指纹识别为 bot（403 Access denied），
只放行真实浏览器环境。本服务常驻 N 个独立 Chromium context，通过页面内
fetch 完成 提交→轮询→取图 全流程（纯网络调用，可并发，不受 UI 40s 串行限制）。

防风控策略（2026-10-26 加固）：
  1. 指纹分散：每页独立 profile（视口/DPR/CPU 核数/设备内存/WebGL 渲染器/
     languages），UA 版本取自真实 Chromium（避免与 sec-ch-ua 矛盾）；
  2. 时区地域：默认对齐代理出口所在时区（容器 UTC 是典型机房信号）；
  3. 节奏拟人：轮询间隔抖动 0.85~1.5x、单页提交最小间隔、全局并发上限；
  4. 出口轮换：429（IP 配额）标记该页冷却并换页；每页 N 次提交后回收
     context（新连接=新出口 IP + 干净 cookie）；
  5. 熔断：连续 403 判定上游策略收紧，全站短路冷却，避免烧 IP 与 CPU。

POST /generate {image_b64, prompt, aspect_ratio, resolution, mode}
  -> {image_b64} | {error}
GET  /healthz -> 池/熔断/错误统计
"""

import asyncio
import base64
import json
import os
import random
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel

BASE = "https://flux-tools.bfl.ai"
PAGE_URL = f"{BASE}/precise-editing"
POLL_INTERVAL_S = float(os.environ.get("BFL2OPENAI_EGRESS_POLL_S", "1.0"))
#: 轮询上限必须短于调用方超时（bfl2openai 侧 180s）：否则客户先断开，
#: 我们连"为什么失败"都送不回去（实测一笔 pending 280s 的请求就这么消失的）。
POLL_TIMEOUT_S = float(os.environ.get("BFL2OPENAI_EGRESS_POLL_TIMEOUT", "165"))
#: 页面池大小。默认 8（每页独立 context = 独立出口 IP），与生产 compose 的显式
#: 配置一致——不带环境变量直接跑也是够用的池子，别退回 4 页把并发压死。
PAGES = int(os.environ.get("BFL2OPENAI_EGRESS_PAGES", "8"))
#: 池代理（socks5://user:pass@host:port）：Chromium 直连=服务器 IP，已被上游
#: 按 IP 限流（429）；真浏览器指纹 + 换出口 IP 才能过。
PROXY_URL = os.environ.get("BFL2OPENAI_EGRESS_PROXY", "")

#: 全局并发上限。默认 20（8 页 → 每页 cap = ceil(20/8) = 3，见 acquire_entry）。
#: 这是「池子总量」而非每页放开：单 IP 并发连接越多，越容易撞上游配额。
MAX_INFLIGHT = int(os.environ.get("BFL2OPENAI_EGRESS_MAX_INFLIGHT", "20"))
#: 单个 context 用 N 次提交后回收：新连接拿新出口 IP + 干净 cookie，
#: 避免长时间「一个会话跑几百张」的非人类画像。
MAX_REQ_PER_CTX = int(os.environ.get("BFL2OPENAI_EGRESS_MAX_REQ_PER_CTX", "25"))
#: 时区须与代理出口地域一致（UTC 是机房信号）。可用环境变量覆盖成 US/... 。
TZ_ID = os.environ.get("BFL2OPENAI_EGRESS_TZ", "America/New_York")
#: 连续 403 达到阈值 → 熔断（说明上游策略又收紧了，猛打只会烧 IP）。
BREAKER_THRESHOLD = int(os.environ.get("BFL2OPENAI_EGRESS_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN_S = float(
    os.environ.get("BFL2OPENAI_EGRESS_BREAKER_COOLDOWN", "60"))
#: 429 后该页冷却秒数（出口 IP 配额被打热，短期复用只会继续 429）。
IP_COOLDOWN_S = float(os.environ.get("BFL2OPENAI_EGRESS_IP_COOLDOWN", "20"))

app = FastAPI()
_state: dict = {
    "pool": [], "lock": asyncio.Lock(), "recycle_lock": asyncio.Lock(),
    "browser": None, "pw": None, "sem": None, "total_inflight": 0,
}
_breaker = {"fails": 0, "opened_until": 0.0, "trips": 0, "last_reason": ""}

SUBMIT_JS = """
async ({prompt, aspectRatio, resolution, mode, imgB64, boxes, references, refs}) => {
  // 契约（2026-10-09 实测）：空 image 部件 ≠ 无 image —— 上游会回
  // 400 "A composition must not include a canvas image."。无图时必须整个省略。
  const hasImage = !!(imgB64 && imgB64.length);
  // auto 表示「从画布推导」；没有画布时上游要求显式比例（2026-10-09）。
  let ar = aspectRatio;
  if (!hasImage && ar === "auto") ar = "1:1";

  const fd = new FormData();
  fd.append("mode", mode);
  fd.append("prompt", prompt);
  fd.append("aspectRatio", ar);
  fd.append("resolution", resolution);
  // boxes/references 两个 JSON 字段必须始终存在，缺失会被当成 composition
  // （t2i）→ 带 image 即 400（2026-10-09/14 实测）。
  fd.append("boxes", boxes || "[]");
  fd.append("references", references || "[]");
  if (hasImage) {
    const bin = atob(imgB64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    fd.append("image", new Blob([arr], {type: "image/png"}), "image.png");
  }
  if (refs && refs.length) {
    for (let i = 0; i < refs.length; i++) {
      const rb = atob(refs[i]);
      const ra = new Uint8Array(rb.length);
      for (let j = 0; j < rb.length; j++) ra[j] = rb.charCodeAt(j);
      fd.append("references", new Blob([ra], {type: "image/png"}), `ref${i}.png`);
    }
  }
  const r = await fetch("/api/flux3-image/generate", {method: "POST", body: fd});
  const t = await r.text();
  return {status: r.status, body: t};
}
"""

STATUS_JS = """
async (token) => {
  const r = await fetch("/api/flux3-image/status?pollToken=" + encodeURIComponent(token));
  const t = await r.text();
  return {status: r.status, body: t};
}
"""

MEDIA_JS = """
async (token) => {
  const r = await fetch("/api/flux3-image/media?imageToken=" + encodeURIComponent(token));
  if (r.status !== 200) return {status: r.status, body: await r.text()};
  const buf = await r.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let s = "";
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  }
  return {status: r.status, b64: btoa(s)};
}
"""

#: 每页注入：抹掉自动化痕迹 + 按 profile 定制可探测维度。
#: %PROFILE% 由 json.dumps(dict) 替换（JSON 是合法 JS 字面量）。
INIT_JS = """
(() => {
  const P = %PROFILE%;
  try { delete Navigator.prototype.webdriver; } catch (e) {}
  try {
    if ("webdriver" in navigator) {
      Object.defineProperty(navigator, "webdriver", {get: () => undefined});
    }
  } catch (e) {}

  const def = (obj, k, v) => {
    try { Object.defineProperty(obj, k, {get: () => v, configurable: true}); }
    catch (e) {}
  };
  def(navigator, "hardwareConcurrency", P.hw);
  def(navigator, "deviceMemory", P.mem);
  def(navigator, "languages", P.langs);
  def(navigator, "language", P.langs[0]);

  // plugins/mimeTypes：headless 常为空数组，是最经典的自动化标记之一
  try {
    const mk = (name, filename, desc) => {
      const p = Object.create(Plugin.prototype);
      Object.defineProperty(p, "name", {value: name});
      Object.defineProperty(p, "filename", {value: filename});
      Object.defineProperty(p, "description", {value: desc});
      Object.defineProperty(p, "length", {value: 1});
      return p;
    };
    const arr = [
      mk("PDF Viewer", "internal-pdf-viewer", "Portable Document Format"),
      mk("Chrome PDF Viewer", "internal-pdf-viewer", "Portable Document Format"),
      mk("Chromium PDF Viewer", "internal-pdf-viewer", "Portable Document Format"),
    ];
    arr.item = (i) => arr[i];
    arr.namedItem = (n) => arr.find((p) => p.name === n) || null;
    Object.defineProperty(navigator, "plugins", {get: () => arr, configurable: true});
  } catch (e) {}

  // WebGL：swiftshader 渲染器字符串是「无 GPU 机房」的强信号，换成常见显卡
  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return P.glVendor;    // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return P.glRenderer;  // UNMASKED_RENDERER_WEBGL
      return orig.call(this, p);
    };
  };
  try { patchGL(WebGLRenderingContext && WebGLRenderingContext.prototype); } catch (e) {}
  try { patchGL(WebGL2RenderingContext && WebGL2RenderingContext.prototype); } catch (e) {}

  // permissions.query 的 notifications 状态不一致也是常见判定点
  try {
    const oq = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (p) =>
      (p && p.name === "notifications")
        ? Promise.resolve({state: Notification.permission, onchange: null})
        : oq(p);
  } catch (e) {}

  // 部分检测依赖 window.chrome 存在（真 Chrome 必有）
  try {
    if (!window.chrome) {
      window.chrome = {runtime: {}, app: {isInstalled: false},
                       csi: () => {}, loadTimes: () => {}};
    }
  } catch (e) {}

  // 视口/DPR 与 profile 对齐（window.outerWidth 与 inner 的差值也要像浏览器）
  try {
    Object.defineProperty(window, "outerWidth", {get: () => P.vw + 16});
    Object.defineProperty(window, "outerHeight", {get: () => P.vh + 88});
  } catch (e) {}
})();
"""

_VIEWPORTS = [
    (1920, 1080, 1.0), (1680, 1050, 1.0), (1536, 864, 1.25),
    (1440, 900, 2.0), (1600, 900, 1.0), (1728, 1117, 2.0),
    (1366, 768, 1.0), (1512, 982, 2.0),
]
_GL_PROFILES = [
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660/PCIe/SSE2, OpenGL 4.6.0)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 (polaris10, LLVM 15.0.7, DRM 3.49), "
     "OpenGL 4.6)"),
]
_HW = [8, 12, 16, 6, 10]
_MEM = [8, 8, 4, 16]


def _parse_proxy(url: str) -> dict | None:
    if not url:
        return None
    from urllib.parse import unquote, urlparse

    u = urlparse(url)
    if not u.hostname:
        return None
    scheme = "socks5" if u.scheme.startswith("socks5") else "http"
    cfg = {"server": f"{scheme}://{u.hostname}:{u.port or 1080}"}
    if u.username:
        cfg["username"] = unquote(u.username)
        cfg["password"] = unquote(u.password or "")
    return cfg


def _build_profile(idx: int, version: str) -> dict:
    """每页一份指纹档案（确定性：同一 idx 稳定，避免同页刷新后画像跳变）。

    UA 版本必须取真实 Chromium 版本：Chromium 会自动发 sec-ch-ua，
    与 UA 大版本不一致是比 headless 更硬的指纹矛盾。
    """
    major = version.split(".")[0] if version else "156"
    vw, vh, dpr = _VIEWPORTS[idx % len(_VIEWPORTS)]
    gl_vendor, gl_renderer = _GL_PROFILES[idx % len(_GL_PROFILES)]
    return {
        "ua": (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
        "vw": vw, "vh": vh, "dpr": dpr,
        "hw": _HW[idx % len(_HW)],
        "mem": _MEM[idx % len(_MEM)],
        "langs": ["en-US", "en"],
        "glVendor": gl_vendor,
        "glRenderer": gl_renderer,
    }


#: 页面加载后"熄火"：编辑器 UI 的 canvas 动画会被 SwiftShader 持续光栅化，
#: 实测空转吃 550% CPU（renderer 只有 3%）——我们只需要 fetch，不需要像素。
DEFUSE_JS = """
(() => {
  try {
    const st = document.createElement('style');
    st.textContent = '*,*::before,*::after{animation:none!important;' +
                     'transition:none!important}';
    document.documentElement.appendChild(st);
  } catch (e) {}
  try {
    document.querySelectorAll('canvas,video').forEach((el) => {
      el.style.display = 'none';
    });
  } catch (e) {}
  return document.querySelectorAll('canvas').length;
})()
"""


async def _new_page(browser, idx: int, version: str) -> dict:
    prof = _build_profile(idx, version)
    ctx = await browser.new_context(
        locale="en-US",
        timezone_id=TZ_ID,
        viewport={"width": prof["vw"], "height": prof["vh"]},
        device_scale_factor=prof["dpr"],
        user_agent=prof["ua"],
    )
    await ctx.add_init_script(
        INIT_JS.replace("%PROFILE%", json.dumps(prof)))
    pg = await ctx.new_page()
    await pg.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        canvases = await pg.evaluate(DEFUSE_JS)
        print(f"[pool] page#{idx} defused (canvases={canvases})", flush=True)
    except Exception:  # noqa: BLE001
        pass
    cdp = await ctx.new_cdp_session(pg)
    entry = {"idx": idx, "ctx": ctx, "page": pg, "cdp": cdp, "profile": prof,
             "inflight": 0, "submits": 0, "errors": 0, "err_streak": 0,
             "cooldown_until": 0.0, "recycled": 0,
             "eval_lock": asyncio.Lock()}
    await _page_sleep(entry)
    return entry


async def _page_sleep(entry: dict):
    """冻结页面：页面上的动画/canvas 合成会持续烧 SwiftShader（实测空转
    550% CPU）。生成在服务端跑，页面只需要在 fetch 时醒着。"""
    try:
        await entry["cdp"].send("Page.setWebLifecycleState", {"state": "frozen"})
    except Exception:  # noqa: BLE001 - 冻结失败不影响功能
        pass


async def _page_wake(entry: dict):
    try:
        await entry["cdp"].send("Page.setWebLifecycleState", {"state": "active"})
    except Exception:  # noqa: BLE001
        pass


async def _eval(entry: dict, script: str, arg=None, timeout: float = 90):
    """唤醒 → 求值 → 再冻结。用页面级锁串行化，避免并发请求互相冻结。"""
    async with entry["eval_lock"]:
        await _page_wake(entry)
        try:
            return await asyncio.wait_for(entry["page"].evaluate(script, arg),
                                          timeout=timeout)
        finally:
            await _page_sleep(entry)


async def ensure_pool():
    pool = _state["pool"]
    if pool and all(not e["page"].is_closed() for e in pool):
        return pool
    async with _state["lock"]:
        pool = _state["pool"]
        if pool and all(not e["page"].is_closed() for e in pool):
            return pool
        _state["pool"] = []
        pw = await async_playwright().start()
        proxy_cfg = _parse_proxy(PROXY_URL)
        print(f"[pool] proxy={'on' if proxy_cfg else 'off'} pages={PAGES} "
              f"max_inflight={MAX_INFLIGHT} tz={TZ_ID}", flush=True)
        browser = await pw.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--lang=en-US",
                # 反 headless 检测：上游按 UA/webdriver/视口识别自动化环境
                "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080",
                "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            ],
        )
        version = browser.version
        pages = [await _new_page(browser, i, version) for i in range(PAGES)]
        _state.update({"pw": pw, "browser": browser, "pool": pages,
                       "sem": asyncio.Semaphore(MAX_INFLIGHT)})
        print(f"[pool] {len(pages)} pages ready (chromium {version})", flush=True)
        return pages


def schedule_recycle(entry: dict) -> None:
    """同步摘牌 + 后台回收。摘牌必须发生在 create_task 之前：协程要等到
    下一个事件循环 tick 才真正开始执行，这个窗口足够另一笔请求选中该页
    （实测会 evaluate 到已关闭的 ctx → 500）。"""
    if entry.get("recycling"):
        return
    entry["recycling"] = True
    asyncio.create_task(recycle_entry(entry, premarked=True))


async def recycle_entry(entry: dict, *, premarked: bool = False):
    """回收单个 context：新连接=新出口 IP、干净 cookie、新指纹时序。"""
    if not premarked:
        if entry.get("recycling"):
            return
        entry["recycling"] = True      # 同步：以下所有 await 前就生效
    try:
        async with _state["recycle_lock"]:
            browser = _state.get("browser")
            if browser is None or not browser.is_connected():
                # 浏览器都掉了：整池重建
                _state["pool"] = []
                await ensure_pool()
                return
            idx = entry["idx"]
            version = browser.version
            try:
                await entry["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
            fresh = await _new_page(browser, idx, version)
            fresh["recycled"] = entry["recycled"] + 1
            pool = _state["pool"]
            for i, e in enumerate(pool):
                if e is entry:
                    pool[i] = fresh
                    break
            print(f"[pool] page#{idx} recycled (n={fresh['recycled']}, "
                  f"prev_errs={entry['errors']}, "
                  f"prev_submits={entry['submits']})", flush=True)
    finally:
        entry["recycling"] = False



def _reserve(entry: dict):
    entry["inflight"] += 1
    _state["total_inflight"] += 1


def _release(entry: dict):
    entry["inflight"] = max(0, entry["inflight"] - 1)
    _state["total_inflight"] = max(0, _state["total_inflight"] - 1)


async def acquire_entry(exclude: set[int] | None = None) -> dict:
    """挑一个健康且不处于 IP 冷却的页面；全忙则短暂排队（不失败）。"""
    per_page_cap = max(2, (MAX_INFLIGHT + PAGES - 1) // PAGES)
    exclude = exclude or set()
    deadline = time.time() + 25.0
    while True:
        pool = _state["pool"]
        now = time.time()
        cands = [e for e in pool
                 if id(e) not in exclude
                 and not e.get("recycling")
                 and not e["page"].is_closed()
                 and e["cooldown_until"] <= now
                 and e["inflight"] < per_page_cap]
        if cands:
            entry = min(cands, key=lambda e: (e["inflight"], e["err_streak"]))
            _reserve(entry)
            return entry
        if time.time() >= deadline:
            # 兜底：宁可排队超时前先接单，也不要整链 503
            pool = [e for e in pool if not e["page"].is_closed()]
            entry = min(pool, key=lambda e: e["inflight"])
            _reserve(entry)
            return entry
        await asyncio.sleep(0.3)


def _breaker_open() -> bool:
    return time.time() < _breaker["opened_until"]


def _breaker_on_success():
    if _breaker["fails"] or _breaker["opened_until"]:
        print("[breaker] CLOSED (upstream accepted)", flush=True)
    _breaker["fails"] = 0
    _breaker["opened_until"] = 0.0


def _breaker_on_block(reason: str):
    _breaker["fails"] += 1
    _breaker["last_reason"] = reason
    if _breaker["fails"] >= BREAKER_THRESHOLD:
        _breaker["opened_until"] = time.time() + BREAKER_COOLDOWN_S
        _breaker["trips"] += 1
        _breaker["fails"] = 0
        print(f"[breaker] OPEN {BREAKER_COOLDOWN_S:.0f}s reason={reason}",
              flush=True)


async def _maintain_pool():
    """后台维护：空闲页才回收。

    回收必须与请求生命周期解耦——在请求里关 ctx 无论怎么加锁都会有窗口
    打断并发请求（实测 TargetClosedError → 500）。这里只在 inflight==0
    且已标记时才动手，标记与判定在同一个 tick 内完成（无 await）。
    """
    while True:
        await asyncio.sleep(15)
        try:
            pool = _state["pool"]
            for e in list(pool):
                if e.get("recycling") or e.get("inflight", 0) > 0:
                    continue
                if e.get("want_recycle") or e["submits"] >= MAX_REQ_PER_CTX:
                    e["want_recycle"] = False
                    schedule_recycle(e)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintain] error: {type(exc).__name__}: {str(exc)[:100]}",
                  flush=True)


class GenReq(BaseModel):
    image_b64: str
    prompt: str
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    mode: str = "i2i"
    boxes: str | None = None
    references: str | None = None
    refs: list[str] | None = None


FP_JS = """
(() => {
  const gl = (() => {
    try {
      const c = document.createElement('canvas');
      const g = c.getContext('webgl');
      if (!g) return 'no-webgl';
      const d = g.getExtension('WEBGL_debug_renderer_info');
      return d ? g.getParameter(d.UNMASKED_VENDOR_WEBGL) + ' | '
                 + g.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'no-ext';
    } catch (e) { return 'err'; }
  })();
  return {
    ua: navigator.userAgent,
    webdriver: navigator.webdriver === undefined ? 'undefined'
                                                   : String(navigator.webdriver),
    wdIn: 'webdriver' in navigator,
    plugins: navigator.plugins.length,
    hw: navigator.hardwareConcurrency,
    mem: navigator.deviceMemory,
    langs: navigator.languages.join(','),
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    gl,
    chrome: !!window.chrome,
    outer: window.outerWidth + 'x' + window.outerHeight,
    inner: window.innerWidth + 'x' + window.innerHeight,
  };
})()
"""


@app.get("/fingerprint")
async def fingerprint():
    """自检：每页当前可探测到的身份特征（验证反检测是否真的生效）。"""
    await ensure_pool()
    out = []
    for e in _state["pool"]:
        try:
            fp = await _eval(e, FP_JS, None, timeout=15)
        except Exception as exc:  # noqa: BLE001
            fp = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
        out.append({"idx": e["idx"], **fp})
    return {"pages": out}


@app.get("/healthz")
async def healthz():
    pool = _state.get("pool") or []
    now = time.time()
    return {
        "ok": True,
        "pages": len(pool),
        "pages_ready": sum(1 for e in pool if not e["page"].is_closed()),
        "pages_cooling": sum(1 for e in pool if e["cooldown_until"] > now),
        "pages_recycling": sum(1 for e in pool if e.get("recycling")),
        "inflight": _state["total_inflight"],
        "max_inflight": MAX_INFLIGHT,
        "breaker": {"open": _breaker_open(), "trips": _breaker["trips"],
                    "fails": _breaker["fails"],
                    "last_reason": _breaker["last_reason"]},
        "per_page": [{"idx": e["idx"], "inflight": e["inflight"],
                      "submits": e["submits"], "errors": e["errors"],
                      "streak": e["err_streak"],
                      "recycled": e["recycled"],
                      "want_recycle": bool(e.get("want_recycle"))}
                     for e in pool],
    }


@app.post("/generate")
async def generate(req: GenReq):
    if _breaker_open():
        return JSONResponse(
            status_code=503,
            content={"error": f"upstream blocking browsers "
                              f"(breaker open, last={_breaker['last_reason']})"})
    await ensure_pool()
    sem = _state.get("sem") or asyncio.Semaphore(MAX_INFLIGHT)
    _state["sem"] = sem
    await sem.acquire()
    entry: dict | None = None
    switched: set[int] = set()
    t_start = time.time()
    try:
        entry = await acquire_entry()
        sub = None
        for attempt in range(3):
            if entry["err_streak"] >= 3:
                # 该页连续失败：冷却 + 后台换新会话，避免在同一坏出口反复撞墙
                bad = entry
                bad["cooldown_until"] = time.time() + IP_COOLDOWN_S * 3
                _release(entry)
                switched.add(id(bad))
                bad["want_recycle"] = True    # 后台任务空闲时换 IP
                entry = await acquire_entry(exclude=switched)
            t_page = time.time()
            sub = await _eval(entry, SUBMIT_JS, {
                "prompt": req.prompt, "aspectRatio": req.aspect_ratio,
                "resolution": req.resolution, "mode": req.mode,
                "imgB64": req.image_b64, "boxes": req.boxes,
                "references": req.references, "refs": req.refs or [],
            }, timeout=90)
            status = sub.get("status")
            if status not in (403, 429):
                break
            # 403/429 都是「这个出口 IP 不可用」：冷却 + 换页（独立 context
            # = 新连接 = 新出口 IP）重试，不该让客户为此失败。
            entry["cooldown_until"] = time.time() + IP_COOLDOWN_S
            if status == 403:
                entry["want_recycle"] = True   # 后台任务空闲时换 IP
                print(f"[gen] 403 denied on page#{entry['idx']}, "
                      f"rotating exit (try {attempt + 2})", flush=True)
            else:
                print(f"[gen] 429 on exit ip, cooling page#{entry['idx']} "
                      f"{IP_COOLDOWN_S:.0f}s (try {attempt + 2})", flush=True)
            if attempt < 2:
                _release(entry)
                switched.add(id(entry))
                entry = await acquire_entry(exclude=switched)

        t_submit = time.time()
        print(f"[gen] page#{entry['idx']} prep={t_page - t_start:.1f}s "
              f"submit={t_submit - t_page:.1f}s status={sub['status']} "
              f"body={(sub['body'] or '')[:120]!r}", flush=True)
        if sub["status"] != 200:
            entry["errors"] += 1
            entry["err_streak"] += 1
            body = sub["body"][:200]
            if sub["status"] == 403:
                # 三个出口都 403 才算一次"上游在拒绝浏览器"（单个坏 IP 不算）
                _breaker_on_block("403 access_denied (browser)")
                entry["cooldown_until"] = time.time() + IP_COOLDOWN_S
            return JSONResponse(status_code=502,
                               content={"error": f"submit {sub['status']}: {body}"})
        entry["err_streak"] = 0
        _breaker_on_success()
        try:
            token = json.loads(sub["body"]).get("pollToken")
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=502,
                               content={"error": f"bad submit body: {sub['body'][:200]}"})
        if not token:
            return JSONResponse(status_code=502, content={"error": "no pollToken"})
        entry["submits"] += 1

        deadline = time.time() + POLL_TIMEOUT_S
        image_token = None
        last = ""
        rounds = 0
        while time.time() < deadline:
            st = await _eval(entry, STATUS_JS, token, timeout=30)
            rounds += 1
            last = st["body"][:200]
            if st["status"] == 200:
                try:
                    doc = json.loads(st["body"])
                except Exception:  # noqa: BLE001
                    doc = {}
                s = doc.get("status")
                if s == "ready":
                    image_token = doc.get("imageToken")
                    break
                if s == "error":
                    return JSONResponse(status_code=502,
                                       content={"error": f"upstream: {last}"})
            elif st["status"] in (422, 451):
                return JSONResponse(status_code=451, content={"error": last})
            # 抖动：固定 1.000s 的等间隔轮询是最容易被识别的机器节奏
            await asyncio.sleep(POLL_INTERVAL_S * random.uniform(0.85, 1.5))
        if not image_token:
            print(f"[gen] poll timeout after {rounds} rounds "
                  f"({time.time() - t_submit:.0f}s) last={last[:120]!r}",
                  flush=True)
            return JSONResponse(status_code=504,
                               content={"error": f"poll timeout after "
                                                 f"{rounds} rounds: {last}"})

        t_ready = time.time()
        med = await _eval(entry, MEDIA_JS, image_token, timeout=120)
        print(f"[gen] poll={t_ready - t_submit:.1f}s rounds={rounds} "
              f"media={time.time() - t_ready:.1f}s "
              f"total={time.time() - t_start:.1f}s", flush=True)
        if med.get("status") != 200 or not med.get("b64"):
            return JSONResponse(status_code=502,
                               content={"error": f"media {med.get('status')}: "
                                                 f"{str(med.get('body'))[:150]}"})
        return {"image_b64": med["b64"]}
    except Exception as exc:  # noqa: BLE001
        import traceback
        page_tag = entry["idx"] if entry is not None else -1
        if entry is not None:
            entry["errors"] += 1
            entry["err_streak"] += 1
        print(f"[gen] EXC page#{page_tag} {type(exc).__name__}: "
              f"{str(exc)[:200]}", flush=True)
        print(traceback.format_exc()[-1200:], flush=True)
        return JSONResponse(status_code=500,
                           content={"error": f"{type(exc).__name__}: {str(exc)[:150]}"})
    finally:
        if entry is not None:
            _release(entry)
        sem.release()


async def _prewarm_generate():
    """启动预热：页面加载后跑一发 dummy 生成，把 TLS/上游会话/页面 JS 全部
    热起来（实测冷启动首发生成 >300s，预热后首发即稳态 ~50s）。"""
    try:
        await ensure_pool()
        import io as _io

        from PIL import Image as _Img

        buf = _io.BytesIO()
        _Img.new("RGB", (256, 256), (128, 128, 128)).save(buf, "PNG")
        await generate(GenReq(image_b64=base64.b64encode(buf.getvalue()).decode(),
                              prompt="a plain gray square",
                              aspect_ratio="1:1", resolution="1k", mode="i2i"))
        print("[prewarm] done", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[prewarm] failed: {type(exc).__name__}: {str(exc)[:100]}",
              flush=True)


@app.on_event("startup")
async def _warm():
    asyncio.create_task(_prewarm_generate())
    asyncio.create_task(_maintain_pool())
