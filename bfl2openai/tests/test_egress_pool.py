"""出口槽注册表：计数键必须是 IP、碰撞必须在建映射时消除、429 必须换槽。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import egress_pool as ep  # noqa: E402


@pytest.fixture(autouse=True)
def _pool_env(monkeypatch):
    monkeypatch.setenv("BFL2OPENAI_POOL_HOST", "pool.example.cn")
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "p@ss:word/1")
    monkeypatch.setenv("BFL2OPENAI_POOL_PORT", "2090")
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")
    monkeypatch.setenv("BFL2OPENAI_POOL_QUOTA", "30")
    monkeypatch.delenv("REDIS_URL", raising=False)
    ep.reset_backend()
    yield
    ep.reset_backend()


def test_configured_requires_all_fields(monkeypatch):
    assert ep.configured() is True
    monkeypatch.setenv("BFL2OPENAI_POOL_PASS", "")
    assert ep.configured() is False


def test_slot_username_is_zero_padded():
    assert ep.slot_username(7) == "poolclient-007"
    assert ep.slot_username(123) == "poolclient-123"


def test_proxy_url_encodes_credentials():
    url = ep.slot_proxy_url(3)
    assert url.startswith("socks5h://poolclient-003:")
    # 密码里的 @ : / 必须转义，否则会被当成 host 分隔符
    assert "p%40ss%3Aword%2F1@" in url
    assert url.endswith("pool.example.cn:2090")


def test_scan_marks_collision_as_dup(monkeypatch):
    """同 IP 的第二个槽位必须标 dup —— 否则两个槽位各记 30，把 IP 打成 60。"""
    ips = {1: "1.1.1.1", 2: "2.2.2.2", 3: "1.1.1.1", 4: None}

    async def fake_probe(slot, timeout=12.0):
        return ips[slot]

    monkeypatch.setattr(ep, "_probe_ip", fake_probe)
    import asyncio
    stat = asyncio.run(ep.scan())

    assert stat == {"slots": 4, "distinct_ips": 2, "dups": 1, "errors": 1}
    rows = {s.slot: s for s in asyncio.run(ep.list_slots(scope="all"))}
    assert rows[1].state == ep.STATE_PRIMARY
    assert rows[2].state == ep.STATE_PRIMARY
    assert rows[3].state == ep.STATE_DUP
    assert rows[3].ip == "1.1.1.1"
    assert rows[4].state == ep.STATE_ERROR


def test_scan_does_not_reset_used(monkeypatch):
    """重扫不能把当天已消耗的额度抹掉，否则会重复领取配额。"""
    import asyncio
    monkeypatch.setattr(ep, "_probe_ip",
                        lambda slot, timeout=12.0: _ret(f"9.9.9.{slot}"))
    asyncio.run(ep.scan([1]))
    asyncio.run(ep.note_success(1))
    asyncio.run(ep.note_success(1))
    asyncio.run(ep.scan([1]))
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].used == 2


async def _ret(v):
    return v


def test_acquire_skips_dup_and_error(monkeypatch):
    import asyncio
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1, 2],
                                                          "3.3.3.3": [3]}))
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "3")
    asyncio.run(ep.scan())
    picked = asyncio.run(ep.acquire())
    assert picked is not None
    # 槽位 2 是 dup、不参与调度
    for _ in range(10):
        assert asyncio.run(ep.acquire()).slot in (1, 3)


def _probe_by_slot(mapping):
    """按「IP -> 槽位列表」反查探测结果。"""
    rev = {n: ip for ip, slots in mapping.items() for n in slots}

    async def _p(slot, timeout=12.0):
        return rev.get(slot)
    return _p


def test_acquire_round_robins(monkeypatch):
    """严格轮询：最久没用的先上，不允许连续两次压同一个 IP。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1],
                                                          "2.2.2.2": [2]}))
    asyncio.run(ep.scan())

    first = asyncio.run(ep.acquire())
    asyncio.run(ep.note_success(first.slot))
    second = asyncio.run(ep.acquire())
    assert second.slot != first.slot, "连续两次不得落在同一出口"

    asyncio.run(ep.note_success(second.slot))
    third = asyncio.run(ep.acquire())
    assert third.slot == first.slot, "用完一轮后应回到最久未用的那个"


