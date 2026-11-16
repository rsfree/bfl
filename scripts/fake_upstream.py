"""本地假上游：录制 bfl2openai 真正发出去的 multipart 报文。

用途：验证 composition 降级分支。真实上游只回 200 或 400，看不到「到底发了
什么」；这里把每个字段、每个文件部件（名字/文件名/大小/魔数/sha1）原样存下来，
`GET /_records` 读回。

跑法（两个终端）：

    # 1) 假上游
    .venv/Scripts/python -m uvicorn scripts.fake_upstream:app --port 8799
    # 2) 被测服务，指向上面的假上游
    BFL2OPENAI_UPSTREAM=http://127.0.0.1:8799 \
    .venv/Scripts/python -m uvicorn bfl2openai.server:app --port 8798

注意：这里只验证「我们发了什么」，不验证上游如何解读，所以不需要外网。
"""
from __future__ import annotations

import hashlib
import io
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image

app = FastAPI(title="fake-upstream")

RECORDS: list[dict] = []
MEDIA: dict[str, bytes] = {}


def _png(seed: int = 0, size: int = 512) -> bytes:
    im = Image.new("RGB", (size, size), (210 - seed * 20, 120, 60))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@app.post("/api/flux3-image/generate")
async def generate(request: Request):
    form = await request.form()
    fields: dict[str, str] = {}
    files: list[dict] = []
    for key, value in form.multi_items():
        if isinstance(value, str):
            fields[key] = value
            continue
        data = await value.read()
        files.append({
            "field": key,
            "filename": value.filename,
            "content_type": value.content_type,
            "bytes": len(data),
            "magic": data[:8].hex(),
            "sha1": hashlib.sha1(data).hexdigest()[:12],
            "is_empty": len(data) == 0,
        })
    token = f"poll{len(RECORDS):04d}"
    RECORDS.append({
        "ts": time.time(),
        "path": str(request.url.path),
        "mode": fields.get("mode"),
        "aspectRatio": fields.get("aspectRatio"),
        "resolution": fields.get("resolution"),
        "prompt": fields.get("prompt"),
        "fields": fields,
        "files": files,
        "file_fields": [f["field"] for f in files],
        "has_image_part": any(f["field"] == "image" and not f["is_empty"] for f in files),
        "empty_image_part": any(f["field"] == "image" and f["is_empty"] for f in files),
        "ref_parts": sum(1 for f in files if f["field"] == "references"),
        "pollToken": token,
    })
    MEDIA[token] = _png(len(RECORDS))
    return {"status": "submitted", "pollToken": token}


@app.get("/api/flux3-image/status")
async def status(pollToken: str = ""):
    if not pollToken:
        return JSONResponse({"status": "error", "error": "missing pollToken"}, 400)
    return {"status": "ready", "imageToken": f"img-{pollToken}", "progress": 100}


@app.get("/api/flux3-image/media")
async def media(imageToken: str = ""):
    token = imageToken.replace("img-", "")
    return Response(content=MEDIA.get(token) or _png(0), media_type="image/png")


@app.get("/_records")
async def records():
    return {"count": len(RECORDS), "records": RECORDS}


@app.post("/_reset")
async def reset():
    RECORDS.clear()
    MEDIA.clear()
    return {"ok": True}
