"""size 像素写法 → 分辨率档位（tier_for_long_edge）。

背景（2026-10-26 矩阵测试抓到）：/v1/images/edits 传 size=1024x1024，输出却是
1536×1536 —— size 只决定宽高比，输出像素由 resolution 档位（默认 1.5k）决定。
OpenAI 客户端按 size 预期像素，这里把「像素长边」映射到最近的档位
（1024→1k / 1536→1.5k / 2048→2k，更大走既有升采样复合）。
显式 resolution 永远优先；比例写法（"3:2"）不参与映射。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import server  # noqa: E402
from bfl2openai.bridging import tier_for_long_edge  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
SAMPLE = TESTS_DIR / "sample_input.png"


@pytest.fixture(autouse=True)
def _noauth(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.mark.parametrize(("px", "tier"), [
    # 768 档是上游真实存在的档位（native `768sq` 实测真出 768x768，
    # 2026-10-27）。此前缺这一档，OpenAI 客户端传 size=768 会被上舍入
    # 到 1k 拿 1024²，"按 size 给像素"的约定就破了。
    (None, None), (0, None), (512, "768sq"), (768, "768sq"), (1024, "1k"),
    (1025, "1.5k"), (1536, "1.5k"), (1537, "2k"), (2048, "2k"),
    (2049, None), (4096, None),
])
def test_tier_mapping(px, tier):
    assert tier_for_long_edge(px) == tier


@pytest.fixture()
def fake_job(monkeypatch):
    seen: dict = {}

    async def _job(**kwargs):
        seen.update(kwargs)
        return JSONResponse({"created": 1, "data": [{"b64_json": "AA=="}]})

    monkeypatch.setattr(server, "run_job", _job)
    return seen


def test_generations_pixel_size_picks_tier(client, fake_job):
    r = client.post("/v1/images/generations",
                    json={"prompt": "x", "size": "1024x1024"})
    assert r.status_code == 200
    assert fake_job["resolution"] == "1k"
    assert fake_job["upscale_to"] is None


def test_generations_pixel_1536_maps_to_15k(client, fake_job):
    client.post("/v1/images/generations",
                json={"prompt": "x", "size": "1536x1024"})
    assert fake_job["resolution"] == "1.5k"


def test_generations_pixel_2048_maps_to_2k(client, fake_job):
    client.post("/v1/images/generations",
                json={"prompt": "x", "size": "2048x2048"})
    assert fake_job["resolution"] == "2k"


def test_generations_ratio_size_keeps_default_tier(client, fake_job):
    client.post("/v1/images/generations", json={"prompt": "x", "size": "3:2"})
    assert fake_job["resolution"] == "1.5k"


def test_generations_explicit_resolution_wins(client, fake_job):
    client.post("/v1/images/generations",
                json={"prompt": "x", "size": "1024x1024", "resolution": "2k"})
    assert fake_job["resolution"] == "2k"


def test_generations_huge_pixel_still_upscales(client, fake_job, monkeypatch):
    monkeypatch.setattr(server.mps, "configured", lambda: True)
    r = client.post("/v1/images/generations",
                    json={"prompt": "x", "size": "4096x4096"})
    assert r.status_code == 200
    assert fake_job["upscale_to"] == 4096


def test_edits_pixel_size_picks_tier(client, fake_job):
    with SAMPLE.open("rb") as fh:
        r = client.post("/v1/images/edits",
                        files={"image": ("in.png", fh, "image/png")},
                        data={"prompt": "warmer", "size": "1024x1024"})
    assert r.status_code == 200
    assert fake_job["resolution"] == "1k"


def test_edits_explicit_resolution_wins(client, fake_job):
    with SAMPLE.open("rb") as fh:
        r = client.post("/v1/images/edits",
                        files={"image": ("in.png", fh, "image/png")},
                        data={"prompt": "warmer", "size": "1024x1024",
                              "resolution": "1.5k"})
    assert r.status_code == 200
    assert fake_job["resolution"] == "1.5k"
