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
import re
import sys
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel

BASE = "https://flux-tools.bfl.ai"
PAGE_URL = f"{BASE}/precise-editing"
POLL_INTERVAL_S = float(os.environ.get("BFL2OPENAI_EGRESS_POLL_S", "1.0"))
#: 轮询上限必须短于调用方超时（bfl2openai 侧 180s）：否则客户先断开，
#: 我们连"为什么失败"都送不回去（实测一笔 pending 280s 的请求就这么消失的）。
POLL_TIMEOUT_S = float(os.environ.get("BFL2OPENAI_EGRESS_POLL_TIMEOUT", "165"))
#: 取图（页面内 fetch media）的超时。**注意这不是轮询**：轮询是
#: POLL_TIMEOUT_S（等 status 变 ready），这里是 ready 之后把图片字节下回来。
#: 上游 CDN 到客户端这段走页面 fetch，带宽差时 1k PNG 实测 78–111s，
#: 原来硬编码 120s 贴着上限、偶发 500。放到 300s（2026-11-16）。
#: 约束：调用方总超时必须 > POLL_TIMEOUT_S + MEDIA_TIMEOUT_S，否则先断。
MEDIA_TIMEOUT_S = float(os.environ.get("BFL2OPENAI_EGRESS_MEDIA_TIMEOUT", "300"))

#: 生成耗时基线（秒），用于轮询节奏「先疏后密」。
#: 来源：2026-11-16 本地实测 —— LARGE i2i 1.5k 的 poll≈27.3s（13 轮），
#: KLEIN 768sq 的 poll≈6.4s（4 轮）。生成在 ETA 前 55% 内几乎必然是 pending，
#: 密集轮询纯属浪费：既抬高该 IP 的 CF 请求速率，又反复唤醒冻结页面。
POLL_ETA_S = {"flux-3-image-klein": 8.0, "flux-3-image": 28.0}
DEFAULT_MODEL = "flux-3-image"
DEFAULT_ETA_S = 28.0
#: 页面池大小。默认 8。每页独立 context ⇒ 独立 socket 池 ⇒ 通常各自一个出口 IP；
#: 但 IP 是「每条 TCP 连接」分配的（poolclient 逐连接轮换，见 socksbridge.py），
#: 所以连接重建/context 回收就会换 IP，不是固定绑定。与生产 compose 的显式
#: 配置一致——不带环境变量直接跑也是够用的池子，别退回 4 页把并发压死。
PAGES = int(os.environ.get("BFL2OPENAI_EGRESS_PAGES", "8"))
#: 池代理（socks5://user:pass@host:port）：Chromium 直连=服务器 IP，已被上游
#: 按 IP 限流（429）；真浏览器指纹 + 换出口 IP 才能过。
PROXY_URL = os.environ.get("BFL2OPENAI_EGRESS_PROXY", "")

# ── 2090 pin 模式：账号=槽位 → 固定出口 IP ───────────────────────────────
# 为什么要它：2088 每连接换 IP，系统里没有任何地方知道「某个 IP 已经用掉
# 28 次」，配额无法落账。2090 用 `poolclient-{n}` 把出口钉死，计数键才能
# 定成 IP。Chromium 不支持带认证的 socks5，所以每个槽位起一个本地桥
# （socksbridge 已存在），页面按槽位的桥端口建 context。
POOL_HOST = os.environ.get("BFL2OPENAI_POOL_HOST", "")
POOL_PORT = int(os.environ.get("BFL2OPENAI_POOL_PORT", "2090"))
POOL_PASS = os.environ.get("BFL2OPENAI_POOL_PASS", "")
POOL_USER_PREFIX = os.environ.get("BFL2OPENAI_POOL_USER_PREFIX", "poolclient")
#: 槽位端口基数：slot n → 127.0.0.1:(base+n)。避开 1080（2088 那把桥）。
POOL_BRIDGE_BASE_PORT = int(os.environ.get("BFL2OPENAI_POOL_BRIDGE_BASE", "11000"))
PIN_ENABLED = bool(POOL_HOST and POOL_PASS)
#: 预绑定槽位：启动完成前先把这些槽位绑到页面上，第一个真实请求就不用再
#: 重建 context（一次重建 = 页面加载 + 静默期 ≈ 2s）。必须与 app 侧同名
#: 变量一致 —— 只有两边都认同一批槽位，绑定才算数。
def _prebind_slots() -> list[int]:
    """预绑定槽位表。

    两种写法：
    - `BFL2OPENAI_POOL_PREBIND=1,2,3` 显式列出
    - `PREBIND_FROM/TO/STRIDE/OFFSET` 按步长取片：每台侧车只绑自己那一份
      （slot % 实例数 == 本实例序号），app 侧按同一规则做路由亲和，
      两边才对得上。
    """
    raw = os.environ.get("BFL2OPENAI_POOL_PREBIND", "")
    explicit = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if explicit:
        return explicit
    stride = int(os.environ.get("BFL2OPENAI_POOL_PREBIND_STRIDE", "0") or 0)
    if stride <= 0:
        return []
    off = int(os.environ.get("BFL2OPENAI_POOL_PREBIND_OFFSET", "0") or 0)
    lo = int(os.environ.get("BFL2OPENAI_POOL_PREBIND_FROM", "1") or 1)
    hi = int(os.environ.get("BFL2OPENAI_POOL_PREBIND_TO", "0") or 0)
    return [n for n in range(lo, hi + 1) if n % stride == off % stride]


PREBIND_SLOTS = _prebind_slots()

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
#: 单页连续失败的冷却上限。原实现硬编码 3×IP_COOLDOWN_S=60s，8 页同时进
#: 冷却时 acquire_entry 只能干等到 25s 兜底——池子明明有网络容量，却被自己
#: 的冷却墙堵死（2026-11-16 实测三台 502 率已到 51~57%）。默认 60s 与旧值
#: 等价，现网行为不变，需要时再下调。
IP_COOLDOWN_MAX_S = float(
    os.environ.get("BFL2OPENAI_EGRESS_IP_COOLDOWN_MAX", "60"))
