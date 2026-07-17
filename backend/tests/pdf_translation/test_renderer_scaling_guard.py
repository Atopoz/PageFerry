"""Paragraph renderer inline scaling guard tests.

Run:
    pytest tests/pdf_translation/test_renderer_scaling_guard.py
    python tests/pdf_translation/test_renderer_scaling_guard.py
"""

from __future__ import annotations

import re
import tempfile

import pikepdf

from modules.pdf.entities import BBox, TextBlock, TextSpan
from modules.pdf.font_manager import FontLanguage, FontSubsetData
from modules.pdf.renderer import FillOptions, ParagraphRenderer
from modules.pdf.table_renderer import TableRenderConfig, _CellText


def _build_test_subset() -> FontSubsetData:
    """构造这个 PDF parity test 需要的替代数据。"""
    chars = "abcdefghijklmnopqrstuvwxyz ()"
    char_to_cid = {char: index + 1 for index, char in enumerate(chars)}
    cid_to_unicode = {
        index + 1: char.encode("utf-16-be").hex().upper() for index, char in enumerate(chars)
    }
    cid_widths = {index + 1: 500 for index in range(len(chars))}
    return FontSubsetData(
        language=FontLanguage.ENGLISH,
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


def _build_chinese_test_subset() -> FontSubsetData:
    """构造这个 PDF parity test 需要的替代数据。"""
    chars = "物料清单中活跃的颜色代码数量"
    char_to_cid = {char: index + 1 for index, char in enumerate(chars)}
    cid_to_unicode = {
        index + 1: char.encode("utf-16-be").hex().upper() for index, char in enumerate(chars)
    }
    cid_widths = {index + 1: 1000 for index in range(len(chars))}
    return FontSubsetData(
        language=FontLanguage.CHINESE,
        font_bytes=b"",
        postscript_name="TestCJKFont",
        char_to_cid=char_to_cid,
        cid_to_unicode=cid_to_unicode,
        cid_widths=cid_widths,
        default_width=1000,
        ascent=800,
        descent=-200,
        cap_height=700,
        bbox=(0, 0, 1000, 1000),
        units_per_em=1000,
        is_cff=False,
    )


def test_translation_only_disables_horizontal_scaling_by_default() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 20, 10),
            text="ppt",
            translated_text="ppt skills for docs and slides",
            font_size=9.0,
            source_textbox_bbox=BBox(0, 0, 20, 10),
        )
        options = FillOptions(
            min_translation_font_size=6.0,
            min_horizontal_scaling=80.0,
        )
        commands = renderer._build_translation_only_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            options,
        )

    assert commands
    command = commands[0]
    assert " Tz" not in command
    assert "/FTR_EN 6.00 Tf" in command


def test_translation_only_keeps_original_size_when_text_fits() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 120, 10),
            text="ppt",
            translated_text="ppt",
            font_size=9.0,
            source_textbox_bbox=BBox(0, 0, 120, 10),
        )
        options = FillOptions(
            min_translation_font_size=6.0,
            min_horizontal_scaling=80.0,
        )
        commands = renderer._build_translation_only_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            options,
        )

    assert commands
    command = commands[0]
    assert " Tz" not in command
    assert "/FTR_EN 9.00 Tf" in command


def test_translation_only_can_enable_horizontal_scaling_explicitly() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 20, 10),
            text="ppt",
            translated_text="ppt skills for docs and slides",
            font_size=9.0,
            source_textbox_bbox=BBox(0, 0, 20, 10),
        )
        options = FillOptions(
            min_translation_font_size=6.0,
            allow_horizontal_scaling=True,
            min_horizontal_scaling=80.0,
        )
        commands = renderer._build_translation_only_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            options,
        )

    assert commands
    command = commands[0]
    assert "80.0 Tz" in command
    assert "/FTR_EN 6.00 Tf" in command


