# 号池自愈设计（2026-11-22 事故复盘）

## 一、事故是什么

18:00 前后 50 分钟窗口：成功率 33.5%，429 占 50.5%，宿主 load 72（8 核），
CPU pressure `some avg10=97%`，三个 sidecar 吃掉 7.8 核。

拆开看是**三层资源耦合**，任何一层的判断失真都会级联：

| 层 | 资源 | 真实容量 | 控制器 |
|---|---|---|---|
| 额度层 | 595 IP × 30 张/日 | 17850 张/日 | `egress_pool` 账本 |
| 页面层 | 3 sidecar × 9 页 | 27 并发 | sidecar 池 + watchdog |
| CPU 层 | 8 核 | ~7.8 核 | 无（缺口在此） |

根因不是闸门，而是**额度层记账不准 + 入口层没有 CPU 反馈**：

1. `scan()` 的 `keep` 丢字段 → 账本记忆被周期性清洗
2. 规则置账后无复核 → 误杀只增不减
3. `GATE_LIMIT` 是启动常量 → 无法随 CPU 自适应
4. 日额度耗尽后仍换槽重试 → 把 240s 预算烧成慢失败

## 二、五个自愈层

### L1 修 scan 的字段丢失（缺陷修复，最高优先级）

`egress_pool.scan()` 重写 hash 时 `keep` 只保留 5 个字段：

```
day / used / quota / soft_quota / last_used
```

丢失 `n_ok`、`n429`、`n429d`、`booked`、`cool_until`。后果：

- `n429d` 归零 → 「二次确认」退化为「永远第一次」，已判死的 IP 会无限次重新试探
- `n_ok` 归零 → `hidden_capacity` 读数不可信（实测从 2866 掉到 0）

修法（约 5 行）：

```python
keep = {
    "day": cur.get("day", ""),
    "used": cur.get("used", 0),
    "quota": cur.get("quota", c["quota"]),
    "soft_quota": cur.get("soft_quota", c["soft_quota"]),
    "last_used": cur.get("last_used", 0),
    # 账本记忆必须跨扫描存活，否则二次确认失效
    "n_ok": cur.get("n_ok", 0),
    "n429": cur.get("n429", 0),
    "n429d": cur.get("n429d", 0),
    "booked": cur.get("booked", 0),
}
```

`cool_until` 刻意不保留：扫描间隔 12h，旧冷却必然已过期，保留反而是污染。

验收：扫描前后 `sum(n429d)` 不变；`hidden_capacity` 不归零。

### L2 规则置账自动对账（30 分钟周期）

现在 `note_429` 第二次命中日额度文案就把 `used=quota` 写死，**之后永不复核**。
实测今天 566 个 exhausted 槽位里 143 个是规则置账，回滚后捞回 3238 张账面额度。

自愈逻辑：

```
每 30 分钟，或 usable/primary < 30% 时立即触发：
  for slot in 所有 booked=1 且 last_used 在 2 小时内:
      发一次最小尺寸探针（256×256，上游在额度检查后校验，成本最低）
      真 429 日额度 → 保持置死，booked 从 1 改 2（表示已复核，冷却 6h 后再查）
      成功 / 非额度错 → used = n_ok，booked = 0，恢复调度
```

成本控制：单轮最多复核 50 个，避免把剩余额度烧在探针上。
探针优先级：`last_used` 越新越先查（最近还在工作的 IP 最可能是误杀）。

验收：`booked=1` 且 `last_used > 2h` 的槽位数应稳定趋近 0。

### L3 入口闸门自适应（CPU 反馈闭环）

现状 `GATE_LIMIT` 是模块级常量，改一次要重启一次（本次就因此卡住）。
改成从 Redis 读 + AIMD 控制：

```python
# metrics.py
async def _limit() -> int:
    v = await store.get("bfl:gate:limit")
    return int(v) if v else GATE_LIMIT

# 控制循环（每 30s，只在一个 worker 里跑，靠 Redis 抢锁）
pressure = read_cpu_pressure_avg60()
if pressure > 90 or loadavg > os.cpu_count() * 4:
    new = max(8, int(limit * 0.75))          # 乘性减
elif pressure < 60 and success_rate > 0.85:
    new = min(27, limit + 1)                  # 加性增，上限=物理页数
```

要点：

- **上限必须是 27（物理页数）而不是 60（sidecar 的 MAX_INFLIGHT）**。
  60 是排队位，超过 27 的部分只会拉长延迟，不增加吞吐（利特尔法则：饱和时吞吐 = 并发/延迟，二者同向变化时吞吐不变）。
- 下限 8：再低会让正常请求也被拒，反而放大客户端重试。
- 手动覆盖：`POST /panel/api/gate {"limit": N}`，写 Redis 的 `bfl:gate:limit`，优先级高于 AIMD。

