"""451（内容审核拒绝）负缓存：同指纹请求秒回，不再占用出口页面。

真实背景：单个客户端 2.3 小时打了 249 次 451，每次平均耗 14 秒浏览器
页面时间 —— 页面是最贵的资源，而拒绝是内容属性，同内容重提必然再拒。
"""
from __future__ import annotations

import asyncio

from bfl2openai import server, store


def _reset():
    store._backend = None if hasattr(store, "_backend") else None  # noqa: SLF001
    try:
        store.reset_backend()  # type: ignore[attr-defined]
    except AttributeError:
        pass


def _run(coro):
    return asyncio.run(coro)


def _make_produce(calls, status=451):
    async def produce():
        calls.append(1)
        return server.openai_error(
            status, "content policy violation", "content_policy_violation")

    return produce


def test_451_is_cached_and_replayed_without_rerunning(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(server, "_NEG_TTL_S", 1800)
    _reset()

    calls: list[int] = []
    r1 = _run(server._idem_run("fp-neg-1", _make_produce(calls)))
    assert r1.status_code == 451
    assert len(calls) == 1, "首次必须真的执行一次"
    assert r1.headers.get("x-bfl-idempotent") is None

    r2 = _run(server._idem_run("fp-neg-1", _make_produce(calls)))
    assert r2.status_code == 451
    assert r2.headers.get("x-bfl-idempotent") == "negative"
    assert len(calls) == 1, "命中负缓存不应再执行 produce"
    assert b"content_policy_violation" in r2.body


def test_negative_cache_can_be_disabled(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(server, "_NEG_TTL_S", 0)
    _reset()

    calls: list[int] = []
    _run(server._idem_run("fp-neg-2", _make_produce(calls)))
    r = _run(server._idem_run("fp-neg-2", _make_produce(calls)))
    assert r.status_code == 451
    assert len(calls) == 2, "TTL=0 时每次都要真的执行"


def test_only_451_is_negative_cached(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(server, "_NEG_TTL_S", 1800)
    _reset()

    calls: list[int] = []
    _run(server._idem_run("fp-neg-3", _make_produce(calls, status=502)))
    r = _run(server._idem_run("fp-neg-3", _make_produce(calls, status=502)))
    assert r.status_code == 502
    assert len(calls) == 2, "502 是环境状态，不能被负缓存固化"


def test_success_path_unaffected(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(server, "_NEG_TTL_S", 1800)
    _reset()

    calls: list[int] = []

    async def produce():
        calls.append(1)
        return server.JSONResponse({"data": [{"b64_json": "x"}]})

    r1 = _run(server._idem_run("fp-neg-4", produce))
    r2 = _run(server._idem_run("fp-neg-4", produce))
    assert r1.status_code == r2.status_code == 200
    assert r2.headers.get("x-bfl-idempotent") == "replay"
    assert len(calls) == 1
