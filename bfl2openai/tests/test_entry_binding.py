"""入口绑定自检：账本里的 slot→IP 必须等于实测出口 IP。

这条检查存在的理由（2026-11-27 实测）：2090/2091 的选路是
`crc32(用户名) % 节点数`，结果同时取决于**前置机**与**节点清单**。
两者任一变化，同一个 slot 的 IP 就变了，而 used/quota 是按 IP 记的：
账面以为旧 IP 还有额度，真实新 IP 却在无人计数的情况下被消耗。
自检只做观测（写 ERROR + 挂 /healthz），不阻断请求。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import server  # noqa: E402


class _Slot:
    def __init__(self, ip: str) -> None:
        self.ip = ip


def _patch(monkeypatch, *, slot_ip, probed):
    async def fake_get_slot(_n: int):
        return _Slot(slot_ip) if slot_ip is not None else None

    async def fake_probe(_n: int, *, timeout: float = 12.0):
        if isinstance(probed, Exception):
            raise probed
        return probed

    monkeypatch.setattr(server.egress_pool, "get_slot", fake_get_slot)
    monkeypatch.setattr(server.egress_pool, "probe_slot_ip", fake_probe)


def test_entry_binding_ok(monkeypatch):
    _patch(monkeypatch, slot_ip="154.44.25.169", probed="154.44.25.169")
    out = asyncio.run(server._verify_entry_binding())
    assert out["state"] == "ok"
    assert out["actual"] == out["expected"]


def test_entry_binding_mismatch(monkeypatch):
    """同名账号换前置机 ⇒ IP 不同，必须报 mismatch 并带上两边的值。"""
    _patch(monkeypatch, slot_ip="154.44.25.43", probed="154.44.25.169")
    out = asyncio.run(server._verify_entry_binding())
    assert out["state"] == "mismatch"
    assert (out["expected"], out["actual"]) == ("154.44.25.43", "154.44.25.169")


def test_entry_binding_no_table(monkeypatch):
    """槽位表还没建（新装/扫描未完成）时不能报 ok，也不能抛。"""
    _patch(monkeypatch, slot_ip=None, probed="1.2.3.4")
    assert asyncio.run(server._verify_entry_binding())["state"] == "no_table"


def test_entry_binding_probe_failed(monkeypatch):
    """探测失败≠绑定错误：分开报，避免把网络抖动当成换池。"""
    _patch(monkeypatch, slot_ip="1.2.3.4", probed=None)
    out = asyncio.run(server._verify_entry_binding())
    assert out["state"] == "probe_failed"
    assert out["expected"] == "1.2.3.4"


def test_entry_binding_error_is_contained(monkeypatch):
    """自检自身抛异常也只能变成状态字符串，绝不能冒到 lifespan。"""
    _patch(monkeypatch, slot_ip="1.2.3.4", probed=RuntimeError("boom"))
    assert asyncio.run(
        server._verify_entry_binding())["state"] == "error:RuntimeError"


def test_healthz_exposes_entry_binding():
    """运维只看 /healthz；字段缺了等于没做。"""
    server._ENTRY_BINDING.clear()
    server._ENTRY_BINDING.update(state="mismatch", expected="a", actual="b")
    out = asyncio.run(server.healthz())
    assert out["entry_binding"]["state"] == "mismatch"
