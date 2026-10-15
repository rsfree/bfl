"""BFL official native API (docs.bfl.ai) endpoints over the playground upstream.

POST /v1/flux-3-image          official native generate/edit submit
GET  /v1/get_result?id=        official native polling
GET  /v1/flux3-image/results/{imageToken}   the URL `result.sample` points at

Contract (api.bfl.ai/openapi.json, fetched 2026-10-09 -- see
analysis/bfl_openapi.json):

  request   application/json
    prompt             string, required
    images             string | [string] (http(s) URL or base64), 1..10
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

import base64
import binascii
import io
import logging
import os
import re
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image

from . import store
from . import upstream
from . import upscale as mps
from .payload import normalize_image
from . import uploads as uploads_mod

log = logging.getLogger("bfl2openai.native")

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


def _check_x_key(request: Request) -> None:
    """Honours the official `x-key` header; enforces only when configured."""
    if not NATIVE_API_KEY:
        return
    if request.headers.get("x-key") != NATIVE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-key")


def _png_bytes(ctx_image_data: bytes, source: str) -> bytes:
    """Normalize a reference image to PNG (the playground's file spelling)."""
    img = Image.open(io.BytesIO(ctx_image_data))
    if img.format == "PNG":
        return ctx_image_data
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    log.info("converted %s input %s to png", img.format, source)
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
        resolution = payload.get("resolution") or "1k"
        if resolution not in RESOLUTIONS and resolution not in COMPOSITE_TIERS:
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "resolution"],
                                     "msg": f"must be one of {sorted(set(RESOLUTIONS) | set(COMPOSITE_TIERS))}"}]},
            )
        if resolution in COMPOSITE_TIERS and not mps.configured():
            # Guard BEFORE any reference processing or submission: 4k/8k are
            # composites (2k generation + MPS x2/x4); without MPS they would
            # otherwise spend a generation and fail afterwards.
            return JSONResponse(
                status_code=503,
                content={"detail":
                         "resolution 4k/8k requires MPS super-resolution "
                         "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)"},
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
        except (ValueError, upstream.UpstreamError) as exc:
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
            except (ValueError, upstream.UpstreamError) as exc:
                log.warning("reference #%d dropped (fetch failed): %s", idx, exc)

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
                    aspect_ratio=str(payload.get("aspect_ratio") or "auto"),
                    resolution=gen_resolution,
                    mode="i2i",
                    reference_images=reference_images or None,
                )
                if resolution in COMPOSITE_TIERS:
                    await store.put(f"comp:{poll_token}", {"phase": "gen", "target": resolution}, ttl_s=_COMP_TTL)
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
                    aspect_ratio=str(payload.get("aspect_ratio") or "auto"),
                    resolution="2k" if resolution in COMPOSITE_TIERS else resolution,
                    mode="t2i",
                )
                if resolution in COMPOSITE_TIERS:
                    await store.put(f"comp:{poll_token}", {"phase": "gen", "target": resolution}, ttl_s=_COMP_TTL)
        except upstream.UpstreamError as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": exc.message},
            )

        base = str(request.base_url).rstrip("/")
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
        _check_x_key(request)
        composite = await store.get(f"comp:{id}")
        if composite:
            return await _composite_step(request, id, composite)

        try:
            state = await upstream.poll_once(id)
        except upstream.UpstreamError as exc:
            return JSONResponse(status_code=502, content={"detail": exc.message})

        status = state.get("status")
        out = {"id": id, "progress": None, "details": None, "preview": None}
        if status == "ready":
            out["status"] = "Ready"
            # 产物持久化：playground media token 分钟级过期，代理链接会随之
            # 失效；COS 凭证在 ⇒ 首次 Ready 时转存并回 24h 预签名链（幂等，
            # persisted_url 缓存在 state 里）。无凭证时退回代理链接。
            if not state.get("persisted_url"):
                if mps.configured():
                    media = await upstream.fetch_media(state["imageToken"])
                    key = f"results/{state['imageToken'][:16]}.png"
                    log.info("persist plain ready token=%s %d bytes",
                             state["imageToken"][:16], len(media))
                    await mps.upload_to_cos(media, key)
                    state["persisted_url"] = mps.cos_presigned_get(
                        "/" + key, ttl_s=86400)
                else:
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
                return JSONResponse(status_code=502, content={"detail": exc.message})
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
                # CI 同步超分的输入上限是 5MB（实测 2026-10-15：>5MB 400
                # ImageTooLarge）—— 用 JPEG 压体积但**保留尺寸**（放大倍数
                # 作用在输入上；normalize 的降档梯子会把 2048² 压到 1024²
                # 导致 4k 只出 2k，full_matrix 实测抓到）。
                ci_input = mps.ci_input_image(media)
                await mps.upload_to_cos(ci_input, key)
                magnify = int(COMPOSITE_TIERS[composite["target"]])
                # 数据万象同步超分优先（秒级直返，实测 2026-10-15）；
                # MPS 异步任务兜底（其队列曾 pending 40min+）。
                try:
                    up_bytes = await mps.ci_super_resolve_cos(key, magnify=magnify)
                    out_key = f"results/{token[:16]}.png"
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
        out["status"] = "Ready"
        out["result"] = {"sample": state["url"]}
        return JSONResponse(out)

    @app.get("/v1/flux3-image/results/{image_token}")
    async def native_sample(image_token: str):
        try:
            media = await upstream.fetch_media(image_token)
        except upstream.UpstreamError as exc:
            return JSONResponse(status_code=502, content={"detail": exc.message})
        return Response(content=media, media_type="image/png")

    # NOTE: deliberately no standalone /upscale endpoint. Super-resolution is
    # never a separate product here: any request that asks for more than the
    # 2k anonymous ceiling (resolution 4k/8k on this door, size >2048 on the
    # OpenAI doors) transparently runs generate-then-upscale in one flow.
