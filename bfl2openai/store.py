"""跨 worker 状态外置：Redis（REDIS_URL）或进程内回落。

多 gunicorn worker 时，任务表/上传句柄/组合态必须在进程间可见 —— 全走这里。
REDIS_URL 未设时用进程内 dict（惰性 TTL 清理），单 worker 与单测零依赖。

约定：所有接口 async；doc 存 JSON，blob 存原始字节；TTL 秒，None=不过期。
"""
from __future__ import annotations

import json
import os
import time

REDIS_URL = os.environ.get("REDIS_URL", "")

if REDIS_URL:
    from redis import asyncio as aioredis

    _r = aioredis.from_url(REDIS_URL, decode_responses=False)

# ---- 进程内回落 ----
_MEM: dict[str, tuple[bytes, float | None]] = {}
_ZSETS: dict[str, dict[str, float]] = {}
_last_sweep = 0.0


def _sweep() -> None:
    global _last_sweep
    now = time.time()
    if now - _last_sweep < 30:
        return
    _last_sweep = now
    for key in [k for k, (_, exp) in _MEM.items() if exp and exp < now]:
        _MEM.pop(key, None)


async def put(key: str, doc, ttl_s: int | None = None) -> None:
    data = json.dumps(doc).encode()
    if REDIS_URL:
        await _r.set(key, data, ex=ttl_s)
        return
    _sweep()
    _MEM[key] = (data, time.time() + ttl_s if ttl_s else None)


async def get(key: str):
    if REDIS_URL:
        data = await _r.get(key)
        return json.loads(data) if data else None
    _sweep()
    item = _MEM.get(key)
    if item is None:
        return None
    data, exp = item
    if exp and exp < time.time():
        _MEM.pop(key, None)
        return None
    return json.loads(data)


async def drop(key: str) -> None:
    if REDIS_URL:
        await _r.delete(key)
        return
    _MEM.pop(key, None)


async def put_blob(key: str, data: bytes, ttl_s: int | None = None) -> None:
    if REDIS_URL:
        await _r.set(key, data, ex=ttl_s)
        return
    _sweep()
    _MEM[key] = (data, time.time() + ttl_s if ttl_s else None)


async def get_blob(key: str) -> bytes | None:
    if REDIS_URL:
        return await _r.get(key)
    _sweep()
    item = _MEM.get(key)
    if item is None:
        return None
    data, exp = item
    if exp and exp < time.time():
        _MEM.pop(key, None)
        return None
    return data


async def zadd(setkey: str, member: str, score: float) -> None:
    if REDIS_URL:
        await _r.zadd(setkey, {member: score})
        return
    _ZSETS.setdefault(setkey, {})[member] = score


async def zrem(setkey: str, member: str) -> None:
    if REDIS_URL:
        await _r.zrem(setkey, member)
        return
    _ZSETS.get(setkey, {}).pop(member, None)


async def zcard(setkey: str) -> int:
    if REDIS_URL:
        return await _r.zcard(setkey)
    return len(_ZSETS.get(setkey, {}))


async def zrevrange(setkey: str, count: int) -> list[str]:
    """按 score 降序取前 count 个 member。"""
    if REDIS_URL:
        return [m.decode() if isinstance(m, bytes) else m
                for m in await _r.zrevrange(setkey, 0, count - 1)]
    items = sorted(_ZSETS.get(setkey, {}).items(), key=lambda kv: -kv[1])
    return [m for m, _ in items[:count]]
