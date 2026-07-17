"""Paragraph text wrapping tests.

Run:
    pytest tests/pdf_translation/test_text_wrap.py
    python tests/pdf_translation/test_text_wrap.py
"""

from __future__ import annotations

import re
import tempfile

from modules.pdf.entities import BBox
from modules.pdf.font_manager import FontLanguage, FontSubsetData
from modules.pdf.renderer import ParagraphRenderer


def _build_test_subset(
    chars: str = "abcdefghijklmnopqrstuvwxyz ",
    language: FontLanguage = FontLanguage.ENGLISH,
) -> FontSubsetData:
    """构造这个 PDF parity test 需要的替代数据。"""
    char_to_cid = {char: index + 1 for index, char in enumerate(chars)}
    cid_to_unicode = {
        index + 1: char.encode("utf-16-be").hex().upper() for index, char in enumerate(chars)
    }
    cid_widths = {index + 1: 500 for index in range(len(chars))}
    return FontSubsetData(
        language=language,
        font_bytes=b"",
        postscript_name="TestFont",
        char_to_cid=char_to_cid,
        cid_to_unicode=cid_to_unicode,
        cid_widths=cid_widths,
        default_width=500,
        ascent=800,
        descent=-200,
        cap_height=700,
        bbox=(0, 0, 1000, 1000),
        units_per_em=1000,
        is_cff=False,
    )


def test_wrap_text_lines_fit_width() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        bbox = BBox(0, 0, 50, 100)
        lines = renderer._wrap_text_to_lines("hello world", bbox, 10.0, font_subsets)
        assert len(lines) >= 2
        for line in lines:
            width = renderer._calculate_text_width_by_language(line, font_subsets, 10.0)
            assert width <= 52.0


def test_wrap_preserves_latin_words_in_cjk_mixed_title() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    english_subset = _build_test_subset("ClaudeMythos ")
    chinese_subset = _build_test_subset("系统卡：预览 ", FontLanguage.CHINESE)  # noqa: RUF001
    font_subsets = {
        FontLanguage.ENGLISH: english_subset,
        FontLanguage.CHINESE: chinese_subset,
    }
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        bbox = BBox(0, 0, 50, 100)
        lines = renderer._wrap_text_to_lines(
            "系统卡： Claude Mythos 预览",  # noqa: RUF001
            bbox,
            10.0,
            font_subsets,
        )

    latin_runs = [run for line in lines for run in re.findall(r"[A-Za-z]+", line)]
    assert latin_runs == ["Claude", "Mythos"]
    for line in lines:
        width = renderer._calculate_text_width_by_language(line, font_subsets, 10.0)
        assert width <= 52.0


if __name__ == "__main__":
    test_wrap_text_lines_fit_width()
    test_wrap_preserves_latin_words_in_cjk_mixed_title()