def test_acquire_alternates_subnets(monkeypatch):
    """相邻请求不得落在同一个 /24：池子 42% 的 IP 挤在单段，这一点必须分开。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({
        "1.1.1.10": [1], "1.1.1.11": [2],
        "2.2.2.20": [3], "2.2.2.21": [4]}))
    asyncio.run(ep.scan())

    segs = []
    for _ in range(8):
        s = asyncio.run(ep.acquire())
        segs.append(ep._prefix(s.ip))
        asyncio.run(ep.note_success(s.slot))
    for a, b in zip(segs, segs[1:]):
        assert a != b, f"相邻两次落在同一网段：{segs}"
    assert len(set(segs)) == 2, "四个槽位两个段，应严格交替"


def test_acquire_does_not_bias_by_prefix_name(monkeypatch):
    """时间戳相同时不得按段名排序 —— 否则会一直从字母序最小的段开打。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "4")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({
        "1.1.1.1": [1], "1.1.1.2": [2],
        "9.9.9.1": [3], "9.9.9.2": [4]}))
    asyncio.run(ep.scan())
    picks = {asyncio.run(ep.acquire()).seg for _ in range(40)}
    assert picks == {"1.1.1", "9.9.9"}, "两个段都必须被抽到，不能只打 1.1.1"


def test_hset_always_uses_mapping_kwarg():
    """redis-py 的 hset 第二个位置参数是 `key`，传 dict 会抛 DataError。

    这条是生产事故的回归锁：内存后端接受 `hset(k, {...})`，单测全绿，
    真实 Redis 上却直接炸掉整个扫描。这类"只在真后端暴露"的形状错误，
    用源码断言守住比再造一个假 Redis 更直接。
    """
    import re
    from pathlib import Path
    src = Path(ep.__file__).read_text()
    bad = re.findall(r"\.hset\([^)=]*?,\s*\{", src)
    assert not bad, f"hset 必须用 mapping= 关键字：{bad}"


def test_reaching_quota_puts_slot_in_rest(monkeypatch):
    """配额用尽 = 当日冷却：该槽位退出调度，面板标"额度用尽"。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setenv("BFL2OPENAI_POOL_QUOTA", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1],
                                                          "2.2.2.2": [2]}))
    asyncio.run(ep.scan())
    asyncio.run(ep.note_success(1))
    asyncio.run(ep.note_success(1))
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert not rows[1].available(), "额度用尽后不得再被调度"
    snap = asyncio.run(ep.snapshot())
    ex = {r["slot"]: r["exhausted"] for r in snap["rows"]}
    assert ex[1] is True and ex[2] is False
    assert asyncio.run(ep.acquire()).slot == 2


def test_soft_quota_stops_before_hard_limit(monkeypatch):
    """软上限优先：到 20 就换 IP，不许打满硬额度 30（打满本身就是风控特征）。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    monkeypatch.setenv("BFL2OPENAI_POOL_QUOTA", "30")
    monkeypatch.setenv("BFL2OPENAI_POOL_SOFT_QUOTA", "3")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1]}))
    asyncio.run(ep.scan())

    for _ in range(3):
        assert asyncio.run(ep.acquire()) is not None
        asyncio.run(ep.note_success(1))
    assert asyncio.run(ep.acquire()) is None, "软额度用尽即停，不能继续吃硬额度"

    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].used == 3
    assert rows[1].remaining == 27, "硬额度还留着余量"


