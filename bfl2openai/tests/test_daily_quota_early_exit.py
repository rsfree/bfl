"""日额度枯竭时必须早退，而不是烧满 240s 预算（2026-11-22 事故回归）。

线上实测（额度耗尽期）：

    18:13:31 /v1/images/edits -> 429  208716ms
    18:14:23 /v1/images/edits -> 429  211412ms

429 的耗时从 12ms 涨到 210s：`_browser_generate` 遇到「今日额度已用完」
只 `continue` 换槽，一个 IP 一个 IP 试到墙钟预算耗尽。对确定性失败这是
纯浪费——客户端等 3 分半，拿到的还是一个 retry later。

现在的规则：连续 `_DAILY_HITS_LIMIT` 个 IP 都报日额度耗尽，就判定整池枯竭，
立即返回 429，并把 Retry-After 给到换日时间（客户端不会做无效重试）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import egress_pool as ep  # noqa: E402
from bfl2openai import server  # noqa: E402

_DAILY_BODY = '{"error":"You\'ve used today\'s 30 free generations. Please come back tomorrow."}'


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "x")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "6")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()
    server._INFLIGHT.clear()
    server._STICKY["slot"] = 0
    yield
    server._INFLIGHT.clear()
    ep.reset_backend()


async def _scan(monkeypatch, n=6):
    async def fake_probe(slot, timeout=12.0):
        return "10.0.0.%d" % slot

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    await ep.scan(list(range(1, n + 1)))


def test_early_exit_after_n_daily_hits(monkeypatch):
    calls = []

    async def go():
        await _scan(monkeypatch)

        async def fake_generate(proxy_slot=None, **kw):
            calls.append(proxy_slot)
            raise bec.BrowserEgressError("browser egress 429: " + _DAILY_BODY, 429)

        monkeypatch.setattr(bec, "generate", fake_generate)

        with pytest.raises(bec.BrowserEgressError) as ei:
            await server._browser_generate(prompt="x", deadline=None)

        return ei.value

    exc = asyncio.run(go())
    assert exc.status == 429
    assert len(calls) == server._DAILY_HITS_LIMIT, (
        "应在 %d 个 IP 报日额度耗尽后收手，实际试了 %d 个"
        % (server._DAILY_HITS_LIMIT, len(calls)))
    assert len(calls) < server._SLOT_ATTEMPTS, "早退必须发生在槽位次数上限之前"
    assert exc.retry_after is not None, "整池枯竭必须带 Retry-After"
    assert exc.retry_after >= 60


def test_acquire_many_prefers_proven_slots(monkeypatch):
    """抢救模式只挑「今天真实出过图」且本地还有余额的槽位。"""

    async def go():
        await _scan(monkeypatch)
        await ep.note_success(5)          # slot 5: n_ok=1, used=1
        return await ep.acquire_many(1, prefer_proven=True)

    slots = asyncio.run(go())
    assert slots and slots[0].slot == 5
    assert slots[0].n_ok == 1


def test_acquire_many_prefers_none_when_no_proven(monkeypatch):
    """没有任何 proven 槽位时返回空 —— 让上层按原有早退收手，不盲试。"""

    async def go():
        await _scan(monkeypatch)
        return await ep.acquire_many(1, prefer_proven=True)

    assert asyncio.run(go()) == []


def test_scavenge_uses_proven_slot_after_daily_limit(monkeypatch):
    """连撞 3 个日额度后，用剩余尝试抢救 proven 槽位并成功。"""
    calls: list[tuple] = []

    async def ready():
        return True

    monkeypatch.setattr(ep, "configured", lambda: True)
    monkeypatch.setattr(ep, "ready", ready)

    async def fake_next(exclude, prefer_proven=False):
        if prefer_proven:
            calls.append(("proven", 5))
            return ep.Slot(slot=5, ip="10.0.0.5", state=ep.STATE_PRIMARY,
                           used=1, quota=30, cool_until=0, n429=0, n_ok=1)
        n = 1 + sum(1 for c in calls if c[0] == "normal")
        calls.append(("normal", n))
        return ep.Slot(slot=n, ip=f"10.0.0.{n}", state=ep.STATE_PRIMARY,
                       used=0, quota=30, cool_until=0, n429=0, n_ok=0)

    async def fake_generate(proxy_slot=None, **kw):
        calls.append(("gen", proxy_slot))
        if proxy_slot == 5:
            return b"PNG"
        raise bec.BrowserEgressError("browser egress 429: " + _DAILY_BODY, 429)

    monkeypatch.setattr(server, "_next_slot", fake_next)
    monkeypatch.setattr(bec, "generate", fake_generate)

    async def go():
        await _scan(monkeypatch)
        return await server._browser_generate(
            prompt="x", deadline=time.monotonic() + 200)

    assert asyncio.run(go()) == b"PNG"
    normal = [c for c in calls if c[0] == "normal"]
    proven = [c for c in calls if c[0] == "proven"]
    assert len(normal) == server._DAILY_HITS_LIMIT
    assert proven == [("proven", 5)]
    assert ("gen", 5) in calls


def test_non_daily_429_does_not_early_exit(monkeypatch):
    """短窗限流的 429 不能触发早退——换个 IP 很可能就成功了。"""
    calls = []

    async def go():
        await _scan(monkeypatch)

        async def fake_generate(proxy_slot=None, **kw):
            calls.append(proxy_slot)
            raise bec.BrowserEgressError("browser egress 429: too many requests", 429)

        monkeypatch.setattr(bec, "generate", fake_generate)

        with pytest.raises(bec.BrowserEgressError) as ei:
            await server._browser_generate(prompt="x", deadline=None)
        return ei.value

    exc = asyncio.run(go())
    assert exc.status == 429
    assert len(calls) == server._SLOT_ATTEMPTS, (
        "普通限流应继续换槽直到次数上限，实际 %d" % len(calls))
    assert getattr(exc, "retry_after", None) is None, "普通限流不给换日级别的 Retry-After"


def test_wrap_uses_exc_retry_after():
    """整池枯竭的 Retry-After 必须是换日秒数，不是固定的 60。"""
    exc = bec.BrowserEgressError("daily quota exhausted", 429, retry_after=4321)
    resp = server.wrap_browser_egress_error(exc)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "4321"

    plain = bec.BrowserEgressError("browser egress 429: too many requests", 429)
    resp2 = server.wrap_browser_egress_error(plain)
    assert resp2.headers["Retry-After"] == server._RETRY_AFTER_S, (
        "没有 retry_after 时保持原行为")
