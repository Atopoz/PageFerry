"""
PDF 文本处理 Part1: 段落提取
"""


import sys
import os
from contextlib import contextmanager

@contextmanager
def suppress_stderr():
    """临时抑制stderr输出的上下文管理器，用于过滤PDF处理库的警告信息"""
    with open(os.devnull, "w") as devnull:
        original_stderr = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = original_stderr


import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from statistics import median, pstdev
from typing import Dict, Iterable, List, Literal, Sequence, Tuple

import numpy as np
from loguru import logger
from rtree import index

from modules.pdf.constants import PDF_INVISIBLE_TEXT_RENDER_MODES, PDF_RASTER_SCALE
from vendor.pdfminerex.high_level import extract_pages
from vendor.pdfminerex.layout import LAParams, LTChar, LTCurve, LTFigure, LTLine, LTPage, LTRect, LTTextBox
from vendor.pdfminerex.pdfcolor import PDFColorSpace

from .entities import (
    BBox,
    DocumentLayout,
    LayoutResult,
    PageInfo,
    PreservedBlock,
    TextCharBox,
    TextBlock,
    TextSpan,
)
from .layout import LayoutDetector
from .layout_tools import calculate_overlap_ratio
from .rasterizer import PDFToImageConverter
from .span_line_utils import group_spans_into_lines, sort_spans_by_reading_order


LayoutBehavior = Literal["layout_block", "textbox_block", "preserve"]

LAYOUT_BEHAVIOR_LAYOUT: LayoutBehavior = "layout_block"
LAYOUT_BEHAVIOR_TEXTBOX: LayoutBehavior = "textbox_block"
LAYOUT_BEHAVIOR_PRESERVE: LayoutBehavior = "preserve"

LAYOUT_BEHAVIOR_MAP: Dict[str, LayoutBehavior] = {
    # 需要保持原 textbox 粒度的类型（表格、公式等）
    "table": LAYOUT_BEHAVIOR_TEXTBOX,
    "chart": LAYOUT_BEHAVIOR_TEXTBOX,
    "image": LAYOUT_BEHAVIOR_TEXTBOX,
    "display_formula": LAYOUT_BEHAVIOR_PRESERVE,
    "formula_number": LAYOUT_BEHAVIOR_TEXTBOX,
    # PageFerry 只跳过图片，不跳过页眉页脚里的原生文本。
    "header": LAYOUT_BEHAVIOR_LAYOUT,
    "footer": LAYOUT_BEHAVIOR_LAYOUT,
    "number": LAYOUT_BEHAVIOR_PRESERVE,
    "aside_text": LAYOUT_BEHAVIOR_PRESERVE,
    "vertical_text": LAYOUT_BEHAVIOR_PRESERVE,
    "table_of_contents": LAYOUT_BEHAVIOR_PRESERVE,
    "vision_footnote": LAYOUT_BEHAVIOR_PRESERVE,
    "algorithm": LAYOUT_BEHAVIOR_PRESERVE,
}

DEFAULT_LAYOUT_BEHAVIOR: LayoutBehavior = LAYOUT_BEHAVIOR_LAYOUT

PARAGRAPH_LABELS_DEFAULT: set[str] = {
    "text",
    "footnote",
    "paragraph_title",
    "doc_title",
    "figure_title",
    "abstract",
    "reference",
    "reference_content",
    "header",
    "footer",
}

TITLE_LABELS: set[str] = {"paragraph_title", "doc_title", "figure_title"}
REFERENCE_LABELS: set[str] = {"reference", "reference_content"}


@dataclass(frozen=True)
class ParagraphModeConfig:
    """段落模式配置。"""

    labels: set[str] = field(default_factory=lambda: set(PARAGRAPH_LABELS_DEFAULT))
    aggressiveness: Literal["conservative", "balanced", "aggressive"] = "balanced"
    max_font_ratio: float = 1.8
    tiny_text_max_len: int = 2
    short_line_width_ratio: float = 0.4


@dataclass(frozen=True)
class _TableRuling:
    """表格横竖线段，坐标使用 PDF 坐标系。"""

    orientation: Literal["h", "v"]
    x1: float
    y1: float
    x2: float
    y2: float


def get_layout_behavior(label: str) -> LayoutBehavior:
    """获取 layout label 对应的行为策略"""
    return LAYOUT_BEHAVIOR_MAP.get(label, DEFAULT_LAYOUT_BEHAVIOR)


