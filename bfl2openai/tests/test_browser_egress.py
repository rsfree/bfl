"""浏览器出口客户端：多实例分发、故障切换、冷却。

回归背景（2026-10-26 两实例上线实测）：`_rr` 是进程级游标，gunicorn 3 个
worker 各自从 0 起算 ⇒ 每个 worker 的首发请求都打到第一个实例，日志里
连续两次请求全在 172.18.0.10。低流量时等于只用了半套 IP 池。
"""

import asyncio
import base64

import pytest

from bfl2openai import browser_egress_client as cl

_IMG = base64.b64encode(b"fake-png-bytes").decode()
_OK = (200, '{"image_b64": "%s"}' % _IMG)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cl.reset_state()
    monkeypatch.setenv("BFL2OPENAI_BROWSER_EGRESS",
                       "http://a:8011,http://b:8011")
    yield
    cl.reset_state()


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
    seen = []

    def fake_post(url, payload):
        seen.append(url)
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    _run(_call())
    _run(_call())
    assert seen == ["http://a:8011", "http://b:8011"]


def test_each_process_gets_its_own_phase(monkeypatch):
    """worker 间相位不同：两个"新进程"的首发不该都落在同一实例。"""
    seen = []

    def fake_post(url, payload):
        seen.append(url)
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)

    cl.reset_state()
    cl._SEED = 5  # 模拟 A 进程
    _run(_call())

    cl._rr = 0
    cl._SEED = 6  # 模拟 B 进程（不同相位）
    _run(_call())

    assert len(set(seen)) == 2, f"两个进程首发都落到同一个实例: {seen}"


def test_failover_on_503(monkeypatch):
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        if url == "http://a:8011":
            return 503, '{"error":"breaker open"}'
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    assert _run(_call()) == b"fake-png-bytes"
    assert calls == ["http://a:8011", "http://b:8011"]


def test_failover_on_429_quota(monkeypatch):
    """429 = 该实例 8 个出口 IP 全打热；换一台是换一批 IP，值得重试。"""
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        if url == "http://a:8011":
            return 429, '{"error":"30 free generations"}'
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    assert _run(_call()) == b"fake-png-bytes"
    assert calls == ["http://a:8011", "http://b:8011"]


def test_429_on_last_instance_still_raises(monkeypatch):
    def fake_post(url, payload):
        return 429, '{"error":"quota"}'

    monkeypatch.setattr(cl, "_post", fake_post)
    with pytest.raises(cl.BrowserEgressError) as ei:
        _run(_call())
    assert ei.value.status == 429


def test_cooldown_skips_recently_failed_instance(monkeypatch):
    """429 过的实例进冷却，下一次请求先试另一台（不再固定打同一台）。"""
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        if url == "http://a:8011":
            return 429, "quota"
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    _run(_call())          # a 429 -> 落到 b，a 进入冷却
    calls.clear()
    _run(_call())          # 下一次应先试 b（a 在冷却里）
    assert calls[0] == "http://b:8011"


def test_failover_on_transport_error(monkeypatch):
    def fake_post(url, payload):
        if url == "http://a:8011":
            raise OSError("connection refused")
        return _OK

    monkeypatch.setattr(cl, "_post", fake_post)
    assert _run(_call()) == b"fake-png-bytes"


def test_no_failover_on_content_reject(monkeypatch):
    """451 是语义结果（内容违规），换实例重试没有意义。"""
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        return 451, '{"error":"content moderation"}'

    monkeypatch.setattr(cl, "_post", fake_post)
    with pytest.raises(cl.BrowserEgressError) as ei:
        _run(_call())
    assert ei.value.status == 451
    assert len(calls) == 1


def test_no_failover_on_502_contract_error(monkeypatch):
    """502 = 上游契约错误（如 composition 冲突），换实例只会重复同一个错。"""
    calls = []

    def fake_post(url, payload):
        calls.append(url)
        return 502, '{"error":"submit 400: composition"}'

    monkeypatch.setattr(cl, "_post", fake_post)
    with pytest.raises(cl.BrowserEgressError):
        _run(_call())
    assert len(calls) == 1
