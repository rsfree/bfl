"""浏览器出口客户端：多实例轮询与故障切换的单元测试。"""

import asyncio
import base64

import pytest

from bfl2openai import browser_egress_client as cl

_IMG = base64.b64encode(b"fake-png-bytes").decode()
_OK = (200, '{"image_b64": "%s"}' % _IMG)


def _run(coro):
    return asyncio.run(coro)


def _call():
    return cl.generate(b"png", "p", "1:1", "1k", "i2i")


def test_endpoints_parses_comma_list(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       " http://a:8011/ , http://b:8011 ")
    assert cl._endpoints() == ["http://a:8011", "http://b:8011"]
    assert cl.configured() is True


def test_endpoints_empty(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS", "")
    assert cl._endpoints() == []
    assert cl.configured() is False


def test_round_robin_alternates(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       "http://a:8011,http://b:8011")
    seen = []

    def fake_post(url, payload):
        seen.append(url)
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    _run(_call())
    _run(_call())
    assert seen == ["http://a:8011", "http://b:8011"]


def test_failover_on_503(monkeypatch):
    """一个实例熔断（503）时，另一个实例仍应出图。"""
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       "http://a:8011,http://b:8011")
    cl._rr = 0
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        if url == "http://a:8011":
            return 503, '{"error":"breaker open"}'
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    out = _run(_call())
    assert out == b"fake-png-bytes"
    assert calls == ["http://a:8011", "http://b:8011"]


def test_failover_on_transport_error(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       "http://a:8011,http://b:8011")
    cl._rr = 0

    def fake_post(url, payload):
        if url == "http://a:8011":
            raise OSError("connection refused")
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    assert _run(_call()) == b"fake-png-bytes"


def test_no_failover_on_content_reject(monkeypatch):
    """451 是语义结果（内容违规），换实例重试没有意义。"""
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       "http://a:8011,http://b:8011")
    cl._rr = 0
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        return 451, '{"error":"content moderation"}'

    monkeypatch.setattr(cl, "_post", fake_post)
    with pytest.raises(cl.BrowserEgressError) as ei:
        _run(_call())
    assert ei.value.status == 451
    assert len(calls) == 1
