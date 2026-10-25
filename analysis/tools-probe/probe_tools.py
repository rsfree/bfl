"""flux-tools.bfl.ai 三个公开工具的匿名可用性探针（2026-10-24 实测通过）。

用法：python probe_tools.py [outpainting|erase|vto]
产物落在本目录。注意：会真实提交生成（playground 匿名免费，别高频刷）。
"""
import base64, io, json, sys, time, os
from curl_cffi import requests, CurlMime
from PIL import Image, ImageDraw

BASE = "https://flux-tools.bfl.ai"
OUT = os.path.dirname(os.path.abspath(__file__))
S = requests.Session(impersonate="chrome", trust_env=False)
H = {"Origin": BASE, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


def dataurl(im):
    b = io.BytesIO(); im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def make_base(path=f"{OUT}/base.png"):
    im = Image.new("RGB", (512, 512))
    d = ImageDraw.Draw(im)
    for y in range(512):
        d.line([(0, y), (511, y)], fill=(30 + y // 8, 90, 200 - y // 4))
    d.ellipse([150, 150, 360, 360], fill=(240, 200, 60))
    d.rectangle([40, 420, 200, 480], fill=(220, 60, 60))
    im.save(path)
    return im


def save(url, name):
    r = S.get(url, timeout=120)
    print("  media", r.status_code, len(r.content), r.headers.get("content-type"))
    if r.ok:
        open(f"{OUT}/{name}", "wb").write(r.content)


def outpainting(im, w=1012, h=512):
    """同步扩图：2MP 上限，fast/high 两档。"""
    r = S.post(f"{BASE}/api/outpainting", headers={**H, "Referer": f"{BASE}/outpainting",
               "Content-Type": "application/json"},
               json={"imageDataUrl": dataurl(im), "width": w, "height": h, "mode": "fast"}, timeout=300)
    print("outpainting", r.status_code, r.text[:200].replace("\n", " "))
    if r.ok:
        j = r.json(); json.dump(j, open(f"{OUT}/outpainting.json", "w"), indent=1)
        save(j["imageUrl"], "outpainting_result.png")


def erase(im, dilate=8):
    """同步擦除：mask 白色 = 待擦除区。curl_cffi 必须用 CurlMime。"""
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).ellipse([140, 140, 370, 370], fill=255)
    b1, b2 = io.BytesIO(), io.BytesIO()
    im.save(b1, "PNG"); mask.save(b2, "PNG")
    mp = CurlMime()
    mp.addpart(name="image", filename="image.png", content_type="image/png", data=b1.getvalue())
    mp.addpart(name="mask", filename="mask.png", content_type="image/png", data=b2.getvalue())
    mp.addpart(name="dilatePixels", data=str(dilate).encode())
    r = S.post(f"{BASE}/api/erase", headers={**H, "Referer": f"{BASE}/erase"}, multipart=mp, timeout=300)
    print("erase", r.status_code, r.text[:200].replace("\n", " "))
    if r.ok:
        j = r.json(); json.dump(j, open(f"{OUT}/erase.json", "w"), indent=1)
        save(j["imageUrl"], "erase_result.png")


def vto(user_im, garment_im):
    """虚拟试衣：字段已探明，2026-10-24 未跑通出图（请求会长时间同步等待）。"""
    r = S.post(f"{BASE}/api/vto", headers={**H, "Referer": f"{BASE}/virtual-try-on",
               "Content-Type": "application/json"},
               json={"userImageDataUrl": dataurl(user_im),
                     "garmentDataUrls": [dataurl(garment_im)]}, timeout=300)
    print("vto", r.status_code, r.text[:300].replace("\n", " "))


if __name__ == "__main__":
    im = make_base()
    which = sys.argv[1] if len(sys.argv) > 1 else "outpainting"
    t0 = time.time()
    {"outpainting": lambda: outpainting(im), "erase": lambda: erase(im)}.get(which, lambda: None)()
    print(f"done in {time.time()-t0:.1f}s")
