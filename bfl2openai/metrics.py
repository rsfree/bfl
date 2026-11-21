"""运行指标 + 入口并发闸门。

为什么要独立成模块：闸门与指标要同时被 `server`（热路径）和 `panel`（只读）
使用。放在任一侧都会形成循环导入，或者让阈值出现两份。

闸门口径
--------
`GATE_LIMIT` 是**跨 worker** 的在途生成数上限，靠 Redis 计数器实现。不能用
进程内 semaphore：6 个 gunicorn worker 各算各的，实际上限会放大 6 倍。

取值依据（2026-11-21 线上实测）：3 台 sidecar × 9 页 = 27 个页面；26 个在途
时宿主 load 44（8 核）、sidecar 内存 73~76%，rebind 开始成批超时。

默认 24 而不是 18：请求日志回放显示高峰期入口并发长期在 25~30（含幂等
follower，真实在途约 26），阈值压到 18 会在高峰期把大量正常请求打成 429。
24 只在真正越过实测水位时才削峰；若 rebind/5xx 仍持续，再下调到 20。
削峰只是止血 —— 真正的容量上限在 CPU/页数（见部署备注）。

指标口径
--------
计数写 Redis hash `bfl:metrics`（多 worker 汇总），键不存在就是 0。
指标只增不减，跨日不清零 —— 用面板读数时看的是"自部署以来"。

  rebind_timeout     sidecar 回 503 且原因是 rebind 超时（槽位重建失败）
  sidecar_500        sidecar 内部 500（多为页面 TimeoutError）
  transport_timeout  连不上/等不到 sidecar（status=0）
  retry_transient    瞬态错误后换槽重试的次数
  retry_ok           重试后成功的次数
  budget_exhausted   墙钟预算耗尽、主动收手的次数
  gate_reject        闸门拒绝的次数
  gen_ok             成功出图次数
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from . import browser_egress_client
from . import store

log = logging.getLogger("bfl2openai.metrics")

METRICS_KEY = "bfl:metrics"

#: 0 = 关闭闸门（回滚开关）。负值同样视为关闭。
GATE_LIMIT = int(os.environ.get("BFL2OPENAI_GATE_LIMIT", "24"))
GATE_KEY = "bfl:gate:inflight"
#: 在途计数 TTL：必须大于单笔预算（240s），否则计数会先过期再被 DECR。
GATE_TTL_S = int(os.environ.get("BFL2OPENAI_GATE_TTL", "420"))


async def bump(name: str, delta: int = 1) -> None:
    """累计指标 +1。状态库抖动不能影响主流程，因此吞异常。"""
    try:
        await store.hincr(METRICS_KEY, name, delta)
    except Exception as exc:  # noqa: BLE001
        log.debug("metric %s skipped: %s", name, exc)


async def inflight() -> int:
    """当前在途数；Redis 不可用时返回 -1（面板显示 unknown）。"""
    try:
        return await store.read_counter(GATE_KEY)
    except Exception:  # noqa: BLE001
        return -1


@asynccontextmanager
async def admit(weight: int = 1):
    """入口闸门：yield True 放行 / False 已满（调用方回 429）。

    fail-open 是刻意的：状态库故障时宁可放行，也不能把整站判成繁忙。
    """
    w = max(1, int(weight))
    if GATE_LIMIT <= 0:
        yield True
        return
    try:
        cur = await store.incr_counter(GATE_KEY, GATE_TTL_S, delta=w)
    except Exception as exc:  # noqa: BLE001
        log.warning("gate incr failed, fail-open: %s", exc)
        yield True
        return
    if cur > GATE_LIMIT:
        for _ in range(w):
            try:
                await store.decr_counter(GATE_KEY)
            except Exception:  # noqa: BLE001
                break
        await bump("gate_reject")
        yield False
        return
    try:
        yield True
    finally:
        for _ in range(w):
            try:
                await store.decr_counter(GATE_KEY)
            except Exception as exc:  # noqa: BLE001 - TTL 兜底
                log.warning("gate decr failed: %s", exc)
                break


async def snapshot() -> dict:
    """面板/健康检查用：累计指标 + 当前在途 + 实例熔断状态。"""
    counters = {}
    try:
        counters = await store.hgetall_int(METRICS_KEY)
    except Exception:  # noqa: BLE001
        pass
    return {
        "counters": counters,
        "gate": {"limit": GATE_LIMIT, "inflight": await inflight()},
        "breaker": browser_egress_client.stats(),
    }
