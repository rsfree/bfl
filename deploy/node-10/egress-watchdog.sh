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
#   之后：同一实例 30 分钟内最多重启 3 次，超了只记日志。
#   2026-11-18：加启动保护 + 状态目录迁出 /tmp。见下。
#
# 🔴 2026-11-18 事故（本次修复针对它）：
#   发版重建后 sidecar 慢启动（12 页都要过上游，遇限流就重试），预热期
#   pages_ready 必然是 0。watchdog 连续 2 次判死 → docker restart →
#   12 页重新预热 → 三台叠加把 8 核打满（实测 load 22.9、CPU pressure 77%、
#   运行队列 39，而 25.9 天平均 CPU 利用率只有 4.4%）。
#   13:32–13:34 只有 1 台可用，业务侧刷出 "egress pool empty" 503。
#   两处修复：
#     1) 容器 uptime < MIN_UPTIME 时只记日志、不重启 —— 预热中的容器不该被判死，
#        这是整条反馈环里唯一没有保护的双边判断；
#     2) 退避/失败计数从 /tmp 挪到 /var/lib/bfl —— /tmp 被清理就丢计数，
#        重启风暴会重新计数再打一轮。
#
# 部署：/opt/bfl/deploy/node-10/egress-watchdog.sh + crontab
#   */2 * * * * /opt/bfl/deploy/node-10/egress-watchdog.sh >/dev/null 2>&1
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
LOG=/var/log/bfl-egress-watchdog.log
STATE_DIR=/var/lib/bfl/egress-watchdog
MIN_UPTIME=${MIN_UPTIME:-600}          # 秒。实测 12 页冷启动 3–5 分钟，留一倍余量
BACKOFF_WINDOW_S=${BACKOFF_WINDOW_S:-1800}
BACKOFF_MAX=${BACKOFF_MAX:-3}

mkdir -p "$STATE_DIR" 2>/dev/null

# 日志封顶 1MB，只留尾部 2000 行（风暴期每 2 分钟最多 3 行，正常一年也到不了）
if [ -f "$LOG" ]; then
  sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
  if [ "${sz:-0}" -gt 1048576 ]; then
    tail -2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
  fi
fi

for pair in "172.18.0.10:bfl-browser-egress" \
            "172.18.0.11:bfl-browser-egress-2" \
            "172.18.0.12:bfl-browser-egress-3"; do
  ip="${pair%%:*}"
  name="${pair##*:}"
  key="$STATE_DIR/fail-$name"
  wstart_f="$STATE_DIR/wstart-$name"
  wcount_f="$STATE_DIR/wcount-$name"

  h=$(curl -sS -m 6 "http://$ip:8011/healthz" 2>/dev/null)
  ready=$(echo "$h" | grep -o '"pages_ready":[0-9]*' | cut -d: -f2)

  # ── 启动保护 ──────────────────────────────────────────────────────
  # 🔴 不要用 {{.State.StartedAt.Unix}}：本机 docker 的 StartedAt 在模板里是
  # interface{}，取 .Unix 报 "can't evaluate field Unix"，实测 rc=1。
  # 拿字符串再截掉纳秒（2026-11-18T05:14:43.683501222Z → 2026-11-18T05:14:43），
  # 按 UTC 解析成 epoch。
  started_raw=$(docker inspect -f '{{.State.StartedAt}}' "$name" 2>/dev/null)
  started_ts=$(date -u -d "${started_raw%%.*}" +%s 2>/dev/null || echo 0)
  if [ -n "$started_ts" ] && [ "${started_ts:-0}" -gt 0 ]; then
    up=$(( $(date +%s) - started_ts ))
    if [ "$up" -lt "$MIN_UPTIME" ]; then
      rm -f "$key"
      if [ -z "$ready" ] || [ "$ready" -lt 1 ] 2>/dev/null; then
        echo "$(date -Is) $name ready=${ready:-none} -> skip (uptime ${up}s < ${MIN_UPTIME}s)" >> "$LOG"
      fi
      continue
    fi
  fi

  if [ -z "$ready" ] || [ "$ready" -lt 1 ] 2>/dev/null; then
    n=$(( $(cat "$key" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$key"
    if [ "$n" -ge 2 ]; then
      now=$(date +%s)
      wstart=$(cat "$wstart_f" 2>/dev/null || echo 0)
      wcount=$(cat "$wcount_f" 2>/dev/null || echo 0)
      # 滑动窗口，窗口外清零。🔴 清零后必须落盘 wstart：
      # 原版只清内存变量，wstart 仍是旧值 ⇒ 每次进来都判超窗 ⇒ 退避永不生效。
      if [ $((now - wstart)) -gt "$BACKOFF_WINDOW_S" ]; then
        wcount=0
        wstart=$now
        echo "$wstart" > "$wstart_f"
        echo 0 > "$wcount_f"
      fi
      if [ "$wcount" -ge "$BACKOFF_MAX" ]; then
        echo "$(date -Is) $name pages_ready=${ready:-none} -> skip (backoff ${wcount}/${BACKOFF_WINDOW_S}s)" >> "$LOG"
        rm -f "$key"
      else
        echo "$(date -Is) $name pages_ready=${ready:-none} -> docker restart" >> "$LOG"
        docker restart "$name" >/dev/null 2>&1
        echo $((wcount + 1)) > "$wcount_f"
        echo "$now" > "$wstart_f"
        rm -f "$key"
      fi
    fi
  else
    rm -f "$key"
  fi
done
