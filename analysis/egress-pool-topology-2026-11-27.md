# 出口池拓扑与端口分配优化（2026-11-27 实测）

> 数据采集时间：2026-11-27 17:55–18:15 CST，宿主机 core-2（172.93.108.217）＋ 两台前置机。
> 结论全部基于实测，不是推断；推断处已标注。

---

## 0. 结论（先看这段）

> **落地状态（18:50 CST）**
> - **P0-1 已执行**：生产 `.env` 的 `BFL2OPENAI_POOL_HOST` 由 `pool.livetest.cn`
>   改为 `172.96.142.53`；三台 egress 滚动重建 **65s**（各 20s 预热，11/11/10 就绪），
>   app 与 probe 同步重建，`probe.last_ok=true`（15.8s）。
> - **P0-2 代码已入库**：`bfl2openai/server.py` 增加入口绑定自检（每 30min，
>   结果挂 `/healthz.entry_binding`），随下一个镜像 tag 生效。
> - 未做：`roles/gost_front` 节点清单确定性排序（双前提高可用的前提）。

1. **「到 2090 的连接数」不是需要优化的指标。** 32 页 ≈ 400 条稳态连接、峰值 1,186 条，
   折合前端 fd ≈ 2.4k；实测 gost-front 进程 fd 上限 **524,288**（当前仅 894）。
   连接数离天花板差两个数量级，砍连接不会换来任何配额或成功率收益。

2. **真正的脆弱点是「入口 = 键映射的一部分」，而这一点目前没有被当成契约管理。**
   同一账号 `poolclient-441`：
   - 经 `172.96.142.53:2090`（chatfire）→ **154.44.25.169**
   - 经 `137.175.102.252:2090`（exit-1）→ **154.44.25.43**

   两台前置机的出口集合与顺序**都不同**（各 588 个，排序后 md5 不同）。
   因此 **DNS 漂移 = 全池 588 个槽位静默换 IP**，账面 `used/quota` 立即失真。
   今天 12:23 那张槽位表就是这么作废的（16:00 已重扫，现与 chatfire 对齐）。

3. **四个端口的分工没有配错**，bfl / duck / image-adapter-v2 的现状都符合各自语义；
   `image-adapter-v2 = 0` 是预期行为（临时验证实例，只绑 `127.0.0.1:39002`），不是故障。

4. 唯一值得改的代码点是 `recycle_entry` 的 **2088 过渡态**，但代价只有约 3% 容量，
   **不建议动**（详见 §5.3）。

---

## 1. 实测基线

### 1.1 各容器到池端口的连接（17:58 快照）

| 容器 | :2090 | :2088 | 全部 established | 页数 | 备注 |
|---|---:|---:|---:|---:|---|
| `bfl-browser-egress` | 67 | 27 | 296 | 11 | |
| `bfl-browser-egress-2` | 107 | 11 | 351 | 11 | |
| `bfl-browser-egress-3` | 89 | 19 | 332 | 10 | 用户峰值快照 433 条 |
| `duck-a` | 0 | 0 | 0 | 128 slot | 走 2091，快照瞬间空闲（此前 38 条） |
| `image-adapter-v2` | 0 | 0 | 3 | — | 无代理 env，见 §4 |

- 单页稳态 ≈ **8–13 条**，峰值 ≈ **35–40 条**（用户 433 条 / 10 页）。
- 一条 2090 连接 = 一次 SOCKS5 认证 + 一次 `crc32(用户名) % 588` 选路；**占配额的是请求，不是连接**。

### 1.2 前端容量

```
gost-front pid 25498  fds=894   LimitNOFILE=524288   出口 588 个
```

- fd 预算 ≈ `2 × 并发连接 + 监听 + 日志`。
- 32 页 × 40 条（悲观峰值）= 1,280 条 → ~2.6k fd，占上限 **0.5%**。
- **结论：扩页不受连接数约束，受「可用 IP × 30 张/日」约束（410 × 30 ≈ 12,300/日）。**

### 1.3 当前槽位表（redis `egress:v2:*`）

| 字段 | 值 |
|---|---|
| `scan_finished` | 1795769978 ≈ 16:00 CST |
| `distinct_ips` | 588 |
| `errors` | 3（12:23 那次是 12,000） |
| `dups` | 11,409（12,000 个用户名落在 588 个 IP 上，符合预期） |
| `index` | 588 个主槽位 |
| `slot:001` | ip=38.6.213.100，used=30/30 |
| `slot:030` | ip=38.6.213.248，used=30/30 |
| `slot:441` | ip=154.44.25.169，n429d=3，n403=2，booked=1 |

- 表内 IP 与 **chatfire 的实际返回逐条一致**（441/001/030 三例核对），当前是自洽的。
- `used=30/30` 是今日 429 的直接来源（UTC 08:00 重置）。

---

## 2. 四个端口的真实语义（从 gost 配置读出来的）

