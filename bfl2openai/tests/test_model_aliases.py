"""模型别名：flux-3-image / flux-3 / flux-kontext-max / flux-kontext-pro → 同一个上游模型。

背景（2026-10-26）：客户端与网关按 fal 的叫法传 `flux-kontext-pro` 之类的名字，
而适配器里模型名本来就不参与路由（真正决定模型的是 `BFL2OPENAI_MODEL`）。
别名要做的事只有两件：**在 /v1/models 里被看见**，以及**请求里出现时被认出来**
（响应头回写 requested/resolved，便于确认没落到别的渠道）。

默认宽松：未登记的名字照样出图（兼容层的本分），只留一条日志；
`BFL2OPENAI_MODEL_STRICT=1` 才收紧成 404 model_not_found。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import server  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
SAMPLE = TESTS_DIR / "sample_input.png"

ALIASES = ["flux-3-image", "flux-3", "flux-kontext-max", "flux-kontext-pro"]


@pytest.fixture(autouse=True)
def _noauth(monkeypatch):
    """测试环境不校验 key，避免本机 shell 里的 API_KEY 干扰。"""
    monkeypatch.setattr(server, "API_KEY", "")


@pytest.fixture()
def client():
    return TestClient(server.app)


# ---------------------------------------------------------------- 列表与发现


def test_models_lists_canonical_then_aliases(client):
    body = client.get("/v1/models").json()
    ids = [m["id"] for m in body["data"]]
    assert ids == [server.MODEL_ID, *ALIASES]
    # 别名用 root 指回规范名（OpenAI 的「同一个模型的另一个名字」语义）
    for entry in body["data"][1:]:
        assert entry["root"] == server.MODEL_ID
    assert "root" not in body["data"][0]


def test_healthz_exposes_aliases(client):
    body = client.get("/healthz").json()
    assert body["model"] == server.MODEL_ID
    assert body["aliases"] == ALIASES


# ---------------------------------------------------------------- 名称解析


@pytest.mark.parametrize("spelling", [
    "flux-kontext-max", "FLUX-KONTEXT-MAX", " flux-kontext-max ",
])
def test_alias_is_case_and_space_insensitive(spelling):
    route = server.model_route(spelling)
    assert route["known"] is True
    assert route["denied"] is False
    assert route["resolved"] == server.MODEL_ID
    assert route["alias"] == "flux-kontext-max"


def test_canonical_name_is_not_an_alias():
    route = server.model_route(server.MODEL_ID)
    assert route["known"] is True and route["alias"] is None


def test_short_alias_flux_3_is_recognized():
    """`flux-3` 是网关渠道里实际在用的短名，必须与 `flux-3-image` 同为已登记。

    它与 `flux-3-image` 不是前缀关系就能互认——登记表是精确比对（仅忽略大小写
    与首尾空白），所以短名要单独占一项。
    """
    route = server.model_route("flux-3")
    assert route["known"] is True
    assert route["denied"] is False
    assert route["alias"] == "flux-3"
    assert route["resolved"] == server.MODEL_ID


def test_missing_model_uses_default():
    route = server.model_route(None)
    assert route == {"requested": "", "resolved": server.MODEL_ID,
                     "alias": None, "known": True, "denied": False}


def test_unregistered_model_is_lenient_by_default():
    route = server.model_route("gpt-image-1")
    assert route["known"] is False and route["denied"] is False


def test_strict_mode_denies_unregistered(monkeypatch):
    monkeypatch.setattr(server, "_MODEL_STRICT", True)
    assert server.model_route("gpt-image-1")["denied"] is True
    # 别名在严格模式下依然放行
    assert server.model_route("flux-3-image")["denied"] is False


def test_env_list_dedupes_and_splits_semicolons(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_MODEL_ALIASES", " a , A ,b;c ,,")
    assert server._env_list("BFL2OPENAI_MODEL_ALIASES", "") == ["a", "b", "c"]


# ---------------------------------------------------------------- 端到端（假上游）


@pytest.fixture()
def fake_job(monkeypatch):
    """把 run_job 换掉：只验证路由/头，不碰任何真实生成。"""
    seen: dict = {}

    async def _job(**kwargs):
        seen.update(kwargs)
        return JSONResponse({"created": 1, "data": [{"b64_json": "AA=="}]})

    monkeypatch.setattr(server, "run_job", _job)
    return seen


def test_generations_accepts_alias_and_echoes_headers(client, fake_job):
    resp = client.post("/v1/images/generations", json={
        "model": "flux-kontext-pro", "prompt": "a red cube",
    })
    assert resp.status_code == 200
    assert resp.headers["X-Bfl-Model-Requested"] == "flux-kontext-pro"
    assert resp.headers["X-Bfl-Model-Resolved"] == server.MODEL_ID
    # 上游参数不受影响：仍是 t2i
    assert fake_job["mode"] == "t2i"


def test_edits_accepts_alias(client, fake_job):
    with SAMPLE.open("rb") as fh:
        resp = client.post(
            "/v1/images/edits",
            files={"image": ("canvas.png", fh, "image/png")},
            data={"prompt": "warmer light", "model": "flux-3-image"},
        )
    assert resp.status_code == 200
    assert resp.headers["X-Bfl-Model-Requested"] == "flux-3-image"
    assert fake_job["image_png"]


def test_strict_mode_returns_404_before_upstream(client, fake_job, monkeypatch):
    monkeypatch.setattr(server, "_MODEL_STRICT", True)
    resp = client.post("/v1/images/generations", json={
        "model": "flux-kontext-ultra", "prompt": "x",
    })
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "model_not_found"
    assert fake_job == {}  # 没有触达生成链路
