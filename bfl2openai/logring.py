"""运行日志环形缓冲：面板「运行日志」的数据源。

为什么要双写
------------
gunicorn 起 6 个 worker，每个进程各有一份内存环。只读内存的话，面板的
请求随机落到哪个 worker，就只能看到那 1/6 的日志 —— 用户看到的现象是
"日志缺一段、时有时无"。所以：

* 内存 deque：Redis 不可用时的降级视图；也是写入的必经缓冲
* Redis list：所有 worker 汇成一条流（``LTRIM`` 保留最近 ``CAP`` 条）

写入不允许拖慢请求路径：``emit()`` 只做 deque 追加 + ``put_nowait``，
真正碰 Redis 的是独立 daemon 线程；Redis 挂了就丢日志，绝不抛异常。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from typing import Any

CAP = 1000
KEY = os.environ.get("BFL_LOG_KEY", "bfl:logs")

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_local: deque[dict[str, Any]] = deque(maxlen=CAP)
_q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=5000)
_installed = False
_writer: threading.Thread | None = None
_wlock = threading.Lock()


# ---------------------------------------------------------------- 写入

class RingHandler(logging.Handler):
    """把日志记录进内存环 + Redis 队列。"""

    #: 访问日志单独一条流（面板另有槽位/用量视图）——4000 单/天会把
    #: 1000 条的环迅速冲掉，把业务 WARNING/ERROR 挤没。
    _SKIP = ("uvicorn.access", "gunicorn.access")

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            if record.name in self._SKIP:
                return
            msg = record.getMessage()
            if len(msg) > 4000:
                msg = msg[:4000] + "…"
            entry = {
                "ts": round(record.created, 3),
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            }
            if record.exc_info:
                entry["msg"] = (entry["msg"] + " | "
                                + logging.Formatter().formatException(record.exc_info))[:4000]
            _local.append(entry)
            try:
                _q.put_nowait(entry)
            except queue.Full:  # 队列打满说明 Redis 长期不可用，丢新日志
                pass
        except Exception:  # noqa: BLE001 - 日志处理器绝不能影响业务
            self.handleError(record)


def _redis_client():
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    try:
        from redis import Redis  # 同步客户端，只在后台线程用
        return Redis.from_url(
            url, decode_responses=True,
            socket_timeout=1.5, socket_connect_timeout=1.5,
        )
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
        # 攒一批再写，避免每条日志一次 RTT
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
        except Exception:  # noqa: BLE001 - Redis 抖动：丢弃这批，下轮重连
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            c = None
            time.sleep(2.0)


def install(level: int = logging.INFO) -> None:
    """把 RingHandler 挂到 root logger。重复调用无副作用。"""
    global _installed, _writer
    with _wlock:
        if _installed:
            return
        _installed = True
        root = logging.getLogger()
        root.addHandler(RingHandler(level))
        # gunicorn 自己会重设 root.level；这里只保证不低于 INFO，避免把
        # 业务日志挡在处理器之前（debug 级不进面板，量太大）
        if root.level > level:
            root.setLevel(level)
        _writer = threading.Thread(target=_writer_loop, name="logring", daemon=True)
        _writer.start()


# ---------------------------------------------------------------- 读取

def _read_redis(limit: int) -> list[dict[str, Any]] | None:
    c = _redis_client()
    if c is None:
        return None
    try:
        raw = c.lrange(KEY, -limit, -1)
        out = []
        for line in reversed(raw):  # Redis 里旧的在前，面板要新的在前
            try:
                d = json.loads(line)
                if isinstance(d, dict) and "msg" in d:
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


def _filter(entries: list[dict[str, Any]], level: str, q: str) -> list[dict[str, Any]]:
    need = _LEVELS.get((level or "").upper(), 0)
    q = (q or "").strip().lower()
    out = []
    for e in entries:
        if need and _LEVELS.get(str(e.get("level", "")).upper(), 0) < need:
            continue
        if q:
            hay = f"{e.get('logger','')} {e.get('msg','')}".lower()
            if q not in hay:
                continue
        out.append(e)
    return out


def read(limit: int = 200, level: str = "", q: str = "") -> dict[str, Any]:
    """返回 ``{"entries": [...], "source": "redis|memory"}``，新的在前。

    Redis 里存的是全 worker 汇总；不可用时回落到本进程内存环（source 字段
    让前端可以提示"当前仅本 worker"）。
    """
    limit = max(1, min(int(limit or 200), CAP))
    entries = _read_redis(limit * 4 if (level or q) else limit)
    source = "redis"
    if entries is None:
        entries = list(_local)[-limit * 4:] if (level or q) else list(_local)[-limit:]
        entries.reverse()
        source = "memory"
    entries = _filter(entries, level, q)
    return {"entries": entries[:limit], "source": source, "cap": CAP}
