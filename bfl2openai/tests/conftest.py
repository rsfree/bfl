"""测试全局隔离。

images 门自 2026-11-09 起带幂等（同参数单飞 + 结果短缓存）。`store` 在未设
`REDIS_URL` 时会回落到**进程内** dict，而 pytest 全程同一个进程 —— 上一例
留下的 `idem_done:*` 会让下一例直接走 replay，绕过被 monkeypatch 掉的实现，
表现为「单跑通过、整跑失败」（实测：test_upload_budget 的 413 用例）。

这里在每个用例前后清空进程内状态，保证用例之间互不可见。
Redis 模式下由各用例自行隔离（CI 不设 REDIS_URL）。
"""
from __future__ import annotations

import pytest

from bfl2openai import store


@pytest.fixture(autouse=True)
def _isolate_inprocess_store():
    for container in (store._MEM, store._ZSETS):
        container.clear()
    yield
    for container in (store._MEM, store._ZSETS):
        container.clear()
