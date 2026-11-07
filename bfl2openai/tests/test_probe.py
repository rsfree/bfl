"""端到端探针：PNG 校验、URL 本地化、状态路由、鉴权。

不测真实跑图（那会产生上游任务），只钉住探针的判定逻辑和接口契约。
"""
from __future__ import annotations

import io

import pytest


def test_png_size_accepts_valid_image():
    from PIL import Image

    from bfl2openai.probe import _png_size

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 30, 30)).save(buf, "PNG")
    assert _png_size(buf.getvalue()) == "64x48"


def test_png_size_rejects_junk():
    from bfl2openai.probe import _png_size

    with pytest.raises(Exception):
        _png_size(b"this is definitely not an image")


def test_localise_rewrites_host_to_loopback(monkeypatch):
    """polling_url 要用回环跑：探针测服务本身，不测 nginx/公网。"""
    from bfl2openai import probe

    monkeypatch.setattr(probe, "BASE", "http://127.0.0.1:8000")
    assert probe._localise(
        "https://bfl.1task.cn/v1/get_result?id=abc") == \
        "http://127.0.0.1:8000/v1/get_result?id=abc"


def test_probe_route_reports_state(monkeypatch):
    from fastapi.testclient import TestClient

    from bfl2openai import native, probe, server

    monkeypatch.setattr(probe, "ENABLE", False)
    monkeypatch.setattr(native, "NATIVE_API_KEY", "")
    with TestClient(server.app) as c:
        r = c.get("/v1/probe")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False
        assert "interval_s" in body
        assert "last" in body


def test_probe_run_requires_x_key(monkeypatch):
    """?run=1 会创建真实上游任务，必须过原生门的鉴权。"""
    from fastapi.testclient import TestClient

    from bfl2openai import native, probe, server

    monkeypatch.setattr(probe, "ENABLE", False)
    monkeypatch.setattr(native, "NATIVE_API_KEY", "sk-probe-test")
    with TestClient(server.app) as c:
        r = c.get("/v1/probe", params={"run": 1})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid x-key"
