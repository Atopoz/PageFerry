"""PDF table geometry extraction tests."""

from __future__ import annotations

import pytest

from modules.pdf.entities import BBox, TextCharBox, TextSpan
from modules.pdf.extractor import ParagraphExtractor, _TableRuling
from vendor.pdfminerex.layout import LTRect


def test_filled_thin_rect_is_treated_as_table_ruling() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    rect = LTRect(
        linewidth=0,
        bbox=(10, 20, 110, 20.48),
        stroke=False,
        fill=True,
    )

    ruling = ParagraphExtractor._build_ruling_from_filled_rect(rect)

    assert ruling is not None
    assert ruling.orientation == "h"
    assert ruling.x1 == pytest.approx(10)
    assert ruling.x2 == pytest.approx(110)
    assert ruling.y1 == pytest.approx(20.24)
    assert ruling.y2 == pytest.approx(20.24)


def test_resolve_span_table_cell_bbox_from_rulings() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    span = TextSpan(
        span_id=0,
        bbox=BBox(120, 50, 180, 60),
        text="text",
    )
    table_bbox = BBox(0, 0, 200, 100)
    rulings = [
        _TableRuling("v", 0, 0, 0, 100),
        _TableRuling("v", 100, 0, 100, 100),
        _TableRuling("v", 200, 0, 200, 100),
        _TableRuling("h", 0, 0, 200, 0),
        _TableRuling("h", 0, 40, 200, 40),
        _TableRuling("h", 0, 70, 200, 70),
        _TableRuling("h", 0, 100, 200, 100),
    ]

    cell_bbox = ParagraphExtractor._resolve_span_table_cell_bbox(span, table_bbox, rulings)

    assert cell_bbox is not None
    assert 100 < cell_bbox.x1 < 105
    assert 195 < cell_bbox.x2 < 200
    assert 40 < cell_bbox.y1 < 45
    assert 65 < cell_bbox.y2 < 70


def test_resolve_span_table_cell_bbox_skips_multi_column_textline() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    span = TextSpan(
        span_id=0,
        bbox=BBox(10, 50, 190, 60),
        text="Mine Method 2000 2001 2002 2003 2004",
        font_size=8.0,
    )
    table_bbox = BBox(0, 0, 200, 100)
    rulings = [
        _TableRuling("v", 0, 0, 0, 100),
        _TableRuling("v", 80, 0, 80, 100),
        _TableRuling("v", 100, 0, 100, 100),
        _TableRuling("v", 120, 0, 120, 100),
        _TableRuling("v", 140, 0, 140, 100),
        _TableRuling("v", 160, 0, 160, 100),
        _TableRuling("v", 180, 0, 180, 100),
        _TableRuling("v", 200, 0, 200, 100),
        _TableRuling("h", 0, 40, 200, 40),
        _TableRuling("h", 0, 70, 200, 70),
    ]

    cell_bbox = ParagraphExtractor._resolve_span_table_cell_bbox(span, table_bbox, rulings)

    assert cell_bbox is None


def test_split_span_by_table_grid_splits_header_cells() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    char_boxes = [
        *_make_char_boxes("Mine", 10, 50),
        *_make_char_boxes("Method", 32, 50),
        *_make_char_boxes("2000", 84, 50),
        *_make_char_boxes("2001", 104, 50),
    ]
    span = TextSpan(
        span_id=0,
        bbox=BBox(10, 50, 116, 60),
        text="Mine Method 2000 2001",
        font_size=8.0,
        layout_label="table",
        char_boxes=char_boxes,
    )
    table_bbox = BBox(0, 0, 120, 100)
    rulings = [
        _TableRuling("v", 0, 0, 0, 100),
        _TableRuling("v", 80, 0, 80, 100),
        _TableRuling("v", 100, 0, 100, 100),
        _TableRuling("v", 120, 0, 120, 100),
        _TableRuling("h", 0, 40, 120, 40),
        _TableRuling("h", 0, 70, 120, 70),
    ]

    split_spans = ParagraphExtractor._split_span_by_table_grid(
        span,
        table_bbox,
        rulings,
        start_span_id=10,
    )

    assert split_spans is not None
    assert [item.text for item in split_spans] == ["Mine Method", "2000", "2001"]
    assert [item.span_id for item in split_spans] == [10, 11, 12]
    assert split_spans[0].table_cell_bbox is not None
    assert split_spans[0].table_cell_bbox.x1 == pytest.approx(2.0)
    assert split_spans[0].table_cell_bbox.x2 == pytest.approx(78.0)
    assert split_spans[1].is_preserved is True
    assert split_spans[1].translated_text == "2000"
    assert split_spans[2].is_preserved is True
    assert split_spans[2].translated_text == "2001"


