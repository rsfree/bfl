"""bfl2openai — OpenAI Images API compatibility layer over flux-tools.bfl.ai
precise editing (reverse-engineered /api/flux3-image/* endpoints).

Exposed endpoints
-----------------
POST /v1/images/edits       image edit (core mapping; supports global prompt,
                            OpenAI mask -> region boxes, and a `boxes`
                            extension param for precise per-region edits)
POST /v1/images/generations text-to-image (upstream is used without an input
                            image, mode="t2i"; unverified — the playground
                            only exposes i2i)
GET  /v1/images/results/{imageToken}   proxy for the generated image bytes
GET  /v1/models             model list (canonical id + aliases)
GET  /healthz

Run:  uvicorn bfl2openai.server:app --host 127.0.0.1 --port 8977
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from contextlib import asynccontextmanager
import os
from contextvars import ContextVar
import re
import time
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import browser_egress_client
from . import egress_pool
from . import payload
from . import upstream
from . import native
from . import bridging
from . import upscale as mps
from . import uploads
from . import async_images
from . import probe
from . import store
from .bridging import (
    image_to_png,
    mask_to_boxes,
    parse_boxes,
    parse_size_dims,
    size_to_aspect_ratio,
    tier_for_long_edge,
)
from .upstream import UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bfl2openai")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 探针在 lifespan 里启动：on_event 已被 FastAPI 标记 deprecated，
    # 而 requirements 是 fastapi>=0.110（会随 pip 升到最新），不能赌它还在。
    probe.start()
    yield


app = FastAPI(title="bfl2openai", version="0.2.0", lifespan=_lifespan)
native.register(app)
uploads.register(app)
async_images.register(app)
# 运维面：入口层 nginx 对 /panel 直接 404，只能走 SSH 隧道访问。
from . import panel  # noqa: E402 - 必须在 app 建好之后注册

app.include_router(panel.router)

MODEL_ID = os.environ.get("BFL2OPENAI_MODEL", "flux-precise-edit")
API_KEY = os.environ.get("BFL2OPENAI_API_KEY", "")  # optional; empty = auth disabled
UPSTREAM_BASE = os.environ.get("BFL2OPENAI_UPSTREAM", upstream.DEFAULT_BASE)

#: 模型别名 = 同一个上游模型的另外几个「名字」。
#: 适配器里模型名不参与路由（真正决定模型的是 BFL2OPENAI_MODEL，上游也只有
#: flux-tools 这一个模型），所以别名唯一的职责是「让客户端按它熟悉的叫法来调、
#: 并且能在 /v1/models 里被发现」：fal 的 flux-3-image、kontext 系列，或网关里
#: 给客户看的商用名。逗号（或分号）分隔，匹配大小写不敏感。
#:
#: `flux-3`（短名）是 new-api 渠道 39 里已经在用的写法 —— 服务端不登记它，
#: 列表里就看不见，strict 模式下还会被判 404，所以必须与 `flux-3-image` 并列。
DEFAULT_MODEL_ALIASES = "flux-3-image,flux-3,flux-kontext-max,flux-kontext-pro"


def _env_list(name: str, default: str) -> list[str]:
    """逗号分隔的环境变量 → 去空白、去重（忽略大小写）、保留原拼写的列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in os.environ.get(name, default).replace(";", ",").split(","):
        value = chunk.strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out


MODEL_ALIASES = _env_list("BFL2OPENAI_MODEL_ALIASES", DEFAULT_MODEL_ALIASES)

#: 未登记模型名的处置。默认放行——兼容层不该因为客户端写了个本地别名就拒单
#: （上游只有一个模型，叫什么都出同一张图）；置 1 则 404 model_not_found，
#: 让「渠道配错了名字」当场暴露，而不是静默扣量。
_MODEL_STRICT = os.environ.get("BFL2OPENAI_MODEL_STRICT", "") == "1"


def model_ids() -> list[str]:
    """对外公布的全部模型名：规范名在前，别名去重后跟。"""
    ids = [MODEL_ID]
    seen = {MODEL_ID.lower()}
    for alias in MODEL_ALIASES:
        if alias.lower() not in seen:
            seen.add(alias.lower())
            ids.append(alias)
    return ids


def model_route(name: str | None) -> dict:
    """请求里的 `model` → 路由信息（别名与规范名完全等价）。

    返回 requested/resolved/alias/known/denied 五个键。`denied` 只在
    BFL2OPENAI_MODEL_STRICT=1 且名字未登记时为真。
    """
    requested = (name or "").strip()
    alias = None
    known = not requested
    if requested:
        if requested.lower() == MODEL_ID.lower():
            known = True
        else:
            for candidate in MODEL_ALIASES:
                if requested.lower() == candidate.lower():
                    alias, known = candidate, True
                    break
    return {
        "requested": requested,
        "resolved": MODEL_ID,
        "alias": alias,
        "known": known,
        "denied": bool(requested) and not known and _MODEL_STRICT,
    }


def stamp_model(resp: JSONResponse, route: dict) -> JSONResponse:
    """把「请求了什么名字 / 实际跑的是哪个模型」写进响应头。

    别名命中时客户端可据此确认没有落到别的渠道；未登记名字留一条日志，
    用来发现客户端/网关的拼写漂移（不落 prompt，不侵犯客户内容）。
    """
    if route["requested"]:
        resp.headers["X-Bfl-Model-Requested"] = route["requested"][:64]
        resp.headers["X-Bfl-Model-Resolved"] = route["resolved"]
        if not route["known"]:
            log.info("unregistered model %r accepted as %s (lenient mode)",
                     route["requested"][:64], route["resolved"])
    return resp


# ---------------------------------------------------------------- auth


async def check_auth(request: Request) -> None:
    if not API_KEY:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {API_KEY}":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error"}},
        )


# ---------------------------------------------------------------- helpers


def openai_error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


#: 对外错误文案的唯一来源。上游原文**只进日志**，因为它会泄漏：
#:   - CF ray id（`sin1::mqx5x-…` 直接暴露出口机房区域）
#:   - 上游 Zod 报错的枚举清单（等于把内部契约交出去）
#:   - 内部拓扑（`browser egress 500` / `127.0.0.1:8011`）
#:   - 代理 URL（万一异常信息里带了 user:pass）
_SAFE_ERRORS = {
    "content_policy_violation": "content rejected by upstream policy",
    "rate_limit": "upstream quota exhausted; retry later",
    "upstream_timeout": "upstream timeout; retry later",
    "upstream_unavailable": "upstream temporarily unavailable",
    "upstream_rejected": "upstream rejected the request",
    "invalid_request": "invalid or expired request token; submit a new job",
}

