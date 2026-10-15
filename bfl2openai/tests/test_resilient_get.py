"""_resilient_get: retry + proxy fallback, fabricated transports only."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import upstream  # noqa: E402


class _Resp:
    def __init__(self, status: int, body: bytes = b"ok"):
        self.status_code = status
        self.text = ""
        self.content = body


class _FakeSession:
    """Context-manager session whose GET pops scripted statuses."""

    statuses: list[int] = [200]
    calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, timeout=None):
        status = _FakeSession.statuses[
            min(_FakeSession.calls, len(_FakeSession.statuses) - 1)
        ]
        _FakeSession.calls += 1
        return _Resp(status)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _FakeSession.statuses = [200]
    _FakeSession.calls = 0
    monkeypatch.setattr(upstream, "FALLBACK_PROXY", "")
    monkeypatch.setattr(upstream, "_make_session",
                        lambda proxy=None, timeout=None: _FakeSession())
    monkeypatch.setattr(upstream, "_session_direct",
                        lambda: _FakeSession())
    monkeypatch.setattr(upstream.asyncio, "sleep", lambda s: _done())
    yield


def _done():
    async def _noop():
        return None

    return _noop()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_retries_429_then_succeeds():
    _FakeSession.statuses = [429, 200]
    data = _run(upstream._resilient_get("https://x/media", label="media"))
    assert data == b"ok"
    assert _FakeSession.calls == 2


def test_all_retries_exhausted_raises():
    _FakeSession.statuses = [429]
    with pytest.raises(upstream.UpstreamError) as exc:
        _run(upstream._resilient_get("https://x/media", label="media"))
    assert "3 attempts" in str(exc.value)
    assert _FakeSession.calls == 3  # direct only, no proxy configured


def test_400_is_an_answer_not_an_outage():
    _FakeSession.statuses = [400]
    with pytest.raises(upstream.UpstreamError) as exc:
        _run(upstream._resilient_get("https://x/media", label="media"))
    assert _FakeSession.calls == 1  # no retry on a plain business 400


def test_proxy_chain_when_configured(monkeypatch):
    """新架构（2026-10-15）：配置池时全部走代理，无 direct 前缀。"""
    monkeypatch.setattr(upstream, "FALLBACK_PROXY", "http://pool:2086")
    monkeypatch.setattr(upstream, "_DIRECT_ATTEMPTS", 2)
    monkeypatch.setattr(upstream, "_PROXY_ATTEMPTS", 2)
    _FakeSession.statuses = [429, 200]
    data = _run(upstream._resilient_get("https://x/media", label="media"))
    assert data == b"ok"
    assert _FakeSession.calls == 2
