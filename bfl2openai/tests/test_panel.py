"""管理中台：只读注册表、改配额生效、token 可选校验。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import egress_pool as ep  # noqa: E402
from bfl2openai import server  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "x")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()

    async def fake_probe(slot, timeout=12.0):
        return {1: "1.1.1.1", 2: "1.1.1.1"}[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    import asyncio
    asyncio.run(ep.scan())
    yield TestClient(server.app)
    ep.reset_backend()


def test_page_renders(client):
    r = client.get("/panel")
    assert r.status_code == 200
    assert "出口池中台" in r.text


def test_snapshot_reports_collision(client):
    r = client.get("/panel/api/snapshot")
    assert r.status_code == 200
    d = r.json()
    assert d["slots"] == 1, "索引里只保留唯一 IP 的那个槽位"
    assert d["dups"] == 1, "同 IP 的第二个槽位应被剔除"
    assert d["usable"] == 1
    assert d["quota_per_ip"] == 30


def test_quota_update_takes_effect(client):
    r = client.post("/panel/api/quota", json={"slot": 1, "quota": 5})
    assert r.status_code == 200
    d = client.get("/panel/api/snapshot").json()
    row = [x for x in d["rows"] if x["slot"] == 1][0]
    assert row["quota"] == 5
    assert row["remaining"] == 5


def test_quota_requires_fields(client):
    r = client.post("/panel/api/quota", json={"slot": 1})
    assert r.status_code == 400


def test_soft_quota_update_takes_effect(client):
    """软上限是实际停手点：调小它应当立即降低可用容量。"""
    r = client.post("/panel/api/quota", json={"slot": 1, "soft": 4})
    assert r.status_code == 200
    d = client.get("/panel/api/snapshot").json()
    row = [x for x in d["rows"] if x["slot"] == 1][0]
    assert row["soft_quota"] == 4
    assert row["remaining_safe"] == 4
    assert d["capacity_left"] == 4, "容量按软上限算"


def test_token_guard_blocks_without_header(client, monkeypatch):
    import bfl2openai.panel as panel_mod
    monkeypatch.setattr(panel_mod, "_TOKEN", "s3cret")
    assert client.get("/panel/api/snapshot").status_code == 401
    ok = client.get("/panel/api/snapshot",
                    headers={"authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_panel_requires_basic_auth_when_password_set(client, monkeypatch):
    """配了 BFL2PANEL_PASS 后，页面与 API 都必须要凭据，且 401 要带
    WWW-Authenticate（否则浏览器不会弹窗）。"""
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    r = client.get("/panel")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")

    assert client.get("/panel/api/snapshot").status_code == 401


def test_panel_accepts_correct_password(client, monkeypatch):
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    # 用户名不校验，只看密码
    assert client.get("/panel", auth=("any", "sk-bfl")).status_code == 200
    assert client.get("/panel/api/snapshot",
                      auth=("any", "sk-bfl")).status_code == 200


def test_panel_rejects_wrong_password(client, monkeypatch):
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    assert client.get("/panel", auth=("any", "sk-wrong")).status_code == 401


def test_panel_bearer_token_still_works(client, monkeypatch):
    """脚本/curl 的 Bearer 通道不能被 Basic Auth 挤掉。"""
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "tok123")

    r = client.get("/panel", headers={"Authorization": "Bearer tok123"})
    assert r.status_code == 200