#: 429 响应的 Retry-After 秒数（OpenAI 语义：客户端据此退避）。
#: 上游配额是「当天耗尽」，这里给 60s 而不是 3600 —— 出口池会换 IP/换页，
#: 短期内确有恢复可能；给太大等于让客户端一小时内不再尝试。
_RETRY_AFTER_S = "60"

#: 429 语义兜底识别。显式 kind/status 之外，旧版 sidecar 会把上游 429 折叠进
#: 502 的错误文本里（`submit 429: ...`），滚动发布期间必须靠文案兜住。
#: 只参与「→ 429」的判定，绝不参与对外文案。
_RATE_LIMIT_RE = re.compile(
    r"(?:\bHTTP\s*429\b|\bsubmit\s*429\b|\brate[_ -]?limit"
    r"|\bquota\b|\btoo many requests\b)", re.I)

_SECRET_RE = re.compile(r"(?:socks5h?|https?|wss?)://[^@\s/]+@")
#: CF 边缘 ray id（`sin1::mqx5x-1794…`）前缀是机房代码，等于暴露出口区域。
_RAY_RE = re.compile(r"\b[a-z]{3}\d{1,2}::[0-9A-Za-z_-]+")

#: 上游固定文案 → 标准词。日志里保留原文没意义（都是已知分类），
#: 而 `You've used today's 30 free generations` 这类句子会把额度规则写进日志。
#: 正则必须吃到整句尾巴，否则脱敏后仍残留「明天再来」这条规则。
_PHRASE_MAP = (
    (re.compile(r"you'?ve used today'?s \d+ free generations"
                r"(?:[.!\s]*please come back tomorrow)?", re.I),
     "<quota_exhausted>"),
    (re.compile(r"\btoo many requests\b", re.I), "<rate_limited>"),
    (re.compile(r"\baccess denied\b", re.I), "<access_denied>"),
    (re.compile(r"\bcontent moderation\b", re.I), "<moderation_rejected>"),
)

#: 内部 kind → 对外 type。只放行 OpenAI 规范的词汇，
#: 其余一律 api_error（避免 access_denied / composition 这类内部词外泄）。
_PUBLIC_TYPE = {
    "invalid_request_error": "invalid_request_error",
    "content_policy": "content_policy_violation",
    "rate_limit": "rate_limit_error",
}


def _public_type(kind: str) -> str:
    return _PUBLIC_TYPE.get(kind, "api_error")


def _redact(text: str, limit: int = 400) -> str:
    """日志用：清 URL 凭据、CF ray id，并把上游固定文案归一成标准词。

    对外响应不要用它 —— 用 _SAFE_ERRORS 的固定文案。
    """
    s = _SECRET_RE.sub("<redacted>://", str(text))
    s = _RAY_RE.sub("<ray>", s)
    for pat, token in _PHRASE_MAP:
        s = pat.sub(token, s)
    return s[:limit]


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：未捕获异常一律回 JSON 错误体，而不是纯文本 500。

    Starlette 默认的 500 是 21 字节纯文本 "Internal Server Error"：OpenAI
    客户端按 JSON 解析会得到空 message，把"我们自己的 bug"误判成"网关
    整体不可用"。这里至少给出异常类型，且日志里有完整 traceback。
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return openai_error(500, f"internal error: {type(exc).__name__}",
                        "server_error")


def wrap_upstream_error(exc: UpstreamError) -> JSONResponse:
    # 内容审核（moderation）是客户端内容问题：422 让调用方明确知道
    # "换内容"而不是"重试"（此前误映射为 502，客户会无效重试且监控失真）。
    if exc.kind == "content_policy":
        # 451 Unavailable For Legal Reasons：内容合规拒绝的 HTTP 标准语义
        log.warning("moderation rejected: %s", _redact(exc.message, 300))
        return openai_error(451, _SAFE_ERRORS["content_policy_violation"],
                            "content_policy_violation")
    # 上游配额 / 限流：对齐 OpenAI 语义回 429 + Retry-After，让客户端退避。
    # 2026-11-17 之前这里一律 502，客户端把"额度耗尽"当"服务故障"持续重试，
    # 一个下午放大出 718 次 504 + 242 次 502。
    if exc.kind == "rate_limit" or (
            exc.kind != "invalid_request_error"
            and _RATE_LIMIT_RE.search(str(exc.message))):
        log.warning("upstream rate limited: %s", _redact(exc.message, 400))
        resp = openai_error(429, _SAFE_ERRORS["rate_limit"], "rate_limit_error")
        resp.headers["Retry-After"] = _RETRY_AFTER_S
        return resp
    if exc.kind == "invalid_request_error":
        # 严格脱敏：上游 token 失效原文含内部标识，只在服务端日志里留原生记录
        log.warning("upstream invalid request: %s", _redact(exc.message, 400))
        return openai_error(400, _SAFE_ERRORS["invalid_request"],
                            "invalid_request_error")
    log.warning("upstream error kind=%s status=502: %s",
                exc.kind, _redact(exc.message))
    return openai_error(502, _SAFE_ERRORS["upstream_unavailable"],
                        _public_type(exc.kind))


def wrap_browser_egress_error(
        exc: browser_egress_client.BrowserEgressError) -> JSONResponse:
    """sidecar 失败 → 客户端有意义的码。

    历史行为：browser_egress_client 抛裸 RuntimeError，run_job 只捕
    UpstreamError / MPSError ⇒ 冒到 ASGI 层变 500 + 21 字节空体。客户端
    拿不到原因，会把"上游暂时不可用"当"服务端崩溃"。生产 2026-10-26
    实测 50 次，其中 34 次真实原因是多图契约 400、10 次是匿名配额 429。

    映射依据：
      - 451：内容审核/合规拒绝（sidecar 已把上游 422 归一成 451）→ 客户换内容
      - 429：上游匿名配额耗尽 → 客户端退避，而不是立刻重试
      - 0  ：传输层（连接/超时）→ 504，语义是"等得到但没等到"
      - 其它：上游契约/内部错误 → 502，重试有意义但不该被当成 4xx
    """
    log.warning("browser egress failure status=%s: %s",
                exc.status, _redact(exc, 400))
    # 429：上游匿名配额 / 边缘限流耗尽 → 客户端退避。
    # 兼容滚动发布期的新旧版本混跑：旧 sidecar 会把上游 429 折叠成 502 的 body，
    # 这里按文案兜底，否则 429 语义会重新丢回 502。
    if exc.status == 429 or (exc.status == 502
                             and _RATE_LIMIT_RE.search(str(exc))):
        resp = openai_error(429, _SAFE_ERRORS["rate_limit"], "rate_limit_error")
        resp.headers["Retry-After"] = _RETRY_AFTER_S
        return resp
    if exc.status == 451:
        return openai_error(451, _SAFE_ERRORS["content_policy_violation"],
                            "content_policy_violation")
    if "413" in str(exc) or "Request Entity Too Large" in str(exc):
        # 上游 body 上限：重试无用，必须让调用方缩图（此前笼统报 502，
        # 客户端会当成服务故障无限重试）。
        return openai_error(413, "payload too large for upstream; "
                                 "downscale the input images", "invalid_request_error")
    if exc.status == 0:
        return openai_error(504, _SAFE_ERRORS["upstream_timeout"],
                            "api_error")
    return openai_error(502, _SAFE_ERRORS["upstream_unavailable"],
                        "api_error")


