"""size 生效矩阵：各端点 size/aspect_ratio 语义 + 产物转存链接验证（可并发）。

用例（5 发真实生成）：
  g1  OpenAI generations size=1024x1024 -> 1:1
  g2  OpenAI generations size=1536x1024 -> 3:2 横幅
  e1  OpenAI edits      size=1024x1536 -> 2:3 竖幅（i2i）
  n1  原生门 aspect_ratio=16:9 -> 触发 1k 容量守卫自动升 2k（1360x768）
  a1  异步门 t2i 2k -> 2048²
"""
import base64
import io
import json
import subprocess
import sys
import time
import urllib.request as ur
from pathlib import Path

from PIL import Image

BASE = "https://bfl.1task.cn"
KEY = "sk-bfl"
SAMPLE = (Path(__file__).resolve().parent / "sample_input.png").read_bytes()
OUT = Path(__file__).resolve().parents[1] / "saved_images" / "matrix"
RESULTS = []
_OPENER = ur.build_opener(ur.ProxyHandler({}))


def http(method, path, data=None, headers=None, timeout=300):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    hdrs.setdefault("Authorization", f"Bearer {KEY}")
    req = ur.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except ur.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def fetch_image(url, name, tries=3):
    for _ in range(tries):
        p = subprocess.run(["curl", "-sL", "--noproxy", "*", "--max-time", "120", url],
                           capture_output=True, timeout=130)
        if p.returncode == 0 and p.stdout[:8] == b"\x89PNG\r\n\x1a\n":
            fp = OUT / f"{name}.png"
            fp.write_bytes(p.stdout)
            im = Image.open(fp)
            return {"url_ok": True, "size": list(im.size), "saved": str(fp)}
        time.sleep(2)
    return {"url_ok": False,
            "head": (p.stdout[:80].decode("utf-8", "replace") if p else "no attempt")}


def record(case, ok, detail):
    detail = {k: (v.decode("utf-8", "replace")[:160] if isinstance(v, bytes) else v)
              for k, v in detail.items()}
    RESULTS.append({"case": case, "ok": ok, **detail})
    print(("PASS " if ok else "FAIL ") + case, json.dumps(detail, ensure_ascii=False), flush=True)


def dims_ok(size, got):
    w, h = got
    ew, eh = map(int, size.lower().split("x"))
    return abs(w / h - ew / eh) / (ew / eh) < 0.1


def poll_native(token, max_s=300):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(2)
        st, resp = http("GET", f"/v1/get_result?id={token}")
        d = json.loads(resp)
        if d.get("status") == "Ready":
            return d["result"]["sample"]
        if d.get("status") == "Error":
            raise RuntimeError(f"native error: {d.get('details')}")
    raise RuntimeError("native poll timeout 300s")


def poll_async(tid, max_s=300):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(2)
        st, resp = http("GET", f"/async/v1/images/generations/{tid}")
        d = json.loads(resp)
        if d.get("status") in ("success", "failure", "canceled"):
            if d["status"] != "success":
                raise RuntimeError(f"async {d['status']}: {d.get('error')}")
            return d["data"][0]["url"]
    raise RuntimeError("async poll timeout 300s")


def g1():
    st, resp = http("POST", "/v1/images/generations",
                    json.dumps({"model": "flux-precise-edit",
                                "prompt": "a tiny wooden cabin in a pine forest, square frame",
                                "size": "1024x1024", "resolution": "1k"}).encode())
    d = json.loads(resp)
    im = Image.open(io.BytesIO(base64.b64decode(d["data"][0]["b64_json"])))
    got = list(im.size)
    record("openai_generations size=1024x1024", dims_ok("1024x1024", got) and st == 200,
           {"http": st, "got": got})


def g2():
    st, resp = http("POST", "/v1/images/generations",
                    json.dumps({"model": "flux-precise-edit",
                                "prompt": "a long wooden bridge over a river, wide panorama",
                                "size": "1536x1024", "resolution": "1k"}).encode())
    d = json.loads(resp)
    im = Image.open(io.BytesIO(base64.b64decode(d["data"][0]["b64_json"])))
    got = list(im.size)
    record("openai_generations size=1536x1024(3:2)", dims_ok("1536x1024", got) and st == 200,
           {"http": st, "got": got})


def e1():
    mp = "----sz"
    body = (f'--{mp}\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
            f'same scene, vertical poster crop\r\n'
            f'--{mp}\r\nContent-Disposition: form-data; name="size"\r\n\r\n1024x1536\r\n'
            f'--{mp}\r\nContent-Disposition: form-data; name="resolution"\r\n\r\n1k\r\n').encode()
    body += (f'--{mp}\r\nContent-Disposition: form-data; name="image"; filename="in.png"\r\n'
             f'Content-Type: image/png\r\n\r\n').encode() + SAMPLE + f"\r\n--{mp}--\r\n".encode()
    st, resp = http("POST", "/v1/images/edits", body,
                    {"Content-Type": f"multipart/form-data; boundary={mp}"})
    d = json.loads(resp)
    im = Image.open(io.BytesIO(base64.b64decode(d["data"][0]["b64_json"])))
    got = list(im.size)
    record("openai_edits size=1024x1536(2:3)", dims_ok("1024x1536", got) and st == 200,
           {"http": st, "got": got})


def n1():
    st, resp = http("POST", "/v1/flux-3-image",
                    json.dumps({"prompt": "a lighthouse on a cliff, wide seascape",
                                "aspect_ratio": "16:9", "resolution": "1k"}).encode())
    tid = json.loads(resp)["id"]
    url = poll_native(tid)
    cos = "myqcloud.com" in url
    info = fetch_image(url, "caseN_native_16x9")
    w, h = info.get("size", [0, 0])
    record("native 16:9 自动升2k + COS链", bool(cos and info.get("url_ok") and w > h),
           {"cos_link": cos, **info})


def a1():
    st, resp = http("POST", "/async/v1/images/generations",
                    json.dumps({"prompt": "a field of sunflowers under a blue sky",
                                "resolution": "2k"}).encode())
    tid = json.loads(resp)["task_id"]
    url = poll_async(tid)
    cos = "myqcloud.com" in url
    info = fetch_image(url, "caseA_async_t2i_2k")
    w, h = info.get("size", [0, 0])
    record("async_t2i 2k + COS链", bool(cos and info.get("url_ok") and w == h and w >= 2048),
           {"cos_link": cos, **info})


CASES = (g1, g2, e1, n1, a1)

if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    for case in CASES:
        if only and case.__name__ not in only:
            continue
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            record(case.__name__, False, {"error": str(exc)[:200]})
        time.sleep(2)
    ok = sum(r["ok"] for r in RESULTS)
    print("SIZE MATRIX:", "ALL PASS" if ok == len(RESULTS) else "HAS FAILURES",
          f"({ok}/{len(RESULTS)})")
    json.dump(RESULTS, open(OUT / "size_matrix.json", "w"), ensure_ascii=False, indent=1)
