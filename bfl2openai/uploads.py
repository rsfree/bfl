"""Uploaded-image handles: `@image#N:filename` references for prompts.

Flow (mirrors how chat clients paste screenshots):

1. `POST /v1/uploads` (multipart, field `file`, repeatable) stores the bytes
   and returns handles:
       {"files": [{"id": "...", "ref": "@image#1:Clipboard_Screenshot.png",
                   "filename": "...", "size": n, "expires_in": 3600}]}
   `@image#N` is the per-call ordinal (first file of one call is #1) and
   `filename` is the client's name -- together they form the reference.
2. A later `POST /v1/flux-3-image` may reference those handles inside
   `prompt` (e.g. "edit @image#1:Clipboard_Screenshot.png: ..."). Before the
   request goes upstream, every handle is rewritten to the BFL multi-reference
   placeholder `<ref_image_K>` (docs.bfl.ai layout convention) and the stored
   bytes are appended to `images` in ordinal order (after any caller-supplied
   images, which keep indices 0..k-1).

Storage is in-process and best-effort by design: TTL 1h, bounded in count and
bytes, gone on restart. Handles are a convenience inside one client session --
durable assets should travel as URLs/base64 directly. Guards: a missing or
expired handle fails the request with the reference named; the store is size
capped so a paste storm cannot OOM the worker.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

log = logging.getLogger("bfl2openai.uploads")

HANDLE_TTL_S = 3600
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_STORE_BYTES = 128 * 1024 * 1024

_HANDLE_RE = re.compile(r"@image#(\d+):([^\s]+)")

_STORE: dict[str, dict] = {}   # id -> {data, filename, exp}
_REFS: dict[str, str] = {}     # "@image#N:name" -> id
_LOCK = threading.Lock()
_TOTAL = 0


def _janitor() -> None:
    global _TOTAL
    now = time.time()
    for key in [k for k, v in _STORE.items() if v["exp"] < now]:
        _TOTAL -= len(_STORE.pop(key)["data"])
    while _TOTAL > MAX_STORE_BYTES and _STORE:
        oldest = min(_STORE.items(), key=lambda kv: kv[1]["exp"])[0]
        _TOTAL -= len(_STORE.pop(oldest)["data"])


def store_upload(data: bytes, filename: str, ordinal: int) -> dict:
    global _TOTAL
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"file exceeds {MAX_FILE_BYTES} bytes")
    with _LOCK:
        _janitor()
        if _TOTAL + len(data) > MAX_STORE_BYTES:
            raise HTTPException(status_code=507,
                                detail="upload store is full, retry later")
        file_id = uuid.uuid4().hex[:16]
        _STORE[file_id] = {
            "data": data,
            "filename": filename,
            "exp": time.time() + HANDLE_TTL_S,
        }
        _TOTAL += len(data)
    ref = f"@image#{ordinal}:{filename}"
    register_ref(ref, file_id)
    return {
        "id": file_id,
        "ref": ref,
        "filename": filename,
        "size": len(data),
        "expires_in": HANDLE_TTL_S,
    }


def register_ref(ref: str, file_id: str) -> None:
    """Maps one handle to its stored id; the table is bounded like the store."""
    with _LOCK:
        _REFS[ref] = file_id
        if len(_REFS) > 512:
            for stale in list(_REFS)[: len(_REFS) - 512]:
                _REFS.pop(stale, None)


def resolve_handles(prompt: str, explicit_images: list[str] | None) -> tuple[str, list[str]]:
    """Rewrites `@image#N:name` handles into BFL `<ref_image_K>` placeholders.

    Returns (rewritten_prompt, images). Uploaded bytes come back as data URIs
    (the one spelling every downstream path already accepts). A missing or
    expired handle raises 400 naming the reference.
    """
    matches = list(_HANDLE_RE.finditer(prompt or ""))
    if not matches:
        return prompt or "", list(explicit_images or [])

    images = list(explicit_images or [])
    rewritten = prompt or ""
    resolved: list[str] = []
    with _LOCK:
        _janitor()
        for match in matches:
            # Filename charset excludes whitespace; a trailing run of colons
            # belongs to the caller's punctuation ("fix @image#1:a.png: make
            # it pop"), so it is stripped -- and survives the rewrite intact.
            ref = match.group(0).rstrip(":")
            file_id = _REFS.get(ref)
            entry = _STORE.get(file_id) if file_id else None
            if entry is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown or expired image reference: {ref}",
                )
            placeholder = f"<ref_image_{len(images)}>"
            images.append(
                "data:image/png;base64,"
                + base64.b64encode(entry["data"]).decode("ascii")
            )
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
            info = store_upload(data, filename, ordinal)
            files.append(info)
        if not files:
            return JSONResponse(status_code=422,
                                content={"detail": "no non-empty file parts"})
        return JSONResponse({"files": files})