def _parse_long_side(size: str | None) -> int | None:
    """A "WxH" size as its long edge, or None for ratio/junk.

    走 bridging.parse_size_dims，和宽高比解析共用一套分隔符（x / × / *），
    否则 "1536×1024" 会「比例对了但长边丢了」，超 2k 升采样静默不触发。
    """
    dims = parse_size_dims(size)
    if not dims:
        return None
    return max(dims)


#: Anonymous generation tops out at the 2k tier (long edge 2048); anything
#: beyond is reached by generating at 2k and MPS-upscaling (x = long/2048).
GEN_LONG_EDGE = 2048

#: 审核诊断采样开关（默认关）。开时审核拒绝会记录 prompt 前 100 字符，
#: 用于定位高频违规词；采样期结束务必关掉（客户内容不落日志是默认原则）。
_MODERATION_DIAG = os.environ.get("BFL2OPENAI_MODERATION_DIAG", "") == "1"

#: 浏览器优先：上游指纹封禁期间（curl_cffi 恒 403 + 代理重试白耗 2-3 分钟），
#: 置 1 时直接走 browser_egress sidecar，跳过 curl_cffi 尝试链。
_BROWSER_PRIMARY = os.environ.get("BFL2OPENAI_BROWSER_PRIMARY", "") == "1"

#: 参考图分支策略（2026-11-16 官网抓包新增第二条分支；2026-11-17 起默认走新版）：
#:   "single"（默认）-> reference(单数) 文件部件 + references 退化成 id 数组 + i2i
#:                 = 官网 precise-editing 分支，canvas 作为 image 部件受保护。
#:                   **实测保真度比 composition 好约 10 倍**（墙面偏差 89→8.7），
#:                   且模糊 prompt 下不会丢物体（composition 会整幅重画成别的东西）。
#:   "multi"      -> references(复数) 文件部件 + t2i + canvas 并入 refs[0]
#:                 = 旧 composition 语义，保留作回退开关。
#: 两条都保留：single 被上游拒绝(400/422)时自动降级 multi（见 _run_job_impl）。
REF_BRANCH = os.environ.get("BFL2OPENAI_REF_BRANCH", "single")


# ---- images 门幂等（2026-11-09）------------------------------------------
# 背景：到机房 64.178.99.0/24 的 TCP 握手丢包 20–58%（实测），客户端超时
# 重试是常态。`/v1/images/generations` 是同步长请求（18–45s），没有幂等时
# 每次重试都会**真的再生成一遍**：白烧上游额度、白等一遍，两次结果还不一样。
# 这里用「参数指纹 + 单飞（singleflight）+ 结果短缓存」把重试变成廉价操作：
# 同指纹的第二个请求等第一个的结果，而不是另起炉灶。
# 边界：只能兜住「请求到了、响应没回来」；SYN 阶段就丢的请求服务端根本
# 收不到，那种只能靠客户端重试再打进来 —— 但那时重试也安全了。
_IDEM_TTL_S = int(os.environ.get("BFL2OPENAI_IDEM_TTL_S") or 900)
_IDEM_LOCK_TTL_S = int(os.environ.get("BFL2OPENAI_IDEM_LOCK_TTL_S") or 900)
_IDEM_WAIT_S = float(os.environ.get("BFL2OPENAI_IDEM_WAIT_S") or 240)
#: 结果超过此体积不落 Redis：缓存会挤占任务状态空间。
#: 3MB 是旧值，而 2k b64 单图中位 **7.7MB**（2026-11-13 线上实测 n=244）——
#: 最常见的成功响应恰好落在缓存之外，幂等对它们完全失效，客户端每次重试都
#: 重新生成一张、白烧上游配额。这里放宽到 12MB（覆盖 p90），并用
#: `_IDEM_BIG_TTL_S` 给大体积结果一个很短的 TTL：Redis 的
#: maxmemory-policy=volatile-ttl 优先淘汰 TTL 最短的键，大图缓存会先于
#: `bridge:*`（TTL 1800/3600）被淘汰，不会把任务状态挤掉。
_IDEM_MAX_CACHE_BYTES = int(
    os.environ.get("BFL2OPENAI_IDEM_MAX_CACHE_BYTES") or 12_000_000)
#: 大体积结果的缓存寿命（秒）。幂等窗口只需覆盖「客户端重试间隔」，
#: 留 15 分钟没有收益，只会长期占着每条 7~12MB 的内存。
_IDEM_BIG_TTL_S = int(os.environ.get("BFL2OPENAI_IDEM_BIG_TTL_S") or 180)
_IDEM_BIG_BYTES = 3_000_000
#: 锁心跳：leader 执行期间每 N 秒刷新一次锁的 ts。
#: 没有心跳时，进程被重启（发版 / OOM / worker 回收）会在 Redis 里留下一条
#: 活到 TTL 结束的「僵尸锁」：后续同指纹请求只能干等到 `_IDEM_WAIT_S` 再收
#: 504，而那个任务其实早就没人跑了。2026-11-13 03:14 发版后 a107942e 就是
#: 这么卡了 240 秒。
_IDEM_HEARTBEAT_S = float(os.environ.get("BFL2OPENAI_IDEM_HEARTBEAT_S") or 5)
#: 锁上的心跳超过这个秒数没更新 ⇒ 认定 leader 已死，follower 可以抢锁重跑。
#: 必须明显大于心跳间隔（丢一两次心跳不算死），又必须小于客户端超时。
_IDEM_STALE_S = float(os.environ.get("BFL2OPENAI_IDEM_STALE_S") or 30)


