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
    # 进程内在途计数与黏性槽位是模块级全局：用例间必须清空。
    # 有些用例直接调 `_next_slot()` 不配对释放，残留会让后面的用例
    # 看到「池里还有空闲槽位却租不到」——表现为随机失败（2026-11-21）。
    server._INFLIGHT.clear()
    server._STICKY["slot"] = 0
    yield
    server._INFLIGHT.clear()
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
    # 首次命中只冷却：同一 IP 第三次才判死（上游文案对频控也复用）。
    assert failed.used == 0, "首次 429 不得扣额度"
    assert failed.cool_until > 0, "首次 429 必须进入冷却"
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
    # 首次 429 一律只冷却：两个槽位都不该被扣额度
    rows = asyncio.run(ep.list_slots())
    assert all(r.cool_until > 0 for r in rows), "首次 429 应各自进入冷却"
    assert all(r.used == 0 for r in rows), "没有一条该被直接判成额度耗尽"


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
    # 2026-11-23：只有传输超时 / 500 / 503(rebind) 才记「槽位坏了」。
    # 502 是上游契约错误（composition 冲突等），出口本身是好的 —— 记失败
    # 会让 `note_failure` 把它冷却，等于白白扔掉一个还能出 30 张的 IP。
    assert sum(r.fail for r in rows) == 0, "502 是契约错误，不该记槽位失败"
    assert all(r.available() for r in rows if r.state == ep.STATE_PRIMARY), \
        "契约错误不得冷却出口"


def test_transport_failure_does_not_cool_slot(monkeypatch):
    """连不上 sidecar 是实例问题，不能记到出口头上（会在过载时批量误杀）。"""
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    async def dead(proxy_slot=None, **kw):
        raise bec.BrowserEgressError("browser egress transport: timed out", 0)

    monkeypatch.setattr(bec, "generate", dead)
    with pytest.raises(bec.BrowserEgressError):
        asyncio.run(server._browser_generate(prompt="x"))
    rows = asyncio.run(ep.list_slots())
    assert sum(r.fail for r in rows) == 0, "传输层失败不该记槽位失败"
    assert all(r.available() for r in rows if r.state == ep.STATE_PRIMARY), \
        "实例不可用不得冷却出口"


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
    # 本用例只验"黏性复用"这一个语义：把最小间隔关掉，否则 instant 的假
    # generate 会让第二次请求因间隔不足而换槽（那是 MIN_GAP_S 的职责，
    # 由 test_min_gap_prefers_idle_slot 覆盖）。
    monkeypatch.setattr(ep, "MIN_GAP_S", 0.0)

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

    # 真额度耗尽：首次仍只冷却，**同一 IP 第二次**才置 used=quota
    monkeypatch.setattr(server, "_STICKY", {"slot": 0})
    calls.clear()

    async def quota(proxy_slot=None, **kw):
        calls.append(proxy_slot)
        raise bec.BrowserEgressError(
            'browser egress 429: {"error":"You\'ve used today\'s 30 free '
            'generations. Please come back tomorrow."}', 429)

    monkeypatch.setattr(bec, "generate", quota)
    for round_ in range(3):
        # 清掉冷却，模拟冷却期满后同一 IP 再次被选中
        for s in asyncio.run(ep.list_slots()):
            asyncio.run(ep._b().hset(ep._slot_key(s.slot),
                                     mapping={"cool_until": 0}))
        try:
            asyncio.run(server._browser_generate(prompt="x"))
        except bec.BrowserEgressError:
            pass
        burned = [r for r in asyncio.run(ep.list_slots())
                  if r.used == r.quota]
        if round_ < 2:
            assert not burned, "前两次命中只冷却（上游文案对频控也复用）"
        else:
            assert burned, "第三次命中才判死"


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


def test_second_429_exhausts_slot(monkeypatch):
    """二次确认：同一 IP 连撞两次 429 才判当日耗尽，首次只冷却。

    线上教训（2026-11-18）：353 个被判耗尽的槽位里 341 个只撞过一次 429，
    而当天真实出图约 4700 张（≈8 张/IP）—— 不可能都真打满 30 张。
    一次性置满把还有二十多张额度的 IP 整天枪毙，可用池从 588 掉到 234。
    """
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    assert asyncio.run(ep.note_429(1, daily_hint=True)) == "cooling"
    row = {s.slot: s for s in asyncio.run(ep.list_slots())}[1]
    assert row.used == 0, "首次 429 不得扣额度"
    assert row.cool_until > 0, "首次 429 必须进入冷却"

    assert asyncio.run(ep.note_429(1, daily_hint=True)) == "cooling"
    assert asyncio.run(ep.note_429(1, daily_hint=True)) == "exhausted"
    row = {s.slot: s for s in asyncio.run(ep.list_slots())}[1]
    assert row.used == row.quota, "第三次 429 才判定当日耗尽"


