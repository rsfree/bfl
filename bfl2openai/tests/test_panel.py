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


def test_inline_script_parses_with_node(client, tmp_path):
    '''面板整页 JS 必须能被 JS 引擎解析。

    真实事故（0.2.110）：`_PAGE` 是 Python 三引号字符串，源码里写的
    反斜杠 + 引号在运行时被吃掉反斜杠，esc() 的对象字面量变成两个引号
    连写 —— 整段 JS 语法错误，页面只剩静态壳（数据为空、日志区永远
    "加载中"）。所以校验必须针对**运行时**的 HTML，而不是 panel.py
    的源码文本。
    '''
    import re
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    js = re.findall(r"<script>(.*?)</script>", client.get("/panel").text, re.S)[-1]
    f = tmp_path / "panel.js"
    f.write_text(js)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_gate_hidden_rule_overrides_flex(client):
    """#gate 自带 display:flex，必须显式给 [hidden] 补 display:none。

    真实事故（0.2.107）：hideGate() 只设 `hidden` 属性，而 UA 样式的
    display:none 优先级低于作者样式 `#gate{display:flex}` ⇒ 登录成功后
    遮罩层永不消失。数据其实已经加载成功，用户看到的却是登录框一直
    停在"验证中/请输入面板密钥"，被误判成"密钥验证不通过"。
    """
    r = client.get("/panel")
    assert "#gate[hidden]{display:none}" in r.text


def test_logs_endpoint_captures_and_filters(client, monkeypatch):
    """运行日志：环形缓冲要能收、能按级别/关键字过滤，且要鉴权。"""
    import logging

    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    logging.getLogger("bfl2openai.tests").warning("slot 777 429 -> cooling")
    logging.getLogger("bfl2openai.tests").info("plain info line")

    assert client.get("/panel/api/logs").status_code == 401

    r = client.get("/panel/api/logs", auth=("x", "sk-bfl"))
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["entries"], list) and d["entries"]
    assert d["source"] in {"redis", "memory"}
    assert {"ts", "level", "logger", "msg"} <= set(d["entries"][0])

    warn = client.get("/panel/api/logs?level=WARNING", auth=("x", "sk-bfl")).json()
    assert all(e["level"] in {"WARNING", "ERROR", "CRITICAL"} for e in warn["entries"])

    hit = client.get("/panel/api/logs?q=777", auth=("x", "sk-bfl")).json()
    assert hit["entries"] and all("777" in e["msg"] for e in hit["entries"])


def test_request_logs_records_business_calls_only(client, monkeypatch):
    """请求记录：记业务路径，跳过面板自身轮询；支持错误过滤。"""
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    client.get("/v1/definitely-not-a-route")          # 404，业务路径 → 记
    client.get("/panel/api/snapshot", auth=("x", "sk-bfl"))  # 运维面 → 不记

    r = client.get("/panel/api/request_logs", auth=("x", "sk-bfl"))
    assert r.status_code == 200
    d = r.json()
    paths = [e["path"] for e in d["entries"]]
    assert "/v1/definitely-not-a-route" in paths
    assert not any(p.startswith("/panel") for p in paths)
    row = [e for e in d["entries"] if e["path"] == "/v1/definitely-not-a-route"][0]
    assert row["status"] == 404 and row["method"] == "GET" and "ms" in row

    err = client.get("/panel/api/request_logs?status=error",
                     auth=("x", "sk-bfl")).json()
    assert all(int(e["status"]) >= 400 for e in err["entries"])

    miss = client.get("/panel/api/request_logs?q=nothing-matches-xyz",
                      auth=("x", "sk-bfl")).json()
    assert miss["entries"] == []


def test_request_logs_alias(client, monkeypatch):
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")
    assert client.get("/pq/request_logs", auth=("x", "sk-bfl")).status_code == 200


def test_logs_alias_available_for_public_entry(client, monkeypatch):
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")
    assert client.get("/pq/logs", auth=("x", "sk-bfl")).status_code == 200


def test_snapshot_returns_503_when_store_unavailable(client, monkeypatch):
    """状态库抖动时面板应快失败给 503，而不是 500 + traceback。"""
    from bfl2openai import egress_pool

    async def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(egress_pool, "snapshot", boom)
    r = client.get("/panel/api/snapshot")
    assert r.status_code == 503
    assert r.json()["detail"] == "state store unavailable"


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


def test_panel_page_is_public_but_api_requires_key(client, monkeypatch):
    """照 workbuddy2api：页面壳公开（前端自己弹登录框），数据接口要 key。

    401 **不能**带 WWW-Authenticate —— 带了浏览器会弹原生 Basic 弹窗，
    把面板自己的登录框盖住。
    """
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    assert client.get("/panel").status_code == 200

    r = client.get("/panel/api/snapshot")
    assert r.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in r.headers}


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

    # 页面壳是公开的，要验的是数据接口
    assert client.get("/panel/api/snapshot",
                      auth=("any", "sk-wrong")).status_code == 401


def test_panel_bearer_token_still_works(client, monkeypatch):
    """脚本/curl 的 Bearer 通道不能被 Basic Auth 挤掉。"""
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "tok123")

    r = client.get("/panel", headers={"Authorization": "Bearer tok123"})
    assert r.status_code == 200


def test_panel_alias_routes_available_for_public_entry(client, monkeypatch):
    """入口 nginx 把 /panel/api/ 拦成 404 时，前端回退到 /pq/ 别名。

    回归背景（2026-11-18 16:20）：公网面板输入正确密钥仍提示"密钥无效"，
    实际是 `/panel/api/snapshot` 被 nginx 404 了，前端把 404 当成了鉴权失败。
    """
    from bfl2openai import panel

    monkeypatch.setattr(panel, "_PASS", "sk-bfl")
    monkeypatch.setattr(panel, "_TOKEN", "")

    assert client.get("/pq/snapshot").status_code == 401
    r = client.get("/pq/snapshot", headers={"Authorization": "Bearer sk-bfl"})
    assert r.status_code == 200
    assert "slots" in r.json()