验收：load 从 72 回落时，limit 应在 3 个周期内回升；load 冲高时 1 个周期内下降。

### L4 日额度耗尽时快速失败

现在 `_browser_generate` 遇到 429 daily 只 `continue` 换槽，直到 240s 预算耗尽：

```
18:13:31 /v1/images/edits -> 429  208716ms
18:14:23 /v1/images/edits -> 429  211412ms
```

对确定性失败（日额度耗尽）不该耗满预算。修法：

```python
daily_hits = 0
...
if exc.status == 429:
    verdict = await egress_pool.note_429(...)
    if _is_daily_quota(exc):
        daily_hits += 1
        if daily_hits >= 3:          # 连续 3 个 IP 都报日额度耗尽 → 判定池子枯竭
            return openai_error(429, "daily quota exhausted; retry after reset",
                                "rate_limit_error",
                                headers={"Retry-After": str(seconds_until_utc_midnight())})
    continue
```

3 次的依据：`usable/primary` 低于 5% 时换 3 个槽位仍失败的概率 > 95%，
继续换只是把 60s 变成 210s。`Retry-After` 直接给到换日秒数，客户端就不会做无效重试。

验收：额度枯竭期 429 的 p50 耗时从 210s 降到 60s 以内。

### L6 重启必须清空闸门计数（部署安全，2026-11-22 实测踩到）

`bfl:gate:inflight` 是 Redis 里的跨 worker 计数器，**进程被杀时不会 DECR**。
0.2.122 部署后实测：新容器健康启动，但计数器残留在 24（=limit），
TTL 还要 420 秒才过期 —— 这 7 分钟里**每个新请求都会被闸门拒绝**。

修法（二选一）：

```python
# A. 启动时清空（最简单，适合单实例主服务）
@app.on_event("startup")
async def _reset_gate():
    await store.delete(GATE_KEY)   # 容器重启 = 所有旧在途必然已死

# B. 分键（适合多实例/滚动发布）
GATE_KEY = f"bfl:gate:inflight:{os.environ.get('HOSTNAME', 'x')}"
# 读取时对全部键求和，进程退出后自己的键由 TTL 回收
```

A 的风险是滚动发布时两个实例互相清对方的计数；B 更严谨但需要聚合读。
当前是单实例主服务，建议先用 A，并在 `deploy.sh` 的第 6 步加一行
`docker exec redis redis-cli DEL bfl:gate:inflight` 作为兜底。

验收：重启后 `/pq/metrics` 的 `gate.inflight` 应立即为 0，而不是停在 limit。

### L5 页面重建预算（防 CPU 雪崩）

现状：`TargetClosedError` → retire → 重建，没有全局限速。load 72 时重建本身
就是负载源，容易形成「崩→重建→更崩」的正反馈。

自愈：全局信号量限制同时重建的页面数（建议 3，即每个 shard 最多 1 个），
并对同一 shard 连续 3 次重建失败做 half-open 降级（跳过该 shard，只用其余 18 页）。

验收：load > 32 时不出现单 shard 连续重建。

## 三、优先级与成本

| 层 | 改动量 | 风险 | 收益 | 建议顺序 |
|---|---|---|---|---|
| L1 scan 丢字段 | ~5 行 + 1 单测 | 极低 | 修掉账本根因 | 1 |
| L4 快速失败 | ~15 行 | 低 | 429 延迟 210s→60s | 2 |
| L3 闸门自适应 | ~80 行 | 中（需 Redis 锁） | 免重启调参 | 3 |
| L2 自动对账 | ~100 行 + 探针 | 中（消耗额度） | 误杀自愈 | 4 |
| L5 重建预算 | ~40 行 | 低 | 防雪崩 | 5 |

建议 L1 + L4 一起发版（都是低风险、立竿见影），L3 单独一版。

## 四、一个必须承认的边界

自愈能解决「浪费」和「卡死」，**解决不了「额度不足」**。

595 × 30 = 17850 张是硬天花板。今天用量 13801 张（`n_ok` 口径），
已到 77%。剩下 4000 张按当前速率撑不到换日（UTC 明日 00:00 = 北京 08:00）。

要在额度耗尽后仍有服务能力，只有三条路：

1. **扩池**：增加出口 IP 数量（`BFL2OPENAI_POOL_SLOTS` 已是 12000，实际唯一 IP 595
   已经是上限，需要换代理供应商）
2. **多账号**：多个上游账号轮换，绕过单账号 30 张/日的限制
3. **降级服务**：额度低于 10% 时主动返回 503 + 明确的恢复时间，而不是让客户端等 210 秒

自愈系统应该在第 3 条上做决策：`usable/primary < 10%` 时自动进入 degraded 模式
（闸门降到 8、Retry-After 给到换日），把「失败得慢」变成「失败得明白」。
