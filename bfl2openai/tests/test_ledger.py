"""配额账本：真实成功数与 429 置账必须分开（2026-11-22）。

背景：`note_429` 的二次确认规则会把 used 直接置成 quota，导致面板的
「今日已用 / 剩余」混入了非真实消耗；而超时重试若已在上游出图，used 又会
少记。n_ok / booked 两个字段用来把这两个方向拆开。
"""
from __future__ import annotations

import asyncio
import os
import socket

import pytest

from bfl2openai import egress_pool as ep


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "x")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()
    yield
    ep.reset_backend()


def _seed(slot: int, **kw):
    base = {"day": ep._today(), "ip": f"10.0.0.{slot}", "state": ep.STATE_PRIMARY,
            "used": 0, "quota": 30, "n_ok": 0, "booked": 0}
    base.update(kw)
    return ep._b().hset(ep._slot_key(slot), mapping=base)


def test_success_counts_real_and_clears_booked():
    async def go():
        await _seed(1, used=5, n_ok=5, booked=1)
        await ep.note_success(1)
        return await ep.get_slot(1)

    s = asyncio.run(go())
    assert s.used == 6 and s.n_ok == 6, "used 与 n_ok 必须同步 +1"
    assert s.booked is False, "真出图后置账标记必须清掉"


def test_second_daily_429_still_cools():
    """两次命中还不够判死：上游对短窗口频控复用同一句文案。"""
    async def go():
        await _seed(1, used=3, n_ok=3, n429d=1)
        verdict = await ep.note_429(1, daily_hint=True)
        return verdict, await ep.get_slot(1)

    verdict, s = asyncio.run(go())
    assert verdict == "cooling"
    assert s.used == 3, "未确认前不得扣额度"
    assert s.booked is False


def test_third_daily_429_books_slot():
    """第三次命中才判死：均摊之后连续命中同一 IP 极罕见。"""
    async def go():
        await _seed(1, used=3, n_ok=3, n429d=2)
        verdict = await ep.note_429(1, daily_hint=True)
        return verdict, await ep.get_slot(1)

    verdict, s = asyncio.run(go())
    assert verdict == "exhausted"
    assert s.used == 30, "规则扫尾：used 被置成 quota"
    assert s.n_ok == 3, "真实成功数不能被伪装"
    assert s.booked is True


def test_first_daily_429_only_cools():
    """首次命中只冷却：上游对短窗口频控复用同一句「今天 30 张用完」。

    2026-11-24 实测：改成「首次即置死」后一天累积 566 个 IP 被判死，
    平均只出过 7 张图，而 sidecar 侧成功率仍有 60% —— 多数是误杀。
    """
    async def go():
        await _seed(1, used=3, n_ok=3)
        verdict = await ep.note_429(1, daily_hint=True)
        return verdict, await ep.get_slot(1)

    verdict, s = asyncio.run(go())
    assert verdict == "cooling"
    assert s.used == 3, "未确认前不得扣额度"
    assert s.booked is False
    assert s.cool_until > 0, "必须进入冷却"


def test_snapshot_exposes_real_and_booked():
    async def go():
        await _seed(1, used=30, n_ok=12, booked=1)
        await _seed(2, used=4, n_ok=4, booked=0)
        return await ep.snapshot()

    snap = asyncio.run(go())
    assert snap["used_today"] == 34
    assert snap["real_ok_today"] == 16
    assert snap["booked_slots"] == 1
    assert snap["hidden_capacity"] == 18, "置账吞掉的额度 = used - n_ok"


def test_reset_day_clears_ledger():
    async def go():
        await _seed(1, used=30, n_ok=7, booked=1)
        await ep.reset_day()
        return await ep.get_slot(1)

    s = asyncio.run(go())
    assert s.used == 0 and s.n_ok == 0 and s.booked is False


# ------------------------------------------------ 扫描僵死标志

def test_owner_alive_for_current_process():
    assert ep._owner_alive(f"{socket.gethostname()}:{os.getpid()}") is True


def test_owner_from_recreated_container_is_dead():
    assert ep._owner_alive("old-container-id:1") is False


def test_owner_with_dead_pid_is_dead():
    assert ep._owner_alive(f"{socket.gethostname()}:{os.getpid() + 999999}") is False


def test_scan_clears_zombie_flag(monkeypatch):
    """上次 worker 被回收留下的 scan_running=1 不能挡 12h。"""
    async def fake_probe(slot, timeout=12.0):
        return "10.1.1.1"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)

    async def go():
        await ep._b().hset(ep._meta_key(), mapping={
            "scan_running": 1, "scan_started": int(ep.time.time()),
            "scan_owner": "dead-container:1"})
        return await ep.scan([1])

    res = asyncio.run(go())
    assert not res.get("skipped"), "归属进程已死时必须接管，而不是跳过"
    assert res["distinct_ips"] == 1


def test_scan_skips_when_owner_alive(monkeypatch):
    async def fake_probe(slot, timeout=12.0):
        return "10.1.1.1"

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)

    async def go():
        await ep._b().hset(ep._meta_key(), mapping={
            "scan_running": 1, "scan_started": int(ep.time.time()),
            "scan_owner": f"{socket.gethostname()}:{os.getpid()}"})
        return await ep.scan([1])

    res = asyncio.run(go())
    assert res.get("skipped") is True


def test_legacy_record_does_not_backfill_n_ok():
    """缺 n_ok 时不得用 used 回填（2026-11-22 修正）。

    置账槽位的 used 被强制设成 quota，回填会把「一张没出」读成「出了 30 张」：
    线上正是这个 bug 让面板 real_ok_today 显示 13346，而账本真实只有 1755。
    """
    async def go():
        await ep._b().hset(ep._slot_key(3), mapping={
            "day": ep._today(), "ip": "10.0.0.3", "state": ep.STATE_PRIMARY,
            "used": 17, "quota": 30})
        return await ep.get_slot(3)

    s = asyncio.run(go())
    assert s.n_ok == 0, "缺失即未知，宁可记 0 也不能拿 used 冒充产出"
    assert s.used == 17, "used 本身不受影响"
    assert s.booked is False


def test_new_record_with_zero_n_ok_is_not_backfilled():
    async def go():
        await ep._b().hset(ep._slot_key(4), mapping={
            "day": ep._today(), "ip": "10.0.0.4", "state": ep.STATE_PRIMARY,
            "used": 0, "quota": 30, "n_ok": 0})
        return await ep.get_slot(4)

    assert asyncio.run(go()).n_ok == 0
