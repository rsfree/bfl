"""跨 worker 状态外置：Redis（REDIS_URL）或进程内回落。

多 gunicorn worker 时，任务表/上传句柄/组合态必须在进程间可见 —— 全走这里。
REDIS_URL 未设时用进程内 dict（惰性 TTL 清理），单 worker 与单测零依赖。

约定：所有接口 async；doc 存 JSON，blob 存原始字节；TTL 秒，None=不过期。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

REDIS_URL = os.environ.get("REDIS_URL", "")

if REDIS_URL:
    from redis import asyncio as aioredis

    _r = aioredis.from_url(REDIS_URL, decode_responses=False)



# ---- 大对象落盘（Redis 只存引用）----
# 为什么：幂等缓存存的是整个响应 JSON，含 b64 图后单条约 1.7MB。Redis 配的是
# maxmemory 256MB + volatile-ttl，一打满就开始淘汰 key（实测已淘汰 1800+），
# 把任务状态、槽位记录一起挤出去。图片放文件系统、Redis 只留一个路径。
_SPILL_DIR = os.environ.get("BFL2OPENAI_SPILL_DIR", "/tmp/bfl-spill")
_SPILL_THRESHOLD = int(
    os.environ.get("BFL2OPENAI_SPILL_THRESHOLD_BYTES", str(256 * 1024)))
_SPILL_MAX_MB = int(os.environ.get("BFL2OPENAI_SPILL_MAX_MB", "2048"))
_SPILL_SWEEP_S = 60.0
_last_spill_sweep = 0.0
_SPILL_MARK = "__spill__"


def _spill_path(key: str, kind: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:32]
    return os.path.join(_SPILL_DIR, f"{h}.{kind}")


def _spill_write(key: str, data: bytes, ttl_s: int | None, kind: str) -> dict:
    os.makedirs(_SPILL_DIR, exist_ok=True)
    path = _spill_path(key, kind)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)          # 原子替换：并发写同一 key 不会读到半个文件
    exp = time.time() + ttl_s if ttl_s else 0.0
    with open(path + ".exp", "w") as f:
        f.write(str(exp))
    return {_SPILL_MARK: path, "__exp__": exp, "__kind__": kind}


def _spill_read(ref: str) -> bytes | None:
    try:
        with open(ref, "rb") as f:
            return f.read()
    except OSError:
        return None


def _spill_sweep(force: bool = False) -> None:
    """清过期文件 + 控总容量。失败一律忽略：缓存丢了只是少一次幂等命中。"""
    global _last_spill_sweep
    now = time.time()
    if not force and now - _last_spill_sweep < _SPILL_SWEEP_S:
        return
    _last_spill_sweep = now
    try:
        entries = []
        total = 0
        with os.scandir(_SPILL_DIR) as it:
            for e in it:
                if not e.name.endswith((".doc", ".blob")):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                try:
                    with open(e.path + ".exp") as f:
                        exp = float(f.read() or 0)
                except (OSError, ValueError):
                    exp = 0.0
                if exp and exp < now:
                    for suf in ("", ".exp"):
                        try:
                            os.remove(e.path + suf)
                        except OSError:
                            pass
                    continue
                entries.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
        cap = _SPILL_MAX_MB * 1024 * 1024
        if total > cap:
            for _, size, path in sorted(entries):
                if total <= cap:
                    break
                for suf in ("", ".exp"):
                    try:
                        os.remove(path + suf)
                    except OSError:
                        pass
                total -= size
    except OSError:
        pass


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


def _deref(doc):
    """把落盘引用还原成原始文档；文件丢了就当未命中（缓存失效，不是错误）。"""
    if isinstance(doc, dict) and doc.get(_SPILL_MARK):
        raw = _spill_read(doc[_SPILL_MARK])
        return json.loads(raw) if raw is not None else None
    return doc


async def put(key: str, doc, ttl_s: int | None = None) -> None:
    data = json.dumps(doc).encode()
    if len(data) > _SPILL_THRESHOLD:
        # 超阈值：内容进文件，Redis 只留引用（几十字节）
        data = json.dumps(_spill_write(key, data, ttl_s, "doc")).encode()
        _spill_sweep()
    if REDIS_URL:
        await _r.set(key, data, ex=ttl_s)
        return
    _sweep()
    _MEM[key] = (data, time.time() + ttl_s if ttl_s else None)


async def get(key: str):
    if REDIS_URL:
        data = await _r.get(key)
        doc = json.loads(data) if data else None
        return _deref(doc)
    _sweep()
    item = _MEM.get(key)
    if item is None:
        return None
    data, exp = item
    if exp and exp < time.time():
        _MEM.pop(key, None)
        return None
    return _deref(json.loads(data))


async def set_nx(key: str, doc, ttl_s: int) -> bool:
    """原子占位：只有第一个调用者拿到 True（探针跨 worker 去重用）。

    Redis 用 SET NX EX；内存回落用「存在且未过期即失败」。
    """
    data = json.dumps(doc).encode()
    if REDIS_URL:
        return bool(await _r.set(key, data, ex=ttl_s, nx=True))
    _sweep()
    item = _MEM.get(key)
    if item is not None:
        _, exp = item
        if exp is None or exp > time.time():
            return False
        _MEM.pop(key, None)
    _MEM[key] = (data, time.time() + ttl_s if ttl_s else None)
    return True


async def mget(keys: list[str]) -> list:
    """批量读：一次 RTT 取多个 key。

    `get_result` 每轮要查 comp / bridge / bridge_done 三个键，高频轮询下
    3 次 RTT 变 1 次（2026-11-05）。返回与 keys 等长，缺失或过期即 None。
    """
    if REDIS_URL:
        rows = await _r.mget(keys)
        return [_deref(json.loads(r)) if r else None for r in rows]
    _sweep()
    now = time.time()
    out = []
    for key in keys:
        item = _MEM.get(key)
        if item is None:
            out.append(None)
            continue
        data, exp = item
        if exp and exp < now:
            _MEM.pop(key, None)
            out.append(None)
            continue
        out.append(_deref(json.loads(data)))
    return out


async def drop(key: str) -> None:
    for kind in ("doc", "blob"):
        path = _spill_path(key, kind)
        for suf in ("", ".exp"):
            try:
                os.remove(path + suf)
            except OSError:
                pass
    if REDIS_URL:
        await _r.delete(key)
        return
    _MEM.pop(key, None)


async def put_blob(key: str, data: bytes, ttl_s: int | None = None) -> None:
    if len(data) > _SPILL_THRESHOLD:
        # 原始图片字节同样不该进 Redis（单张 ~1.7MB）
        data = json.dumps(_spill_write(key, data, ttl_s, "blob")).encode()
        _spill_sweep()
    if REDIS_URL:
        await _r.set(key, data, ex=ttl_s)
        return
    _sweep()
    _MEM[key] = (data, time.time() + ttl_s if ttl_s else None)


async def get_blob(key: str) -> bytes | None:
    if REDIS_URL:
        data = await _r.get(key)
    else:
        _sweep()
        item = _MEM.get(key)
        if item is None:
            return None
        data, exp = item
        if exp and exp < time.time():
            _MEM.pop(key, None)
            return None
    if data and data.startswith(b'{"__spill__'):
        try:
            return _spill_read(json.loads(data)[_SPILL_MARK])
        except (ValueError, KeyError):
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
