"""Helpers to bridge OpenAI images semantics onto the flux-tools precise-editing API."""

from __future__ import annotations

import io
import json
from typing import Any

from PIL import Image

from .upstream import new_box_id

# ---------- mask -> boxes ----------


def mask_to_boxes(mask_png: bytes, min_fraction: float = 0.005) -> list[dict[str, Any]]:
    """Convert an OpenAI-style mask (transparent pixels = editable area) into
    normalized bounding boxes: [{id, box{x,y,w,h}, source:"mask"}].

    Connected-component labeling via BFS on downscaled alpha to stay fast.
    """
    img = Image.open(io.BytesIO(mask_png)).convert("RGBA")
    # downscale for speed, keep aspect
    scale = 512 / max(img.size)
    if scale < 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    w, h = img.size
    alpha = img.getchannel("A").load()

    editable = [[alpha[x, y] < 128 for x in range(w)] for y in range(h)]  # transparent=editable

    seen = [[False] * w for _ in range(h)]
    boxes: list[dict[str, Any]] = []
    for y0 in range(h):
        for x0 in range(w):
            if not editable[y0][x0] or seen[y0][x0]:
                continue
            # BFS
            queue = [(x0, y0)]
            seen[y0][x0] = True
            minx = maxx = x0
            miny = maxy = y0
            count = 0
            while queue:
                x, y = queue.pop()
                count += 1
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and editable[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if count / (w * h) < min_fraction:
                continue
            boxes.append(
                {
                    "id": new_box_id(),
                    "box": {
                        "x": round(minx / w, 4),
                        "y": round(miny / h, 4),
                        "w": round((maxx - minx + 1) / w, 4),
                        "h": round((maxy - miny + 1) / h, 4),
                    },
                    "source": "mask",
                }
            )
    return boxes


# ---------- size / aspect ratio ----------

#: 官方全表 15 种（analysis/bfl_openapi.json · Flux3ImageInputs.aspect_ratio enum，
#: 21:9 到 9:21）—— size 落表用全表；1k 档 Playground 匿名可用子集另见
#: ensure_resolution_capacity 的安全表。
_COMMON_RATIOS = [
    ("21:9", 21 / 9),
    ("2:1", 2.0),
    ("16:9", 16 / 9),
    ("3:2", 1.5),
    ("7:5", 1.4),
    ("4:3", 4 / 3),
    ("5:4", 1.25),
    ("1:1", 1.0),
    ("4:5", 0.8),
    ("3:4", 0.75),
    ("5:7", 5 / 7),
    ("2:3", 2 / 3),
    ("9:16", 9 / 16),
    ("1:2", 0.5),
    ("9:21", 9 / 21),
    ("9:21", 9 / 21),
]


#: 上游 aspectRatio 的合法枚举值（"3:2" 这种写法本身就是上游契约）。
_RATIO_NAMES = {name for name, _ in _COMMON_RATIOS}


def _nearest_ratio(ratio: float) -> str:
    """最接近的合法枚举；偏离超过 12% 视为不可映射（交回 auto）。"""
    best = min(_COMMON_RATIOS, key=lambda kv: abs(kv[1] - ratio))
    if abs(best[1] - ratio) / best[1] < 0.12:
        return best[0]
    return "auto"


#: 客户端实际出现过的分隔符：ASCII x、Unicode ×、乘号 *、比例冒号。
#: 统一成一个入口解析，避免某一种写法静默落到 auto。
_SIZE_SEPARATORS = ("x", "×", "*", ":")


def _split_size(token: str) -> tuple[float, float] | None:
    """把 "1536x1024" / "1536×1024" / "1536*1024" / "3:2" 拆成两个正数。"""
    for sep in _SIZE_SEPARATORS:
        if sep in token:
            left, _, right = token.partition(sep)
            try:
                a, b = float(left.strip()), float(right.strip())
            except Exception:
                return None
            if a > 0 and b > 0:
                return a, b
            return None
    return None


def parse_size_dims(size: str | None) -> tuple[int, int] | None:
    """像素写法的宽高；比例写法/非法值返回 None。

    与 size_to_aspect_ratio 共用同一套分隔符，否则 "1536×1024" 这类写法
    会「比例对了、长边没解析到」，尺寸相关逻辑（超 2k 升采样）静默失效。
    """
    if not size:
        return None
    token = str(size).strip().lower()
    # 只有 "WxH" 一族算像素；"3:2" 是比例，别把长边当成 3。
    if ":" in token and not any(sep in token for sep in ("x", "×", "*")):
        return None
    dims = _split_size(token)
    if not dims:
        return None
    a, b = dims
    if a != int(a) or b != int(b):
        return None
    return int(a), int(b)


def size_to_aspect_ratio(size: str | None) -> str:
    """Map a client `size` to an upstream aspect ratio; fall back to "auto".

    Accepts both spellings, because callers use both and one of them used to
    fail silently (2026-10-25: `size:"3:2"` fell through to "auto", so a
    landscape request came back portrait at 1488x2731 with no error):

      * "1536x1024"       -- OpenAI's pixel form, matched to the nearest enum
      * "1536×1024" / "*" -- the same thing pasted from a UI
      * "3:2"             -- the ratio form, upstream's own enum spelling
    """
    if not size:
        return "auto"
    token = str(size).strip().lower()
    if ":" in token and token in _RATIO_NAMES:
        return token
    dims = _split_size(token)
    if not dims:
        return "auto"
    a, b = dims
    ratio = a / b
    if abs(ratio - 1.0) < 0.02:
        return "1:1"
    return _nearest_ratio(ratio)


# ---------- boxes param ----------


def parse_boxes(raw: str | None) -> list[dict[str, Any]]:
    """Parse and validate the extension `boxes` param (same JSON shape the
    official playground sends)."""
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("boxes must be a JSON array")
    out: list[dict[str, Any]] = []
    for item in data:
        b = item.get("box") or {}
        box = {
            "x": max(0.0, min(1.0, float(b.get("x", 0)))),
            "y": max(0.0, min(1.0, float(b.get("y", 0)))),
            "w": max(0.0, min(1.0, float(b.get("w", 1)))),
            "h": max(0.0, min(1.0, float(b.get("h", 1)))),
        }
        out.append(
            {
                "id": item.get("id") or new_box_id(),
                "box": box,
                "instruction": str(item.get("instruction", "")),
                "source": item.get("source", "user"),
            }
        )
    return out


def image_to_png(data: bytes) -> bytes:
    """Normalize any uploaded raster (jpeg/webp/png) to PNG, as the upstream
    form field expects an image file (site always sends image/png)."""
    img = Image.open(io.BytesIO(data))
    if img.format == "PNG":
        return data
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    return buf.getvalue()


# ---------- 分辨率×比例 有效域防御 ----------

#: Playground **匿名端点**的 1k 档实测只放行方形系比例（16:9 提交成功但 pollToken
#: 即时作废；官方 API 文档的等像素面积档语义下 16:9@1k≈1365×768 本应合法 ——
#: 即限制在 Playground 免费层，不在模型）。策略：匿名链路 1k 只放行实测安全比例，
#: 其余升 2k（16:9@2k=1360×768 实测可行），把"静默作废"变成"拿到图"。
_SAFE_1K_RATIOS = {"1:1", "3:2", "2:3", "4:3", "3:4"}


def ensure_resolution_capacity(aspect_ratio: str, resolution: str) -> str:
    """1k 档遇到表外比例（宽银幕等）自动升 2k，避免上游静默作废整单。

    仅对显式 W:H 判定；"auto"（画布派生，全天实测正常）与无法解析的原样返回。
    """
    if resolution != "1k":
        return resolution
    ratio = aspect_ratio.lower().strip()
    if ratio in _SAFE_1K_RATIOS:
        return "1k"
    try:
        w_str, h_str = ratio.split(":")
        float(w_str), float(h_str)   # 是 W:H 且不在安全表 ⇒ 表外比例，升档
    except Exception:
        return "1k"                  # "auto"/无法解析：不连坐
    return "2k"


def tier_for_long_edge(px: int | None) -> str | None:
    """像素长边 -> 分辨率档位；只覆盖 1k/1.5k/2k，更大的走升采样复合（返回 None）。

    OpenAI 客户端按 `size` 预期输出像素，而上游的输出尺寸由分辨率档位决定：
    不做这个映射，"1024x1024" 会在默认 1.5k 档拿到 1536^2（实测矩阵抓到）。
    显式 `resolution` 永远优先，这个函数只在调用方没给档位时兜底。
    """
    if not px:
        return None
    if px <= 1024:
        return "1k"
    if px <= 1536:
        return "1.5k"
    if px <= 2048:
        return "2k"
    return None
