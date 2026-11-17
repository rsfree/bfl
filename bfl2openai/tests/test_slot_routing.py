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
            raise bec.BrowserEgressError(
            'browser egress 429: {"error":"You\'ve used today\'s 30 free '
            'generations. Please come back tomorrow."}', 429)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)

    server._STICKY["slot"] = 0
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
        raise bec.BrowserEgressError(
            'browser egress 429: {"error":"You\'ve used today\'s 30 free '
            'generations. Please come back tomorrow."}', 429)

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


def test_canary_header_forces_pool(monkeypatch):
    """Global 关着时，带 X-Bfl-Egress: pool 的请求仍走槽位 —— 灰度验证用。"""
    import asyncio
    from fastapi.testclient import TestClient

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())
    # 全局开关关闭
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "0")
    ep.reset_backend()

    async def fake_probe2(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]
    monkeypatch.setattr(ep, "_probe_ip", fake_probe2)
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    asyncio.run(ep.scan())
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "0")

    calls: list[int | None] = []

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)
    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(server, "API_KEY", "")

    client = TestClient(server.app, headers={"X-Bfl-Egress": "pool"})
    r = client.post("/v1/images/generations",
                    json={"prompt": "x", "response_format": "b64_json"})
    assert r.status_code == 200, r.text
    assert calls and calls[0] is not None, "灰度请求必须带上槽位"


def test_legacy_header_forces_old_path(monkeypatch):
    import asyncio
    from fastapi.testclient import TestClient

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    asyncio.run(ep.scan())

    calls: list[int | None] = []

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)
    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(server, "API_KEY", "")
    client = TestClient(server.app, headers={"X-Bfl-Egress": "legacy"})
    r = client.post("/v1/images/generations",
                    json={"prompt": "x", "response_format": "b64_json"})
    assert r.status_code == 200, r.text
    assert calls == [None], "legacy 强制不得注入槽位"


def test_403_also_rotates(monkeypatch):
    """403 与 429 同为"这个出口不可用"，必须换槽而不是直接抛错。"""
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2", 3: "3.3.3.3"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "3")
    asyncio.run(ep.scan())

    calls: list[int] = []

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        if len(calls) < 3:
            raise bec.BrowserEgressError("browser egress 403: Access denied", 403)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)
    out = asyncio.run(server._browser_generate(prompt="x"))
    assert out == b"PNG"
    assert len(calls) == 3 and len(set(calls)) == 3, "每个槽位各试一次"

    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    blocked = [rows[c] for c in calls[:2]]
    assert all(not b.available() for b in blocked), "被拒的出口必须退出调度"
    assert all(b.n403 == 1 for b in blocked), "403 计入 n403"
    assert all(b.n429 == 0 for b in blocked), "403 不该记进 429 计数"
    assert all(b.used == 0 for b in blocked), "403 不消耗额度，今日用量要真实"


def test_sticky_slot_reuses_until_exhausted(monkeypatch):
    """同一 worker 应复用同一出口，避免每请求都重建 context（Vercel 挑战代价）。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "3")
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})
    monkeypatch.delenv("BFL2OPENAI_POOL_QUOTA", raising=False)
    monkeypatch.setenv("BFL2OPENAI_POOL_SOFT_QUOTA", "2")

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2", 3: "3.3.3.3"}[slot]
    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    calls: list[int] = []

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", fake_generate)
    for _ in range(4):
        asyncio.run(server._browser_generate(prompt="x"))
    assert calls[0] == calls[1], "前两次必须复用同一槽位"
    assert calls[0] != calls[2], "用满停手点后必须换槽"


def test_prebind_slots_are_preferred(monkeypatch):
    """预绑定槽位优先：它们在启动时已经热好，用它可跳过 2s 的 context 重建。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "12")
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})
    monkeypatch.setattr(server, "_PREBIND", [7, 9])

    async def fake_probe(slot, timeout=12.0):
        return f"10.0.0.{slot}"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    picked = []
    for _ in range(4):
        s = asyncio.run(server._next_slot(set()))
        picked.append(s.slot)
        asyncio.run(ep.note_success(s.slot))
    assert picked[0] in (7, 9), f"应优先选预绑定槽位，实际 {picked}"


def test_daily_quota_vs_short_throttle(monkeypatch):
    """只有报文里带「free generations」才算当日额度耗尽。"""
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})

    calls = []

    async def throttle_then_ok(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        if len(calls) == 1:
            raise bec.BrowserEgressError("browser egress 429: slow down", 429)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", throttle_then_ok)
    assert asyncio.run(server._browser_generate(prompt="x")) == b"PNG"
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    first = rows[calls[0]]
    assert first.used == 0, "短窗限流不能烧额度"
    assert first.nthrottle == 1

    # 真额度耗尽：走 note_rate_limited，置 used=quota
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})
    calls.clear()

    async def quota(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        raise bec.BrowserEgressError(
            'browser egress 429: {"error":"You\'ve used today\'s 30 free '
            'generations. Please come back tomorrow."}', 429)

    monkeypatch.setattr(bec, "generate", quota)
    try:
        asyncio.run(server._browser_generate(prompt="x"))
    except bec.BrowserEgressError:
        pass
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    burned = [r for r in rows.values() if r.used == r.quota]
    assert burned, "日额度耗尽必须标记为当天不可用"


def test_concurrent_requests_spread_across_slots(monkeypatch):
    """并发必须摊到不同 IP：单 IP 跑 2 个并发就可能被边缘限速。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "6")
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})
    monkeypatch.setattr(server, "_INFLIGHT", {})
    monkeypatch.setattr(server, "_SLOT_CAP", 1)
    monkeypatch.setattr(server, "_PREBIND", [])
    monkeypatch.setattr(server, "_OFFSET", 0)

    async def fake_probe(slot, timeout=12.0):
        return f"10.0.0.{slot}"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    async def grab_many():
        return [await server._next_slot(set()) for _ in range(4)]

    picked = asyncio.run(grab_many())
    slots = [s.slot for s in picked]
    assert len(set(slots)) == 4, f"4 个并发应落在 4 个不同槽位，实际 {slots}"


def test_prebind_order_differs_per_worker(monkeypatch):
    """不同 worker 的预绑定起点必须错开，否则 6 个 worker 全压第一个 IP。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "8")
    monkeypatch.setattr(server, "_INFLIGHT", {})
    monkeypatch.setattr(server, "_SLOT_CAP", 1)
    monkeypatch.setattr(server, "_PREBIND", [1, 2, 3, 4])

    async def fake_probe(slot, timeout=12.0):
        return f"10.0.0.{slot}"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    picks = []
    for off in range(4):
        monkeypatch.setattr(server, "_STICKY", {"slot": 0})
        monkeypatch.setattr(server, "_OFFSET", off)
        picks.append(asyncio.run(server._next_slot(set())).slot)
    assert len(set(picks)) == 4, f"4 个偏移应选中 4 个不同槽位，实际 {picks}"
