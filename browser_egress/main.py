"""Playwright 浏览器出口：真实 Chromium 环境内的 fetch 代理。

上游 flux-tools 对 curl_cffi 的 HTTP/2 指纹识别为 bot（403 Access denied），
只放行真实浏览器环境。本服务常驻一个 headless Chromium 页面，通过页面内
fetch 完成 提交→轮询→取图 全流程（纯网络调用，可并发，不受 UI 40s 串行限制）。

POST /generate {image_b64, prompt, aspect_ratio, resolution, mode}
  -> {image_b64} | {error}
GET  /healthz -> {ok, page_ready, inflight}
"""

import asyncio
import base64
import json
import os
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel

BASE = "https://flux-tools.bfl.ai"
PAGE_URL = f"{BASE}/precise-editing"
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0

app = FastAPI()
_state = {"page": None, "inflight": 0, "lock": asyncio.Lock()}

SUBMIT_JS = """
async ({prompt, aspectRatio, resolution, mode, imgB64, boxes, refs}) => {
  const bin = atob(imgB64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const fd = new FormData();
  fd.append("prompt", prompt);
  fd.append("aspectRatio", aspectRatio);
  fd.append("resolution", resolution);
  fd.append("mode", mode);
  if (boxes) fd.append("boxes", boxes);
  fd.append("image", new Blob([arr], {type: "image/png"}), "image.png");
  if (refs && refs.length) {
    for (let i = 0; i < refs.length; i++) {
      const rb = atob(refs[i]);
      const ra = new Uint8Array(rb.length);
      for (let j = 0; j < rb.length; j++) ra[j] = rb.charCodeAt(j);
      fd.append("image", new Blob([ra], {type: "image/png"}), `ref${i}.png`);
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


async def ensure_page():
    page = _state.get("page")
    if page and not page.is_closed():
        return page
    async with _state["lock"]:
        page = _state.get("page")
        if page and not page.is_closed():
            return page
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(locale="en-US")
        page = await ctx.new_page()
        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        _state["pw"] = pw
        _state["page"] = page
        return page


class GenReq(BaseModel):
    image_b64: str
    prompt: str
    aspect_ratio: str = "1:1"
    resolution: str = "1k"
    mode: str = "i2i"
    boxes: str | None = None
    refs: list[str] | None = None


@app.get("/healthz")
async def healthz():
    page = _state.get("page")
    return {"ok": True, "page_ready": bool(page and not page.is_closed()),
            "inflight": _state["inflight"]}


@app.post("/generate")
async def generate(req: GenReq):
    _state["inflight"] += 1
    try:
        page = await ensure_page()
        sub = await page.evaluate(SUBMIT_JS, {
            "prompt": req.prompt, "aspectRatio": req.aspect_ratio,
            "resolution": req.resolution, "mode": req.mode,
            "imgB64": req.image_b64, "boxes": req.boxes,
            "refs": req.refs or [],
        })
        if sub["status"] != 200:
            return JSONResponse(status_code=502,
                               content={"error": f"submit {sub['status']}: {sub['body'][:200]}"})
        try:
            token = json.loads(sub["body"]).get("pollToken")
        except Exception:
            return JSONResponse(status_code=502,
                               content={"error": f"bad submit body: {sub['body'][:200]}"})
        if not token:
            return JSONResponse(status_code=502, content={"error": "no pollToken"})

        deadline = time.time() + POLL_TIMEOUT_S
        image_token = None
        last = ""
        while time.time() < deadline:
            st = await page.evaluate(STATUS_JS, token)
            last = st["body"][:200]
            if st["status"] == 200:
                try:
                    doc = json.loads(st["body"])
                except Exception:
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
            await asyncio.sleep(POLL_INTERVAL_S)
        if not image_token:
            return JSONResponse(status_code=504, content={"error": f"poll timeout: {last}"})

        med = await page.evaluate(MEDIA_JS, image_token)
        if med.get("status") != 200 or not med.get("b64"):
            return JSONResponse(status_code=502,
                               content={"error": f"media {med.get('status')}: {str(med.get('body'))[:150]}"})
        return {"image_b64": med["b64"]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {str(exc)[:150]}"})
    finally:
        _state["inflight"] -= 1


@app.on_event("startup")
async def _warm():
    asyncio.create_task(ensure_page())
