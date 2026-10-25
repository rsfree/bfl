"""`size` -> 上游 aspectRatio 的映射。

历史缺陷（2026-10-25）：客户端传 `size:"3:2"` 时只按 "WxH" 解析，split 失败
→ 静默落 `auto`，横图请求回来是 1488x2731 的竖图，且没有任何报错。
两种拼法都必须认，非法值才允许 fallback 到 auto。
"""

import pytest

from bfl2openai.bridging import _COMMON_RATIOS, size_to_aspect_ratio


@pytest.mark.parametrize(
    "name,ratio",
    [
        ("3:2", "3:2"),
        ("2:3", "2:3"),
        ("16:9", "16:9"),
        ("9:16", "9:16"),
        ("7:5", "7:5"),
        ("5:7", "5:7"),
        ("5:4", "5:4"),
        ("4:5", "4:5"),
        ("21:9", "21:9"),
        ("9:21", "9:21"),
        ("1:1", "1:1"),
    ],
)
def test_ratio_spelling_passes_through(name: str, ratio: str) -> None:
    """上游枚举本身就是 "W:H"，这类输入必须原样透传。"""
    assert size_to_aspect_ratio(name) == ratio


@pytest.mark.parametrize(
    "size,ratio",
    [
        ("1536x1024", "3:2"),
        ("1024x1536", "2:3"),
        ("2512x1664", "3:2"),
        ("1024x1024", "1:1"),
        ("1280x720", "16:9"),
        ("720x1280", "9:16"),
    ],
)
def test_pixel_spelling_maps_to_nearest(size: str, ratio: str) -> None:
    assert size_to_aspect_ratio(size) == ratio


@pytest.mark.parametrize("size", [None, "", "foo", "3x", "x2", "0:0", "auto"])
def test_unparseable_falls_back_to_auto(size) -> None:
    assert size_to_aspect_ratio(size) == "auto"


def test_non_enum_ratio_snaps_to_nearest() -> None:
    # 16:11 ≈ 1.4545，靠近 3:2（1.5，差 3%），应吸附而不是 auto
    assert size_to_aspect_ratio("16:11") == "3:2"
    # 10:1 远超全表任何比例，只能 auto
    assert size_to_aspect_ratio("10:1") == "auto"


def test_every_enum_member_round_trips() -> None:
    for name, _ in _COMMON_RATIOS:
        assert size_to_aspect_ratio(name) == name


def test_whitespace_and_case_tolerated() -> None:
    assert size_to_aspect_ratio("  3:2 ") == "3:2"