#: 全局提交速率（次/秒，0=不限）。CF 侧 429 按 IP+时间窗计，而当前每台常驻
#: 43~57 条连接、三台约 150 条并发摊在 13 个 /24 上，单 IP 速率是限流的直接
#: 推手。默认 0 = 关闭，不改变现网行为。注意：2026-11-16 实测饱和模型下，
#: 降速只缩短排队长度、不提升单次成功率（单次仅 ~22%），优先级低于扩池。
GLOBAL_RPS = float(os.environ.get("BFL2OPENAI_EGRESS_RPS", "0"))
#: 出口 IP 采样间隔（秒，0=关闭）。用途：① 把 429 归因到网段；② 捕捉代理
#: 失效回落直连（2026-11-16 实测 299 次抽样中 1.3% 的出口 = 本机公网 IP）。
EXIT_IP_SAMPLE_S = float(os.environ.get("BFL2OPENAI_EGRESS_IP_SAMPLE", "60"))
#: cgroup 内存水位。Chromium 的分配器不会把回收的 context 内存还给内核，
#: 长时间跑必然单调上涨，到 mem_limit 就被内核 OOM-kill（杀哪组进程不可控）。
#: 与其等 OOM，不如到水位自己重启整池：soft 只在完全空闲时动手，hard 立即动手。
MEM_SOFT_RATIO = float(os.environ.get("BFL2OPENAI_EGRESS_MEM_SOFT", "0.80"))
MEM_HARD_RATIO = float(os.environ.get("BFL2OPENAI_EGRESS_MEM_HARD", "0.92"))

#: 页面重建失败后的退避：30s → 60s → 120s → 240s → 300s 封顶。
#: 2026-11-18 生产实测：上游限流时 12 个槽位每 15 秒各重试一次整页重载，
#: 30 分钟打了 129 次，把 8 核的 chromium 打满（load 22.9、队列 39）。
#: 退避只作用于「重建失败」这条路径，正常换 IP 的回收节奏不受影响。
RECYCLE_BACKOFF_BASE_S = float(
    os.environ.get("BFL2OPENAI_EGRESS_REBUILD_BACKOFF", "30"))
RECYCLE_BACKOFF_MAX_S = float(
    os.environ.get("BFL2OPENAI_EGRESS_REBUILD_BACKOFF_MAX", "300"))

app = FastAPI()
_state: dict = {
    "pool": [], "lock": asyncio.Lock(), "recycle_lock": asyncio.Lock(),
    "browser": None, "pw": None, "sem": None, "total_inflight": 0,
    #: idx -> (连续失败次数, 下次允许重建的 epoch)。见 _backoff_fail()。
    "refill_backoff": {},
    # 限速与出口 IP 观测（2026-11-16 增加，不影响原有字段语义）
    "rps_lock": asyncio.Lock(), "rps_next": 0.0,
    "exit_ip": None, "exit_ip_local": None,
    "exit_ip_leak": 0, "exit_ip_samples": 0, "exit_ip_seen": [],
}

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_breaker = {"fails": 0, "opened_until": 0.0, "trips": 0, "last_reason": ""}

