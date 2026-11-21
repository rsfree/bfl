"""槽位扫描定时化（2026-11-21）：表放三天等于拿失效出口继续打。"""
from __future__ import annotations

from bfl2openai import egress_pool as ep


def test_due_now_when_just_built(monkeypatch):
    monkeypatch.setattr(ep, "SCAN_INTERVAL_S", 43200.0)
    monkeypatch.setattr(ep, "SCAN_STARTUP_DELAY_S", 600.0)
    now = 1_700_000_000.0
    assert ep.scan_due_delay(now, now=now) == 43200.0


def test_due_uses_startup_floor_when_stale(monkeypatch):
    monkeypatch.setattr(ep, "SCAN_INTERVAL_S", 43200.0)
    monkeypatch.setattr(ep, "SCAN_STARTUP_DELAY_S", 600.0)
    now = 1_700_000_000.0
    # 三天前建的表：已过期，但不能在启动瞬间抢 CPU
    assert ep.scan_due_delay(now - 3 * 86400, now=now) == 600.0
    # 从未扫过
    assert ep.scan_due_delay(0, now=now) == 600.0


def test_due_zero_when_disabled(monkeypatch):
    monkeypatch.setattr(ep, "SCAN_INTERVAL_S", 0.0)
    assert ep.scan_due_delay(0, now=1_700_000_000.0) == 0.0
