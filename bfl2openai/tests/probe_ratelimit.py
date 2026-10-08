"""Rate-limit probe for flux-tools.bfl.ai anonymous generate (minimal cost).

Strategy:
- 64x64 PNG, short prompt, submit-only (never poll / never download media)
  -> each probe costs one quota unit but minimal bandwidth/GPU wait.
- Sequential with small gap; log status/body/ratelimit headers.
- Stop at first non-200 (quota/rate error) or PROBE_CEILING submissions.

NOTE: already-known baseline: ~4 successful generations fired from this IP
earlier today (2 browser captures + 1 direct E2E + 1 adapter E2E).
"""

from __future__ import annotations

import io
import json
import time

from curl_cffi import requests as cr
from curl_cffi import CurlMime
from PIL import Image

BASE = "https://flux-tools.bfl.ai"
HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": BASE,
    "referer": BASE + "/precise-editing",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
PROBE_CEILING = 40          # hard safety ceiling
GAP_FAST_S = 2.0            # gap between probes while everything passes


def tiny_png() -> bytes:
    # upstream rejects images smaller than 256x256 (validated before quota)
    img = Image.new("RGB", (256, 256), (40, 40, 40))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def submit(s: cr.Session, png: bytes, prompt: str = "red"):
    mp = CurlMime()
    for name, val in [
        ("mode", "i2i"),
        ("prompt", prompt),
        ("aspectRatio", "auto"),
        ("resolution", "1.5k"),
        ("boxes", "[]"),
        ("references", "[]"),
    ]:
        mp.addpart(name=name, data=str(val).encode())
    mp.addpart(name="image", filename="image.png", content_type="image/png", data=png)
    t0 = time.monotonic()
    r = s.post(BASE + "/api/flux3-image/generate", multipart=mp, headers=HEADERS, timeout=60)
    return r, time.monotonic() - t0


def main() -> None:
    png = tiny_png()
    print(f"probe image: {len(png)} bytes, ceiling={PROBE_CEILING}, gap={GAP_FAST_S}s")
    s = cr.Session(impersonate="chrome", trust_env=False, timeout=60)

    ok = 0
    for i in range(1, PROBE_CEILING + 1):
        r, dt = submit(s, png)
        rl = {k: v for k, v in r.headers.items() if "ratelimit" in k.lower() or "retry" in k.lower()}
        body = r.text[:220].replace("\n", " ")
        print(f"[{i:02d}] +{dt:4.1f}s HTTP {r.status_code} rl={rl or '-'} body={body}")
        if r.status_code == 200:
            ok += 1
            time.sleep(GAP_FAST_S)
            continue
        # first failure: confirm once after 60s (burst window vs hard cap)
        print("--- first failure, retry once after 60s to classify ---")
        time.sleep(60)
        r2, dt2 = submit(s, png)
        print(f"[retry] +{dt2:4.1f}s HTTP {r2.status_code} body={r2.text[:220]}")
        break

    print(json.dumps({"successful_submissions": ok}, indent=1))


if __name__ == "__main__":
    main()
