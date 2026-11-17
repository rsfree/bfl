"""槽位路由：429 必须换槽，不能原地重试；未配置时行为与历史完全一致。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import egress_pool as ep  # noqa: E402
from bfl2openai import server  # noqa: E402


def _slot(n: int, ip: str) -> ep.Slot:
    return ep.Slot(slot=n, ip=ip, state=ep.STATE_PRIMARY, used=0, quota=30,
                   cool_until=0.0, n429=0, fail=0)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "x")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()
    yield
    ep.reset_backend()


async def _scan(monkeypatch, ips: dict[int, str]):
    async def fake_probe(slot, timeout=12.0):
        return ips.get(slot)

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    await ep.scan()


def test_passthrough_when_pool_not_configured(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "0")
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake)
    import asyncio
    assert asyncio.run(server._browser_generate(prompt="x")) == b"PNG"
    assert "proxy_slot" not in seen, "未配置池时不得注入槽位，行为必须与历史一致"


def test_passthrough_when_not_scanned(monkeypatch):
    """配了 env 但没跑 scan：必须回落到老路径，不能把所有请求判成 429。

    这是部署顺序错误（先配 env 后扫描）的护栏 —— 没有它，索引为空会让
    acquire() 恒返回 None，整站瞬间全 429。
    """
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")
    ep._ready_cache.update(ok=0.0, ts=0.0)   # 没有索引

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake)
    assert asyncio.run(server._browser_generate(prompt="x")) == b"PNG"
    assert "proxy_slot" not in seen
    assert asyncio.run(ep.ready()) is False


def test_429_rotates_to_another_slot(monkeypatch):
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    calls: list[int] = []

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        if len(calls) == 1:
            raise bec.BrowserEgressError("browser egress 429: quota", 429)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)

    out = asyncio.run(server._browser_generate(prompt="x"))
    assert out == b"PNG"
    assert len(calls) == 2, "429 之后必须换槽重试"
    assert calls[0] != calls[1], "不能在同一槽位重试"

    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    failed = rows[calls[0]]
    ok = rows[calls[1]]
    assert failed.used == failed.quota, "撞 429 的槽位应直接置为耗尽"
    assert ok.used == 1, "成功的槽位记 1 次"


def test_all_slots_exhausted_maps_to_429(monkeypatch):
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    async def always_429(proxy_slot=None, **kw):
        raise bec.BrowserEgressError("browser egress 429: quota", 429)

    monkeypatch.setattr(bec, "generate", always_429)
    with pytest.raises(bec.BrowserEgressError) as ei:
        asyncio.run(server._browser_generate(prompt="x"))
    assert ei.value.status == 429
    # 两个槽位都试过并全部置为耗尽
    rows = asyncio.run(ep.list_slots())
    assert all(r.used == r.quota for r in rows)


def test_non_429_failure_does_not_consume_quota(monkeypatch):
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    async def boom(proxy_slot=None, **kw):
        raise bec.BrowserEgressError("browser egress 502: contract", 502)

    monkeypatch.setattr(bec, "generate", boom)
    with pytest.raises(bec.BrowserEgressError):
        asyncio.run(server._browser_generate(prompt="x"))
    rows = asyncio.run(ep.list_slots())
    assert sum(r.used for r in rows) == 0, "没出图不该扣额度"
    assert sum(r.fail for r in rows) == 1