def test_429_without_daily_hint_never_exhausts(monkeypatch):
    """文案不含"今日额度"（纯细粒度频控）时，撞多少次都不许扣额度。"""
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    for _ in range(5):
        assert asyncio.run(ep.note_429(1, daily_hint=False)) == "cooling"
    row = {s.slot: s for s in asyncio.run(ep.list_slots())}[1]
    assert row.used == 0, "纯频控不得消耗额度"


def test_acquire_spreads_candidates(monkeypatch):
    """摊薄：acquire() 必须在最老的一批候选里随机，不能每次都给同一个槽位。

    原实现严格取 last_used 的全局最小值，而 last_used 要等生成完成（约 20s）
    才更新 ⇒ 并发请求全都拿到同一候选，抢不到租约就报"无可用槽位"。
    """
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "16")

    async def fake_probe(slot, timeout=12.0):
        return f"10.0.{slot}.1"          # 16 个互不相同的 /24

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    picks = {asyncio.run(ep.acquire()).slot for _ in range(40)}
    assert len(picks) > 1, f"acquire() 未随机化，40 次只返回 {picks}"


def test_min_gap_prefers_idle_slot(monkeypatch):
    """最小间隔：刚出过图的槽位要让位给静默更久的槽位。"""
    import asyncio
    import time
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "MIN_GAP_S", 18.0)

    async def fake_probe(slot, timeout=12.0):
        return {1: "10.1.1.1", 2: "10.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    asyncio.run(ep._b().hset(ep._slot_key(1), mapping={"last_used": time.time()}))
    asyncio.run(ep._b().hset(ep._slot_key(2),
                             mapping={"last_used": time.time() - 600}))
    assert asyncio.run(ep.acquire()).slot == 2, "应跳过刚用过的槽位"


# ------------------------------- 瞬态错误换槽（2026-11-21）
# 事故：sidecar 高负载下 `rebind slot N failed: TimeoutError` 124 次，
# 旧实现直接上行成 502 —— 一个槽位的页面状态坏了，却让整笔请求失败。


def test_rebind_503_rotates_to_another_slot(monkeypatch):
    import asyncio

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "2.2.2.2"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    calls: list[int] = []

    async def flaky(proxy_slot=None, deadline=None, **kw):
        calls.append(proxy_slot)
        if len(calls) == 1:
            raise bec.BrowserEgressError(
                'browser egress 503: {"error":"rebind slot 1 failed: '
                'TimeoutError"}', 503)
        return b"PNG"

    monkeypatch.setattr(bec, "generate", flaky)
    out = asyncio.run(server._browser_generate(prompt="x"))
    assert out == b"PNG"
    assert len(calls) == 2, "rebind 失败必须换槽重试"
    assert calls[0] != calls[1], "不能在同一槽位重试"

    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[calls[0]].fail == 1, "失败槽位记账，但不扣额度"
    assert rows[calls[0]].used == 0
    assert rows[calls[1]].used == 1


def test_rebind_retries_are_bounded(monkeypatch):
    """瞬态重试有上限：不能 6 个槽位全部重试到把预算耗尽。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")

    async def fake_probe(slot, timeout=12.0):
        return f"10.0.0.{slot}"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    asyncio.run(ep.scan())

    calls: list[int] = []

    async def always(proxy_slot=None, deadline=None, **kw):
        calls.append(proxy_slot)
        raise bec.BrowserEgressError(
            'browser egress 503: {"error":"rebind slot failed: TimeoutError"}',
            503)

    monkeypatch.setattr(bec, "generate", always)
    with pytest.raises(bec.BrowserEgressError) as ei:
        asyncio.run(server._browser_generate(prompt="x"))
    assert ei.value.status == 503
    assert len(calls) == 1 + server._TRANSIENT_ATTEMPTS, \
        f"瞬态重试次数必须封顶，实际 {len(calls)} 次"
