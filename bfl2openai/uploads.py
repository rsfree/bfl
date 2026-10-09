"""Uploaded-image handles: `@image#N:filename` references for prompts.

Flow (mirrors how chat clients paste screenshots):

1. `POST /v1/uploads` (multipart, field `file`, repeatable) stores the bytes
   and returns handles:
       {"files": [{"id": "...", "ref": "@image#1:Clipboard_Screenshot.png",
                   "filename": "...", "size": n, "expires_in": 3600}]}
   `@image#N` is the per-call ordinal (first file of one call is #1) and
   `filename` is the client's name -- together they form the reference.
2. A later `POST /v1/flux-3-image` may reference those handles inside
   `prompt`; resolve_handles() swaps them for the BFL multi-reference
   placeholder `<ref_image_K>` and returns the bytes.

Storage: Redis (REDIS_URL) or in-process fallback -- 1h TTL both ways, so a
multi-gunicorn-worker deployment shares handles through Redis while a single
worker / tests keep working with zero dependencies.
"""

from __future__ import annotations

import base64
import logging
import re
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from . import store

log = logging.getLogger("bfl2openai.uploads")

HANDLE_TTL_S = 3600
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_STORE_BYTES = 128 * 1024 * 1024

_HANDLE_RE = re.compile(r"@image#(\d+):([^\s]+)")


def store_upload(data: bytes, filename: str, ordinal: int) -> dict:
    """Sync wrapper for out-of-loop callers (tests); asyncio.run is loop-safe."""
    import asyncio

    return asyncio.run(store_upload_async(data, filename, ordinal))


async def store_upload_async(data: bytes, filename: str, ordinal: int) -> dict:
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"file exceeds {MAX_FILE_BYTES} bytes")
    file_id = uuid.uuid4().hex[:16]
    await store.put_blob(f"upl:{file_id}", data, ttl_s=HANDLE_TTL_S)
    ref = f"@image#{ordinal}:{filename}"
    await store.put(f"ref:{ref}", {"id": file_id}, ttl_s=HANDLE_TTL_S)
    return {
        "id": file_id,
        "ref": ref,
        "filename": filename,
        "size": len(data),
        "expires_in": HANDLE_TTL_S,
    }


def resolve_handles(prompt: str, explicit_images: list[str] | None):
    """Sync wrapper for out-of-loop callers; in-loop callers use the async one."""
    import asyncio

    return asyncio.run(resolve_handles_async(prompt, explicit_images))


async def resolve_handles_async(prompt: str, explicit_images: list[str] | None):
    """Rewrites `@image#N:name` handles into BFL `<ref_image_K>` placeholders.

    Returns (rewritten_prompt, images). A missing or expired handle raises
    400 naming the reference. Uploaded bytes come back as data URIs.
    """
    matches = list(_HANDLE_RE.finditer(prompt or ""))
    if not matches:
        return prompt or "", list(explicit_images or [])

    images = list(explicit_images or [])
    rewritten = prompt or ""
    resolved: list[str] = []
    for match in matches:
        # Filename charset excludes whitespace; a trailing run of colons
        # belongs to the caller's punctuation ("fix @image#1:a.png: make it
        # pop"), so it is stripped -- and survives the rewrite intact.
        ref = match.group(0).rstrip(":")
        entry = await store.get(f"ref:{ref}")
        if entry is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown or expired image reference: {ref}",
            )
        blob = await store.get_blob(f"upl:{entry['id']}")
        if blob is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown or expired image reference: {ref}",
            )
        placeholder = f"<ref_image_{len(images)}>"
        images.append(
            "data:image/png;base64," + base64.b64encode(blob).decode("ascii"))
        rewritten = rewritten.replace(ref, placeholder, 1)
        resolved.append(ref)
    if resolved:
        log.info("resolved uploaded handle(s): %s", resolved)
    return rewritten, images


def register(app: FastAPI) -> None:
    @app.post("/v1/uploads")
    async def uploads(request: Request, file: list[UploadFile] = File(...)):
        files: list[dict] = []
        for ordinal, upload in enumerate(file, start=1):
            data = await upload.read()
            if not data:
                continue
            filename = upload.filename or f"upload_{ordinal}.png"
            info = await store_upload_async(data, filename, ordinal)
            files.append(info)
        if not files:
            return JSONResponse(status_code=422,
                                content={"detail": "no non-empty file parts"})
        return JSONResponse({"files": files})
