"""浏览器出口客户端：curl_cffi 被上游指纹识别（403 Access denied）时的兜底。

上游 flux-tools 只放行真实浏览器环境（2026-10-26 起）。browser_egress sidecar
常驻 headless Chromium，页面内 fetch 完成 提交→轮询→取图，返回生成图字节。

支持多实例：`BFL2OPENAI_BROWSER_EGRESS` 用逗号分隔多个地址，按轮询分发。

换实例重试的条件分两类：
  * 可重试：传输层失败、500（sidecar 内部超时/页面崩溃）、503（rebind 超时/
    熔断/繁忙）、**429（该实例的出口 IP 都打热了 —— 换一台就是换一批 IP，
    实测这是唯一能救回配额的路径）**
  * 不重试：451（内容审核）、502（上游契约，如 composition 冲突）、
    504（上游超时，重试只会把 180s 再花一遍）

2026-11-21 加固：
  * 所有重试都受调用方的 `deadline` 约束（墙钟预算），预算不足时立即收手，
    而不是把 3 个实例各拖 180s、最后撞 nginx 300s 超时变成 HTML 504。
  * 实例级熔断：连续 N 次传输/5xx 失败后短时绕开该实例（不再靠每次
    请求现撞 180s 才发现它挂了）。全部实例都熔断时用短超时做半开探测。
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

#: 单次尝试的最小可用预算：低于这个值再试一次也只是白白占着连接。
_MIN_ATTEMPT_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_MIN_ATTEMPT", "25"))

#: 实例级熔断：连续失败达到阈值 → 冷却期内排到最后并缩到探测超时。
_BREAKER_FAILS = int(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_BREAKER_FAILS", "2"))
_BREAKER_OPEN_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_BREAKER_OPEN", "120"))
_BREAKER_PROBE_S = float(os.environ.get("BFL2OPENAI_BROWSER_EGRESS_BREAKER_PROBE", "20"))
_fail_streak: dict[str, int] = {}
_open_until: dict[str, float] = {}

#: 进程内累计（面板/日志用；重启清零，跨进程口径看 Redis 指标）。
_stats: dict[str, int] = {
    "calls": 0, "attempts": 0, "failovers": 0,
    "breaker_trips": 0, "deadline_hits": 0,
}

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


def _order(proxy_slot: int | None = None) -> list[str]:
    """本次调用的实例尝试顺序。

    给了 `proxy_slot` 就按 `slot % 实例数` 做**亲和**：该槽位被预绑定在
    哪台侧车上，请求就去哪台，命中热页面（省掉 ~2s 的 context 重建）。
    没给槽位时退回原来的进程内交替 + 进程间随机相位。

    冷却中的实例排到后面（不是直接剔除——全都冷却时总得试一个，
    直接失败会把"上游配额紧张"放大成"服务不可用"）。
    """
    global _rr
    urls = _endpoints()
    if not urls:
        return []
    if proxy_slot is not None:
        head = proxy_slot % len(urls)
        order = [urls[(head + i) % len(urls)] for i in range(len(urls))]
    else:
        start = (_SEED + _rr) % len(urls)
        _rr += 1
        order = [urls[(start + i) % len(urls)] for i in range(len(urls))]
    now = time.monotonic()
    # 两级排序：熔断中的实例最后试，其次是冷却中的。两者都命中时仍保留
    # 原顺序 —— 全部不可用时总得试一个，直接失败会把「入口紧张」放大成 503。
    return sorted(order, key=lambda u: (
        1 if _open_until.get(u, 0.0) > now else 0,
        1 if _avoid_until.get(u, 0.0) > now else 0))


def _mark_bad(url: str) -> None:
    _avoid_until[url] = time.monotonic() + _COOLDOWN_S


def _note_ok(url: str) -> None:
    _fail_streak.pop(url, None)
    _open_until.pop(url, None)


def _note_fail(url: str) -> None:
    """记一次实例级失败；连续失败到阈值就把该实例熔断一段时间。"""
    n = _fail_streak.get(url, 0) + 1
    _fail_streak[url] = n
    if _BREAKER_FAILS > 0 and n >= _BREAKER_FAILS:
        _open_until[url] = time.monotonic() + _BREAKER_OPEN_S
        _fail_streak[url] = 0
        _stats["breaker_trips"] += 1


def reset_state() -> None:
    """测试用：清空游标、冷却表与熔断状态。"""
    global _rr, _SEED
    _rr = 0
    _SEED = 0
    _avoid_until.clear()
    _fail_streak.clear()
    _open_until.clear()
    for k in _stats:
        _stats[k] = 0


def stats() -> dict:
    """面板/排查用：每个实例的熔断与冷却剩余时间 + 进程内累计。"""
    now = time.monotonic()
    return {
        "endpoints": [{
            "url": u,
            "open_s_left": round(max(0.0, _open_until.get(u, 0.0) - now), 1),
            "cooling_s_left": round(max(0.0, _avoid_until.get(u, 0.0) - now), 1),
            "fail_streak": _fail_streak.get(u, 0),
        } for u in _endpoints()],
        **dict(_stats),
    }


def _post(url: str, payload: dict,
          timeout: float | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{url}/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=_TIMEOUT_S if timeout is None else timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


async def generate(image_png: bytes, prompt: str, aspect_ratio: str,
                   resolution: str, mode: str, boxes_json: str | None = None,
                   references_json: str | None = None,
                   refs: list[bytes] | None = None,
                   ref_mode: str | None = None,
                   proxy_slot: int | None = None,
                   deadline: float | None = None) -> bytes:
    """经浏览器 sidecar 完成一次生成，返回图片字节。失败抛 BrowserEgressError。

    `ref_mode`：
      None/"multi" -> references(复数) 文件部件，composition 语义（默认）
      "single"     -> reference(单数) 文件部件 + references 为 id 数组，
                      官网 precise-editing 分支：canvas 保留为 image 部件，
                      mode 应为 i2i，素材不被当作画布参与重绘。
    """
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
        "ref_mode": ref_mode,
        # 2090 pin 模式：指定本次必须走哪个出口槽位。给了就必须钉死，
        # 否则「这次成功算到哪个 IP 头上」会失真，配额就白记了。
        "proxy_slot": proxy_slot,
    }

    order = _order(proxy_slot)
    start = time.monotonic()
    # 没给 deadline 时按「每个实例都用满超时」退化，保持旧行为（单测/直调）。
    if deadline is None:
        deadline = start + _TIMEOUT_S * max(1, len(urls))
    all_open = all(_open_until.get(u, 0.0) > start for u in order)
    _stats["calls"] += 1

    last: BrowserEgressError | None = None
    for i, url in enumerate(order):
        final = i == len(order) - 1
        remaining = deadline - time.monotonic()
        if last is not None and remaining < _MIN_ATTEMPT_S:
            # 预算见底：再试也只是把连接拖到 nginx 300s，直接带着最后一次
            # 的真实原因返回，让上层决定 429/504。
            _stats["deadline_hits"] += 1
            break
        attempt_timeout = min(_TIMEOUT_S, max(_MIN_ATTEMPT_S, remaining))
        if all_open:
            # 全部实例都在熔断中：半开探测，短超时快速判断，不占满预算。
            attempt_timeout = min(attempt_timeout, _BREAKER_PROBE_S)
        _stats["attempts"] += 1
        try:
            status, body = await asyncio.to_thread(
                _post, url, payload, attempt_timeout)
        except Exception as exc:  # noqa: BLE001 - 连接层失败，换实例
            _mark_bad(url)
            _note_fail(url)
            last = BrowserEgressError(f"browser egress transport({url}): {exc}", 0)
            if final:
                break
            _stats["failovers"] += 1
            continue
        if status == 200:
            _note_ok(url)
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
        if status == 503 and "rebind" in body:
            # 槽位级失败：该 pin 槽位的代理链路/页面状态坏了。换实例没意义
            # —— 新实例仍要重建同一个槽位，只会把同样的超时再花两遍。
            # 直接上行，让 server 换一个槽位（rebind 的真实处置在那一层）。
            raise BrowserEgressError(
                f"browser egress 503({url}): {body[:200]}", 503)
        if status in (429, 503) and not final:
            # 429：该实例的出口 IP 全部打热；换一台就是换一批 IP。
            # 503：熔断/繁忙，另一实例的会话状态可能正常。
            _mark_bad(url)
            _note_fail(url)
            last = BrowserEgressError(
                f"browser egress {status}({url}): {body[:120]}", status)
            _stats["failovers"] += 1
            continue
        if status == 500 and not final \
                and deadline - time.monotonic() > _MIN_ATTEMPT_S:
            # sidecar 内部 500（多为页面 TimeoutError）：另一个实例可能是好的。
            # 只在预算还够一次尝试时才重试 —— 否则等于把 165s 再花一遍。
            _mark_bad(url)
            _note_fail(url)
            last = BrowserEgressError(
                f"browser egress 500({url}): {body[:120]}", 500)
            _stats["failovers"] += 1
            continue
        if status >= 500:
            _note_fail(url)
        raise BrowserEgressError(f"browser egress {status}: {body[:200]}", status)

    raise last or BrowserEgressError("browser egress: no endpoint tried", 0)
