"""ParagraphTranslator 对 block 模式的容错测试。"""

from __future__ import annotations

from collections.abc import Sequence

from modules.pdf.entities import BBox, PageInfo, TextBlock, TextSpan
from modules.pdf.formatter import TaggedSpan, TaggedTextBlock
from modules.pdf.translator import ChunkTranslation, ParagraphTranslator
from modules.translation.contracts import TranslationBatchItem, TranslationBatchResult


class EmptySpanTranslator:
    """返回 marker 数量正确但第二个 span 为空的无效候选。"""

    per_job_concurrency = 1

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """构造会触发整个 chunk fallback 的空 span 响应。"""

        del source_language, target_language, format_hint, read_only_context, repair_candidates
        candidate = texts[0].replace(
            "<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>",
            "<SPAN_0>第一段</SPAN_0><SPAN_1> </SPAN_1>",
        )
        return TranslationBatchResult(items=(TranslationBatchItem(index=0, text=candidate),))


def test_chunk_validation_accepts_complete_plain_and_span_blocks() -> None:
    """plain block 与按源顺序完整回填的 span block 都应通过。"""

    translator = ParagraphTranslator()
    source = (
        "[PAGE_0]\n[BLOCK_0]plain source\n[BLOCK_1]<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>"
    )
    candidate = (
        "[PAGE_0]\n[BLOCK_0]普通译文\n[BLOCK_1]<SPAN_0>第一段</SPAN_0><SPAN_1>第二段</SPAN_1>"
    )

    assert translator._chunk_translation_is_usable(source, candidate) is True


def test_chunk_validation_accepts_strict_span_wrapping_for_plain_block() -> None:
    """保持模型为 plain block 添加规范 span 包装的兼容行为。"""

    translator = ParagraphTranslator()
    source = "[PAGE_0]\n[BLOCK_0]plain source"
    candidate = "[PAGE_0]\n[BLOCK_0]<SPAN_0>普通</SPAN_0><SPAN_1>译文</SPAN_1>"

    assert translator._chunk_translation_is_usable(source, candidate) is True


def test_chunk_validation_accepts_unchanged_symbol_value() -> None:
    """纯符号无需强行改写, 但仍必须作为非空值明确回填。"""

    translator = ParagraphTranslator()
    source = "[PAGE_0]\n[BLOCK_0]<SPAN_0>label</SPAN_0><SPAN_1>→</SPAN_1>"
    candidate = "[PAGE_0]\n[BLOCK_0]<SPAN_0>标签</SPAN_0><SPAN_1>→</SPAN_1>"

    assert translator._chunk_translation_is_usable(source, candidate) is True


def test_chunk_validation_rejects_empty_plain_or_span_value() -> None:
    """空 block 与空白 span 会导致正文丢失, 必须让整个 chunk fallback。"""

    translator = ParagraphTranslator()

    assert (
        translator._chunk_translation_is_usable(
            "[PAGE_0]\n[BLOCK_0]plain source",
            "[PAGE_0]\n[BLOCK_0]   ",
        )
        is False
    )
    assert (
        translator._chunk_translation_is_usable(
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>",
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一段</SPAN_0><SPAN_1> \n </SPAN_1>",
        )
        is False
    )


def test_chunk_validation_rejects_crossed_or_nested_spans() -> None:
    """标签计数相同也不能接受交叉或嵌套结构。"""

    translator = ParagraphTranslator()
    source = "[PAGE_0]\n[BLOCK_0]<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>"

    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一<SPAN_1>第二</SPAN_0></SPAN_1>",
        )
        is False
    )
    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一<SPAN_1>第二</SPAN_1></SPAN_0>",
        )
        is False
    )


def test_chunk_validation_rejects_unmapped_text_and_extra_page_marker() -> None:
    """span 外正文或没有 block 的额外 page marker 都属于未消费结构。"""

    translator = ParagraphTranslator()
    source = "[PAGE_0]\n[BLOCK_0]<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>"

    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一</SPAN_0>残留<SPAN_1>第二</SPAN_1>",
        )
        is False
    )
    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_99]\n[PAGE_0]\n[BLOCK_0]<SPAN_0>第一</SPAN_0><SPAN_1>第二</SPAN_1>",
        )
        is False
    )


