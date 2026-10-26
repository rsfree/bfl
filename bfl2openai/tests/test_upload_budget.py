"""上传体积预算：浏览器出口路径不能绕过 4MB 规范化。

回归（2026-10-26 实测）：切到 `BROWSER_PRIMARY=1` 后，原始字节直接进 sidecar，
11.5MB 的图本可压到 2.76MB，实际吃上游 413，还被包成 502。
"""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import payload  # noqa: E402
from bfl2openai import server  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
SAMPLE = TESTS_DIR / "sample_input.png"


def _noise_png(size: int = 1400) -> bytes:
    """不可压缩噪声图：1400² 的 RGB 噪声 ≈ 5.9MB PNG，稳定超过 4MB 预算。"""
    random.seed(3)
    im = Image.frombytes("RGB", (size, size), random.randbytes(size * size * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _noauth(monkeypatch):
    monkeypatch.setattr(server, "API_KEY", "")


@pytest.fixture()
def client():
    with TestClient(server.app) as c:
        yield c


# ---------------------------------------------------------------- 预算函数


def test_single_image_shrinks_below_budget():
    big = _noise_png()
    assert len(big) > payload.UPSTREAM_BODY_BUDGET
    out, refs = payload.fit_body_budget(big, [])
    assert refs == []
    assert len(out) <= payload.UPSTREAM_BODY_BUDGET


def test_joint_budget_splits_between_main_and_refs():
    big = _noise_png()
    out_main, out_refs = payload.fit_body_budget(big, [big, big])
    assert len(out_refs) == 2
    total = len(out_main) + sum(len(r) for r in out_refs)
    # 3.0MB 实际可用 + 每张最低 300KB 的下限容差
    assert total <= payload.UPSTREAM_BODY_BUDGET * 1.10


def test_refs_only_gets_the_whole_budget():
    big = _noise_png()
    out_main, out_refs = payload.fit_body_budget(b"", [big, big])
    assert out_main == b""
    assert len(out_refs) == 2
    assert all(len(r) <= payload.UPSTREAM_BODY_BUDGET for r in out_refs)


def test_small_image_passes_through_unchanged():
    small = SAMPLE.read_bytes()
    out, _ = payload.fit_body_budget(small, [])
    assert out == small  # PNG 且远小于预算：不该重编码


# ---------------------------------------------------------------- 端到端


@pytest.fixture()
def sidecar(monkeypatch):
    """sidecar 在线，捕获它实际收到的字节。"""
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    seen: dict = {}

    async def fake_generate(**kwargs):
        seen.update(kwargs)
        return b"\x89PNG-ok"

    monkeypatch.setattr(bec, "generate", fake_generate)
    return seen


def test_primary_path_feeds_sidecar_compressed_bytes(client, sidecar, monkeypatch):
    monkeypatch.setattr(server.mps, "configured", lambda: False)
    big = _noise_png()
    with SAMPLE.open("rb") as ref:
        r = client.post(
            "/v1/images/edits",
            files=[("image", ("big.png", io.BytesIO(big), "image/png")),
                   ("image", ("ref.png", ref, "image/png"))],
            data={"prompt": "smooth gradient", "size": "1024x1024"})
    assert r.status_code == 200, r.text
    assert len(sidecar["image_png"]) == 0          # canvas 并入 refs 首位
    total = sum(len(b) for b in sidecar["refs"])
    assert total <= payload.UPSTREAM_BODY_BUDGET * 1.10
    assert all(len(b) < len(big) for b in sidecar["refs"])


def test_413_from_sidecar_maps_to_413(client, monkeypatch):
    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(bec, "configured", lambda: True)

    async def fail(**kwargs):
        raise bec.BrowserEgressError(
            'browser egress 502: {"error":"submit 413: Request Entity Too Large"}', 502)

    monkeypatch.setattr(bec, "generate", fail)
    r = client.post("/v1/images/generations",
                    json={"prompt": "x", "size": "1024x1024"})
    assert r.status_code == 413
    body = r.json()["error"]
    assert body["type"] == "invalid_request_error"
    assert "downscale" in body["message"]
