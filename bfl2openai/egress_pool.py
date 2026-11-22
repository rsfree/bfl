"""出口槽位注册表：2090 pin 模式下的「账号=槽位 → 固定出口 IP」调度。

为什么需要它
------------
上游匿名额度是**按出口 IP** 计的（`You've used today's 30 free generations`）。
2088 轮换模式下每个连接都换 IP，系统里没有任何地方知道「某个 IP 已经用掉
28 次」，于是要么提前把 IP 打死（429 风暴），要么不敢用（浪费容量）。

2090 把「账号」变成可寻址的出口：`poolclient-{n}` 恒定落到同一个 IP。
于是计数键可以定成 IP，配额才能落在 IP 上。

三条硬约束
----------
1. **计数键是 IP，不是槽位**。实测 40 个账号 → 39 个不同 IP，存在碰撞
   （`acct-100` 与 `acct-003` 同为 142.4.122.45）。按槽位计数会让共享同一
   IP 的两个槽位各记一份配额，实际把那个 IP 打成两倍。
2. **碰撞在建映射时就消除**。同 IP 的第二个槽位标 `dup` 且不参与调度——
   它和后一个槽位共享同一份配额，留着只会误导路由。
3. **429 立即换槽**。429 的语义是「这个 IP 短期不可用了」，继续用同一槽位
   重试只会把上游的限流窗口越拖越长。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Iterable
from urllib.parse import quote

#: 上游免费额度的硬上限。任何配置都不得越过它 —— 越过也不会多出图，
#: 只会换来 429 和更长的冷却。
MAX_DAILY_QUOTA = 30

#: 每 IP 每日额度，可配置但**被 MAX_DAILY_QUOTA 夹住**。
DEFAULT_QUOTA = int(os.environ.get("BFL2OPENAI_POOL_QUOTA", "30"))

#: 撞 429 后的冷却。上游既有「短窗口频率限制」也有「当日额度耗尽」，
#: 单看状态码无法区分，只能给一个中等值：太短会反复撞墙，太长会白闲置。
DEFAULT_429_COOLDOWN_S = int(os.environ.get("BFL2OPENAI_POOL_429_COOLDOWN", "600"))

#: 候选随机度：在最老的 N 个段 / N 个槽位之间随机挑，而不是只挑"唯一最老"的那一个。
#:
#: 为什么必须随机：原实现严格取 last_used 唯一的全局最小值，而多个并发请求会
#: 拿到**同一个**候选（`last_used` 要等生成完成才更新，约 20s 后才变），
#: 于是全线挤在同一个 IP 上抢租约，抢不到的就报"无可用槽位"——
#: 实测 234 个槽位空闲时仍在刷 429。摊开到 N 个候选后，并发请求自然落到不同 IP。
#:
#: 32 而不是 8：最大段 `38.6.213.0/24` 独占 220 个 IP，取最老的 8 个仍会被
#: 20 并发一次抢空；32 个候选足以覆盖实际并发（MAX_INFLIGHT=20）。
POOL_SPREAD = max(1, int(os.environ.get("BFL2OPENAI_POOL_SPREAD", "32")))

#: 同一出口两次提交的**最小间隔**（秒）。
#:
#: 上游除日额度外还有细粒度频控：实测单 IP 超过约 3.5 发/分钟就被边缘限速
#: （即间隔 < 17s）。这里把间隔压在自己的调度层，主动不去撞阈值，
#: 而不是撞了 429 再冷却——后者会浪费掉本可出图的额度。
#: 0 = 关闭（退化为纯 LRU 轮询）。
MIN_GAP_S = float(os.environ.get("BFL2OPENAI_POOL_MIN_GAP", "18"))

#: 建映射时探测出口 IP 的并发度。探测只打 ipify，不消耗上游额度。
SCAN_CONCURRENCY = int(os.environ.get("BFL2OPENAI_POOL_SCAN_CONCURRENCY", "16"))

#: 定时重建槽位→IP 映射。默认 12h。IP 是租来的、会漂：旧表放三天等于拿一批
#: 可能已经失效的出口继续打（2026-11-21 实测 built_at 还是 11-18 部署那次）。
#: 0 = 关闭；``BFL2OPENAI_POOL_SCAN_ENABLE=0`` 也可关。
SCAN_INTERVAL_S = float(os.environ.get("BFL2OPENAI_POOL_SCAN_INTERVAL", "43200"))
SCAN_ENABLE = os.environ.get("BFL2OPENAI_POOL_SCAN_ENABLE", "1") == "1"
#: 首次扫描的最短延迟：给页面池/业务请求让路，别在部署后立刻抢 CPU。
SCAN_STARTUP_DELAY_S = float(
    os.environ.get("BFL2OPENAI_POOL_SCAN_STARTUP_DELAY", "600"))
_scan_task: asyncio.Task | None = None
log = logging.getLogger("bfl2openai.egress_pool")

#: 上游按「天」计额度，换日时区未公开 —— **2026-11-18 已实测确定为 UTC 整点**。
#:
#: 实测方法：node-4 上用 `/opt/burn/reset_probe.sh` 每 6 分钟 pin 同一出口 IP
#: 提交一次，记录 HTTP 码，共 103 条无缺口。结果：
#:   - 11-18 07:54:01(+08) 仍 429「today's 30 free generations」，
#:   - 11-18 08:00:01(+08) 首条 200（22 秒后写入 RESET_AT=08:00:23）。
#: 边界被夹在 (07:54, 08:00] 这六分钟内，区间内唯一整点边界是 08:00 = UTC 00:00。
#: 同批数据排除了北京零点（00:00:01 仍 429）与柏林零点（07:00:01 仍 429）；
#: 美国各时区零点落在北京 12:00–18:00，与美国口径也不符。
#:
#: 结论：`DAILY_RESET_TZ` 保持 UTC（= 北京每日 08:00，北京无夏令时故全年恒定）。
#: 改这个值前请重跑上述探针，不要凭印象调整。
DAILY_RESET_TZ = os.environ.get("BFL2OPENAI_POOL_TZ", "UTC")

_DAY_TTL_S = 3 * 24 * 3600
_PREFIX = "egress:v2"

STATE_PRIMARY = "primary"     # 唯一占用该 IP 的槽位，可参与调度
STATE_DUP = "dup"             # 该 IP 已被别的槽位占用，不参与调度
STATE_ERROR = "error"         # 探测失败，未建映射


class PoolNotConfigured(RuntimeError):
    """未配置 2090 池；调用方应回落到原有 2088 行为。"""


# --------------------------------------------------------------- 配置

def _today() -> str:
    """当前「账本日」，按 DAILY_RESET_TZ 计算（默认 UTC）。

    换日只重置 `used` 与 429 计数；`n403` 是出口被封的历史，跨日仍然有效。
    """
    try:
        tz = ZoneInfo(DAILY_RESET_TZ)
    except Exception:  # noqa: BLE001 - 时区名写错不该把服务打挂
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%Y-%m-%d")


def next_reset_at(now: float | None = None) -> float:
    """下一个换日时刻（epoch 秒）= DAILY_RESET_TZ 的下一个本地零点。

    面板拿它做倒计时。时区名解析失败时回落 UTC —— 必须与 `_today()` 同一口径，
    否则会出现「账本已换日、倒计时还在等」的错位，运维会据此误判。
    """
    try:
        tz = ZoneInfo(DAILY_RESET_TZ)
    except Exception:  # noqa: BLE001 - 与 _today() 保持一致，不抛
        tz = ZoneInfo("UTC")
    cur = datetime.fromtimestamp(time.time() if now is None else now, tz)
    nxt = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                            microsecond=0)
    return nxt.timestamp()


def _prefix(ip: str, octets: int = 3) -> str:
    """/24 前缀（IPv4 取前三段）。非 IPv4 原样返回，交由调用方当独立段处理。

    为什么按段分组：实测 1,278 个槽位只落进 31 个 /24，其中 `38.6.213.0/24`
    占 220 个 IP（42%）。同一 C 段的 IP 在风控眼里高度相关，单靠「换 IP」
    并不等于「换身份」——必须让相邻请求尽量落在不同段上。
    """
    parts = ip.split(".")
    if len(parts) == 4:
        return ".".join(parts[:octets])
    return ip


def _cfg() -> dict[str, Any]:
    # 每次调用读 env：模块级常量在 import 时就固化了，测试与热改配置都会失真
    quota = min(int(os.environ.get("BFL2OPENAI_POOL_QUOTA", DEFAULT_QUOTA)),
                MAX_DAILY_QUOTA)
    raw_soft = os.environ.get("BFL2OPENAI_POOL_SOFT_QUOTA", "").strip()
    # 停手点缺省等于额度（榨干模式）；显式配小才留风控余量，且不得高于额度。
    soft = min(int(raw_soft), quota) if raw_soft else quota
    return {
        "host": os.environ.get("BFL2OPENAI_POOL_HOST", ""),
        "port": int(os.environ.get("BFL2OPENAI_POOL_PORT", "2090")),
        "password": os.environ.get("BFL2OPENAI_POOL_PASS", ""),
        "user_prefix": os.environ.get("BFL2OPENAI_POOL_USER_PREFIX", "poolclient"),
        "slots": int(os.environ.get("BFL2OPENAI_POOL_SLOTS", "0")),
        "quota": quota,
        "soft_quota": soft,
        #: 停手点用尽后是否允许继续吃到硬额度。
        #: 榨干模式下 soft == quota，此开关无额外作用；仅当显式把 soft 配小
        #: 又想在池子见底时继续出图，才需要打开。
        "stretch": os.environ.get("BFL2OPENAI_POOL_STRETCH", "0") == "1",
    }


def _has_creds() -> bool:
    """只判断"有没有池凭证"，与全局开关无关（扫描/灰度用）。"""
    c = _cfg()
    return bool(c["host"] and c["password"])


def _scan_slots() -> int:
    """扫描号段上限。与全局开关解耦：全局关闭时仍要能重建映射给灰度用。"""
    # 注意 "0" 是 truthy：必须先转 int 再判空，否则配置为空时会拿到 0
    raw = int(os.environ.get("BFL2OPENAI_POOL_SCAN_SLOTS") or 0)
    return raw or _cfg()["slots"] or 3000


def configured() -> bool:
    c = _cfg()
    return bool(c["host"] and c["password"] and c["slots"] > 0)


#: 就绪状态缓存：True 一旦成立就不再回查（索引只在 scan 时变），
#: False 缓存 10s，避免每个请求都打一次 Redis。
_ready_cache: dict[str, float] = {"ok": 0.0, "ts": 0.0}


async def ready() -> bool:
    """注册表是否真的可用 —— 必须已经完成过一次扫描（存在主槽位索引）。

    为什么必须单独判：`configured()` 只看环境变量。如果按 env 配好了
    2090 但**还没跑 scan**，索引为空 ⇒ 所有槽位都没有 IP ⇒ `acquire()`
    恒返回 None ⇒ 每个请求都被映射成 429，整站瞬间不可用。
    这是部署顺序错误，不是流量问题；入口必须拦住并回落到老路径。
    """
    if _ready_cache["ok"]:
        return True
    now = time.time()
    if _ready_cache["ts"] and now - _ready_cache["ts"] < 10:
        return False
    meta = await _b().hgetall(_meta_key()) or {}
    ok = bool(meta.get("index"))
    if ok:
        _ready_cache["ok"] = 1.0
    else:
        _ready_cache["ts"] = now
    return ok


def slot_username(slot: int) -> str:
    return f"{_cfg()['user_prefix']}-{slot:03d}"


def slot_proxy_url(slot: int) -> str:
    """构造该槽位的 socks5h 代理 URL（含认证）。调用方负责不要把它写进日志。"""
    c = _cfg()
    return (f"socks5h://{quote(slot_username(slot), safe='')}:"
            f"{quote(c['password'], safe='')}@{c['host']}:{c['port']}")


# --------------------------------------------------------------- 存储

class _Memory:
    """进程内实现。仅用于单测与未配 Redis 的单 worker 场景。"""

    def __init__(self) -> None:
        self.h: dict[str, dict[str, str]] = {}
        self.lock = asyncio.Lock()

    async def hgetall(self, key: str) -> dict[str, str]:
        async with self.lock:
            return dict(self.h.get(key, {}))

    async def hset(self, key: str, mapping: dict[str, Any] | None = None,
                    **kw: Any) -> None:
        # 签名与 redis-py 对齐：mapping 是关键字参数。用位置参数传 dict 在
        # 真实 Redis 上会变成 key，抛 DataError（生产踩过）。
        if mapping is None:
            mapping = kw
        async with self.lock:
            d = self.h.setdefault(key, {})
            for k, v in mapping.items():
                d[k] = str(v)

    async def hincrby(self, key: str, field: str, delta: int) -> int:
        async with self.lock:
            d = self.h.setdefault(key, {})
            d[field] = str(int(d.get(field, 0)) + delta)
            return int(d[field])

    async def expire(self, key: str, ttl: int) -> None:  # 内存模式不做过期
        return None


_backend: _Memory | None = None
_redis = None


def _b():
    global _backend, _redis
    if _redis is not None or _backend is not None:
        return _redis or _backend
    url = os.environ.get("REDIS_URL", "")
    if url:
        from redis import asyncio as aioredis

        # 显式超时 + 有限重试：redis-py 默认 socket_connect_timeout=None，
        # 容器内 DNS（redis 这个服务名）偶发抖动时会一直等，实测面板
        # /pq/snapshot 出现过 12s 挂起与 TimeoutError 500。宁可快失败。
        kw = dict(decode_responses=True, socket_timeout=2.0,
                  socket_connect_timeout=2.0, health_check_interval=15)
        try:
            from redis.asyncio.retry import Retry
            from redis.backoff import ExponentialWithJitterBackoff
            kw["retry"] = Retry(ExponentialWithJitterBackoff(base=0.05, cap=0.3), 1)
        except Exception:  # noqa: BLE001 - 老版本 redis-py 没有 retry API
            pass
        _redis = aioredis.from_url(url, **kw)
        return _redis
    _backend = _Memory()
    return _backend


def reset_backend() -> None:
    """测试用：丢弃已建连接/内存态，并清掉就绪缓存。"""
    global _backend, _redis
    _backend = None
    _redis = None
    _ready_cache.update(ok=0.0, ts=0.0)


def _slot_key(slot: int) -> str:
    return f"{_PREFIX}:slot:{slot:03d}"


def _meta_key() -> str:
    return f"{_PREFIX}:meta"


# --------------------------------------------------------------- 建映射

#: 扫描专用线程池。
#: 不能用 asyncio.to_thread：它走事件循环的默认执行器（max_workers 约等于
#: CPU+4，本机 8 核≈12），SCAN_CONCURRENCY 调到 48 也只会有 12 个探测真正
#: 并行 —— 实测 12000 个槽位扫了 20 分钟还没扫完。探测是纯 I/O，线程数按
#: 并发上限给足才有意义。
_PROBE_EX = None


def _probe_executor():
    global _PROBE_EX
    if _PROBE_EX is None:
        from concurrent.futures import ThreadPoolExecutor
        _PROBE_EX = ThreadPoolExecutor(
            max_workers=max(8, SCAN_CONCURRENCY), thread_name_prefix="probe")
    return _PROBE_EX


async def _probe_ip(slot: int, *, timeout: float = 12.0) -> str | None:
    """经该槽位取一次出口 IP。失败返回 None。

    用 curl_cffi（已是运行时依赖）而不是 subprocess curl：主镜像里没有 curl。
    """
    from curl_cffi import requests as creq

    proxies = {"https": slot_proxy_url(slot), "http": slot_proxy_url(slot)}
    try:
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(
            _probe_executor(),
            lambda: creq.get("https://api.ipify.org", proxies=proxies, timeout=timeout))
        if r.status_code == 200:
            ip = (r.text or "").strip()
            return ip if ip and len(ip) <= 45 else None
    except Exception:  # noqa: BLE001 - 探测失败即标记 error，不阻断其它槽位
        return None
    return None


def _owner_id() -> str:
    """扫描归属标识：`<容器 hostname>:<worker pid>`。

    Docker 默认把容器 ID 作为 hostname，容器重建后必然变化；因此这个标识
    同时能识别「另一个 worker 在扫」与「上一个容器的尸检标志」。
    """
    return f"{socket.gethostname()}:{os.getpid()}"


def _owner_alive(owner: str) -> bool:
    """归属的扫描进程是否还活着。

    没有 owner 字段（旧版本写入的数据）时退回 True —— 让原来的 30 分钟
    超时规则继续兜底。
    """
    if not owner:
        return True
    host, _, pid_s = owner.rpartition(":")
    if host != socket.gethostname():
        return False            # 容器已重建，旧进程不可能还活着
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True             # 进程存在，只是不归当前用户
    except OSError:
        return False


async def scan(slots: Iterable[int] | None = None) -> dict[str, Any]:
    """探测槽位出口 IP 并**就地消除碰撞**，结果写入注册表。

    - 同一 IP 只保留编号最小的槽位为 `primary`，其余标 `dup`
    - 探测失败标 `error`（保留配额字段，便于人工重扫）
    - 已存在当日用量的槽位**不清零**（重扫不能把已消耗的额度抹掉）
    """
    if not _has_creds():
        raise PoolNotConfigured("BFL2OPENAI_POOL_HOST / _PASS 未配置")
    b0 = _b()
    st = await b0.hgetall(_meta_key()) or {}
    # 扫描状态必须放 Redis：gunicorn 6 个 worker，进程内变量互相看不见，
    # 面板会显示"没在扫"而实际正在扫（或反之）。
    age = time.time() - float(st.get("scan_started", 0) or 0)
    if st.get("scan_running") == "1" and age < 1800:
        owner = st.get("scan_owner", "")
        if _owner_alive(owner):
            return {"running": True, "skipped": True}
        # 归属进程已死（worker 被 max_requests 回收 / 容器重建）：清掉僵死
        # 标志继续扫。2026-11-22 08:13 那次就是这样被拖到 10:34 的。
        log.warning("clearing zombie scan flag owner=%s age=%.0fs", owner, age)
    await b0.hset(_meta_key(), mapping={
        "scan_running": 1, "scan_started": int(time.time()),
        "scan_owner": _owner_id(), "scan_error": ""})
    try:
        return await _scan_impl(slots)
    except BaseException as exc:  # noqa: BLE001 - 记录后再抛，面板才看得到原因
        await b0.hset(_meta_key(),
                      mapping={"scan_error": f"{type(exc).__name__}: {exc}"[:300]})
        raise
    finally:
        await b0.hset(_meta_key(), mapping={
            "scan_running": 0, "scan_finished": int(time.time())})


async def scan_status() -> dict:
    st = await _b().hgetall(_meta_key()) or {}
    return {
        "running": st.get("scan_running") == "1",
        "started": int(float(st.get("scan_started", 0) or 0)),
        "finished": int(float(st.get("scan_finished", 0) or 0)),
        "built_at": int(float(st.get("built_at", 0) or 0)),
        "error": st.get("scan_error", ""),
    }


def scan_due_delay(built_at: float, now: float | None = None) -> float:
    """距下一次扫描应等多久。

    `built_at` = 上次成功建表时刻。已过期（或从未扫过）时不要立刻扫：
    部署/重启瞬间页面池还在预热，扫描的 16 路探测会跟着抢 CPU。给一个
    最短启动延迟，让业务先落地。
    """
    now = time.time() if now is None else now
    if SCAN_INTERVAL_S <= 0:
        return 0.0
    due = (float(built_at or 0) + SCAN_INTERVAL_S) - now
    return min(max(due, SCAN_STARTUP_DELAY_S), SCAN_INTERVAL_S)


async def _scan_loop() -> None:
    from . import store  # 局部导入：store 不依赖本模块，但保持模块顶部轻量

    try:
        st = await scan_status()
        delay = scan_due_delay(float(st.get("built_at") or 0))
    except Exception:  # noqa: BLE001 - 状态读不到就按最短延迟来
        delay = SCAN_STARTUP_DELAY_S
    log.info("scan scheduler: first run in %.0fs, interval=%.0fs",
             delay, SCAN_INTERVAL_S)
    while True:
        await asyncio.sleep(delay)
        delay = SCAN_INTERVAL_S
        if not configured() or not _has_creds():
            continue
        try:
            # 6 个 worker 都会跑这个循环：同一时间桶只让一个真正扫描。
            bucket = int(time.time() // max(1.0, SCAN_INTERVAL_S))
            claimed = await store.set_nx(
                f"{_PREFIX}:scan:claim:{bucket}", {"pid": os.getpid()},
                ttl_s=int(SCAN_INTERVAL_S) + 600)
            if not claimed:
                continue
            res = await scan()
            if res.get("skipped"):
                # 有别的进程在扫：10 分钟后再看，而不是傻等 12 小时
                # （一次手动扫描 18 分钟，12h 的等待会把当天的刷新彻底错过）。
                log.warning("scheduled scan skipped: another scan in progress")
                delay = 600.0
                continue
            log.info("scheduled scan done: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 定时任务不能因一次失败退出
            log.warning("scheduled scan failed: %s: %s",
                        type(exc).__name__, str(exc)[:200])
            delay = min(600.0, SCAN_INTERVAL_S)   # 10 分钟后重试
            continue


def start_scan_scheduler() -> None:
    """应用启动时调用：按 SCAN_INTERVAL_S 定时重建映射。"""
    global _scan_task
    if not SCAN_ENABLE or SCAN_INTERVAL_S <= 0:
        log.info("scan scheduler disabled")
        return
    if not configured() or not _has_creds():
        log.info("scan scheduler idle: pool not configured")
        return
    if _scan_task is not None and not _scan_task.done():
        return
    _scan_task = asyncio.create_task(_scan_loop())


async def _scan_impl(slots: Iterable[int] | None) -> dict[str, Any]:
    c = _cfg()
    n = _scan_slots()
    want = list(slots) if slots is not None else list(range(1, n + 1))

    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(n: int) -> tuple[int, str | None]:
        async with sem:
            return n, await _probe_ip(n)

    results = await asyncio.gather(*(one(n) for n in want))

    b = _b()
    seen: dict[str, int] = {}
    dup_count = 0
    err_count = 0
    # 重扫只更新映射，绝不能动 used / last_used —— 那是当日配额账本。
    for n, ip in results:
        key = _slot_key(n)
        cur = await b.hgetall(key) or {}
        keep = {"day": cur.get("day", ""),
                "used": cur.get("used", 0),
                "quota": cur.get("quota", c["quota"]),
                "soft_quota": cur.get("soft_quota", c["soft_quota"]),
                "last_used": cur.get("last_used", 0)}
        # 「新增 IP」的判定依据：只有 IP 真的变了才刷新 seen_at，
        # 否则每次重扫都会把全部槽位标成新增，标记就没意义了。
        cur_ip = cur.get("ip", "")
        seen_at = int(float(cur.get("seen_at", 0) or 0))
        if ip and ip != cur_ip:
            seen_at = int(time.time())
        if not ip:
            err_count += 1
            await b.hset(key, mapping={
                "ip": cur.get("ip", ""), "state": STATE_ERROR, **keep})
            continue
        if ip in seen:
            dup_count += 1
            await b.hset(key, mapping={
                "ip": ip, "state": STATE_DUP, "prefix": _prefix(ip),
                "dup_of": seen[ip], "seen_at": seen_at, **keep})
        else:
            seen[ip] = n
            await b.hset(key, mapping={
                "ip": ip, "state": STATE_PRIMARY, "prefix": _prefix(ip),
                "dup_of": 0, "seen_at": seen_at, **keep})
        # 不给槽位记录设 TTL：Redis 的 maxmemory-policy=volatile-ttl 会优先
        # 淘汰带过期时间的 key，实测 588 个槽位被清到 471。跨日重置由 day
        # 字段负责，这里不需要 TTL。

    await b.hset(_meta_key(), mapping={
        "built_at": int(time.time()),
        "slots": len(want),
        "distinct_ips": len(seen),
        "dups": dup_count,
        "errors": err_count,
        # 主槽位索引：只保留唯一 IP 的那一个槽位号，取槽时按它读，
        # 避免为了 588 个有效槽位去遍历 3000 个号段。
        "index": ",".join(str(n) for n in sorted(seen.values())),
    })
    # 索引刚建好，就绪状态立即成立，不必等下一次回查
    _ready_cache.update(ok=1.0, ts=0.0)
    return {"slots": len(want), "distinct_ips": len(seen),
            "dups": dup_count, "errors": err_count}


# --------------------------------------------------------------- 调度

@dataclass
class Slot:
    slot: int
    ip: str
    state: str
    used: int
    quota: int
    cool_until: float
    n429: int
    n403: int = 0
    nthrottle: int = 0
    fail: int = 0
    soft_quota: int = 0
    last_used: float = 0.0
    prefix: str = ""
    #: 这个 IP 第一次出现的时刻（扫描时发现 IP 变化才刷新）。面板「新增」用。
    first_seen: float = 0.0
    #: 真实成功出图次数（只由 note_success 累加，429 置账不会碰它）。
    #: 与 used 分开是为了区分「真的出满 30 张」和「被二次 429 判死置账」。
    n_ok: int = 0
    #: True = 当日 used 是被 429 规则置成 quota 的，不是真实消耗。
    booked: bool = False

    @property
    def seg(self) -> str:
        return self.prefix or _prefix(self.ip)

    @property
    def cap(self) -> int:
        """本次调度允许用到多少：软上限优先，低于硬额度才有意义。"""
        c = _cfg()
        soft = self.soft_quota or c["soft_quota"]
        if c["stretch"]:
            return self.quota
        return min(soft, self.quota)

    @property
    def remaining(self) -> int:
        """面板口径：相对**硬**额度还剩多少（风控余量另算）。"""
        return max(0, self.quota - self.used)

    @property
    def remaining_safe(self) -> int:
        """调度口径：在软上限之前还能用几次。"""
        return max(0, self.cap - self.used)

    def available(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return (self.state == STATE_PRIMARY and self.ip
                and self.used < self.cap and self.cool_until <= now)


def _to_slot(n: int, d: dict[str, str], default_quota: int) -> Slot:
    # 跨日：昨天的 used 不算数。用读时归零而不是写时重置，避免
    # 每个请求都产生一次写。
    fresh = d.get("day") == _today()
    return Slot(
        slot=n, ip=d.get("ip", ""), state=d.get("state", STATE_ERROR),
        used=int(d.get("used", 0) or 0) if fresh else 0,
        quota=int(d.get("quota", default_quota) or default_quota),
        cool_until=float(d.get("cool_until", 0) or 0),
        n429=int(d.get("n429", 0) or 0),
        n403=int(d.get("n403", 0) or 0),
        nthrottle=int(d.get("nthrottle", 0) or 0),
        fail=int(d.get("fail", 0) or 0),
        soft_quota=int(d.get("soft_quota", 0) or 0),
        last_used=float(d.get("last_used", 0) or 0),
        prefix=d.get("prefix", ""),
        first_seen=float(d.get("seen_at", 0) or 0),
        n_ok=int(d.get("n_ok", 0) or 0) if fresh else 0,
        booked=(d.get("booked") == "1") if fresh else False,
    )


async def list_slots(scope: str = "primary") -> list[Slot]:
    """读出槽位。

    `scope="primary"`（默认）：只读**主槽位索引**，即每个唯一 IP 保留的那一个。
      实测 3000 个号段里只有 588 个是唯一 IP，其余是碰撞；取槽路径必须走索引，
      否则每次请求从 588 次读膨胀到 3000 次。
    `scope="all"`：遍历整个号段，供排障/测试查看碰撞与被剔除的槽位。
    """
    c = _cfg()
    default_n = max(c["slots"], 0)
    b = _b()
    if scope == "all":
        nums = list(range(1, default_n + 1))
    else:
        meta = await b.hgetall(_meta_key()) or {}
        idx_raw = meta.get("index", "")
        idx = [int(x) for x in idx_raw.split(",") if x] if idx_raw else []
        nums = idx or list(range(1, default_n + 1))
    if not nums:
        return []

    if _redis is not None:
        pipe = b.pipeline(transaction=False)
        for i in nums:
            pipe.hgetall(_slot_key(i))
        res = await pipe.execute()
    else:
        res = [await b.hgetall(_slot_key(i)) for i in nums]
    return [_to_slot(n, d, c["quota"]) for n, d in zip(nums, res) if d]


async def get_slot(n: int) -> Slot | None:
    """按编号取单个槽位（黏性调度用）。"""
    d = await _b().hgetall(_slot_key(n))
    return _to_slot(n, d, _cfg()["quota"]) if d else None


async def acquire_many(limit: int = 1, *, exclude: Iterable[int] = ()) -> list[Slot]:
    """按调度优先级返回**至多 limit 个候选**（一次快照，供调用方逐个试租约）。

    为什么要批量：`list_slots()` 要扫 588 个 key，而并发时每个请求都要
    反复取槽。原实现一次只给一个候选，抢不到租约就得再扫一遍 ——
    20 并发下实测 14 个请求取不到槽（此时池里还有 233 个可用）。
    一次快照给出 N 个候选，调用方本地逐个试租约，扫描次数降一个数量级。

    顺序：段按"最久没用"排序（/24 隔离优先），段内在最老的 POOL_SPREAD 个
    IP 里随机（摊薄并发），并跳过"段内 IP 都还没静默够"的段。
    """
    ex = set(exclude)
    now = time.time()
    cand = [s for s in await list_slots()
            if s.available(now) and s.slot not in ex]
    if not cand:
        return []

    by_seg: dict[str, list[Slot]] = {}
    for s in cand:
        by_seg.setdefault(s.seg, []).append(s)
    # 段年龄必须用**未过滤**的候选算：若先用最小间隔把刚用过的 IP 剔掉再算，
    # 该段的 max 会退回旧值、被误判成"最久没用"，于是连着两次压进同一个 /24。
    seg_age = [(seg, max(x.last_used for x in g))
               for seg, g in by_seg.items()]
    # 先 shuffle 再稳定排序：等值段保留随机顺序，等价于"同年龄随机取"。
    random.shuffle(seg_age)
    seg_age.sort(key=lambda kv: kv[1])

    out: list[Slot] = []
    for seg, _ in seg_age:
        group = sorted(by_seg[seg], key=lambda x: x.last_used)
        if MIN_GAP_S > 0:
            group = [x for x in group if now - x.last_used >= MIN_GAP_S]
        if not group:
            continue
        head = group[:POOL_SPREAD]
        random.shuffle(head)      # 段内摊薄，别让并发全落在同一个 IP
        out.extend(head)
        if len(out) >= limit:
            return out[:limit]

    if not out:
        # 所有段都还没过静默期：退化为最老段里最老的若干 IP。
        # 宁可密一点，也不能因为"间隔不足"就返回空 —— 那会把调度问题
        # 伪装成容量耗尽。
        group = sorted(by_seg[seg_age[0][0]], key=lambda x: x.last_used)
        head = group[:POOL_SPREAD]
        random.shuffle(head)
        out = head
    return out[:limit]


async def acquire(*, exclude: Iterable[int] = ()) -> Slot | None:
    """取下一个槽位：`acquire_many(1)` 的兼容入口。"""
    got = await acquire_many(1, exclude=exclude)
    return got[0] if got else None


async def note_success(slot: int) -> int:
    """一次成功生成：该 IP 用掉 1 次额度，并刷新 last_used（驱动轮询）。"""
    b = _b()
    key = _slot_key(slot)
    today = _today()
    d = await b.hgetall(key) or {}
    if d.get("day") != today:
        # 当日首单：把用量重新起算（昨天的账不带到今天）
        await b.hset(key, mapping={"day": today, "used": 1, "n_ok": 1,
                                   "booked": 0, "n429": 0, "nthrottle": 0})
        n = 1
    else:
        n = await b.hincrby(key, "used", 1)
        await b.hincrby(key, "n_ok", 1)
    await b.hset(key, mapping={"last_used": time.time(), "nthrottle": 0,
                               # 出过图就说明这个出口是好的：清掉"疑似日额度"
                               # 的历史，否则明天之前的一次误判会连累后面的判定
                               "n429d": 0,
                               # 真的出图了：之前的置账标记作废
                               "booked": 0})
    return n


async def note_rate_limited(slot: int, cooldown_s: int | None = None,
                            field: str = "n429") -> float:
    """撞 429（额度耗尽）或 403（出口被拒）：标记冷却并退出调度。

    直接置 `used = quota` 而不是只设冷却，是因为 429 有两种来源（短窗口
    限流 / 当日额度耗尽），而两者的正确处置相同：**今天不要再碰这个 IP**。
    """
    b = _b()
    key = _slot_key(slot)
    if field == "n403":
        # 403 = 出口被拒，不是"额度用掉了"。绝不能置 used=quota：
        # 那会把"被封"记成"已出图 30 张"，面板的今日用量直接失真。
        cd = int(os.environ.get("BFL2OPENAI_POOL_403_COOLDOWN", "21600"))
    else:
        cd = DEFAULT_429_COOLDOWN_S if cooldown_s is None else cooldown_s
    until = time.time() + cd
    await b.hincrby(key, field, 1)
    upd: dict[str, Any] = {"cool_until": int(until)}
    if field != "n403":
        d = await b.hgetall(key) or {}
        # day 必须一起写：otherwise 读侧会把它当成"昨天的账"归零，
        # 额度耗尽的标记就失效了（单测抓到的就是这个）。
        upd["day"] = _today()
        upd["used"] = int(d.get("quota", _cfg()["quota"]) or _cfg()["quota"])
    await b.hset(key, mapping=upd)
    return until


async def note_429(slot: int, *, daily_hint: bool = False,
                   cooldown_s: int | None = None) -> str:
    """记一次 429，返回处置结论：`"exhausted"`（当日额度耗尽）或 `"cooling"`（仅冷却）。

    为什么不看文案就直接置满 —— 上游对**细粒度频控**复用同一句
    "You've used today's 30 free generations" 文案，只靠 `_is_daily_quota()`
    的文案匹配必然把"限流"判成"额度用完"。线上实测（2026-11-18 12:40）：
    353 个被判耗尽的槽位里 **341 个只撞过一次 429**，而当天全站真实出图
    约 4700 张（≈8 张/IP），不可能都真的打满 30 张 ⇒ 绝大多数是误杀。
    被误杀的 IP 还有二十多张额度，却整天不参与调度，可用池从 588 掉到 234。

    因此改成**二次确认**：文案命中 **且** 该 IP 已撞过一次，才置 used=quota；
    首次一律只冷却（指数退避，120s 起）。代价是真耗尽时多一次被拒的提交
    （429 不消耗额度），收益是把被误杀的额度收回来。
    """
    b = _b()
    key = _slot_key(slot)
    d = await b.hgetall(key) or {}
    # 换日：昨日的 429 计数不算数。
    # `_to_slot` 已经把 used 做读时归零（跨日 ⇒ exhausted 自动消失），但
    # `n429d` 如果不清，昨天被判死过两次的槽位今天第一次 429 就又判死
    # （下面 seen_daily >= 1），比全新槽位更脆弱 —— 这会让"每天 08:00
    # 自动重置"变成半重置：used 回来了，判定阈值却还带着昨天的伤。
    if d.get("day") != _today():
        await b.hset(key, mapping={"day": _today(), "n429": 0,
                                   "n429d": 0, "nthrottle": 0})
        d = await b.hgetall(key) or {}
    # 必须分开计数：`n429` 是面板口径的「所有 429」，而判定耗尽只该看
    # 「带今日额度文案的 429」次数（`n429d`）。混用一个计数器会让
    # 「先被频控打一次、随后真撞额度」这种序列在第一次就被判死。
    seen_daily = int(d.get("n429d", 0) or 0)
    await b.hincrby(key, "n429", 1)
    if daily_hint:
        await b.hincrby(key, "n429d", 1)
    if daily_hint and seen_daily >= 1:
        quota = int(d.get("quota", _cfg()["quota"]) or _cfg()["quota"])
        cd = DEFAULT_429_COOLDOWN_S if cooldown_s is None else cooldown_s
        await b.hset(key, mapping={
            "cool_until": int(time.time() + cd),
            # day 必须一起写，否则读侧会把这条"已耗尽"当成昨天的账归零
            "day": _today(),
            "used": quota,
            # 标记这是规则置账（真实成功数在 n_ok 里，通常远小于 quota）
            "booked": 1,
        })
        return "exhausted"
    await note_throttled(slot, cooldown_s)
    return "cooling"


async def try_lease(slot: int, cap: int = 1, ttl_s: int = 180) -> bool:
    """跨 worker 的槽位并发租约。

    为什么必须跨进程：进程内计数器在 6 个 gunicorn worker 之间各算各的，
    实测 12 并发下有 4 个槽位被同时压了 2-3 个请求 —— 单 IP 超 3.5 发/分钟
    就会被边缘限速。

    用 HINCRBY 的返回值做原子判定（Redis 单线程，跨进程天然一致）；
    计数归零不删 key，让 TTL 自己回收，进程被杀造成的泄漏最多持续 ttl_s。
    """
    b = _b()
    key = f"{_PREFIX}:lease:{slot}"
    v = await b.hincrby(key, "n", 1)
    if v == 1:
        await b.expire(key, ttl_s)
    if v > cap:
        await b.hincrby(key, "n", -1)
        return False
    return True


async def drop_lease(slot: int) -> None:
    try:
        await _b().hincrby(f"{_PREFIX}:lease:{slot}", "n", -1)
    except Exception:  # noqa: BLE001 - 释放失败交给 TTL 兜底
        pass


async def note_throttled(slot: int, cooldown_s: int | None = None) -> float:
    """短窗限流：只让出口歇一会儿，**不扣额度、不当成当天废掉**。

    实测 slot 584/724 在 20:35 回 429、21:32 就恢复正常出图 —— 57 分钟内
    自愈，没有任何时区零点落在这个区间。把这种 429 记成"当日额度耗尽"
    会让好 IP 被白白扔掉 10 分钟以上。
    """
    b = _b()
    key = _slot_key(slot)
    n = await b.hincrby(key, "nthrottle", 1)
    if cooldown_s is None:
        # 指数退避：实测单个 IP 被 Vercel 边缘限速后，12 分钟仍未恢复
        # （重启容器换 context 也无效 ⇒ 限速绑 IP 而非会话）。固定 90s
        # 会让它反复撞墙，所以每撞一次翻倍，上限 30 分钟。
        base = int(os.environ.get("BFL2OPENAI_POOL_THROTTLE_BASE", "120"))
        cap = int(os.environ.get("BFL2OPENAI_POOL_THROTTLE_MAX", "1800"))
        cd = min(base * (2 ** (n - 1)), cap)
    else:
        cd = cooldown_s
    until = time.time() + cd
    await b.hset(key, mapping={"cool_until": int(until)})
    return until


async def note_failure(slot: int) -> int:
    """非 429 的失败：只记数，不扣额度（没有出图就是没消耗上游配额）。"""
    return await _b().hincrby(_slot_key(slot), "fail", 1)


async def set_quota(slot: int, quota: int) -> None:
    """改硬额度。夹在 [0, MAX_DAILY_QUOTA]：上游上限是 30，配再大也拿不到图。"""
    await _b().hset(_slot_key(slot), mapping={
        "quota": max(0, min(int(quota), MAX_DAILY_QUOTA))})


async def set_soft_quota(slot: int, soft: int) -> None:
    """改停手点（≤ 硬额度）。等于额度即榨干，小于额度即留风控余量。"""
    await _b().hset(_slot_key(slot), mapping={
        "soft_quota": max(0, min(int(soft), MAX_DAILY_QUOTA))})


async def reset_day() -> int:
    """把全部槽位的当日用量清零（运维手工操作，正常由 key 前缀/日期轮转）。"""
    slots = await list_slots()
    b = _b()
    for s in slots:
        await b.hset(_slot_key(s.slot), mapping={
            "day": _today(), "used": 0, "n_ok": 0, "booked": 0,
            "n429": 0, "n429d": 0, "nthrottle": 0, "fail": 0,
            "cool_until": 0, "last_used": 0})
    return len(slots)


async def snapshot() -> dict[str, Any]:
    """管理中台的数据源。"""
    b = _b()
    meta = await b.hgetall(_meta_key()) or {}
    slots = await list_slots()
    now = time.time()
    rows = [{
        "slot": s.slot, "ip": s.ip, "state": s.state, "prefix": s.seg,
        "used": s.used, "quota": s.quota, "remaining": s.remaining,
        "soft_quota": s.cap, "remaining_safe": s.remaining_safe,
        "n429": s.n429, "n403": s.n403, "nthrottle": s.nthrottle,
        "fail": s.fail, "n_ok": s.n_ok, "booked": s.booked,
        "first_seen": int(s.first_seen),
        "cooling_s": max(0, int(s.cool_until - now)),
        "idle_s": int(now - s.last_used) if s.last_used else None,
        "available": s.available(now),
        #: 配额已用尽（进入当日冷却）。与 429 冷却区分开：这是"用完了"，
        #: 不是"被上游拒了"，运维看到的是两种不同的休息原因。
        "exhausted": s.used >= s.cap and s.state == STATE_PRIMARY,
    } for s in slots]
    rows.sort(key=lambda r: (r["state"] != STATE_PRIMARY, r["slot"]))
    usable = [r for r in rows if r["available"]]
    seg: dict[str, int] = {}
    for r in rows:
        if r["state"] == STATE_PRIMARY:
            seg[r["prefix"]] = seg.get(r["prefix"], 0) + 1
    top_prefix = sorted(seg.items(), key=lambda kv: -kv[1])[:10]
    return {
        "configured": configured(),
        "slots": len(rows),
        "usable": len(usable),
        "distinct_ips": int(meta.get("distinct_ips", 0) or 0),
        "dups": int(meta.get("dups", 0) or 0),
        "errors": int(meta.get("errors", 0) or 0),
        "built_at": int(meta.get("built_at", 0) or 0),
        # /24 段分布：段数越少、头部占比越高，被风控关联的风险越大
        "prefixes": len(seg),
        "top_prefix": top_prefix,
        # 容量按**软**额度算：这才是敢用出去的量
        "capacity_left": sum(r["remaining_safe"] for r in usable),
        "used_today": sum(r["used"] for r in rows if r["state"] == STATE_PRIMARY),
        # 账面 vs 真实：429 二次确认会把 used 置成 quota，n_ok 才是真出图数。
        # hidden_capacity = 被置账吞掉、但可能实际还存在的额度（上限估计）。
        "real_ok_today": sum(r["n_ok"] for r in rows
                             if r["state"] == STATE_PRIMARY),
        "booked_slots": sum(1 for r in rows if r["booked"]),
        "hidden_capacity": sum(max(0, r["used"] - r["n_ok"]) for r in rows
                               if r["booked"] and r["state"] == STATE_PRIMARY),
        "quota_per_ip": _cfg()["quota"],
        "soft_quota_per_ip": _cfg()["soft_quota"],
        "stretch": _cfg()["stretch"],
        # 换日口径：面板直接显示，运维不必再去翻代码或猜时区
        "reset": {
            "tz": DAILY_RESET_TZ,
            "day": _today(),
            "next_at": int(next_reset_at(now)),
        },
        "rows": rows,
    }
