"""大对象落盘：Redis 里不能再出现 b64 图。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import store  # noqa: E402


@pytest.fixture(autouse=True)
def _spill_env(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(store, "REDIS_URL", "")
    monkeypatch.setattr(store, "_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.setattr(store, "_SPILL_THRESHOLD", 1024)
    monkeypatch.setattr(store, "_last_spill_sweep", 0.0)
    store._MEM.clear()
    yield


def test_small_doc_stays_inline():
    import asyncio
    asyncio.run(store.put("k1", {"a": 1}))
    raw = store._MEM["k1"][0]
    assert b"__spill__" not in raw
    assert asyncio.run(store.get("k1")) == {"a": 1}


def test_large_doc_spills_and_roundtrips():
    import asyncio
    big = {"b64_json": "A" * 5000, "revised_prompt": "x"}
    asyncio.run(store.put("k2", big, ttl_s=600))

    raw = store._MEM["k2"][0]
    assert len(raw) < 400, "Redis 里只应剩引用，不能再存 b64"
    assert b"__spill__" in raw
    assert asyncio.run(store.get("k2")) == big


def test_blob_spills_and_roundtrips():
    import asyncio
    data = os.urandom(4000)
    asyncio.run(store.put_blob("b1", data, ttl_s=600))
    raw = store._MEM["b1"][0]
    assert len(raw) < 400
    assert asyncio.run(store.get_blob("b1")) == data


def test_missing_spill_file_degrades_to_miss():
    """文件被清掉不该抛错，只当缓存未命中（下次重跑即可）。"""
    import asyncio
    asyncio.run(store.put("k3", {"b64_json": "B" * 5000}, ttl_s=600))
    ref = store._MEM["k3"][0]
    path = store._deref.__globals__  # noqa: F841 - 仅为可读性
    import json
    p = json.loads(ref)["__spill__"]
    os.remove(p)
    assert asyncio.run(store.get("k3")) is None


def test_drop_removes_spill_file():
    import asyncio, json
    asyncio.run(store.put("k4", {"b64_json": "C" * 5000}, ttl_s=600))
    p = json.loads(store._MEM["k4"][0])["__spill__"]
    assert os.path.exists(p)
    asyncio.run(store.drop("k4"))
    assert not os.path.exists(p)


def test_sweep_deletes_expired_files():
    import asyncio, json, time
    asyncio.run(store.put("k5", {"b64_json": "D" * 5000}, ttl_s=1))
    p = json.loads(store._MEM["k5"][0])["__spill__"]
    with open(p + ".exp", "w") as f:
        f.write(str(time.time() - 10))
    store._spill_sweep(force=True)
    assert not os.path.exists(p)