def _fp_feed(h, val) -> None:
    """把任意嵌套结构稳定地喂进 sha256（bytes 原样，其余取 repr）。"""
    if isinstance(val, bytes):
        h.update(b"b" + str(len(val)).encode() + b":")
        h.update(val)
    elif isinstance(val, (list, tuple)):
        h.update(b"l" + str(len(val)).encode() + b":")
        for item in val:
            _fp_feed(h, item)
    elif isinstance(val, dict):
        h.update(b"d" + str(len(val)).encode() + b":")
        for key in sorted(val, key=str):
            h.update(str(key).encode())
            _fp_feed(h, val[key])
    else:
        h.update(repr(val).encode())


def job_fingerprint(**parts) -> str:
    h = hashlib.sha256()
    for key in sorted(parts):
        h.update(key.encode())
        _fp_feed(h, parts[key])
    return h.hexdigest()[:32]


async def _idem_heartbeat(lock_key: str) -> None:
    """执行期间定期刷新锁的 ts，让 follower 能区分「还在跑」和「人都没了」。"""
    while True:
        await asyncio.sleep(_IDEM_HEARTBEAT_S)
        try:
            await store.put(lock_key, {"ts": time.time()}, _IDEM_LOCK_TTL_S)
        except Exception:  # noqa: BLE001 - 心跳失败不该打断生成
            return


async def _idem_lock_alive(lock_key: str) -> bool:
    """锁还在、且心跳没有过期。过期 = leader 进程已经不在了（僵尸锁）。"""
    doc = await store.get(lock_key)
    if doc is None:
        return False
    try:
        ts = float((doc or {}).get("ts") or 0)
    except (TypeError, ValueError):
        return True          # 结构不认识就当成活的，宁可等也别抢
    if ts and time.time() - ts > _IDEM_STALE_S:
        log.warning("idem lock stale (%.0fs without heartbeat), stealing",
                    time.time() - ts)
        try:
            await store.drop(lock_key)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


async def _idem_run(fp: str, produce) -> JSONResponse:
    """同指纹请求单飞：第一个执行并缓存，其余等它的结果。

    `produce` 只在拿到锁的进程里执行一次。锁用 SET NX 保证跨 worker 唯一；
    异常路径一定释放，避免一次失败把后续重试全卡死在 504 上。

    🔴 等不到结果时**不能直接 504**：锁消失（leader 抛错 / 被回收 / 超时退出）
    而没有缓存，说明这笔活现在没人干，follower 必须接管重跑。旧实现一 break
    就返回 504，等于把 leader 的一次失败放大成「所有并发重试全部 504」，
    客户端再重试又生成一轮（2026-11-12 18:34–19:02 的 40 次 504 全走这条
    路径：报文 107 字节、与 `idem wait timeout` 日志逐秒对齐，且同期
    `browser egress status=0` 计数为 0，证明上游根本没超时）。
    """
    done_key = f"idem_done:{fp}"
    lock_key = f"idem:{fp}"

    # 两轮：第一轮可能是 follower，第二轮（接管）才有机会真正执行 produce。
    for _ in (1, 2):
        cached = await store.get(done_key)
        if cached is not None:
            log.info("idem replay fp=%s", fp[:8])
            resp = JSONResponse(cached)
            resp.headers["X-Bfl-Idempotent"] = "replay"
            return resp

        if await store.set_nx(lock_key, {"ts": time.time()}, _IDEM_LOCK_TTL_S):
            hb = asyncio.create_task(_idem_heartbeat(lock_key))
            try:
                resp = await produce()
                raw = bytes(resp.body)
                if resp.status_code == 200 and len(raw) <= _IDEM_MAX_CACHE_BYTES:
                    ttl = _IDEM_BIG_TTL_S if len(raw) > _IDEM_BIG_BYTES else _IDEM_TTL_S
                    try:
                        await store.put(done_key, json.loads(raw), ttl)
                        log.info("idem cached fp=%s bytes=%d ttl=%d",
                                 fp[:8], len(raw), ttl)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("idem cache skipped fp=%s: %s", fp[:8], exc)
                return resp
            finally:
                hb.cancel()
                await store.drop(lock_key)

        deadline = time.time() + _IDEM_WAIT_S
        log.info("idem wait fp=%s (identical job in flight)", fp[:8])
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            cached = await store.get(done_key)
            if cached is not None:
                log.info("idem replay-after-wait fp=%s", fp[:8])
                resp = JSONResponse(cached)
                resp.headers["X-Bfl-Idempotent"] = "replay"
                return resp
            if not await _idem_lock_alive(lock_key):
                break
        else:
            # 锁一直在，说明 leader 确实还在跑：这次是「等不到」而非「没人干」
            log.warning("idem wait timeout fp=%s (leader still running)", fp[:8])
            resp = openai_error(
                504,
                "an identical generation is still in flight; retry in a few seconds",
                "timeout")
            resp.headers["Retry-After"] = "3"
            return resp

        # 走到这里 = 锁已消失但没缓存：leader 失败/退出，接管重跑
        log.warning("idem leader gone without result fp=%s, taking over", fp[:8])

    # 两轮都被抢走（极端并发）：让客户端退避重试，别继续压
    resp = openai_error(504, "generation slot busy; retry shortly", "timeout")
    resp.headers["Retry-After"] = "2"
    return resp


#: 单个请求最多换几个槽位。429 说明该出口 IP 当天废了，换一个继续；
#: 3 次仍失败说明池子整体耗尽，此时回 429 让客户端退避是正确的语义。
_SLOT_ATTEMPTS = int(os.environ.get("BFL2OPENAI_SLOT_ATTEMPTS", "3"))

#: 请求级出口开关（灰度用）。`X-Bfl-Egress: pool` 强制走槽位，`legacy`
#: 强制走 2088。不带头时按环境变量决定 —— 这样可以在真实流量的同时验证
#: 新链路，不必为了测试把全站切过去。
_EGRESS_MODE: ContextVar[str] = ContextVar("bfl_egress_mode", default="")


@app.middleware("http")
async def _egress_mode_mw(request: Request, call_next):
    tok = _EGRESS_MODE.set(
        request.headers.get("x-bfl-egress", "").strip().lower())
    try:
        return await call_next(request)
    finally:
        _EGRESS_MODE.reset(tok)


