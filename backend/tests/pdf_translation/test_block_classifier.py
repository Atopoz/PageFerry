"""Paragraph block classifier tests.

Run:
    pytest tests/pdf_translation/test_block_classifier.py
    python tests/pdf_translation/test_block_classifier.py
"""

from __future__ import annotations

from modules.pdf.entities import BBox, LayoutResult, TextBlock, TextSpan
from modules.pdf.extractor import (
    ParagraphExtractor,
    ParagraphModeConfig,
    _convert_pdf_color_to_rgb,
)
from vendor.pdfminerex.pdfcolor import PDFColorSpace


class _DummyLayoutDetector:
    """提供 _DummyLayoutDetector 使用的 PDF parity test 替身。"""

    def __init__(self) -> None:
        """保存这个测试替身需要的状态。"""
        pass


def _make_span(
    span_id: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    text: str,
    *,
    textbox_id: int | None = None,
    textbox_bbox: BBox | None = None,
    layout_label: str | None = None,
) -> TextSpan:
    """构造这个 PDF parity test 需要的替代数据。"""
    return TextSpan(
        span_id=span_id,
        bbox=BBox(x1, y1, x2, y2),
        text=text,
        font_size=10.0,
        source_textbox_id=textbox_id,
        source_textbox_bbox=textbox_bbox,
        layout_label=layout_label,
    )


def test_classify_paragraph_block() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    spans = [
        _make_span(0, 0, 80, 180, 90, "Line one"),
        _make_span(1, 0, 60, 180, 70, "Line two"),
        _make_span(2, 0, 40, 180, 50, "Line three"),
    ]
    block = TextBlock(
        block_id=0,
        bbox=BBox(0, 30, 200, 100),
        text="".join(span.text for span in spans),
        spans=spans,
        layout_label="text",
    )
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    mode = extractor._classify_block_translation_mode(
        block, extractor._paragraph_mode_config, "text"
    )
    assert mode == "block"


def test_classify_axis_labels_as_span() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    spans = [
        _make_span(0, 0, 80, 30, 90, "1"),
        _make_span(1, 80, 60, 110, 70, "2"),
        _make_span(2, 150, 40, 180, 50, "3"),
    ]
    block = TextBlock(
        block_id=0,
        bbox=BBox(0, 30, 200, 100),
        text="".join(span.text for span in spans),
        spans=spans,
        layout_label="text",
    )
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    mode = extractor._classify_block_translation_mode(
        block, extractor._paragraph_mode_config, "text"
    )
    assert mode == "span"


def test_assign_block_dominant_color_uses_weighted_majority() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    spans = [
        TextSpan(
            span_id=0,
            bbox=BBox(0, 80, 30, 90),
            text="A",
            color=(1.0, 0.9, 0.0),
            font_size=10.0,
        ),
        TextSpan(
            span_id=1,
            bbox=BBox(30, 80, 160, 90),
            text="Long black text",
            color=(0.0, 0.0, 0.0),
            font_size=10.0,
        ),
    ]
    block = TextBlock(
        block_id=0,
        bbox=BBox(0, 70, 180, 100),
        text="A Long black text",
        spans=spans,
        layout_label="text",
    )
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )

    extractor._assign_block_dominant_colors([block])

    assert block.dominant_color == (0.0, 0.0, 0.0)


def test_convert_pdf_color_to_rgb_resolves_separation_spot_color() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    spot_color = PDFColorSpace(
        "Separation",
        1,
        alternate=PDFColorSpace("DeviceCMYK", 4),
        tint_transform={
            "/FunctionType": 2,
            "/C0": [0.0, 0.0, 0.0, 0.0],
            "/C1": [0.8, 0.772549, 0.670588, 0.917647],
            "/N": 1,
        },
    )

    rgb = _convert_pdf_color_to_rgb(1.0, spot_color)

    assert rgb == (0.0, 0.0, 0.0)


def test_group_unmatched_spans_merges_inline_orphan_block() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    main_box = BBox(316.85, 216.92, 502.05, 238.91)
    orphan_box = BBox(506.0, 228.88, 532.38, 238.84)
    spans = [
        _make_span(
            0,
            316.85,
            228.88,
            502.05,
            238.91,
            "[Pos. #1] Human intelligence is not general",
            textbox_id=24,
            textbox_bbox=main_box,
        ),
        _make_span(
            1,
            316.85,
            216.92,
            381.22,
            226.88,
            "meaningful way",
            textbox_id=24,
            textbox_bbox=main_box,
        ),
        _make_span(
            2,
            506.0,
            228.88,
            532.38,
            238.84,
            "in any",
            textbox_id=22,
            textbox_bbox=orphan_box,
        ),
    ]

    blocks = extractor._group_unmatched_spans_by_textbox(spans, start_block_id=0)

    assert len(blocks) == 1
    assert [span.text for span in blocks[0].spans] == [
        "[Pos. #1] Human intelligence is not general",
        "in any",
        "meaningful way",
    ]


