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


_LEAK_MARKERS = ("sin1::", "mqx5x", "pool.example.com", "socks5h://", "Zod",
                 "Invalid enum", "browser egress", "127.0.0.1")


def _assert_no_leak(body: str) -> None:
    for bad in _LEAK_MARKERS:
        assert bad not in body, f"响应体泄漏内部信息 {bad!r}: {body}"


def _err(status: int, text: str) -> bec.BrowserEgressError:
    return bec.BrowserEgressError(f'browser egress {status}: {text}', status)


# ---------------------------------------------------------------- mapping


@pytest.mark.parametrize(("exc", "code", "kind"), [
    # 匿名配额：退避重试，不是「服务崩了」
    (_err(429, '{"error":"You\'ve used today\'s 30 free generations."}'),
     429, "rate_limit_error"),
    # 内容审核/合规
    (_err(451, '{"error":"flagged"}'), 451, "content_policy_violation"),
    # 传输层（连接失败/超时）
    (bec.BrowserEgressError("browser egress transport: timed out", 0),
     504, "api_error"),
    # 上游契约 400（如多图 composition）对调用方是上游错误，不是它的请求错
    (_err(502, '{"error":"submit 400: A composition must not include a canvas image."}'),
     502, "api_error"),
    # sidecar 自身 500 不对外暴露成 500
    (_err(500, "Internal Server Error"), 502, "api_error"),
    # 200 但缺图
    (bec.BrowserEgressError('browser egress no image: {"status":"ok"}', 200),
     502, "api_error"),
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
    # 2026-11-17 起上游契约原文不再外泄（它会暴露内部拓扑与上游规则），
    # 客户端只看通用文案；细节在服务端日志里（已脱敏）。
    assert "composition" not in r.json()["error"]["message"]
    _assert_no_leak(r.json()["error"]["message"])


def test_generation_returns_429_on_quota(sidecar_primary):
    async def boom(**kwargs):
        raise bec.BrowserEgressError('browser egress 429: {"error":"quota"}', 429)

    sidecar_primary.setattr(bec, "generate", boom)
    client = TestClient(server.app)
    r = client.post("/v1/images/generations", json={"prompt": "a red fox"})
    assert r.status_code == 429, r.text
    assert r.json()["error"]["type"] == "rate_limit_error"


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


# ------------------------------------------------- 对外错误必须脱敏（2026-11-17）
# 上游原文含 CF ray id（暴露出口机房区域）、Zod 枚举、内部拓扑、代理 URL。
# 这些只允许进日志，不允许出现在 HTTP 响应体里。
_LEAKY = ('submit 400: Invalid enum sin1::mqx5x-1794832947662-54162c0d3047 '
          'browser egress 500 socks5h://user:pw@pool.example.com:2088')


@pytest.mark.parametrize("status,expect", [(451, 451), (429, 429), (0, 504),
                                           (502, 502), (500, 502),
                                           (503, 503)])
def test_browser_error_response_is_redacted(status, expect):
    resp = server.wrap_browser_egress_error(bec.BrowserEgressError(_LEAKY, status))
    assert resp.status_code == expect
    _assert_no_leak(resp.body.decode())


def test_upstream_error_response_is_redacted():
    from bfl2openai.upstream import UpstreamError
    resp = server.wrap_upstream_error(UpstreamError(_LEAKY, "api_error"))
    assert resp.status_code == 502
    _assert_no_leak(resp.body.decode())


def test_redact_strips_credentials_and_ray_ids():
    out = server._redact(_LEAKY)
    assert "sin1::" not in out and "mqx5x" not in out
    assert "user:pw" not in out, "凭据必须清掉"
    assert "<ray>" in out and "<redacted>://" in out


# -------------------------------------- 429 语义对齐（2026-11-17）
# 事故背景：sidecar 把上游 429 折叠成 502，客户端把「额度耗尽、应当退避」
# 读成「服务故障、立即重试」，14:41–17:30 放大出 718 次 504 + 242 次 502。
# 修复后：sidecar 透传 429；应用层对旧版 sidecar 的折叠做文案兜底。


def test_legacy_sidecar_502_is_remapped_to_429():
    """滚动发布期旧 sidecar 仍会把 429 塞进 502 的 body，必须兜住。"""
    exc = _err(502, '{"error":"submit 429: Too Many Requests"}')
    resp = server.wrap_browser_egress_error(exc)
    assert resp.status_code == 429, resp.body
    assert resp.headers.get("Retry-After"), "429 必须带 Retry-After"


def test_429_keeps_retry_after_and_no_upstream_text():
    exc = _err(429, '{"error":"You\'ve used today\'s 30 free generations."}')
    resp = server.wrap_browser_egress_error(exc)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"].isdigit()
    msg = __import__("json").loads(resp.body)["error"]["message"]
    assert msg == server._SAFE_ERRORS["rate_limit"]
    assert "free generations" not in msg


def test_contract_error_is_not_mistaken_for_rate_limit():
    """上游契约错误（composition）不能被误判成 429 —— 否则客户端会无脑重试。"""
    exc = _err(502, '{"error":"submit 400: A composition must not include '
                    'a canvas image."}')
    assert server.wrap_browser_egress_error(exc).status_code == 502


def test_upstream_direct_path_429_maps_to_429():
    from bfl2openai.upstream import UpstreamError
    resp = server.wrap_upstream_error(
        UpstreamError('generate submit failed after 4 attempts: '
                      'HTTP 429: {"error":"Too Many Requests"}'))
    assert resp.status_code == 429, resp.body
    assert resp.headers.get("Retry-After")
    assert __import__("json").loads(resp.body)["error"]["type"] == "rate_limit_error"


def test_upstream_explicit_rate_limit_kind_maps_to_429():
    from bfl2openai.upstream import UpstreamError
    resp = server.wrap_upstream_error(UpstreamError("whatever", "rate_limit"))
    assert resp.status_code == 429


def test_upstream_invalid_request_is_strictly_redacted():
    """上游 token 失效原文含内部标识，只允许进日志。"""
    from bfl2openai.upstream import UpstreamError
    resp = server.wrap_upstream_error(
        UpstreamError('{"error":"pollToken 8f3a2b1c invalid or expired"}',
                      "invalid_request_error"))
    assert resp.status_code == 400
    body = resp.body.decode()
    assert "8f3a2b1c" not in body
    assert body.count("invalid or expired request token") == 1


def test_phrase_map_swallows_quota_tail():
    """脱敏不能只吃掉数字，「明天再来」这半句同样泄漏额度恢复周期。"""
    out = server._redact("You've used today's 30 free generations. "
                         "Please come back tomorrow.")
    assert "<quota_exhausted>" in out
    assert "come back tomorrow" not in out
    assert "30" not in out


# --------------------------- 瞬态错误的退避语义（2026-11-21）
# rebind 超时是「此刻不可用」而不是「上游契约错误」：503 + Retry-After 让
# 客户端有序退避；压成 502 会被监控当成上游故障，也会诱发立即重试。


def test_rebind_503_maps_to_503_with_retry_after():
    exc = _err(503, '{"error":"rebind slot 42 failed: TimeoutError"}')
    resp = server.wrap_browser_egress_error(exc)
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After"), "503 必须带 Retry-After"


def test_timeout_504_carries_retry_after():
    resp = server.wrap_browser_egress_error(_err(0, "timed out"))
    assert resp.status_code == 504
    assert resp.headers.get("Retry-After"), "504 必须带 Retry-After"
