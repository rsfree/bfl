"""请求记录：面板「请求记录」表的数据源（参考 workbuddy2api 的 reqlog）。

与 logring 同一套双写策略：
* 内存 deque —— Redis 不可用时本进程最近记录（降级）
* Redis list（``LTRIM`` 1000）—— 6 个 worker 汇总成一条流

只记业务路径。面板自己每 5s/15s 轮询，全记会把 1000 条环冲成"全是 /pq"，
真正要看的图片请求反而留不住。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from typing import Any

CAP = 1000
KEY = os.environ.get("BFL_REQLOG_KEY", "bfl:reqlogs")

#: 运维面与探活：量太密且没有业务含义
_SKIP = ("/panel", "/pq", "/healthz", "/docs", "/redoc", "/openapi.json",
         "/favicon", "/metrics")

_local: deque[dict[str, Any]] = deque(maxlen=CAP)
_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=5000)
_started = False
_lock = threading.Lock()


def should_record(path: str) -> bool:
    return not any(path == p or path.startswith(p) for p in _SKIP)


def record(entry: dict[str, Any]) -> None:
    _local.append(entry)
    try:
        _q.put_nowait(entry)
    except queue.Full:
        pass


def _redis_client():
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    try:
        from redis import Redis
        return Redis.from_url(url, decode_responses=True,
                              socket_timeout=1.5, socket_connect_timeout=1.5)
    except Exception:  # noqa: BLE001
        return None


def _writer_loop() -> None:
    c = _redis_client()
    while True:
        try:
            first = _q.get()
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        batch = [first]
        deadline = time.time() + 0.2
        while len(batch) < 200 and time.time() < deadline:
            try:
                batch.append(_q.get_nowait())
            except queue.Empty:
                break
        if c is None:
            c = _redis_client()
            if c is None:
                time.sleep(5.0)
                continue
        try:
            pipe = c.pipeline(transaction=False)
            pipe.rpush(KEY, *[json.dumps(e, ensure_ascii=False) for e in batch])
            pipe.ltrim(KEY, -CAP, -1)
            pipe.execute()
        except Exception:  # noqa: BLE001
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            c = None
            time.sleep(2.0)


def _start() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_writer_loop, name="reqlog", daemon=True).start()


def _read_redis(limit: int) -> list[dict[str, Any]] | None:
    c = _redis_client()
    if c is None:
        return None
    try:
        raw = c.lrange(KEY, -limit, -1)
        out = []
        for line in reversed(raw):
            try:
                d = json.loads(line)
                if isinstance(d, dict) and "path" in d:
                    out.append(d)
            except (ValueError, TypeError):
                continue
        return out
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def read(limit: int = 200, status: str = "", q: str = "") -> dict[str, Any]:
    """``status``: "" | "error"（>=400） | "slow"（>=3000ms）。新的在前。"""
    limit = max(1, min(int(limit or 200), CAP))
    wide = limit * 4 if (status or q) else limit
    entries = _read_redis(wide)
    source = "redis"
    if entries is None:
        entries = list(_local)[-wide:]
        entries.reverse()
        source = "memory"
    q = (q or "").strip().lower()
    out = []
    for e in entries:
        code = int(e.get("status") or 0)
        if status == "error" and code < 400:
            continue
        if status == "slow" and int(e.get("ms") or 0) < 3000:
            continue
        if q and q not in f"{e.get('method','')} {e.get('path','')} {e.get('ip','')}".lower():
            continue
        out.append(e)
    return {"entries": out[:limit], "source": source, "cap": CAP}


def install(app) -> None:
    """挂请求记录中间件。只测耗时与状态，不消费请求体。"""
    from starlette.requests import Request

    _start()

    @app.middleware("http")
    async def _reqlog_mw(request: Request, call_next):
        start = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            if should_record(request.url.path):
                record({
                    "ts": round(time.time(), 3),
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "ms": int((time.perf_counter() - start) * 1000),
                    "ip": _client_ip(request),
                })
            raise
        if should_record(request.url.path):
            record({
                "ts": round(time.time(), 3),
                "method": request.method,
                "path": request.url.path,
                "status": resp.status_code,
                "ms": int((time.perf_counter() - start) * 1000),
                "ip": _client_ip(request),
            })
        return resp


def _client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return getattr(request.client, "host", "") or ""
