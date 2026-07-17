"""Formatter block plain text tests."""

from __future__ import annotations

from modules.pdf.entities import BBox, PageInfo, TextBlock, TextSpan
from modules.pdf.formatter import build_tagged_text_blocks, render_tagged_text


def test_build_tagged_text_blocks_preserves_line_breaks_for_reference_block() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 200, 60),
        text="Author one\nAuthor two",
        spans=[
            TextSpan(span_id=0, bbox=BBox(0, 40, 200, 50), text="Author one", font_size=10.0),
            TextSpan(span_id=1, bbox=BBox(0, 20, 200, 30), text="Author two", font_size=10.0),
        ],
        layout_label="reference_content",
        translation_mode="block",
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 1
    assert tagged[0].plain_text == "Author one\nAuthor two"


def test_build_tagged_text_blocks_keeps_inline_formula_fragments_in_line_order() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    block = TextBlock(
        block_id=0,
        bbox=BBox(70.5, 92.0, 526.3, 133.4),
        text="",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(70.9, 119.1, 479.7, 133.4),
                text="the decoder iteratively refines N object queries Q = {qi}N",
                font_size=10.0,
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(474.5, 115.9, 485.4, 125.4),
                text="i=1",
                font_size=7.0,
            ),
            TextSpan(
                span_id=2,
                bbox=BBox(489.2, 119.1, 526.3, 133.4),
                text="∈ R_N×d.",  # noqa: RUF001
                font_size=10.0,
            ),
            TextSpan(
                span_id=3,
                bbox=BBox(70.5, 105.6, 524.6, 116.5),
                text="The reading order is then derived from the refined query embeddings.",
                font_size=10.0,
            ),
            TextSpan(
                span_id=4,
                bbox=BBox(70.9, 92.0, 252.2, 102.9),
                text="through a Global Pointer Mechanism.",
                font_size=10.0,
            ),
        ],
        layout_label="text",
        translation_mode="block",
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 1
    assert tagged[0].plain_text == (
        "the decoder iteratively refines N object queries Q = {qi}Ni=1 ∈ R_N×d. "  # noqa: RUF001
        "The reading order is then derived from the refined query embeddings. "
        "through a Global Pointer Mechanism."
    )


def test_build_tagged_text_blocks_splits_table_by_cell_bbox() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    row1_bbox = BBox(200, 40, 500, 60)
    row2_bbox = BBox(200, 20, 500, 40)
    block = TextBlock(
        block_id=7,
        bbox=BBox(200, 20, 500, 60),
        text="row1 line1 row1 line2 row2 line1 row2 line2",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(205, 52, 480, 58),
                text="row1 line1",
                table_cell_bbox=row1_bbox,
                font_size=10.0,
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(205, 43, 350, 49),
                text="row1 line2",
                table_cell_bbox=row1_bbox,
                font_size=10.0,
            ),
            TextSpan(
                span_id=2,
                bbox=BBox(205, 32, 480, 38),
                text="row2 line1",
                table_cell_bbox=row2_bbox,
                font_size=10.0,
            ),
            TextSpan(
                span_id=3,
                bbox=BBox(205, 23, 350, 29),
                text="row2 line2",
                table_cell_bbox=row2_bbox,
                font_size=10.0,
            ),
        ],
        layout_label="table",
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 2
    assert [item.marker for item in tagged] == ["[BLOCK_0]", "[BLOCK_1]"]
    assert [span.text for span in tagged[0].spans] == ["row1 line1", "row1 line2"]
    assert [span.original_span_id for span in tagged[0].spans] == [0, 1]
    assert [span.text for span in tagged[1].spans] == ["row2 line1", "row2 line2"]
    assert [span.original_span_id for span in tagged[1].spans] == [2, 3]