| 端口 | 协议 | 选路键 | 出口决定 | 可记账 | 适用 | 禁用 |
|---|---|---|---|---|---|---|
| `2086` | HTTP | 无 | 每连接轮换（复用 `chain-round`，与 2088 同语义） | ❌ | 不支持 SOCKS 的一次性抓取 | 长事务 |
| `2088` | SOCKS5 | 无 | 每连接轮换（`strategy: round, maxFails: 1`） | ❌ | 无会话、可重试的单发请求 | 提交→轮询类事务 |
| `2089` | SOCKS5 | 目标域名 | `hash(host) % N`，同站点恒定 | ❌ | 站点级会话连续、无需精确记账 | 需要按 IP 计配额 |
| `2090` | SOCKS5 | 用户名 | `crc32(用户名) % 588`，**`maxFails: 1000000` 冻结节点集** | ✅ | 长事务 + 按 IP 记账 | 一次性抓取（会堆在单一 IP） |
| `2091` | HTTP | 用户名（clientID） | 与 2090 **同一 auther、同一 chain ⇒ 同一映射** | ✅ | 只支持 HTTP 代理的客户端 | 同上 |

关键实现细节（`/etc/gost/front.yaml`）：

```yaml
- name: entry-keyed
  addr: "172.96.142.53:2090"
  handler: { type: socks5, auther: keyed }
  chain: chain-keyed           # selector: { strategy: hash, maxFails: 1000000, failTimeout: 90s }
```

- `maxFails: 1000000` 是刻意的：**只要节点清单不变，`% 588` 的分母就恒定，slot→IP 才是稳定的**。
  代价是死节点永远不会被摘出环（这也是「410/588 可用」的结构性来源）。
- `2091` 忽略 `metadata.hash`，走 auth 返回的 clientID ⇒ **与 2090 出口完全相同**。
  即：同名账号在 2090 与 2091 上是同一个 IP、同一份上游额度。

---

## 3. 现有消费者的裁决

| 消费者 | 端口 | 判定 | 依据 |
|---|---|---|---|
| `bfl-browser-egress` ×3 | 2090 | ✅ 正确 | 长事务（提交→轮询→取图跨分钟），必须钉 IP；且要按 IP 落账。只有 2090/2091 能记账 |
| `duck-a` | 2091 | ✅ 正确 | HTTP 客户端；key = `poolclient-{instance}-s{slot}-g{gen}`，**命名空间隔离做对了**，不会撞 bfl 的 `poolclient-NNN`；`DUCK_PROXY_PREFLIGHT=1` 已有启动预检 |
| `image-adapter-v2` | — | ✅ 0 属预期 | `analysis` 里的临时验证实例，只绑 `127.0.0.1:39002`，无 proxy env，本来就不该有出口连接 |
| `bfl` 的 2088 残留 | 2088 | ⚠️ 过渡态 | 见 §5.3 |

> 注意：duck 与 bfl 共用同一个 588 环，`crc32` 撞到同一出口的概率 = `1-(1-1/588)^128 ≈ 19.6%`/槽。
> 但两者上游不同（DDG ≠ flux），**不共享上游额度**，只共享 IP 信誉。无需处理。

---

## 4. 三个结构性结论

### 4.1 入口是映射契约的一部分（P0）

实测四组对照，**同名账号在两台前置机上出口全不同**：

| 用户名 | chatfire `172.96.142.53` | exit-1 `137.175.102.252` |
|---|---|---|
| `poolclient-441` | 154.44.25.169 | 154.44.25.43 |
| `poolclient-001` | 38.6.213.100 | 38.6.213.151 |
| `poolclient-005` | 38.6.213.45 | 38.6.213.18 |
| `poolclient-030` | 38.6.213.248 | 38.6.213.74 |

两台机器的 `chain-keyed` 各 588 个节点，**集合与顺序都不同**：

```
chatfire  sort md5 = 055663bb3e4f8bf1bbe1fe0ecec516a9   前 3: 142.4.114.81 / 38.6.213.41 / 38.175.195.226
exit-1    sort md5 = 8baf660e39c2e4e9c39341e3b8259850   前 3: 38.33.57.73 / 38.6.213.56 / 64.81.112.85
```

**推论**：`pool.livetest.cn` 的 A 记录一旦切到另一台，588 个槽位**全部静默换 IP**：
- 账面 `used/quota` 记在旧 IP 上，新 IP 的实际消耗无人记账 → 配额提前穿底；
- 已加载页面持有的 CF/Vercel 挑战凭据跨 IP → 403 概率上升；
- 槽位表（`egress:v2:slot:*`）整体失效，必须重扫。

今天 12:23 的「空表」是拒连版本；**今天 16:00 的重扫能对上，只是因为那次扫的是 chatfire**。
两件事是同一个根因的两种表现。

### 4.2 环冻结是双刃剑