def test_stretch_allows_using_hard_quota(monkeypatch):
    """显式开启 stretch 才允许吃到硬额度上限。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    monkeypatch.setenv("BFL2OPENAI_POOL_QUOTA", "30")
    monkeypatch.setenv("BFL2OPENAI_POOL_SOFT_QUOTA", "3")
    monkeypatch.setenv("BFL2OPENAI_POOL_STRETCH", "1")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1]}))
    asyncio.run(ep.scan())
    for _ in range(3):
        asyncio.run(ep.note_success(1))
    assert asyncio.run(ep.acquire()) is not None


def test_quota_exhaustion_takes_slot_out(monkeypatch):
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    monkeypatch.setenv("BFL2OPENAI_POOL_QUOTA", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1]}))
    asyncio.run(ep.scan())
    assert asyncio.run(ep.acquire()).slot == 1
    asyncio.run(ep.note_success(1))
    assert asyncio.run(ep.acquire()).slot == 1
    asyncio.run(ep.note_success(1))
    assert asyncio.run(ep.acquire()) is None


def test_429_burns_slot_and_cools(monkeypatch):
    """429 之后该槽位当天不再可用，且不在 exclude 里也能被正确跳过。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1],
                                                          "2.2.2.2": [2]}))
    asyncio.run(ep.scan())
    until = asyncio.run(ep.note_rate_limited(1, cooldown_s=600))
    assert until > 0
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].used == rows[1].quota, "429 应直接置为额度耗尽"
    assert not rows[1].available()
    # 剩余唯一可用的是 2
    assert asyncio.run(ep.acquire()).slot == 2


def test_failure_does_not_consume_quota(monkeypatch):
    """没出图 = 没消耗上游配额，不能扣额度。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1]}))
    asyncio.run(ep.scan())
    asyncio.run(ep.note_failure(1))
    asyncio.run(ep.note_failure(1))
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].used == 0, "失败不该扣额度"
    assert rows[1].fail == 2
    assert rows[1].available()


def test_snapshot_shape(monkeypatch):
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1, 2]}))
    asyncio.run(ep.scan())
    snap = asyncio.run(ep.snapshot())
    assert snap["configured"] is True
    assert snap["slots"] == 1, "索引里只保留唯一 IP 的槽位"
    assert snap["usable"] == 1
    assert snap["dups"] == 1
    assert snap["quota_per_ip"] == 30
    assert snap["soft_quota_per_ip"] == 30, "榨干模式：停手点等于额度"
    assert snap["capacity_left"] == 30, "容量按停手点算"
    assert snap["rows"][0]["state"] == ep.STATE_PRIMARY


def test_set_quota_is_clamped_to_upstream_max(monkeypatch):
    """配上限定死 30：配 50 不会多出图，只会换来 429。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "1")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1]}))
    asyncio.run(ep.scan())
    asyncio.run(ep.set_quota(1, 50))
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].quota == 30, "硬额度不得超过上游上限"

    asyncio.run(ep.set_quota(1, 12))
    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].quota == 12, "调小应生效"
    assert rows[1].remaining == 12


def test_403_does_not_inflate_usage(monkeypatch):
    """403 是"被封"不是"出图"，不能把 used 记成满额 —— 否则今日用量虚高。"""
    import asyncio
    monkeypatch.setenv("BFL2OPENAI_POOL_SLOTS", "2")
    monkeypatch.setattr(ep, "_probe_ip", _probe_by_slot({"1.1.1.1": [1],
                                                          "2.2.2.2": [2]}))
    asyncio.run(ep.scan())
    asyncio.run(ep.note_success(1))
    asyncio.run(ep.note_rate_limited(1, field="n403"))

    rows = {s.slot: s for s in asyncio.run(ep.list_slots())}
    assert rows[1].used == 1, "403 不该扣额度"
    assert rows[1].n403 == 1
    assert not rows[1].available(), "被封后必须退出调度"
    snap = asyncio.run(ep.snapshot())
    assert snap["used_today"] == 1, "今日用量只算真实出图"
