#!/bin/sh
# generations 门 · 图生图最小脚本
# 用法: BFL_KEY=sk-... ./gen_i2i.sh <输入图> "<prompt>" [输出.png]
# 可选: API=https://bfl.1task.cn（默认）
# 注意: prompt 内不要含双引号（本脚本用 printf 直接拼 JSON）
set -e
IN=${1:?用法: $0 <输入图> "<prompt>" [输出.png]}
P=${2:?缺少 prompt}
OUT=${3:-out.png}
API=${API:-https://bfl.1task.cn}

REQ=$(mktemp -t i2i.XXXXXX); RSP=$(mktemp -t i2i.XXXXXX)
trap 'rm -f "$REQ" "$RSP"' EXIT

printf '{"prompt":"%s","image":"%s","size":"1024x1024"}' \
  "$P" "$(openssl base64 -A -in "$IN")" > "$REQ"

curl -sS --noproxy '*' --max-time 150 -o "$RSP" -X POST "$API/v1/images/generations" \
  -H "Authorization: Bearer ${BFL_KEY:?需要环境变量 BFL_KEY}" \
  -H 'Content-Type: application/json' --data-binary "@$REQ"

uv run --no-project python -c 'import base64,json,sys
d = json.load(open(sys.argv[1]))
assert "data" in d, d
open(sys.argv[2], "wb").write(base64.b64decode(d["data"][0]["b64_json"]))' "$RSP" "$OUT"

echo "ok -> $OUT"
