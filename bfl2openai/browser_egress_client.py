"""浏览器出口客户端：curl_cffi 被上游指纹识别（403 Access denied）时的兜底。

上游 flux-tools 只放行真实浏览器环境（2026-10-26 起）。browser_egress sidecar
常驻 headless Chromium，页面内 fetch 完成 提交→轮询→取图，返回生成图字节。

支持多实例：`BFL2OPENAI_BROWSER_EGRESS` 用逗号分隔多个地址，按轮询分发。

换实例重试的条件分两类：
  * 可重试：传输层失败、503（熔断/繁忙）、**429（该实例的出口 IP 都打热了
    —— 换一台就是换一批 IP，实测这是唯一能救回配额的路径）**
  * 不重试：451（内容审核）、502（上游契约，如 composition 冲突）、
    504（上游超时，重试只会把 180s 再花一遍）
"""

import asyncio
import base64
import json
import os
import random
import time
import urllib.error
import urllib.request

_TIMEOUT_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_TIMEOUT", "180"))

_rr = 0
#: 每个进程一个随机相位。gunicorn 多 worker 各自持有 `_rr`，若都从 0 起算，
#: 每个 worker 的首发请求会全落到第一个实例（生产实测：2 次请求都在同一台）。
_SEED = random.randrange(1 << 20)

#: 实例级冷却：刚回过 429/503/传输失败的实例，短时间先不碰。
_COOLDOWN_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_COOLDOWN", "20"))
_avoid_until: dict[str, float] = {}


def _endpoints() -> list[str]:
    raw = os.environ.get("BFL2OPENAI_BROWSER_EGRESS", "")
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


class BrowserEgressError(RuntimeError):
    """sidecar 调用失败。

    `status` 取 sidecar 返回的 HTTP 码；0 = 传输层失败（连接/超时），
    200 = sidecar 回了 200 但内容不可用（缺 image / 非 JSON）。
    调用方据此把失败翻译成对客户端有意义的码，而不是一律 500 —— 500 会让
    客户端误判"服务崩了"并无效重试（生产实测：重试放大了 502 潮）。
    """

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def configured() -> bool:
    return bool(_endpoints())


def _order() -> list[str]:
    """本次调用的实例尝试顺序：进程内交替，进程间相位随机。

    冷却中的实例排到后面（不是直接剔除——全都冷却时总得试一个，
    直接失败会把"上游配额紧张"放大成"服务不可用"）。
    """
    global _rr
    urls = _endpoints()
    if not urls:
        return []
    start = (_SEED + _rr) % len(urls)
    _rr += 1
    order = [urls[(start + i) % len(urls)] for i in range(len(urls))]
    now = time.monotonic()
    return sorted(order, key=lambda u: _avoid_until.get(u, 0.0) > now)


def _mark_bad(url: str) -> None:
    _avoid_until[url] = time.monotonic() + _COOLDOWN_S


def reset_state() -> None:
    """测试用：清空游标与冷却表。"""
    global _rr, _SEED
    _rr = 0
    _SEED = 0
    _avoid_until.clear()


def _post(url: str, payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{url}/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=_TIMEOUT_S) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


async def generate(image_png: bytes, prompt: str, aspect_ratio: str,
                   resolution: str, mode: str, boxes_json: str | None = None,
                   references_json: str | None = None,
                   refs: list[bytes] | None = None) -> bytes:
    """经浏览器 sidecar 完成一次生成，返回图片字节。失败抛 BrowserEgressError。"""
    global _rr
    urls = _endpoints()
    if not urls:
        raise BrowserEgressError("browser egress not configured", 0)

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

    order = _order()
    last: BrowserEgressError | None = None
    for i, url in enumerate(order):
        final = i == len(order) - 1
        try:
            status, body = await asyncio.to_thread(_post, url, payload)
        except Exception as exc:  # noqa: BLE001 - 连接层失败，换实例
            _mark_bad(url)
            last = BrowserEgressError(f"browser egress transport({url}): {exc}", 0)
            if final:
                break
            continue
        if status in (429, 503) and not final:
            # 429：该实例 8 个出口 IP 全部打热；换一台就是换一批 IP。
            # 503：熔断/繁忙，另一实例的会话状态可能正常。
            _mark_bad(url)
            last = BrowserEgressError(
                f"browser egress {status}({url}): {body[:120]}", status)
            continue
        if status != 200:
            raise BrowserEgressError(f"browser egress {status}: {body[:200]}", status)
        try:
            doc = json.loads(body)
        except Exception as exc:  # noqa: BLE001
            raise BrowserEgressError(
                f"browser egress bad body: {body[:150]}", 200) from exc
        b64 = doc.get("image_b64")
        if not b64:
            raise BrowserEgressError(
                f"browser egress no image: {str(doc)[:150]}", 200)
        return base64.b64decode(b64)

    raise last or BrowserEgressError("browser egress: no endpoint tried", 0)
