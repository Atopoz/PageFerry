"""
PDF 文本处理 Part2: 段落格式化

将提取结果转化为大模型易懂格式

转换后的格式示例：

单 span 的 block（直接拼接）：
    [PAGE_0]
    [BLOCK_0]This is a simple paragraph with one span.
    [BLOCK_1]Another paragraph.

多 span 的 block（显式分割）：
    [PAGE_1]
    [BLOCK_0]<SPAN_0>First part of text </SPAN_0><SPAN_1>second part of text.</SPAN_1>
    [BLOCK_1]<SPAN_0>Multi-line </SPAN_0><SPAN_1>paragraph </SPAN_1><SPAN_2>example.</SPAN_2>

注意：
- 每个 block 一行输出
- 保留的 span (is_preserved=True) 会被自动跳过，不进入翻译流程
- CID 标记、纯数字等特殊内容已在提取阶段被标记为保留

后续可优化方向：
- 表格内的block沿用现逻辑
- 文本段将会按照block级翻译，后续渲染时会进行智能换行
"""


import re
from dataclasses import dataclass
from statistics import median
from typing import List, Sequence

from modules.pdf.token_counter import token_counter

from .entities import BBox, PageInfo, TextBlock, TextSpan
from .span_line_utils import group_spans_into_lines


@dataclass(frozen=True)
class TaggedSpan:
    """带格式化标记的 span 信息。"""

    marker: str
    original_span_id: int
    text: str
    original_block_id: int | None = None


@dataclass(frozen=True)
class TaggedTextBlock:
    """带格式化标记的文本块信息。"""

    marker: str
    page_marker: str
    original_block_id: int
    page_index: int
    spans: Sequence[TaggedSpan]
    plain_text: str | None = None


@dataclass(frozen=True)
class _SpanRef:
    block: TextBlock
    span: TextSpan


@dataclass(frozen=True)
class _TaggedSpanGroup:
    original_block_id: int
    spans: Sequence[TaggedSpan]
    plain_text: str | None = None


_ROW_PRESERVING_LAYOUT_LABELS = frozenset({"chart"})


def build_tagged_text_blocks(pages: Sequence[PageInfo]) -> List[TaggedTextBlock]:
    """
    根据页面内容构建带标记的文本块列表。

    注意：para_extractor 已经将需要保留的文本分离到 preserved_texts 中，
    因此 page.texts 中的所有 TextBlock 都应该被处理。
    """
    tagged_blocks: List[TaggedTextBlock] = []
    for page in pages:
        block_counter = 0
        page_marker = f"[PAGE_{page.page_index}]"
        cross_block_cell_groups = _build_cross_block_table_cell_groups(page.texts)
        grouped_span_keys = {
            (ref.block.block_id, ref.span.span_id)
            for groups in cross_block_cell_groups.values()
            for refs in groups
            for ref in refs
        }
        for block in page.texts:
            # 新结构中不再需要类型过滤，page.texts 中的所有块都应该被处理
            if getattr(block, "translation_mode", "span") == "block":
                marker = f"[BLOCK_{block_counter}]"
                label = (block.layout_label or block.layout_type or "").strip().lower()
                plain_text = _build_plain_text(
                    block.spans,
                    preserve_line_breaks=label in {"reference", "reference_content"},
                )
                if not plain_text:
                    continue
                if label in {"reference", "reference_content"}:
                    plain_text = "\n".join(
                        _whitespace_re.sub(" ", line).strip()
                        for line in plain_text.splitlines()
                        if _whitespace_re.sub(" ", line).strip()
                    ).strip()
                else:
                    plain_text = _whitespace_re.sub(" ", plain_text.replace("\n", " ")).strip()
                tagged_blocks.append(
                    TaggedTextBlock(
                        marker=marker,
                        page_marker=page_marker,
                        original_block_id=block.block_id,
                        page_index=page.page_index,
                        spans=[],
                        plain_text=plain_text,
                    )
                )
                block_counter += 1
            else:
                for tagged_group in _build_tagged_span_groups(
                    block,
                    cross_block_cell_groups=cross_block_cell_groups,
                    grouped_span_keys=grouped_span_keys,
                ):
                    if not tagged_group.spans:
                        continue
                    marker = f"[BLOCK_{block_counter}]"
                    tagged_blocks.append(
                        TaggedTextBlock(
                            marker=marker,
                            page_marker=page_marker,
                            original_block_id=tagged_group.original_block_id,
                            page_index=page.page_index,
                            spans=tagged_group.spans,
                            plain_text=tagged_group.plain_text,
                        )
                    )
                    block_counter += 1
    return tagged_blocks


