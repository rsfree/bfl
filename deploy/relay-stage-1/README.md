# bfl 公网中转（stage-1 / 103.207.68.201）

## 为什么存在

2026-11-09 实测：源站 `64.178.99.0/24`（Hostwinds 西雅图）到中国大陆
**丢包 20–58%**，TLS 握手 2/3 超时（12–14s 后放弃），同步 `images` 门因此
经常整请求失败。而 stage-1 与源站**同机房（TCP 0.4ms）**，但其 IP 段
`103.207.68.0/24` 回国 **0% 丢包、40ms**。

⇒ 只换入口 IP 段，后端一行不改。

```
客户端 → bfl.1task.cn = 103.207.68.201 (本机) → 64.178.99.49:443 (源站)
                    TLS 终止 + 反代              ↑ 0.4ms / 同机房
```

## 文件

| 文件 | 部署到 |
|---|---|
| `bfl.1task.cn.conf` | `/www/server/panel/vhost/nginx/bfl.1task.cn.conf` |
| `health.sh` | `/opt/bfl-relay/health.sh`（cron `*/2 * * * *`） |

```bash
nginx -t && nginx -s reload
```

## 三个不能动的点

1. **`proxy_ssl_verify_depth 5`**
   源站证书是 4 级链（leaf → YE2 → Root YE → ISRG Root X2 → ISRG Root X1）。
   nginx 默认深度 1，会报 `20:unable to get local issuer certificate` → 502。

2. **80 端口的 `/.well-known/acme-challenge/`**
   DNS 指向本机后，Let's Encrypt 的 HTTP-01 验证会打到本机。
   这里用 `root /www/wwwroot/acme-bfl` + `try_files $uri @acme_origin`：
   本机证书续期命中本地目录，源站续期则回落透传。
   **删掉这段 ⇒ 源站 90 天后续期静默失败。**

3. **`so_keepalive=30s:10s:6`**
   同步门等上游出图时有 15–45s 完全静默，客户端 NAT 会回收映射。

## 证书

两端都走 acme.sh 自动续期，`reloadcmd` 必须指向脚本文件
（直接写 `nginx -t && nginx -s reload` 会被 acme.sh 当参数解析，报 `Unknown parameter: -t`）：

- 本机 `bfl.1task.cn_ecc`，webroot `/www/wwwroot/acme-bfl`
- 源站 `bfl.1task.cn_ecc`，webroot `/www/wwwroot/acme`

续期脚本：`/www/server/panel/vhost/cert/bfl.1task.cn/reload.sh`

## 回滚

```bash
aliyun alidns UpdateDomainRecord \
  --RecordId 2108274679722357760 --RR bfl --Type A \
  --Value 64.178.99.49 --TTL 600
```

TTL 600 秒生效。回滚后中转配置留着不影响任何东西。

## 监控

```bash
tail -f /var/log/bfl-relay-health.log
```

连续 2–3 次失败自动 `nginx -s reload`；≥4 次停手（防重启风暴）。
