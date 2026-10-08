# bfl2openai · ai-prod 部署

- 机器：210.121.44.245（ai-prod）；目录 `/opt/bfl/`（= 本仓库内容平铺）
- 端口：容器 8000，宿主只绑 `127.0.0.1:39100`（红线：不裸公网）
- 域名：`bfl.1task.cn`（nginx :80 → ACME → :443；apply-https.sh 一键落地）
- 凭证：`/opt/bfl/.env`（TENCENT_API_KEY / MPS_REGION / MPS_OUTPUT_* / BFL2OPENAI_FALLBACK_PROXY），
  宿主 `chown root:10001 && chmod 640`（容器以 uid 10001 只读挂载）

## 首次部署

```sh
# 本机：同步仓库内容（排除 .git / .env——.env 单独传）
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 'mkdir -p /opt/bfl'
rsync -az -e "ssh -i ~/.ssh/id_ed25519_ops" --exclude .git --exclude .env \
  ./ root@210.121.44.245:/opt/bfl/
scp -i ~/.ssh/id_ed25519_ops bfl2openai/.env root@210.121.44.245:/opt/bfl/.env
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 \
  'chown root:10001 /opt/bfl/.env && chmod 640 /opt/bfl/.env'
ssh -i ~/.ssh/id_ed25519_ops root@210.121.44.245 \
  'cd /opt/bfl && docker compose -f deploy/ai-prod/docker-compose.yml up -d --build'
```

之后：投 :80 vhost → 加 DNS A 记录（bfl.1task.cn → 210.121.44.245）→ 跑 apply-https.sh。

## 暴露面

- 服务默认免鉴权（BFL2OPENAI_API_KEY / BFL_NATIVE_API_KEY 未设）；
  nginx 侧有限速闸门（5r/s + burst 60，超限 429）。
- 上游是 BFL 免费演示 + 会消耗你的腾讯云 MPS 费用（4k/8k 复合）——
  不想公网可用就在 .env 设 BFL2OPENAI_API_KEY 并同步给调用方。
