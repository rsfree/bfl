#!/usr/bin/env bash
# curl 多图生图：POST /v1/images/edits（主图 + 任意张参考图）
#
# 用法:
#   ./curl_multi_edit.sh <out.png> "<prompt>" <img1> [img2 [img3 ...]]
#
# 例:
#   ./curl_multi_edit.sh out.png "put the duck from image 2 into the mug" mug.png duck.png
#
# 环境变量:
#   API_BASE       默认 http://127.0.0.1:8977
#   SPELL          字段拼法。默认 "image[]"（OpenAI 官方 curl 写法）
#                  设 SPELL=image 则用重复同名字段（openai-python SDK 写法）
#   RESOLUTION     1k | 1.5k | 2k ...，默认 1k
#   SIZE           可选，如 1024x1024（覆盖 aspectRatio 推导）
#   ASPECT_RATIO   可选，如 16:9
#   RESPONSE_FORMAT b64_json（默认，脚本解码落盘）| url（脚本再下载）
#   BFL2OPENAI_API_KEY 未设置时自动从 bfl2openai/.env 读取
#   ENV_FILE       自定义 .env 路径
#
# 注意: 本地回环必须 --noproxy '*'，否则 HTTP_PROXY 会把请求带去代理。
set -euo pipefail

API_BASE="${API_BASE:-http://127.0.0.1:8977}"
SPELL="${SPELL:-image[]}"
RESOLUTION="${RESOLUTION:-1k}"
RESPONSE_FORMAT="${RESPONSE_FORMAT:-b64_json}"
SIZE="${SIZE:-}"
ASPECT_RATIO="${ASPECT_RATIO:-}"
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env}"

if [[ -z "${BFL2OPENAI_API_KEY:-}" && -f "$ENV_FILE" ]]; then
  BFL2OPENAI_API_KEY="$(python3 - "$ENV_FILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("BFL2OPENAI_API_KEY="):
        print(line.split("=", 1)[1].strip().strip('"').strip("'"))
        break
PY
)"
fi

if (( $# < 3 )); then
  sed -n '2,10p' "${BASH_SOURCE[0]}" >&2
  exit 2
fi

OUT="$1"; PROMPT="$2"; shift 2

FIELDS=(-F "prompt=$PROMPT" -F "resolution=$RESOLUTION" -F "response_format=$RESPONSE_FORMAT")
[[ -n "$SIZE" ]] && FIELDS+=(-F "size=$SIZE")
[[ -n "$ASPECT_RATIO" ]] && FIELDS+=(-F "aspect_ratio=$ASPECT_RATIO")
for f in "$@"; do
  [[ -f "$f" ]] || { echo "no such image: $f" >&2; exit 2; }
  FIELDS+=(-F "$SPELL=@$f")
done

AUTH=()
[[ -n "${BFL2OPENAI_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer $BFL2OPENAI_API_KEY")

BODY="$(mktemp -t bfl.XXXXXX.json)"
trap 'rm -f "$BODY"' EXIT

echo "-> POST $API_BASE/v1/images/edits  spell=$SPELL  images=$#  res=$RESOLUTION" >&2
START=$(date +%s)
CODE="$(curl -s --noproxy '*' -X POST "$API_BASE/v1/images/edits" \
  "${AUTH[@]}" "${FIELDS[@]}" -o "$BODY" -w '%{http_code}')"
ELAPSED=$(( $(date +%s) - START ))

if [[ "$CODE" != "200" ]]; then
  echo "!! HTTP $CODE after ${ELAPSED}s" >&2
  cat "$BODY" >&2; echo >&2
  exit 1
fi

python3 - "$BODY" "$OUT" "$ELAPSED" <<'PY'
import base64, json, sys
body, out, elapsed = sys.argv[1], sys.argv[2], sys.argv[3]
doc = json.load(open(body, encoding="utf-8"))
item = doc["data"][0]
if "b64_json" in item:
    raw = base64.b64decode(item["b64_json"])
    open(out, "wb").write(raw)
    print(f"ok {elapsed}s -> {out} ({len(raw)} bytes)")
else:
    print(f"ok {elapsed}s url={item.get('url')}")
    print("（RESPONSE_FORMAT=url 时用 curl 自行下载该地址）")
PY
