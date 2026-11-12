"""images 门幂等：同参数请求在窗口内只真正生成一次。

背景（2026-11-09）：到机房的 TCP 握手丢包 20–58%，客户端超时重试是常态。
`/v1/images/generations` 是同步长请求（18–45s），没有幂等时每次重试都会
再生成一遍 —— 白烧上游额度、白等一遍，两次结果还不一样。这里锁住三条
不变量：串行重试不重生成、并发重试不重生成、参数不同必须各生成一次。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bfl2openai import server


@pytest.fixture(autouse=True)
def _noauth(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.fixture()
def fake_impl(monkeypatch):
    """替掉真正的生成实现，只数调用次数。"""
    calls: list[dict] = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return server.JSONResponse(
            {"created": 1, "data": [{"url": "https://example.test/a.png"}]})

    monkeypatch.setattr(server, "_run_job_impl", impl)
    return calls


PAYLOAD = {"prompt": "a lighthouse on a cliff", "size": "1024x1024"}


def test_serial_retry_generates_once(client, fake_impl):
    """客户端超时后重发同样的请求：不该再生成一遍。"""
    r1 = client.post("/v1/images/generations", json=PAYLOAD)
    r2 = client.post("/v1/images/generations", json=PAYLOAD)

    assert r1.status_code == 200 and r2.status_code == 200
    assert len(fake_impl) == 1, "重试触发了重复生成"
    assert r2.headers.get("X-Bfl-Idempotent") == "replay"
    assert r1.json() == r2.json()


def test_different_prompt_generates_twice(client, fake_impl):
    """参数不同就不能合并，否则会串图。"""
    client.post("/v1/images/generations", json=PAYLOAD)
    client.post("/v1/images/generations",
                json={**PAYLOAD, "prompt": "a red sports car"})

    assert len(fake_impl) == 2


def test_different_size_generates_twice(client, fake_impl):
    """size 影响出图尺寸，必须进指纹。"""
    client.post("/v1/images/generations", json=PAYLOAD)
    client.post("/v1/images/generations",
                json={**PAYLOAD, "size": "1536x1024"})

    assert len(fake_impl) == 2


def test_fingerprint_is_order_and_type_stable():
    assert (server.job_fingerprint(a="1", b="2")
            == server.job_fingerprint(b="2", a="1"))
    assert (server.job_fingerprint(img=b"\x89PNG")
            != server.job_fingerprint(img=b"\x89PNJ"))


def test_oversize_result_not_cached(client, monkeypatch):
    """b64 大响应不落缓存：等待者宁可 504，也不该把 Redis 挤爆。"""
    monkeypatch.setattr(server, "_IDEM_MAX_CACHE_BYTES", 10)

    async def impl(**kwargs):
        return server.JSONResponse({"data": [{"b64_json": "x" * 1000}]})

    monkeypatch.setattr(server, "_run_job_impl", impl)
    r1 = client.post("/v1/images/generations", json=PAYLOAD)
    r2 = client.post("/v1/images/generations", json=PAYLOAD)

    # 第一次正常返回；缓存被跳过，第二次只能重新生成（不是 replay）
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("X-Bfl-Idempotent") is None


def test_follower_takes_over_when_leader_fails(client, fake_impl):
    """leader 挂了（锁消失但没有结果）时，等待者必须接管重跑，不能回 504。

    回归背景（2026-11-12 18:34–19:02）：单飞 leader 失败释放锁后，follower
    的等待循环 break 出来直接 return 504，40 次客户端重试全被打成 504，
    报文 107 字节、与 `idem wait timeout` 日志逐秒对齐。客户端把它当服务故障
    继续重试 → 又生成一轮，越重试越糟。
    """
    import asyncio

    fp = server.job_fingerprint(prompt="leader-dies")
    asyncio.run(server.store.set_nx(f"idem:{fp}", {"ts": 0}, 60))

    async def _drop_lock_soon():
        await asyncio.sleep(0.3)
        await server.store.drop(f"idem:{fp}")

    async def _produce():
        return server.JSONResponse(
            {"created": 1, "data": [{"url": "https://example.test/ok.png"}]})

    async def _scenario():
        dropper = asyncio.create_task(_drop_lock_soon())
        resp = await server._idem_run(fp, _produce)
        await dropper
        return resp

    resp = asyncio.run(_scenario())
    assert resp.status_code == 200, "follower 没有接管，仍在回错误"
    assert len(fake_impl) == 0   # produce 是测试自带的 lambda，不走 fake_impl


def test_large_body_is_cached_with_short_ttl(client, monkeypatch):
    """2k b64 单图（中位 7.7MB）必须进缓存，否则幂等对最常见请求完全失效。"""
    big = "x" * 8_000_000
    calls = []

    async def impl(**kwargs):
        calls.append(kwargs)
        return server.JSONResponse({"created": 1, "data": [{"b64_json": big}]})

    monkeypatch.setattr(server, "_run_job_impl", impl)
    payload = {"prompt": "a very large 2k render", "size": "1024x1024"}

    r1 = client.post("/v1/images/generations", json=payload)
    r2 = client.post("/v1/images/generations", json=payload)

    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 1, "8MB 结果没被缓存，重试又生成了一遍"
    assert r2.headers.get("X-Bfl-Idempotent") == "replay"


def test_stale_lock_is_stolen(client, monkeypatch):
    """leader 进程已死留下的僵尸锁必须被抢，不能让 follower 干等 240s。

    回归背景（2026-11-13 03:14 发版后）：容器重建会把正在执行的那条锁留在
    Redis 里活到 TTL 结束，同指纹请求看到「锁还在」就一路等到 _IDEM_WAIT_S，
    最后回 504 —— 而那个任务根本没人跑了。
    """
    import asyncio
    import time as _t

    monkeypatch.setattr(server, "_IDEM_STALE_S", 30.0)
    fp = server.job_fingerprint(prompt="zombie-lock")
    # 心跳停在 5 分钟前：锁还在，但持有者显然已经不在了
    asyncio.run(server.store.put(f"idem:{fp}", {"ts": _t.time() - 300}, 900))

    calls = []

    async def _produce():
        calls.append(1)
        return server.JSONResponse({"created": 1, "data": [{"url": "u"}]})

    resp = asyncio.run(server._idem_run(fp, _produce))

    assert resp.status_code == 200, "僵尸锁没有被抢，follower 又被打回 504"
    assert calls == [1]