class ParagraphExtractor:
    def __init__(
        self,
        pdf_path: str,
        *,
        layout_detector: LayoutDetector | None = None,
        pdf_to_image_converter_cls: type[PDFToImageConverter] | None = None,
        paragraph_mode_config: ParagraphModeConfig | None = None,
    ) -> None:
        """初始化 PDF 提取器，允许在测试中注入依赖"""
        self.pdf_path = pdf_path
        self._layout_detector = layout_detector or LayoutDetector()
        self._pdf_to_image_converter_cls = pdf_to_image_converter_cls or PDFToImageConverter
        self._paragraph_mode_config = paragraph_mode_config or ParagraphModeConfig()
        self.layout_fallback_used = False


    async def extract_page_info_with_layout(self) -> List[PageInfo]:
        """提取页面信息（layout 优先策略）"""
        # 1. layout布局检测
        layout_results = await self._detect_layout(self.pdf_path)

        # Log summary info for layout detection
        total_layouts = sum(len(doc_layout.layouts) for doc_layout in layout_results)
        filtered_layouts = sum(sum(1 for l in doc_layout.layouts if l.is_filtered) for doc_layout in layout_results)
        logger.info(f"Layout detection completed: {len(layout_results)} pages processed, {total_layouts - filtered_layouts} valid regions detected ({filtered_layouts} filtered).")

        layout_by_page: Dict[int, List[LayoutResult]] = {
            doc_layout.page_index: [layout for layout in doc_layout.layouts if not layout.is_filtered]
            for doc_layout in layout_results
        }

        laparams = LAParams(
            line_margin=0.45,
            char_margin=1.0,
            word_margin=0.1,
            boxes_flow=None,
            # 允许对 Form XObject / Figure 内文本做布局分析，否则图内文字不会生成 LTTextBox。
            all_texts=True,
        )

        pages: List[PageInfo] = []

        # with suppress_stderr():
        for page_number, page in enumerate(extract_pages(self.pdf_path, laparams=laparams)):
            # 2. 提取所有 textlines (span 级别)
            all_spans = self._extract_all_textlines(page)

            # 3. 获取当前页 layout 结果，并按行为分类
            page_layouts = self._prune_container_layouts(layout_by_page.get(page_number, []))
            layout_groups = self._split_layouts_by_behavior(page_layouts)
            matching_layouts = layout_groups[LAYOUT_BEHAVIOR_LAYOUT]
            textbox_layouts = layout_groups[LAYOUT_BEHAVIOR_TEXTBOX]
            preserve_layouts = layout_groups[LAYOUT_BEHAVIOR_PRESERVE]

            # 4. 标记 span 的 layout 归属
            all_spans = self._mark_span_layouts(
                all_spans,
                matching_layouts,
                textbox_layouts,
                preserve_layouts,
                overlap_threshold=0.3,
            )
            self._assign_table_cell_bboxes(
                all_spans,
                [layout for layout in textbox_layouts if layout.label == "table"],
                page,
            )
            all_spans = self._split_table_spans_by_grid(
                all_spans,
                [layout for layout in textbox_layouts if layout.label == "table"],
                page,
            )

            # 6. 根据 span 的 layout 标记进行分组
            matched_spans, unmatched_spans = self._group_spans_by_layout_mark(all_spans)

            # 7. 构建 TextBlock
            text_blocks: List[TextBlock] = []

            # 7.1 Layout 聚合块
            for layout_idx, spans in matched_spans.items():
                if not spans:
                    continue
                layout = matching_layouts[layout_idx]
                ordered_spans = self._sort_spans_by_reading_order(spans)
                text_blocks.append(
                    TextBlock(
                        block_id=len(text_blocks),
                        bbox=layout.bbox,
                        text="".join(s.text for s in ordered_spans),
                        spans=ordered_spans,
                        source="layout",
                        layout_type=layout.label,
                        layout_label=layout.label,
                        layout_score=layout.score,
                    )
                )

            # 7.2 pdfminer TextBox 聚合块
            unmatched_blocks = self._group_unmatched_spans_by_textbox(
                unmatched_spans,
                start_block_id=len(text_blocks),
            )
            text_blocks.extend(unmatched_blocks)

            self._apply_paragraph_mode(text_blocks, self._paragraph_mode_config)
            self._assign_block_dominant_colors(text_blocks)
            self._assign_block_dominant_bold_flags(text_blocks)

            text_blocks, preserved_blocks = self._partition_preserved_blocks(text_blocks)

            pages.append(
                PageInfo(
                    page_index=page_number,
                    texts=text_blocks,
                    preserved_texts=preserved_blocks,
                )
            )

        return pages

    @staticmethod
    def _split_layouts_by_behavior(page_layouts: Sequence[LayoutResult]) -> Dict[LayoutBehavior, List[LayoutResult]]:
        """按照行为策略划分 layout 结果"""
        groups: Dict[LayoutBehavior, List[LayoutResult]] = {
            LAYOUT_BEHAVIOR_LAYOUT: [],
            LAYOUT_BEHAVIOR_TEXTBOX: [],
            LAYOUT_BEHAVIOR_PRESERVE: [],
        }
        for layout in page_layouts:
            behavior = get_layout_behavior(layout.label)
            groups[behavior].append(layout)
        return groups

    @staticmethod
    def _prune_container_layouts(page_layouts: Sequence[LayoutResult]) -> List[LayoutResult]:
        """移除被更细粒度子区域覆盖的大容器布局，避免同一区域重复翻译。"""
        if not page_layouts:
            return []

        reference_contents = [layout for layout in page_layouts if layout.label == "reference_content"]
        if not reference_contents:
            return list(page_layouts)

        pruned: list[LayoutResult] = []
        for layout in page_layouts:
            if layout.label != "reference":
                pruned.append(layout)
                continue
            nested_count = sum(
                1
                for child in reference_contents
                if calculate_overlap_ratio(child.bbox.bounds, layout.bbox.bounds) >= 0.9
            )
            if nested_count >= 2:
                continue
            pruned.append(layout)
        return pruned

    def _extract_all_textlines(self, page: LTPage) -> List[TextSpan]:
        """提取页面中所有的 LTTextLine（span 级别）"""
        all_spans: List[TextSpan] = []

        global_span_id = 0

        def iter_textboxes(container: LTPage | LTFigure) -> Iterable[LTTextBox]:
            for item in container:
                if isinstance(item, LTTextBox):
                    yield item
                elif isinstance(item, LTFigure):
                    yield from iter_textboxes(item)

        for textbox_id, component in enumerate(iter_textboxes(page)):
            textbox_bbox = BBox(*component.bbox)

            for span in component:
                sources = span.get_source_ranges()
                ops: list[bytes] = []
                ops_paths: list[tuple[str, ...]] = []
                for source in sources:
                    text_block = getattr(source, "text_block", None)
                    if text_block is None:
                        continue
                    ops.append(text_block.as_pdf_bytes())
                    ops_paths.append(getattr(text_block, "xobject_path", ()) or ())
                chars = [c for c in span if isinstance(c, LTChar)]
                if not chars:
                    continue

                span_text = span.get_text()
                if not span_text:
                    global_span_id += 1
                    continue

                current_bbox = BBox(*span.bbox)

                # 提取字体信息
                font_name = _get_most_common_font_name(chars)
                font_size = _get_most_common_font_size(chars)
                span_is_bold = _get_most_common_bold_flag(chars)
                span_fill_color, span_stroke_color, span_render_mode = _get_most_common_text_style(chars)
                span_is_visible, span_has_transparency = _get_span_text_visibility(chars)

                # 检查是否需要保留
                should_preserve, span_text_value = _check_span_is_preserved(span_text)
                # invisible/transparent OCR search layer 不能被翻译后重绘为不透明文字。
                should_preserve = (
                    should_preserve
                    or not span_is_visible
                    or span_has_transparency
                )
                char_boxes = [
                    TextCharBox(char=char.get_text(), bbox=BBox(*char.bbox))
                    for char in chars
                    if char.get_text()
                ]

                all_spans.append(
                    TextSpan(
                        span_id=global_span_id,
                        bbox=current_bbox,
                        text=span_text_value,
                        font_name=font_name,
                        font_size=font_size,
                        is_bold=span_is_bold,
                        color=span_fill_color,
                        stroke_color=span_stroke_color,
                        text_render_mode=span_render_mode,
                        is_visible=span_is_visible,
                        has_transparency=span_has_transparency,
                        translated_text=span_text_value if should_preserve else "",
                        source_textbox_id=textbox_id,
                        source_textbox_bbox=textbox_bbox,
                        ops=ops,
                        ops_xobject_paths=ops_paths,
                        char_boxes=char_boxes,
                        is_preserved=should_preserve,
                    )
                )
                global_span_id += 1

        return all_spans

    def _mark_span_layouts(
        self,
        spans: List[TextSpan],
        matching_layouts: List[LayoutResult],
        textbox_layouts: List[LayoutResult],
        preserved_layouts: List[LayoutResult],
        overlap_threshold: float = 0.3,
    ) -> List[TextSpan]:
        """为每个 span 标记它属于哪个 layout，并记录行为类别"""

        matching_idx = self._build_rtree_index(matching_layouts)
        textbox_idx = self._build_rtree_index(textbox_layouts)
        preserved_idx = self._build_rtree_index(preserved_layouts)

        for span in spans:
            span.layout_label = None
            span.layout_behavior = None
            span.matched_layout = None
            span.matched_layout_idx = None

            match = self._match_span_to_layouts(
                span,
                matching_layouts,
                matching_idx,
                overlap_threshold,
            )
            if match is not None:
                layout_idx, layout = match
                span.layout_label = layout.label
                span.layout_behavior = LAYOUT_BEHAVIOR_LAYOUT
                span.matched_layout = layout
                span.matched_layout_idx = layout_idx
                # layout 聚合区域默认参与翻译，不修改 is_preserved
                continue

            match = self._match_span_to_layouts(
                span,
                textbox_layouts,
                textbox_idx,
                overlap_threshold,
            )
            if match is not None:
                _, layout = match
                span.layout_label = layout.label
                span.layout_behavior = LAYOUT_BEHAVIOR_TEXTBOX
                span.matched_layout = layout
                continue

            match = self._match_span_to_layouts(
                span,
                preserved_layouts,
                preserved_idx,
                overlap_threshold,
            )
            if match is not None:
                _, layout = match
                span.layout_label = layout.label
                span.layout_behavior = LAYOUT_BEHAVIOR_PRESERVE
                span.matched_layout = layout
                span.is_preserved = True

        return spans

    @staticmethod
    def _build_rtree_index(layouts: Sequence[LayoutResult]) -> index.Index | None:
        """为给定 layout 构建 rtree 索引，便于快速匹配"""
        if not layouts:
            return None
        tree = index.Index()
        for idx, layout in enumerate(layouts):
            tree.insert(idx, layout.bbox.bounds)
        return tree

    @staticmethod
    def _match_span_to_layouts(
        span: TextSpan,
        layouts: Sequence[LayoutResult],
        tree: index.Index | None,
        overlap_threshold: float,
    ) -> Tuple[int, LayoutResult] | None:
        """在指定 layout 列表中寻找与 span 重叠度最高的候选"""
        if not layouts or tree is None:
            return None

        best_idx: int | None = None
        best_overlap = 0.0
        span_bounds = span.bbox.bounds

        for layout_idx in tree.intersection(span_bounds):
            layout = layouts[layout_idx]
            overlap_ratio = calculate_overlap_ratio(span_bounds, layout.bbox.bounds)
            if overlap_ratio > best_overlap:
                best_overlap = overlap_ratio
                best_idx = layout_idx

        if best_idx is not None and best_overlap >= overlap_threshold:
            return best_idx, layouts[best_idx]
        return None

    def _assign_table_cell_bboxes(
        self,
        spans: Sequence[TextSpan],
        table_layouts: Sequence[LayoutResult],
        page: LTPage,
    ) -> None:
        """基于 PDF 原生框线为 table span 推断所在单元格 bbox。"""
        if not spans or not table_layouts:
            return

        page_rulings = self._extract_table_rulings(page)
        if not page_rulings:
            return

        for layout in table_layouts:
            expanded_layout_bbox = self._expand_bbox(layout.bbox, 2.0)
            layout_rulings = [
                ruling
                for ruling in page_rulings
                if self._ruling_intersects_bbox(ruling, expanded_layout_bbox)
            ]
            if not layout_rulings:
                continue

            layout_spans: list[TextSpan] = []
            for span in spans:
                if (span.layout_label or "").strip().lower() != "table":
                    continue
                if calculate_overlap_ratio(span.bbox.bounds, layout.bbox.bounds) < 0.3:
                    continue
                layout_spans.append(span)
                cell_bbox = self._resolve_span_table_cell_bbox(
                    span,
                    layout.bbox,
                    layout_rulings,
                )
                if cell_bbox is not None:
                    span.table_cell_bbox = cell_bbox
            self._drop_unreliable_table_cell_bboxes(layout_spans, layout_rulings)

    def _split_table_spans_by_grid(
        self,
        spans: Sequence[TextSpan],
        table_layouts: Sequence[LayoutResult],
        page: LTPage,
    ) -> List[TextSpan]:
        """在高置信框线表格内，把跨多列 textline 拆成 cell 级 span。"""

        if not spans or not table_layouts:
            return list(spans)

        page_rulings = self._extract_table_rulings(page)
        if not page_rulings:
            return list(spans)

        table_layout_rulings: list[tuple[LayoutResult, list[_TableRuling]]] = []
        for layout in table_layouts:
            expanded_layout_bbox = self._expand_bbox(layout.bbox, 2.0)
            layout_rulings = [
                ruling
                for ruling in page_rulings
                if self._ruling_intersects_bbox(ruling, expanded_layout_bbox)
            ]
            if layout_rulings:
                table_layout_rulings.append((layout, layout_rulings))
        if not table_layout_rulings:
            return list(spans)

        next_span_id = max((span.span_id for span in spans), default=-1) + 1
        split_spans: list[TextSpan] = []
        for span in spans:
            span_label = (span.layout_label or "").strip().lower()
            if span_label != "table" or not span.char_boxes:
                split_spans.append(span)
                continue

            replacements: list[TextSpan] | None = None
            for layout, rulings in table_layout_rulings:
                if calculate_overlap_ratio(span.bbox.bounds, layout.bbox.bounds) < 0.3:
                    continue
                replacements = self._split_span_by_table_grid(
                    span,
                    layout.bbox,
                    rulings,
                    next_span_id,
                )
                if replacements:
                    break

            if not replacements:
                split_spans.append(span)
                continue

            split_spans.extend(replacements)
            next_span_id += len(replacements)

        return split_spans

    @classmethod
    def _extract_table_rulings(cls, page: LTPage) -> list[_TableRuling]:
        rulings: list[_TableRuling] = []

        def iter_curves(container: LTPage | LTFigure) -> Iterable[LTCurve]:
            for item in container:
                if isinstance(item, LTFigure):
                    yield from iter_curves(item)
                elif isinstance(item, (LTLine, LTRect, LTCurve)):
                    yield item

        for item in iter_curves(page):
            if isinstance(item, LTRect) and getattr(item, "fill", False):
                filled_rect_ruling = cls._build_ruling_from_filled_rect(item)
                if filled_rect_ruling is not None:
                    rulings.append(filled_rect_ruling)
                    continue
            if not getattr(item, "stroke", False):
                continue
            linewidth = float(getattr(item, "linewidth", 0.0) or 0.0)
            tolerance = max(0.75, linewidth * 2.0)
            min_length = max(2.0, linewidth * 3.0)
            for p0, p1 in cls._iter_curve_edges(item):
                ruling = cls._build_ruling_from_edge(
                    p0,
                    p1,
                    tolerance=tolerance,
                    min_length=min_length,
                )
                if ruling is not None:
                    rulings.append(ruling)
        return rulings

    @staticmethod
    def _build_ruling_from_filled_rect(item: LTRect) -> _TableRuling | None:
        x0, y0, x1, y1 = (float(value) for value in item.bbox)
        width = x1 - x0
        height = y1 - y0
        if width <= 0.0 or height <= 0.0:
            return None
        thin_threshold = 1.25
        min_length = 2.0
        if height <= thin_threshold and width >= min_length:
            y = (y0 + y1) / 2
            return _TableRuling("h", x0, y, x1, y)
        if width <= thin_threshold and height >= min_length:
            x = (x0 + x1) / 2
            return _TableRuling("v", x, y0, x, y1)
        return None

    @staticmethod
    def _iter_curve_edges(item: LTCurve) -> Iterable[tuple[tuple[float, float], tuple[float, float]]]:
        pts = [(float(x), float(y)) for x, y in getattr(item, "pts", [])]
        if len(pts) < 2:
            return

        if isinstance(item, LTRect):
            yield pts[0], pts[1]
            yield pts[1], pts[2]
            yield pts[2], pts[3]
            yield pts[3], pts[0]
            return

        if isinstance(item, LTLine):
            yield pts[0], pts[1]
            return

        for start, end in zip(pts, pts[1:]):
            yield start, end

    @staticmethod
    def _build_ruling_from_edge(
        p0: tuple[float, float],
        p1: tuple[float, float],
        *,
        tolerance: float,
        min_length: float,
    ) -> _TableRuling | None:
        x0, y0 = p0
        x1, y1 = p1
        if abs(y1 - y0) <= tolerance and abs(x1 - x0) >= min_length:
            y = (y0 + y1) / 2
            return _TableRuling("h", min(x0, x1), y, max(x0, x1), y)
        if abs(x1 - x0) <= tolerance and abs(y1 - y0) >= min_length:
            x = (x0 + x1) / 2
            return _TableRuling("v", x, min(y0, y1), x, max(y0, y1))
        return None

    @staticmethod
    def _ruling_intersects_bbox(ruling: _TableRuling, bbox: BBox, tolerance: float = 1.5) -> bool:
        if ruling.orientation == "h":
            return (
                bbox.y1 - tolerance <= ruling.y1 <= bbox.y2 + tolerance
                and ruling.x2 >= bbox.x1 - tolerance
                and ruling.x1 <= bbox.x2 + tolerance
            )
        return (
            bbox.x1 - tolerance <= ruling.x1 <= bbox.x2 + tolerance
            and ruling.y2 >= bbox.y1 - tolerance
            and ruling.y1 <= bbox.y2 + tolerance
        )

    @classmethod
    def _resolve_span_table_cell_bbox(
        cls,
        span: TextSpan,
        table_bbox: BBox,
        rulings: Sequence[_TableRuling],
    ) -> BBox | None:
        center_x = (span.bbox.x1 + span.bbox.x2) / 2
        center_y = (span.bbox.y1 + span.bbox.y2) / 2
        coverage_tolerance = max(2.0, (span.bbox.y2 - span.bbox.y1) * 0.35)

        vertical_positions = cls._merge_axis_positions(
            [
                ruling.x1
                for ruling in rulings
                if ruling.orientation == "v"
                and ruling.y1 <= center_y + coverage_tolerance
                and ruling.y2 >= center_y - coverage_tolerance
                and table_bbox.x1 - 2.0 <= ruling.x1 <= table_bbox.x2 + 2.0
            ],
            tolerance=1.5,
        )
        if len(vertical_positions) < 3:
            return None

        x_bounds = cls._find_axis_interval(vertical_positions, center_x, tolerance=1.5)
        if x_bounds is None:
            return None
        left, right = x_bounds
        if cls._looks_like_multi_cell_text_span(span, left, right, vertical_positions):
            return None

        horizontal_positions = cls._collect_horizontal_positions_for_column(
            rulings,
            table_bbox,
            left,
            right,
        )
        y_bounds = cls._find_axis_interval(horizontal_positions, center_y, tolerance=1.5)
        if y_bounds is None:
            return None
        bottom, top = y_bounds

        width = right - left
        height = top - bottom
        if width <= 1.0 or height <= 0.0:
            return None

        return cls._build_padded_cell_bbox(left, right, bottom, top)

    @classmethod
    def _split_span_by_table_grid(
        cls,
        span: TextSpan,
        table_bbox: BBox,
        rulings: Sequence[_TableRuling],
        start_span_id: int,
    ) -> list[TextSpan] | None:
        """基于表格网格把一个跨多列 span 拆成多个单元格 span。"""

        if not span.char_boxes:
            return None

        center_y = (span.bbox.y1 + span.bbox.y2) / 2
        coverage_tolerance = max(2.0, (span.bbox.y2 - span.bbox.y1) * 0.35)
        vertical_positions = cls._merge_axis_positions(
            [
                ruling.x1
                for ruling in rulings
                if ruling.orientation == "v"
                and ruling.y1 <= center_y + coverage_tolerance
                and ruling.y2 >= center_y - coverage_tolerance
                and table_bbox.x1 - 2.0 <= ruling.x1 <= table_bbox.x2 + 2.0
            ],
            tolerance=1.5,
        )
        if len(vertical_positions) < 3:
            return None

        center_x = (span.bbox.x1 + span.bbox.x2) / 2
        horizontal_positions = cls._merge_axis_positions(
            [
                ruling.y1
                for ruling in rulings
                if ruling.orientation == "h"
                and ruling.x1 <= center_x + 2.0
                and ruling.x2 >= center_x - 2.0
                and table_bbox.y1 - 2.0 <= ruling.y1 <= table_bbox.y2 + 2.0
            ],
            tolerance=1.5,
        )
        y_bounds = cls._find_axis_interval(horizontal_positions, center_y, tolerance=1.5)
        if y_bounds is None:
            return None
        bottom, top = y_bounds

        assignable_chars = [char_box for char_box in span.char_boxes if char_box.char.strip()]
        if len(assignable_chars) < 2:
            return None
        grouped_chars: dict[tuple[float, float], list[TextCharBox]] = {}
        for char_box in span.char_boxes:
            if not char_box.char:
                continue
            char_center_x = (char_box.bbox.x1 + char_box.bbox.x2) / 2
            char_center_y = (char_box.bbox.y1 + char_box.bbox.y2) / 2
            if not (bottom - 1.5 <= char_center_y <= top + 1.5):
                continue
            x_bounds = cls._find_axis_interval(vertical_positions, char_center_x, tolerance=1.5)
            if x_bounds is None:
                continue
            grouped_chars.setdefault(x_bounds, []).append(char_box)

        assigned_count = sum(
            1
            for items in grouped_chars.values()
            for char_box in items
            if char_box.char.strip()
        )
        if assigned_count / len(assignable_chars) < 0.8:
            return None
        if len(grouped_chars) < 2:
            return None

        replacements: list[TextSpan] = []
        for offset, ((left, right), char_boxes) in enumerate(sorted(grouped_chars.items())):
            text = cls._join_char_boxes_for_table_cell(char_boxes, span.font_size)
            if not text:
                continue
            should_preserve, processed_text = _check_span_is_preserved(text)
            should_preserve = (
                should_preserve or not span.is_visible or span.has_transparency
            )
            char_bbox = cls._bbox_from_char_boxes(char_boxes)
            replacements.append(
                TextSpan(
                    span_id=start_span_id + offset,
                    bbox=char_bbox,
                    text=processed_text,
                    translated_text=processed_text if should_preserve else "",
                    adjusted_font_size=span.adjusted_font_size,
                    font_name=span.font_name,
                    font_size=span.font_size,
                    is_bold=span.is_bold,
                    color=span.color,
                    stroke_color=span.stroke_color,
                    text_render_mode=span.text_render_mode,
                    is_visible=span.is_visible,
                    has_transparency=span.has_transparency,
                    ops=list(span.ops),
                    ops_xobject_paths=list(span.ops_xobject_paths),
                    char_boxes=list(char_boxes),
                    source_textbox_id=span.source_textbox_id,
                    source_textbox_bbox=span.source_textbox_bbox,
                    table_cell_bbox=cls._build_padded_cell_bbox(left, right, bottom, top),
                    layout_label=span.layout_label,
                    layout_behavior=span.layout_behavior,
                    matched_layout=span.matched_layout,
                    matched_layout_idx=span.matched_layout_idx,
                    is_preserved=should_preserve,
                )
            )

        return replacements if len(replacements) >= 2 else None

    @classmethod
    def _drop_unreliable_table_cell_bboxes(
        cls,
        spans: Sequence[TextSpan],
        rulings: Sequence[_TableRuling],
    ) -> None:
        groups: dict[tuple[float, float, float, float], list[TextSpan]] = {}
        for span in spans:
            if span.table_cell_bbox is None:
                continue
            groups.setdefault(cls._table_cell_bbox_key(span.table_cell_bbox), []).append(span)

        for cell_spans in groups.values():
            if len(cell_spans) < 2:
                continue
            bbox = cell_spans[0].table_cell_bbox
            if bbox is None:
                continue
            if not cls._looks_like_unbounded_row_label_group(bbox, cell_spans, rulings):
                continue
            for span in cell_spans:
                span.table_cell_bbox = None

    @classmethod
    def _looks_like_unbounded_row_label_group(
        cls,
        cell_bbox: BBox,
        spans: Sequence[TextSpan],
        rulings: Sequence[_TableRuling],
    ) -> bool:
        line_centers = cls._cluster_span_line_centers(spans)
        if len(line_centers) < 4:
            return False

        internal_separators = [
            ruling
            for ruling in rulings
            if ruling.orientation == "h"
            and cell_bbox.y1 + 1.5 < ruling.y1 < cell_bbox.y2 - 1.5
            and cls._horizontal_ruling_covers_interval(ruling, cell_bbox.x1, cell_bbox.x2)
        ]
        if internal_separators:
            return False

        return True

    @classmethod
    def _cluster_span_line_centers(cls, spans: Sequence[TextSpan]) -> list[float]:
        centers = sorted((span.bbox.y1 + span.bbox.y2) / 2 for span in spans)
        if not centers:
            return []
        median_font = cls._median_span_font_size(spans)
        tolerance = max(median_font * 0.55, 1.5)
        clusters: list[list[float]] = [[centers[0]]]
        for center in centers[1:]:
            current = clusters[-1]
            current_center = sum(current) / len(current)
            if abs(center - current_center) <= tolerance:
                current.append(center)
            else:
                clusters.append([center])
        return [sum(cluster) / len(cluster) for cluster in clusters]

    @staticmethod
    def _median_span_font_size(spans: Sequence[TextSpan]) -> float:
        sizes = [span.font_size for span in spans if span.font_size and span.font_size > 0.0]
        if sizes:
            return median(sizes)
        heights = [span.bbox.y2 - span.bbox.y1 for span in spans if span.bbox.y2 > span.bbox.y1]
        return median(heights) if heights else 10.0

    @classmethod
    def _collect_horizontal_positions_for_column(
        cls,
        rulings: Sequence[_TableRuling],
        table_bbox: BBox,
        left: float,
        right: float,
    ) -> list[float]:
        return cls._merge_axis_positions(
            [
                ruling.y1
                for ruling in rulings
                if ruling.orientation == "h"
                and cls._horizontal_ruling_covers_interval(ruling, left, right)
                and table_bbox.y1 - 2.0 <= ruling.y1 <= table_bbox.y2 + 2.0
            ],
            tolerance=1.5,
        )

    @staticmethod
    def _horizontal_ruling_covers_interval(
        ruling: _TableRuling,
        left: float,
        right: float,
        tolerance: float = 2.0,
    ) -> bool:
        return ruling.x1 <= left + tolerance and ruling.x2 >= right - tolerance

    @staticmethod
    def _table_cell_bbox_key(bbox: BBox) -> tuple[float, float, float, float]:
        return (
            round(bbox.x1, 2),
            round(bbox.y1, 2),
            round(bbox.x2, 2),
            round(bbox.y2, 2),
        )

    @staticmethod
    def _build_padded_cell_bbox(left: float, right: float, bottom: float, top: float) -> BBox:
        width = right - left
        height = top - bottom
        padding_x = min(2.0, max(width * 0.03, 0.5))
        padding_y = min(1.0, max(height * 0.05, 0.2))
        padded_left = left + padding_x
        padded_right = right - padding_x
        padded_bottom = bottom + padding_y
        padded_top = top - padding_y
        if padded_right <= padded_left or padded_top <= padded_bottom:
            return BBox(left, bottom, right, top)
        return BBox(padded_left, padded_bottom, padded_right, padded_top)

    @staticmethod
    def _join_char_boxes_for_table_cell(char_boxes: Sequence[TextCharBox], font_size: float | None) -> str:
        ordered = sorted(char_boxes, key=lambda item: item.bbox.x1)
        widths = [max(item.bbox.x2 - item.bbox.x1, 0.0) for item in ordered]
        median_width = median([width for width in widths if width > 0.0]) if any(width > 0.0 for width in widths) else 0.0
        gap_threshold = max(median_width * 0.8, (font_size or 0.0) * 0.22, 1.2)
        parts: list[str] = []
        prev_right: float | None = None
        for char_box in ordered:
            char = char_box.char
            if not char:
                continue
            if prev_right is not None:
                gap = char_box.bbox.x1 - prev_right
                if gap > gap_threshold and parts and not parts[-1].endswith(" "):
                    parts.append(" ")
            parts.append(char)
            prev_right = char_box.bbox.x2
        return "".join(parts).strip()

    @staticmethod
    def _bbox_from_char_boxes(char_boxes: Sequence[TextCharBox]) -> BBox:
        return BBox(
            min(item.bbox.x1 for item in char_boxes),
            min(item.bbox.y1 for item in char_boxes),
            max(item.bbox.x2 for item in char_boxes),
            max(item.bbox.y2 for item in char_boxes),
        )

    @staticmethod
    def _merge_axis_positions(values: Sequence[float], *, tolerance: float) -> list[float]:
        if not values:
            return []
        ordered = sorted(float(value) for value in values)
        groups: list[list[float]] = [[ordered[0]]]
        for value in ordered[1:]:
            current_group = groups[-1]
            if abs(value - current_group[-1]) <= tolerance:
                current_group.append(value)
            else:
                groups.append([value])
        return [sum(group) / len(group) for group in groups]

    @staticmethod
    def _find_axis_interval(
        positions: Sequence[float],
        coordinate: float,
        *,
        tolerance: float,
    ) -> tuple[float, float] | None:
        if len(positions) < 2:
            return None
        ordered = sorted(positions)
        for lower, upper in zip(ordered, ordered[1:]):
            if upper - lower <= tolerance:
                continue
            if lower - tolerance <= coordinate <= upper + tolerance:
                return lower, upper
        return None

    @staticmethod
    def _looks_like_multi_cell_text_span(
        span: TextSpan,
        cell_left: float,
        cell_right: float,
        vertical_positions: Sequence[float],
    ) -> bool:
        """避免把横跨多列的 LTTextLine 误绑定到中心点所在的小单元格。"""

        span_left = span.bbox.x1
        span_right = span.bbox.x2
        span_width = max(span_right - span_left, 0.0)
        cell_width = max(cell_right - cell_left, 0.0)
        if span_width <= 0.0 or cell_width <= 0.0:
            return False

        overflow_left = max(cell_left - span_left, 0.0)
        overflow_right = max(span_right - cell_right, 0.0)
        overflow_total = overflow_left + overflow_right
        font_size = span.font_size or max(span.bbox.y2 - span.bbox.y1, 0.0) or 10.0
        overflow_tolerance = max(cell_width * 0.35, font_size * 1.5, 2.0)
        if overflow_total <= overflow_tolerance:
            return False

        if overflow_left > overflow_tolerance and overflow_right > overflow_tolerance:
            return True

        if span_width > cell_width * 2.0:
            return True

        interior_rulings = [
            position
            for position in vertical_positions
            if span_left + 1.5 < position < span_right - 1.5
        ]
        return len(interior_rulings) >= 2 and span_width > cell_width * 1.35

    @staticmethod
    def _expand_bbox(bbox: BBox, amount: float) -> BBox:
        return BBox(
            bbox.x1 - amount,
            bbox.y1 - amount,
            bbox.x2 + amount,
            bbox.y2 + amount,
        )

    def _group_spans_by_layout_mark(
        self,
        spans: List[TextSpan]
    ) -> Tuple[Dict[int, List[TextSpan]], List[TextSpan]]:
        """
        根据 span 的 layout 标记进行分组

        Returns:
            - matched_spans: {layout_idx: [span1, span2, ...]}  # 匹配到 layout 聚合区域的 spans
            - unmatched_spans: 其他 spans（包括 table 以及完全未匹配的情况）
        """
        matched_spans: Dict[int, List[TextSpan]] = {}
        unmatched_spans: List[TextSpan] = []

        for span in spans:
            if span.matched_layout is not None and span.matched_layout_idx is not None:
                # 匹配到 matching_layout，按 layout_idx 分组
                if span.matched_layout_idx not in matched_spans:
                    matched_spans[span.matched_layout_idx] = []
                matched_spans[span.matched_layout_idx].append(span)
            else:
                # 未匹配或是需要按 textbox 处理（table 等）
                unmatched_spans.append(span)

        return matched_spans, unmatched_spans

    def _group_unmatched_spans_by_textbox(
        self,
        unmatched_spans: List[TextSpan],
        start_block_id: int,
    ) -> List[TextBlock]:
        """
        将未匹配的 spans 按 layout_label 和 textbox 分组，构建 TextBlock

        现在使用 span 级别的 matched_layout 标记（在 _mark_span_layouts 中已标记），
        避免一个 textbox 部分被匹配后，剩余部分标签混乱的问题。

        Args:
            unmatched_spans: 未匹配的 spans（包括 table 等情况）
            start_block_id: 起始 block_id

        Returns:
            TextBlock 列表
        """
        if not unmatched_spans:
            return []

        # 按 (layout_label, textbox_id) 二维分组
        # 这样同一个 textbox 内，不同 layout_label 的 spans 会分开
        from collections import defaultdict
        grouped_spans: Dict[Tuple[str | None, int], List[TextSpan]] = defaultdict(list)

        for span in unmatched_spans:
            # 使用 span 上已标记的 layout_label（在 _mark_span_layouts 中已设置）
            layout_label = span.layout_label
            textbox_id = span.source_textbox_id

            if textbox_id is not None:
                grouped_spans[(layout_label, textbox_id)].append(span)

        # 构建 TextBlock
        text_blocks: List[TextBlock] = []

        # 排序时处理 None 值：None 排在最后
        sorted_groups = sorted(
            grouped_spans.items(),
            key=lambda x: (x[0][0] or "zzz_none", x[0][1])  # label 为 None 时用 "zzz_none" 排在最后
        )

        for (layout_label, textbox_id), spans in sorted_groups:
            if not spans:
                continue

            ordered_spans = self._sort_spans_by_reading_order(spans)

            # 计算这组 spans 的 bbox
            x1 = min(s.bbox.x1 for s in ordered_spans)
            y1 = min(s.bbox.y1 for s in ordered_spans)
            x2 = max(s.bbox.x2 for s in ordered_spans)
            y2 = max(s.bbox.y2 for s in ordered_spans)
            block_bbox = BBox(x1, y1, x2, y2)

            # 合并文本
            text = "".join(s.text for s in ordered_spans)

            # 使用第一个 span 的 layout 信息
            layout_score = ordered_spans[0].matched_layout.score if ordered_spans[0].matched_layout else None

            text_blocks.append(
                TextBlock(
                    block_id=start_block_id + len(text_blocks),
                    bbox=block_bbox,
                    text=text,
                    spans=ordered_spans,
                    source="pdfminer",  # 标记来源为 pdfminer（保持独立）
                    layout_type=layout_label or "text",
                    layout_label=layout_label,  # table/number/None
                    layout_score=layout_score
                )
            )

        text_blocks = self._merge_inline_orphan_blocks(text_blocks)
        for idx, block in enumerate(text_blocks):
            block.block_id = start_block_id + idx
        return text_blocks

    def _merge_inline_orphan_blocks(self, blocks: Sequence[TextBlock]) -> List[TextBlock]:
        """合并与左侧主块同一行的短续接块，避免脚注/注释类文本被拆碎。"""
        if len(blocks) < 2:
            return list(blocks)

        ordered_blocks = sorted(
            blocks,
            key=lambda block: (-round(block.bbox.y2, 2), round(block.bbox.x1, 2)),
        )
        merged_blocks: list[TextBlock] = []

        for block in ordered_blocks:
            anchor = self._find_inline_merge_anchor(merged_blocks, block)
            if anchor is None:
                merged_blocks.append(block)
                continue
            anchor.spans = self._sort_spans_by_reading_order([*anchor.spans, *block.spans])
            anchor.bbox = BBox(
                min(anchor.bbox.x1, block.bbox.x1),
                min(anchor.bbox.y1, block.bbox.y1),
                max(anchor.bbox.x2, block.bbox.x2),
                max(anchor.bbox.y2, block.bbox.y2),
            )
            anchor.text = "".join(span.text for span in anchor.spans)

        return merged_blocks

    def _find_inline_merge_anchor(
        self,
        existing_blocks: Sequence[TextBlock],
        candidate: TextBlock,
    ) -> TextBlock | None:
        """查找可以吸收短续接块的左侧主块。"""
        if (candidate.layout_label or "").strip().lower() == "table":
            return None
        if len(candidate.spans) != 1:
            return None
        candidate_text = (candidate.text or "").strip()
        if not candidate_text or len(candidate_text) > 24:
            return None
        candidate_span = candidate.spans[0]
        if candidate_span.is_preserved:
            return None

        candidate_font_size = candidate_span.font_size or 0.0
        candidate_center_y = (candidate.bbox.y1 + candidate.bbox.y2) / 2
        baseline_tolerance = max(2.5, candidate_font_size * 0.6)
        # 只合并非常贴近的行内碎片。无框线表格的相邻列也常表现为不同 textbox，
        # 过大的 gap 容忍度会把相邻单元格误当成断裂短语合并。
        gap_tolerance = max(6.0, candidate_font_size * 0.9)

        for anchor in reversed(existing_blocks):
            if anchor.source != candidate.source:
                continue
            if anchor.layout_label != candidate.layout_label:
                continue
            if not anchor.spans or anchor.bbox.x2 > candidate.bbox.x1:
                continue
            if (candidate.bbox.x1 - anchor.bbox.x2) > gap_tolerance:
                continue

            anchor_lines = self._group_spans_into_lines(anchor.spans)
            if not anchor_lines:
                continue
            best_line = min(anchor_lines, key=lambda line: abs(line.center_y - candidate_center_y))
            if abs(best_line.center_y - candidate_center_y) > baseline_tolerance:
                continue

            anchor_font_sizes = [span.font_size for span in anchor.spans if span.font_size]
            if anchor_font_sizes:
                anchor_font = median(anchor_font_sizes)
                if abs(anchor_font - candidate_font_size) > max(1.5, anchor_font * 0.25):
                    continue

            return anchor

        return None

    def _partition_preserved_blocks(
        self,
        blocks: List[TextBlock],
    ) -> Tuple[List[TextBlock], List[PreservedBlock]]:
        """将需要保留的 block 与需要翻译的 block 分离"""
        preserved: List[PreservedBlock] = []
        translated: List[TextBlock] = []

        for block in blocks:
            # 判定保留逻辑：
            # 1. 包含 CID 的块（具有乱码风险）
            # 2. 根据 layout_behavior 标记为 PRESERVE 的块, 如 number、vision_footnote
            #    或 display_formula；header/footer 的原生文本统一参与翻译。
            # 3. Span 级别标记为 is_preserved 的块

            has_cid = any(_contains_cid_marker(s.text) for s in block.spans)
            should_preserve_by_policy = self._should_preserve_block(block)

            if has_cid or should_preserve_by_policy:
                 preserved.append(
                    PreservedBlock(
                        original_block_id=block.block_id,
                        bbox=block.bbox,
                        text=block.text,
                        spans=block.spans,
                        source=block.source,
                        layout_label=block.layout_label,
                        layout_score=block.layout_score,
                    )
                )
            else:
                translated.append(block)

        for idx, block in enumerate(translated):
            block.block_id = idx

        return translated, preserved

    @staticmethod
    def _should_preserve_block(block: TextBlock) -> bool:
        """判断一个 block 是否应该进入保留集合"""
        if block.layout_label and get_layout_behavior(block.layout_label) == LAYOUT_BEHAVIOR_PRESERVE:
            return True
        if any(
            not span.is_visible
            or span.has_transparency
            or span.text_render_mode in PDF_INVISIBLE_TEXT_RENDER_MODES
            for span in block.spans
        ):
            return True
        if block.spans and all(span.is_preserved for span in block.spans):
            return True
        if not block.spans and block.layout_label and get_layout_behavior(block.layout_label) == LAYOUT_BEHAVIOR_TEXTBOX:
            return False
        return False

    @staticmethod
    def _sort_spans_by_reading_order(spans: Sequence[TextSpan]) -> List[TextSpan]:
        """按阅读顺序（先上后下，再左到右）对 spans 排序"""
        return sort_spans_by_reading_order(spans)

    @staticmethod
    def _assign_block_dominant_colors(blocks: Sequence[TextBlock]) -> None:
        """为每个 block 计算主色与文字绘制样式（基于 span 加权众数）。"""
        for block in blocks:
            block.dominant_color = ParagraphExtractor._resolve_block_dominant_color(block.spans)
            block.dominant_stroke_color = ParagraphExtractor._resolve_block_dominant_stroke_color(
                block.spans
            )
            block.dominant_text_render_mode = ParagraphExtractor._resolve_block_dominant_render_mode(
                block.spans
            )

    @staticmethod
    def _assign_block_dominant_bold_flags(blocks: Sequence[TextBlock]) -> None:
        """为每个 block 计算主字重（按 span 文本长度加权众数）。"""
        for block in blocks:
            block.is_bold = ParagraphExtractor._resolve_block_dominant_bold(block.spans)

    @staticmethod
    def _resolve_block_dominant_color(spans: Sequence[TextSpan]) -> tuple[float, float, float] | None:
        """计算 block 级主色，按 span 文本长度加权。"""
        if not spans:
            return None
        color_counter: Counter[tuple[float, float, float]] = Counter()
        for span in spans:
            if span.color is None:
                continue
            color_key = _normalize_rgb_color(span.color)
            text_weight = max(len((span.text or "").strip()), 1)
            color_counter[color_key] += text_weight
        if not color_counter:
            return None
        return color_counter.most_common(1)[0][0]

    @staticmethod
    def _resolve_block_dominant_stroke_color(
        spans: Sequence[TextSpan],
    ) -> tuple[float, float, float] | None:
        """计算 block 级描边主色，按 span 文本长度加权。"""
        if not spans:
            return None
        color_counter: Counter[tuple[float, float, float]] = Counter()
        for span in spans:
            if span.stroke_color is None:
                continue
            color_key = _normalize_rgb_color(span.stroke_color)
            text_weight = max(len((span.text or "").strip()), 1)
            color_counter[color_key] += text_weight
        if not color_counter:
            return None
        return color_counter.most_common(1)[0][0]

    @staticmethod
    def _resolve_block_dominant_render_mode(spans: Sequence[TextSpan]) -> int:
        """计算 block 级文字渲染模式，按 span 文本长度加权。"""
        if not spans:
            return 0
        render_counter: Counter[int] = Counter()
        for span in spans:
            text_weight = max(len((span.text or "").strip()), 1)
            render_counter[int(getattr(span, "text_render_mode", 0) or 0)] += text_weight
        return render_counter.most_common(1)[0][0] if render_counter else 0

    @staticmethod
    def _resolve_block_dominant_bold(spans: Sequence[TextSpan]) -> bool:
        """计算 block 级主字重，按 span 文本长度加权。"""
        if not spans:
            return False
        bold_weight = 0
        regular_weight = 0
        for span in spans:
            text_weight = max(len((span.text or "").strip()), 1)
            if span.is_bold:
                bold_weight += text_weight
            else:
                regular_weight += text_weight
        return bold_weight > regular_weight

    def _apply_paragraph_mode(self, blocks: List[TextBlock], config: ParagraphModeConfig) -> None:
        """为文本块标记翻译模式（span 或 block）。"""
        if not blocks:
            return
        for block in blocks:
            label = block.layout_label or block.layout_type or ""
            if label not in config.labels:
                block.translation_mode = "span"
                continue
            block.translation_mode = self._classify_block_translation_mode(block, config, label)

    @staticmethod
    def _resolve_paragraph_profile(config: ParagraphModeConfig) -> dict:
        if config.aggressiveness == "conservative":
            return {
                "min_line_count": 3,
                "min_median_width_ratio": 0.65,
                "max_short_line_ratio": 0.25,
                "max_left_alignment_std": 0.06,
                "max_vertical_gap_cv": 0.50,
            }
        if config.aggressiveness == "aggressive":
            return {
                "min_line_count": 2,
                "min_median_width_ratio": 0.45,
                "max_short_line_ratio": 0.45,
                "max_left_alignment_std": 0.12,
                "max_vertical_gap_cv": 0.80,
            }
        return {
            "min_line_count": 2,
            "min_median_width_ratio": 0.55,
            "max_short_line_ratio": 0.35,
            "max_left_alignment_std": 0.08,
            "max_vertical_gap_cv": 0.60,
        }

    @staticmethod
    def _resolve_paragraph_profile_for_label(label: str, config: ParagraphModeConfig) -> dict:
        if label in TITLE_LABELS:
            return {
                "min_line_count": 2,
                "min_median_width_ratio": 0.35,
                "max_short_line_ratio": 0.60,
                "max_left_alignment_std": 0.15,
                "max_vertical_gap_cv": 0.90,
            }
        return ParagraphExtractor._resolve_paragraph_profile(config)

    def _classify_block_translation_mode(
        self, block: TextBlock, config: ParagraphModeConfig, label: str
    ) -> Literal["span", "block"]:
        """判定文本块是否可以按段落整体翻译。"""
        spans = block.spans or []
        if len(spans) < 2:
            return "span"

        if label in REFERENCE_LABELS:
            return "block"

        lines = self._group_spans_into_lines(spans)
        if len(lines) < 2:
            return "span"

        profile = self._resolve_paragraph_profile_for_label(label, config)
        block_width = max(block.bbox.x2 - block.bbox.x1, 0.0)
        if block_width <= 0.0:
            return "span"

        if self._looks_like_compact_annotation_block(block, lines, block_width):
            return "block"

        if self._looks_like_single_textbox_compact_block(block, lines, block_width):
            return "block"

        line_widths = [line.width for line in lines]
        median_width_ratio = median(line_widths) / block_width if line_widths else 0.0
        short_line_ratio = sum(1 for width in line_widths if width < block_width * config.short_line_width_ratio) / max(
            len(line_widths), 1
        )
        left_alignment_std = pstdev([line.x1 for line in lines]) / block_width if len(lines) > 1 else 0.0

        vertical_gaps = [
            max(lines[index].y1 - lines[index + 1].y1, 0.0)
            for index in range(len(lines) - 1)
        ]
        if vertical_gaps:
            gap_mean = sum(vertical_gaps) / len(vertical_gaps)
            gap_std = pstdev(vertical_gaps) if len(vertical_gaps) > 1 else 0.0
            vertical_gap_cv = gap_std / gap_mean if gap_mean > 0 else 0.0
        else:
            vertical_gap_cv = 0.0

        if len(lines) < profile["min_line_count"]:
            return "span"
        if median_width_ratio < profile["min_median_width_ratio"]:
            return "span"
        if short_line_ratio > profile["max_short_line_ratio"]:
            return "span"
        if left_alignment_std > profile["max_left_alignment_std"]:
            return "span"
        if vertical_gap_cv > profile["max_vertical_gap_cv"]:
            return "span"

        if self._has_multi_column_alignment(lines, block_width):
            return "span"

        font_sizes = [span.font_size for span in spans if span.font_size]
        if font_sizes:
            min_size = min(font_sizes)
            max_size = max(font_sizes)
            if min_size > 0 and max_size / min_size > config.max_font_ratio:
                return "span"

        if any(
            line.text_len <= config.tiny_text_max_len and line.width / block_width < 0.25
            for line in lines
        ):
            return "span"

        return "block"

    @staticmethod
    def _looks_like_compact_annotation_block(
        block: TextBlock,
        lines: Sequence["_LineInfo"],
        block_width: float,
    ) -> bool:
        """识别带标签的紧凑注释块，优先按整块翻译避免短句碎裂。"""
        if not 2 <= len(lines) <= 4 or block_width <= 0.0:
            return False
        first_line_text = "".join(
            span.text
            for span in sorted(block.spans, key=lambda span: (-(span.bbox.y1 + span.bbox.y2) / 2, span.bbox.x1))
            if abs(((span.bbox.y1 + span.bbox.y2) / 2) - lines[0].center_y) <= 2.5
        ).strip()
        if not re.match(r"^\[[^\]]+\]", first_line_text):
            return False
        if lines[0].width / block_width < 0.75:
            return False
        return all(line.width / block_width >= 0.18 for line in lines[1:])

    @staticmethod
    def _looks_like_single_textbox_compact_block(
        block: TextBlock,
        lines: Sequence["_LineInfo"],
        block_width: float,
    ) -> bool:
        """识别同一 textbox 内的短多行文本，优先按整块翻译避免句子割裂。"""
        if not 2 <= len(lines) <= 4 or block_width <= 0.0:
            return False
        textbox_ids = {span.source_textbox_id for span in block.spans if span.source_textbox_id is not None}
        if len(textbox_ids) != 1:
            return False
        total_text_len = sum(line.text_len for line in lines)
        if not 20 <= total_text_len <= 140:
            return False
        width_ratios = [line.width / block_width for line in lines]
        if max(width_ratios) < 0.45:
            return False
        if any(line.text_len <= 1 for line in lines):
            return False
        return True

    @staticmethod
    def _has_multi_column_alignment(lines: Sequence["_LineInfo"], block_width: float) -> bool:
        if len(lines) < 3 or block_width <= 0:
            return False
        x1s = sorted(line.x1 for line in lines)
        gaps = [x1s[i + 1] - x1s[i] for i in range(len(x1s) - 1)]
        if not gaps:
            return False
        max_gap = max(gaps)
        if max_gap < block_width * 0.25:
            return False
        split_index = gaps.index(max_gap) + 1
        left_count = split_index
        right_count = len(x1s) - split_index
        return left_count >= 2 and right_count >= 2

    def _group_spans_into_lines(self, spans: Sequence[TextSpan]) -> List["_LineInfo"]:
        """将 span 按行聚合，用于段落判定。"""
        lines = [_LineInfo.from_spans(line_spans) for line_spans in group_spans_into_lines(spans)]
        lines.sort(key=lambda line: -line.center_y)
        return lines


    async def _detect_layout(self, pdf_path: str) -> List[DocumentLayout]:
        """
        逐页检测布局并进行坐标系转换

        返回的 DocumentLayout 结果中的 bbox 已转换为 pdfminer 坐标系。
        """
        def pil_to_bgr(img_pil):
            """将 PIL 图像转换为 BGR 图像"""
            if img_pil.mode != "RGB":
                img_pil = img_pil.convert("RGB")
            img = np.asarray(img_pil)  # RGB, shape: (H, W, 3)
            return np.ascontiguousarray(img[:, :, ::-1])  # 翻转通道 → BGR

        image_converter = self._pdf_to_image_converter_cls(pdf_path)
        iter_images = getattr(image_converter, "iter_pdf_images", None)
        page_count_method = getattr(image_converter, "page_count", None)
        if callable(iter_images) and callable(page_count_method):
            page_count = int(page_count_method())
            pdf_images = iter_images()
        else:
            # 只为旧测试 adapter 保留列表接口; production converter 走逐页 iterator。
            buffered_images = image_converter.convert_pdf_to_images()
            page_count = len(buffered_images)
            pdf_images = iter(buffered_images)

        scale = PDF_RASTER_SCALE
        converted_results: List[DocumentLayout] = []

        for fallback_page_index, pdf_image in enumerate(pdf_images):
            image = pil_to_bgr(pdf_image.image)
            try:
                detected_pages = await self._layout_detector.detect_layout_batch([image])
                if len(detected_pages) != 1:
                    raise ValueError("layout_page_count_mismatch")
            except Exception as exc:
                self.layout_fallback_used = True
                logger.error(
                    "Layout detection failed, fallback to empty layouts",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
                return [
                    DocumentLayout(page_index=index, layouts=[])
                    for index in range(page_count)
                ]

            doc_layout = detected_pages[0]
            converted_layouts: List[LayoutResult] = []
            for layout in doc_layout.layouts:
                # 将 layout bbox 从图像坐标系转换为 pdfminer 坐标系
                height = layout.shape[0]  # 图像高度
                pdfminer_bounds = layout.bbox.pdfminer_bounds(scale, height)

                # 创建转换坐标后的新 LayoutResult
                converted_layout = LayoutResult(
                    cls_id=layout.cls_id,
                    label=layout.label,
                    shape=layout.shape,
                    bbox=BBox(*pdfminer_bounds),
                    score=layout.score,
                    is_filtered=layout.is_filtered,
                )
                converted_layouts.append(converted_layout)

            source_page_index = int(
                getattr(pdf_image, "page_index", fallback_page_index + 1)
            ) - 1
            converted_results.append(
                DocumentLayout(
                    page_index=source_page_index,
                    layouts=converted_layouts,
                )
            )

        if len(converted_results) != page_count:
            raise ValueError("rasterized_page_count_mismatch")
        return converted_results




_NUMERIC_PATTERN = re.compile(
    r"""
    ^[+-]?(
        \d+(?:\.\d+)?               # 整数或小数
        |
        \d+\s+\d+[/⁄]\d+            # 带分数 (20 1/2 或 20 1⁄2)
        |
        \d+[/⁄]\d+                  # 真分数 (1/4 或 1⁄4)
        |
        [¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]         # Unicode 分数
        |
        \d+[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]       # Unicode 带分数 (2½)
    )$
    """,
    re.VERBOSE,
)

_CID_PATTERN = re.compile(r"\(cid:\d+\)", re.IGNORECASE)
_BOLD_FONT_NAME_TOKENS: tuple[str, ...] = (
    "extrabold",
    "ultrabold",
    "semibold",
    "demibold",
    "black",
    "heavy",
    "bold",
    "demi",
)


def _check_span_is_preserved(span_text: str) -> Tuple[bool, str]:
    """
    检查 span 是否需要保留

    Returns:
        (是否需要保留, 处理后的文本)
    """
    should_preserve = False
    processed_text = span_text
    # 检查是否含有 pdfminer 无法解析的 CID。
    # 少量 CID 混入正常句子时，优先清洗占位符后继续翻译，避免整行被跳过。
    if _contains_cid_marker(span_text):
        sanitized_text = _sanitize_cid_markers(span_text)
        if _is_recoverable_cid_text(span_text, sanitized_text):
            processed_text = sanitized_text
        else:
            should_preserve = True
    # 检查是否为纯数字行
    if _is_numeric_text(span_text):
        should_preserve = True
        processed_text = processed_text.replace("⁄", "/")  # 纯数字需要替换分号
    # pdfminer无法解析的特殊符号也进行保留
    if span_text == "":
        should_preserve = True
        processed_text = ""
    return should_preserve, processed_text

def _is_numeric_text(text: str) -> bool:
    """判断文本是否为数字/符号组成的片段（用于保留原文）。"""
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if _NUMERIC_PATTERN.match(compact):
        return True
    for char in compact:
        category = unicodedata.category(char)
        if category.startswith(("N", "P", "S")):
            continue
        return False
    return True


def _contains_cid_marker(text: str) -> bool:
    """判断文本是否包含任何CID占位符。"""
    return bool(_CID_PATTERN.search(text))


def _sanitize_cid_markers(text: str) -> str:
    """将 CID 占位符替换为空格，保留可读正文。"""
    return _CID_PATTERN.sub(" ", text)


def _is_recoverable_cid_text(original_text: str, sanitized_text: str) -> bool:
    """判断包含少量 CID 的文本是否仍可恢复为可翻译正文。"""
    cid_count = len(_CID_PATTERN.findall(original_text))
    if cid_count == 0:
        return False

    compact_text = re.sub(r"\s+", "", sanitized_text)
    if not compact_text:
        return False

    visible_parts = _extract_visible_cid_parts(original_text)
    alpha_count = sum(1 for char in compact_text if char.isalpha())
    alnum_count = sum(1 for char in compact_text if char.isalnum())
    word_like_count = len(re.findall(r"[^\W\d_]{3,}", sanitized_text, flags=re.UNICODE))

    # 纯 CID / 高密度 CID 乱码仍走保留分支，避免把噪声送给翻译模型。
    if cid_count >= max(3, alnum_count):
        return False

    # 短标题/短列表项里若只有极少量 CID 混入，且两侧仍有可见正文，则允许继续翻译。
    if _is_recoverable_short_cid_text(visible_parts, compact_text, cid_count, alnum_count):
        return True

    if word_like_count >= 3:
        return True

    return alpha_count >= 12 and cid_count <= max(2, alpha_count // 8)


def _extract_visible_cid_parts(text: str) -> list[str]:
    """提取 CID 占位符两侧仍可见的文本片段。"""
    visible_parts: list[str] = []
    for part in _CID_PATTERN.split(text):
        compact_part = re.sub(r"\s+", "", part)
        if compact_part:
            visible_parts.append(compact_part)
    return visible_parts


def _is_recoverable_short_cid_text(
    visible_parts: list[str],
    compact_text: str,
    cid_count: int,
    alnum_count: int,
) -> bool:
    """判断短文本中的少量 CID 是否可以安全清洗。"""
    if cid_count > 1 or len(visible_parts) < 2:
        return False

    if _contains_cjk_text(compact_text):
        return len(compact_text) >= 3

    return alnum_count >= 4


def _contains_cjk_text(text: str) -> bool:
    """判断文本是否包含中日韩统一表意文字。"""
    for char in text:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x2EBEF
        ):
            return True
    return False


def _normalize_font_name(font_name: str) -> str:
    """规范化字体名，去除 PDF 子集前缀。"""
    normalized = (font_name or "").strip().lstrip("/")
    if "+" in normalized:
        prefix, suffix = normalized.split("+", 1)
        if len(prefix) == 6 and prefix.isalnum():
            return suffix
    return normalized


def _is_bold_font_name(font_name: str) -> bool:
    """基于字体名判断是否粗体。"""
    normalized = _normalize_font_name(font_name).lower()
    if not normalized:
        return False
    return any(token in normalized for token in _BOLD_FONT_NAME_TOKENS)


def _get_most_common_font_name(chars: list[LTChar]) -> str:
    """
    获取字符列表中出现最多的字体名称（众数）。

    Args:
        chars: 字符列表

    Returns:
        出现频率最高的字体名称
    """
    if not chars:
        return ""

    # 统计每个字体名称的出现次数
    font_counter = Counter(char.fontname for char in chars)

    # 返回出现次数最多的字体名称
    return font_counter.most_common(1)[0][0]


def _get_most_common_bold_flag(chars: list[LTChar]) -> bool:
    """基于字符级字体名众数判断 span 是否粗体。"""
    if not chars:
        return False
    bold_count = 0
    regular_count = 0
    for char in chars:
        font_name = getattr(char, "fontname", "") or ""
        if _is_bold_font_name(str(font_name)):
            bold_count += 1
        else:
            regular_count += 1
    return bold_count > regular_count


def _get_most_common_text_style(
    chars: list[LTChar],
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None, int]:
    """获取字符列表的主填充色、描边色和文字渲染模式。"""
    if not chars:
        return None, None, 0

    fill_counter: Counter[tuple[float, float, float]] = Counter()
    stroke_counter: Counter[tuple[float, float, float]] = Counter()
    render_counter: Counter[int] = Counter()

    for char in chars:
        fill_color, stroke_color, render_mode = _extract_char_text_style(char)
        if fill_color is not None:
            fill_counter[_normalize_rgb_color(fill_color)] += 1
        if stroke_color is not None:
            stroke_counter[_normalize_rgb_color(stroke_color)] += 1
        render_counter[render_mode] += 1

    dominant_fill = fill_counter.most_common(1)[0][0] if fill_counter else None
    dominant_stroke = stroke_counter.most_common(1)[0][0] if stroke_counter else None
    dominant_render = render_counter.most_common(1)[0][0] if render_counter else 0
    return dominant_fill, dominant_stroke, dominant_render


def _extract_char_text_style(
    char: LTChar,
) -> tuple[tuple[float, float, float] | None, tuple[float, float, float] | None, int]:
    """提取单个字符的填充色、描边色和文字渲染模式。"""
    graphic_state = getattr(char, "graphicstate", None)
    fill_color = _convert_pdf_color_to_rgb(
        getattr(graphic_state, "ncolor", None),
        getattr(graphic_state, "ncs", None),
    )
    stroke_color = _convert_pdf_color_to_rgb(
        getattr(graphic_state, "scolor", None),
        getattr(graphic_state, "scs", None),
    )
    source_text_block = getattr(char, "source_text_block", None)
    render_mode = int(getattr(source_text_block, "_render", 0) or 0)
    return fill_color, stroke_color, render_mode


def _get_span_text_visibility(chars: Sequence[LTChar]) -> tuple[bool, bool]:
    """判断 span 是否实际绘制, 并标记 renderer 暂不支持的透明文字。"""

    has_visible_paint = False
    has_transparency = False
    for char in chars:
        _fill_color, _stroke_color, render_mode = _extract_char_text_style(char)
        graphic_state = getattr(char, "graphicstate", None)
        fill_alpha = _normalize_text_alpha(getattr(graphic_state, "nalpha", 1.0))
        stroke_alpha = _normalize_text_alpha(getattr(graphic_state, "salpha", 1.0))
        if render_mode in {3, 7}:
            continue
        if render_mode in {1, 5}:
            active_alphas = (stroke_alpha,)
        elif render_mode in {2, 6}:
            active_alphas = (fill_alpha, stroke_alpha)
        else:
            active_alphas = (fill_alpha,)
        if any(alpha > 1e-6 for alpha in active_alphas):
            has_visible_paint = True
        if any(alpha < 1.0 - 1e-6 for alpha in active_alphas):
            has_transparency = True
    return has_visible_paint, has_transparency


def _normalize_text_alpha(value: object) -> float:
    """把 pdfminerex graphic state alpha 限制到 PDF 合法区间。"""

    try:
        alpha = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(alpha):
        return 1.0
    return min(max(alpha, 0.0), 1.0)


def _convert_pdf_color_to_rgb(
    color: object,
    colorspace: PDFColorSpace | None = None,
) -> tuple[float, float, float] | None:
    """将 PDF 颜色值统一转换为 RGB。"""
    if color is None:
        return None

    separation_rgb = _convert_separation_color_to_rgb(color, colorspace)
    if separation_rgb is not None:
        return separation_rgb

    if isinstance(color, (int, float)):
        gray = _normalize_rgb_color((float(color), float(color), float(color)))[0]
        return (gray, gray, gray)
    if not isinstance(color, (list, tuple)):
        return None

    components = _normalize_color_components(color)
    if components is None:
        return None

    if len(components) == 1:
        gray = components[0]
        return (gray, gray, gray)
    if len(components) == 3:
        return _normalize_rgb_color((components[0], components[1], components[2]))
    if len(components) == 4:
        c, m, y, k = components
        rgb = (
            1.0 - min(1.0, c + k),
            1.0 - min(1.0, m + k),
            1.0 - min(1.0, y + k),
        )
        return _normalize_rgb_color(rgb)
    return None


def _convert_separation_color_to_rgb(
    color: object,
    colorspace: PDFColorSpace | None,
) -> tuple[float, float, float] | None:
    """将 Separation 专色 tint 值转换为实际 RGB。"""
    if colorspace is None or getattr(colorspace, "name", "") != "Separation":
        return None

    components = _normalize_color_components((color,) if isinstance(color, (int, float)) else color)
    if components is None or len(components) != 1:
        return None

    alternate = getattr(colorspace, "alternate", None)
    tint_transform = getattr(colorspace, "tint_transform", None)
    alternate_components = _apply_pdf_type2_tint_transform(tint_transform, components[0])
    if alternate_components is None:
        return None
    return _convert_pdf_color_to_rgb(tuple(alternate_components), alternate)


def _apply_pdf_type2_tint_transform(function: object, tint: float) -> list[float] | None:
    """执行最常见的 FunctionType 2 tint transform。"""
    if not isinstance(function, dict):
        return None

    function_type = function.get("/FunctionType", function.get("FunctionType"))
    try:
        function_type_value = int(function_type)
    except (TypeError, ValueError):
        return None
    if function_type_value != 2:
        return None

    c0 = _normalize_color_components(function.get("/C0", function.get("C0", [0.0])))
    c1 = _normalize_color_components(function.get("/C1", function.get("C1", [1.0])))
    if c0 is None or c1 is None or len(c0) != len(c1):
        return None

    exponent = function.get("/N", function.get("N", 1.0))
    try:
        exponent_value = float(exponent)
    except (TypeError, ValueError):
        exponent_value = 1.0

    tint_value = min(max(float(tint), 0.0), 1.0)
    factor = tint_value ** exponent_value
    return [c0_item + factor * (c1_item - c0_item) for c0_item, c1_item in zip(c0, c1)]


def _normalize_color_components(raw_components: Sequence[object]) -> list[float] | None:
    """将颜色分量归一化到 [0, 1]。"""
    if not raw_components:
        return None

    components: list[float] = []
    for component in raw_components:
        try:
            components.append(float(component))
        except (TypeError, ValueError):
            return None

    max_abs = max(abs(value) for value in components)
    if max_abs > 1.0:
        if max_abs <= 100.0:
            components = [value / 100.0 for value in components]
        elif max_abs <= 255.0:
            components = [value / 255.0 for value in components]

    return [min(max(value, 0.0), 1.0) for value in components]


def _normalize_rgb_color(color: tuple[float, float, float]) -> tuple[float, float, float]:
    """规范化 RGB 值，避免浮点噪声影响众数统计。"""
    r, g, b = color
    return (
        round(min(max(r, 0.0), 1.0), 4),
        round(min(max(g, 0.0), 1.0), 4),
        round(min(max(b, 0.0), 1.0), 4),
    )


@dataclass(frozen=True)
class _LineInfo:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    center_y: float
    text_len: int

    @classmethod
    def from_spans(cls, spans: Sequence[TextSpan]) -> "_LineInfo":
        ordered = sorted(spans, key=lambda span: span.bbox.x1)
        x1 = min(span.bbox.x1 for span in ordered)
        y1 = min(span.bbox.y1 for span in ordered)
        x2 = max(span.bbox.x2 for span in ordered)
        y2 = max(span.bbox.y2 for span in ordered)
        center_y = (y1 + y2) / 2
        width = max(x2 - x1, 0.0)
        text_len = len("".join(span.text for span in ordered).strip())
        return cls(x1=x1, y1=y1, x2=x2, y2=y2, width=width, center_y=center_y, text_len=text_len)


def _get_most_common_font_size(chars: list[LTChar]) -> float:
    """
    获取字符列表中出现最多的字体大小（众数）。

    Args:
        chars: 字符列表

    Returns:
        出现频率最高的字体大小
    """
    if not chars:
        return 12.0  # 默认字体大小

    # 统计每个字体大小的出现次数
    # 由于浮点数精度问题，四舍五入到小数点后1位
    size_counter = Counter(round(char.size, 1) for char in chars)

    # 返回出现次数最多的字体大小
    return size_counter.most_common(1)[0][0]
