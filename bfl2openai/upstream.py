"""Upstream client for flux-tools.bfl.ai /api/flux3-image/* (reverse-engineered).

Contract (captured from the official playground via browser fetch hook):

1. POST /api/flux3-image/generate          multipart/form-data, anonymous
       mode         = "i2i"
       prompt       = <global instruction, may be empty when boxes carry instructions>
       aspectRatio  = "auto" | "<w>:<h>"
       resolution   = "1.5k"
       boxes        = JSON string: [{"id":"b-x","box":{"x":0.3,"y":0.3,"w":0.4,"h":0.4},
                                     "instruction":"...","source":"user"}]   (normalized 0..1)
       image        = PNG file (full current canvas)
       references   = JSON string (usually "[]")
   -> 200 {"status":"submitted","requestId":uuid,"pollToken":...,"prompt":...}

2. GET /api/flux3-image/status?pollToken=...
       -> {"status":"pending"}
       -> {"status":"ready","imageToken":...}
       -> {"status":"error","error":...}

3. GET /api/flux3-image/media?imageToken=...  -> image bytes (PNG)

Endpoints are anonymous; the site is fronted by a Vercel security checkpoint that
a chrome-impersonated TLS client passes (verified with curl_cffi).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from curl_cffi import CurlMime

from .payload import content_type_of, normalize_image
from curl_cffi.requests import AsyncSession

log = logging.getLogger("bfl2openai.upstream")

DEFAULT_BASE = "https://flux-tools.bfl.ai"
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0
SUBMIT_TIMEOUT_S = 120.0
MEDIA_TIMEOUT_S = 300.0


def _load_project_env() -> None:
    """Reads the deployment .env next to the package (missing file is fine).

    Runs at import so every module-level env read below (FALLBACK_PROXY
    included) sees the file. Real environment variables win: setdefault.
    """
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_project_env()

#: Optional fallback egress for resilient GETs (media / reference fetches).
#: When the direct attempts exhaust themselves on 429/403/503, the same GET
#: retries through this proxy (a rotating pool URL works: new exit IP + fresh
#: session). Empty = direct retries only. The pool's verified spelling is
#: socks5h (its HTTP entry is flaky; measured 2026-10-09):
#:   socks5h://<user>:<pass>@pool.livetest.cn:2088
FALLBACK_PROXY = os.environ.get("BFL2OPENAI_FALLBACK_PROXY", "")

#: 上游全局并发槽 = 50（共享 showcases key，503 原文实测 2026-10-15）。
#: gunicorn 3 worker x 16 = 48，留 2 槽余量给上游其他流量。
_SUBMIT_SEM = asyncio.Semaphore(
    int(os.environ.get("BFL2OPENAI_SUBMIT_CONCURRENCY", "16")))

_DIRECT_ATTEMPTS = 3
_PROXY_ATTEMPTS = 2
#: Transport-ish failures that a retry (or a different exit) can fix. A 400
#: "invalid token" is an answer, not an outage, and returns immediately.
_RETRY_STATUSES = frozenset({429, 403, 503})


class UpstreamError(Exception):
    """Upstream returned a business error."""

    def __init__(self, message: str, kind: str = "upstream_error"):
        super().__init__(message)
        self.message = message
        self.kind = kind


def _session_direct() -> AsyncSession:
    """强制直连（腾讯云内网：COS 上传/签名/CI 超分——不该绕代理）。"""
    return AsyncSession(impersonate="chrome", trust_env=False, timeout=SUBMIT_TIMEOUT_S)


def _session(timeout: float | None = None) -> AsyncSession:
    """默认会话：走池代理。

    实测 2026-10-15：服务器直连出口 IP 被上游持久限流（direct#1/2/3 全 429），
    所有 playground 流量（提交/轮询/媒体）改走池代理；池出口轮转跨多个 /24。
    impersonate chrome 过 Vercel checkpoint；trust_env=False 防 HTTP_PROXY 干扰。
    """
    if FALLBACK_PROXY:
        return AsyncSession(impersonate="chrome", trust_env=False,
                            proxy=FALLBACK_PROXY,
                            timeout=timeout or SUBMIT_TIMEOUT_S)
    return AsyncSession(impersonate="chrome", trust_env=False,
                        timeout=timeout or SUBMIT_TIMEOUT_S)


def _make_session(proxy: str | None = None, timeout: float | None = None) -> AsyncSession:
    """One attempt's session; a factory (not inline) so tests can fake it."""
    if proxy:
        return AsyncSession(impersonate="chrome", trust_env=False,
                            proxy=proxy, timeout=timeout or MEDIA_TIMEOUT_S)
    return _session()


