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
# 用法: ./deploy.sh <image-tag>     例: ./deploy.sh 0.2.95
set -eu

TAG=${1:?用法: ./deploy.sh <image-tag>}
JUMP=${JUMP:-jump-ops}
HOST=${HOST:-node-10}
DIR=/opt/bfl/deploy/node-10
ROOT=$(cd "$(dirname "$0")/../.." && pwd)

remote() { ssh "$JUMP" "ssh $HOST bash -s"; }
run() { remote <<EOF
$*
EOF
}

echo "==> 1/5 备份服务器上的 compose"
run "cd $DIR && cp -a docker-compose.yml docker-compose.yml.bak-\$(date +%H%M%S) && ls -1t docker-compose.yml.bak-* | tail -n +6 | xargs -r rm -f"

echo "==> 2/5 下发仓库里的 compose"
cat "$ROOT/deploy/node-10/docker-compose.yml" | ssh "$JUMP" "ssh $HOST 'cat > $DIR/docker-compose.yml'"

echo "==> 3/5 校验渲染后的配置"
N=$(run "cd $DIR && docker compose config 2>/dev/null | grep -cE 'BFL2OPENAI_POOL|BFL2OPENAI_EGRESS_PAGES' || true" | tail -1)
[ "${N:-0}" -ge 8 ] || { echo "配置渲染异常：只找到 $N 个关键变量"; exit 1; }
echo "    关键变量 $N 个"

echo "==> 4/5 更新镜像 tag 并滚动重启"
run "cd $DIR && sed -i 's/^BFL_IMAGE_TAG=.*/BFL_IMAGE_TAG=$TAG/' .env && docker compose pull -q >/dev/null 2>&1; docker compose up -d 2>&1 | tail -4"

echo "==> 5/5 健康检查"
for _ in $(seq 1 40); do
  ok=$(run "docker ps --filter name=bfl2openai --format '{{.Status}}' | grep -c healthy || true" | tail -1)
  [ "${ok:-0}" = "1" ] && break
  sleep 5
done
run "docker ps --filter name=bfl --format '{{.Names}}|{{.Status}}|{{.Image}}'"
run "curl -s -m 10 http://127.0.0.1:39100/panel/api/snapshot | head -c 150"
echo
echo "完成：$TAG"