#: 进程内黏性槽位：同一个 worker 反复用同一个出口，直到它没额度。
#: 为什么黏：换槽位就要重建 context，而 Vercel 挑战只对"活了一会儿的页面"
#: 放行。实测每次请求都换槽 → 延迟 31~98s；黏住之后页面是热的。
#: 代价：并发出口数 ≈ worker 数（6），但每个槽位仍按额度轮转，总吞吐不变。
_STICKY: dict[str, int] = {"slot": 0}


#: 与侧车同名的预绑定槽位：这些槽位在启动时已经被绑到页面上，优先用它们
#: 能直接跳过 context 重建（每次约 2s）。两边必须完全一致，否则绑了用不上。
_PREBIND: list[int] = [int(x) for x in
                       os.environ.get("BFL2OPENAI_POOL_PREBIND", "").split(",")
                       if x.strip().isdigit()]


#: 单槽位并发上限。实测单个 IP 只要约 3.5 发/分钟就会被 Vercel 边缘限速，
#: 而一次生成约 20s ⇒ 一个 IP 同时只该跑 1 个请求。
#: 并发要靠**摊到不同 IP** 拿到，不是靠一个 IP 硬扛。
_SLOT_CAP = int(os.environ.get("BFL2OPENAI_SLOT_INFLIGHT", "1"))
_INFLIGHT: dict[int, int] = {}

#: 每个 worker 从预绑定列表的不同位置起扫。不这么做的话 6 个 worker 会全部
#: 从第一个元素开始，集体压在同一 IP 上（实测 6 并发即触发边缘限速）。
_OFFSET = (os.getpid() + int(os.environ.get("BFL2OPENAI_SLOT_OFFSET", "0"))) % 997


def _take(n: int) -> None:
    _INFLIGHT[n] = _INFLIGHT.get(n, 0) + 1


def _release(n: int) -> None:
    v = _INFLIGHT.get(n, 0) - 1
    if v <= 0:
        _INFLIGHT.pop(n, None)
    else:
        _INFLIGHT[n] = v


def _has_room(n: int) -> bool:
    return _INFLIGHT.get(n, 0) < _SLOT_CAP


async def _next_slot(exclude: set[int]):
    # 1) 当前黏住的槽位（前提是还有并发余量）
    cur = _STICKY["slot"]
    if cur and cur not in exclude and _has_room(cur):
        s = await egress_pool.get_slot(cur)
        if s is not None and s.available():
            _take(s.slot)
            return s

    # 2) 预绑定槽位：从本 worker 专属的偏移开始，避免惊群
    order = _PREBIND[_OFFSET % len(_PREBIND):] + _PREBIND[:_OFFSET % len(_PREBIND)]         if _PREBIND else []
    for n in order:
        if n in exclude or not _has_room(n):
            continue
        s = await egress_pool.get_slot(n)
        if s is not None and s.available():
            _STICKY["slot"] = n
            _take(s.slot)
            return s

    # 3) 正常轮转：先找还有并发余量的
    ex = set(exclude)
    for _ in range(8):
        s = await egress_pool.acquire(exclude=ex)
        if s is None:
            break
        if _has_room(s.slot):
            _STICKY["slot"] = s.slot
            _take(s.slot)
            return s
        ex.add(s.slot)
    # 全部满载：退回任意可用槽位。宁可轻度超发，也不要因为"没余量"直接 429。
    s = await egress_pool.acquire(exclude=exclude)
    if s is not None:
        _STICKY["slot"] = s.slot
        _take(s.slot)
    return s


#: 上游「当日额度耗尽」的报文特征。只有命中这些才算当天废掉，
#: 其余 429 一律按短窗限流处理（歇 90s 再试）。
_DAILY_QUOTA_HINTS = ("free generation", "come back tomorrow", "today's 30")


def _is_daily_quota(exc) -> bool:
    text = str(exc).lower()
    return any(h in text for h in _DAILY_QUOTA_HINTS)


async def _browser_generate(**kwargs) -> bytes:
    """经侧车生成，并**把成败记到出口 IP 头上**。

    未配置 2090 池时完全走原路径（2088 轮换），行为与历史一致 —— 这是
    上线时的安全阀：env 不配就不改变任何现有行为。

    配了 env 但**还没扫描**时同样走原路径：此时索引为空，按槽位调度会
    把所有请求判成"池子耗尽"并回 429。宁可退回旧行为，也不能因为部署
    顺序把整站打挂。

    429 的处置是这里的关键：不重试同一槽位，而是**换槽**。同一个 IP 在
    429 之后短期内不会恢复，原地重试只会把上游的限流窗口越拖越长。
    """
    mode = _EGRESS_MODE.get()
    if mode == "legacy":
        use_pool = False
    elif mode == "pool":
        use_pool = await egress_pool.ready()
    else:
        use_pool = egress_pool.configured() and await egress_pool.ready()
    if not use_pool:
        return await browser_egress_client.generate(**kwargs)

    tried: set[int] = set()
    last: browser_egress_client.BrowserEgressError | None = None
    for _ in range(_SLOT_ATTEMPTS):
        slot = await _next_slot(tried)
        if slot is None:
            # 全部槽位额度耗尽/冷却中：429 比 502 更接近真相
            raise browser_egress_client.BrowserEgressError(
                "all exit slots exhausted", 429)
        tried.add(slot.slot)
        try:
            data = await browser_egress_client.generate(
                proxy_slot=slot.slot, **kwargs)
        except browser_egress_client.BrowserEgressError as exc:
            # 429（额度/限流）与 403（出口被拒）都是"这个 IP 今天别再用"，
            # 处置一致：标记冷却 + 换下一个槽位。区别只在计数口径。
            if exc.status == 429 and not _is_daily_quota(exc):
                # 短窗限流：歇一会儿还能用，不能按「当天废了」处理
                await egress_pool.note_throttled(slot.slot)
                log.warning("slot %03d ip=%s throttled (short window), rotating",
                            slot.slot, slot.ip)
                last = exc
                continue
            if exc.status in (429, 403):
                await egress_pool.note_rate_limited(
                    slot.slot, field=("n403" if exc.status == 403 else "n429"))
                log.warning("slot %03d ip=%s hit %s, rotating (n=%d)",
                            slot.slot, slot.ip, exc.status, len(tried))
                last = exc
                continue
            await egress_pool.note_failure(slot.slot)
            raise
        finally:
            _release(slot.slot)
        await egress_pool.note_success(slot.slot)
        return data
    raise last or browser_egress_client.BrowserEgressError(
        "all exit slots exhausted", 429)


