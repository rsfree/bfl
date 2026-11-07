#!/bin/bash
# bfl browser-egress 页面池看门狗
#
# 职责：某实例 pages_ready=0（池子空了）时重启它。
#
# 历史：
#   2026-11-05 上线，每 2 分钟检查一次，连续 2 次空才重启。
#   2026-11-06 凌晨 01:00 前后 1.5 小时内重启 8 次 —— 暴露两件事：
#     1) 池子只会缩不会长（回收失败摘页），已由 sidecar 的 _fill_pool_gap 自愈修复；
#     2) 重启没有退避，上游持续不可用时会打重启风暴。
#   现在：同一实例 30 分钟内最多重启 3 次，超了只记日志，把恢复交给池子自愈。
#
# 部署：/opt/bfl/deploy/node-10/egress-watchdog.sh + crontab
#   */2 * * * * /opt/bfl/deploy/node-10/egress-watchdog.sh >/dev/null 2>&1
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
LOG=/var/log/bfl-egress-watchdog.log

for pair in "172.18.0.10:bfl-browser-egress" \
            "172.18.0.11:bfl-browser-egress-2" \
            "172.18.0.12:bfl-browser-egress-3"; do
  ip="${pair%%:*}"
  name="${pair##*:}"
  h=$(curl -sS -m 6 "http://$ip:8011/healthz" 2>/dev/null)
  ready=$(echo "$h" | grep -o '"pages_ready":[0-9]*' | cut -d: -f2)
  key="/tmp/bfl-egress-fail-$name"

  if [ -z "$ready" ] || [ "$ready" -lt 1 ] 2>/dev/null; then
    n=$(( $(cat "$key" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$key"
    if [ "$n" -ge 2 ]; then
      now=$(date +%s)
      wstart=$(cat "/tmp/bfl-egress-wstart-$name" 2>/dev/null || echo 0)
      wcount=$(cat "/tmp/bfl-egress-wcount-$name" 2>/dev/null || echo 0)
      # 30 分钟滑动窗口，窗口外清零
      if [ $((now - wstart)) -gt 1800 ]; then
        wcount=0
        wstart=$now
      fi
      if [ "$wcount" -ge 3 ]; then
        echo "$(date -Is) $name pages_ready=${ready:-none} -> skip (backoff ${wcount}/30min)" >> "$LOG"
      else
        echo "$(date -Is) $name pages_ready=${ready:-none} -> docker restart" >> "$LOG"
        docker restart "$name" >/dev/null 2>&1
        echo $((wcount + 1)) > "/tmp/bfl-egress-wcount-$name"
        echo "$now" > "/tmp/bfl-egress-wstart-$name"
      fi
      rm -f "$key"
    fi
  else
    rm -f "$key"
  fi
done
