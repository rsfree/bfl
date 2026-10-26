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
