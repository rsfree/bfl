"""composition 分支埋点：锁住「什么形状走哪条分支」。

这套断言的用途不是测上游，而是防回归：`b442808` 把多图从 i2i 改成 t2i 时
只验证了「200 + 出图」，没验证参考图是否被消费，结果静默降级进了生产。
埋点 + 这些断言让分支变化一定会被看见。
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import compose as compose_mod  # noqa: E402
from bfl2openai import server  # noqa: E402

PNG_A = b"canvas-bytes"
PNG_B = b"ref-bytes"


def _png(color=(200, 40, 40)) -> bytes:
    im = Image.new("RGB", (64, 64), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _data_uri(color) -> str:
    return "data:image/png;base64," + base64.b64encode(_png(color)).decode()


@pytest.fixture(autouse=True)
def _clean_stats():
    compose_mod.reset()
    yield
    compose_mod.reset()


# ---------------------------------------------------------------- 分支判定


def test_no_image_is_t2i_plain():
    p = compose_mod.plan(None, None, "t2i")
    assert p.branch == compose_mod.BRANCH_T2I
    assert p.mode_out == "t2i"
    assert not p.downgraded


def test_single_canvas_is_i2i():
    p = compose_mod.plan(PNG_A, None, "i2i")
    assert p.branch == compose_mod.BRANCH_I2I
    assert p.mode_out == "i2i"
    assert p.image_png == PNG_A
    assert p.reference_images == ()
    assert not p.downgraded


def test_canvas_plus_refs_downgrades_to_t2i():
    """现行生产行为：canvas 并入 references 首位，mode 强制 t2i。"""
    p = compose_mod.plan(PNG_A, [(PNG_B, "ref_2.png")], "i2i")
    assert p.branch == compose_mod.BRANCH_COMPOSE_CANVAS
    assert p.mode_in == "i2i" and p.mode_out == "t2i"
    assert p.downgraded is True
    assert p.image_png is None, "canvas 不能再占 image 部件"
    assert [nm for _, nm in p.reference_images] == ["canvas.png", "ref_2.png"]
    assert p.reference_images[0][0] == PNG_A


def test_refs_only_is_t2i():
    """无画布 + 垫图：i2i 会被上游拒绝（An image is required for an edit.）。"""
    p = compose_mod.plan(None, [(PNG_B, "ref_2.png")], "i2i")
    assert p.branch == compose_mod.BRANCH_COMPOSE_REFS_ONLY
    assert p.mode_out == "t2i"
    assert len(p.reference_images) == 1


def test_env_switch_keeps_i2i_shape(monkeypatch):
    """BFL2OPENAI_MULTI_IMAGE_MODE=i2i：回到 2026-10-25 的对照形状。"""
    monkeypatch.setattr(compose_mod, "MULTI_IMAGE_MODE", "i2i")
    p = compose_mod.plan(PNG_A, [(PNG_B, "ref_2.png")], "i2i")
    assert p.branch == compose_mod.BRANCH_COMPOSE_KEEP
    assert p.mode_out == "i2i"
    assert p.downgraded is False
    assert p.image_png == PNG_A, "对照形状必须保留 canvas 的 image 部件"
    assert [nm for _, nm in p.reference_images] == ["ref_2.png"]


def test_headers_expose_branch():
    p = compose_mod.plan(PNG_A, [(PNG_B, "ref_2.png")], "i2i")
    h = p.headers()
    assert h["X-Bfl-Branch"] == compose_mod.BRANCH_COMPOSE_CANVAS
    assert h["X-Bfl-Mode"] == "i2i->t2i"
    assert h["X-Bfl-Downgraded"] == "1"
    assert h["X-Bfl-Refs"] == "in=1,out=2"


def test_stats_count_branches():
    compose_mod.plan(PNG_A, None, "i2i")
    compose_mod.plan(None, None, "t2i")
    compose_mod.plan(PNG_A, [(PNG_B, "r.png")], "i2i")
    snap = compose_mod.snapshot()
    assert snap["total"] == 3
    assert snap["by_branch"][compose_mod.BRANCH_I2I] == 1
    assert snap["by_branch"][compose_mod.BRANCH_T2I] == 1
    assert snap["by_branch"][compose_mod.BRANCH_COMPOSE_CANVAS] == 1
    assert snap["downgraded"] == 1
    assert snap["downgraded_pct"] == pytest.approx(33.33, abs=0.01)


# ---------------------------------------------------------------- 端到端埋点


@pytest.fixture()
def _sidecar(monkeypatch):
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _png((0, 0, 0))

    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(bec, "configured", lambda: True)
    monkeypatch.setattr(bec, "generate", fake_generate)
    monkeypatch.setattr(server, "API_KEY", "")
    return captured


def test_two_images_carry_downgrade_headers(_sidecar):
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={
        "prompt": "merge these two",
        "image": [_data_uri((10, 10, 200)), _data_uri((10, 200, 10))],
        "response_format": "b64_json",
    })
    assert r.status_code == 200, r.text
    assert r.headers["X-Bfl-Branch"] == compose_mod.BRANCH_COMPOSE_CANVAS
    assert r.headers["X-Bfl-Mode"] == "i2i->t2i"
    assert r.headers["X-Bfl-Downgraded"] == "1"
    assert _sidecar["mode"] == "t2i"
    assert _sidecar["image_png"] == b""


def test_single_image_has_no_downgrade_header(_sidecar):
    client = TestClient(server.app)
    r = client.post(
        "/v1/images/edits",
        files=[("image", ("canvas.png", _png(), "image/png"))],
        data={"prompt": "make it green", "response_format": "b64_json"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["X-Bfl-Branch"] == compose_mod.BRANCH_I2I
    assert r.headers["X-Bfl-Downgraded"] == "0"