def test_build_tagged_text_blocks_splits_table_spans_without_cell_bbox() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    block = TextBlock(
        block_id=3,
        bbox=BBox(0, 0, 200, 40),
        text="left right",
        spans=[
            TextSpan(span_id=0, bbox=BBox(0, 20, 80, 30), text="left", font_size=10.0),
            TextSpan(span_id=1, bbox=BBox(100, 20, 180, 30), text="right", font_size=10.0),
        ],
        layout_label="table",
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 2
    assert [[span.text for span in item.spans] for item in tagged] == [["left"], ["right"]]


def test_build_tagged_text_blocks_merges_cross_block_table_cell_fragments() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    cell_bbox = BBox(90, 720, 250, 750)
    first_block = TextBlock(
        block_id=10,
        bbox=BBox(92, 736, 240, 748),
        text="销售商品、提供劳务收到的",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(94, 740, 238, 748),
                text="销售商品、提供劳务收到的",
                table_cell_bbox=cell_bbox,
                font_size=10.0,
            )
        ],
        layout_label="table",
    )
    second_block = TextBlock(
        block_id=11,
        bbox=BBox(92, 724, 120, 734),
        text="现金",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(94, 726, 118, 734),
                text="现金",
                table_cell_bbox=cell_bbox,
                font_size=10.0,
            )
        ],
        layout_label="table",
    )
    page = PageInfo(page_index=0, texts=[first_block, second_block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 1
    assert tagged[0].plain_text == "销售商品、提供劳务收到的现金"
    assert [span.original_block_id for span in tagged[0].spans] == [10, 11]
    assert [span.original_span_id for span in tagged[0].spans] == [0, 0]
    assert render_tagged_text(tagged) == "[PAGE_0]\n[BLOCK_0]销售商品、提供劳务收到的现金"


def test_build_tagged_text_blocks_does_not_merge_over_tall_table_cell_fragments() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    cell_bbox = BBox(15, 35, 776, 402)
    first_block = TextBlock(
        block_id=10,
        bbox=BBox(19, 350, 675, 397),
        text="Deborah McMullen 1. Please help",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(19, 388, 95, 397),
                text="Deborah McMullen",
                table_cell_bbox=cell_bbox,
                font_size=9.0,
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(19, 370, 468, 383),
                text="1. Please help to add back POM P47 Pocket Bag Width",
                table_cell_bbox=cell_bbox,
                font_size=13.0,
            ),
        ],
        layout_label="table",
    )
    second_block = TextBlock(
        block_id=11,
        bbox=BBox(19, 298, 185, 325),
        text="Deborah McMullen 1. POM P47 has been added",
        spans=[
            TextSpan(
                span_id=0,
                bbox=BBox(19, 316, 95, 325),
                text="Deborah McMullen",
                table_cell_bbox=cell_bbox,
                font_size=9.0,
            ),
            TextSpan(
                span_id=1,
                bbox=BBox(19, 298, 185, 311),
                text="1. POM P47 has been added",
                table_cell_bbox=cell_bbox,
                font_size=13.0,
            ),
        ],
        layout_label="table",
    )
    page = PageInfo(page_index=0, texts=[first_block, second_block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 2
    assert all(block.plain_text is None for block in tagged)
    assert [[span.original_block_id for span in block.spans] for block in tagged] == [
        [10, 10],
        [11, 11],
    ]


def test_build_tagged_text_blocks_preserves_chart_rows_as_separate_blocks() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    block = TextBlock(
        block_id=5,
        bbox=BBox(8, 160, 88, 182),
        text="MAIN FABRIC 10 3 Rhinestone STRIPES",
        spans=[
            TextSpan(
                span_id=12,
                bbox=BBox(29, 173, 88, 181),
                text="MAIN FABRIC 10",
                font_size=8.0,
            ),
            TextSpan(
                span_id=13,
                bbox=BBox(9, 162, 88, 170),
                text="3 Rhinestone STRIPES",
                font_size=8.0,
            ),
        ],
        layout_label="chart",
    )
    page = PageInfo(page_index=0, texts=[block], preserved_texts=[])

    tagged = build_tagged_text_blocks([page])

    assert len(tagged) == 2
    assert [item.marker for item in tagged] == ["[BLOCK_0]", "[BLOCK_1]"]
    assert [[span.text for span in item.spans] for item in tagged] == [
        ["MAIN FABRIC 10"],
        ["3 Rhinestone STRIPES"],
    ]
    assert render_tagged_text(tagged) == (
        "[PAGE_0]\n[BLOCK_0]MAIN FABRIC 10\n[BLOCK_1]3 Rhinestone STRIPES"
    )
