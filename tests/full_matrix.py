"""全形态覆盖测试：已实现端点 × 参数形态，打生产 https://bfl.1task.cn。

分组（避免单次运行过长）：
  python tests/full_matrix.py basic    零成本：健康/模型/鉴权矩阵/参数校验
  python tests/full_matrix.py sync     同步门：generations/edits 各形态
  python tests/full_matrix.py native   原生门：t2i/i2i/多参考/16:9升档/4k复合
  python tests/full_matrix.py async    上传句柄 + 异步受理/轮询/列表/取消
  python tests/full_matrix.py all      全部

成本口径：生成走 Playground 匿名额度（免费）；4k 会触发 MPS（腾讯云按量），
默认跑；8k 跳过（与 4k 同形态、仅超分倍率不同，避免重复付费）。
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import time
import urllib.request as ur
from pathlib import Path

from PIL import Image, ImageDraw

BASE = "https://bfl.1task.cn"
KEY = "sk-bfl"
OUT = Path(__file__).resolve().parents[1] / "saved_images" / "full_matrix"
OUT.mkdir(parents=True, exist_ok=True)
_OPENER = ur.build_opener(ur.ProxyHandler({}))   # 沙箱代理不可信，显式直连
RESULTS: list[dict] = []


def http(method: str, path: str, data=None, headers=None, timeout=300,
         auth: bool = True, xkey: bool = False):
    hdrs = {"Content-Type": "application/json"}
    if auth:
        hdrs["Authorization"] = f"Bearer {KEY}"
    if xkey:
        hdrs["x-key"] = KEY
    hdrs.update(headers or {})
    req = ur.Request(BASE + path, data=data, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except ur.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def record(case: str, ok: bool, detail: dict):
    RESULTS.append({"case": case, "ok": ok, **detail})
    print(("PASS " if ok else "FAIL ") + f"{case:44s}",
          json.dumps(detail, ensure_ascii=False)[:150], flush=True)


def save_png(name: str, body: bytes) -> list[int]:
    path = OUT / f"{name}.png"
    path.write_bytes(body)
    return list(Image.open(path).size)


def curl_img(url: str, tries: int = 3):
    """curl -sL 抓图（跟随跳转、重试）。"""
    for _ in range(tries):
        p = subprocess.run(["curl", "-sL", "--noproxy", "*", "--max-time", "150", url],
                           capture_output=True, timeout=160)
        if p.returncode == 0 and p.stdout[:8] == b"\x89PNG\r\n\x1a\n":
            return p.stdout
        time.sleep(2)
    return None


def ratio_close(got, want, tol=0.12) -> bool:
    w, h = got
    return abs((w / h) - want) / want < tol


def gen_png(w=512, h=512, color=(220, 60, 60), shape="circle") -> bytes:
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    box = [w * 0.2, h * 0.2, w * 0.8, h * 0.8]
    (d.ellipse if shape == "circle" else d.rectangle)(box, fill=color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def multipart(fields: dict, file_field: str, filename: str, content: bytes):
    boundary = "----fullmatrix"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body += content + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def multipart_multi(fields: dict, files: list[tuple[str, str, bytes]]):
    """files: [(field, filename, content)] — 同名重复字段即多垫图。"""
    boundary = "----fullmatrixmulti"
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    for field, filename, content in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n"
                 ).encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def poll_native(token: str, max_s=300):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(3)
        st, body = http("GET", f"/v1/get_result?id={token}", xkey=True)
        d = json.loads(body)
        if d.get("status") == "Ready":
            return d["result"]["sample"], time.time() - t0
        if d.get("status") == "Error":
            raise RuntimeError(str(d.get("details"))[:120])
    raise RuntimeError("native poll timeout")


def poll_async(tid: str, max_s=420):
    t0 = time.time()
    while time.time() - t0 < max_s:
        time.sleep(3)
        st, body = http("GET", f"/async/v1/images/generations/{tid}")
        d = json.loads(body)
        if d.get("status") in ("success", "failure", "canceled"):
            return d, time.time() - t0
    raise RuntimeError("async poll timeout")


# ────────────────────────── basic（零成本） ──────────────────────────
def grp_basic():
    st, body = http("GET", "/healthz", auth=False)
    record("basic.healthz", st == 200 and b'"ok":true' in body, {"http": st})

    st, body = http("GET", "/v1/models")
    models = [m["id"] for m in json.loads(body).get("data", [])] if st == 200 else []
    record("basic.models", st == 200 and bool(models), {"http": st, "models": models})

    # 鉴权矩阵：无 key / 错 key → 401
    for path, xk in (("/v1/images/generations", False), ("/v1/flux-3-image", True)):
        st, _ = http("POST", path, json.dumps({"prompt": "x"}).encode(),
                     auth=False, xkey=False)
        record(f"auth.no-key {path}", st == 401, {"http": st})
    st, _ = http("POST", "/v1/images/generations",
                 json.dumps({"prompt": "x"}).encode(),
                 headers={"Authorization": "Bearer wrong-key"})
    record("auth.wrong-key", st == 401, {"http": st})

    # 参数校验
    st, body = http("POST", "/v1/images/edits",
                    *multipart({"prompt": "x"}, "image", "i.png", gen_png()))
    st2, _ = http("POST", "/v1/images/edits",
                  multipart({"prompt": "x"}, "image", "i.png", b"")[0],
                  multipart({}, "image", "i.png", b"")[1])
    record("validate.edits-empty-image", st2 in (400, 422), {"http": st2})
    st3, body3 = http("POST", "/v1/flux-3-image",
                      json.dumps({"prompt": "x", "images": ["u"] * 11}).encode(),
                      xkey=True)
    record("validate.native-max-10-images", st3 == 422, {"http": st3})


# ────────────────────────── sync（OpenAI 门） ──────────────────────────
def grp_sync():
    # S1 generations b64 1k 1:1
    st, body = http("POST", "/v1/images/generations", json.dumps({
        "prompt": "a red apple on a white plate", "resolution": "1k",
        "size": "1024x1024"}).encode())
    ok = False
    if st == 200:
        raw = base64.b64decode(json.loads(body)["data"][0]["b64_json"])
        size = save_png("sync_generations_b64_1k", raw)
        ok = size == [1024, 1024]
    record("sync.generations b64 1k 1:1", ok, {"http": st})

    # S2 generations url 模式（默认 1.5k）+ COS 链接
    st, body = http("POST", "/v1/images/generations", json.dumps({
        "prompt": "a lighthouse at night", "response_format": "url"}).encode())
    url = json.loads(body)["data"][0].get("url") if st == 200 else None
    img = curl_img(url) if url else None
    size = save_png("sync_generations_url", img) if img else None
    record("sync.generations url(COS 24h)", bool(img) and "myqcloud" in (url or ""),
           {"http": st, "size": size})

    # S3 size=1536x1024 → 3:2
    st, body = http("POST", "/v1/images/generations", json.dumps({
        "prompt": "wide landscape with a bridge", "size": "1536x1024",
        "resolution": "1k"}).encode())
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(body)["data"][0]["b64_json"])
        size = save_png("sync_generations_3x2", raw)
    record("sync.generations size 3:2", bool(size and ratio_close(size, 1.5)),
           {"http": st, "size": size})

    # S4 edits 单图
    body, hdrs = multipart({"prompt": "make it a watercolor painting",
                            "resolution": "1k"}, "image", "in.png", gen_png())
    st, resp = http("POST", "/v1/images/edits", body, hdrs)
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_edits_single", raw)
    record("sync.edits single image", bool(size and size == [1024, 1024]),
           {"http": st, "size": size})

    # S5 edits + boxes 扩展
    body, hdrs = multipart({
        "prompt": "add stars in the marked area", "resolution": "1k",
        "boxes": json.dumps([{"box": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3},
                             "instruction": "draw a bright yellow star"}])},
        "image", "in.png", gen_png())
    st, resp = http("POST", "/v1/images/edits", body, hdrs)
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_edits_boxes", raw)
    record("sync.edits boxes extension", bool(size), {"http": st, "size": size})

    # S6 edits 大图（>4MB 触发规范化路径）
    # ~5MB：刚超 4MB 预算触发规范化，且沙箱上行（~220KB/s）可承载
    big = Image.frombytes("RGB", (1300, 1300), __import__("os").urandom(1300 * 1300 * 3))
    buf = io.BytesIO()
    big.save(buf, "PNG", compress_level=1)
    body, hdrs = multipart({"prompt": "abstract art", "resolution": "1k"},
                           "image", "big.png", buf.getvalue())
    st, resp = http("POST", "/v1/images/edits", body, hdrs)
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_edits_big_normalized", raw)
    record("sync.edits big(>4MB,auto-normalize)", bool(size), {"http": st, "size": size})

    # S7 edits 多垫图（3 张：主图 + 2 references）
    files = [("image", f"in{i}.png", gen_png(color=c, shape=s))
             for i, (c, s) in enumerate((((220, 60, 60), "circle"),
                                         ((60, 90, 220), "rect"),
                                         ((60, 200, 90), "circle")), start=1)]
    body, hdrs = multipart_multi(
        {"prompt": "arrange all three reference shapes in one row",
         "resolution": "1k"}, files)
    st, resp = http("POST", "/v1/images/edits", body, hdrs)
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_edits_multi_image", raw)
    record("sync.edits multi-image(3垫图)", bool(size), {"http": st, "size": size})

    # S8 generations 单垫图（image=string）
    one = "data:image/png;base64," + base64.b64encode(
        gen_png(color=(200, 80, 40), shape="circle")).decode()
    st, resp = http("POST", "/v1/images/generations", json.dumps({
        "prompt": "make the circle look like a glowing orange sun",
        "image": one, "resolution": "1k", "aspect_ratio": "1:1"}).encode())
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_generations_image_string", raw)
    record("sync.generations image=string(单垫图)", bool(size),
           {"http": st, "size": size})

    # S9 generations 多垫图（image=list，1 主 + 2 refs）
    multi = ["data:image/png;base64," + base64.b64encode(
        gen_png(color=c, shape=s)).decode()
        for c, s in (((220, 60, 60), "circle"), ((60, 90, 220), "rect"),
                     ((60, 200, 90), "circle"))]
    st, resp = http("POST", "/v1/images/generations", json.dumps({
        "prompt": "combine the three reference shapes side by side",
        "image": multi, "resolution": "1k", "aspect_ratio": "1:1"}).encode())
    size = None
    if st == 200:
        raw = base64.b64decode(json.loads(resp)["data"][0]["b64_json"])
        size = save_png("sync_generations_image_list", raw)
    record("sync.generations image=list(多垫图)", bool(size),
           {"http": st, "size": size})


# ────────────────────────── native（原生门） ──────────────────────────
def grp_native():
    # N1 t2i 16:9 @1k → 自动升 2k，1360×768
    st, body = http("POST", "/v1/flux-3-image", json.dumps({
        "prompt": "a wide desert road at sunset",
        "aspect_ratio": "16:9", "resolution": "1k"}).encode(), xkey=True)
    url, dt = poll_native(json.loads(body)["id"])
    img = curl_img(url)
    size = save_png("native_t2i_16x9", img) if img else None
    record("native.t2i 16:9(1k→2k升档)", bool(size and ratio_close(size, 16 / 9)),
           {"size": size, "sec": round(dt)})

    # N2 i2i 单图
    st, body = http("POST", "/v1/flux-3-image", json.dumps({
        "prompt": "turn the circle into a glossy sphere",
        "images": ["data:image/png;base64," +
                   base64.b64encode(gen_png()).decode()],
        "aspect_ratio": "1:1", "resolution": "1k"}).encode(), xkey=True)
    url, dt = poll_native(json.loads(body)["id"])
    img = curl_img(url)
    size = save_png("native_i2i_single", img) if img else None
    record("native.i2i single", bool(size and size == [1024, 1024]),
           {"size": size, "sec": round(dt)})

    # N3 多参考图（1 主 + 2 refs）
    imgs = ["data:image/png;base64," + base64.b64encode(
        gen_png(color=c, shape=s)).decode()
        for c, s in (((220, 60, 60), "circle"), ((60, 90, 220), "rect"),
                     ((60, 200, 90), "circle"))]
    st, body = http("POST", "/v1/flux-3-image", json.dumps({
        "prompt": "arrange the three reference shapes in a single row",
        "images": imgs, "aspect_ratio": "1:1", "resolution": "1k"}).encode(),
        xkey=True)
    url, dt = poll_native(json.loads(body)["id"])
    img = curl_img(url)
    size = save_png("native_i2i_multi_refs", img) if img else None
    record("native.i2i multi-ref(1+2)", bool(size), {"size": size, "sec": round(dt)})

    # N4 4k 复合（MPS；唯一付费项）
    st, body = http("POST", "/v1/flux-3-image", json.dumps({
        "prompt": "a detailed mountain valley with a river",
        "aspect_ratio": "1:1", "resolution": "4k"}).encode(), xkey=True)
    url, dt = poll_native(json.loads(body)["id"], max_s=600)
    img = curl_img(url)
    size = save_png("native_t2i_4k_mps", img) if img else None
    record("native.t2i 4k(MPS复合)", bool(size and size == [4096, 4096]),
           {"size": size, "sec": round(dt)})


# ────────────────────────── async（上传 + 异步门） ──────────────────────────
def grp_async():
    # U1 /v1/uploads 句柄
    body, hdrs = multipart({}, "file", "Clipboard_Screenshot.png", gen_png())
    st, resp = http("POST", "/v1/uploads", body, hdrs)
    ref = json.loads(resp)["files"][0]["ref"] if st == 200 else None
    record("uploads.handle", bool(ref and ref.startswith("@image#")),
           {"http": st, "ref": ref})

    # A1 异步受理（句柄 i2i）→ success → COS 链
    st, resp = http("POST", "/async/v1/images/generations", json.dumps({
        "prompt": f"edit {ref}: turn it into a neon cyberpunk version",
        "resolution": "1k"}).encode())
    tid = json.loads(resp)["task_id"] if st == 202 else None
    record("async.accept(202)", st == 202 and bool(tid), {"http": st})
    if tid:
        d, dt = poll_async(tid)
        url = d["data"][0]["url"] if d["status"] == "success" else None
        img = curl_img(url) if url else None
        size = save_png("async_handle_i2i", img) if img else None
        record("async.handle-i2i → success",
               bool(img) and "myqcloud" in (url or ""),
               {"status": d["status"], "size": size, "sec": round(dt),
                "task_id": tid})

    # A2 异步 t2i
    st, resp = http("POST", "/async/v1/images/generations", json.dumps({
        "prompt": "a hot air balloon over green hills", "resolution": "1k"}).encode())
    tid = json.loads(resp)["task_id"] if st == 202 else None
    if tid:
        d, dt = poll_async(tid)
        img = curl_img(d["data"][0]["url"]) if d["status"] == "success" else None
        size = save_png("async_t2i", img) if img else None
        record("async.t2i → success", bool(img), {"status": d["status"],
                                                  "size": size, "sec": round(dt)})

    # A3 列表（Bearer 指纹）
    st, body = http("GET", "/async/v1/images/generations?limit=50")
    tasks = json.loads(body).get("tasks", []) if st == 200 else []
    record("async.list", st == 200 and len(tasks) >= 2,
           {"http": st, "count": len(tasks)})

    # A4 生命周期管理（jimeng 语义：无 cancel 端点——运行中 DELETE 明确 400，
    # 完成后 DELETE 才允许删除）
    if RESULTS[-1].get("task_id"):
        done_tid = RESULTS[-1]["task_id"]
    else:
        done_tid = None
    # 运行中任务 DELETE → 400
    st, resp = http("POST", "/async/v1/images/generations", json.dumps({
        "prompt": "a very complex scene to occupy the worker",
        "resolution": "2k"}).encode())
    run_tid = json.loads(resp)["task_id"] if st == 202 else None
    ok_run = False
    if run_tid:
        st_d, body_d = http("DELETE", f"/async/v1/images/generations/{run_tid}")
        ok_run = st_d == 400 and "still running" in body_d.decode("utf-8", "replace")
    record("async.delete running→400", ok_run, {"http": st_d})
    # 已完成任务 DELETE → 200
    if done_tid:
        st_d, body_d = http("DELETE", f"/async/v1/images/generations/{done_tid}")
        record("async.delete finished→200", st_d == 200, {"http": st_d})


GROUPS = {"basic": grp_basic, "sync": grp_sync, "native": grp_native,
          "async": grp_async}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = list(GROUPS) if which == "all" else [which]
    for name in targets:
        print(f"===== {name} =====", flush=True)
        try:
            GROUPS[name]()
        except Exception as exc:  # noqa: BLE001
            record(f"{name}.CRASHED", False, {"error": str(exc)[:150]})
        time.sleep(3)
    ok = sum(r["ok"] for r in RESULTS)
    print(f"\nFULL MATRIX [{which}]: "
          f"{'ALL PASS' if ok == len(RESULTS) else 'HAS FAILURES'} "
          f"({ok}/{len(RESULTS)})")
    tag = "_".join(targets)
    (OUT / f"results_{tag}.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
