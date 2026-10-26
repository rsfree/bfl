"""转发前图片规范化：把超过上游 body 预算的参考图自动降采样。

上游 flux-tools 是 Vercel serverless，请求体上限 ≈4.5MB（实测 2026-10-14：
4MB multipart 可过，5MB 起 413 FUNCTION_PAYLOAD_TOO_LARGE）。客户端传来的
大图（相机原图、4k 截图）在自家 nginx 放行后全数撞死在这道墙外——编辑
并不需要原始分辨率，模型侧反正会重采样，降采样把"必然 413"变成"拿到图"。

策略（迭代降档，首个 ≤budget 的即返回）：
  1. 原图已 ≤budget：原样（零重编码开销）
  2. PIL 重编码 PNG，长边 2048 → 1536 → 1024 → 768
  3. 仍超：JPEG q=85 同梯度（编辑参考图对压缩不敏感）
  4. PIL 不可用或图片无法解码：原样透传（transparent degrade，让上游错误说话）
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("bfl2openai.payload")

#: 上游 multipart 总预算取 4.0MB（实测上限 4.5MB，留 multipart 开销余量）
UPSTREAM_BODY_BUDGET = 4_000_000
_LONG_EDGE_LADDER = (2048, 1536, 1024, 768)


def normalize_image(data: bytes,
                    budget: int = UPSTREAM_BODY_BUDGET) -> tuple[bytes, str]:
    """返回 (image_bytes, content_type)。降不下来的极端情况原样透传。"""
    if len(data) <= budget:
        return data, "image/png"
    try:
        from PIL import Image
    except ImportError:
        log.warning("image %.1fMB over budget but Pillow unavailable; "
                    "forwarding as-is", len(data) / 1e6)
        return data, "image/png"
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        log.warning("image %.1fMB over budget and undecodable; "
                    "forwarding as-is", len(data) / 1e6)
        return data, "image/png"
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    for long_edge in _LONG_EDGE_LADDER:
        scale = min(1.0, long_edge / max(im.size))
        if scale < 1.0:
            resized = im.resize((max(1, round(im.size[0] * scale)),
                                 max(1, round(im.size[1] * scale))),
                                Image.LANCZOS)
        else:
            resized = im
        buf = io.BytesIO()
        resized.save(buf, "PNG", optimize=True)
        out = buf.getvalue()
        if len(out) <= budget:
            log.info("normalized %.1fMB -> PNG %dx%d (%.2fMB)",
                     len(data) / 1e6, *resized.size, len(out) / 1e6)
            return out, "image/png"
    for quality in (85, 75):
        buf = io.BytesIO()
        resized.save(buf, "JPEG", quality=quality)
        out = buf.getvalue()
        if len(out) <= budget:
            log.info("normalized %.1fMB -> JPEG q%d %dx%d (%.2fMB)",
                     len(data) / 1e6, quality, *resized.size, len(out) / 1e6)
            return out, "image/jpeg"
    return out, "image/jpeg"   # 最后一级也超：交由上游裁决


def normalize_image_joint(main: bytes, refs: list[bytes]) -> tuple[bytes, list[bytes]]:
    """主图 + N 张参考图共享上游 body 预算。

    分配：主图 60%，参考图均分剩余；各图独立走降档梯子。
    预算常量整体下调（multipart 开销按 N 张线性增长，留 25% 余量）。
    """
    n = max(1, len(refs))
    total = int(UPSTREAM_BODY_BUDGET * 0.75)          # 3.0MB 实际可用
    main_budget = int(total * 0.6)
    ref_budget = max(300_000, (total - main_budget) // n)
    out_main, _ = normalize_image(main, budget=main_budget)
    out_refs = [normalize_image(r, budget=ref_budget)[0] for r in refs]
    return out_main, out_refs


def content_type_of(data: bytes) -> str:
    """按魔数判断图片类型（normalize 的 JPEG 兜底需要正确标注部件类型）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


def fit_body_budget(main: bytes, refs: list[bytes]) -> tuple[bytes, list[bytes]]:
    """主图 + 参考图压进上游 body 预算（两条提交路径共用同一套规则）。

    背景（2026-10-26）：浏览器出口路径当初直接转发原始字节，绕过了这里的
    规范化 —— 11.5MB 的图本可压到 2.76MB，实际吃上游 413。凡是要发到上游
    的字节，都必须先过这一关。

    分配与 normalize_image_joint 一致：主图 60%，参考图均分剩余 25% 余量里
    的部分；无主图时整份预算给参考图（composition 降级语义）。
    """
    refs = [r for r in refs if r]
    if not refs:
        return (normalize_image(main)[0] if main else b""), []
    if main:
        main_out, refs_out = normalize_image_joint(main, refs)
        return main_out, refs_out
    budget = max(300_000, int(UPSTREAM_BODY_BUDGET * 0.75) // max(1, len(refs)))
    return b"", [normalize_image(r, budget=budget)[0] for r in refs]
