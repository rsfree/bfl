"""sidecar 页面重建退避 + 分片重建的回归锁（2026-11-18）。

两组背景：
  1) 退避：`_fill_pool_gap` 每 15 秒补一页，补不上就摘页、下个 tick 再补。
     上游限流时 12 槽位 30 分钟触发 129 次整页重载，把 8 核打满（load 22.9）。
  2) 分片：内存到 92% 时整池重启 = 该实例 40 秒零出口；三台撞到同一时刻
     （14:13–14:15）全站 502 33 次。改成每片独立 Chromium，只重建一片。

锁住的行为：
  - 退避序列按 base 翻倍、封顶 MAX；成功清零
  - 退避中的槽位不被 `_fill_pool_gap` 挑中
  - `recycle_entry` 在退避期内不重建，且把废页摘出池子
  - 分片分配覆盖全部页面且均匀
  - **重建一片时，其他片的页面必须留在池子里**（这是"不归零"的核心）
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

    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class _FakePw:
    async def stop(self):
        pass


def _entry(idx, *, with_ctx=False):
    e = {
        "idx": idx, "page": _FakePage(), "cooldown_until": 0.0,
        "inflight": 0, "submits": 0, "errors": 0, "recycled": 0,
        "gen": 0, "recycling": False, "want_recycle": False,
        "eval_lock": asyncio.Lock(),
    }
    if with_ctx:
        e["ctx"] = _FakeCtx()
    return e


def _install_shards(side):
    """装上 SHARDS 个假的健康分片。"""
    side._state["shards"] = [
        {"pw": _FakePw(), "browser": _FakeBrowser()}
        for _ in range(side.SHARDS)
    ]


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
    side._state["shard_locks"] = [asyncio.Lock() for _ in range(side.SHARDS)]
    side._state["shards"] = []
    side._state["shard_block"].clear()
    side._state["shard_cursor"] = 0
    side._state["refill_backoff"].clear()
    side._state["pool"] = []
    yield
    side._state["shards"] = []
    side._state["shard_block"].clear()
    side._state["refill_backoff"].clear()
    side._state["pool"] = []


# ── 退避 ────────────────────────────────────────────────────────────

def test_backoff_sequence_doubles_and_caps(side):
    base = side.RECYCLE_BACKOFF_BASE_S
    cap = side.RECYCLE_BACKOFF_MAX_S
    got = [side._backoff_fail(3) for _ in range(6)]
    expect = [min(cap, base * (2 ** i)) for i in range(6)]
    assert got == expect
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
    _install_shards(side)
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
    _install_shards(side)
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
    _install_shards(side)
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
    _install_shards(side)
    e = _entry(0, with_ctx=True)
    side._state["pool"] = [e]
    side._backoff_fail(0)
    side._backoff_ok(0)

    asyncio.run(side.recycle_entry(e))

    assert calls == [0], "退避已清空时应当重建"
    assert side._state["pool"][0]["recycled"] == 1


# ── 分片 ────────────────────────────────────────────────────────────

def test_shard_distribution_covers_all_pages(side):
    buckets = {}
    for i in range(side.PAGES):
        buckets.setdefault(side._shard_of(i), []).append(i)
    assert len(buckets) == side.SHARDS, f"分片数不符: {sorted(buckets)}"
    sizes = [len(v) for v in buckets.values()]
    assert max(sizes) - min(sizes) <= 1, f"分片不均: {sizes}"


def test_restart_shard_keeps_other_shards_serving(side, monkeypatch):
    """核心断言：重建一片时，其他片的页面必须还在池子里。"""
    observed_during = []

    async def fake_launch(k):
        # 此刻 _restart_shard 已摘掉该片的页、还没补回 —— 必须仍非空
        observed_during.append(len(side._state["pool"]))
        new_pages = [_entry(i) for i in range(side.PAGES)
                     if side._shard_of(i) == k]
        return _FakePw(), _FakeBrowser(), new_pages

    monkeypatch.setattr(side, "_launch_shard", fake_launch)
    _install_shards(side)
    side._state["pool"] = [_entry(i) for i in range(side.PAGES)]

    asyncio.run(side._restart_shard(0, "mem 92% >= hard 92%"))

    page_idxs = {e["idx"] for e in side._state["pool"]}
    assert page_idxs == set(range(side.PAGES)), "重建后池子未补齐"
    assert observed_during and observed_during[0] > 0, \
        "重建期间池子归零了 —— 分片没起作用"
    assert observed_during[0] == side.PAGES - len(
        [i for i in range(side.PAGES) if side._shard_of(i) == 0])
    # 重建完成后不该残留 block
    assert not any(side._shard_blocked(i) for i in range(side.PAGES))


def test_pick_shard_rotates(side):
    seq = [side._pick_shard() for _ in range(side.SHARDS * 2)]
    assert seq[:side.SHARDS] == list(range(side.SHARDS))
    assert seq[side.SHARDS:] == list(range(side.SHARDS))


# ── 优化1：重建前等在途请求结束 ─────────────────────────────────────

class _FakePg:
    def __init__(self):
        self.goto_calls = 0
        self.eval_calls = 0

    async def goto(self, url, **kw):
        self.goto_calls += 1

    async def evaluate(self, js, *a):
        self.eval_calls += 1
        return 0


def test_drain_waits_until_inflight_clears(side):
    import time as _t

    e = _entry(0)
    e["inflight"] = 1

    async def _run():
        async def _clear():
            await asyncio.sleep(0.6)
            e["inflight"] = 0
        t = asyncio.create_task(_clear())
        t0 = _t.time()
        left = await side._drain_shard([e], 5.0)
        await t
        return left, _t.time() - t0

    left, dur = asyncio.run(_run())
    assert left == 0, "在途请求跑完后不该报剩余"
    assert dur >= 0.5, f"没有真的等待（{dur:.2f}s）"


def test_drain_timeout_reports_leftover(side):
    e = _entry(0)
    e["inflight"] = 2
    left = asyncio.run(side._drain_shard([e], 0.3))
    assert left == 1, "超时应返回仍在途的页数"


def test_drain_budget_shrinks_near_limit(side, monkeypatch):
    monkeypatch.setattr(side, "_cgroup_mem", lambda: (99, 100))
    assert side._drain_budget() == 0.0, "99% 水位不该再等"
    monkeypatch.setattr(side, "_cgroup_mem", lambda: (97, 100))
    assert side._drain_budget() <= 5.0
    monkeypatch.setattr(side, "_cgroup_mem", lambda: (90, 100))
    assert side._drain_budget() == side.DRAIN_BUDGET_S


def test_restart_shard_teardown_happens_after_drain(side, monkeypatch):
    """核心：teardown 必须在在途请求归零之后。"""
    inflight_at_teardown = []

    async def fake_launch(k):
        return _FakePw(), _FakeBrowser(), [
            _entry(i) for i in range(side.PAGES) if side._shard_of(i) == k]

    monkeypatch.setattr(side, "_launch_shard", fake_launch)
    orig = side._teardown_shard

    async def spy(k):
        inflight_at_teardown.append(busy["inflight"])
        await orig(k)

    monkeypatch.setattr(side, "_teardown_shard", spy)
    _install_shards(side)
    busy = _entry(0)
    busy["inflight"] = 1
    side._state["pool"] = [busy] + [_entry(i) for i in range(1, side.PAGES)]

    async def _run():
        async def _clear():
            await asyncio.sleep(0.4)
            busy["inflight"] = 0
        t = asyncio.create_task(_clear())
        await side._restart_shard(0, "mem 94% >= hard 92%")
        await t

    asyncio.run(_run())
    # _restart_shard 与 _ensure_shard 都会调 teardown（后者是幂等清理），
    # 关键是每一次调用时该片都已无在途请求
    assert inflight_at_teardown, "teardown 没被调用"
    assert all(x == 0 for x in inflight_at_teardown), \
        f"teardown 时仍有在途请求 {inflight_at_teardown} —— 会抛 TargetClosedError"


# ── 优化2：页面级 reload ──────────────────────────────────────────

def test_reload_page_resets_submits(side):
    e = _entry(0)
    e["page"] = _FakePg()
    e["submits"] = 20

    asyncio.run(side._reload_page(e))

    assert e["page"].goto_calls == 1, "没有重新加载页面"
    assert e["submits"] == 0, "reload 后计数没清零"
    assert e["reloading"] is False, "reloading 标记未复位"


def test_reload_failure_retires_page(side):
    class _BoomPg(_FakePg):
        async def goto(self, url, **kw):
            raise RuntimeError("goto aborted")

    e = _entry(0)
    e["page"] = _BoomPg()
    retired = []

    side_backup = side._retire
    side._retire = lambda entry, reason: retired.append(reason)
    try:
        asyncio.run(side._reload_page(e))
    finally:
        side._retire = side_backup

    assert retired and "reload" in retired[0], "reload 失败没交给 retire"
    assert e["reloading"] is False


# ── 页面 JS 错误分级处置 ────────────────────────────────────────────

def test_page_eval_error_classification(side):
    assert side._page_eval_error(RuntimeError("Error: Page.evaluate: Event"))
    assert side._page_eval_error(RuntimeError("Page.evaluate: TypeError: x"))
    # 超时是慢不是坏，不该换页
    assert not side._page_eval_error(RuntimeError("Timeout 30000ms exceeded"))
    # 页面真死由 _page_dead 负责，不走这条
    assert not side._page_eval_error(
        RuntimeError("Target page, context or browser has been closed"))


def test_eval_error_cools_first_then_retires(side, monkeypatch):
    retired = []
    monkeypatch.setattr(side, "_retire", lambda e, r: retired.append(r))
    e = _entry(0)
    exc = RuntimeError("Error: Page.evaluate: Event")

    side._handle_page_exc(e, exc, "submit")
    assert retired == [], "第一次 JS 错误就摘牌 —— 一轮风暴会把整池 retire"
    assert e["eval_fails"] == 1
    assert e["cooldown_until"] > 0, "没有冷却该页"

    side._handle_page_exc(e, exc, "submit")
    assert len(retired) == 1, "连续第二次应当摘牌"


def test_page_dead_retires_immediately(side, monkeypatch):
    retired = []
    monkeypatch.setattr(side, "_retire", lambda e, r: retired.append(r))
    e = _entry(0)
    side._handle_page_exc(e, RuntimeError("Target closed"), "poll")
    assert len(retired) == 1, "页面真死应立刻摘牌，不该冷却等待"