def test_chunk_validation_rejects_duplicate_and_malformed_span_fragments() -> None:
    """重复 marker 与未闭合的 marker 碎片都不能被当作正文吞掉。"""

    translator = ParagraphTranslator()
    source = "[PAGE_0]\n[BLOCK_0]<SPAN_0>first</SPAN_0><SPAN_1>second</SPAN_1>"

    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一</SPAN_0><SPAN_0>重复</SPAN_0>",
        )
        is False
    )
    assert (
        translator._chunk_translation_is_usable(
            source,
            "[PAGE_0]\n[BLOCK_0]<SPAN_0>第一<SPAN_X>残片</SPAN_0><SPAN_1>第二</SPAN_1>",
        )
        is False
    )


def test_empty_span_candidate_falls_back_whole_chunk() -> None:
    """一个 span 为空时不得部分回写, 其余 span 也必须使用当前 chunk 原文。"""

    first_span = TextSpan(span_id=0, bbox=BBox(0, 20, 100, 30), text="first", font_size=10.0)
    second_span = TextSpan(span_id=1, bbox=BBox(0, 0, 100, 10), text="second", font_size=10.0)
    text_block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 100, 30),
        text="first second",
        spans=[first_span, second_span],
    )
    page = PageInfo(page_index=0, texts=[text_block], preserved_texts=[])
    tagged_block = TaggedTextBlock(
        marker="[BLOCK_0]",
        page_marker="[PAGE_0]",
        original_block_id=0,
        page_index=0,
        spans=[
            TaggedSpan("SPAN_0", original_span_id=0, text="first"),
            TaggedSpan("SPAN_1", original_span_id=1, text="second"),
        ],
    )

    result = ParagraphTranslator().translate(
        [page],
        [tagged_block],
        "en",
        "zh-CN",
        EmptySpanTranslator(),
    )

    assert result.chunks[0].fallback is True
    assert result.pages[0].texts[0].spans[0].translated_text == "first"
    assert result.pages[0].texts[0].spans[1].translated_text == "second"


def test_plain_block_uses_single_span_wrapped_translation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    translator = ParagraphTranslator()
    text_block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 200, 80),
        text="source paragraph",
        spans=[
            TextSpan(span_id=0, bbox=BBox(0, 0, 200, 20), text="source paragraph", font_size=12.0)
        ],
        translation_mode="block",
    )
    page = PageInfo(page_index=0, texts=[text_block], preserved_texts=[])
    tagged_block = TaggedTextBlock(
        marker="[BLOCK_0]",
        page_marker="[PAGE_0]",
        original_block_id=0,
        page_index=0,
        spans=[],
        plain_text="source paragraph",
    )
    chunk = ChunkTranslation(
        index=0,
        content="[PAGE_0]\n[BLOCK_0]source paragraph",
        prompt="",
        translation="[PAGE_0]\n[BLOCK_0]<SPAN_0>译文段落</SPAN_0>",
        block_refs=((0, "[BLOCK_0]"),),
    )

    translator._apply_translations([page], [tagged_block], [chunk], "zh")

    assert text_block.translated_text == "译文段落"
    assert text_block.translation_mode == "block"


def test_plain_block_merges_multiple_span_wrapped_translation_in_order() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    translator = ParagraphTranslator()
    span_map = {
        "SPAN_2": "第三段",
        "SPAN_0": "第一段",
        "SPAN_1": "第二段",
    }

    translated_text = translator._resolve_plain_block_translation(span_map)

    assert translated_text == "第一段第二段第三段"