async def _run_job_impl(
    image_png: bytes | None,
    prompt: str,
    boxes: list[dict],
    aspect_ratio: str,
    resolution: str,
    mode: str,
    n: int,
    response_format: str,
    request: Request,
    upscale_to: int | None = None,
    reference_images: list[tuple[bytes, str]] | None = None,
) -> JSONResponse:
    # ---- 多图（composition）归一化 -------------------------------------
    # 上游契约（2026-10-26 实测，Zod 报错暴露枚举）：mode 只有 't2i' | 'i2i'，
    # 且 canvas(`image` 部件) 与 references 互斥：
    #     i2i + canvas + refs  -> 400 "A composition must not include a canvas image."
    #     i2i + refs（无 canvas）-> 400 "An image is required for an edit."
    #     t2i + refs（无 canvas）-> 200 正常出图（1536x1536，带 C2PA）
    # 所以带参考图的请求一律降级成 composition：canvas 并入 references 首位
    # （上游前端 prompt 也是 "Edit the first reference image."），mode 置 t2i。
    #
    # 2026-11-16 官网抓包修正：上面第三条 400 的触发条件是**复数字段名**
    # `references`。官网 precise-editing 用另一条分支——文件部件名是单数
    # `reference`，`references` 退化成 id 数组（["ref","ref-2","<hash>"]），
    # 且 mode 仍是 i2i 并保留 canvas。两条分支共存，语义不同：
    #   multi  : 4 张图当素材重新画一张（画布也会被重绘）
    #   single : 编辑原图 + 注入素材（画布受保护，保真度高）
    # 单数分支依赖 sidecar 改写文件部件名；curl_cffi 路径（upstream.py）只会
    # 发复数字段名，混用必然 400，因此该分支仅在浏览器优先模式下启用。
    single_branch = (REF_BRANCH == "single" and bool(reference_images)
                     and _BROWSER_PRIMARY)
    if reference_images:
        if single_branch:
            # canvas 留在 image 部件里不动；reference_images 只放真正的素材。
            # 上游要求 references 与 reference 部件一一对应，id 由 sidecar 生成。
            mode = "i2i"
        else:
            if image_png:
                reference_images = [(image_png, "canvas.png"), *reference_images]
                image_png = None
            mode = "t2i"

    # 浏览器出口路径的上传预算：sidecar 直连上游，不走
    # upstream.submit_generation 的 normalize，这里必须自己压一遍，
    # 否则超大图直接吃上游 413（原本能压到预算内正常出图）。
    sidecar_upload: tuple[bytes | None, list[tuple[bytes, str]]] | None = None

    def fit_upload() -> tuple[bytes | None, list[tuple[bytes, str]]]:
        nonlocal sidecar_upload
        if sidecar_upload is None:
            main_out, refs_out = payload.fit_body_budget(
                image_png or b"", [b for b, _ in (reference_images or [])])
            names = [n for _, n in (reference_images or [])]
            sidecar_upload = (
                main_out or None,
                [(b, n) for b, n in zip(refs_out, names)],
            )
        return sidecar_upload

    async def one() -> dict[str, str]:
        gen_resolution = bridging.ensure_resolution_capacity(
            aspect_ratio, "2k" if upscale_to else resolution)
        browser_media: bytes | None = None
        if _BROWSER_PRIMARY and browser_egress_client.configured():
            # 直连浏览器出口：跳过 curl_cffi（当前恒 403，且代理重试链要
            # 白耗 2-3 分钟）。生成图由 sidecar 直出，后处理复用原有分支。
            import uuid as _uuid
            poll_token = _uuid.uuid4().hex
            fit_main, fit_refs = fit_upload()
            log.info("browser-primary token=%s… (resolution=%s main=%dB refs=%d)",
                     poll_token[:10], gen_resolution, len(fit_main or b""),
                     len(fit_refs))
            # 不做任何自动降级：composition 分支已被实测证伪——它对 prompt 的
            # 名词覆盖极敏感，模糊指令下会把整幅重画成无关物体，却照样返回
            # 200 + 正常尺寸。那种"看起来成功、内容全错"的静默降级比直接报错
            # 危险得多（客户无法察觉）。失败就让错误上行。
            browser_media = await _browser_generate(
                image_png=fit_main or b"", prompt=prompt,
                aspect_ratio=aspect_ratio, resolution=gen_resolution, mode=mode,
                boxes_json=json.dumps(boxes) if boxes else None,
                refs=[b for b, _ in fit_refs],
                ref_mode="single" if single_branch else None)
            image_token = poll_token
        else:
            try:
                poll_token = await upstream.submit_generation(
                    image_png=image_png or b"",
                    prompt=prompt,
                    boxes=boxes,
                    aspect_ratio=aspect_ratio,
                    resolution=gen_resolution,
                    mode=mode,
                    base_url=UPSTREAM_BASE,
                    reference_images=reference_images or None,
                )
                log.info("submitted pollToken=%s… (resolution=%s)",
                         poll_token[:10], gen_resolution)
            except UpstreamError as submit_exc:
                # 上游按 HTTP/2 指纹拒绝 curl_cffi（403 Access denied）——只有
                # 真实浏览器环境能过。降级到 browser_egress sidecar。
                if (submit_exc.kind != "access_denied"
                        or not browser_egress_client.configured()):
                    raise
                import uuid as _uuid
                poll_token = _uuid.uuid4().hex
                fit_main, fit_refs = fit_upload()
                log.warning("fingerprint 403 from upstream; browser egress "
                            "fallback token=%s… (resolution=%s main=%dB refs=%d)",
                            poll_token[:10], gen_resolution,
                            len(fit_main or b""), len(fit_refs))
                browser_media = await _browser_generate(
                    image_png=fit_main or b"", prompt=prompt,
                    aspect_ratio=aspect_ratio, resolution=gen_resolution,
                    mode=mode,
                    boxes_json=json.dumps(boxes) if boxes else None,
                    refs=[b for b, _ in fit_refs],
                    ref_mode="single" if single_branch else None)
                image_token = poll_token
            else:
                image_token = await upstream.wait_for_result(
                    poll_token, UPSTREAM_BASE)
                log.info("ready imageToken=%s…", image_token[:10])
        if upscale_to:
            # Automatic super-resolution for >2k asks: the generated image
            # rides through our COS -- MPS's own fetcher gets 429'd by the
            # playground edge (measured), so the URL input path is not used.
            media = browser_media if browser_media is not None \
                else await upstream.fetch_media(image_token, UPSTREAM_BASE)
            object_key = f"upscale/{poll_token[:16]}.png"
            await mps.upload_to_cos(media, object_key)
            percent = max(1.0, min(10.0, upscale_to / GEN_LONG_EDGE))
            task_id = await mps.submit_cos(object_key, percent=percent)
            log.info("upscale task=%s percent=%.2f", task_id[:12], percent)
            sign_url = await mps.wait_done(task_id)
            if response_format == "url":
                return {"url": sign_url}  # managed-storage link, ~1h validity
            upscaled = await upstream.fetch_url(sign_url)
            return {"b64_json": base64.b64encode(upscaled).decode()}
        if response_format == "url":
            # 与异步门同策略：产物转存 COS 出 24h 预签名链。官方 media
            # token 分钟级过期，代理链接也随 token 一起死。
            if mps.configured():
                media = browser_media if browser_media is not None \
                    else await upstream.fetch_media(image_token, UPSTREAM_BASE)
                key = f"results/{image_token[:16]}.png"
                await mps.upload_to_cos(media, key)
                return {"url": mps.cos_presigned_get("/" + key, ttl_s=86400)}
            base = str(request.base_url).rstrip("/")
            return {"url": f"{base}/v1/images/results/{image_token}"}
        media = browser_media if browser_media is not None \
            else await upstream.fetch_media(image_token, UPSTREAM_BASE)
        return {"b64_json": base64.b64encode(media).decode()}

    try:
        if n == 1:
            data = [await one()]
        else:
            import asyncio as _aio

            results = await _aio.gather(*(one() for _ in range(n)), return_exceptions=True)
            data, first_err = [], None
            for r in results:
                if isinstance(r, Exception):
                    first_err = first_err or r
                else:
                    data.append(r)
            if not data and first_err is not None:
                raise first_err
    except UpstreamError as exc:
        # ── 原生日志（唯一允许出现上游原文的位置）─────────────────────────
        # 客户端侧一律只拿 _SAFE_ERRORS 固定文案；原文留在这里供事后归因。
        # 不要为了"统一脱敏"把这条也改成 _redact：那是排查上游契约变更的
        # 唯一线索。
        log.warning("run_job upstream error: %s", str(exc)[:300])
        if _MODERATION_DIAG and exc.kind == "content_policy":
            snippet = " ".join((prompt or "").split())[:100]
            log.warning(
                "MODERATION_DIAG prompt=%r res=%s has_image=%s refs=%d msg=%s",
                snippet, resolution, bool(image_png),
                len(reference_images or []), str(exc)[-80:])
        return wrap_upstream_error(exc)
    except browser_egress_client.BrowserEgressError as exc:
        # 同上：原生日志只落服务端（含 sidecar 原文与出口状态码）。
        log.warning("run_job browser egress error (status=%s): %s",
                    exc.status, str(exc)[:300])
        return wrap_browser_egress_error(exc)
    except mps.MPSError as exc:
        log.warning("run_job mps error: %s", str(exc)[:300])
        return openai_error(502, "super-resolution failed",
                            "api_error")

    return JSONResponse(
        {
            "created": int(time.time()),
            "data": data,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }
    )


