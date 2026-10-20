# bfl2openai · ai-prod 部署

- 机器：210.121.44.245（ai-prod）；目录 `/opt/bfl/`（= 本仓库内容平铺）
- 端口：容器 8000，宿主只绑 `127.0.0.1:39100`（红线：不裸公网）
- 域名：`bfl.1task.cn`（nginx :80 → ACME → :443；apply-https.sh 一键落地）
- 凭证：**实际生效的是 `/opt/bfl/deploy/ai-prod/.env`** —— compose 里 `env_file: ./.env`
  的相对路径按 **compose 文件所在目录** 解析，不是仓库根。`/opt/bfl/.env` 是早期按旧文档
  上传的副本，当前**不生效**（2026-10-20 核实：两文件并存，只认 `deploy/ai-prod/.env`，
  容器内 `BFL2OPENAI_API_KEY` 取自后者）。改凭证只改这一个文件，然后 `up -d` 重建容器。
  宿主 `chown root:10001 && chmod 640`。

## 首次部署

```sh
# 本机：同步仓库内容（排除 .git / .env——.env 单独传）
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 'mkdir -p /opt/bfl'
rsync -az -e "ssh -i ~/.ssh/id_ed25519_ops" --exclude .git --exclude .env \
  ./ root@210.121.44.245:/opt/bfl/
# .env 必须落在 deploy/ai-prod/ 下 —— 那是 compose 的 env_file 解析位置
scp -i ~/.ssh/id_ed25519_ops bfl2openai/.env root@210.121.44.245:/opt/bfl/deploy/ai-prod/.env
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 \
  'chown root:10001 /opt/bfl/deploy/ai-prod/.env && chmod 640 /opt/bfl/deploy/ai-prod/.env'
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 \
  'cd /opt/bfl && docker compose -f deploy/ai-prod/docker-compose.yml pull \
   && docker compose -f deploy/ai-prod/docker-compose.yml up -d'
```

之后：投 :80 vhost → 加 DNS A 记录（bfl.1task.cn → 210.121.44.245）→ 跑 apply-https.sh。

## 发布与回滚（2026-10-20 起：不在服务器 build）

```sh
git push origin main          # release.yml：自增版本 → 构建并推 ghcr.io/rsfree/bfl:{版本,latest,sha-<sha>}
gh run watch --exit-status    # 等价于看 CI 绿

ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 'bash -s' <<'EOS'
cd /opt/bfl
sed -i '/^BFL_IMAGE_TAG=/d' deploy/ai-prod/.env
echo 'BFL_IMAGE_TAG=<版本>' >> deploy/ai-prod/.env      # 回滚＝把这里改回旧 tag
docker compose -f deploy/ai-prod/docker-compose.yml pull bfl2openai
docker compose -f deploy/ai-prod/docker-compose.yml up -d bfl2openai
EOS
```

GHCR tag **不带 `v` 前缀**（`git tag v0.2.34` → 镜像 `0.2.34`），与 jimeng/qwen 等既有镜像一致。

镜像包 `ghcr.io/rsfree/bfl` 已设为 **public**（2026-10-20 核实：匿名 `docker manifest inspect`
直接返回 manifest、匿名 `docker pull` 成功下载），所以部署本服务**不需要** `docker login`。
🔴 但同一台机器上 `ghcr.io/rsfree/metaso` 仍是 private，**不要**执行 `docker logout ghcr.io`，
否则 metaso 一旦重建就拉不下来。包归属是 **组织 rsfree**，改可见性只有 org owner 能操作
（`https://github.com/orgs/rsfree/packages/container/bfl/settings`）；改仓库可见性不会联动改包。

## 并发与 gunicorn

镜像 CMD 已是 gunicorn（uvicorn worker），worker 数由 `GUNICORN_WORKERS` 控制（默认 1）：

- **1 worker 也扛高并发**：服务是纯 I/O 型 asyncio——每个生成请求就是挂起 30s 等上游，
  单 worker 可同时挂几百个在途请求；瓶颈在上游匿名配额，不在进程。
- **调 worker 数**：compose 里加 `environment: [GUNICORN_WORKERS=2]`（或 .env）后重建。
  ⚠️ N>1 时 native/async/uploads 三门的进程内状态（任务表/上传句柄/复合态）按 worker
  分片：同一调用方的"受理→轮询"必须落在同一 worker——前置 nginx `ip_hash` 粘性路由，
  或把状态外置 Redis/SQLite（参考 jimeng 的 SQLite store）。两者都没做就保持 1。
- gunicorn 的价值：进程守护、graceful reload（改代码不掐在途生成，graceful_timeout=90s）。

## 多图生图（2026-10-20 已上线）

`/v1/images/edits` 支持重复字段多图（第 1 张画布 + 其余参考图），两种拼法都认：
`image[]`（OpenAI 官方 curl 写法）与重复 `image`（openai-python SDK 写法）。
改前只认后者，`image[]` 在旧代码上直接 422 `missing body.image`。

上线实测（bfl.1task.cn 公网 HTTPS）：

| 路径 | 结果 |
| --- | --- |
| 双图 + `resolution=2k` + `aspect_ratio=auto` | 200 / 57s / 2048×2048（响应 8.8MB b64） |
| 服务器回环同参数 | 200 / 37s / 2.08MB |
| `image[]` 与 `image` 绑定探测（坏图） | 均 400（说明字段被接住），空 image → 400 |

### nginx 收体超时（同日加固）

`bfl-proxy.inc` 增加 `client_body_timeout 300s`。默认 60s 时，弱网客户端上传多张大图
（每张 3~5MB）一旦出现 >60s 读空档就返回 **408 且请求到不了应用层**（容器日志无记录，
排查时极易误判成"服务端挂了"）。同文件 `client_max_body_size 64m` 不受影响，
6.3MB body 实测 0.2s 收完。

## 暴露面

- 服务默认免鉴权（BFL2OPENAI_API_KEY / BFL_NATIVE_API_KEY 未设）；
  nginx 侧有限速闸门（5r/s + burst 60，超限 429）。
- 上游是 BFL 免费演示 + 会消耗你的腾讯云 MPS 费用（4k/8k 复合）——
  不想公网可用就在 .env 设 BFL2OPENAI_API_KEY 并同步给调用方。
