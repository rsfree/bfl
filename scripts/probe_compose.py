"""composition 分支探针：一次跑完 4 种请求形状，打印走的是哪条分支。

回答的问题：「这个请求到底进了 i2i（参考图生效）还是降级成 t2i（参考图白给）」。

用法：

    # 本地
    .venv/Scripts/python scripts/probe_compose.py --base http://127.0.0.1:8798 --key sk-local

    # 生产（建议先用小图、别带 4k）
    python scripts/probe_compose.py --base https://bfl.1task.cn --key sk-xxx

判定看响应头，不用翻日志：

    X-Bfl-Branch        t2i_plain | i2i_canvas | compose_canvas_refs | ...
    X-Bfl-Mode          i2i->t2i 就是发生了降级
    X-Bfl-Downgraded    1 = 参考图这条路上被降成文生图
    X-Bfl-Refs          in=N,out=M，数量对不上说明中途丢图

参考图用内存里现生成的两张（纯色 + 圆），不依赖外网，也不需要准备素材。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import urllib.error
import urllib.request
import uuid

from PIL import Image, ImageDraw


def _ref_png(color, shape, size=512) -> bytes:
    im = Image.new("RGB", (size, size), color)
    ImageDraw.Draw(im).ellipse(
        [size // 4, size // 4, size * 3 // 4, size * 3 // 4], fill=shape)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _post_json(base: str, path: str, body: dict, key: str, timeout: float):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    return _send(req, timeout)


def _post_multipart(base: str, path: str, fields: dict, files: list[tuple[str, bytes, str]],
                    key: str, timeout: float):
    boundary = "----probe" + uuid.uuid4().hex
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(str(value).encode() + b"\r\n")
    for name, data, filename in files:
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
        body.write(b"Content-Type: image/png\r\n\r\n")
        body.write(data + b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    return _send(req, timeout)


def _send(req, timeout: float):
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        code, headers, data = r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        code, headers, data = exc.code, exc.headers, exc.read()
    except Exception as exc:  # noqa: BLE001
        return "EXC", {}, f"{type(exc).__name__}: {exc}".encode()
    return code, headers, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8798")
    ap.add_argument("--key", default="sk-local")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--resolution", default="", help="如 768sq；留空用服务端默认")
    args = ap.parse_args()

    a = _ref_png((220, 120, 60), (30, 80, 200))
    b = _ref_png((40, 160, 90), (230, 230, 40))
    extra = {"resolution": args.resolution} if args.resolution else {}

    cases = [
        ("1 纯文生图（无图）", lambda: _post_json(
            args.base, "/v1/images/generations",
            {"model": "flux-3-image", "prompt": "a red fox in snow", **extra},
            args.key, args.timeout)),
        ("2 单图 generations", lambda: _post_json(
            args.base, "/v1/images/generations",
            {"model": "flux-3-image", "prompt": "把圆圈移到左上角",
             "image": [_data_uri(a)], **extra},
            args.key, args.timeout)),
        ("3 双图 generations", lambda: _post_json(
            args.base, "/v1/images/generations",
            {"model": "flux-3-image", "prompt": "把第一张的圆圈放到第二张的方块上",
             "image": [_data_uri(a), _data_uri(b)], **extra},
            args.key, args.timeout)),
        ("4 edits multipart 单图", lambda: _post_multipart(
            args.base, "/v1/images/edits",
            {"prompt": "把圆圈移到左上角", **extra},
            [("image", a, "ref.png")], args.key, args.timeout)),
        ("5 edits multipart 双图", lambda: _post_multipart(
            args.base, "/v1/images/edits",
            {"prompt": "把第一张的圆圈放到第二张的方块上", **extra},
            [("image", a, "ref.png"), ("image", b, "ref2.png")],
            args.key, args.timeout)),
    ]

    print(f"{'case':<24} {'http':<5} {'branch':<28} {'mode':<12} {'refs':<12} downgraded")
    print("-" * 96)
    for label, fn in cases:
        code, headers, data = fn()
        print(f"{label:<24} {str(code):<5} "
              f"{str(headers.get('X-Bfl-Branch')):<28} "
              f"{str(headers.get('X-Bfl-Mode')):<12} "
              f"{str(headers.get('X-Bfl-Refs')):<12} "
              f"{headers.get('X-Bfl-Downgraded')}")
        if code != 200:
            print("     body:", data[:200].decode("utf-8", "replace"))

    # 分支计数（多 worker 时只是当前 worker 的）
    code, _, data = _send(urllib.request.Request(
        args.base.rstrip("/") + "/v1/_compose",
        headers={"Authorization": f"Bearer {args.key}"}), 20)
    print()
    if code == 200:
        stats = json.loads(data)
        print("分支计数:", json.dumps(stats["by_branch"], ensure_ascii=False),
              " 降级占比:", stats["downgraded_pct"], "%")
    else:
        print("/v1/_compose ->", code, data[:120].decode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