async def run_job(
    image_png: bytes | None,
    prompt: str,
    boxes: list[dict],
    aspect_ratio: str,
    resolution: str,
    mode: str,
    n: int,
    response_format: str,
    request: Request,
    upscale_to: int | None = None,
    reference_images: list[tuple[bytes, str]] | None = None,
) -> JSONResponse:
    """幂等包装：同参数请求在窗口内只真正生成一次。

    指纹只取「影响出图结果」的参数，不含 request —— 同一个 prompt 从不同
    连接打进来（客户端重试）必须命中同一笔。
    """
    fp = job_fingerprint(
        image_png=image_png or b"",
        prompt=prompt,
        boxes=boxes,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        mode=mode,
        n=n,
        response_format=response_format,
        upscale_to=upscale_to or 0,
        reference_images=[[b, nm] for b, nm in (reference_images or [])],
    )
    return await _idem_run(
        fp,
        lambda: _run_job_impl(
            image_png=image_png,
            prompt=prompt,
            boxes=boxes,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            mode=mode,
            n=n,
            response_format=response_format,
            request=request,
            upscale_to=upscale_to,
            reference_images=reference_images,
        ),
    )


# ---------------------------------------------------------------- endpoints


@app.get("/healthz")
async def healthz() -> dict:
    out = {"ok": True, "model": MODEL_ID, "aliases": model_ids()[1:],
           "upstream": UPSTREAM_BASE}
    # 端到端探针摘要：`last_ok=false` + 小 `age_s` 才是真故障；
    # 读不到（Redis 抖动）不能让 healthz 500，所以整段吞异常。
    try:
        out["probe"] = await probe.summary()
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/v1/probe")
async def probe_endpoint(request: Request, run: int = 0):
    """探针状态；`?run=1` 立即跑一次（要 x-key，会创建一张真实上游任务）。"""
    native._check_x_key(request)
    if run:
        return await probe.run_once()
    return await probe.status()


@app.get("/v1/models")
async def list_models(request: Request):
    await check_auth(request)
    # 别名与规范名是同一个模型，用 OpenAI 的 `root` 字段把它们挂在一起：
    # 客户端（New API / Cherry Studio 等）拿 id 建下拉，拿 root 认亲。
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "owned_by": "bfl2openai",
             **({} if mid == MODEL_ID else {"root": MODEL_ID})}
            for mid in model_ids()
        ],
    }


