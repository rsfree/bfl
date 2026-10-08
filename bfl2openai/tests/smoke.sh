#!/usr/bin/env bash
# Smoke test for bfl2openai. Fires ONE real upstream generation (free demo).
# Usage: bash tests/smoke.sh [port]
set -euo pipefail
PORT="${1:-8977}"
BASE="http://127.0.0.1:${PORT}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== healthz =="
curl -sf --noproxy '*' "$BASE/healthz"; echo

echo "== /v1/models =="
curl -sf --noproxy '*' "$BASE/v1/models"; echo

echo "== POST /v1/images/edits (one real generation, ~30s) =="
curl -s --noproxy '*' -X POST "$BASE/v1/images/edits" \
  -F "image=@$DIR/sample_input.png;type=image/png" \
  -F "prompt=change the background color to solid blue" \
  -F "response_format=b64_json" \
  -o "$DIR/smoke_response.json" \
  -w "HTTP %{http_code}, %{size_download} bytes\n"

python3 - "$DIR" <<'EOF'
import json, sys, base64, io
from PIL import Image
d = json.load(open(sys.argv[1] + "/smoke_response.json"))
assert "data" in d and d["data"] and "b64_json" in d["data"][0], d
img = Image.open(io.BytesIO(base64.b64decode(d["data"][0]["b64_json"])))
img.save(sys.argv[1] + "/smoke_result.png")
print("OK:", img.format, img.size, "->", "smoke_result.png")
EOF