- 好：节点清单不变 ⇒ slot→IP 恒定，配额键成立。
- 坏：**任何一次 Ansible 增删出口 = 下标全变 = 全池换 IP**。
  `roles/gost_front` 目前没有「节点变更 ⇒ 重建槽位表 + 清 IP 记账」的联动。

### 4.3 连接数不是瓶颈

见 §1.2。32 页 2.4k fd / 上限 524k。**扩页请先扩 IP，别为了「减少连接」改浏览器参数**——
`--disable-preconnect` 之类最多省 10–15% 连接（≈ 0 收益），却要冒指纹变化的风险。

---

## 5. 建议动作

### 5.1 P0：入口钉死 IP，禁 DNS 漂移（✅ 已执行 18:50）

```diff
- BFL2OPENAI_POOL_HOST=pool.livetest.cn
+ BFL2OPENAI_POOL_HOST=172.96.142.53
```

- 理由：在 4.1 的前提下，DNS 自动切换不是容灾，是**静默数据损坏**。
- `pool.livetest.cn` 保留为文档名，但生产不让它参与选路。
- 配套：启动时校验 `slot:001` 的出口 IP 与表内 `ip` 是否一致，不一致写 ERROR
  并挂 `/healthz.entry_binding`（已实现，见下）。

执行记录：

| 步骤 | 结果 |
|---|---|
| `cp .env .env.bak-pin*` + `sed` | 已备份，值 `172.96.142.53` |
| egress-1 / 2 / 3 滚动重建 | 20s / 20s / 20s，`pages_ready=11/11/10` |
| app + probe 重建、清 `bfl:gate:inflight` | 30s 后 healthy |
| 端到端 | `probe.last_ok=true, elapsed 15.8s` |


### 5.2 P0：两台前置机在「同构」之前不得互为备份

要让 `pool.livetest.cn` 可以安全漂移，`roles/gost_front` 必须满足：
1. 同一份节点清单（同一个事实源）；
2. **确定性排序**（按地址排序后渲染），保证 `% N` 的下标一致。

在此之前：`137.175.102.252` 视为**另一套池**，只能整池切换 + 全量重扫，不能分流、不能热备。

### 5.3 P2：2088 过渡态（建议不改）

来源已定位：`browser_egress/main.py:855` `recycle_entry()` 重建页面时
`_new_page(browser, idx, version, gen+1)` **不传 slot** ⇒ 新页先落在浏览器级代理（1080 → 2088），
等下一次请求 `_rebind` 才换到目标槽位。

- 代价：每次回收多一次页面加载（约 2–3s CPU）＋ 一段不落账的出口；约 **3% 容量**。
- 「修」的代价：`slot=entry["slot"]` 会固定 IP，与「每 25 单换 IP」的设计冲突；
  正解是回收时向 registry 租新槽（要 app 侧接口），改动面大于收益。
- **判断：留作已知代价，写进注释即可。**

### 5.4 P1：节点集变更的运维联动

`roles/gost_front` 的 nodes 一旦变化：
1. 重启 `gost-front`（`systemctl restart`，自动重载会 rebind 失败留空监听）；
2. 强制 `POST /panel/api/scan` 重扫；
3. `DEL egress:v2:slot:*` 清账（或按新映射重建）；
4. 重启 app + 三台 egress（页面全部 rebind）。

### 5.5 P1：观测口径

连接数按「客户端 IP → 前端 :2090」统计，一条命令即可，比逐容器 `ss` 稳：

```bash
ss -tn state established '( dport = :2090 )' | awk 'NR>1{print $5}' \
  | cut -d: -f1 | sort | uniq -c | sort -rn
```

---

## 6. 复核命令（可直接复制）

```bash
# 1) 容器内实际走哪台前置机
nsenter -t $(docker inspect -f '{{.State.Pid}}' bfl-browser-egress) -m -n -- \
  getent hosts pool.livetest.cn

# 2) 每容器到各池端口的连接
for c in bfl-browser-egress bfl-browser-egress-2 bfl-browser-egress-3; do
  pid=$(docker inspect -f '{{.State.Pid}}' $c)
  echo -n "$c "; nsenter -t $pid -n ss -tn state established | grep -c ':2090'
done

# 3) 前端 fd 与上限（注意别用 pgrep -f，会匹配到 ssh 命令自身）
pid=$(pgrep -x gost); ls /proc/$pid/fd | wc -l; grep 'open files' /proc/$pid/limits

# 4) 同名账号在两台上的出口是否一致（一致性校验）
for e in 172.96.142.53:2090 137.175.102.252:2090; do
  curl -s --max-time 12 --socks5-hostname "poolclient-441:$PASS@$e" https://api.ipify.org; echo
done

# 5) 槽位表与前端是否对齐
docker exec bfl2openai-redis redis-cli HGET egress:v2:slot:441 ip
```

---

## 7. 一句话总结

> 这台机器上的 433 条连接没什么可优化的；**该被钉死的是「账号 → 前置机 → 出口」这条链的确定性**。
> 前置机是映射的一部分，DNS 不能替你做决定。
