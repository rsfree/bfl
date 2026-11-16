"""多图 = 上游 composition 模式（canvas 与 references 互斥）。

实测契约（2026-10-26，经 sidecar）：
  i2i + canvas + refs   -> 400 "A composition must not include a canvas image."
  i2i + refs 无 canvas  -> 400 "An image is required for an edit."
  t2i + refs 无 canvas  -> 200 正常出图
所以带参考图时 canvas 必须并入 references 首位、mode 置 t2i。
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
from bfl2openai import server  # noqa: E402


def png_bytes(color=(200, 40, 40), size=(64, 64)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def data_uri(color=(40, 40, 200)) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes(color)).decode()


@pytest.fixture()
def seen(monkeypatch):
    """捕获真正下发到 sidecar 的参数，不发任何网络请求。"""
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return png_bytes((0, 0, 0))

    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    # 分支必须显式固定：2026-11-17 起默认已改为 single（官网分支），
    # 本文件的用例验证的是 composition 回退分支，不依赖默认值。
    monkeypatch.setattr(server, "REF_BRANCH", "multi")
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(bec, "generate", fake_generate)
    monkeypatch.setattr(server, "API_KEY", "")
    return captured


def test_edits_multi_image_becomes_composition(seen):
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[
            ("image", ("canvas.png", png_bytes(), "image/png")),
            ("image", ("ref.png", png_bytes((40, 40, 200)), "image/png")),
        ],
        data={"prompt": "put the blue square next to the red one",
              "response_format": "b64_json"},
    )
    assert r.status_code == 200, r.text
    assert seen["mode"] == "t2i", "composition 必须走 t2i（i2i 要求 canvas）"
    assert seen["image_png"] == b"", "canvas 不能再占据 image 部件"
    assert len(seen["refs"]) == 2, "canvas 要并入 references 首位"


def test_generations_multi_image_becomes_composition(seen):
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={
        "prompt": "merge these two",
        "image": [data_uri(), data_uri((40, 200, 40))],
        "response_format": "b64_json",
    })
    assert r.status_code == 200, r.text
    assert seen["mode"] == "t2i"
    assert seen["image_png"] == b""
    assert len(seen["refs"]) == 2


def test_single_image_keeps_edit_mode(seen):
    """回归：单图仍走 i2i + canvas，不能被归一化带偏。"""
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[("image", ("canvas.png", png_bytes(), "image/png"))],
        data={"prompt": "make it green", "response_format": "b64_json"},
    )
    assert r.status_code == 200, r.text
    assert seen["mode"] == "i2i"
    assert seen["image_png"], "单图必须保留 image 部件"
    assert seen["refs"] == []


def test_boxes_with_references_is_rejected(seen):
    """区域编辑 + 参考图在上游无解，必须明确拒绝而不是静默丢 boxes。"""
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[
            ("image", ("canvas.png", png_bytes(), "image/png")),
            ("image", ("ref.png", png_bytes((40, 40, 200)), "image/png")),
        ],
        data={
            "prompt": "turn the shirt red",
            "boxes": json.dumps([{
                "box": {"x": 0.2, "y": 0.2, "w": 0.3, "h": 0.3},
                "instruction": "red shirt",
            }]),
        },
    )
    assert r.status_code == 400, r.text
    assert "reference images" in r.json()["error"]["message"]
    assert not seen, "拒绝路径不该下发到 sidecar"


# ---------------------------------------------------------------- single 分支
# 2026-11-17 起的默认分支（官网 precise-editing 语义）：canvas 留在 image 部件，
# 素材走 `reference`(单数) 文件部件，`references` 退化成 id 数组，mode 保持 i2i。
@pytest.fixture()
def seen_single(monkeypatch):
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return png_bytes((0, 0, 0))

    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(server, "REF_BRANCH", "single")
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(bec, "generate", fake_generate)
    monkeypatch.setattr(server, "API_KEY", "")
    return captured


def test_edits_multi_image_uses_single_branch(seen_single):
    """有参考图时默认走官网分支：canvas 保留、素材走 reference(单数)。"""
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[
            ("image", ("canvas.png", png_bytes(), "image/png")),
            ("image", ("ref.png", png_bytes((40, 40, 200)), "image/png")),
        ],
        data={"prompt": "put the blue square next to the red one",
              "response_format": "b64_json"},
    )
    assert r.status_code == 200, r.text
    assert seen_single["mode"] == "i2i", "官网分支必须保持编辑语义"
    assert seen_single["image_png"], "canvas 必须留在 image 部件（保真的前提）"
    assert len(seen_single["refs"]) == 1, "refs 只能放真正的素材，不含 canvas"
    assert seen_single["ref_mode"] == "single"


def test_generations_multi_image_uses_single_branch(seen_single):
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={
        "prompt": "merge these two",
        "image": [data_uri(), data_uri((40, 200, 40))],
        "response_format": "b64_json",
    })
    assert r.status_code == 200, r.text
    assert seen_single["mode"] == "i2i"
    assert seen_single["image_png"]
    assert len(seen_single["refs"]) == 1
    assert seen_single["ref_mode"] == "single"


def test_single_branch_error_propagates_without_downgrade(seen_single, monkeypatch):
    """single 被上游拒绝时**不许**静默降级 composition。

    composition 已实测证伪：模糊 prompt 下会把整幅重画成无关物体，却仍返回
    200 + 正常尺寸。那种「看起来成功、内容全错」的降级比报错危险得多。
    """
    calls: list[dict] = []

    async def boom(**kwargs):
        calls.append(dict(kwargs))
        raise bec.BrowserEgressError("submit 400: invalid reference", 400)

    monkeypatch.setattr(bec, "generate", boom)
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[
            ("image", ("canvas.png", png_bytes(), "image/png")),
            ("image", ("ref.png", png_bytes((40, 40, 200)), "image/png")),
        ],
        data={"prompt": "x", "response_format": "b64_json"},
    )
    # 上游 400 经 wrap_browser_egress_error 映射为 502（既有契约，本次不动）
    assert r.status_code == 502, r.text
    assert len(calls) == 1, "只允许调用一次，不得降级重试"
    assert calls[0]["ref_mode"] == "single"
    # 对外不能泄漏上游原文
    assert "invalid reference" not in r.text
