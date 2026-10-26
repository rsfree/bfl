"""浏览器出口客户端：curl_cffi 被上游指纹识别（403 Access denied）时的兜底。

上游 flux-tools 只放行真实浏览器环境（2026-10-26 起）。browser_egress sidecar
常驻 headless Chromium，页面内 fetch 完成 提交→轮询→取图，返回生成图字节。
"""

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request

BROWSER_EGRESS_URL = os.environ.get("BFL2OPENAI_BROWSER_EGRESS", "").rstrip("/")
_TIMEOUT_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_TIMEOUT", "180"))


def configured() -> bool:
    return bool(BROWSER_EGRESS_URL)


async def generate(image_png: bytes, prompt: str, aspect_ratio: str,
                   resolution: str, mode: str, boxes_json: str | None = None,
                   references_json: str | None = None,
                   refs: list[bytes] | None = None) -> bytes:
    """经浏览器 sidecar 完成一次生成，返回图片字节。失败抛 RuntimeError。"""
    payload = {
        "image_b64": base64.b64encode(image_png).decode(),
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "mode": mode,
        "boxes": boxes_json or "[]",
        "references": references_json or "[]",
        "refs": [base64.b64encode(r).decode() for r in (refs or [])],
    }
    def _post() -> tuple[int, str]:
        req = urllib.request.Request(
            f"{BROWSER_EGRESS_URL}/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with op.open(req, timeout=_TIMEOUT_S) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    try:
        status, body = await asyncio.to_thread(_post)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"browser egress transport: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"browser egress {status}: {body[:200]}")
    try:
        doc = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"browser egress bad body: {body[:150]}") from exc
    b64 = doc.get("image_b64")
    if not b64:
        raise RuntimeError(f"browser egress no image: {str(doc)[:150]}")
    return base64.b64decode(b64)