async def _resilient_get(url: str, params: dict | None = None,
                         timeout: float = MEDIA_TIMEOUT_S,
                         label: str = "GET") -> bytes:
    """GET with retry + optional proxy fallback.

    Sequence: 3 direct attempts (impersonated session, 1s/3s backoff -- these
    429s are rate-limit flavoured and clear on their own), then, when
    FALLBACK_PROXY is configured, 2 attempts through the proxy (fresh session,
    different exit IP). Any other status returns immediately.
    """
    attempts: list[tuple[str, AsyncSession]] = [
        (f"proxy#{i + 1}", _make_session(proxy=FALLBACK_PROXY, timeout=timeout))
        for i in range(_PROXY_ATTEMPTS)
    ] if FALLBACK_PROXY else [
        (f"direct#{i + 1}", _session_direct()) for i in range(_DIRECT_ATTEMPTS)
    ]

    last = "no attempts"
    for index, (attempt_label, session) in enumerate(attempts):
        try:
            async with session as s:
                r = await s.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code not in _RETRY_STATUSES:
                raise UpstreamError(f"{label} failed: {last}")
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors retry
            last = f"{type(exc).__name__}: {exc}"
        log.warning("%s %s via %s failed (%s)", label, url[:80], attempt_label, last)
        if index < len(attempts) - 1:
            if index == _DIRECT_ATTEMPTS - 1:
                await asyncio.sleep(3.0)   # before the first proxy attempt
            else:
                await asyncio.sleep(1.0)
    raise UpstreamError(f"{label} failed after {len(attempts)} attempts: {last}")


def _browser_headers(base_url: str, referer_path: str = "/precise-editing") -> dict[str, str]:
    # POST /api/flux3-image/generate returns an empty 403 without these
    # same-origin markers; GET endpoints tolerate their absence.
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "origin": base_url,
        "referer": f"{base_url}{referer_path}",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def submit_generation(
    image_png: bytes,
    prompt: str,
    boxes: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    reference_images: list[tuple[bytes, str]] | None = None,
    extra_form_fields: dict[str, str] | None = None,
    aspect_ratio: str = "auto",
    resolution: str = "1.5k",
    mode: str = "i2i",
    base_url: str = DEFAULT_BASE,
) -> str:
    """Submit an edit job, return the pollToken."""
    fields: list[tuple[str, Any]] = [
        ("mode", mode),
        ("prompt", prompt),
        ("aspectRatio", aspect_ratio),
        ("resolution", resolution),
        ("boxes", json.dumps(boxes or [])),
        ("references", json.dumps(references or [])),
    ]
    for _k, _v in (extra_form_fields or {}).items():
        fields.append((_k, _v))
    if not image_png and aspect_ratio == "auto":
        # "auto" means "derive from the canvas" -- without one the upstream
        # demands an explicit ratio ("Choose an aspect ratio for the canvas.",
        # measured 2026-10-09). Square is the neutral t2i default (t2i
        # verified live the same day: 4k composite produced a real landscape).
        fields[2] = ("aspectRatio", "1:1")
    try:
        # 多参考图：联合预算规范化（主图 60% + 参考图均分），再以重复文件
        # 部件转发（契约实测 2026-10-14：按部件数量与 filename 配对）。
        reference_parts: list[tuple[bytes, str]] | None = None
        if image_png and not reference_images:
            # 单图路径：>4MB 规范化（PNG 降档 → JPEG 兜底），否则上游
            # Vercel 4.5MB body 上限直接 413（实测 2026-10-14/15）。
            image_png, _ct = normalize_image(image_png)
        if reference_images:
            from .payload import content_type_of, normalize_image_joint

            main_bytes, ref_bytes_list = normalize_image_joint(
                image_png, [b for b, _ in reference_images])
            image_png = main_bytes
            reference_parts = [(rb, name)
                               for rb, (_, name) in zip(ref_bytes_list,
                                                        reference_images)]
        async with _SUBMIT_SEM:   # 并发闸门：排队等槽，避免 503 槽满失败
            return await _submit_generation_impl(
                fields=fields, image_png=image_png, base_url=base_url,
                reference_parts=reference_parts)
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络/超时/上游 5xx 统一包装
        raise UpstreamError(f"generate submit failed: {type(exc).__name__}: {exc}") from exc


