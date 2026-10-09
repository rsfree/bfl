"""多形态生成矩阵：三门 × 多参数形态，打生产 https://bfl.1task.cn。

用例（4 发真实生成）：
  case1  OpenAI edits 门   multipart + boxes 扩展          i2i 1k
  case2  BFL 原生门        JSON（无图 t2i + x-key）        1k
  case3  异步门            /v1/uploads 句柄 + prompt 引用   i2i 1k
  case4  异步门            无图 t2i                         2k

用法：python tests/matrix_prod.py [case1 case2 ...]   # 缺省跑全部
注意：句柄/任务都是进程内存 —— 跨实例不清场，重跑请先等上一轮任务终态。
"""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request as ur
from PIL import Image

BASE = "https://bfl.1task.cn"
KEY = "sk-bfl"
SAMPLE = (Path(__file__).resolve().parent / "sample_input.png").read_bytes()
RESULTS = []

#: 沙箱 HTTP_PROXY 会劫持 urlopen 且中途抽风 ⇒ 显式直连。
_OPENER = ur.build_opener(ur.ProxyHandler({}))


def http(method, path, data=None, headers=None, timeout=300):
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    hdrs.setdefault("Authorization", f"Bearer {KEY}")
    hdrs.setdefault("x-key", KEY)
    req = ur.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except ur.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:
        return 0, str(e).encode(), {}


def fetch_image_retry(url, tries=3):
    """curl -sL 子进程抓图（跟随跳转；沙箱 urllib 直连会逐连接抖动）。"""
    for attempt in range(tries):
        p = subprocess.run(["curl", "-sL", "--noproxy", "*", "--max-time", "120", url],
                           capture_output=True, timeout=130)
        if p.returncode == 0 and p.stdout[:8] == b"\x89PNG\r\n\x1a\n":
            return 200, p.stdout, {}
        print(f"  fetch attempt {attempt+1}: rc={p.returncode} "
              f"head={p.stdout[:60]!r}, retrying...")
        time.sleep(2)
    return 0, (p.stdout if p else b""), {}


def save(name, body):
    path = Path(__file__).resolve().parents[1] / "saved_images" / "matrix" / f"{name}.png"
    path.write_bytes(body)
    im = Image.open(path)
    return {"saved": str(path), "format": im.format, "size": list(im.size)}


def record(name, ok, detail):
    detail = {k: (v.decode("utf-8", "replace")[:200] if isinstance(v, bytes) else v)
              for k, v in detail.items()}
    RESULTS.append({"case": name, "ok": ok, **detail})
    print(("PASS " if ok else "FAIL ") + name, json.dumps(detail, ensure_ascii=False))


def poll_native(token, max_s=300):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(2)
        st, body, _ = http("GET", f"/v1/get_result?id={token}")
        d = json.loads(body)
        if d.get("status") == "Ready":
            return d["result"]["sample"]
        if d.get("status") == "Error":
            raise RuntimeError(f"native error: {d.get('details')}")
    raise RuntimeError("native poll timeout")


def poll_async(tid, max_s=300):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(2)
        st, body, _ = http("GET", f"/async/v1/images/generations/{tid}")
        d = json.loads(body)
        if d.get("status") in ("success", "failure", "canceled"):
            if d["status"] != "success":
                raise RuntimeError(f"async {d['status']}: {d.get('error')}")
            return d["data"][0]["url"]
    raise RuntimeError("async poll timeout")


