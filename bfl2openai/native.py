"""BFL official native API (docs.bfl.ai) endpoints over the playground upstream.

POST /v1/flux-3-image          official native generate/edit submit
GET  /v1/get_result?id=        official native polling
GET  /v1/flux3-image/results/{imageToken}   the URL `result.sample` points at

Contract (api.bfl.ai/openapi.json, fetched 2026-10-09 -- see
analysis/bfl_openapi.json):

  request   application/json
    prompt             string, required
    images             string | [string] (http(s) URL or base64), 1..10
    width / height     exact pixels, [256, 6256]; <=16.8 MP for t2i and
                       <=4 MP with reference images; set together and
                       override resolution + aspect_ratio (openapi 2026-10-22)
    aspect_ratio       default "auto"
    resolution         enum 768sq | 1k | 1.5k | 2k | 4k, default "1k"
    safety_tolerance   int 0..4 (default 2)      -- accepted, dropped
    grounding          bool (default true)       -- accepted, dropped
    version            "latest"                  -- accepted, dropped
    bounding_boxes     NOT in the official spec: a convenience extension of
                       this adapter, [{"id","bbox":[x1,y1,x2,y2],"desc"}]
                       in 0..1000 coordinates (the docs' own box spelling),
                       mapped onto the playground's region form field.

  submit reply   AsyncResponse  {"id", "polling_url", "cost", "input_mp"}
  poll reply     {"id", "status": "Pending|Reasoning|Generating|Ready|Error|
                 "Request Moderated"|"Content Moderated"|..., "result",
                 "progress", "details"}; on Ready, result.sample is a URL.

Bounding boxes / layout (docs pages flux3_image_layout, flux3_image_bounding_boxes)
are PROMPT conventions, not API fields: the caller embeds
`{"id","bbox":[x1,y1,x2,y2],"desc"}` rows (0..1000 coordinates) as JSON inside
`prompt`, with `<id>` placeholders naming each region and `<ref_image_N>`
naming reference images. Those prompts ride through untouched -- the model
consumes them directly. The structured `bounding_boxes` extension above is
the one path that reaches the playground's own region channel (per-region
instructions instead of prompt text).

Verified against the playground upstream (2026-10-09): tiers 768sq=768^2,
1k=1024^2, 1.5k=1536^2, 2k=2048^2; `4k` is refused anonymously with
403 "4K resolution is available to BFL accounts only." Tiers other than
1.5k were measured through this adapter's own submit path (i2i, 256px input,
aspect auto).

Known bounds:
  * `images` without http(s)/data/base64 spellings is refused locally.
  * text-to-image (no `images`) submits with mode="t2i" and no file part --
    the playground UI never exercises that mode, so it is UNVERIFIED; an
    upstream refusal surfaces verbatim.
  * webhooks are not supported (poll only); `safety_tolerance`, `grounding`
    and `version` are accepted for drop-in compatibility but have no
    playground counterpart.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

from . import store
from . import upstream
from . import upscale as mps
from . import bridging
from . import browser_egress_client
from . import payload as payload_mod
from .payload import normalize_image
from . import uploads as uploads_mod

log = logging.getLogger("bfl2openai.native")

#: 上游错误原文只进日志，对外一律通用文案。原文会泄漏 CF ray id（`sin1::…`，
#: 暴露出口机房区域）、上游 Zod 枚举、内部拓扑与代理 URL（2026-11-17 加固）。
_TIMEOUT_MSG = "upstream temporarily unavailable"
_RAY_RE = re.compile(r"\b[a-z]{3}\d{1,2}::[0-9A-Za-z_-]+")
_SECRET_RE = re.compile(r"(?:socks5h?|https?|wss?)://[^@\s/]+@")


def _redact_upstream(text: object, limit: int = 400) -> str:
    s = _SECRET_RE.sub("<redacted>://", str(text))
    s = _RAY_RE.sub("<ray>", s)
    return s[:limit]


def _upstream_502(exc: object) -> JSONResponse:
    """上游失败对外只回通用文案；细节打日志（脱敏后）。"""
    log.warning("upstream failure: %s", _redact_upstream(exc))
    return JSONResponse(status_code=502, content={"detail": _TIMEOUT_MSG})

PUBLIC_BASE = os.environ.get("BFL2OPENAI_PUBLIC_BASE", "").rstrip("/")

NATIVE_API_KEY = os.environ.get("BFL_NATIVE_API_KEY", "")  # optional enforcement
RESOLUTIONS = ("768sq", "1k", "1.5k", "2k", "4k")
MAX_IMAGES = 10

#: Adapter extensions beyond the official spec: anonymous generation tops out
#: at 2k (4k is refused upstream without an account), so 4k/8k run as a
#: composite -- generate at 2k, then MPS super-resolution x2 / x4. Keyed by
#: poll token; advanced by get_result calls (client-driven state machine).
COMPOSITE_TIERS = {"4k": 2.0, "8k": 4.0}
# composite 态外置（store）：多 worker 下受理与轮询可能落在不同进程
_COMP_TTL = 3600

#: 原生端点的「异步桥」：submit 立刻回 id，生成在后台任务里跑浏览器出口
#: sidecar，get_result 读任务状态。上游 2026-10-26 起封 curl_cffi 指纹，
#: 原生方言直连必 502（矩阵测试实测）——sidecar 在，桥就是唯一的活路；
#: sidecar 不在（本地开发）才回落原有直连路径。
_BRIDGE_TTL_RUNNING = 1800
_BRIDGE_TTL_DONE = 3600
#: 墓碑：状态过期后仍能由 id 解析出产物链接。必须与 COS 预签名 TTL
#: (86400) 对齐——bridge:{id} 只活 1h，而图能活 24h，中间的 23h 里
#: 用 id 查图会掉进 get_result 的上游兜底分支（把我们的 uuid 当上游
#: pollToken 打过去）→ 上游 400 → 502（2026-10-27 实测复现）。
_BRIDGE_TOMBSTONE_TTL = 86400
_BRIDGE_TASKS: set = set()  # 持强引用防 GC（asyncio 只存弱引用）

#: 桥任务看门狗：后台 task 只活在 gunicorn worker 内存里，容器重建 / worker
#: 被 kill / sidecar 页面池空转时它会直接消失，Redis 里的 bridge:{id} 却
#: 永远停在 generating，客户端会无限轮询（2026-11-05 生产实测三个任务卡死）。
#: 超过这个秒数仍无终态就明确回 Error，让调用方放弃而不是空等。
_BRIDGE_STALE_S = float(os.environ.get("BFL2OPENAI_BRIDGE_STALE_S", "300"))


def _check_x_key(request: Request) -> None:
    """Honours the official `x-key` header; enforces only when configured."""
    if not NATIVE_API_KEY:
        return
    if request.headers.get("x-key") != NATIVE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-key")


def _png_bytes(ctx_image_data: bytes, source: str) -> bytes:
    """Normalize a reference image to PNG (the playground's file spelling).

    解码失败统一抛 ValueError：PIL 的 UnidentifiedImageError 继承 OSError
    （MRO: UnidentifiedImageError -> OSError -> Exception），**不是**
    ValueError，漏出去会穿过所有 `except (ValueError, ...)` 变成 500 纯文本
    （2026-10-27 实测：坏 base64 图 -> "Internal Server Error" 21 字节）。
    调用方按 ValueError 映射 422 / 丢弃参考图。
    """
    try:
        img = Image.open(io.BytesIO(ctx_image_data))
        fmt = img.format
        if fmt == "PNG":
            return ctx_image_data
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"undecodable image ({source}): {type(exc).__name__}: {exc}") from exc
    log.info("converted %s input %s to png", fmt, source)
    return buf.getvalue()


async def _reference_png(ref: str) -> bytes:
    """One official `images` entry (URL, data URI or base64) as PNG bytes."""
    if ref.startswith("data:"):
        _, _, payload = ref.partition(",")
        try:
            raw = base64.b64decode(payload)
        except (binascii.Error, ValueError):
            raise ValueError("images data URI payload is not valid base64")
    elif ref.startswith(("http://", "https://")):
        raw = await upstream.fetch_url(ref)
    else:
        try:
            raw = base64.b64decode(ref, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(
                "images entries must be http(s) URLs, data URIs or base64 "
                f"(got scheme {ref.split(':', 1)[0][:16]!r})"
            )
    return _png_bytes(raw, ref[:60])


def _boxes_native(rows) -> list[dict]:
    """Official 0..1000 boxes -> the playground's normalized region form."""
    out = []
    for item in rows:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("each bounding_boxes entry needs bbox:[x1,y1,x2,y2]")
        x1, y1, x2, y2 = (float(v) for v in bbox)
        out.append(
            {
                "id": item.get("id") or upstream.new_box_id(),
                "box": {
                    "x": min(1.0, max(0.0, x1 / 1000.0)),
                    "y": min(1.0, max(0.0, y1 / 1000.0)),
                    "w": min(1.0, max(0.0, (x2 - x1) / 1000.0)),
                    "h": min(1.0, max(0.0, (y2 - y1) / 1000.0)),
                },
                "instruction": str(item.get("desc", "")),
                "source": "user",
            }
        )
    return out


# ---- exact size (width/height) -------------------------------------------
#: docs.bfl.ai `/v1/flux-3-image` (openapi 2026-11-27): `width`/`height` are
#: exact pixels in [256, 6256]; the pixel budget is 16.8 MP for text-to-image
#: and 4 MP once reference images are attached. Both override `resolution`
#: and `aspect_ratio`.
#:
#: The playground upstream we reverse-engineered (2026-10-09) exposes only
#: tier + ratio. So an exact size is honoured as:
#:   tier  = smallest playground tier whose long edge covers max(w, h)
#:   ratio = nearest legal enum
#:   bytes = cover-crop to w:h, then LANCZOS to exactly w x h
#: That keeps the API contract ("you get the pixels you asked for") without
#: inventing an upstream knob that does not exist. Sizes the tier already
#: produces bit-for-bit (2048x2048 on 2k) skip the re-encode entirely.
EXACT_MIN_SIDE = 256
EXACT_MAX_SIDE = 6256
EXACT_MAX_PIXELS = 16_822_272
EXACT_MAX_PIXELS_REFS = 4_194_304

#: Long-edge ceiling of each anonymous tier (square-equivalent).
_TIER_EDGES = (("768sq", 768), ("1k", 1024), ("1.5k", 1536), ("2k", 2048))


#: Slot-aware generator injected by server.py at import time (it owns the
#: 2090 pool, lease ledger and 403/429 rotation). Falls back to the plain
#: sidecar client when native runs standalone (unit tests / local dev).
_BROWSER_GENERATE = None


def set_browser_generate(fn) -> None:
    """Wire the pool-aware generator without a native -> server import cycle."""
    global _BROWSER_GENERATE
    _BROWSER_GENERATE = fn


class _SizeError(ValueError):
    """A width/height pair that violates the official contract."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _as_pixels(value: object) -> int | None:
    """JSON number -> int, rejecting bools and fractional values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _parse_exact_size(payload: dict, has_refs: bool) -> tuple[int, int] | None:
    """Validate the official width/height pair; None when neither is sent.

    Raises ``_SizeError`` for half a pair, non-integers, out-of-range sides
    and over-budget areas. Budgets are the documented ones: 16.8 MP for
    text-to-image, 4 MP when reference images ride along.
    """
    raw_w, raw_h = payload.get("width"), payload.get("height")
    if raw_w is None and raw_h is None:
        return None
    if raw_w is None or raw_h is None:
        raise _SizeError("width", "width and height must be set together")
    width, height = _as_pixels(raw_w), _as_pixels(raw_h)
    for field, value in (("width", width), ("height", height)):
        if value is None:
            raise _SizeError(field, f"{field} must be an integer")
        if not EXACT_MIN_SIDE <= value <= EXACT_MAX_SIDE:
            raise _SizeError(
                field,
                f"{field} must be between {EXACT_MIN_SIDE} and {EXACT_MAX_SIDE}")
    budget = EXACT_MAX_PIXELS_REFS if has_refs else EXACT_MAX_PIXELS
    if width * height > budget:
        suffix = " with reference images" if has_refs else ""
        raise _SizeError(
            "width",
            f"width x height must be at most {budget} pixels{suffix}")
    return width, height


def _aspect_for_size(width: int, height: int) -> str:
    """Nearest legal upstream ratio; exact pixel error is absorbed later."""
    return bridging.nearest_ratio(width / height)


def _tier_for_exact(width: int, height: int) -> tuple[str, int | None]:
    """Smallest tier covering the requested long edge; composite beyond 2k."""
    edge = max(width, height)
    for name, cap in _TIER_EDGES:
        if edge <= cap:
            return name, None
    # Beyond the anonymous 2k ceiling: generate at 2k, then MPS x2/x4.
    return ("4k", 2) if edge <= 3840 else ("8k", 4)


def _fit_exact(media: bytes, width: int, height: int) -> bytes:
    """Return PNG bytes at exactly ``width x height``.

    Cover-crop to the target ratio first (a 2:1 target served by a 1:1 tier
    must lose top/bottom, not squash), then LANCZOS to the exact size. A
    no-op when the tier already produced the requested pixels, so the common
    2048x2048 case keeps the upstream bytes untouched.
    """
    try:
        im = Image.open(io.BytesIO(media))
        im.load()
    except Exception as exc:  # noqa: BLE001 - never fail a paid generation here
        log.warning("exact-size fit skipped (undecodable %d bytes): %s",
                    len(media), exc)
        return media
    if im.size == (width, height):
        return media
    target = width / height
    current = im.width / im.height
    if abs(current - target) > 1e-6:
        if current > target:
            keep = max(1, round(im.height * target))
            left = (im.width - keep) // 2
            im = im.crop((left, 0, left + keep, im.height))
        else:
            keep = max(1, round(im.width / target))
            top = (im.height - keep) // 2
            im = im.crop((0, top, im.width, top + keep))
    if im.size != (width, height):
        im = im.resize((width, height), Image.LANCZOS)
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _fetch_url_bytes(url: str, timeout: float = 120.0) -> bytes:
    """Blocking download for COS signed URLs (call via asyncio.to_thread)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


async def _bridge_upscale(token: str, media: bytes, magnify: int,
                          target_size: tuple[int, int] | None = None,
                          ) -> tuple[bytes | None, str]:
    """4k/8k 复合的超分段：CI 同步优先，MPS 异步任务兜底（同 _composite_step）。"""
    # COMPOSITE_TIERS 的值是 float（2.0 / 4.0），直接拼进 URL 会变成
    # `magnify=2.0`，数据万象判 InvalidArgument "param magnify invalid"
    # ⇒ CI 同步超分 100% 失败，每次都退化到慢得多的 MPS 异步任务
    # （2026-10-27 生产日志实测）。server._composite_step 有 int()，
    # 桥这条路径漏了。
    magnify = int(magnify)
    ikey = f"upscale/{token[:16]}.png"
    await mps.upload_to_cos(media, ikey)
    try:
        up = await mps.ci_super_resolve_cos(ikey, magnify=magnify, data=media)
        if target_size:
            # CI 输出被裁到精确尺寸后一律是 PNG，key 必须跟着变。
            up = await asyncio.to_thread(_fit_exact, up, *target_size)
            okey = f"results/{token[:16]}-{target_size[0]}x{target_size[1]}.png"
        else:
            # CI 输出格式跟随输入（超预算的 2k 输入必被压成 JPEG），key 必须
            # 与内容一致，否则 COS 按扩展名回 image/png 而体是 JPEG。
            okey = f"results/{token[:16]}.{mps.image_ext(up)}"
        await mps.upload_to_cos(up, okey)
        return up, mps.cos_presigned_get("/" + okey, ttl_s=86400)
    except mps.MPSError as exc:
        log.warning("bridge CI upscale failed, MPS task fallback: %s", exc)
    task_id = await mps.submit_cos(ikey, percent=float(magnify))
    url = await mps.wait_done(task_id)
    if target_size:
        # 异步 MPS 只回 COS 链接；精确尺寸必须落地成本地字节再转存。
        try:
            raw = await asyncio.to_thread(_fetch_url_bytes, url)
        except Exception as exc:  # noqa: BLE001 - 保留可用的放大结果
            log.warning("exact-size fit skipped for MPS url: %s", exc)
            return None, url
        fitted = await asyncio.to_thread(_fit_exact, raw, *target_size)
        okey = f"results/{token[:16]}-{target_size[0]}x{target_size[1]}.png"
        await mps.upload_to_cos(fitted, okey)
        return fitted, mps.cos_presigned_get("/" + okey, ttl_s=86400)
    return None, url


async def _bridge_run(token: str, image_png: bytes, prompt: str, boxes: list[dict],
                      aspect_ratio: str, resolution: str, mode: str,
                      reference_images: list[tuple[bytes, str]] | None,
                      magnify: int | None, proxy_sample: str,
                      target_size: tuple[int, int] | None = None) -> None:
    """后台跑 sidecar 生成，终态写进 bridge:{token}（get_result 消费）。

    多图归一化与 server.run_job 一致：canvas 与 references 互斥（上游
    composition 语义），带参考图时 canvas 并入首位、mode 置 t2i。
    """
    key = f"bridge:{token}"
    try:
        if reference_images and image_png:
            reference_images = [(image_png, "canvas.png"), *reference_images]
            image_png = b""
            mode = "t2i"
        gen_resolution = bridging.ensure_resolution_capacity(aspect_ratio, resolution)
        # 同一套上传预算（桥也直连 sidecar，不走 upstream 的 normalize）
        fit_main, fit_refs = payload_mod.fit_body_budget(
            image_png or b"", [b for b, _ in (reference_images or [])])
        # server.py injects its pool-aware wrapper: native requests must lease
        # a 2090 slot like every other door, otherwise they are stuck on the
        # 32 sidecar pages and burn them (429) while 410 slots sit idle
        # (2026-11-27: three 2048 probes all hit "today's 30 free generations").
        generate = _BROWSER_GENERATE or browser_egress_client.generate
        media = await generate(
            image_png=fit_main, prompt=prompt, aspect_ratio=aspect_ratio,
            resolution=gen_resolution, mode=mode,
            boxes_json=json.dumps(boxes) if boxes else None,
            refs=fit_refs)
        sample: str | None = None
        if magnify:
            media, sample = await _bridge_upscale(token, media, magnify,
                                                  target_size)
        if target_size and media is not None and sample is None:
            media = await asyncio.to_thread(_fit_exact, media, *target_size)
        if sample is None:
            if mps.configured():
                okey = f"results/{token[:16]}.png"
                await mps.upload_to_cos(media, okey)
                sample = mps.cos_presigned_get("/" + okey, ttl_s=86400)
            else:
                # 无 COS：字节暂存 store，代理端点按 token 取
                await store.put_blob(f"bridge_bytes:{token}", media, ttl_s=3600)
                sample = proxy_sample
        await store.put(key, {"status": "ready", "sample": sample},
                        ttl_s=_BRIDGE_TTL_DONE)
        await store.put(f"bridge_done:{token}", {"sample": sample},
                        ttl_s=_BRIDGE_TOMBSTONE_TTL)
        log.info("native bridge done token=%s %d bytes", token[:16], len(media or b""))
    except browser_egress_client.BrowserEgressError as exc:
        await store.put(key, {"status": "error", "error": str(exc)[:300]},
                        ttl_s=_BRIDGE_TTL_DONE)
        log.warning("native bridge failed token=%s status=%s", token[:16], exc.status)
    except Exception as exc:  # noqa: BLE001
        await store.put(key, {"status": "error", "error": f"bridge: {exc}"[:300]},
                        ttl_s=_BRIDGE_TTL_DONE)
        log.exception("native bridge crashed token=%s", token[:16])


def register(app: FastAPI) -> None:
    @app.post("/v1/flux-3-image")
    async def flux_3_image(request: Request, payload: dict):
        _check_x_key(request)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "prompt"],
                                     "msg": "Field required"}]},
            )
        images = payload.get("images")
        if images is None:
            refs = []
        elif isinstance(images, str):
            refs = [images]
        elif isinstance(images, list):
            refs = images
        else:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "images"],
                                     "msg": "string or array of strings"}]},
            )
        if len(refs) > MAX_IMAGES:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "images"],
                                     "msg": f"at most {MAX_IMAGES} images"}]},
            )

        # `@image#N:name` handles from /v1/uploads: rewritten into the BFL
        # `<ref_image_K>` placeholders (layout convention) and appended to
        # images after any caller-supplied ones.
        try:
            prompt, refs = await uploads_mod.resolve_handles_async(prompt, refs or None)
            refs = refs or []
        except HTTPException:
            raise
        if len(refs) > MAX_IMAGES:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "images"],
                                     "msg": f"at most {MAX_IMAGES} images "
                                            f"(uploads included)"}]},
            )

        # width/height (official contract since 2026-10-22) override
        # resolution and aspect_ratio. Validate AFTER upload handles so the
        # 4 MP reference budget counts every image that will ride upstream.
        try:
            target_size = _parse_exact_size(payload, has_refs=bool(refs))
        except _SizeError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", exc.field],
                                     "msg": exc.message}]},
            )
        if target_size:
            aspect_ratio = _aspect_for_size(*target_size)
            resolution, magnify = _tier_for_exact(*target_size)
            if magnify and not mps.configured():
                # Same guard as resolution 4k/8k: reject before any generation.
                return JSONResponse(
                    status_code=503,
                    content={"detail":
                             f"exact size {target_size[0]}x{target_size[1]} "
                             "exceeds the 2k anonymous ceiling and requires MPS "
                             "super-resolution (MPS_SECRET_ID / "
                             "MPS_SECRET_KEY not configured)"},
                )
        else:
            resolution = payload.get("resolution") or "1k"
            if resolution not in RESOLUTIONS and resolution not in COMPOSITE_TIERS:
                return JSONResponse(
                    status_code=422,
                    content={"detail": [{"loc": ["body", "resolution"],
                                         "msg": f"must be one of {sorted(set(RESOLUTIONS) | set(COMPOSITE_TIERS))}"}]},
                )
            if resolution in COMPOSITE_TIERS and not mps.configured():
                # Guard BEFORE any reference processing or submission: 4k/8k
                # are composites (2k generation + MPS x2/x4); without MPS they
                # would otherwise spend a generation and fail afterwards.
                return JSONResponse(
                    status_code=503,
                    content={"detail":
                             "resolution 4k/8k requires MPS super-resolution "
                             "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)"},
                )
            magnify = COMPOSITE_TIERS.get(resolution)
            aspect_ratio = str(payload.get("aspect_ratio") or "auto")

        boxes_raw = payload.get("bounding_boxes")
        try:
            boxes = _boxes_native(boxes_raw) if boxes_raw else []
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "bounding_boxes"],
                                     "msg": str(exc)}]},
            )

        try:
            png = await _reference_png(refs[0]) if refs else None
        except (ValueError, OSError, upstream.UpstreamError) as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "images"],
                                     "msg": str(exc)}]},
            )

        # 多参考图：refs[1:] 转发为 references 文件部件（上游按部件+filename
        # 配对，实测 2026-10-14）。逐张下载失败不阻塞主图——丢弃并记日志。
        reference_images = []
        for idx, ref in enumerate(refs[1:], start=2):
            try:
                ref_png = await _reference_png(ref)
                ref_name = f"ref_{idx}.png"
                m = re.match(r"@image#\d+:(.+)", str(ref))
                if m:
                    ref_name = m.group(1)
                reference_images.append((ref_png, ref_name))
            except (ValueError, OSError, upstream.UpstreamError) as exc:
                log.warning("reference #%d dropped (fetch failed): %s", idx, exc)

        if browser_egress_client.configured():
            # 异步桥：受理即回 id，生成在后台跑 sidecar（上游封 curl_cffi
            # 指纹后，直连 submit 恒 403 => 502；桥让原生方言恢复可用）。
            token = uuid.uuid4().hex
            await store.put(f"bridge:{token}",
                            {"status": "generating", "started_at": time.time()},
                            ttl_s=_BRIDGE_TTL_RUNNING)
            proxy_base = PUBLIC_BASE or str(request.base_url).rstrip("/")
            task = asyncio.create_task(_bridge_run(
                token, png or b"", prompt, boxes,
                aspect_ratio,
                "2k" if resolution in COMPOSITE_TIERS else resolution,
                "i2i" if png else "t2i",
                reference_images or None,
                magnify,
                f"{proxy_base}/v1/flux3-image/results/{token}",
                target_size))
            _BRIDGE_TASKS.add(task)
            task.add_done_callback(_BRIDGE_TASKS.discard)
            # 🔴 必须用 PUBLIC_BASE：nginx 反代下 request.base_url 是 http，
            # 回给客户端的 polling_url 会在 :80 吃 301（官方客户端不一定跟随
            # 重定向，2026-11-05 生产实测连吃 15 次 301 轮询不到终态）。
            base = PUBLIC_BASE or str(request.base_url).rstrip("/")
            return JSONResponse(
                {
                    "id": token,
                    "polling_url": f"{base}/v1/get_result?id={token}",
                    "cost": None,
                    "input_mp": None,
                }
            )
        try:
            if png:
                # 4k/8k composite: generate at the 2k anonymous ceiling, then
                # upscale x2/x4 via MPS (see COMPOSITE_TIERS). The MPS guard
                # ran above, before anything was spent.
                gen_resolution = "2k" if resolution in COMPOSITE_TIERS else resolution
                poll_token = await upstream.submit_generation(
                    image_png=png,
                    prompt=prompt,
                    boxes=boxes,
                    aspect_ratio=aspect_ratio,
                    resolution=gen_resolution,
                    mode="i2i",
                    reference_images=reference_images or None,
                )
                if resolution in COMPOSITE_TIERS:
                    await store.put(f"comp:{poll_token}", {
                        "phase": "gen", "target": resolution,
                        "target_size": list(target_size) if target_size else None,
                    }, ttl_s=_COMP_TTL)
                elif target_size:
                    # No sidecar: the tier output is persisted in get_result,
                    # which is where the exact-size fit happens.
                    await store.put(f"exact:{poll_token}",
                                    {"w": target_size[0], "h": target_size[1]},
                                    ttl_s=_COMP_TTL)
            else:
                # text-to-image: verified 2026-10-09 (mode t2i, no image part,
                # explicit aspect ratio -- "auto" is canvas-only semantics).
                # 4k/8k 复合同 i2i：2k 生成 + MPS 超分。上游 2026-10-14 起对
                # 匿名直出 4k 报 403 "4K resolution is available to BFL
                # accounts only"，t2i 分支此前漏降档已补（full_matrix 抓到）。
                poll_token = await upstream.submit_generation(
                    image_png=b"",
                    prompt=prompt,
                    boxes=boxes,
                    aspect_ratio=aspect_ratio,
                    resolution="2k" if resolution in COMPOSITE_TIERS else resolution,
                    mode="t2i",
                )
                if resolution in COMPOSITE_TIERS:
                    await store.put(f"comp:{poll_token}", {
                        "phase": "gen", "target": resolution,
                        "target_size": list(target_size) if target_size else None,
                    }, ttl_s=_COMP_TTL)
                elif target_size:
                    await store.put(f"exact:{poll_token}",
                                    {"w": target_size[0], "h": target_size[1]},
                                    ttl_s=_COMP_TTL)
        except upstream.UpstreamError as exc:
            return _upstream_502(exc.message)

        base = PUBLIC_BASE or str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                "id": poll_token,
                "polling_url": f"{base}/v1/get_result?id={poll_token}",
                "cost": None,
                "input_mp": None,
            }
        )

    @app.get("/v1/get_result")
    async def get_result(request: Request, id: str):
        # 🔴 故意不校验 x-key：polling_url 是「能力 URL」(capability URL)。
        # 官方 BFL 协议里 submit 带 x-key，但客户端拿到 polling_url 之后是
        # 裸 GET，不会再带任何自定义头 —— 这里要求 x-key 会让官方 SDK /
        # new-api 渠道的每一次轮询都 401（2026-11-05 生产实测：无头
        # 401 {"detail":"Invalid x-key"}；2026-10-27 线上日志 401×25）。
        #
        # 安全模型：id 本身就是凭证。桥路径为 uuid4 hex（128 bit 不可枚举），
        # 直连路径为上游 pollToken（高熵）。且产物端点
        # /v1/flux3-image/results/{token} 与 COS 预签名 sample 本来就不鉴权，
        # 在这里加 x-key 保护不了任何资源，只会挡住状态查询。
        # 真正需要保护的「创建生成任务」仍由 /v1/flux-3-image 的 x-key 兜住。
        # 三个状态键一次 MGET：高频轮询（客户端每 2-3s 一次）下把每轮
        # 3 次 Redis RTT 压到 1 次。桥路径全程零上游请求，状态只在 Redis。
        composite, bridge, tomb = await store.mget(
            [f"comp:{id}", f"bridge:{id}", f"bridge_done:{id}"])
        if composite:
            return await _composite_step(request, id, composite)

        # 异步桥任务：生成跑在 sidecar 后台任务里，这里只读状态（零上游请求）
        if bridge:
            out = {"id": id, "progress": None, "details": None, "preview": None}
            if bridge.get("status") == "ready":
                out["status"] = "Ready"
                out["result"] = {"sample": bridge["sample"]}
            elif bridge.get("status") == "error":
                out["status"] = "Error"
                out["details"] = {"error": bridge.get("error") or "unknown"}
            elif time.time() - bridge.get("started_at", 0) > _BRIDGE_STALE_S:
                # 旧记录没有 started_at 也按过期处理：直接清掉历史僵尸 key。
                out["status"] = "Error"
                out["details"] = {
                    "error": "generation timed out (worker lost or egress unavailable)"}
            else:
                out["status"] = "Generating"
            return JSONResponse(out)

        # 墓碑：状态记录已过期，但产物还在 COS（24h）。没有这个分支，
        # 1h-24h 之间的查询会掉到最下面的上游兜底，把我们的 uuid 当成
        # 上游 pollToken 发一次真实请求，然后 502。
        if tomb:
            return JSONResponse({
                "id": id, "progress": None, "details": None, "preview": None,
                "status": "Ready", "result": {"sample": tomb["sample"]}})

        # 轮询合并缓存：多租户并发下，同一 token 的密集轮询（客户每 2-3s
        # 一次）只透传上游一次。生成中缓存 3s；终态缓存 120s（生成完成后
        # 的重复轮询全部零上游请求——429 风暴的主源就是这里的 1:1 透传）。
        cache_key = f"pollcache:{id}"
        state = await store.get(cache_key)
        if state is None:
            try:
                state = await upstream.poll_once(id)
            except upstream.UpstreamError as exc:
                if "invalid or expired" in str(exc).lower():
                    # 不是上游故障，是"查不到这个任务"（我们的 uuid 过期 /
                    # 客户端编造 id）。语义应为 404：让客户端明确放弃而不是
                    # 当 502 无限重试。负缓存 300s —— 否则每次重试都会向上游
                    # 发一次真实请求（实测每次 1.3-1.7s，且永不缓存）。
                    await store.put(cache_key, {"status": "gone"}, ttl_s=300)
                    return JSONResponse(status_code=404,
                                        content={"detail": "unknown or expired result id"})
                return _upstream_502(exc.message)
            ttl = 120 if state.get("status") in ("ready", "error") else 3
            await store.put(cache_key, state, ttl_s=ttl)

        status = state.get("status")
        if status == "gone":
            return JSONResponse(status_code=404,
                                content={"detail": "unknown or expired result id"})
        out = {"id": id, "progress": None, "details": None, "preview": None}
        if status == "ready":
            out["status"] = "Ready"
            # 产物持久化：playground media token 分钟级过期，代理链接会随之
            # 失效；COS 凭证在 ⇒ 首次 Ready 时转存并回 24h 预签名链（幂等，
            # persisted_url 缓存在 state 里）。无凭证时退回代理链接。
            if not state.get("persisted_url"):
                # 直连路径的精确尺寸：提交时记了 exact:{pollToken}，首次
                # Ready 转存前再裁切，桥路径已在 _bridge_run 内完成同一件事。
                exact = await store.get(f"exact:{id}")
                if mps.configured():
                    media = await upstream.fetch_media(state["imageToken"])
                    if exact:
                        media = await asyncio.to_thread(
                            _fit_exact, media, int(exact["w"]), int(exact["h"]))
                    key = f"results/{state['imageToken'][:16]}.png"
                    log.info("persist plain ready token=%s %d bytes",
                             state["imageToken"][:16], len(media))
                    await mps.upload_to_cos(media, key)
                    state["persisted_url"] = mps.cos_presigned_get(
                        "/" + key, ttl_s=86400)
                else:
                    if exact:
                        media = await upstream.fetch_media(state["imageToken"])
                        media = await asyncio.to_thread(
                            _fit_exact, media, int(exact["w"]), int(exact["h"]))
                        await store.put_blob(
                            f"bridge_bytes:{state['imageToken']}", media,
                            ttl_s=3600)
                    # PUBLIC_BASE 优先：nginx 后面 request.base_url 会是 http，
                    # 交给调用方一个 http 链接会在 :80 吃 301（矩阵测试实测）。
                    base = PUBLIC_BASE or str(request.base_url).rstrip("/")
                    state["persisted_url"] = (
                        f"{base}/v1/flux3-image/results/{state['imageToken']}")
            out["result"] = {"sample": state["persisted_url"]}
        elif status == "error":
            out["status"] = "Error"
            out["details"] = {"error": state.get("error") or "unknown"}
        else:
            out["status"] = "Pending"
        return JSONResponse(out)

    async def _composite_step(request: Request, token: str, composite: dict):
        """One client-driven step of a generate+upscale composite (4k/8k)."""
        base = str(request.base_url).rstrip("/")
        out = {"id": token, "progress": None, "details": None, "preview": None}

        if composite["phase"] == "gen":
            try:
                state = await upstream.poll_once(token)
            except upstream.UpstreamError as exc:
                return _upstream_502(exc.message)
            if state.get("status") == "error":
                await store.drop(f"comp:{token}")
                out["status"] = "Error"
                out["details"] = {"error": state.get("error") or "unknown"}
                return JSONResponse(out)
            if state.get("status") != "ready":
                out["status"] = "Pending"
                return JSONResponse(out)
            if not mps.configured():
                await store.drop(f"comp:{token}")
                out["status"] = "Error"
                out["details"] = {"error": "MPS not configured for 4k/8k composite"}
                return JSONResponse(out)
            # MPS cannot fetch the playground media URL itself (its fetcher
            # gets 429'd by the edge, measured on a real task), so the image
            # rides through our COS: download, upload, CosInputInfo.
            try:
                media = await upstream.fetch_media(state["imageToken"])
                log.info("composite gen ready token=%s target=%s media=%d bytes",
                         token[:16], composite["target"], len(media))
                key = f"upscale/{token[:16]}.png"
                magnify = int(COMPOSITE_TIERS[composite["target"]])
                # 数据万象同步超分优先（秒级直返，实测 2026-10-15）；
                # MPS 异步任务兜底（其队列曾 pending 40min+）。
                try:
                    up_bytes = await mps.ci_super_resolve_cos(
                        key, magnify=magnify, data=media)
                    target = composite.get("target_size")
                    if target:
                        up_bytes = await asyncio.to_thread(
                            _fit_exact, up_bytes, *target)
                        out_key = (f"results/{token[:16]}"
                                   f"-{target[0]}x{target[1]}.png")
                    else:
                        out_key = f"results/{token[:16]}.{mps.image_ext(up_bytes)}"
                    await mps.upload_to_cos(up_bytes, out_key)
                    await store.drop(f"comp:{token}")
                    log.info("composite done (CI sync) token=%s %d bytes",
                             token[:16], len(up_bytes))
                    out["status"] = "Ready"
                    out["result"] = {"sample": mps.cos_presigned_get(
                        "/" + out_key, ttl_s=86400)}
                    return JSONResponse(out)
                except mps.MPSError as exc:
                    log.warning("CI sync failed, falling back to MPS task: %s", exc)
                task_id = await mps.submit_cos(key, percent=COMPOSITE_TIERS[composite["target"]])
                log.info("composite MPS task=%s percent=%.1f", task_id[:16],
                         COMPOSITE_TIERS[composite["target"]])
            except (mps.MPSError, RuntimeError, upstream.UpstreamError) as exc:
                await store.drop(f"comp:{token}")
                out["status"] = "Error"
                out["details"] = {"error": f"upscale submit failed: {exc}"}
                return JSONResponse(out)
            composite.update(phase="upscale", upscale_task=task_id)
            await store.put(f"comp:{token}", composite, ttl_s=_COMP_TTL)  # 🔴 必须落库
            out["status"] = "Generating"
            out["details"] = {"stage": "upscale"}
            return JSONResponse(out)

        # phase == "upscale": one MPS query per client poll.
        try:
            state = await mps.query(composite["upscale_task"])
        except mps.MPSError as exc:
            await store.drop(f"comp:{token}")
            out["status"] = "Error"
            out["details"] = {"error": str(exc)}
            return JSONResponse(out)
        if state["state"] == "pending":
            out["status"] = "Generating"
            out["details"] = {"stage": "upscale"}
            return JSONResponse(out)
        await store.drop(f"comp:{token}")
        if state["state"] == "error":
            out["status"] = "Error"
            out["details"] = {"error": state["error"]}
            return JSONResponse(out)
        log.info("composite done token=%s url=%s", token[:16], state["url"][:70])
        sample = state["url"]
        target = composite.get("target_size")
        if target:
            # 异步 MPS 只回 COS 链接；精确尺寸必须落地成本地字节再转存。
            try:
                raw = await asyncio.to_thread(_fetch_url_bytes, sample)
                fitted = await asyncio.to_thread(_fit_exact, raw, *target)
                out_key = f"results/{token[:16]}-{target[0]}x{target[1]}.png"
                await mps.upload_to_cos(fitted, out_key)
                sample = mps.cos_presigned_get("/" + out_key, ttl_s=86400)
            except Exception as exc:  # noqa: BLE001 - 保留可用的放大结果
                log.warning("exact-size fit skipped for composite url: %s", exc)
        out["status"] = "Ready"
        out["result"] = {"sample": sample}
        return JSONResponse(out)

    @app.get("/v1/flux3-image/results/{image_token}")
    async def native_sample(image_token: str):
        blob = await store.get_blob(f"bridge_bytes:{image_token}")
        if blob:
            return Response(content=blob, media_type="image/png")
        try:
            media = await upstream.fetch_media(image_token)
        except upstream.UpstreamError as exc:
            return _upstream_502(exc.message)
        return Response(content=media, media_type="image/png")

    # NOTE: deliberately no standalone /upscale endpoint. Super-resolution is
    # never a separate product here: any request that asks for more than the
    # 2k anonymous ceiling (resolution 4k/8k on this door, size >2048 on the
    # OpenAI doors) transparently runs generate-then-upscale in one flow.