SUBMIT_JS = """
async ({prompt, aspectRatio, resolution, mode, imgB64, boxes, references, refs, refMode, model}) => {
  // 契约（2026-10-09 实测）：空 image 部件 ≠ 无 image —— 上游会回
  // 400 "A composition must not include a canvas image."。无图时必须整个省略。
  const hasImage = !!(imgB64 && imgB64.length);
  // auto 表示「从画布推导」；没有画布时上游要求显式比例（2026-10-09）。
  let ar = aspectRatio;
  if (!hasImage && ar === "auto") ar = "1:1";

  const fd = new FormData();
  fd.append("mode", mode);
  // 官网下拉里有两个模型（FLUX 3 LARGE / KLEIN，2026-11-16 实测）。
  // 官网请求体里带 model=flux-3-image；此前我们靠上游默认值。留空即不发，
  // 保持旧行为，仅在显式指定时才追加。
  if (model) fd.append("model", model);
  fd.append("prompt", prompt);
  fd.append("aspectRatio", ar);
  fd.append("resolution", resolution);
  // boxes/references 两个 JSON 字段必须始终存在，缺失会被当成 composition
  // （t2i）→ 带 image 即 400（2026-10-09/14 实测）。
  fd.append("boxes", boxes || "[]");
  // 两条上游分支（2026-11-16 抓包发现，此前只用了第一条）：
  //   references(复数) 文件部件 -> composition 契约，canvas 必须缺席，mode=t2i
  //   reference(单数) 文件部件  -> editing 契约，references 退化成 id 数组，
  //                                mode=i2i 且带 image 画布。官网 precise-editing
  //                                页走的是后者（canvas 受保护，保真度更高）。
  const single = refMode === "single";
  const refIds = (refs || []).map((_, i) => (i === 0 ? "ref" : `ref-${i + 1}`));
  fd.append("references", single ? JSON.stringify(refIds) : (references || "[]"));
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
      fd.append(single ? "reference" : "references",
                new Blob([ra], {type: "image/png"}),
                single ? refIds[i] : `ref${i}.png`);
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


def _build_profile(idx: int, version: str, gen: int = 0) -> dict:
    """每页一份指纹档案。

    gen = 该 page 的会话代次（context 每重建一次 +1）。同一 gen 内同一 idx
    恒定（避免同页刷新后画像跳变），跨 gen 换一组组合。

    2026-11-16 实测纠正：旧实现直接 idx % len() 取模，PAGES=8 时每个维度都
    落在低基数上，而 recycle_entry 又复用同一 idx 重建 → 整个容器生命周期
    只有 8 套固定指纹，等于 8 张面孔跑一整天。

    UA 版本必须取真实 Chromium 版本：Chromium 会自动发 sec-ch-ua，
    与 UA 大版本不一致是比 headless 更硬的指纹矛盾。
    """
    major = version.split(".")[0] if version else "156"
    # 各维度用互质步长错开，让 (idx, gen) 在 8×3×5×4=480 个组合上铺开；
    # 同一 gen 内 idx 0..7 仍保证每个维度全覆盖。
    vw, vh, dpr = _VIEWPORTS[(idx + 2 * gen) % len(_VIEWPORTS)]
    gl_vendor, gl_renderer = _GL_PROFILES[
        (2 * idx + gen) % len(_GL_PROFILES)]
    hw = _HW[(idx + 3 * gen) % len(_HW)]
    mem = _MEM[(idx + gen) % len(_MEM)]
    return {
        "ua": (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
        "vw": vw, "vh": vh, "dpr": dpr,
        "hw": hw,
        "mem": mem,
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


# ── 槽位本地桥 ──────────────────────────────────────────────────────────
# 一个槽位 = 一条上游认证 socks5 = 一个固定出口 IP。Chromium 不会认证，
# 所以每条槽位起一个本地无认证桥，页面按端口选桥。

_BRIDGE: dict[int, asyncio.subprocess.Process] = {}
_BRIDGE_LAST: dict[int, float] = {}
_BRIDGE_LOCK = asyncio.Lock()
#: 常驻桥上限。槽位可轮转到数百个，每换一个就常驻一个进程会把容器吃爆
#: （588 个槽位 × 3 实例 ≈ 1,700 个进程）。页面并发上限只有 MAX_INFLIGHT，
#: 32 条桥足够覆盖在飞的页面，其余按 LRU 淘汰。
MAX_BRIDGES = int(os.environ.get("BFL2OPENAI_POOL_MAX_BRIDGES", "32"))


def _bridge_port(slot: int) -> int:
    return POOL_BRIDGE_BASE_PORT + slot


def _evict_bridges_locked() -> None:
    """淘汰最久未用的空闲桥。正在被页面占用的槽位不淘汰。"""
    if len(_BRIDGE) <= MAX_BRIDGES:
        return
    busy = {e.get("slot") for e in (_state.get("pool") or [])}
    idle = [(s, p) for s, p in _BRIDGE.items()
            if s not in busy and p.returncode is None]
    idle.sort(key=lambda kv: _BRIDGE_LAST.get(kv[0], 0.0))
    for s, p in idle[:len(_BRIDGE) - MAX_BRIDGES]:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass
        _BRIDGE.pop(s, None)
        _BRIDGE_LAST.pop(s, None)


async def _ensure_bridge(slot: int) -> int:
    """保证该槽位的本地桥在跑，返回端口。

    桥进程常驻：同一个槽位重建桥不会换出口 IP，只会丢连接，所以按需拉起、
    不主动回收。
    """
    if not PIN_ENABLED:
        raise RuntimeError("2090 pin 模式未配置（BFL2OPENAI_POOL_HOST/_PASS）")
    port = _bridge_port(slot)
    _BRIDGE_LAST[slot] = time.time()
    async with _BRIDGE_LOCK:
        proc = _BRIDGE.get(slot)
        if proc is not None and proc.returncode is None:
            return port
        argv = [sys.executable, "/app/socksbridge.py", str(port),
                POOL_HOST, str(POOL_PORT)]
        # 凭据走环境变量：argv 会出现在容器内 ps 输出里，等于明文张贴密码。
        env = dict(os.environ, SB_USER=f"{POOL_USER_PREFIX}-{slot:03d}",
                   SB_PASS=POOL_PASS)
        _BRIDGE[slot] = await asyncio.create_subprocess_exec(
            *argv, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        _evict_bridges_locked()
    for _ in range(50):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return port
        except Exception:  # noqa: BLE001 - 桥还没 listen，重试
            await asyncio.sleep(0.1)
    raise RuntimeError(f"slot {slot} 的本地桥未就绪（port {port}）")


async def _rebind(entry: dict, slot: int) -> None:
    """把某个页面原地切到指定槽位：换 context 就是换出口 IP。

    原地改 entry 而不新建 dict —— 池子存的是同一个对象引用，换对象会让
    其它协程手里的 entry 失配。
    """
    browser = _state.get("browser")
    if browser is None or not browser.is_connected():
        raise RuntimeError("browser not connected")
    gen = int(entry.get("gen", 0)) + 1
    prof = _build_profile(entry["idx"], entry.get("version", ""), gen)
    port = await _ensure_bridge(slot)
    ctx = await browser.new_context(
        locale="en-US", timezone_id=TZ_ID,
        viewport={"width": prof["vw"], "height": prof["vh"]},
        device_scale_factor=prof["dpr"], user_agent=prof["ua"],
        proxy={"server": f"socks5://127.0.0.1:{port}"},
    )
    try:
        await ctx.add_init_script(INIT_JS.replace("%PROFILE%", json.dumps(prof)))
        pg = await ctx.new_page()
        # wait_until="load" 而不是 domcontentloaded：Vercel 的机器人挑战是页面
        # 加载后由 JS 完成的，脚本没跑完就提交必然是 403 Access denied
        # （实测：rebind 后立即 submit 全被拒，同 IP 的老页面却能成功）。
        await pg.goto(PAGE_URL, wait_until="load", timeout=60000)
        try:
            await pg.evaluate(DEFUSE_JS)
        except Exception:  # noqa: BLE001
            pass
        # 再留一段静默期，模拟"人打开页面看一眼再点生成"的自然间隔
        settle = float(os.environ.get("BFL2OPENAI_POOL_REBIND_SETTLE", "3.0"))
        if settle > 0:
            for _ in range(int(settle / 0.25)):
                try:
                    if await pg.evaluate("document.readyState") == "complete":
                        break
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.25)
            await asyncio.sleep(settle)
        cdp = await ctx.new_cdp_session(pg)
    except BaseException:
        # 新 context 建失败必须就地关掉，否则每失败一次泄漏一份渲染进程
        try:
            await ctx.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    old = entry.get("ctx")
    entry.update({"ctx": ctx, "page": pg, "cdp": cdp, "profile": prof,
                  "gen": gen, "slot": slot, "err_streak": 0,
                  "cooldown_until": 0.0})
    if old is not None:
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass


async def _new_page(browser, idx: int, version: str, gen: int = 0,
                    slot: int | None = None) -> dict:
    prof = _build_profile(idx, version, gen)
    ctx_kwargs = dict(
        locale="en-US",
        timezone_id=TZ_ID,
        viewport={"width": prof["vw"], "height": prof["vh"]},
        device_scale_factor=prof["dpr"],
        user_agent=prof["ua"],
    )
    if slot is not None:
        # context 级 proxy 会覆盖 browser 级：这是把「一个页面钉到一个出口」
        # 落地的唯一方式，Chromium 的认证由本地 socksbridge 兜掉。
        port = await _ensure_bridge(slot)
        ctx_kwargs["proxy"] = {"server": f"socks5://127.0.0.1:{port}"}
    ctx = await browser.new_context(**ctx_kwargs)
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
    entry = {"idx": idx, "gen": gen, "ctx": ctx, "page": pg, "cdp": cdp,
             "profile": prof, "version": version, "slot": slot,
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


async def _teardown_browser() -> None:
    """彻底关掉当前 Chromium 与 Playwright 驱动，把内存还给内核。

    🔴 重建池子**必须**先走这里。旧实现只把 `_state` 里的引用换掉、不关实例：
    每轮「页面全崩 → ensure_pool」都会再拉起一整个 Chromium（主进程 + zygote
    + utility + gpu 一组 6~8 个进程），旧的那组留在 cgroup 里直到 OOM-killer
    出手。2026-11-13 03:00 的 egress-2 里同时挂着 **24 个 browser 主进程**、
    cgroup 内存 2.96/3 GiB、一小时内 24 次 memcg OOM（kill 目标全是
    chromium），就是这条路径。泄漏与崩溃互为因果：OOM 杀渲染进程 → 页面
    is_closed() → 重建池子 → 再泄漏一份，滚到必定 OOM。
    """
    browser = _state.get("browser")
    pw = _state.get("pw")
    # 先摘引用再关：关闭过程有 await，期间不能被别的协程拿到半死实例
    _state.update({"pool": [], "browser": None, "pw": None, "sem": None})
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - 已崩的实例关不掉是正常的
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:  # noqa: BLE001
            pass


async def ensure_pool():
    """保证「浏览器活着 + 池子非空」。

    判定从「所有页都没关」放宽成「浏览器连着且池子非空」：单页崩溃由
    `_retire` / `_fill_pool_gap` 原位处理，不该为此重建整个浏览器——每次
    重建都是一份常驻内存（见 _teardown_browser）。
    """
    pool = _state["pool"]
    browser = _state.get("browser")
    if pool and browser is not None and browser.is_connected():
        return pool
    async with _state["lock"]:
        pool = _state["pool"]
        browser = _state.get("browser")
        if pool and browser is not None and browser.is_connected():
            return pool
        await _teardown_browser()
        pw = await async_playwright().start()
        proxy_cfg = _parse_proxy(PROXY_URL)
        print(f"[pool] proxy={'on' if proxy_cfg else 'off'} pages={PAGES} "
              f"max_inflight={MAX_INFLIGHT} tz={TZ_ID}", flush=True)
        browser = None
        try:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get(
                    "BFL2OPENAI_EGRESS_CHROMIUM", "/usr/bin/chromium"),
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
        except BaseException:
            # 半成品（浏览器起来了但页面没建全）也要收干净，否则又是一份泄漏。
            # 此刻还没登记进 _state，_teardown_browser() 看不到它，必须就地关。
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise
        _state.update({"pw": pw, "browser": browser, "pool": pages,
                       "sem": asyncio.Semaphore(MAX_INFLIGHT)})
        print(f"[pool] {len(pages)} pages ready (chromium {version})", flush=True)
        return pages


def _backoff_wait(idx: int) -> float:
    """该槽位还要等多少秒才能再尝试重建；0 表示可立即重建。"""
    _fails, next_at = _state["refill_backoff"].get(idx, (0, 0.0))
    return max(0.0, next_at - time.time())


def _backoff_fails(idx: int) -> int:
    return _state["refill_backoff"].get(idx, (0, 0.0))[0]


def _backoff_fail(idx: int) -> float:
    """记一次重建失败，返回本次退避秒数（30s 起，翻倍，300s 封顶）。

    🔴 2026-11-18 事故根因之一：_fill_pool_gap 每 15 秒补一页，补不上就把
    坏页摘出池子、下个 tick 再补 —— 12 个槽位在 30 分钟内触发 129 次整页
    重载（每次 = 一个 Chromium 页面完整执行 SPA），8 核被吃干净。上游限流
    持续时这不是恢复，是自激。退避把重试压到分钟级：池子仍然自愈，只是
    不再用 CPU 换重试次数。
    """
    fails = _backoff_fails(idx) + 1
    delay = min(RECYCLE_BACKOFF_MAX_S,
                RECYCLE_BACKOFF_BASE_S * (2 ** (fails - 1)))
    _state["refill_backoff"][idx] = (fails, time.time() + delay)
    return delay


def _backoff_ok(idx: int) -> None:
    _state["refill_backoff"].pop(idx, None)


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
            wait = _backoff_wait(idx)
            if wait > 0:
                # 退避期内不重建：把这个废页真正摘出池子，到期后由
                # _fill_pool_gap 补回。若只 return 不摘，它会带着
                # want_recycle 每 15 秒被重排一次 —— 正是本次风暴的形态。
                try:
                    await entry["ctx"].close()
                except Exception:  # noqa: BLE001
                    pass
                _state["pool"] = [e for e in (_state.get("pool") or [])
                                  if e is not entry]
                print(f"[pool] page#{idx} recycle deferred "
                      f"({wait:.0f}s left, fails={_backoff_fails(idx)})",
                      flush=True)
                return
            try:
                await entry["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
            try:
                # gen 递增 ⇒ 回收后换一组指纹，不再 8 张面孔跑一整天
                fresh = await _new_page(browser, idx, version,
                                        entry.get("gen", 0) + 1)
            except Exception as exc:  # noqa: BLE001
                # 回收失败（Page.goto ERR_ABORTED / Target crashed）：把坏页
                # 摘出池子，否则闭页会一直占位，等全部 closed 后
                # acquire_entry 兜底拿到空列表（2026-11-05 生产实测）。
                delay = _backoff_fail(idx)
                print(f"[pool] page#{idx} recycle failed: "
                      f"{type(exc).__name__}: {exc} "
                      f"(delay {delay:.0f}s, fails={_backoff_fails(idx)})",
                      flush=True)
                _state["pool"] = [e for e in (_state.get("pool") or [])
                                  if e is not entry and not e["page"].is_closed()]
                return
            _backoff_ok(idx)
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



#: Playwright 里「这个页面已经废了」的错误特征：渲染进程崩溃 / context 被关。
#: 这类错误换一页重试就有救，不能当业务失败 —— 2026-11-05 实测
#: `Page.evaluate: Target crashed` 直接冒成 500，把一次正常生成白丢了。
_PAGE_DEAD_MARKERS = (
    "target crashed", "target closed", "page crashed", "page closed",
    "has been closed", "session closed", "browser has been closed",
)


def _page_dead(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(m in text for m in _PAGE_DEAD_MARKERS)


def _page_ok(entry: dict) -> bool:
    """页面当前可复用？

    🔴 只用 is_closed()，**不要**调 is_crashed()：v0.2.61 实测该调用让
    `pages_ready` 恒为 0（8 个正常页全被判死），池子直接不可用、/generate
    全 503，只能回滚。渲染进程崩溃交给异常路径：_page_dead() 判定 +
    _retire() 摘牌重建 —— 最多多失败一次，代价远小于误杀整池。
    """
    pg = entry["page"]
    try:
        return not pg.is_closed()
    except Exception:  # noqa: BLE001 - CDP 断了
        return False


def _retire(entry: dict, reason: str) -> None:
    """摘掉一个废页：立刻长期冷却 + 后台重建，池子不缩水。"""
    entry["cooldown_until"] = time.time() + 3600
    entry["want_recycle"] = True
    print(f"[pool] page#{entry['idx']} retired ({reason})", flush=True)
    schedule_recycle(entry)


def _reserve(entry: dict):
    entry["inflight"] += 1
    _state["total_inflight"] += 1


def _release(entry: dict):
    entry["inflight"] = max(0, entry["inflight"] - 1)
    _state["total_inflight"] = max(0, _state["total_inflight"] - 1)


async def _curl_ip(*, via_proxy: bool) -> str | None:
    """取一次出口 IP。via_proxy=False 用于拿本机公网 IP 作对照基线。"""
    args = ["curl", "-s", "--max-time", "6"]
    if via_proxy:
        args += ["--noproxy", "", "-x", "socks5h://127.0.0.1:1080"]
    args.append("https://api.ipify.org")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:  # noqa: BLE001 - 采样失败绝不冒到业务路径
        return None
    ip = out.decode(errors="replace").strip()
    return ip if _IP_RE.match(ip) else None


async def _detect_local_ip() -> None:
    """启动时用直连取本机公网 IP，作为「代理失效」的对照基线。"""
    ip = await _curl_ip(via_proxy=False)
    if ip:
        _state["exit_ip_local"] = ip
        print(f"[pool] local egress ip={ip}", flush=True)


async def _sample_exit_ip() -> None:
    """经代理采一次出口 IP：归因 429 到网段 + 捕捉代理失效回落直连。

    2026-11-16 实测依据：299 次抽样得 122 个不同 IP（Chao1 估计池规模约
    158），但只有 13 个 /24，其中 38.6.213.0/24 单段塞了 79 个 IP、承担
    61.5% 流量 —— IP 在换、网段没换，这是「只加 IP 无效」的直接解释。
    同批抽样中 1.3% 的出口等于本机公网 IP，即代理失效后回落直连。
    """
    ip = await _curl_ip(via_proxy=True)
    if not ip:
        return
    _state["exit_ip"] = ip
    _state["exit_ip_samples"] += 1
    seen = _state["exit_ip_seen"]
    seen.append(ip)
    del seen[:-64]
    if ip == _state.get("exit_ip_local"):
        _state["exit_ip_leak"] += 1
        print(f"[pool] EXIT IP LEAK: 出口={ip} 等于本机公网 IP，"
              f"该请求绕过了代理", flush=True)


async def _rate_wait() -> None:
    """全局限速令牌桶。GLOBAL_RPS<=0 时零开销直接返回。"""
    if GLOBAL_RPS <= 0:
        return
    interval = 1.0 / GLOBAL_RPS
    async with _state["rps_lock"]:
        now = time.time()
        slot = max(now, _state["rps_next"])
        _state["rps_next"] = slot + interval
    delay = slot - time.time()
    if delay > 0:
        await asyncio.sleep(delay)


async def acquire_entry(exclude: set[int] | None = None,
                        prefer_slot: int | None = None) -> dict:
    """挑一个健康且不处于 IP 冷却的页面；全忙则短暂排队（不失败）。

    `prefer_slot`：优先返回**已经绑定在该槽位**的空闲页。窗口热起来的关键——
    换 context 就要重过 Vercel 挑战（实测每次 5–10s），能复用就别重建。
    """
    per_page_cap = max(2, (MAX_INFLIGHT + PAGES - 1) // PAGES)
    exclude = exclude or set()
    deadline = time.time() + 25.0
    while True:
        pool = _state["pool"]
        now = time.time()
        cands = [e for e in pool
                 if id(e) not in exclude
                 and not e.get("recycling")
                 and _page_ok(e)
                 and e["cooldown_until"] <= now
                 and e["inflight"] < per_page_cap]
        if cands:
            if prefer_slot is not None:
                same = [e for e in cands if e.get("slot") == prefer_slot]
                if same:
                    cands = same
            entry = min(cands, key=lambda e: (e["inflight"], e["err_streak"]))
            _reserve(entry)
            return entry
        if time.time() >= deadline:
            # 兜底：宁可排队超时前先接单，也不要整链 503
            pool = [e for e in pool if _page_ok(e)]
            if not pool:
                # 一个可用页都没有（页面全 crash / 回收失败）。先原位补一页
                # 再决定要不要 503：浏览器还活着时补位只要几秒，而 503 会让
                # app 立刻切实例、把「单页崩溃」放大成「整机不可用」。
                try:
                    await _fill_pool_gap()
                except Exception:  # noqa: BLE001 - 补不上就走 503
                    pass
                pool = [e for e in _state["pool"] if _page_ok(e)]
            if not pool:
                # 仍然没有可用页：快速 503，让 app 侧换下一个 sidecar 实例。
                # 这里以前直接 min([])，会崩成 500 且带上
                # "min() iterable argument is empty"（2026-11-05 生产实测）。
                raise HTTPException(status_code=503, detail="egress pool empty")
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


async def _fill_pool_gap() -> None:
    """池子缩水时补回缺的槽位（不重建健康页，一次补一页）。

    背景：回收失败（Page.goto ERR_ABORTED）会把坏页摘出池子 ⇒ 池子只会缩、
    不会长；缩到 0 就只剩「外部看门狗重启容器」一条路（2026-11-06 凌晨
    1.5 小时内重启 8 次，期间服务不可用）。让池子自己长回来：上游恢复后
    最多 PAGES×15s 补满，不依赖容器重启。
    """
    if len(_state["pool"]) >= PAGES:
        return
    browser = _state.get("browser")
    if browser is None or not browser.is_connected():
        return          # 浏览器都没了：交给 ensure_pool 全量重建
    async with _state["lock"]:
        pool = _state["pool"]
        have = {e["idx"] for e in pool if _page_ok(e)}
        # 退避中的槽位不算缺口：不过滤的话 _fill_pool_gap 每个 tick 都会
        # 挑中同一个失败槽位重试（15s 一次），退避形同虚设。
        missing = [i for i in range(PAGES)
                   if i not in have and _backoff_wait(i) <= 0]
        if not missing:
            return
        idx = missing[0]
        try:
            fresh = await _new_page(browser, idx, browser.version)
        except Exception as exc:  # noqa: BLE001
            delay = _backoff_fail(idx)
            print(f"[pool] refill#{idx} failed: "
                  f"{type(exc).__name__}: {str(exc)[:120]} "
                  f"(delay {delay:.0f}s, fails={_backoff_fails(idx)})",
                  flush=True)
            return
        _backoff_ok(idx)
        _state["pool"] = [e for e in pool if e["idx"] != idx] + [fresh]
        print(f"[pool] page#{idx} refilled "
              f"({len(_state['pool'])}/{PAGES})", flush=True)


def _cgroup_mem() -> tuple[int, int] | None:
    """返回 (已用字节, 上限字节)。cgroup v2 优先，回落 v1；取不到返回 None。

    容器 mem_limit 就是 cgroup 上限，所以这里看到的就是 docker stats 的口径，
    比轮询 `docker stats` 更省事、也比读 /proc 更准（含 page cache）。
    """
    for used_p, limit_p in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            used = int(open(used_p).read().strip())
            raw = open(limit_p).read().strip()
            if raw == "max":
                return None
            limit = int(raw)
        except Exception:  # noqa: BLE001 - 非容器环境直接跳过
            continue
        if limit <= 0 or limit > (1 << 60):
            return None
        return used, limit
    return None


async def _restart_browser(reason: str) -> None:
    """整池重启：关浏览器 → 立刻重建。

    这是「优雅版 OOM」：水位到顶时主动重启，代价是此刻 in-flight 的请求
    失败（本来也会被内核 OOM-killer 杀掉，且杀哪一组进程不可控、还会连带
    打崩同一 cgroup 里其他页面）。
    """
    async with _state["lock"]:
        if _state.get("browser") is None and not _state["pool"]:
            return                      # 别人刚重启过
        print(f"[pool] RESTART browser: {reason}", flush=True)
        await _teardown_browser()
    await ensure_pool()


async def _maintain_pool():
    """后台维护：空闲页才回收 + 内存水位自保。

    回收必须与请求生命周期解耦——在请求里关 ctx 无论怎么加锁都会有窗口
    打断并发请求（实测 TargetClosedError → 500）。这里只在 inflight==0
    且已标记时才动手，标记与判定在同一个 tick 内完成（无 await）。
    """
    mem_restarts: list[float] = []
    last_ip_sample = 0.0
    while True:
        await asyncio.sleep(15)
        # ---- 出口 IP 采样（低频，走容器内 curl，不占用浏览器 page）----
        if EXIT_IP_SAMPLE_S > 0 and \
                time.time() - last_ip_sample >= EXIT_IP_SAMPLE_S:
            last_ip_sample = time.time()
            try:
                await _sample_exit_ip()
            except Exception as exc:  # noqa: BLE001
                print(f"[maintain] ip sample error: "
                      f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
        try:
            await _fill_pool_gap()
        except Exception as exc:  # noqa: BLE001
            print(f"[maintain] refill error: {type(exc).__name__}: "
                  f"{str(exc)[:100]}", flush=True)
        try:
            pool = _state["pool"]
            for e in list(pool):
                if e.get("recycling") or e.get("inflight", 0) > 0:
                    continue
                # pin 模式下「按次数换 context」由配额接管：换 context 会丢掉
                # 已绑定的槽位（回到 2088 浏览器级代理），下一次请求又得重建，
                # 纯属浪费。是否换 IP 交给注册表的 remaining 决定。
                if e.get("want_recycle") or (
                        not PIN_ENABLED and e["submits"] >= MAX_REQ_PER_CTX):
                    e["want_recycle"] = False
                    schedule_recycle(e)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintain] error: {type(exc).__name__}: {str(exc)[:100]}",
                  flush=True)
        # ---- 内存水位自保 ----------------------------------------------
        try:
            mem = _cgroup_mem()
            if mem:
                used, limit = mem
                ratio = used / limit
                now = time.time()
                mem_restarts = [t for t in mem_restarts if now - t < 600]
                idle = _state["total_inflight"] == 0
                reason = ""
                if ratio >= MEM_HARD_RATIO and len(mem_restarts) < 6:
                    reason = f"mem {ratio:.0%} >= hard {MEM_HARD_RATIO:.0%}"
                elif ratio >= MEM_SOFT_RATIO and idle and \
                        len(mem_restarts) < 6:
                    reason = f"mem {ratio:.0%} >= soft {MEM_SOFT_RATIO:.0%} (idle)"
                if reason:
                    mem_restarts.append(now)
                    await _restart_browser(reason)
        except Exception as exc:  # noqa: BLE001
            print(f"[maintain] mem check error: {type(exc).__name__}: "
                  f"{str(exc)[:100]}", flush=True)


class GenReq(BaseModel):
    image_b64: str
    prompt: str
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    mode: str = "i2i"
    boxes: str | None = None
    references: str | None = None
    refs: list[str] | None = None
    #: "single" -> reference(单数) 文件部件 + references(id 数组)，editing 分支
    #: 缺省 -> references(复数) 文件部件，composition 分支（现网行为）
    ref_mode: str | None = None
    #: 上游 model 字段（"flux-3-image" / 待确认的 klein 取值）。
    #: 留空不发，与历史行为一致。
    model: str | None = None
    #: 指定出口槽位（2090 pin 模式）。调用方按注册表决定用哪个槽位；
    #: 给了就必须钉死在该槽位，否则「成功算到哪个 IP 头上」会失真。
    proxy_slot: int | None = None


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
    ready = sum(1 for e in pool if _page_ok(e))
    return {
        # ok 必须反映「能不能干活」：pages_ready=0 时进程活着但 /generate 必 503
        # （2026-11-06 凌晨那 1.5 小时就处于这个状态，应用日志里却看不到 5xx）。
        "ok": ready > 0,
        "pages": len(pool),
        "pages_ready": ready,
        "pages_cooling": sum(1 for e in pool if e["cooldown_until"] > now),
        "pages_recycling": sum(1 for e in pool if e.get("recycling")),
        "inflight": _state["total_inflight"],
        "max_inflight": MAX_INFLIGHT,
        "breaker": {"open": _breaker_open(), "trips": _breaker["trips"],
                    "fails": _breaker["fails"],
                    "last_reason": _breaker["last_reason"]},
        "rps_limit": GLOBAL_RPS,
        "exit_ip": _state.get("exit_ip"),
        "exit_ip_local": _state.get("exit_ip_local"),
        "exit_ip_leak": _state.get("exit_ip_leak", 0),
        "exit_ip_samples": _state.get("exit_ip_samples", 0),
        "exit_ip_distinct": len(set(_state.get("exit_ip_seen") or [])),
        "per_page": [{"idx": e["idx"], "gen": e.get("gen", 0),
                      "inflight": e["inflight"],
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
    await _rate_wait()
    await sem.acquire()
    entry: dict | None = None
    switched: set[int] = set()
    t_start = time.time()
    want_slot = req.proxy_slot
    try:
        entry = await acquire_entry(prefer_slot=want_slot)
        if want_slot is not None:
            if not PIN_ENABLED:
                return JSONResponse(
                    status_code=503,
                    content={"error": "pin mode not configured on sidecar"})
            if entry.get("slot") != want_slot:
                # 页面当前不在目标槽位上：换 context。失败就直接拒绝，
                # 不能退回旧槽位——否则这次成功会被记到错误的 IP 头上。
                try:
                    await _rebind(entry, want_slot)
                except Exception as exc:  # noqa: BLE001
                    return JSONResponse(
                        status_code=503,
                        content={"error": f"rebind slot {want_slot} failed: "
                                          f"{type(exc).__name__}"})
        sub = None
        for attempt in range(3):
            if entry["err_streak"] >= 3 and want_slot is None:
                # 该页连续失败：冷却 + 后台换新会话，避免在同一坏出口反复撞墙。
                # pin 模式下不换页：换了就脱离指定槽位，计数会算错 IP。
                bad = entry
                bad["cooldown_until"] = time.time() + min(
                    IP_COOLDOWN_S * 3, IP_COOLDOWN_MAX_S)
                _release(entry)
                switched.add(id(bad))
                bad["want_recycle"] = True    # 后台任务空闲时换 IP
                entry = await acquire_entry(exclude=switched)
            t_page = time.time()
            try:
                sub = await _eval(entry, SUBMIT_JS, {
                    "prompt": req.prompt, "aspectRatio": req.aspect_ratio,
                    "resolution": req.resolution, "mode": req.mode,
                    "imgB64": req.image_b64, "boxes": req.boxes,
                    "references": req.references, "refs": req.refs or [],
                    "refMode": req.ref_mode, "model": req.model,
                }, timeout=90)
            except Exception as exc:  # noqa: BLE001
                if not _page_dead(exc):
                    raise
                _release(entry)
                _retire(entry, f"submit: {type(exc).__name__}")
                if attempt >= 2:
                    raise
                switched.add(id(entry))
                entry = await acquire_entry(exclude=switched)
                continue
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
            if want_slot is not None:
                # pin 模式：出口由调用方指定，不能私自换页（换了就是换 IP，
                # 429 的归属和后续计数全乱）。直接把这个槽位的 429 抛回去。
                break
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
                # 三个出口都 403 才算一次"上游在拒绝浏览器"（单个坏 IP 不算）。
                # pin 模式下不熔断：403 只说明**这一个**出口被拒，上层会换槽；
                # 在这里熔断会把"某个 IP 被封"放大成"全站停止出图"（实测
                # 3 次 403 后所有请求 0.13s 直接 502）。
                if want_slot is None:
                    _breaker_on_block("403 access_denied (browser)")
                entry["cooldown_until"] = time.time() + IP_COOLDOWN_S
            # 429 必须原样上报。折叠成 502 会让上层把"配额耗尽、应当退避"误判成
            # "服务故障、立即重试"，单小时能放大出数百次无效重试（2026-11-17 实测）。
            if sub["status"] == 429:
                return JSONResponse(status_code=429,
                                    content={"error": f"submit 429: {body}"})
            # 403 = 这个出口被上游拒绝（IP 级或指纹级）。同样要原样上报：
            # 上层据此换槽重试，折成 502 就失去了"换一个出口再试"的依据。
            if sub["status"] == 403:
                return JSONResponse(status_code=403,
                                    content={"error": f"submit 403: {body}"})
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
        #: 轮询阶段吃到的 429 次数。status 是 GET、走同一个出口 IP，
        #: 与 submit 共用 CF 的边缘速率窗口（但**不消耗** generate 的业务额度）。
        poll_429 = 0
        eta_s = POLL_ETA_S.get(req.model or DEFAULT_MODEL, DEFAULT_ETA_S)
        while time.time() < deadline:
            try:
                st = await _eval(entry, STATUS_JS, token, timeout=30)
            except Exception as exc:  # noqa: BLE001
                if not _page_dead(exc):
                    raise
                # 任务已经提交到上游：换一页继续轮询即可，丢任务才是最坏结果
                _release(entry)
                _retire(entry, f"poll: {type(exc).__name__}")
                switched.add(id(entry))
                entry = await acquire_entry(exclude=switched)
                continue
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
            elif st["status"] == 429:
                # 轮询侧同样会吃 CF 边缘频率限制。原实现把 429 当普通抖动，
                # 以固定 1s 间隔打满整个 165s 窗口 —— 频控下最坏的应对方式：
                # 任务救不回来，还持续加码把自己锁更久。这里指数退避。
                poll_429 += 1
                await asyncio.sleep(min(2 ** poll_429, 8))
                continue
            # 抖动：固定 1.000s 的等间隔轮询是最容易被识别的机器节奏。
            # 节奏按 ETA 分三段（先疏后密）：55% ETA 之前几乎不可能 ready，
            # 用 3s 稀疏探测；接近 ETA 时 1.2s；超过 ETA 说明偏慢，0.8s 抢时间。
            elapsed = time.time() - t_submit
            if elapsed < eta_s * 0.55:
                interval = 3.0
            elif elapsed < eta_s:
                interval = 1.2
            else:
                interval = 0.8
            await asyncio.sleep(interval * random.uniform(0.85, 1.5))
        if not image_token:
            print(f"[gen] poll timeout after {rounds} rounds "
                  f"({time.time() - t_submit:.0f}s) 429x{poll_429} "
                  f"last={last[:120]!r}",
                  flush=True)
            return JSONResponse(status_code=504,
                               content={"error": f"poll timeout after "
                                                 f"{rounds} rounds: {last}"})

        t_ready = time.time()
        try:
            med = await _eval(entry, MEDIA_JS, image_token, timeout=MEDIA_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            if not _page_dead(exc):
                raise
            _release(entry)
            _retire(entry, f"media: {type(exc).__name__}")
            switched.add(id(entry))
            entry = await acquire_entry(exclude=switched)
            med = await _eval(entry, MEDIA_JS, image_token, timeout=MEDIA_TIMEOUT_S)
        print(f"[gen] poll={t_ready - t_submit:.1f}s rounds={rounds} "
              f"media={time.time() - t_ready:.1f}s "
              f"total={time.time() - t_start:.1f}s slot={entry.get('slot')}",
              flush=True)
        if med.get("status") != 200 or not med.get("b64"):
            return JSONResponse(status_code=502,
                               content={"error": f"media {med.get('status')}: "
                                                 f"{str(med.get('body'))[:150]}"})
        return {"image_b64": med["b64"], "slot": entry.get("slot")}
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


async def _prebind_pages() -> None:
    """把剩余预绑定槽位分配到空闲页面上（后台跑，不阻塞启动）。

    只绑「当前没有槽位」的页面：已经有绑定的不要动，那是正在服务的窗口。
    """
    if not PIN_ENABLED or not PREBIND_SLOTS:
        return
    for slot in PREBIND_SLOTS[1:]:
        try:
            pool = _state.get("pool") or []
            free = [e for e in pool
                    if e.get("slot") is None and e.get("inflight", 0) == 0
                    and not e.get("recycling")]
            if not free:
                return
            entry = free[0]
            entry["inflight"] = 1          # 占位，避免被并发请求抢走
            try:
                await _rebind(entry, slot)
            finally:
                entry["inflight"] = 0
            print(f"[prebind] page#{entry['idx']} -> slot {slot:03d}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 预绑定失败不影响服务
            print(f"[prebind] slot {slot:03d} failed: {type(exc).__name__}",
                  flush=True)


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
                              aspect_ratio="1:1", resolution="1k", mode="i2i",
                              proxy_slot=PREBIND_SLOTS[0] if PREBIND_SLOTS else None))
        await _prebind_pages()
        print("[prewarm] done", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[prewarm] failed: {type(exc).__name__}: {str(exc)[:100]}",
              flush=True)


@app.on_event("startup")
async def _warm():
    asyncio.create_task(_detect_local_ip())
    asyncio.create_task(_prewarm_generate())
    asyncio.create_task(_maintain_pool())
