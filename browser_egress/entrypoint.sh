#!/bin/bash
# 上游池是 socks5 + 认证，Chromium 不支持认证。socksbridge（纯 Python）
# 把带认证的上游转成本地无认证 127.0.0.1:1080。
if [ -n "$UPSTREAM_PROXY" ]; then
  python - "$UPSTREAM_PROXY" <<'PY' &
import sys
from urllib.parse import unquote, urlparse
u = urlparse(sys.argv[1])
args = ["1080", u.hostname, str(u.port or 1080),
        unquote(u.username or ""), unquote(u.password or "")]
import subprocess
subprocess.run([sys.executable, "/app/socksbridge.py", *args], check=False)
PY
  for i in $(seq 1 20); do
    if curl -s --noproxy "" -x socks5h://127.0.0.1:1080 --max-time 3 \
        https://api.ipify.org >/dev/null 2>&1; then
      echo "[socksbridge] ready"
      break
    fi
    sleep 0.5
  done
  export BFL2OPENAI_EGRESS_PROXY="socks5://127.0.0.1:1080"
fi
exec uvicorn main:app --host 0.0.0.0 --port 8011 --workers 1
