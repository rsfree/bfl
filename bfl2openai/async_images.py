"""`/async/v1/images/generations` — jimeng-shaped async task contract.

Contract (aligned with ~/PycharmProjects/AI/reverse/jimeng, verified in that
repo's main.py/service.py):

  POST /async/v1/images/generations        202 + {"task_id"} + Location
  GET  /async/v1/images/generations/{id}   non-terminal -> 202 {task_id, status}
                                           success      -> 200 {data:[{url}], created, usage}
                                           failure      -> 200 {task_id, status:"failure", error}
                                           unknown      -> 404
  GET  /async/v1/images/generations        this credential's task list
  DELETE /async/v1/images/generations/{id} drop a task (400 while in-flight)

Five lowercase states on the top-level `status` field -- queued /
in_progress / success / failure / canceled -- one field decides everything.
Failures answer 200 on purpose: the task completed, its RESULT is a failure;
an HTTP 5xx would trip caller retry logic for a request that did nothing
wrong. Ownership follows the bearer credential fingerprint (everybody is
"anonymous" while auth is disabled).

Body: the native generate shape (prompt*, images, aspect_ratio, resolution,
bounding_boxes) plus everything this adapter already accepts there --
`@image#N:name` upload handles are resolved at ACCEPT time (the store is
in-process; resolving early means a background task never depends on handle
survival), and resolution 4k/8k runs the MPS composite inside the task.

Execution model: one asyncio task per submission, in-memory store. A service
restart loses tasks -- the upstream is a free playground and re-running is
cheap; durable tasks would need a real store (jimeng uses SQLite) and that is
deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import native
from . import upstream
from . import upscale as mps
from .bridging import parse_boxes

log = logging.getLogger("bfl2openai.async_images")

PUBLIC_BASE = os.environ.get("BFL2OPENAI_PUBLIC_BASE", "").rstrip("/")

_TASKS: dict[str, dict] = {}          # task_id -> record
_ORDER: list[str] = []                # submission order, for listing
_MAX_TASKS = 500


def _fingerprint(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    key = request.headers.get("x-key", "")
    return auth or key or "anonymous"


def _view(rec: dict) -> tuple[int, dict[str, Any]]:
    """The one exit shape (jimeng's four deliberate choices, kept verbatim)."""
    tid = rec["task_id"]
    status = rec["status"]
    if status == "queued":
        return 202, {"task_id": tid, "status": status}
    if status == "in_progress":
        return 202, {"task_id": tid, "status": status,
                     "details": rec.get("details")}
    if status == "failure":
        return 200, {"task_id": tid, "status": "failure",
                     "error": rec.get("error") or {"message": "task failed"}}
    if status == "canceled":
        return 200, {"task_id": tid, "status": "canceled"}
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return 200, {"id": tid, "status": "success", "created": rec["created"],
                 "data": rec["data"], "usage": usage}


def _get_for_credential(task_id: str, credential: str) -> dict | None:
    rec = _TASKS.get(task_id)
    if rec is None or rec["credential"] != credential:
        return None  # missing and foreign are the same 404 (jimeng's rule)
    return rec


async def _run_task(task_id: str, payload: dict) -> None:
    rec = _TASKS.get(task_id)
    if rec is None:
        return
    rec["status"] = "in_progress"
    resolution = rec["resolution"]
    upscale_to = rec["upscale_to"]
    try:
        poll_token = await native_submit(payload, resolution, upscale_to, rec)
        if resolution in native.COMPOSITE_TIERS:
            rec["details"] = {"stage": "generate"}
        image_token = await upstream.wait_for_result(poll_token)
        log.info("async task %s generated (imageToken=%s…)", task_id[:12],
                 image_token[:10])

        data: dict[str, str]
        if upscale_to:
            rec["details"] = {"stage": "upscale"}
            media = await upstream.fetch_media(image_token)
            object_key = f"upscale/{poll_token[:16]}.png"
            await mps.upload_to_cos(media, object_key)
            percent = max(1.0, min(10.0, upscale_to / 2048.0))
            mps_task = await mps.submit_cos(object_key, percent=percent)
            sign_url = await mps.wait_done(mps_task)
            data = {"url": sign_url}  # managed COS link, ~1h validity
        else:
            # 永远不出上游原始链接：media token 分钟级过期（实测 3 分钟即
            # "Invalid or expired"）且直拉会被 429。COS 凭证在 ⇒ 结果落桶出
            # 24h 预签名链（与 4k 复合一致）；无凭证时退回本服务结果代理。
            media = await upstream.fetch_media(image_token)
            if mps.configured():
                key = f"results/{image_token[:16]}.png"
                await mps.upload_to_cos(media, key)
                data = {"url": mps.cos_presigned_get("/" + key, ttl_s=86400)}
            else:
                data = {"url": f"{rec['base']}/v1/flux3-image/results/{image_token}"}

        rec["status"] = "success"
        rec["data"] = [data]
        rec["created"] = int(time.time())
        log.info("async task %s success", task_id[:12])
    except (upstream.UpstreamError, mps.MPSError) as exc:
        rec["status"] = "failure"
        rec["error"] = {"message": str(exc)[:300]}
        log.warning("async task %s failed: %s", task_id[:12], exc)


async def native_submit(payload: dict, resolution: str,
                        upscale_to: int | None, rec: dict) -> str:
    """Shared submit path: uploads already resolved; 4k/8k gen at 2k."""
    prompt = payload["prompt"]
    images = payload.get("images") or []
    boxes = parse_boxes(payload.get("boxes"))
    png = await native._reference_png(images[0]) if images else b""
    gen_resolution = "2k" if upscale_to else resolution
    return await upstream.submit_generation(
        image_png=png,
        prompt=prompt,
        boxes=boxes,
        aspect_ratio=str(payload.get("aspect_ratio") or "auto"),
        resolution=gen_resolution,
        mode="i2i" if images else "t2i",
    )


def register(app: FastAPI) -> None:
    @app.post("/async/v1/images/generations", status_code=202)
    async def create_generation(request: Request, payload: dict):
        credential = _fingerprint(request)
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "prompt"],
                                     "msg": "Field required"}]},
            )
        resolution = str(payload.get("resolution") or "1k")
        if (resolution not in native.RESOLUTIONS
                and resolution not in native.COMPOSITE_TIERS):
            return JSONResponse(
                status_code=422,
                content={"detail": [{"loc": ["body", "resolution"],
                                     "msg": "invalid resolution"}]},
            )
        upscale_to = (2048 * 2 if resolution == "4k"
                      else 2048 * 4 if resolution == "8k" else None)
        if upscale_to and not mps.configured():
            return JSONResponse(
                status_code=503,
                content={"detail": "resolution 4k/8k requires MPS "
                                   "super-resolution (not configured)"},
            )

        # handles resolve at ACCEPT time: the background task must not depend
        # on upload-handle survival (TTL 1h, in-process).
        try:
            images = payload.get("images")
            refs = [images] if isinstance(images, str) else list(images or [])
            prompt, refs = native.uploads_mod.resolve_handles(prompt, refs or None)
        except HTTPException:
            raise
        payload = {**payload, "prompt": prompt, "images": refs}

        task_id = uuid.uuid4().hex[:24]
        while len(_ORDER) >= _MAX_TASKS:
            _TASKS.pop(_ORDER.pop(0), None)
        rec = {
            "task_id": task_id, "credential": credential,
            "base": PUBLIC_BASE or str(request.base_url).rstrip("/"),
            "status": "queued", "resolution": resolution,
            "upscale_to": upscale_to, "payload": payload,
            "created": int(time.time()), "data": [], "error": None,
            "details": None,
        }
        _TASKS[task_id] = rec
        _ORDER.append(task_id)
        asyncio.create_task(_run_task(task_id, payload))
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id},
            headers={"Location": f"/async/v1/images/generations/{task_id}"},
        )

    @app.get("/async/v1/images/generations/{task_id}")
    async def get_generation(request: Request, task_id: str):
        rec = _get_for_credential(task_id, _fingerprint(request))
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        status_code, body = _view(rec)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/async/v1/images/generations")
    async def list_generations(request: Request, limit: int = 50):
        credential = _fingerprint(request)
        limit = max(1, min(limit, 200))
        items = []
        for tid in reversed(_ORDER):
            rec = _TASKS.get(tid)
            if rec and rec["credential"] == credential:
                items.append({"task_id": tid, "status": rec["status"],
                              "created": rec["created"]})
            if len(items) >= limit:
                break
        return JSONResponse({"tasks": items})

    @app.delete("/async/v1/images/generations/{task_id}")
    async def delete_generation(request: Request, task_id: str):
        rec = _get_for_credential(task_id, _fingerprint(request))
        if rec is None:
            raise HTTPException(status_code=404, detail="task not found")
        if rec["status"] in ("queued", "in_progress"):
            return JSONResponse(
                status_code=400,
                content={"detail": "task is still running; there is no "
                                   "cancel endpoint -- deleting it would only "
                                   "hide a running, resource-consuming job"},
            )
        _TASKS.pop(task_id, None)
        if task_id in _ORDER:
            _ORDER.remove(task_id)
        return JSONResponse({"deleted": task_id})
