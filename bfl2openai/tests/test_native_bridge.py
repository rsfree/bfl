"""原生端点异步桥：/v1/flux-3-image 经 sidecar 出图，get_result 读状态。

背景（2026-10-26 矩阵测试）：上游封 curl_cffi 指纹后，原生方言直连 submit
恒 403 Access denied => 502。桥 = 受理即回 id，后台任务跑 sidecar，
get_result 只读 store 状态（零上游请求）。

全部走假 sidecar / 假 MPS：钉住翻译层与状态机，不碰计费路径。
"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import native  # noqa: E402
from bfl2openai import server  # noqa: E402
from bfl2openai import browser_egress_client as bec  # noqa: E402

BUF = io.BytesIO()
Image.new("RGB", (32, 32), (200, 60, 50)).save(BUF, format="PNG")
_PNG = BUF.getvalue()
_B64 = base64.b64encode(_PNG).decode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(native, "NATIVE_API_KEY", "")


@pytest.fixture()
def client():
    # 必须用上下文管理器：portal 常驻，后台桥任务才能跨请求活到 Ready
    # （裸 TestClient 每请求一个临时 loop，create_task 的任务会被掐死）
    with TestClient(server.app) as c:
        yield c


@pytest.fixture()
def bridge(monkeypatch):
    """sidecar 在线 + generate 假实现；返回捕获到的调用参数。

    mps 显式关掉：本机 shell 常带 TENCENT_* 环境变量，会把无 COS 的
    代理样张分支悄悄变成 COS 分支，测试就不确定了。
    """
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(native.mps, "configured", lambda: False)
    calls: dict = {}

    async def fake_generate(**kwargs):
        calls.update(kwargs)
        return _PNG

    monkeypatch.setattr(bec, "generate", fake_generate)
    return calls


def _poll(client: TestClient, token: str, tries: int = 200) -> dict:
    body: dict = {}
    for _ in range(tries):
        body = client.get("/v1/get_result", params={"id": token}).json()
        if body.get("status") in ("Ready", "Error"):
            return body
        time.sleep(0.05)
    return body


def test_t2i_bridge_ready_with_proxy_sample(client, bridge):
    r = client.post("/v1/flux-3-image",
                    json={"prompt": "a red cube", "resolution": "1k"})
    assert r.status_code == 200
    doc = r.json()
    assert doc["id"] and "/v1/get_result?id=" in doc["polling_url"]

    body = _poll(client, doc["id"])
    assert body["status"] == "Ready", body
    assert f"/v1/flux3-image/results/{doc['id']}" in body["result"]["sample"]

    img = client.get(f"/v1/flux3-image/results/{doc['id']}")
    assert img.status_code == 200 and img.content == _PNG
    # t2i：无画布、无参考图
    assert bridge["mode"] == "t2i"
    assert bridge["image_png"] == b""
    assert bridge["refs"] == []


def test_multi_image_bridges_as_composition(client, bridge):
    r = client.post("/v1/flux-3-image", json={
        "prompt": "swap the plate",
        "images": [_B64, _B64],
        "resolution": "1k",
    })
    assert r.status_code == 200
    _poll(client, r.json()["id"])
    # canvas 与 references 互斥：并入首位、mode 置 t2i（同 server.run_job）
    assert bridge["mode"] == "t2i"
    assert bridge["image_png"] == b""
    assert len(bridge["refs"]) == 2


def test_bridge_error_surfaces_in_poll(client, monkeypatch):
    monkeypatch.setattr(bec, "configured", lambda: True)

    async def fail(**kwargs):
        raise bec.BrowserEgressError("browser egress 503: breaker open", 503)

    monkeypatch.setattr(bec, "generate", fail)
    r = client.post("/v1/flux-3-image", json={"prompt": "x", "resolution": "1k"})
    assert r.status_code == 200  # 受理成功，错误延后到轮询
    body = _poll(client, r.json()["id"])
    assert body["status"] == "Error"
    assert "breaker" in body["details"]["error"]


def test_bridge_composite_4k_runs_upscale(client, bridge, monkeypatch):
    monkeypatch.setattr(native.mps, "configured", lambda: True)

    async def fake_up(key, magnify, data):
        assert magnify == 2
        return _PNG

    uploaded: list[str] = []

    async def fake_upload(data, key):
        uploaded.append(key)

    monkeypatch.setattr(native.mps, "ci_super_resolve_cos", fake_up)
    monkeypatch.setattr(native.mps, "upload_to_cos", fake_upload)
    monkeypatch.setattr(native.mps, "cos_presigned_get",
                        lambda path, ttl_s=3600: f"https://cos.test{path}")

    r = client.post("/v1/flux-3-image",
                    json={"prompt": "x", "resolution": "4k"})
    assert r.status_code == 200
    body = _poll(client, r.json()["id"])
    assert body["status"] == "Ready", body
    assert body["result"]["sample"].startswith("https://cos.test/results/")
    # 复合：先按 2k 匿名上限生成，再超分 x2
    assert bridge["resolution"] == "2k"


def test_direct_path_still_works_without_sidecar(client, monkeypatch):
    """sidecar 不在（本地开发）：保持原直连路径，提交行为不变。"""
    monkeypatch.setattr(bec, "configured", lambda: False)
    submitted: dict = {}

    async def fake_submit(**kwargs):
        submitted.update(kwargs)
        return "tok123"

    monkeypatch.setattr(native.upstream, "submit_generation", fake_submit)
    r = client.post("/v1/flux-3-image", json={"prompt": "x", "resolution": "1k"})
    assert r.status_code == 200
    assert r.json()["id"] == "tok123"
    assert submitted["mode"] == "t2i"
