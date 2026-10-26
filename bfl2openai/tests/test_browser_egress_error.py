"""browser egress 失败必须映射成对客户端有意义的码，而不是 500。

回归背景（生产 2026-10-26）：`browser_egress_client` 抛裸 `RuntimeError`，
`run_job` 只捕 `UpstreamError` / `MPSError` ⇒ 冒到 ASGI 层变 **500 + 21 字节空体**。
3 小时内 50 次，客户端拿不到原因，把「上游暂时不可用」当「服务端崩溃」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import browser_egress_client as bec  # noqa: E402
from bfl2openai import server  # noqa: E402


def _err(status: int, text: str) -> bec.BrowserEgressError:
    return bec.BrowserEgressError(f'browser egress {status}: {text}', status)


# ---------------------------------------------------------------- mapping


@pytest.mark.parametrize(("exc", "code", "kind"), [
    # 匿名配额：退避重试，不是「服务崩了」
    (_err(429, '{"error":"You\'ve used today\'s 30 free generations."}'),
     429, "rate_limit"),
    # 内容审核/合规
    (_err(451, '{"error":"flagged"}'), 451, "content_policy_violation"),
    # 传输层（连接失败/超时）
    (bec.BrowserEgressError("browser egress transport: timed out", 0),
     504, "upstream_error"),
    # 上游契约 400（如多图 composition）对调用方是上游错误，不是它的请求错
    (_err(502, '{"error":"submit 400: A composition must not include a canvas image."}'),
     502, "upstream_error"),
    # sidecar 自身 500 不对外暴露成 500
    (_err(500, "Internal Server Error"), 502, "upstream_error"),
    # 200 但缺图
    (bec.BrowserEgressError('browser egress no image: {"status":"ok"}', 200),
     502, "upstream_error"),
])
def test_status_mapping(exc, code, kind):
    resp = server.wrap_browser_egress_error(exc)
    assert resp.status_code == code
    body = resp.body and __import__("json").loads(resp.body)
    assert body["error"]["type"] == kind
    assert body["error"]["message"]


def test_quota_message_hides_free_tier_detail():
    resp = server.wrap_browser_egress_error(
        _err(429, '{"error":"30 free generations"}'))
    assert "free generations" not in __import__("json").loads(resp.body)["error"]["message"]


# ---------------------------------------------------------------- end to end


@pytest.fixture()
def sidecar_primary(monkeypatch):
    """让 run_job 走 browser egress 分支（不碰 curl_cffi、不碰上游）。"""
    monkeypatch.setattr(server, "_BROWSER_PRIMARY", True)
    monkeypatch.setattr(bec, "configured", lambda: True)
    # 容器/开发机可能带 BFL2OPENAI_API_KEY；测试只关心错误映射，关掉鉴权
    monkeypatch.setattr(server, "API_KEY", "")
    return monkeypatch


def test_generation_returns_502_not_500(sidecar_primary):
    async def boom(**kwargs):
        raise bec.BrowserEgressError(
            'browser egress 502: {"error":"submit 400: A composition must not '
            'include a canvas image."}', 502)

    sidecar_primary.setattr(bec, "generate", boom)
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={"prompt": "a red fox"})
    assert r.status_code == 502, r.text
    assert "composition" in r.json()["error"]["message"]


def test_generation_returns_429_on_quota(sidecar_primary):
    async def boom(**kwargs):
        raise bec.BrowserEgressError('browser egress 429: {"error":"quota"}', 429)

    sidecar_primary.setattr(bec, "generate", boom)
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={"prompt": "a red fox"})
    assert r.status_code == 429, r.text
    assert r.json()["error"]["type"] == "rate_limit"


def test_success_path_still_ok(sidecar_primary):
    """映射改动不能把正常路径带坏。"""
    async def fake_generate(**kwargs):
        return b"\x89PNG-fake"

    sidecar_primary.setattr(bec, "generate", fake_generate)
    client = TestClient(server.app)
    r = client.post("/v1/images/generations",
                    json={"prompt": "a red fox", "response_format": "b64_json"})
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["b64_json"]
