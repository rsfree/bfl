"""sidecar 页面重建退避的回归锁（2026-11-18）。

背景：`_fill_pool_gap` 每 15 秒补一页，补不上就把坏页摘出池子、下个 tick
再补。上游限流时 12 个槽位 30 分钟内触发 129 次整页重载（每次 = 一个
Chromium 页面完整跑一遍 SPA），把 8 核打满：load 22.9、运行队列 39，
同期 25.9 天的平均 CPU 利用率只有 4.4%。

这里锁四件事：
  1) 退避序列按 base 翻倍、封顶在 MAX；
  2) 重建成功后清零；
  3) `_fill_pool_gap` 不会挑中还在退避里的槽位；
  4) `recycle_entry` 在退避期内不重建，且必须把废页摘出池子
     （只 return 不摘，它会带着 want_recycle 每 15 秒被重排一次）。
"""

import asyncio
import importlib.util
import pathlib

import pytest

_SIDE = pathlib.Path(__file__).resolve().parents[2] / "browser_egress" / "main.py"


class _FakePage:
    def is_closed(self):
        return False


class _FakeCtx:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeBrowser:
    version = "fake-1.0"

    def is_connected(self):
        return True


def _entry(idx, *, with_ctx=False):
    e = {
        "idx": idx, "page": _FakePage(), "cooldown_until": 0.0,
        "inflight": 0, "submits": 0, "errors": 0, "recycled": 0,
        "gen": 0, "recycling": False, "want_recycle": False,
    }
    if with_ctx:
        e["ctx"] = _FakeCtx()
    return e


@pytest.fixture(scope="module")
def side():
    spec = importlib.util.spec_from_file_location("bfl_sidecar_main", _SIDE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _reset(side):
    # 每个用例重建 lock：asyncio.run 会新建事件循环，模块级 Lock 复用会
    # 报 "attached to a different loop"。
    side._state["lock"] = asyncio.Lock()
    side._state["recycle_lock"] = asyncio.Lock()
    side._state["refill_backoff"].clear()
    side._state["pool"] = []
    yield
    side._state["refill_backoff"].clear()
    side._state["pool"] = []
    side._state["browser"] = None


def test_backoff_sequence_doubles_and_caps(side):
    base = side.RECYCLE_BACKOFF_BASE_S
    cap = side.RECYCLE_BACKOFF_MAX_S
    got = [side._backoff_fail(3) for _ in range(6)]
    expect = [min(cap, base * (2 ** i)) for i in range(6)]
    assert got == expect
    # 序列必须单调不减，且末项等于封顶值
    assert got == sorted(got)
    assert got[-1] == cap


def test_backoff_ok_clears_state(side):
    side._backoff_fail(1)
    side._backoff_fail(1)
    assert side._backoff_fails(1) == 2
    assert side._backoff_wait(1) > 0
    side._backoff_ok(1)
    assert side._backoff_fails(1) == 0
    assert side._backoff_wait(1) == 0.0


def test_fill_pool_gap_skips_slot_in_backoff(side, monkeypatch):
    calls = []

    async def fake_new_page(browser, idx, version, gen=0):
        calls.append(idx)
        return _entry(idx)

    monkeypatch.setattr(side, "_new_page", fake_new_page)
    side._state["browser"] = _FakeBrowser()
    # 缺 idx 0 和 1，其中 0 在退避中 ⇒ 只能补 1
    side._state["pool"] = [_entry(i) for i in range(2, side.PAGES)]
    side._backoff_fail(0)

    asyncio.run(side._fill_pool_gap())

    assert calls == [1], f"退避中的槽位被重试了：{calls}"


def test_fill_pool_gap_noop_when_all_slots_backing_off(side, monkeypatch):
    calls = []

    async def fake_new_page(*a, **k):
        calls.append(a)
        return _entry(0)

    monkeypatch.setattr(side, "_new_page", fake_new_page)
    side._state["browser"] = _FakeBrowser()
    side._state["pool"] = []
    for i in range(side.PAGES):
        side._backoff_fail(i)

    asyncio.run(side._fill_pool_gap())

    assert calls == [], "整池都在退避时仍尝试重建"


def test_recycle_entry_defers_and_detaches(side, monkeypatch):
    calls = []

    async def fake_new_page(*a, **k):
        calls.append(a)
        return _entry(0)

    monkeypatch.setattr(side, "_new_page", fake_new_page)
    side._state["browser"] = _FakeBrowser()
    e = _entry(0, with_ctx=True)
    side._state["pool"] = [e]
    side._backoff_fail(0)

    asyncio.run(side.recycle_entry(e))

    assert calls == [], "退避期内仍执行了页面重建"
    assert e["ctx"].closed, "废页 ctx 未关闭，会泄漏"
    assert side._state["pool"] == [], "废页没摘出池子，会被反复重排"
    assert e["recycling"] is False, "recycling 标记未复位"


def test_recycle_entry_rebuilds_when_backoff_expired(side, monkeypatch):
    calls = []

    async def fake_new_page(browser, idx, version, gen=0):
        calls.append(idx)
        return _entry(idx)

    monkeypatch.setattr(side, "_new_page", fake_new_page)
    side._state["browser"] = _FakeBrowser()
    e = _entry(0, with_ctx=True)
    side._state["pool"] = [e]
    side._backoff_fail(0)
    side._backoff_ok(0)          # 退避到期（等价于已清零）

    asyncio.run(side.recycle_entry(e))

    assert calls == [0], "退避已清空时应当重建"
    assert side._state["pool"][0]["recycled"] == 1
