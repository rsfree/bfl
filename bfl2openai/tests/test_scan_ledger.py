"""扫描不得清空账本记忆（2026-11-22 事故回归）。

背景：`scan()` 重写槽位 hash 时 `keep` 只保留 day/used/quota/soft_quota/
last_used，丢掉 `n_ok` / `n429` / `n429d` / `booked` / `cool_until`。后果：

  * `n429d` 归零 ⇒ `note_429` 的「二次确认」退化成「永远第一次」，已判死的
    IP 重新进调度，每个再烧 20~37s 页面时间（线上实测 12h 扫描一次，
    每次扫描后可用池都会虚高，然后 20 分钟内被打回）。
  * `n_ok` 归零 ⇒ `hidden_capacity` 恒为 0，规则置账造成的误杀无法被对账发现。
  * `cool_until` 被清 ⇒ 正在冷却的槽位立刻被重新调度，撞同一堵墙。

扫描只该更新「IP → 槽位」的映射，账本字段一律原样保留。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import egress_pool as ep  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "x")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "3")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()
    yield
    ep.reset_backend()


def test_scan_keeps_ledger_fields(monkeypatch):
    cool = int(time.time() + 3600)

    async def go():
        b = ep._b()
        await b.hset(ep._slot_key(1), mapping={
            "ip": "10.0.0.1", "state": ep.STATE_PRIMARY, "day": ep._today(),
            "used": 30, "quota": 30, "soft_quota": 30, "last_used": 1234.0,
            "n_ok": 7, "n429": 3, "n429d": 2, "booked": 1,
            "cool_until": cool, "prefix": "10.0.0",
        })

        async def fake_probe(slot, timeout=12.0):
            return "10.0.0.1"

        monkeypatch.setattr(ep, "_probe_ip", fake_probe)
        await ep.scan()

        return await b.hgetall(ep._slot_key(1))

    d = asyncio.run(go())
    assert d["n_ok"] == "7", "扫描不得清空真实出图数，否则 hidden_capacity 恒为 0"
    assert d["n429d"] == "2", "扫描不得清空日额度计数，否则二次确认失效"
    assert d["n429"] == "3"
    assert d["booked"] == "1", "扫描不得清空置账标记"
    assert int(d["cool_until"]) == cool, "扫描不得清掉仍在生效的冷却"
    assert d["used"] == "30", "当日额度账本本身也必须保留"


def test_scan_still_updates_ip_mapping(monkeypatch):
    """保留账本不能以牺牲映射更新为代价。"""

    async def go():
        b = ep._b()
        await b.hset(ep._slot_key(1), mapping={
            "ip": "10.0.0.1", "state": ep.STATE_PRIMARY, "day": ep._today(),
            "used": 5, "quota": 30, "n_ok": 5, "booked": 0,
        })

        async def fake_probe(slot, timeout=12.0):
            return "10.9.9.9"

        monkeypatch.setattr(ep, "_probe_ip", fake_probe)
        await ep.scan()
        return await b.hgetall(ep._slot_key(1))

    d = asyncio.run(go())
    assert d["ip"] == "10.9.9.9", "IP 漂移仍必须被写回"
    assert d["used"] == "5", "换 IP 不等于换额度账本"
