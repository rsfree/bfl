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