def test_resolve_span_table_cell_bbox_requires_vertical_pair() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    span = TextSpan(
        span_id=0,
        bbox=BBox(120, 50, 180, 60),
        text="text",
    )
    table_bbox = BBox(0, 0, 200, 100)
    rulings = [
        _TableRuling("v", 100, 0, 100, 100),
        _TableRuling("h", 0, 40, 200, 40),
        _TableRuling("h", 0, 70, 200, 70),
    ]

    cell_bbox = ParagraphExtractor._resolve_span_table_cell_bbox(span, table_bbox, rulings)

    assert cell_bbox is None


def test_resolve_span_table_cell_bbox_requires_internal_column_ruling() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    span = TextSpan(
        span_id=0,
        bbox=BBox(20, 50, 80, 60),
        text="Canada",
    )
    table_bbox = BBox(0, 0, 200, 100)
    rulings = [
        _TableRuling("v", 0, 0, 0, 100),
        _TableRuling("v", 200, 0, 200, 100),
        _TableRuling("h", 0, 40, 200, 40),
        _TableRuling("h", 0, 70, 200, 70),
    ]

    cell_bbox = ParagraphExtractor._resolve_span_table_cell_bbox(span, table_bbox, rulings)

    assert cell_bbox is None


def test_drop_unreliable_table_cell_bboxes_for_unbounded_row_labels() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    table_bbox = BBox(0, 0, 200, 120)
    rulings = [
        _TableRuling("v", 0, 0, 0, 120),
        _TableRuling("v", 100, 0, 100, 120),
        _TableRuling("v", 200, 0, 200, 120),
        _TableRuling("h", 0, 0, 200, 0),
        _TableRuling("h", 0, 100, 200, 100),
        _TableRuling("h", 0, 120, 200, 120),
    ]
    spans = [
        TextSpan(span_id=0, bbox=BBox(20, 85, 80, 95), text="Total Volume", font_size=10.0),
        TextSpan(span_id=1, bbox=BBox(20, 70, 80, 80), text="Number of Deals", font_size=10.0),
        TextSpan(span_id=2, bbox=BBox(20, 55, 80, 65), text="Lead time", font_size=10.0),
        TextSpan(span_id=3, bbox=BBox(20, 40, 80, 50), text="Form", font_size=10.0),
    ]

    for span in spans:
        span.table_cell_bbox = ParagraphExtractor._resolve_span_table_cell_bbox(
            span, table_bbox, rulings
        )

    assert all(span.table_cell_bbox is not None for span in spans)

    ParagraphExtractor._drop_unreliable_table_cell_bboxes(spans, rulings)

    assert all(span.table_cell_bbox is None for span in spans)


def test_keep_compact_multiline_table_cell_bbox_without_internal_row_lines() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    cell_bbox = BBox(2, 21, 98, 59)
    rulings = [
        _TableRuling("v", 0, 20, 0, 60),
        _TableRuling("v", 100, 20, 100, 60),
        _TableRuling("h", 0, 20, 100, 20),
        _TableRuling("h", 0, 60, 100, 60),
    ]
    spans = [
        TextSpan(
            span_id=0,
            bbox=BBox(10, 43, 70, 53),
            text="Commitment",
            font_size=10.0,
            table_cell_bbox=cell_bbox,
        ),
        TextSpan(
            span_id=1,
            bbox=BBox(10, 31, 70, 41),
            text="Background",
            font_size=10.0,
            table_cell_bbox=cell_bbox,
        ),
    ]

    ParagraphExtractor._drop_unreliable_table_cell_bboxes(spans, rulings)

    assert all(span.table_cell_bbox == cell_bbox for span in spans)


def _make_char_boxes(text: str, start_x: float, y: float) -> list[TextCharBox]:
    """构造这个 PDF parity test 需要的替代数据。"""
    char_width = 3.0
    gap = 0.3
    boxes: list[TextCharBox] = []
    x = start_x
    for char in text:
        boxes.append(TextCharBox(char=char, bbox=BBox(x, y, x + char_width, y + 8)))
        x += char_width + gap
    return boxes
