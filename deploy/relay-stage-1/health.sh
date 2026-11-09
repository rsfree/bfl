#!/bin/sh
# bfl 中转健康检查（stage-1）
#
# 背景：bfl.1task.cn 的 DNS 已指向本机，本机 nginx 反代到源站 node-10。
#   这条链路是唯一的公网入口，坏了整个服务就不可用，因此需要主动探测。
#
# 每 2 分钟：
#   1) 走本机 443（带 SNI、跳过证书域校验无关，纯探活）取 /healthz
#   2) 连续 2 次失败 → nginx -s reload（配置热加载通常能救回上游连接）
#   3) 连续 4 次失败 → 记 FAIL 状态，不再自动重启（避免重启风暴）
# 状态写入 /var/log/bfl-relay-health.log，可用 tail 直接查。

set -u
LOG=/var/log/bfl-relay-health.log
STATE=/var/lib/bfl-relay-failcount
URL="https://bfl.1task.cn/healthz"
RESOLVE="bfl.1task.cn:443:127.0.0.1"

fail=0
[ -f "$STATE" ] && fail=$(cat "$STATE" 2>/dev/null || echo 0)

code=$(curl -s -o /dev/null -w '%{http_code}' -m 12 \
       --resolve "$RESOLVE" "$URL" 2>/dev/null || echo 000)

ts=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$code" = "200" ]; then
    echo "$ts OK code=$code fail_streak=0" >> "$LOG"
    echo 0 > "$STATE"
    exit 0
fi

fail=$((fail + 1))
echo $fail > "$STATE"
echo "$ts FAIL code=$code fail_streak=$fail" >> "$LOG"

if [ "$fail" -ge 2 ] && [ "$fail" -le 3 ]; then
    /usr/bin/nginx -t >/dev/null 2>&1 && /usr/bin/nginx -s reload
    echo "$ts ACTION reloaded nginx (fail_streak=$fail)" >> "$LOG"
fi

# 日志超过 2000 行就截断，避免无限增长
lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
if [ "$lines" -gt 2000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit 1