def render_tagged_text(blocks: Sequence[TaggedTextBlock], line_separator: str = "\n") -> str:
    """将标记文本块渲染为文本。"""
    rendered_lines: List[str] = []
    current_page_marker: str | None = None
    for block in blocks:
        if not block.spans and block.plain_text is None:
            continue
        if block.page_marker != current_page_marker:
            rendered_lines.append(block.page_marker)
            current_page_marker = block.page_marker
        rendered_lines.append(_render_block_line(block))
    return line_separator.join(rendered_lines)


def render_tagged_text_chunks(
    blocks: Sequence[TaggedTextBlock],
    max_tokens: int,
    *,
    min_tokens: int = 0,
    line_separator: str = "\n",
) -> List[str]:
    """
    将标记文本块按token限制拆分成多个段落块。

    每个输出块都会重新包含对应页面的标记，以便独立使用时仍保留上下文信息。
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens 必须为正数")
    if min_tokens < 0:
        raise ValueError("min_tokens 不能为负数")
    if min_tokens > max_tokens:
        raise ValueError("min_tokens 不能大于 max_tokens")

    if not blocks:
        return []

    page_order: List[int] = []
    page_markers: dict[int, str] = {}
    page_lines: dict[int, List[str]] = {}

    for block in blocks:
        if not block.spans and block.plain_text is None:
            continue
        if block.page_index not in page_lines:
            page_order.append(block.page_index)
            page_markers[block.page_index] = block.page_marker
            page_lines[block.page_index] = []
        page_lines[block.page_index].append(_render_block_line(block))

    chunks: List[str] = []
    for page_index in page_order:
        block_lines = page_lines.get(page_index)
        if not block_lines:
            continue
        page_marker = page_markers[page_index]
        marker_tokens = token_counter.count_tokens(page_marker)
        if marker_tokens >= max_tokens:
            raise ValueError("max_tokens 必须大于页面标记所需的 token 数")
        effective_max = max_tokens - marker_tokens
        effective_min = max(0, min_tokens - marker_tokens)
        groups = token_counter.group_texts_by_token_limit(
            block_lines,
            min_tokens=effective_min,
            max_tokens=effective_max,
        )
        if not groups:
            continue
        for group in groups:
            chunk_lines = [page_marker, *(block_lines[idx] for idx in group)]
            chunks.append(line_separator.join(chunk_lines))
    return chunks


def get_plain_text_from_tagged_text(tagged_text: str) -> str:
    """
    从带标记的文本块中提取纯净文本。

    用于知识库召回等场景。
    策略：
    1. 移除 [PAGE_X]
    2. 移除 [BLOCK_X]
    3. 移除 <SPAN_X> 和 </SPAN_X>
    4. 替换为简单的文本连接（这里简单用空格连接，防止粘连）
    """
    # 1. 移除 [PAGE_X] 和 [BLOCK_X]
    # 这些通常是一行的开头
    text = re.sub(r"\[PAGE_\d+\]", "", tagged_text)
    text = re.sub(r"\[BLOCK_\d+\]", "\n", text) # Block 之间换行

    # 2. 处理 SPAN 标签
    # 格式: <SPAN_X>content</SPAN_X>
    # 我们希望保留 content。
    # 简单正则替换: <SPAN_\d+> -> "", </SPAN_\d+> -> " " (为了防止粘连)
    # 或者更精确一点，使用正则捕获 content

    # 先移除闭合标签，替换为空格，防止 "text</SPAN>text" 变成 "texttext"
    text = re.sub(r"</SPAN_\d+>", " ", text)
    # 移除开始标签
    text = re.sub(r"<SPAN_\d+>", "", text)

    # 3. 清理多余空格和空行
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _build_tagged_spans(
    spans: Sequence[TextSpan],
    filter_cid: bool = True,
    *,
    block_id: int | None = None,
) -> List[TaggedSpan]:
    """
    构建文本块内部的 span 标记。

    Args:
        spans: 待处理的 span 序列
        filter_cid: 是否过滤包含 CID 的文本（默认 True）

    注意：para_extractor 已经在提取阶段标记了包含 CID 的 span (is_preserved=True)，
    这里的 CID 检查作为双重保险。
    """
    tagged_spans: List[TaggedSpan] = []
    span_counter = 0
    for span in spans:
        if span.is_preserved:
            # 保留段直接跳过，不进入翻译 pipeline（包括 CID、纯数字等）
            continue
        text = _normalize_span_text(span.text)
        if not text:
            continue

        # 双重保险：再次检查 CID 编码文本（通常是页眉页脚或乱码）
        if filter_cid and "(cid:" in text:
            continue

        marker = f"SPAN_{span_counter}"
        tagged_spans.append(
            TaggedSpan(
                marker=marker,
                original_span_id=span.span_id,
                text=text,
                original_block_id=block_id,
            )
        )
        span_counter += 1
    return tagged_spans


def _build_tagged_span_groups(
    block: TextBlock,
    *,
    cross_block_cell_groups: dict[tuple[int, int], list[tuple[_SpanRef, ...]]] | None = None,
    grouped_span_keys: set[tuple[int, int]] | None = None,
) -> List[_TaggedSpanGroup]:
    """构建 block 的翻译分组。

    表格优先按连续的高置信单元格拆分；没有单元格证据时退回 span 粒度，
    避免模型把无框线表格或跨行 textbox 的上下单元格合并到同一个 tag。
    """
    label = (block.layout_label or block.layout_type or "").strip().lower()
    if label in _ROW_PRESERVING_LAYOUT_LABELS:
        return _build_row_preserving_span_groups(block)
    if label != "table":
        tagged_spans = _build_tagged_spans(block.spans, block_id=block.block_id)
        return [_TaggedSpanGroup(original_block_id=block.block_id, spans=tagged_spans)] if tagged_spans else []

    cross_block_cell_groups = cross_block_cell_groups or {}
    grouped_span_keys = grouped_span_keys or set()
    groups: List[_TaggedSpanGroup] = []
    current_key: tuple[str, int | tuple[float, float, float, float]] | None = None
    current_group: List[TextSpan] = []

    def flush_current_group() -> None:
        nonlocal current_group
        if current_group:
            tagged_spans = _build_tagged_spans(current_group, block_id=block.block_id)
            if tagged_spans:
                groups.append(
                    _TaggedSpanGroup(
                        original_block_id=block.block_id,
                        spans=tagged_spans,
                    )
                )
            current_group = []

    for span in block.spans:
        span_key = (block.block_id, span.span_id)
        if span_key in cross_block_cell_groups:
            flush_current_group()
            current_key = None
            for refs in cross_block_cell_groups[span_key]:
                tagged_spans = _build_tagged_spans_from_refs(refs)
                plain_text = _join_cross_block_cell_text(refs)
                if tagged_spans and plain_text:
                    groups.append(
                        _TaggedSpanGroup(
                            original_block_id=refs[0].block.block_id,
                            spans=tagged_spans,
                            plain_text=plain_text,
                        )
                    )
            continue
        if span_key in grouped_span_keys:
            flush_current_group()
            current_key = None
            continue
        if span.is_preserved:
            flush_current_group()
            current_key = None
            continue
        text = _normalize_span_text(span.text)
        if not text or "(cid:" in text:
            flush_current_group()
            current_key = None
            continue
        if span.table_cell_bbox is not None:
            key: tuple[str, int | tuple[float, float, float, float]] = (
                "cell",
                _table_cell_bbox_key(span.table_cell_bbox),
            )
        else:
            key = ("span", span.span_id)
        if current_key is not None and key != current_key:
            flush_current_group()
        current_key = key
        current_group.append(span)
    flush_current_group()

    return groups


def _build_row_preserving_span_groups(block: TextBlock) -> List[_TaggedSpanGroup]:
    groups: List[_TaggedSpanGroup] = []
    for span in block.spans:
        tagged_spans = _build_tagged_spans([span], block_id=block.block_id)
        if not tagged_spans:
            continue
        groups.append(_TaggedSpanGroup(original_block_id=block.block_id, spans=tagged_spans))
    return groups


def _build_cross_block_table_cell_groups(
    blocks: Sequence[TextBlock],
) -> dict[tuple[int, int], list[tuple[_SpanRef, ...]]]:
    """收集同一页内跨 TextBlock 的同一表格单元格。

    pdfminer 经常把一个视觉单元格里的多行中文拆成多个 TextBlock。若不在翻译前
    合并，译文会分别回填到同一个 cell bbox，最终在渲染阶段叠在一起。
    """

    refs_by_cell: dict[tuple[float, float, float, float], list[_SpanRef]] = {}
    for block in blocks:
        if getattr(block, "translation_mode", "span") != "span":
            continue
        label = (block.layout_label or block.layout_type or "").strip().lower()
        if label != "table":
            continue
        for span in block.spans:
            if span.table_cell_bbox is None:
                continue
            if not _is_translatable_span(span):
                continue
            key = _table_cell_bbox_key(span.table_cell_bbox)
            refs_by_cell.setdefault(key, []).append(_SpanRef(block=block, span=span))

    groups_by_first_span: dict[tuple[int, int], list[tuple[_SpanRef, ...]]] = {}
    for refs in refs_by_cell.values():
        if len(refs) < 2:
            continue
        block_ids = {ref.block.block_id for ref in refs}
        if len(block_ids) < 2:
            continue
        if not _is_compact_cross_block_table_cell(refs):
            continue
        ordered_refs = tuple(sorted(refs, key=_span_ref_reading_order_key))
        first_ref = ordered_refs[0]
        groups_by_first_span.setdefault((first_ref.block.block_id, first_ref.span.span_id), []).append(ordered_refs)

    for groups in groups_by_first_span.values():
        groups.sort(key=lambda refs: _span_ref_reading_order_key(refs[0]))
    return groups_by_first_span


def _build_tagged_spans_from_refs(refs: Sequence[_SpanRef]) -> List[TaggedSpan]:
    tagged_spans: List[TaggedSpan] = []
    for index, ref in enumerate(refs):
        text = _normalize_span_text(ref.span.text)
        if not text:
            continue
        tagged_spans.append(
            TaggedSpan(
                marker=f"SPAN_{index}",
                original_span_id=ref.span.span_id,
                text=text,
                original_block_id=ref.block.block_id,
            )
        )
    return tagged_spans


def _is_compact_cross_block_table_cell(refs: Sequence[_SpanRef]) -> bool:
    if len(refs) < 2:
        return False
    bbox = refs[0].span.table_cell_bbox
    if bbox is None:
        return False
    spans = [ref.span for ref in refs]
    return _is_compact_table_cell_bbox(bbox, spans)


def _is_compact_table_cell_bbox(bbox: BBox, spans: Sequence[TextSpan]) -> bool:
    if not spans:
        return False
    cell_height = bbox.y2 - bbox.y1
    if cell_height <= 0.0:
        return False
    span_top = max(span.bbox.y2 for span in spans)
    span_bottom = min(span.bbox.y1 for span in spans)
    span_band_height = max(span_top - span_bottom, 0.0)
    font_sizes = [span.font_size for span in spans if span.font_size and span.font_size > 0.0]
    median_font = median(font_sizes) if font_sizes else 10.0

    # 某些 D950 评论区只有外框，单元格推断会得到一个覆盖多条记录的巨大 bbox。
    # 这类 bbox 不能当作“同一个 cell 的多行文本”合并，否则会把多条记录挤到一起。
    max_reasonable_height = max(span_band_height * 3.0, median_font * 5.0)
    if cell_height > max_reasonable_height:
        return False

    centers = sorted((span.bbox.y1 + span.bbox.y2) / 2 for span in spans)
    if len(centers) > 1:
        max_gap = max(next_center - current for current, next_center in zip(centers, centers[1:]))
        if max_gap > max(median_font * 2.8, 18.0):
            return False
    return True


def _join_cross_block_cell_text(refs: Sequence[_SpanRef]) -> str:
    parts: list[str] = []
    for ref in refs:
        text = _normalize_span_text(ref.span.text)
        if not text:
            continue
        if parts and text == parts[-1]:
            continue
        parts.append(text)
    if not parts:
        return ""
    result = parts[0]
    for part in parts[1:]:
        if _should_join_without_space(result[-1], part[0]):
            result += part
        else:
            result += " " + part
    return result


def _is_translatable_span(span: TextSpan) -> bool:
    if span.is_preserved:
        return False
    text = _normalize_span_text(span.text)
    return bool(text and "(cid:" not in text)


def _span_ref_reading_order_key(ref: _SpanRef) -> tuple[float, float, int, int]:
    span = ref.span
    center_y = (span.bbox.y1 + span.bbox.y2) / 2
    return (-center_y, span.bbox.x1, ref.block.block_id, span.span_id)


def _should_join_without_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if _is_cjk_char(left) and _is_cjk_char(right):
        return True
    if right in "，。；：！？、）】》」』,.;:!?%)]}":
        return True
    if left in "（【《「『([{":
        return True
    return False


def _is_cjk_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _table_cell_bbox_key(bbox: BBox) -> tuple[float, float, float, float]:
    return (round(bbox.x1, 2), round(bbox.y1, 2), round(bbox.x2, 2), round(bbox.y2, 2))


_whitespace_re = re.compile(r"\s+")


def _normalize_span_text(text: str) -> str:
    """清洗 span 文本，避免换行等噪声影响语义。"""
    text = text.replace("\u00ad", "")  # 去除软断行
    normalized = _whitespace_re.sub(" ", text.strip())
    return normalized


def _render_block_line(block: TaggedTextBlock) -> str:
    if block.plain_text is not None:
        return f"{block.marker}{block.plain_text}"
    if len(block.spans) == 1:
        return f"{block.marker}{block.spans[0].text}"
    spans_text = "".join(f"<{span.marker}>{span.text}</{span.marker}>" for span in block.spans)
    return f"{block.marker}{spans_text}"


def _build_plain_text(spans: Sequence[TextSpan], *, preserve_line_breaks: bool = False) -> str:
    if not spans:
        return ""
    filtered_spans = [span for span in spans if not span.is_preserved]
    if not filtered_spans:
        return ""
    line_groups = _group_spans_into_lines(filtered_spans)
    lines: List[str] = []
    for group in line_groups:
        line_text = _join_spans_with_gaps(group)
        if line_text:
            lines.append(line_text)
    if not lines:
        return ""
    if preserve_line_breaks:
        return "\n".join(lines)
    if any(_is_list_line(line) for line in lines):
        return "\n".join(lines)
    return " ".join(lines)


def _group_spans_into_lines(spans: Sequence[TextSpan]) -> List[List[TextSpan]]:
    return group_spans_into_lines(spans)


def _join_spans_with_gaps(spans: Sequence[TextSpan]) -> str:
    ordered = sorted(spans, key=lambda span: span.bbox.x1)
    font_sizes = [span.font_size for span in ordered if span.font_size]
    median_font = median(font_sizes) if font_sizes else 10.0
    gap_threshold = median_font * 0.35
    parts: List[str] = []
    prev_x2: float | None = None
    for span in ordered:
        text = _normalize_span_text(span.text)
        if not text:
            continue
        if prev_x2 is not None:
            gap = span.bbox.x1 - prev_x2
            if gap > gap_threshold:
                parts.append(" ")
        parts.append(text)
        prev_x2 = span.bbox.x2
    return "".join(parts).strip()


_list_line_pattern = re.compile(r"^(\d+[.)]|[-•·])\s+")


def _is_list_line(text: str) -> bool:
    return bool(_list_line_pattern.match(text.strip()))
