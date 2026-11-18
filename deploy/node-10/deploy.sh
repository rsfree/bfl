#!/bin/bash
# 部署 node-10：镜像 + 配置一起下发
#
# 为什么要有这个脚本：镜像更新不会带 docker-compose.yml，服务器上那份是
# 独立文件。今天两次踩坑都是这个——侧车拿不到 BFL2OPENAI_POOL_*，报
# "pin mode not configured on sidecar"，全站 502。把下发变成脚本的一步，
# 而不是靠人记得。
#
# 注意：所有远程命令都走 `ssh jump 'ssh host bash -s' <<EOF`，不用
# `ssh host '...'` —— 两层单引号会把 sed 表达式和管道吃掉。
#
# 🔴 2026-11-18：sidecar 改滚动重建。
#   原来第 4 步一条 `docker compose up -d` 把三台一起换，36 个 Chromium
#   页面同时预热，8 核直接打满（实测 load 22.9、CPU pressure 77%、运行
#   队列 39），期间 watchdog 还会把"慢启动"的实例判死再来一轮重启。
#   现在一次只换一台，等 pages_ready 补齐再下一台：同一时刻最多 12 页
#   在预热，峰值从 ~8 核降到 ~2.7 核。
#   三台是静态 IP（172.18.0.10/11/12）+ external 网络，重建不会漂 IP。
#
# 用法: ./deploy.sh <image-tag>            例: ./deploy.sh 0.2.99
#       STRATEGY=all ./deploy.sh <tag>     回到一次性全量重建（紧急修复用）
#       READY_TIMEOUT=600 ./deploy.sh <tag> 单台预热等待上限，默认 480s
set -eu

TAG=${1:?用法: ./deploy.sh <image-tag>}
JUMP=${JUMP:-jump-ops}
HOST=${HOST:-node-10}
DIR=/opt/bfl/deploy/node-10
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STRATEGY=${STRATEGY:-rolling}
READY_TIMEOUT=${READY_TIMEOUT:-480}
PAGES=${PAGES:-9}      # 与 compose 的 BFL2OPENAI_EGRESS_PAGES 一致

remote() { ssh "$JUMP" "ssh $HOST bash -s"; }
run() { remote <<EOF
$*
EOF
}

echo "==> 1/6 备份服务器上的 compose"
run "cd $DIR && cp -a docker-compose.yml docker-compose.yml.bak-\$(date +%H%M%S) && ls -1t docker-compose.yml.bak-* | tail -n +6 | xargs -r rm -f"

echo "==> 2/6 下发仓库里的 compose"
cat "$ROOT/deploy/node-10/docker-compose.yml" | ssh "$JUMP" "ssh $HOST 'cat > $DIR/docker-compose.yml'"

echo "==> 3/6 校验渲染后的配置"
N=$(run "cd $DIR && docker compose config 2>/dev/null | grep -cE 'BFL2OPENAI_POOL|BFL2OPENAI_EGRESS_PAGES' || true" | tail -1)
[ "${N:-0}" -ge 8 ] || { echo "配置渲染异常：只找到 $N 个关键变量"; exit 1; }
echo "    关键变量 $N 个"

echo "==> 4/6 更新镜像 tag 并拉取"
run "cd $DIR && sed -i 's/^BFL_IMAGE_TAG=.*/BFL_IMAGE_TAG=$TAG/' .env && docker compose pull -q 2>&1 | tail -3"

if [ "$STRATEGY" = "rolling" ]; then
  echo "==> 5/6 滚动重建 sidecar（一次一台，等 pages_ready >= ${PAGES}，上限 ${READY_TIMEOUT}s/台）"
  ssh "$JUMP" "ssh $HOST bash -s" <<EOF
set -u
cd $DIR
for pair in bfl-browser-egress:172.18.0.10 bfl-browser-egress-2:172.18.0.11 bfl-browser-egress-3:172.18.0.12; do
  svc=\${pair%%:*}; ip=\${pair##*:}
  if ! docker compose up -d --no-deps "\$svc" >/dev/null 2>&1; then
    echo "    \$svc 重建失败，中止（其余实例保持原版本）"
    exit 1
  fi
  ready=0; waited=0
  while [ "\$waited" -lt $READY_TIMEOUT ]; do
    sleep 5; waited=\$((waited+5))
    ready=\$(curl -s -m 5 "http://\$ip:8011/healthz" 2>/dev/null | grep -o '"pages_ready":[0-9]*' | head -1 | cut -d: -f2)
    ready=\${ready:-0}
    [ "\$ready" -ge $PAGES ] 2>/dev/null && break
  done
  if [ "\$ready" -ge $PAGES ] 2>/dev/null; then
    echo "    \$svc pages_ready=\$ready (\${waited}s)"
  else
    echo "    \$svc pages_ready=\$ready 未补齐 (\${waited}s)，继续下一台（退避与自愈会接管）"
  fi
done
EOF
else
  echo "==> 5/6 [STRATEGY=all] 一次性重建全部 sidecar"
  run "cd $DIR && docker compose up -d --no-deps bfl-browser-egress bfl-browser-egress-2 bfl-browser-egress-3 2>&1 | tail -4"
fi

echo "==> 6/6 重建主服务与探针并健康检查"
# bfl-probe 与 app 同镜像（命令不同），漏了它会长期停在旧版本：2026-11-18
# 实测部署完 app 已 0.2.99 而 probe 还是 0.2.98。
run "cd $DIR && docker compose up -d --no-deps bfl2openai bfl-probe 2>&1 | tail -3"
for _ in $(seq 1 40); do
  ok=$(run "docker ps --filter name=bfl2openai --format '{{.Status}}' | grep -c healthy || true" | tail -1)
  [ "${ok:-0}" = "1" ] && break
  sleep 5
done
run "docker ps --filter name=bfl --format '{{.Names}}|{{.Status}}|{{.Image}}'"
run "curl -s -m 10 http://127.0.0.1:39100/panel/api/snapshot | head -c 150"
echo
echo "完成：$TAG"