def case1():
    boundary = "----matrixcase1"
    text = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
        f'turn this into a vivid pop-art poster\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="resolution"\r\n\r\n1k\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="boxes"\r\n\r\n'
        + json.dumps([{"box": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5},
                       "instruction": "add bold halftone dots in the center"}]) + "\r\n"
    ).encode()
    body = (text
            + f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
              f'filename="in.png"\r\nContent-Type: image/png\r\n\r\n'.encode()
            + SAMPLE + f"\r\n--{boundary}--\r\n".encode())
    st, resp, _ = http("POST", "/v1/images/edits", body,
                       {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
    info = save("case1_openai_edits_boxes", raw)
    # i2i 无 size = auto = 画布派生：输出跟随输入图比例（断言比例而非绝对尺寸）
    in_w, in_h = Image.open(io.BytesIO(SAMPLE)).size
    out_w, out_h = info["size"]
    aspect_kept = abs(out_w / out_h - in_w / in_h) / (in_w / in_h) < 0.12
    record("openai_edits+boxes(i2i,1k)", st == 200 and aspect_kept,
           {"http": st, **info})


def case2():
    payload = {"prompt": "a serene mountain lake at dusk, minimalist composition",
               "aspect_ratio": "1:1", "resolution": "1k"}
    st, resp, _ = http("POST", "/v1/flux-3-image",
                       json.dumps(payload).encode(), {"x-key": "sk-bfl"})
    token = json.loads(resp)["id"]
    url = poll_native(token)
    st2, img, _ = fetch_image_retry(url)
    info = save("case2_native_t2i", img)
    record("native_json(t2i,1k)", st2 == 200 and info["size"] == [1024, 1024],
           {"http": st2, **info})


def case3():
    mp = "----matrixcase3"
    body = (f'--{mp}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="Clipboard_Screenshot.png"\r\nContent-Type: image/png\r\n\r\n'
            ).encode() + SAMPLE + f"\r\n--{mp}--\r\n".encode()
    st, resp, _ = http("POST", "/v1/uploads", body,
                       {"Content-Type": f"multipart/form-data; boundary={mp}"})
    ref = json.loads(resp)["files"][0]["ref"]
    st, resp, _ = http("POST", "/async/v1/images/generations",
                       json.dumps({"prompt": f"edit {ref}: make the whole image "
                                             f"a warm sunset palette",
                                   "resolution": "1k"}).encode())
    assert st == 202, resp
    tid = json.loads(resp)["task_id"]
    url = poll_async(tid)
    st2, img, _ = fetch_image_retry(url)
    info = save("case3_async_upload_handle", img)
    record("async_uploads_handle(i2i,1k)", st2 == 200 and info["size"] == [1024, 1024],
           {"http": st2, **info})


def case4():
    st, resp, _ = http("POST", "/async/v1/images/generations",
                       json.dumps({"prompt": "a neon-lit cyberpunk street after rain, "
                                             "reflections everywhere, ultra detailed",
                                   "resolution": "2k"}).encode())
    tid = json.loads(resp)["task_id"]
    url = poll_async(tid)
    st2, img, _ = fetch_image_retry(url)
    info = save("case4_async_t2i_2k", img)
    record("async_t2i(2k)", st2 == 200 and info["size"] == [2048, 2048],
           {"http": st2, **info})


def case5():
    """原生门 i2i + bounding_boxes（form boxes 字段 + 局部指令）。"""
    payload = {"prompt": "keep the scene",
               "images": ["data:image/png;base64,"
                          + base64.b64encode(SAMPLE).decode()],
               "boxes": [{"box": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.6},
                          "instruction": "replace the center with blooming flowers"}],
               "resolution": "1k"}
    st, resp, _ = http("POST", "/v1/flux-3-image",
                       json.dumps(payload).encode(), {"x-key": "sk-bfl"})
    token = json.loads(resp)["id"]
    url = poll_native(token)
    st2, img, _ = fetch_image_retry(url)
    info = save("case5_native_i2i_bbox", img)
    record("native_i2i+bounding_boxes(1k)",
           st2 == 200 and info["size"] == [1024, 1024],
           {"http": st2, **info})


ALL = (case1, case2, case3, case4, case5)

if __name__ == "__main__":
    only = set(sys.argv[1:]) or None
    for case in ALL:
        if only and case.__name__ not in only:
            continue
        try:
            case()
        except Exception as exc:  # noqa: BLE001
            record(case.__name__, False, {"error": str(exc)[:200]})
        time.sleep(2)
    ok = sum(r["ok"] for r in RESULTS)
    print("MATRIX:", "ALL PASS" if ok == len(RESULTS) else "HAS FAILURES",
          f"({ok}/{len(RESULTS)})")
    json.dump(RESULTS, open(Path(__file__).parent / "matrix_results.json", "w"),
              ensure_ascii=False, indent=1)
