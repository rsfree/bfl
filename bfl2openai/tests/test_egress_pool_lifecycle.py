"""出口池生命周期：浏览器实例不能泄漏。

回归背景（2026-11-13 03:00 线上）：`bfl-browser-egress-2` 里同时挂着
**24 个 Chromium 主进程**、cgroup 内存 2.96/3 GiB、一小时内 24 次 memcg OOM
（kill 目标全是 chromium）。原因不是负载太高，而是 `ensure_pool()` 重建池子时
只把 `_state` 里的引用换掉、从不关闭旧实例：每个 page 崩溃都新起一整个
Chromium，旧的那组 6~8 个进程留在 cgroup 里直到 OOM-killer 出手。而 OOM 又
反过来杀渲染进程 → 页面 is_closed() → 再重建 → 再泄漏，形成正反馈。

2026-11-18 起 `_state["browser"]` 换成分片（`_state["shards"]`）：每片一个
独立 pw+Chromium。下面四条规定因此按"片"来锁：
  1. 分片都活着且池子非空 ⇒ 直接复用，不重启；
  2. **单页**崩溃不触发重建（交给 _retire / _fill_pool_gap 原位处理）；
  3. 某片掉线时只重建该片，且旧的 browser / playwright 必须被关掉；
  4. 页面没建全就抛错时，半成品浏览器也要收干净。
"""
from __future__ import annotations

import asyncio
import importlib.util

import pytest

pytest.importorskip("playwright.async_api",
                    reason="出口 sidecar 的依赖不在 app 测试环境里")
spec = importlib.util.spec_from_file_location(
    "bfl_egress_main",
    __file__.replace("bfl2openai/tests/test_egress_pool_lifecycle.py",
                     "browser_egress/main.py"))
egress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egress)


class _FakePage:
    def __init__(self, closed: bool = False):
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


class _FakeBrowser:
    def __init__(self, connected: bool = True):
        self.closed = False
        self.connected = connected
        self.version = "test-1.0"

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class _FakePW:
    def __init__(self, launches: list | None = None):
        self.stopped = False
        self._launches = launches if launches is not None else []

    async def stop(self) -> None:
        self.stopped = True

    @property
    def chromium(self):
        outer = self

        class _C:
            async def launch(self, **kwargs):
                outer._launches.append(kwargs)
                return _FakeBrowser()
        return _C()


def _entry(idx: int = 0, closed: bool = False) -> dict:
    return {"idx": idx, "page": _FakePage(closed), "inflight": 0, "submits": 0,
            "errors": 0, "err_streak": 0, "cooldown_until": 0.0,
            "recycled": 0, "eval_lock": asyncio.Lock()}


def _install_healthy_shards() -> list[dict]:
    """装上 SHARDS 个健康分片，返回它们，便于断言是否被关闭。"""
    shards = [{"pw": _FakePW(), "browser": _FakeBrowser()}
              for _ in range(egress.SHARDS)]
    egress._state["shards"] = shards
    return shards


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    egress._state.update({
        "pool": [], "browser": None, "pw": None, "sem": None,
        "total_inflight": 0, "shards": [],
        # 每个用例重建 lock：asyncio.run 每次新建事件循环
        "shard_locks": [asyncio.Lock() for _ in range(egress.SHARDS)],
        "shard_cursor": 0,
    })
    egress._state["shard_block"].clear()
    egress._state["refill_backoff"].clear()
    launches: list[dict] = []

    class _PWLauncher:
        def __init__(self):
            self.pw = _FakePW(launches)

        async def start(self):
            return self.pw

    monkeypatch.setattr(egress, "async_playwright", lambda: _PWLauncher())

    async def _fake_new_page(browser, idx, version, **kw):
        return _entry(idx)

    monkeypatch.setattr(egress, "_new_page", _fake_new_page)
    yield launches
    egress._state.update({
        "pool": [], "browser": None, "pw": None, "sem": None,
        "total_inflight": 0, "shards": [],
    })
    egress._state["shard_block"].clear()
    egress._state["refill_backoff"].clear()


def test_reuses_live_browser(_reset_state):
    """分片都活着且池子非空：不该重启（每次重启都是一份常驻内存）。"""
    shards = _install_healthy_shards()
    egress._state["pool"] = [_entry(0), _entry(1)]

    pool = asyncio.run(egress.ensure_pool())

    assert len(pool) == 2
    assert _reset_state == [], "健康池子不该重新 launch chromium"
    assert all(s["browser"].closed is False for s in shards)


def test_single_dead_page_does_not_rebuild_pool(_reset_state):
    """单页崩溃只该摘牌补位，不该重建浏览器（泄漏的触发点）。"""
    shards = _install_healthy_shards()
    egress._state["pool"] = [_entry(0), _entry(1, closed=True)]

    pool = asyncio.run(egress.ensure_pool())

    assert len(pool) == 2
    assert _reset_state == [], "单页崩溃触发了重建"
    assert all(s["browser"].closed is False for s in shards)


def test_rebuild_closes_previous_browser(_reset_state):
    """某片掉线时只重建该片，旧浏览器与 playwright 驱动都要关掉。"""
    shards = _install_healthy_shards()
    old = shards[0]
    old_browser, old_pw = old["browser"], old["pw"]
    old_browser.connected = False          # 模拟该片掉线
    egress._state["pool"] = [_entry(0, closed=True)]

    asyncio.run(egress.ensure_pool())

    assert old_browser.closed is True, "旧 chromium 没被关闭（泄漏）"
    assert old_pw.stopped is True, "旧 playwright 驱动没被停止（泄漏）"
    assert len(_reset_state) == 1, "只该重建掉线的那一片"
    # 其余片不能被牵连
    assert all(s["browser"].closed is False for s in shards[1:])


def test_partial_launch_failure_does_not_leak(_reset_state, monkeypatch):
    """页面没建全就抛错时，半成品浏览器也要收干净。"""
    class _BoomBrowser(_FakeBrowser):
        pass

    launched: list[_BoomBrowser] = []

    class _PWLauncher2:
        async def start(self):
            class _PW:
                async def stop(self):
                    pass

                @property
                def chromium(self):
                    class _C:
                        async def launch(self, **kwargs):
                            b = _BoomBrowser()
                            launched.append(b)
                            return b
                    return _C()
            return _PW()

    monkeypatch.setattr(egress, "async_playwright", lambda: _PWLauncher2())

    async def _boom(browser, idx, version, **kw):
        # 让第 2 页失败（必定属于某个分片）
        if idx == 2:
            raise RuntimeError("page.goto aborted")
        return _entry(idx)

    monkeypatch.setattr(egress, "_new_page", _boom)

    with pytest.raises(RuntimeError):
        asyncio.run(egress._ensure_shard(2))

    assert launched and launched[0].closed is True, "半成品浏览器泄漏了"


def test_cgroup_mem_reads_limit(monkeypatch):
    """内存水位读取：拿不到就返回 None，不能抛。"""
    real_open = open

    def _fake_open(path, *a, **kw):
        if path == "/sys/fs/cgroup/memory.current":
            return _Str("900")
        if path == "/sys/fs/cgroup/memory.max":
            return _Str("1000")
        return real_open(path, *a, **kw)

    class _Str:
        def __init__(self, v):
            self.v = v

        def read(self):
            return self.v

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("builtins.open", _fake_open)
    assert egress._cgroup_mem() == (900, 1000)
