"""入口并发闸门与指标（2026-11-21）。

为什么需要：线上 26 个在途时宿主 load 44（8 核），sidecar 的 rebind 开始
成批超时。闸门的目的是把「排队」从 sidecar 内部挪到入口，用 429 快速失败，
而不是让请求等 100 秒再变 502/504。
"""
from __future__ import annotations

import asyncio

import pytest

from bfl2openai import metrics, store


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(metrics, "GATE_LIMIT", 1)
    yield


def test_gate_allows_then_rejects_then_releases():
    async def scenario():
        async with metrics.admit(1) as first:
            assert first is True
            async with metrics.admit(1) as second:
                assert second is False, "超过上限必须拒绝"
        # 释放后应重新可用
        async with metrics.admit(1) as third:
            assert third is True

    asyncio.run(scenario())
    assert asyncio.run(store.read_counter(metrics.GATE_KEY)) == 0


def test_gate_counts_weight(monkeypatch):
    """n=4 的请求按 4 个并发槽位计，避免一个多图请求偷偷压满页面池。"""
    monkeypatch.setattr(metrics, "GATE_LIMIT", 4)

    async def scenario():
        async with metrics.admit(2) as ok:
            assert ok is True
            assert await store.read_counter(metrics.GATE_KEY) == 2

    asyncio.run(scenario())


def test_gate_disabled_is_passthrough(monkeypatch):
    monkeypatch.setattr(metrics, "GATE_LIMIT", 0)

    async def scenario():
        for _ in range(5):
            async with metrics.admit(1) as ok:
                assert ok is True

    asyncio.run(scenario())


def test_gate_reject_is_counted():
    async def scenario():
        async with metrics.admit(1) as ok:
            assert ok
            async with metrics.admit(1) as denied:
                assert denied is False

    asyncio.run(scenario())
    counters = asyncio.run(store.hgetall_int(metrics.METRICS_KEY))
    assert counters.get("gate_reject") == 1
