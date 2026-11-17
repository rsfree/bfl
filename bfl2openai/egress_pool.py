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
import os
import time
from dataclasses import dataclass
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

#: 建映射时探测出口 IP 的并发度。探测只打 ipify，不消耗上游额度。
SCAN_CONCURRENCY = int(os.environ.get("BFL2OPENAI_POOL_SCAN_CONCURRENCY", "16"))

_DAY_TTL_S = 3 * 24 * 3600
_PREFIX = "egress:v2"

STATE_PRIMARY = "primary"     # 唯一占用该 IP 的槽位，可参与调度
STATE_DUP = "dup"             # 该 IP 已被别的槽位占用，不参与调度
STATE_ERROR = "error"         # 探测失败，未建映射


class PoolNotConfigured(RuntimeError):
    """未配置 2090 池；调用方应回落到原有 2088 行为。"""


# --------------------------------------------------------------- 配置

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
    if not configured():
        return False
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
        _redis = aioredis.from_url(url, decode_responses=True)
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

async def _probe_ip(slot: int, *, timeout: float = 12.0) -> str | None:
    """经该槽位取一次出口 IP。失败返回 None。

    用 curl_cffi（已是运行时依赖）而不是 subprocess curl：主镜像里没有 curl。
    """
    from curl_cffi import requests as creq

    try:
        r = await asyncio.to_thread(
            creq.get, "https://api.ipify.org",
            proxies={"https": slot_proxy_url(slot), "http": slot_proxy_url(slot)},
            timeout=timeout)
        if r.status_code == 200:
            ip = (r.text or "").strip()
            return ip if ip and len(ip) <= 45 else None
    except Exception:  # noqa: BLE001 - 探测失败即标记 error，不阻断其它槽位
        return None
    return None


async def scan(slots: Iterable[int] | None = None) -> dict[str, Any]:
    """探测槽位出口 IP 并**就地消除碰撞**，结果写入注册表。

    - 同一 IP 只保留编号最小的槽位为 `primary`，其余标 `dup`
    - 探测失败标 `error`（保留配额字段，便于人工重扫）
    - 已存在当日用量的槽位**不清零**（重扫不能把已消耗的额度抹掉）
    """
    if not configured():
        raise PoolNotConfigured("BFL2OPENAI_POOL_HOST / _PASS / _SLOTS 未配置")
    b0 = _b()
    st = await b0.hgetall(_meta_key()) or {}
    # 扫描状态必须放 Redis：gunicorn 6 个 worker，进程内变量互相看不见，
    # 面板会显示"没在扫"而实际正在扫（或反之）。
    if st.get("scan_running") == "1" and (
            time.time() - float(st.get("scan_started", 0) or 0) < 1800):
        return {"running": True, "skipped": True}
    await b0.hset(_meta_key(), mapping={
        "scan_running": 1, "scan_started": int(time.time()), "scan_error": ""})
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
        "error": st.get("scan_error", ""),
    }


async def _scan_impl(slots: Iterable[int] | None) -> dict[str, Any]:
    c = _cfg()
    want = list(slots) if slots is not None else list(range(1, c["slots"] + 1))

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
        keep = {"used": cur.get("used", 0),
                "quota": cur.get("quota", c["quota"]),
                "soft_quota": cur.get("soft_quota", c["soft_quota"]),
                "last_used": cur.get("last_used", 0)}
        if not ip:
            err_count += 1
            await b.hset(key, mapping={
                "ip": cur.get("ip", ""), "state": STATE_ERROR, **keep})
            continue
        if ip in seen:
            dup_count += 1
            await b.hset(key, mapping={
                "ip": ip, "state": STATE_DUP, "prefix": _prefix(ip),
                "dup_of": seen[ip], **keep})
        else:
            seen[ip] = n
            await b.hset(key, mapping={
                "ip": ip, "state": STATE_PRIMARY, "prefix": _prefix(ip),
                "dup_of": 0, **keep})
        await b.expire(key, _DAY_TTL_S)

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
    fail: int
    soft_quota: int = 0
    last_used: float = 0.0
    prefix: str = ""

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
    return Slot(
        slot=n, ip=d.get("ip", ""), state=d.get("state", STATE_ERROR),
        used=int(d.get("used", 0) or 0),
        quota=int(d.get("quota", default_quota) or default_quota),
        cool_until=float(d.get("cool_until", 0) or 0),
        n429=int(d.get("n429", 0) or 0),
        fail=int(d.get("fail", 0) or 0),
        soft_quota=int(d.get("soft_quota", 0) or 0),
        last_used=float(d.get("last_used", 0) or 0),
        prefix=d.get("prefix", ""),
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


async def acquire(*, exclude: Iterable[int] = ()) -> Slot | None:
    """取下一个槽位：**按 /24 分段的轮询（两级 LRU）**。

    为什么不是普通轮询：实测池子 525 个唯一 IP 只分布在 31 个 /24，其中
    `38.6.213.0/24` 独占 220 个。单纯按 IP 轮询会让相邻请求频繁落在同一个
    C 段，在风控看来「换 IP 但没换身份」。先选**最久没用的段**，再在段内
    选最久没用的 IP，才能把相邻请求分散到不同网段。

    代价：无。段级轮询不减少总调用量，只改变顺序 —— 除非只剩一个段还有
    额度，那时所有请求只能挤在该段，属容量不足而非策略问题。
    """
    if not configured():
        return None
    ex = set(exclude)
    now = time.time()
    cand = [s for s in await list_slots()
            if s.available(now) and s.slot not in ex]
    if not cand:
        return None

    by_seg: dict[str, list[Slot]] = {}
    for s in cand:
        by_seg.setdefault(s.seg, []).append(s)
    # 段级时间戳取组内最大值（最近一次用过），越小越该轮到它
    seg_last = {seg: max(x.last_used for x in g) for seg, g in by_seg.items()}
    best_seg = min(seg_last, key=lambda seg: (seg_last[seg], seg))
    group = sorted(by_seg[best_seg], key=lambda s: (s.last_used, s.slot))
    return group[0]


async def note_success(slot: int) -> int:
    """一次成功生成：该 IP 用掉 1 次额度，并刷新 last_used（驱动轮询）。"""
    b = _b()
    key = _slot_key(slot)
    n = await b.hincrby(key, "used", 1)
    await b.hset(key, mapping={"last_used": time.time()})
    return n


async def note_rate_limited(slot: int, cooldown_s: int | None = None) -> float:
    """撞 429：该 IP 立即标记冷却并置为额度耗尽。

    直接置 `used = quota` 而不是只设冷却，是因为 429 有两种来源（短窗口
    限流 / 当日额度耗尽），而两者的正确处置相同：**今天不要再碰这个 IP**。
    """
    cd = DEFAULT_429_COOLDOWN_S if cooldown_s is None else cooldown_s
    until = time.time() + cd
    b = _b()
    key = _slot_key(slot)
    await b.hincrby(key, "n429", 1)
    d = await b.hgetall(key) or {}
    quota = int(d.get("quota", _cfg()["quota"]) or _cfg()["quota"])
    await b.hset(key, mapping={"used": quota, "cool_until": int(until)})
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
            "used": 0, "n429": 0, "fail": 0, "cool_until": 0, "last_used": 0})
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
        "n429": s.n429, "fail": s.fail,
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
        "quota_per_ip": _cfg()["quota"],
        "soft_quota_per_ip": _cfg()["soft_quota"],
        "stretch": _cfg()["stretch"],
        "rows": rows,
    }