async def _submit_generation_impl(
    fields: list[tuple[str, Any]],
    image_png: bytes,
    base_url: str = DEFAULT_BASE,
    reference_parts: list[tuple[bytes, str]] | None = None,
) -> str:
    """提交尝试链：直连 x1 → 池代理 x3 → 等 15s → 池代理再一轮。

    直连的多个 attempt 是同一台机器的同一个出口 IP——429 后秒级重试同 IP
    必然再 429（实测 2026-10-15，还恶化限流窗口），所以直连只试一次；
    换 IP 靠代理池，配速靠轮间等待（限流窗口分钟级恢复）。
    每次尝试都是 chrome 指纹 + 新 mime；400 类参数错误立即抛（换 IP 无益）。
    """
    attempts: list[tuple[str, AsyncSession]] = [
        (f"proxy#{i + 1}",
         _make_session(proxy=FALLBACK_PROXY, timeout=SUBMIT_TIMEOUT_S))
        for i in range(max(2, _PROXY_ATTEMPTS))
    ] if FALLBACK_PROXY else [("direct#1", _session_direct())]
    # 全挂后的等待重试轮：再走一遍代理（新出口 + 限流窗口已滑过）
    retry_round = [
        (f"retry-proxy#{i + 1}",
         _make_session(proxy=FALLBACK_PROXY, timeout=SUBMIT_TIMEOUT_S))
        for i in range(2)
    ] if FALLBACK_PROXY else []
    retry_wait_s = float(os.environ.get("BFL2OPENAI_SUBMIT_RETRY_WAIT_S", "15"))

    last = "no attempts"
    rounds = [attempts] + ([retry_round] if retry_round else [])
    total = [a for rnd in rounds for a in rnd]
    index = -1
    for rnd_i, rnd in enumerate(rounds):
        if rnd_i > 0:
            import asyncio as _aio
            log.warning("submit: all %d attempts rate-limited; waiting %.0fs "
                        "before retry round", len(attempts), retry_wait_s)
            await _aio.sleep(retry_wait_s)
        for attempt_label, session in rnd:
            index += 1
        # mime 每次重建：CurlMime 的重放语义不可靠
        mp = CurlMime()
        for name, value in fields:
            mp.addpart(name=name, data=str(value).encode())
        if image_png:
            # An empty image part is NOT "no image": upstream answers
            # 400 "A composition must not include a canvas image." (measured
            # 2026-10-09 on a t2i attempt). Omit the part entirely instead.
            mp.addpart(name="image", filename="image.png",
                       content_type=content_type_of(image_png),
                       data=image_png)
        for ref_bytes, ref_name in (reference_parts or []):
            # 多参考图契约（实测 2026-10-14）：references 是重复的文件部件，
            # 每张自带 filename —— 服务端按部件数量与文件名配对。
            mp.addpart(name="references", filename=ref_name,
                       content_type=content_type_of(ref_bytes),
                       data=ref_bytes)
        try:
            async with session as s:
                r = await s.post(
                    f"{base_url}/api/flux3-image/generate",
                    multipart=mp,
                    headers=_browser_headers(base_url),
                )
            text = r.text
            if (r.status_code == 200 and "Security Checkpoint" not in text
                    and (data := r.json()).get("status") == "submitted"
                    and data.get("pollToken")):
                if index > 0:
                    log.info("submit succeeded via %s", attempt_label)
                return data["pollToken"]
            last = f"HTTP {r.status_code}: {text[:200]}"
            # 上游 HTTP/2 指纹级 bot 检测（2026-10-26）：换出口 IP 无效，
            # 立即抛 access_denied 交给浏览器出口兜底，不再浪费重试。
            if r.status_code == 403 and "access denied" in text.lower():
                raise UpstreamError(f"generate failed: {last}", "access_denied")
            retryable = (r.status_code in _RETRY_STATUSES
                         or "Security Checkpoint" in text)
        except Exception as exc:  # noqa: BLE001 - transport errors retry
            last = f"{type(exc).__name__}: {exc}"
            retryable = True
        log.warning("submit via %s failed (%s)", attempt_label, last)
        if not retryable:
            raise UpstreamError(f"generate failed: {last}")
    raise UpstreamError(
        f"generate submit failed after {len(total)} attempts: {last}")


