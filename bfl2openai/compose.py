"""Composition 归一化 + 降级埋点（单一事实源）。

上游契约（2026-10-26 实测，Zod 报错暴露枚举）：mode 只有 't2i' | 'i2i'，且
canvas（`image` 文件部件）与 references 互斥：

    i2i + canvas + refs    -> 400 "A composition must not include a canvas image."
    i2i + refs（无 canvas） -> 400 "An image is required for an edit."
    t2i + refs（无 canvas） -> 200 正常出图（1536x1536，带 C2PA）

历史轨迹（git 可查）：

    2026-10-25 d5843af  多图走 i2i + canvas + refs，**实测生效**
                        （鸭子进杯、红方块出现，见当时 README 记录）
    2026-10-26 b442808  改成 canvas 并入 references 首位 + mode=t2i
    至今               沿用 b442808 的形状

b442808 的验证标准只有「返回 200 + 1536x1536 + C2PA 签名」——从未验证参考图
内容是否被消费。于是出现本次客户反馈的现象：HTTP 200，但输出与参考图无关。

本模块把归一化收敛到一处，并为「这次请求到底走了哪条分支」埋点，输出三样东西：

    1. 结构化日志  compose branch=compose_canvas_refs mode=i2i->t2i ...
    2. 响应头      X-Bfl-Branch / X-Bfl-Mode / X-Bfl-Refs / X-Bfl-Downgraded
    3. 进程计数    GET /v1/_compose（按分支统计命中次数）

日志在调用上游**之前**打，所以上游 429/502 时也能确认分支——这正是排查
「静默降级」最需要的信息。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("bfl2openai")

# 带参考图时的目标形状。空 = 现行行为（canvas 并入 refs + 强制 t2i）。
# 置为 "i2i" 可切回 2026-10-25 那套 i2i + canvas + refs 形状做 A/B 对照：
#   BFL2OPENAI_MULTI_IMAGE_MODE=i2i uvicorn bfl2openai.server:app
MULTI_IMAGE_MODE = (os.environ.get("BFL2OPENAI_MULTI_IMAGE_MODE") or "").strip().lower()

BRANCH_T2I = "t2i_plain"                  # 无图无垫图：本来就是文生图
BRANCH_I2I = "i2i_canvas"                 # 单图画布：正常编辑，参考图生效
BRANCH_COMPOSE_CANVAS = "compose_canvas_refs"       # canvas 并入 refs，mode->t2i（2026-11-16 实测参考图仍生效）
BRANCH_COMPOSE_KEEP = "compose_canvas_refs_keep"    # 对照：canvas 留在 image 部件，mode 保持 i2i
BRANCH_COMPOSE_REFS_ONLY = "compose_refs_only"      # 只有垫图无画布
ALL_BRANCHES = (BRANCH_T2I, BRANCH_I2I, BRANCH_COMPOSE_CANVAS,
                BRANCH_COMPOSE_KEEP, BRANCH_COMPOSE_REFS_ONLY)


@dataclass(frozen=True)
class Plan:
    """一次请求的 composition 决策结果（归一化后的三元组 + 决策依据）。"""

    branch: str
    mode_in: str
    mode_out: str
    canvas_in: bool
    canvas_bytes: int
    canvas_out: bool
    refs_in: int
    refs_out: int
    image_png: bytes | None
    reference_images: tuple[tuple[bytes, str], ...]

    @property
    def downgraded(self) -> bool:
        """形态变更：本来是编辑(i2i)，发出去是 composition(t2i)。

        2026-11-16 修复参考图字段名（`reference` 单数 + `references` 名字数组）
        之前，这个标记等于「参考图失效」；修复后 t2i+refs 与 i2i+canvas+refs
        都能消费参考图，该标记只描述形态，不再代表能力降级。
        """
        return self.mode_in == "i2i" and self.mode_out == "t2i"

    @property
    def is_compose(self) -> bool:
        return self.branch in (BRANCH_COMPOSE_CANVAS, BRANCH_COMPOSE_KEEP,
                               BRANCH_COMPOSE_REFS_ONLY)

    def headers(self) -> dict[str, str]:
        """写进响应的埋点头。客户端不看日志也能判断走了哪条分支。"""
        return {
            "X-Bfl-Branch": self.branch,
            "X-Bfl-Mode": f"{self.mode_in}->{self.mode_out}",
            "X-Bfl-Refs": f"in={self.refs_in},out={self.refs_out}",
            "X-Bfl-Downgraded": "1" if self.downgraded else "0",
        }

    def log_fields(self) -> str:
        canvas = f"{self.canvas_bytes}B" if self.canvas_in else "none"
        if self.canvas_in and not self.canvas_out:
            canvas += "->merged"
        return (
            f"branch={self.branch} mode={self.mode_in}->{self.mode_out} "
            f"canvas={canvas} refs={self.refs_in}->{self.refs_out} "
            f"downgraded={int(self.downgraded)}"
        )


# ------------------------------------------------------------------ 计数器
# 进程内计数（gunicorn 多 worker 时各算各的；日志才是全量事实源）。
_LOCK = threading.Lock()
_STATS: dict = {
    "since": time.time(),
    "pid": os.getpid(),
    "total": 0,
    "by_branch": {b: 0 for b in ALL_BRANCHES},
    "downgraded": 0,
    "compose": 0,
    "last": None,
    "last_ts": None,
}


def plan(
    image_png: bytes | None,
    reference_images: list[tuple[bytes, str]] | None,
    mode: str,
    note: str = "",
) -> Plan:
    """归一化 (canvas, refs, mode) 并落埋点。行为与 b442808 之后完全一致。"""
    refs = list(reference_images or [])
    n_refs = len(refs)
    canvas_in = bool(image_png)
    canvas_bytes = len(image_png or b"")

    out_png: bytes | None = image_png if canvas_in else None
    out_refs: list[tuple[bytes, str]] = refs
    mode_out = mode

    if n_refs == 0:
        branch = BRANCH_I2I if canvas_in else BRANCH_T2I
    elif canvas_in and MULTI_IMAGE_MODE == "i2i":
        # 对照形状（2026-10-25 d5843af 实测生效的那套）：canvas 仍走 `image`
        # 部件，refs 单独走 `references`，mode 保持 i2i。b442808 之后被弃用，
        # 这里用环境变量切回来做真上游 A/B，不改变默认行为。
        branch = BRANCH_COMPOSE_KEEP
        mode_out = "i2i"
    elif canvas_in:
        # 降级分支：canvas 挪进 references 首位（上游前端 prompt 也是
        # "Edit the first reference image."），mode 置 t2i。
        branch = BRANCH_COMPOSE_CANVAS
        out_refs = [(image_png, "canvas.png"), *refs]  # type: ignore[list-item]
        out_png = None
        mode_out = "t2i"
    else:
        # 只有垫图没有画布：i2i 会被上游以 "An image is required for an edit."
        # 拒绝，只能走 t2i + refs。
        branch = BRANCH_COMPOSE_REFS_ONLY
        mode_out = "t2i"

    p = Plan(
        branch=branch,
        mode_in=mode,
        mode_out=mode_out,
        canvas_in=canvas_in,
        canvas_bytes=canvas_bytes,
        canvas_out=out_png is not None,
        refs_in=n_refs,
        refs_out=len(out_refs),
        image_png=out_png,
        reference_images=tuple(out_refs),
    )
    _record(p, note)
    return p


def _record(p: Plan, note: str) -> None:
    with _LOCK:
        _STATS["total"] += 1
        _STATS["by_branch"][p.branch] = _STATS["by_branch"].get(p.branch, 0) + 1
        if p.downgraded:
            _STATS["downgraded"] += 1
        if p.is_compose:
            _STATS["compose"] += 1
        _STATS["last"] = p.log_fields()
        _STATS["last_ts"] = time.time()
    suffix = f" note={note}" if note else ""
    level = log.warning if p.downgraded else log.info
    level("compose %s%s", p.log_fields(), suffix)


def snapshot() -> dict:
    """给 /v1/_compose 用：各分支命中次数 + 降级占比。"""
    with _LOCK:
        total = _STATS["total"]
        by_branch = dict(_STATS["by_branch"])
        out = {
            "pid": _STATS["pid"],
            "since": _STATS["since"],
            "uptime_s": round(time.time() - _STATS["since"], 1),
            "total": total,
            "by_branch": by_branch,
            "compose": _STATS["compose"],
            "downgraded": _STATS["downgraded"],
            "downgraded_pct": round(100.0 * _STATS["downgraded"] / total, 2) if total else 0.0,
            "multi_image_mode": MULTI_IMAGE_MODE or "t2i",
            "last": _STATS["last"],
            "last_ts": _STATS["last_ts"],
        }
    return out


def reset() -> None:
    """测试用：清空计数。"""
    with _LOCK:
        _STATS["since"] = time.time()
        _STATS["total"] = 0
        _STATS["by_branch"] = {b: 0 for b in ALL_BRANCHES}
        _STATS["downgraded"] = 0
        _STATS["compose"] = 0
        _STATS["last"] = None
        _STATS["last_ts"] = None
