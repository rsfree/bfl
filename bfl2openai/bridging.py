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

_COMMON_RATIOS = [
    ("1:1", 1.0),
    ("3:2", 1.5),
    ("2:3", 2 / 3),
    ("4:3", 4 / 3),
    ("3:4", 0.75),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("21:9", 21 / 9),
    ("9:21", 9 / 21),
]


def size_to_aspect_ratio(size: str | None) -> str:
    """Map an OpenAI `size` ("1024x1024") to the closest upstream aspect ratio;
    fall back to "auto"."""
    if not size:
        return "auto"
    try:
        sw, sh = size.lower().split("x")
        ratio = float(sw) / float(sh)
    except Exception:
        return "auto"
    if abs(ratio - 1.0) < 0.02:
        return "1:1"
    best = min(_COMMON_RATIOS, key=lambda kv: abs(kv[1] - ratio))
    if abs(best[1] - ratio) / best[1] < 0.12:
        return best[0]
    return "auto"


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

#: 上游各分辨率档接受的比例是**固定尺寸表**，不是连续缩放（实测 2026-10-09）：
#: 1k: 1:1→1024²、3:2→1248×832、2:3→832×1248 均可行；16:9 不在 1k 表 —— 提交成功
#: 但 pollToken 立即作废（"Invalid or expired image result token"）；16:9@2k 实测可行。
#: 策略：1k 档只放行实测安全比例，其余升 2k（把"静默作废"变成"拿到图"）。
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
