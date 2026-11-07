"""公网原生端点端到端测试（手动运行，不进 pytest）。

用法：
    python3 bfl2openai/tests/e2e_native_public.py
    python3 bfl2openai/tests/e2e_native_public.py --base https://bfl.1task.cn --key sk-bfl

覆盖：
    鉴权矩阵      无 key / 错 key / 对 key（零消耗，不建任务）
    错误语义      空 prompt 422、未知 id 404（零消耗）
    t2i 1k        文生图
    i2i 1k        图生图（单张 base64）
    i2i 多图      两张参考图，走 references 路径
    并发          两个任务同时提交，验证互不阻塞

⚠️ 会创建真实上游任务（flux-tools 匿名免费，非计费）。默认 5 个。
   所有轮询都走裸 GET（不带任何头），和官方客户端一致。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

RESULTS: list[tuple[str, bool, str]] = []


def curl(args: list[str], timeout: int = 60) -> tuple[str, str, str]:
    r = subprocess.run(
        ["curl", "--noproxy", "*", "-sS", "-m", str(timeout),
         "-w", "\n__C__%{http_code}", *args],
        capture_output=True, text=True)
    out = r.stdout
    if "__C__" not in out:
        return "000", out, (r.stderr or "").strip()[:160]
    body, code = out.rsplit("__C__", 1)
    return code.strip(), body.strip(), (r.stderr or "").strip()[:160]


def png_info(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as im:
        size = f"{im.size[0]}x{im.size[1]}"
        im.verify()
    return size


def note(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def submit(base: str, key: str | None, payload: dict) -> tuple[str, str, str]:
    headers = ["-H", "Content-Type: application/json"]
    if key:
        headers += ["-H", f"x-key: {key}"]
    return curl(["-X", "POST", f"{base}/v1/flux-3-image", *headers,
                 "-d", json.dumps(payload)], timeout=60)


def poll(base: str, url: str, max_s: int = 180) -> dict:
    """裸 GET 轮询到终态；公网会间歇超时，所以失败不立即放弃。"""
    t0 = time.time()
    fails = 0
    last: dict = {}
    while time.time() - t0 < max_s:
        code, body, err = curl([url], timeout=25)
        try:
            last = json.loads(body)
        except Exception:
            last = {}
        st = last.get("status")
        if st in ("Ready", "Error"):
            return last
        if code == "000":
            fails += 1
            if fails >= 6:
                return {"status": "TRANSPORT_ABORT", "detail": err}
        else:
            fails = 0
        time.sleep(3)
    return last or {"status": "TIMEOUT"}


def run_case(name: str, base: str, key: str, payload: dict,
             expect_size: str | None = None) -> None:
    t0 = time.time()
    code, body, err = submit(base, key, payload)
    if code != "200":
        note(name, False, f"submit HTTP {code} {body[:120]} {err}")
        return
    doc = json.loads(body)
    url = doc.get("polling_url") or ""
    if not url.startswith("https://"):
        note(name, False, f"polling_url 不是 https: {url[:80]}")
        return
    final = poll(base, url)
    el = time.time() - t0
    if final.get("status") != "Ready":
        note(name, False, f"{final.get('status')} {str(final.get('details'))[:120]} ({el:.0f}s)")
        return
    sample = (final.get("result") or {}).get("sample")
    code2, raw, err2 = curl(["-L", "-o", "/tmp/e2e_tmp.bin", sample]) if sample else ("000", "", "")
    if code2 != "200":
        note(name, False, f"sample HTTP {code2} {err2}")
        return
    data = Path("/tmp/e2e_tmp.bin").read_bytes()
    try:
        size = png_info(data)
    except Exception as exc:  # noqa: BLE001
        note(name, False, f"样本不是有效图片: {exc}")
        return
    ok = expect_size is None or size == expect_size
    note(name, ok, f"{size} {len(data)//1024}KB {el:.0f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://bfl.1task.cn")
    ap.add_argument("--key", default="sk-bfl")
    ap.add_argument("--skip-generate", action="store_true",
                    help="只跑零消耗的鉴权/错误用例")
    a = ap.parse_args()
    base, key = a.base.rstrip("/"), a.key

    # ---------- 鉴权矩阵（零消耗） ----------
    code, body, _ = curl(["-X", "POST", f"{base}/v1/flux-3-image",
                          "-H", "Content-Type: application/json", "-d", "{}"])
    note("鉴权: 无 x-key -> 401", code == "401", f"HTTP {code}")
    code, body, _ = curl(["-X", "POST", f"{base}/v1/flux-3-image",
                          "-H", "Content-Type: application/json",
                          "-H", "x-key: wrong", "-d", "{}"])
    note("鉴权: 错 x-key -> 401", code == "401", f"HTTP {code}")
    code, body, _ = curl(["-X", "POST", f"{base}/v1/flux-3-image",
                          "-H", "Content-Type: application/json",
                          "-H", f"x-key: {key}", "-d", "{}"])
    note("鉴权: 对 x-key -> 422(参数)", code == "422", f"HTTP {code}")

    # ---------- 错误语义（零消耗） ----------
    code, body, _ = curl(["-X", "POST", f"{base}/v1/flux-3-image",
                          "-H", "Content-Type: application/json",
                          "-H", f"x-key: {key}",
                          "-d", '{"prompt":"   "}'])
    note("参数: 空白 prompt -> 422", code == "422", f"HTTP {code}")
    code, body, _ = curl([f"{base}/v1/get_result?id=deadbeefdeadbeef"])
    ok = code == "404" or (code == "200" and '"status":"Error"' in body)
    note("轮询: 未知 id -> 404/Error", ok, f"HTTP {code}")
    code, body, _ = curl(["https://bfl.1task.cn/healthz"]) if base.endswith("1task.cn") else ("000", "", "")
    if code == "200":
        try:
            hz = json.loads(body)
            probe = hz.get("probe") or {}
            note("healthz: 含 probe 摘要", "probe" in hz,
                 json.dumps(probe, ensure_ascii=False)[:120])
        except Exception:
            pass

    if a.skip_generate:
        return summary()

    # ---------- 造两张纯色参考图 ----------
    def _png(color: tuple[int, int, int], size=(512, 512)) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, "PNG")
        return buf.getvalue()

    red = base64.b64encode(_png((200, 30, 30))).decode()
    blue = base64.b64encode(_png((30, 60, 200))).decode()

    # ---------- 串行：t2i 1k / i2i 1k / i2i 多图 ----------
    run_case("t2i 1k", base, key,
             {"prompt": "a single red apple on a white table, studio light",
              "resolution": "1k", "aspect_ratio": "1:1"}, "1024x1024")
    run_case("i2i 1k", base, key,
             {"prompt": "a single red apple on a white table, studio light",
              "images": [f"data:image/png;base64,{red}"],
              "resolution": "1k", "aspect_ratio": "1:1"}, "1024x1024")
    run_case("i2i 多图 (2 张参考)", base, key,
             {"prompt": "a red apple beside a blue mug on a white table",
              "images": [f"data:image/png;base64,{red}",
                         f"data:image/png;base64,{blue}"],
              "resolution": "1k", "aspect_ratio": "1:1"}, "1024x1024")

    # ---------- 并发：两个 768sq 同时提交 ----------
    def _job(spec):
        run_case(spec[0], base, key, spec[1])

    specs = [
        ("并发 t2i 768sq", {"prompt": "a gray cube on white",
                            "resolution": "768sq", "aspect_ratio": "1:1"}),
        ("并发 i2i 768sq", {"prompt": "a gray cube on white",
                            "images": [f"data:image/png;base64,{red}"],
                            "resolution": "768sq", "aspect_ratio": "1:1"}),
    ]
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(_job, specs))

    return summary()


def summary() -> int:
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== {passed}/{total} passed ===")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