def test_group_unmatched_spans_does_not_merge_adjacent_table_cells() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    value_box = BBox(121.0, 207.0, 250.0, 229.0)
    label_box = BBox(264.0, 220.0, 315.0, 229.0)
    spans = [
        _make_span(
            0,
            121.0,
            220.0,
            250.0,
            229.0,
            "D950 OG HR LOOSE STRAIGHT",
            textbox_id=10,
            textbox_bbox=value_box,
            layout_label="table",
        ),
        _make_span(
            1,
            121.0,
            207.0,
            218.0,
            216.0,
            "FL 000724448 Adopted",
            textbox_id=10,
            textbox_bbox=value_box,
            layout_label="table",
        ),
        _make_span(
            2,
            264.0,
            220.0,
            315.0,
            229.0,
            "Design BOM",
            textbox_id=8,
            textbox_bbox=label_box,
            layout_label="table",
        ),
    ]

    blocks = extractor._group_unmatched_spans_by_textbox(spans, start_block_id=0)

    assert len(blocks) == 2
    assert sorted("".join(span.text for span in block.spans) for block in blocks) == sorted(
        [
            "Design BOM",
            "D950 OG HR LOOSE STRAIGHTFL 000724448 Adopted",
        ]
    )


def test_apply_paragraph_mode_uses_layout_type_for_compact_annotation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    block = TextBlock(
        block_id=0,
        bbox=BBox(316.85, 216.92, 532.38, 238.91),
        text="[Pos. #1] Human intelligence is not generalin anymeaningful way",
        spans=[
            _make_span(
                0, 316.85, 228.88, 502.05, 238.91, "[Pos. #1] Human intelligence is not general"
            ),
            _make_span(1, 506.0, 228.88, 532.38, 238.84, "in any"),
            _make_span(2, 316.85, 216.92, 381.22, 226.88, "meaningful way"),
        ],
        layout_type="text",
        layout_label=None,
    )

    extractor._apply_paragraph_mode([block], extractor._paragraph_mode_config)

    assert block.translation_mode == "block"


def test_apply_paragraph_mode_uses_block_for_compact_single_textbox_intro() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    textbox_bbox = BBox(52.2, 644.3, 292.7, 668.8)
    block = TextBlock(
        block_id=0,
        bbox=textbox_bbox,
        text="[Pos. #1] has the following implications for these definitions:",
        spans=[
            _make_span(
                0,
                52.2,
                656.6,
                292.7,
                668.8,
                "[Pos. #1] has the following implications for these defini-",
                textbox_id=1,
                textbox_bbox=textbox_bbox,
            ),
            _make_span(
                1,
                52.2,
                644.3,
                111.0,
                656.4,
                "tions:",
                textbox_id=1,
                textbox_bbox=textbox_bbox,
            ),
        ],
        layout_type="text",
    )

    extractor._apply_paragraph_mode([block], extractor._paragraph_mode_config)

    assert block.translation_mode == "block"


def test_classify_inline_formula_superscript_block_as_block() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_DummyLayoutDetector(),
        paragraph_mode_config=ParagraphModeConfig(labels={"text"}),
    )
    spans = [
        _make_span(
            0,
            70.9,
            119.1,
            479.7,
            133.4,
            "the decoder iteratively refines N object queries Q = {qi}N",
        ),
        TextSpan(
            span_id=1,
            bbox=BBox(474.5, 115.9, 485.4, 125.4),
            text="i=1",
            font_size=7.0,
        ),
        _make_span(2, 489.2, 119.1, 526.3, 133.4, "∈ R_N×d."),  # noqa: RUF001
        _make_span(
            3,
            70.5,
            105.6,
            524.6,
            116.5,
            "The reading order is then derived from the refined query embeddings.",
        ),
        _make_span(4, 70.9, 92.0, 252.2, 102.9, "through a Global Pointer Mechanism."),
    ]
    block = TextBlock(
        block_id=0,
        bbox=BBox(70.5, 92.0, 526.3, 133.4),
        text="".join(span.text for span in spans),
        spans=spans,
        layout_label="text",
    )

    mode = extractor._classify_block_translation_mode(
        block, extractor._paragraph_mode_config, "text"
    )

    assert mode == "block"


def test_prune_container_layouts_removes_reference_container_when_children_exist() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    layouts = [
        LayoutResult(0, "reference", (1000, 1000, 3), BBox(0, 0, 200, 400), 0.99),
        LayoutResult(1, "reference_content", (1000, 1000, 3), BBox(10, 10, 190, 120), 0.98),
        LayoutResult(2, "reference_content", (1000, 1000, 3), BBox(10, 130, 190, 240), 0.97),
        LayoutResult(3, "paragraph_title", (1000, 1000, 3), BBox(0, 410, 80, 430), 0.91),
    ]

    pruned = ParagraphExtractor._prune_container_layouts(layouts)

    assert [layout.label for layout in pruned] == [
        "reference_content",
        "reference_content",
        "paragraph_title",
    ]


if __name__ == "__main__":
    test_classify_paragraph_block()
    test_classify_axis_labels_as_span()
    test_assign_block_dominant_color_uses_weighted_majority()