def test_plain_block_parses_multiline_wrapped_translation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    translator = ParagraphTranslator()
    text_block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 200, 80),
        text="source paragraph",
        spans=[
            TextSpan(span_id=0, bbox=BBox(0, 0, 200, 20), text="source paragraph", font_size=12.0)
        ],
        translation_mode="block",
    )
    page = PageInfo(page_index=0, texts=[text_block], preserved_texts=[])
    tagged_block = TaggedTextBlock(
        marker="[BLOCK_0]",
        page_marker="[PAGE_0]",
        original_block_id=0,
        page_index=0,
        spans=[],
        plain_text="source paragraph",
    )
    chunk = ChunkTranslation(
        index=0,
        content="[PAGE_0]\n[BLOCK_0]source paragraph",
        prompt="",
        translation="[PAGE_0]\n[BLOCK_0]<SPAN_0>第一行\n第二行</SPAN_0>\n<SPAN_1>第三行</SPAN_1>",
        block_refs=((0, "[BLOCK_0]"),),
    )

    translator._apply_translations([page], [tagged_block], [chunk], "zh")

    assert text_block.translated_text == "第一行\n第二行第三行"


def test_plain_block_falls_back_to_source_on_incomplete_span_tags() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    translator = ParagraphTranslator()
    text_block = TextBlock(
        block_id=0,
        bbox=BBox(0, 0, 200, 80),
        text="source paragraph",
        spans=[
            TextSpan(span_id=0, bbox=BBox(0, 0, 200, 20), text="source paragraph", font_size=12.0)
        ],
        translation_mode="block",
    )
    page = PageInfo(page_index=0, texts=[text_block], preserved_texts=[])
    tagged_block = TaggedTextBlock(
        marker="[BLOCK_0]",
        page_marker="[PAGE_0]",
        original_block_id=0,
        page_index=0,
        spans=[],
        plain_text="source paragraph",
    )
    chunk = ChunkTranslation(
        index=0,
        content="[PAGE_0]\n[BLOCK_0]source paragraph",
        prompt="",
        translation="[PAGE_0]\n[BLOCK_0]<SPAN_0>第一段</SPAN_0>\n<SPAN_1>第二段",
        block_refs=((0, "[BLOCK_0]"),),
    )

    translator._apply_translations([page], [tagged_block], [chunk], "zh")

    assert text_block.translated_text == "source paragraph"


def test_plain_span_group_writes_whole_translation_to_first_source_span() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    translator = ParagraphTranslator()
    cell_bbox = BBox(90, 720, 250, 750)
    first_span = TextSpan(
        span_id=0,
        bbox=BBox(94, 740, 238, 748),
        text="销售商品、提供劳务收到的",
        table_cell_bbox=cell_bbox,
        font_size=10.0,
    )
    second_span = TextSpan(
        span_id=0,
        bbox=BBox(94, 726, 118, 734),
        text="现金",
        table_cell_bbox=cell_bbox,
        font_size=10.0,
    )
    first_block = TextBlock(
        block_id=10,
        bbox=BBox(92, 736, 240, 748),
        text=first_span.text,
        spans=[first_span],
        layout_label="table",
    )
    second_block = TextBlock(
        block_id=11,
        bbox=BBox(92, 724, 120, 734),
        text=second_span.text,
        spans=[second_span],
        layout_label="table",
    )
    page = PageInfo(page_index=0, texts=[first_block, second_block], preserved_texts=[])
    tagged_block = TaggedTextBlock(
        marker="[BLOCK_0]",
        page_marker="[PAGE_0]",
        original_block_id=10,
        page_index=0,
        spans=[
            TaggedSpan("SPAN_0", original_span_id=0, text=first_span.text, original_block_id=10),
            TaggedSpan("SPAN_1", original_span_id=0, text=second_span.text, original_block_id=11),
        ],
        plain_text="销售商品、提供劳务收到的现金",
    )
    chunk = ChunkTranslation(
        index=0,
        content="[PAGE_0]\n[BLOCK_0]销售商品、提供劳务收到的现金",
        prompt="",
        translation="[PAGE_0]\n[BLOCK_0]Cash received from sales of goods and rendering services",
        block_refs=((0, "[BLOCK_0]"),),
    )

    translator._apply_translations([page], [tagged_block], [chunk], "en")

    assert first_span.translated_text == "Cash received from sales of goods and rendering services"
    assert first_span.adjusted_font_size is not None
    assert second_span.translated_text == ""
    assert second_span.adjusted_font_size is None
