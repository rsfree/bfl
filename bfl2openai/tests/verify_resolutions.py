"""Verify upstream resolution tiers: submit -> poll -> measure output size.

Tiers to verify: 768sq, 1k, 2k, 4k (1.5k already verified = 1536x1536).
Each tier costs one real free-playground generation.
"""
import base64, io, json, sys, time
from curl_cffi import requests as cr, CurlMime
from PIL import Image

BASE = "https://flux-tools.bfl.ai"
H = {"accept": "*/*", "accept-language": "zh-CN,zh;q=0.9", "origin": BASE,
     "referer": BASE + "/precise-editing", "sec-fetch-dest": "empty",
     "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin"}

img = Image.new("RGB", (256, 256), (40, 40, 40))
buf = io.BytesIO(); img.save(buf, "PNG"); png = buf.getvalue()

def run(tier):
    s = cr.Session(impersonate="chrome", trust_env=False, timeout=120)
    mp = CurlMime()
    for n, v in [("mode","i2i"),("prompt","a red circle on white background"),
                 ("aspectRatio","auto"),("resolution",tier),
                 ("boxes","[]"),("references","[]")]:
        mp.addpart(name=n, data=str(v).encode())
    mp.addpart(name="image", filename="image.png", content_type="image/png", data=png)
    t0 = time.time()
    r = s.post(BASE + "/api/flux3-image/generate", multipart=mp, headers=H)
    if r.status_code != 200:
        return {"tier": tier, "submit": r.status_code, "body": r.text[:200]}
    poll = r.json()["pollToken"]
    for _ in range(150):
        time.sleep(2)
        d = s.get(BASE + "/api/flux3-image/status", params={"pollToken": poll},
                  headers=H, timeout=60).json()
        if d.get("status") == "ready":
            rm = s.get(BASE + "/api/flux3-image/media",
                       params={"imageToken": d["imageToken"]}, headers=H, timeout=300)
            if rm.status_code != 200:
                return {"tier": tier, "submit": 200, "media": rm.status_code}
            im = Image.open(io.BytesIO(rm.content))
            return {"tier": tier, "submit": 200, "gen_s": round(time.time()-t0, 1),
                    "size": list(im.size), "bytes": len(rm.content)}
        if d.get("status") == "error":
            return {"tier": tier, "submit": 200, "error": d.get("error")}
    return {"tier": tier, "submit": 200, "error": "poll timeout"}

out = [run(t) for t in ["768sq", "1k", "2k", "4k"]]
print(json.dumps(out, indent=1, ensure_ascii=False))