async def poll_once(poll_token: str, base_url: str = DEFAULT_BASE) -> dict[str, Any]:
    async with _session(timeout=60) as s:
        r = await s.get(
            f"{base_url}/api/flux3-image/status",
            params={"pollToken": poll_token},
            timeout=60,
        )
    if r.status_code != 200:
        raise UpstreamError(f"status failed: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


_POLL_CACHE_INFLIGHT_S = 3.0    # 生成中：同 token 轮询合并窗口
_POLL_CACHE_TERMINAL_S = 120.0  # 终态：完成后重复轮询零上游请求
_POLL_CACHE_NEG_TTL_S = 7 * 24 * 3600  # 负缓存（审核拒绝/失效 token）= 7 天：
    # 永久终态不会自愈，长缓存彻底杜绝死 token 的重复上游请求


async def poll_once_cached(poll_token: str, base_url: str = DEFAULT_BASE) -> dict:
    """带共享缓存的轮询。

    多租户并发生成时，同一 pollToken 的密集轮询（服务端 3s 一次 x 全部
    并发任务）是 429 风暴的主源——同 token 的轮询在窗口内只透传上游一次，
    终态缓存 120s 让生成完成后的重复查询零上游流量。
    """
    from . import store

    cache_key = f"pollcache:{poll_token[:40]}"
    cached = await store.get(cache_key)
    if cached is not None:
        return cached
    try:
        state = await poll_once(poll_token, base_url)
    except UpstreamError as exc:
        # 负缓存：终态错误（内容审核/失效 token）不会自愈——不缓存的话，
        # 同一个死 token 的每次轮询都会白打上游一次 422。
        low = str(exc).lower()
        if any(k in low for k in ("moderation", "flagged",
                                  "invalid or expired")):
            # kind 必须在这里就定型：缓存重抛的异常不再经过 wait_for_result
            # 的分类分支（否则 wrap_upstream_error 又会错判成 502）。
            kind = ("invalid_request_error"
                    if "invalid or expired" in low else "content_policy")
            await store.put(cache_key, {"status": "error",
                                        "error": str(exc)[:300],
                                        "kind": kind},
                            ttl_s=int(_POLL_CACHE_NEG_TTL_S))
            raise UpstreamError(str(exc), kind) from exc
        raise
    ttl = int(_POLL_CACHE_TERMINAL_S if state.get("status") in ("ready", "error")
              else _POLL_CACHE_INFLIGHT_S)
    await store.put(cache_key, state, ttl_s=ttl)
    return state


async def wait_for_result(
    poll_token: str,
    base_url: str = DEFAULT_BASE,
    on_poll: Any = None,
) -> str:
    """Poll until ready; return imageToken. Raises UpstreamError on error/timeout."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await poll_once_cached(poll_token, base_url)
        status = last.get("status")
        if status == "ready":
            token = last.get("imageToken")
            if not token:
                raise UpstreamError(f"ready without imageToken: {last}")
            return token
        if status == "error":
            msg = last.get("error") or "unknown upstream error"
            low = str(msg).lower()
            if "moderation" in low or "flagged" in low:
                kind = "content_policy"      # 内容审核：客户端内容问题，非上游故障
            elif "token" in low:
                kind = "invalid_request_error"
            else:
                kind = "upstream_error"
            raise UpstreamError(str(msg), kind)
        if callable(on_poll):
            on_poll(last)
        await asyncio.sleep(POLL_INTERVAL_S)
    raise UpstreamError(f"timed out after {POLL_TIMEOUT_S:.0f}s polling {poll_token[:8]}… (last={last})")


async def fetch_media(image_token: str, base_url: str = DEFAULT_BASE) -> bytes:
    """Fetch the generated image, with retry + proxy fallback on 429/403/503."""
    return await _resilient_get(
        f"{base_url}/api/flux3-image/media",
        params={"imageToken": image_token},
        label="media",
    )


async def fetch_url(url: str, timeout: float = MEDIA_TIMEOUT_S) -> bytes:
    """GET an absolute URL through the impersonated session (reference images)."""
    return await _resilient_get(url, label="fetch")


def new_box_id() -> str:
    return f"b-{uuid.uuid4().hex[:12]}"
