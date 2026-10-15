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
GET  /v1/models             model list for client sanity checks
GET  /healthz

Run:  uvicorn bfl2openai.server:app --host 127.0.0.1 --port 8977
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import upstream
from . import native
from . import bridging
from . import upscale as mps
from . import uploads
from . import async_images
from .bridging import image_to_png, mask_to_boxes, parse_boxes, size_to_aspect_ratio
from .upstream import UpstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bfl2openai")

app = FastAPI(title="bfl2openai", version="0.2.0")
native.register(app)
uploads.register(app)
async_images.register(app)

MODEL_ID = os.environ.get("BFL2OPENAI_MODEL", "flux-precise-edit")
API_KEY = os.environ.get("BFL2OPENAI_API_KEY", "")  # optional; empty = auth disabled
UPSTREAM_BASE = os.environ.get("BFL2OPENAI_UPSTREAM", upstream.DEFAULT_BASE)

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


def wrap_upstream_error(exc: UpstreamError) -> JSONResponse:
    # 内容审核（moderation）是客户端内容问题：422 让调用方明确知道
    # "换内容"而不是"重试"（此前误映射为 502，客户会无效重试且监控失真）。
    if exc.kind == "content_policy":
        return openai_error(422, f"Content moderation rejected: {exc.message}",
                            "content_policy_violation")
    status = 400 if exc.kind == "invalid_request_error" else 502
    return openai_error(status, f"Upstream error: {exc.message}", exc.kind)


def _parse_long_side(size: str | None) -> int | None:
    """A canonical "WxH" as its long edge, or None when it does not parse."""
    if not size:
        return None
    try:
        w, h = size.lower().split("x")
        return max(int(w), int(h))
    except ValueError:
        return None


#: Anonymous generation tops out at the 2k tier (long edge 2048); anything
#: beyond is reached by generating at 2k and MPS-upscaling (x = long/2048).
GEN_LONG_EDGE = 2048


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
    async def one() -> dict[str, str]:
        gen_resolution = bridging.ensure_resolution_capacity(
            aspect_ratio, "2k" if upscale_to else resolution)
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
        log.info("submitted pollToken=%s… (resolution=%s)", poll_token[:10], gen_resolution)
        image_token = await upstream.wait_for_result(poll_token, UPSTREAM_BASE)
        log.info("ready imageToken=%s…", image_token[:10])
        if upscale_to:
            # Automatic super-resolution for >2k asks: the generated image
            # rides through our COS -- MPS's own fetcher gets 429'd by the
            # playground edge (measured), so the URL input path is not used.
            media = await upstream.fetch_media(image_token, UPSTREAM_BASE)
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
                media = await upstream.fetch_media(image_token, UPSTREAM_BASE)
                key = f"results/{image_token[:16]}.png"
                await mps.upload_to_cos(media, key)
                return {"url": mps.cos_presigned_get("/" + key, ttl_s=86400)}
            base = str(request.base_url).rstrip("/")
            return {"url": f"{base}/v1/images/results/{image_token}"}
        media = await upstream.fetch_media(image_token, UPSTREAM_BASE)
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
        log.warning("run_job upstream error: %s", str(exc)[:300])
        return wrap_upstream_error(exc)
    except mps.MPSError as exc:
        log.warning("run_job mps error: %s", str(exc)[:300])
        return openai_error(502, f"super-resolution failed: {exc.message}",
                            "upstream_error")

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


# ---------------------------------------------------------------- endpoints


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": MODEL_ID, "upstream": UPSTREAM_BASE}


@app.get("/v1/models")
async def list_models(request: Request):
    await check_auth(request)
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "bfl2openai"}],
    }


@app.post("/v1/images/edits")
async def images_edits(
    request: Request,
    image: list[UploadFile] = File(...),
    prompt: str = Form(""),
    mask: UploadFile | None = File(None),
    n: int = Form(1),
    size: str | None = Form(None),
    response_format: Literal["url", "b64_json"] | None = Form(None),
    # ---- bfl2openai extensions (beyond the OpenAI spec) ----
    boxes: str | None = Form(None),          # JSON: [{"box":{"x","y","w","h"},"instruction"}]
    aspect_ratio: str | None = Form(None),   # "auto" | "1:1" | "16:9" | ...
    resolution: str = Form("1.5k"),          # "1k" | "1.5k" | ...
    mode: str = Form("i2i"),
):
    await check_auth(request)
    if not prompt and not boxes:
        return openai_error(400, "prompt (or boxes) is required")

    try:
        image_png = image_to_png(await image[0].read())
        box_list = parse_boxes(boxes)
    except Exception as exc:
        return openai_error(400, f"invalid image or boxes: {exc}")

    # 多垫图：image[1:] 转发为 references 文件部件（上游按部件+filename 配对，
    # 实测 2026-10-14）。单张失败丢弃、不阻塞主图。
    reference_images: list[tuple[bytes, str]] = []
    for idx, ref_file in enumerate(image[1:], start=2):
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
    upscale_to = _parse_long_side(size)
    upscale_to = upscale_to if upscale_to and upscale_to > GEN_LONG_EDGE else None
    if not upscale_to and resolution in ("4k", "8k"):
        upscale_to = GEN_LONG_EDGE * 2 if resolution == "4k" else GEN_LONG_EDGE * 4
    if upscale_to and not mps.configured():
        return openai_error(
            503,
            "an output beyond 2k requires MPS super-resolution "
            "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)",
        )
    return await run_job(
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
    resolution = str(payload.get("resolution") or "1.5k")
    upscale_to = _parse_long_side(payload.get("size"))
    upscale_to = upscale_to if upscale_to and upscale_to > GEN_LONG_EDGE else None
    if not upscale_to and resolution in ("4k", "8k"):
        upscale_to = GEN_LONG_EDGE * 2 if resolution == "4k" else GEN_LONG_EDGE * 4
    if upscale_to and not mps.configured():
        return openai_error(
            503,
            "an output beyond 2k requires MPS super-resolution "
            "(MPS_SECRET_ID / MPS_SECRET_KEY not configured)",
        )
    return await run_job(
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


@app.get("/v1/images/results/{image_token}")
async def image_result(image_token: str, request: Request):
    await check_auth(request)
    try:
        media = await upstream.fetch_media(image_token, UPSTREAM_BASE)
    except UpstreamError as exc:
        return wrap_upstream_error(exc)
    return Response(content=media, media_type="image/png")