@app.post("/v1/images/edits")
async def images_edits(
    request: Request,
    # 多图两种拼法都要接：OpenAI 官方 curl 示例用可重复的 `image[]`，而
    # openai-python SDK 用重复的 `image`（实测 2026-10-20：只认后者时
    # `image[]` 直接 422 missing image）。合并后 files[0]=画布，其余=参考图。
    image: list[UploadFile] | None = File(None),
    image_bracket: list[UploadFile] | None = File(None, alias="image[]"),
    prompt: str = Form(""),
    mask: UploadFile | None = File(None),
    n: int = Form(1),
    size: str | None = Form(None),
    response_format: Literal["url", "b64_json"] | None = Form(None),
    # ---- bfl2openai extensions (beyond the OpenAI spec) ----
    boxes: str | None = Form(None),          # JSON: [{"box":{"x","y","w","h"},"instruction"}]
    aspect_ratio: str | None = Form(None),   # "auto" | "1:1" | "16:9" | ...
    resolution: str | None = Form(None),     # 缺省时按 size 像素反推档位
    mode: str = Form("i2i"),
    model: str | None = Form(None),   # 仅用于别名识别，不影响上游选择
):
    await check_auth(request)
    route = model_route(model)
    if route["denied"]:
        return openai_error(
            404, f"The model '{route['requested']}' does not exist.",
            "model_not_found")
    if not prompt and not boxes:
        return openai_error(400, "prompt (or boxes) is required")

    files: list[UploadFile] = list(image or []) + list(image_bracket or [])
    if not files:
        return openai_error(400, "image is required")

    try:
        image_png = image_to_png(await files[0].read())
        box_list = parse_boxes(boxes)
    except Exception as exc:
        return openai_error(400, f"invalid image or boxes: {exc}")

    # 多垫图：files[1:] 转发为 references 文件部件（上游按部件+filename 配对，
    # 实测 2026-10-14）。单张失败丢弃、不阻塞主图。
    reference_images: list[tuple[bytes, str]] = []
    for idx, ref_file in enumerate(files[1:], start=2):
        try:
            reference_images.append(
                (image_to_png(await ref_file.read()),
                 ref_file.filename or f"ref_{idx}.png"))
        except Exception as exc:  # noqa: BLE001
            log.warning("edits reference #%d dropped: %s", idx, exc)

    if mask is not None:
        try:
            mask_boxes = mask_to_boxes(await mask.read())
        except Exception as exc:
            return openai_error(400, f"invalid mask: {exc}")
        for b in mask_boxes:
            if not b.get("instruction"):
                b["instruction"] = prompt
        box_list = box_list + mask_boxes

    ratio = aspect_ratio or size_to_aspect_ratio(size)
    # >2k asks run the composite: generate at the 2k ceiling, then
    # MPS-upscale. Two spellings arrive here: size "4096x4096" (long edge
    # beyond 2048) or the native resolution tier 4k/8k -- same treatment.
    long_side = _parse_long_side(size)
    # size 的像素写法决定档位（OpenAI 客户端按 size 预期输出像素，实测
    # "1024x1024" 曾在默认 1.5k 档拿到 1536^2）；比例写法不受影响，
    # 显式 resolution 永远优先。
    resolution = resolution or tier_for_long_edge(long_side) or "1.5k"
    upscale_to = long_side if long_side and long_side > GEN_LONG_EDGE else None
    if not upscale_to and resolution in ("4k", "8k"):
        upscale_to = GEN_LONG_EDGE * 2 if resolution == "4k" else GEN_LONG_EDGE * 4
    if upscale_to and not mps.configured():
        return openai_error(
            503,
            "an output beyond 2k requires MPS super-resolution "
            "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)",
        )
    if reference_images and box_list:
        # composition 是整图语义：第一张参考图被整体编辑，逐区域 boxes
        # 在上游无处落地（真发过去也是 400）。明确拒绝好过返回一张
        # "boxes 被静默忽略"的图。
        return openai_error(
            400,
            "region editing (boxes/mask) cannot be combined with reference "
            "images: composition mode edits the first reference as a whole")
    resp = await run_job(
        image_png=image_png,
        prompt=prompt,
        boxes=box_list,
        aspect_ratio=ratio,
        resolution=resolution,
        mode=mode,
        n=max(1, min(n, 4)),
        response_format=response_format or "b64_json",
        request=request,
        upscale_to=upscale_to,
        reference_images=reference_images or None,
    )
    return stamp_model(resp, route)


@app.post("/v1/images/generations")
async def images_generations(
    request: Request,
    payload: dict,
):
    """Text-to-image, or image-guided generation when `image` is supplied.

    `image` accepts a string or list of strings (base64 / data URI / URL /
    upload handle) -- list[0] becomes the canvas, the rest ride along as
    upstream reference parts (contract measured 2026-10-14).
    """
    await check_auth(request)
    route = model_route(payload.get("model"))
    if route["denied"]:
        return openai_error(
            404, f"The model '{route['requested']}' does not exist.",
            "model_not_found")
    prompt = str(payload.get("prompt") or "")
    if not prompt:
        return openai_error(400, "prompt is required")

    # 垫图（可选）：string | list[string] → 主图 + references
    image_field = payload.get("image")
    if isinstance(image_field, str):
        refs_in = [image_field]
    elif isinstance(image_field, (list, tuple)):
        refs_in = [str(x) for x in image_field]
    else:
        refs_in = []
    if len(refs_in) > native.MAX_IMAGES:
        return openai_error(400, f"at most {native.MAX_IMAGES} images")
    reference_images: list[tuple[bytes, str]] = []
    image_png: bytes | None = None
    for idx, ref in enumerate(refs_in):
        try:
            png = await native._reference_png(ref)
        except Exception as exc:  # noqa: BLE001
            if idx == 0:
                return openai_error(400, f"invalid image: {exc}")
            log.warning("generations reference #%d dropped: %s", idx + 1, exc)
            continue
        if idx == 0:
            image_png = png
        else:
            reference_images.append((png, f"ref_{idx + 1}.png"))
    n = int(payload.get("n") or 1)
    response_format = payload.get("response_format") or "b64_json"
    ratio = payload.get("aspect_ratio") or size_to_aspect_ratio(payload.get("size"))
    long_side = _parse_long_side(payload.get("size"))
    # size 像素写法决定档位（同 edits）；比例写法/未给 size 落默认档。
    resolution = str(payload.get("resolution") or "") \
        or tier_for_long_edge(long_side) or "1.5k"
    upscale_to = long_side if long_side and long_side > GEN_LONG_EDGE else None
    if not upscale_to and resolution in ("4k", "8k"):
        upscale_to = GEN_LONG_EDGE * 2 if resolution == "4k" else GEN_LONG_EDGE * 4
    if upscale_to and not mps.configured():
        return openai_error(
            503,
            "an output beyond 2k requires MPS super-resolution "
            "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)",
        )
    resp = await run_job(
        image_png=image_png,
        prompt=prompt,
        boxes=[],
        aspect_ratio=ratio,
        resolution=resolution,
        mode="i2i" if image_png else "t2i",
        n=max(1, min(n, 4)),
        response_format=response_format,
        request=request,
        upscale_to=upscale_to,
        reference_images=reference_images or None,
    )
    return stamp_model(resp, route)


@app.get("/v1/images/results/{image_token}")
async def image_result(image_token: str, request: Request):
    await check_auth(request)
    try:
        media = await upstream.fetch_media(image_token, UPSTREAM_BASE)
    except UpstreamError as exc:
        return wrap_upstream_error(exc)
    return Response(content=media, media_type="image/png")