def test_translation_only_table_cell_fit_avoids_horizontal_scaling() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 20, 10),
            text="ppt",
            translated_text="ppt skills for docs and slides",
            font_size=9.0,
            source_textbox_bbox=BBox(0, 0, 200, 10),
            table_cell_bbox=BBox(0, 0, 20, 10),
        )
        options = FillOptions(
            min_translation_font_size=6.0,
            min_horizontal_scaling=80.0,
            table_min_translation_font_size=4.0,
        )
        commands = renderer._build_translation_only_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            options,
            table_cell_fit=True,
        )

    assert commands
    command = commands[0]
    assert " Tz" not in command
    font_match = re.search(r"/FTR_EN ([0-9.]+) Tf", command)
    assert font_match is not None
    assert float(font_match.group(1)) >= options.table_min_translation_font_size * 0.75


def test_table_cell_fit_uses_original_font_as_base_when_adjusted_font_is_tiny() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 20, 10),
            text="ppt",
            translated_text="ppt skills",
            font_size=9.0,
            adjusted_font_size=4.0,
            source_textbox_bbox=BBox(0, 0, 200, 10),
            table_cell_bbox=BBox(0, 0, 80, 10),
        )
        commands = renderer._build_translation_only_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
            table_cell_fit=True,
        )

    assert commands
    command = commands[0]
    assert "/FTR_EN 9.00 Tf" in command


def test_table_cell_group_combines_multi_span_cell_translation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        cell_bbox = BBox(0, 0, 80, 24)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(0, 12, 70, 22),
                text="a",
                translated_text="ppt skills for docs",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(0, 1, 70, 11),
                text="b",
                translated_text="and slides",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=cell_bbox,
            text="ab",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert commands
    assert grouped_ids == {id(spans[0]), id(spans[1])}


def test_table_cell_group_wraps_with_shared_font_size() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        first_bbox = BBox(0, 0, 100, 28)
        second_bbox = BBox(110, 0, 210, 28)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(0, 14, 40, 24),
                text="a",
                translated_text="short",
                font_size=10.0,
                table_cell_bbox=first_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(0, 2, 40, 12),
                text="b",
                translated_text="item",
                font_size=10.0,
                table_cell_bbox=first_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=2,
                bbox=BBox(110, 14, 190, 24),
                text="c",
                translated_text="ppt skills for docs",
                font_size=10.0,
                table_cell_bbox=second_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=3,
                bbox=BBox(110, 2, 190, 12),
                text="d",
                translated_text="and slides",
                font_size=10.0,
                table_cell_bbox=second_bbox,
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 210, 28),
            text="abcd",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert commands
    assert grouped_ids == {id(span) for span in spans}
    assert all(" Tz" not in command for command in commands)
    font_sizes = {
        float(match.group(1))
        for command in commands
        for match in re.finditer(r"/FTR_EN ([0-9.]+) Tf", command)
    }
    assert font_sizes == {10.0}


def test_table_cell_layout_preserves_latin_word_boundaries_when_shrinking() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        table_renderer = renderer._build_table_text_renderer()
        cell = _CellText(
            bbox=BBox(0, 0, 36, 24),
            spans=(),
            text="commitment background",
            base_font_size=10.0,
        )
        assert (
            table_renderer._layout_cell_text(
                cell.text,
                cell.bbox,
                10.0,
                font_subsets,
                TableRenderConfig(line_height_factor=1.1, min_font_size=4.0),
            )
            is None
        )
        layout = table_renderer._select_cell_layout(
            cell,
            10.0,
            font_subsets,
            TableRenderConfig(line_height_factor=1.1, min_font_size=4.0),
        )

    assert layout is not None
    assert layout.lines == ("commitment", "background")
    assert layout.font_size < 10.0


