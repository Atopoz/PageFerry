"""PDF bold style detection tests.

Run:
    pytest tests/pdf_translation/test_bold_style_detection.py
    python tests/pdf_translation/test_bold_style_detection.py
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.pdf.entities import BBox, TextSpan
from modules.pdf.extractor import (
    ParagraphExtractor,
    _get_most_common_bold_flag,
    _is_bold_font_name,
)


@dataclass
class _FakeChar:
    """提供 _FakeChar 使用的 PDF parity test 替身。"""

    fontname: str


def test_is_bold_font_name_handles_subset_prefix() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    assert _is_bold_font_name("ABCDEF+SourceSansPro-Bold")
    assert _is_bold_font_name("HSTAIJ+Arial-BoldMT")
    assert not _is_bold_font_name("DGOCJR+SourceSansPro-Regular")


def test_get_most_common_bold_flag_by_char_majority() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    chars = [
        _FakeChar("AAAAAA+SourceSansPro-Bold"),
        _FakeChar("BBBBBB+SourceSansPro-Bold"),
        _FakeChar("CCCCCC+SourceSansPro-Regular"),
    ]
    assert _get_most_common_bold_flag(chars) is True


def test_block_dominant_bold_uses_text_weight() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    bold_long = TextSpan(
        span_id=0,
        bbox=BBox(0, 0, 100, 10),
        text="Bold headline",
        is_bold=True,
    )
    regular_short = TextSpan(
        span_id=1,
        bbox=BBox(0, 0, 20, 10),
        text="x",
        is_bold=False,
    )
    assert ParagraphExtractor._resolve_block_dominant_bold([bold_long, regular_short]) is True

    regular_long = TextSpan(
        span_id=2,
        bbox=BBox(0, 0, 100, 10),
        text="Normal paragraph",
        is_bold=False,
    )
    bold_short = TextSpan(
        span_id=3,
        bbox=BBox(0, 0, 20, 10),
        text="x",
        is_bold=True,
    )
    assert ParagraphExtractor._resolve_block_dominant_bold([regular_long, bold_short]) is False


if __name__ == "__main__":
    test_is_bold_font_name_handles_subset_prefix()
    test_get_most_common_bold_flag_by_char_majority()
    test_block_dominant_bold_uses_text_weight()
