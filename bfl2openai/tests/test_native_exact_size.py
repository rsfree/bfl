"""原生端点 width/height：官方精确尺寸契约的验证与落地。

官方 `/v1/flux-3-image`（openapi 2026-11-27）：
  * width/height 精确像素，[256, 6256]；
  * 文生图 <= 16.8 MP，带参考图 <= 4 MP；
  * 两者同时给，且覆盖 resolution / aspect_ratio。

Playground 上游只有档位+比例，所以适配器的落地是「最小可覆盖档位 +
最近比例 + cover-crop 到精确像素」。这些用例钉住：校验边界、档位/比例
选择、以及桥路径最终返回的字节尺寸（不是只看参数）。
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

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import native  # noqa: E402
from bfl2openai import server  # noqa: E402

BUF = io.BytesIO()
Image.new("RGB", (32, 32), (30, 120, 200)).save(BUF, format="PNG")
_PNG = BUF.getvalue()
_B64 = base64.b64encode(_PNG).decode()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")
    monkeypatch.setattr(native, "NATIVE_API_KEY", "")


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


@pytest.fixture()
def bridge(monkeypatch):
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


def _dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


# ---------------------------------------------------------------- validation


def test_absent_pair_is_none():
    assert native._parse_exact_size({"prompt": "x"}, has_refs=False) is None


def test_square_pair_accepted():
    assert native._parse_exact_size({"width": 2048, "height": 2048},
                                    has_refs=False) == (2048, 2048)


def test_integral_float_accepted():
    assert native._parse_exact_size({"width": 1024.0, "height": 768.0},
                                    has_refs=False) == (1024, 768)


def test_half_pair_refused():
    with pytest.raises(native._SizeError) as ei:
        native._parse_exact_size({"width": 2048}, has_refs=False)
    assert ei.value.field == "width"


def test_non_integer_refused():
    with pytest.raises(native._SizeError):
        native._parse_exact_size({"width": "2048", "height": 2048},
                                 has_refs=False)
    with pytest.raises(native._SizeError):
        native._parse_exact_size({"width": True, "height": 2048},
                                 has_refs=False)


def test_side_bounds():
    with pytest.raises(native._SizeError):
        native._parse_exact_size({"width": 100, "height": 2048}, has_refs=False)
    with pytest.raises(native._SizeError):
        native._parse_exact_size({"width": 6257, "height": 2048}, has_refs=False)


def test_text_to_image_budget():
    with pytest.raises(native._SizeError):
        native._parse_exact_size({"width": 6000, "height": 6000}, has_refs=False)
    # 16.8 MP 以内合法（6256x2688 = 16,816,128 <= 16,822,272）
    assert native._parse_exact_size({"width": 6256, "height": 2688},
                                    has_refs=False) == (6256, 2688)


def test_reference_image_budget_is_4mp():
    # 2048x2048 恰好 4 MP：允许
    assert native._parse_exact_size({"width": 2048, "height": 2048},
                                    has_refs=True) == (2048, 2048)
    with pytest.raises(native._SizeError) as ei:
        native._parse_exact_size({"width": 2304, "height": 2048}, has_refs=True)
    assert "4194304" in ei.value.message


# ---------------------------------------------------------------- mapping


@pytest.mark.parametrize("size,ratio", [
    ((2048, 2048), "1:1"),
    ((1024, 768), "4:3"),
    ((1024, 512), "2:1"),
    ((2048, 878), "21:9"),
])
def test_aspect_for_size(size, ratio):
    assert native._aspect_for_size(*size) == ratio


@pytest.mark.parametrize("size,tier,magnify", [
    ((720, 720), "768sq", None),
    ((1024, 768), "1k", None),
    ((1500, 1000), "1.5k", None),
    ((2048, 2048), "2k", None),
    ((3000, 2000), "4k", 2),
    ((5000, 3000), "8k", 4),
])
def test_tier_for_exact(size, tier, magnify):
    assert native._tier_for_exact(*size) == (tier, magnify)


def test_fit_exact_is_noop_when_already_matching():
    assert native._fit_exact(_PNG, 32, 32) is _PNG


def test_fit_exact_crops_then_resizes():
    src = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 10, 10)).save(src, format="PNG")
    out = native._fit_exact(src.getvalue(), 32, 32)
    assert _dims(out) == (32, 32)


# ---------------------------------------------------------------- endpoint


def test_exact_size_overrides_tier_and_ratio(client, bridge):
    r = client.post("/v1/flux-3-image", json={
        "prompt": "a cat", "width": 2048, "height": 2048,
        "resolution": "1k", "aspect_ratio": "16:9",
    })
    assert r.status_code == 200, r.text
    body = _poll(client, r.json()["id"])
    assert body["status"] == "Ready", body

    # width/height 覆盖：1k/16:9 -> 2k/1:1
    assert bridge["resolution"] == "2k"
    assert bridge["aspect_ratio"] == "1:1"

    img = client.get(f"/v1/flux3-image/results/{r.json()['id']}")
    assert img.status_code == 200
    assert _dims(img.content) == (2048, 2048)


def test_exact_4_3_requests_4_3_tier(client, bridge):
    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "width": 1024, "height": 768,
    })
    assert r.status_code == 200, r.text
    body = _poll(client, r.json()["id"])
    assert body["status"] == "Ready", body
    assert bridge["resolution"] == "1k"
    assert bridge["aspect_ratio"] == "4:3"
    img = client.get(f"/v1/flux3-image/results/{r.json()['id']}")
    assert _dims(img.content) == (1024, 768)


def test_half_pair_is_422(client, bridge):
    r = client.post("/v1/flux-3-image",
                    json={"prompt": "x", "width": 2048})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "width"]
    assert bridge == {}


def test_over_budget_is_422(client, bridge):
    r = client.post("/v1/flux-3-image",
                    json={"prompt": "x", "width": 6000, "height": 6000})
    assert r.status_code == 422
    assert bridge == {}


def test_reference_budget_is_422(client, bridge):
    r = client.post("/v1/flux-3-image", json={
        "prompt": "x", "images": [_B64],
        "width": 2304, "height": 2048,
    })
    assert r.status_code == 422
    assert "4194304" in r.json()["detail"][0]["msg"]
    assert bridge == {}


def test_exact_above_2k_requires_mps(client, bridge):
    r = client.post("/v1/flux-3-image",
                    json={"prompt": "x", "width": 3000, "height": 2000})
    assert r.status_code == 503
    assert "MPS" in r.json()["detail"]
    assert bridge == {}


def test_exact_above_2k_fits_upgraded_bytes(client, bridge, monkeypatch):
    monkeypatch.setattr(native.mps, "configured", lambda: True)
    uploaded: list[str] = []

    async def fake_up(key, magnify, data):
        assert magnify == 2
        return _PNG

    async def fake_upload(data, key):
        uploaded.append(key)

    monkeypatch.setattr(native.mps, "ci_super_resolve_cos", fake_up)
    monkeypatch.setattr(native.mps, "upload_to_cos", fake_upload)
    monkeypatch.setattr(native.mps, "cos_presigned_get",
                        lambda path, ttl_s=3600: f"https://cos.test{path}")

    r = client.post("/v1/flux-3-image",
                    json={"prompt": "x", "width": 3000, "height": 2000})
    assert r.status_code == 200, r.text
    body = _poll(client, r.json()["id"])
    assert body["status"] == "Ready", body
    assert bridge["resolution"] == "2k"
    assert any(k.endswith("-3000x2000.png") for k in uploaded)