def test_table_cell_group_uses_source_siblings_after_plain_span_merge() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_chinese_test_subset()
    font_subsets = {FontLanguage.CHINESE: subset}
    font_resource_names = {FontLanguage.CHINESE: pikepdf.Name("/FTR_ZH")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        cell_bbox = BBox(0, 0, 60, 48)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(18, 36, 58, 45),
                text="Active CC",
                translated_text="物料清单中活跃的颜色代码数量",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(20, 22, 58, 31),
                text="Count on",
                translated_text="",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=2,
                bbox=BBox(36, 8, 58, 17),
                text="BOM",
                translated_text="",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=cell_bbox,
            text="Active CC Count on BOM",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert len(commands) >= 2
    assert grouped_ids == {id(spans[0])}
    assert all(" Tz" not in command for command in commands)
    assert all("/FTR_ZH 9.00 Tf" in command for command in commands)


def test_table_cell_group_wraps_single_span_only_with_extra_cell_room() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        cell_bbox = BBox(0, 0, 80, 30)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 10, 20, 20),
            text="ppt",
            translated_text="ppt skills for docs and slides",
            font_size=9.0,
            table_cell_bbox=cell_bbox,
            layout_label="table",
        )
        block = TextBlock(
            block_id=0,
            bbox=cell_bbox,
            text="ppt",
            spans=[span],
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            [span],
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert len(commands) >= 2
    assert grouped_ids == {id(span)}
    assert all(" Tz" not in command for command in commands)


def test_table_cell_group_skips_overlapping_single_span_cells() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(0, 10, 20, 20),
                text="ppt",
                translated_text="ppt skills for docs and slides",
                font_size=9.0,
                table_cell_bbox=BBox(0, 0, 80, 30),
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(5, 10, 25, 20),
                text="pdf",
                translated_text="pdf skills for docs and slides",
                font_size=9.0,
                table_cell_bbox=BBox(5, 0, 85, 30),
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 85, 30),
            text="ppt pdf",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert commands == []
    assert grouped_ids == set()


def test_table_cell_group_skips_single_span_cell_with_inner_sibling_span() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        cell_bbox = BBox(0, 0, 90, 30)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(0, 12, 80, 24),
                text="Cash received",
                translated_text="Cash received from sales of goods and rendering of",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(0, 2, 30, 10),
                text="services",
                translated_text="services",
                font_size=9.0,
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=cell_bbox,
            text="Cash received services",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert commands == []
    assert grouped_ids == set()


def test_table_cell_group_skips_over_tall_cell_bbox() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        cell_bbox = BBox(15, 35, 776, 402)
        spans = [
            TextSpan(
                span_id=0,
                bbox=BBox(19, 388, 95, 397),
                text="Deborah McMullen",
                translated_text="Deborah McMullen",
                font_size=9.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(19, 370, 468, 383),
                text="1. Please help",
                translated_text="1. Please help to add back POM P47 Pocket Bag Width",
                font_size=13.0,
                table_cell_bbox=cell_bbox,
                layout_label="table",
            ),
        ]
        block = TextBlock(
            block_id=0,
            bbox=BBox(19, 350, 675, 397),
            text="comment",
            spans=spans,
            layout_label="table",
        )
        commands, grouped_ids = renderer._build_table_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(),
        )

    assert commands == []
    assert grouped_ids == set()


if __name__ == "__main__":
    test_translation_only_disables_horizontal_scaling_by_default()
    test_translation_only_keeps_original_size_when_text_fits()
    test_translation_only_can_enable_horizontal_scaling_explicitly()
    test_translation_only_table_cell_fit_avoids_horizontal_scaling()
    test_table_cell_fit_uses_original_font_as_base_when_adjusted_font_is_tiny()
    test_table_cell_group_wraps_with_shared_font_size()
    test_table_cell_layout_preserves_latin_word_boundaries_when_shrinking()
    test_table_cell_group_uses_source_siblings_after_plain_span_merge()
    test_table_cell_group_wraps_single_span_only_with_extra_cell_room()
    test_table_cell_group_skips_overlapping_single_span_cells()
    test_table_cell_group_skips_single_span_cell_with_inner_sibling_span()
    test_table_cell_group_skips_over_tall_cell_bbox()
    test_table_cell_group_combines_multi_span_cell_translation()
