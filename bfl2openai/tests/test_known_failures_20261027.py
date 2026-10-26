"""四处线上缺陷的回归测试（2026-10-27 实测复现后修）。

每个用例都对应一次真实故障取证，不是假想的边界：

1. 坏图 -> 500 纯文本
   PIL 的 UnidentifiedImageError 继承 OSError，`except (ValueError, ...)`
   接不住 => Starlette 回 21 字节 "Internal Server Error"。客户端按 JSON
   解析拿到空 message，误判成"网关挂了"。
2. 第二张参考图坏 -> 同样 500
   代码注释承诺"丢弃坏图、余下照常生成"，但因同一异常缺口从未生效：
   客户传 5 张第 3 张坏，整单 500 一张不给。
3. 4k/8k 的 magnify 是 float
   COMPOSITE_TIERS={"4k":2.0} 直接拼 URL -> 数据万象 InvalidArgument
   "param magnify invalid"。CI 同步超分 100% 失败，退化到慢得多的 MPS
   任务；随后 media 变 None，len(None) 崩掉，已超分的图被丢弃。
4. 过期 id -> 502 且每次重试都真打上游
   bridge:{id} 只活 1h，COS 图活 24h；中间 23h 里 id 查不到，掉进上游
   兜底（把我们的 uuid 当 pollToken），上游 400 被包成 502，且无负缓存。
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import bridging  # noqa: E402
from bfl2openai import native  # noqa: E402
from bfl2openai import server  # noqa: E402
from bfl2openai import upstream  # noqa: E402

BUF = io.BytesIO()
Image.new("RGB", (32, 32), (200, 60, 50)).save(BUF, format="PNG")
_PNG = BUF.getvalue()
_B64 = base64.b64encode(_PNG).decode()
# 有 PNG 魔数、但解码会失败：正是线上那次 "Internal Server Error" 的输入
_BAD_B64 = base64.b64encode(b"\x89PNG" + b"0" * 20).decode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(native, "NATIVE_API_KEY", "")


@pytest.fixture()
def client():
    # raise_server_exceptions=False：要断言"我们返回了什么响应"，
    # 而不是让 TestClient 把异常重新抛出来。
    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def bridge(monkeypatch):
    """sidecar 在线；generate 立即返回一张合法 PNG（不碰上游）。"""
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(native.mps, "configured", lambda: False)
    calls: dict = {}

    async def fake_generate(**kwargs):
        calls.update(kwargs)
        return _PNG

    monkeypatch.setattr(bec, "generate", fake_generate)
    return calls


# ---------------------------------------------------------------- 1. 坏图


def test_undecodable_image_is_value_error_not_oserror():
    """_png_bytes 把 PIL 的 OSError 家族统一成 ValueError（调用方按它映射）。"""
    with pytest.raises(ValueError) as ei:
        native._png_bytes(b"\x89PNG" + b"0" * 20, "test")
    assert "undecodable" in str(ei.value)


def test_native_undecodable_first_image_returns_422(client):
    """首位图坏：422 JSON（此前是 500 纯文本 "Internal Server Error"）。"""
    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "resolution": "1k", "aspect_ratio": "1:1",
        "images": ["data:image/png;base64," + _BAD_B64]})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "images"]
    assert "undecodable" in detail[0]["msg"]


def test_native_undecodable_second_image_does_not_abort(client, bridge):
    """第二张图坏：丢弃它、用第一张继续生成（注释承诺的行为终于成立）。"""
    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "resolution": "1k", "aspect_ratio": "1:1",
        "images": ["data:image/png;base64," + _B64,
                   "data:image/png;base64," + _BAD_B64]})
    assert r.status_code == 200, r.text
    assert r.json()["id"]
    # 坏图被丢弃 => 只剩 canvas，没有 references
    assert bridge.get("refs") in (None, [])


def test_unhandled_exception_still_returns_json(client, monkeypatch):
    """兜底 handler：任何漏网异常都是 JSON 错误体，不是纯文本。"""
    async def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(native.mps, "configured", lambda: False)
    monkeypatch.setattr(bec, "generate", boom)

    r = client.get("/v1/get_result", params={"id": "does-not-exist"})
    # 走的是上游兜底，会真打上游；这里只要求"不是纯文本 500"
    assert r.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------- 2. magnify


def test_bridge_upscale_passes_integer_magnify(monkeypatch):
    """4k => magnify 必须是 int 2，不能是 2.0（CI 只认整数）。"""
    captured = {}

    async def fake_ci(ikey, magnify, data=None):
        captured["magnify"] = magnify
        return _PNG

    async def fake_upload(data, key):
        return None

    monkeypatch.setattr(native.mps, "upload_to_cos", fake_upload)
    monkeypatch.setattr(native.mps, "ci_super_resolve_cos", fake_ci)
    monkeypatch.setattr(native.mps, "cos_presigned_get", lambda k, ttl_s=0: "u")

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        native._bridge_upscale("tok", _PNG, native.COMPOSITE_TIERS["4k"]))
    assert captured["magnify"] == 2
    assert isinstance(captured["magnify"], int)


def test_composite_tier_values_are_float_but_handled(monkeypatch):
    """确认常量本身仍是 float（注释里的坑），但桥已强制转 int。"""
    assert native.COMPOSITE_TIERS["4k"] == 2.0
    assert isinstance(native.COMPOSITE_TIERS["4k"], float)


# ---------------------------------------------------------------- 3. 墓碑


def test_ready_writes_tombstone(client, bridge):
    """Ready 时同时写 bridge_done:{id}，TTL 对齐 COS 的 24h。"""
    from bfl2openai import store

    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "resolution": "1k", "aspect_ratio": "1:1"})
    token = r.json()["id"]

    import time
    for _ in range(100):
        if client.get("/v1/get_result", params={"id": token}).json().get("status") == "Ready":
            break
        time.sleep(0.05)

    tomb = store._MEM.get(f"bridge_done:{token}")
    assert tomb is not None, "墓碑未写入"
    assert json.loads(tomb[0])["sample"]
    assert native._BRIDGE_TOMBSTONE_TTL == 86400


def test_expired_state_still_resolves_via_tombstone(client, bridge):
    """状态被淘汰（=1 小时后）但墓碑在 => 仍返回 Ready，不 502。"""
    from bfl2openai import store

    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "resolution": "1k", "aspect_ratio": "1:1"})
    token = r.json()["id"]

    import time
    for _ in range(100):
        if client.get("/v1/get_result", params={"id": token}).json().get("status") == "Ready":
            break
        time.sleep(0.05)

    sample = json.loads(store._MEM[f"bridge_done:{token}"][0])["sample"]
    # 模拟 1 小时后：状态记录过期，墓碑仍在
    store._MEM.pop(f"bridge:{token}", None)

    body = client.get("/v1/get_result", params={"id": token}).json()
    assert body["status"] == "Ready"
    assert body["result"]["sample"] == sample


# ---------------------------------------------------------------- 4. 404


def test_unknown_id_is_404_not_502(client, monkeypatch):
    """上游说 invalid or expired => 404（不是 502，否则客户端无限重试）。"""
    async def fake_poll_once(token, *a, **kw):
        raise upstream.UpstreamError(
            'status failed: HTTP 400: {"status":"error",'
            '"error":"Invalid or expired image result token."}')

    monkeypatch.setattr(upstream, "poll_once", fake_poll_once)
    r = client.get("/v1/get_result", params={"id": "deadbeef"})
    assert r.status_code == 404, r.text
    assert "unknown or expired" in r.json()["detail"]


def test_unknown_id_negative_cached(client, monkeypatch):
    """第二次查询零上游请求：负缓存拦住，否则重试每次都打穿上游。"""
    from bfl2openai import store

    hits = {"n": 0}

    async def fake_poll_once(token, *a, **kw):
        hits["n"] += 1
        raise upstream.UpstreamError(
            'status failed: HTTP 400: {"status":"error",'
            '"error":"Invalid or expired image result token."}')

    monkeypatch.setattr(upstream, "poll_once", fake_poll_once)
    for _ in range(3):
        r = client.get("/v1/get_result", params={"id": "cached-deadbeef"})
        assert r.status_code == 404
    assert hits["n"] == 1, f"负缓存失效，上游被打了 {hits['n']} 次"


def test_upstream_5xx_still_502(client, monkeypatch):
    """真正的上游故障仍是 502 —— 不要把 404 的改动扩散到别处。"""
    async def fake_poll_once(token, *a, **kw):
        raise upstream.UpstreamError("status failed: HTTP 503: busy")

    monkeypatch.setattr(upstream, "poll_once", fake_poll_once)
    r = client.get("/v1/get_result", params={"id": "boom"})
    assert r.status_code == 502


# ---------------------------------------------------------------- 5. 768 档


def test_tier_for_long_edge_has_768():
    """上游确有 768 档（native 768sq 实测 768²），OpenAI 侧不该上舍入到 1k。"""
    assert bridging.tier_for_long_edge(768) == "768sq"
    assert bridging.tier_for_long_edge(512) == "768sq"
    assert bridging.tier_for_long_edge(1024) == "1k"
    assert bridging.tier_for_long_edge(1536) == "1.5k"
    assert bridging.tier_for_long_edge(2048) == "2k"
    assert bridging.tier_for_long_edge(4096) is None


def test_images_size_768_maps_to_768sq(client, bridge):
    """OpenAI generations size=768x768 => resolution 768sq 透到 sidecar。"""
    r = client.post("/v1/images/generations", json={
        "prompt": "x", "size": "768x768", "response_format": "url"})
    assert r.status_code == 200, r.text
    assert bridge.get("resolution") == "768sq"
