"""
PDF 表格译文渲染辅助逻辑。

renderer.py 只负责 PDF stream 调度；表格内的单元格分组、共享字号和换行策略集中在这里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

import pikepdf

from .entities import BBox, TextBlock, TextSpan
from .font_manager import FontLanguage, FontSubsetData, infer_text_language, segment_text


LineCommandBuilder = Callable[
    [
        list[tuple[str, FontLanguage]],
        dict[FontLanguage, pikepdf.Name],
        dict[FontLanguage, FontSubsetData],
        float,
        str,
        float,
        float,
        float,
    ],
    str,
]
SegmentsWidthCalculator = Callable[
    [list[tuple[str, FontLanguage]], dict[FontLanguage, FontSubsetData], float],
    float,
]
FontSizeResolver = Callable[[TextSpan], float]


@dataclass(frozen=True)
class TableRenderConfig:
    """表格单元格渲染配置。"""

    line_height_factor: float
    min_font_size: float
    shared_fit_ratio: float = 0.75


@dataclass(frozen=True)
class _CellText:
    bbox: BBox
    spans: tuple[TextSpan, ...]
    text: str
    base_font_size: float
    source_spans: tuple[TextSpan, ...] = ()


@dataclass(frozen=True)
class _CellLayout:
    lines: tuple[str, ...]
    font_size: float
    line_height_factor: float


def is_table_text_span(block: TextBlock, span: TextSpan) -> bool:
    """判断 span 是否属于表格上下文。"""

    labels = (
        getattr(block, "layout_label", None),
        getattr(block, "layout_type", None),
        getattr(span, "layout_label", None),
    )
    return any((label or "").strip().lower() == "table" for label in labels)


class TableTextRenderer:
    """负责表格文本在固定单元格内的排版。"""

    _WRAP_WORD_PATTERN = re.compile(
        r"[A-Za-z0-9\u00C0-\u024F\u0400-\u052F]+"
        r"(?:[._'’/-][A-Za-z0-9\u00C0-\u024F\u0400-\u052F]+)*"
    )
    _WRAP_CLOSING_PUNCTUATION = frozenset(
        ",.;:!?%)]}，。；：！？、）】》」』"
    )
    _CJK_LANGUAGES = frozenset(
        {
            FontLanguage.CHINESE,
            FontLanguage.JAPANESE,
            FontLanguage.KOREAN,
        }
    )

    def __init__(
        self,
        *,
        build_single_line_command: LineCommandBuilder,
        calculate_segments_width: SegmentsWidthCalculator,
        determine_font_size: FontSizeResolver,
    ) -> None:
        self._build_single_line_command = build_single_line_command
        self._calculate_segments_width = calculate_segments_width
        self._determine_font_size = determine_font_size

    def build_cell_group_commands(
        self,
        block: TextBlock,
        spans: Sequence[TextSpan],
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        config: TableRenderConfig,
    ) -> tuple[list[str], set[int]]:
        """渲染高置信表格单元格，并返回已被整体渲染的 span id。"""

        cells = self._collect_cell_texts(block, spans)
        if not cells:
            return [], set()

        shared_font_size = self._resolve_shared_font_size(cells, font_subsets, config)
        commands: list[str] = []
        grouped_span_ids: set[int] = set()
        unsafe_single_cell_keys = self._find_unsafe_single_span_cell_keys(cells, spans)

        for cell in cells:
            layout = self._select_cell_layout(
                cell,
                shared_font_size,
                font_subsets,
                config,
            )
            if layout is None:
                if len(cell.spans) < 2:
                    continue
                layout = self._build_single_line_layout(cell, font_subsets, config)
            if layout is None:
                continue
            if len(cell.spans) == 1 and not self._should_render_single_span_cell(
                cell,
                layout,
                font_subsets,
                unsafe_single_cell_keys,
            ):
                continue

            cell_commands = self._render_cell_lines(
                layout.lines,
                cell.bbox,
                layout.font_size,
                font_resource_names,
                font_subsets,
                color_command,
                layout.line_height_factor,
            )
            if not cell_commands:
                continue
            commands.extend(cell_commands)
            grouped_span_ids.update(id(span) for span in cell.spans)

        return commands, grouped_span_ids

    @staticmethod
    def table_cell_bbox_key(bbox: BBox) -> tuple[float, float, float, float]:
        return (round(bbox.x1, 2), round(bbox.y1, 2), round(bbox.x2, 2), round(bbox.y2, 2))

    def fit_table_cell_single_line(
        self,
        segments: list[tuple[str, FontLanguage]],
        font_subsets: dict[FontLanguage, FontSubsetData],
        *,
        font_size: float,
        max_width: float,
        min_font_size: float,
    ) -> float:
        """表格单元格不能扩列：译文溢出时只缩字号，不使用 Tz 横向压字。"""

        if font_size <= 0.0 or max_width <= 0.0 or not segments:
            return font_size

        current_width = self._calculate_segments_width(segments, font_subsets, font_size)
        if current_width <= 0.0 or current_width <= max_width * 1.02:
            return font_size

        guarded_min_font = min(max(min_font_size, 0.1), font_size)
        adjusted_size = max(font_size * (max_width / current_width), guarded_min_font)
        adjusted_width = self._calculate_segments_width(segments, font_subsets, adjusted_size)
        if adjusted_width <= 0.0 or adjusted_width <= max_width * 1.02:
            return adjusted_size

        hard_min_font_size = min(max(min_font_size * 0.75, 0.1), adjusted_size)
        width_at_hard_min = self._calculate_segments_width(
            segments,
            font_subsets,
            hard_min_font_size,
        )
        if width_at_hard_min <= 0.0:
            return adjusted_size
        if width_at_hard_min <= max_width * 1.02:
            return hard_min_font_size
        return hard_min_font_size

    def _collect_cell_texts(
        self,
        block: TextBlock,
        spans: Sequence[TextSpan],
    ) -> list[_CellText]:
        groups: dict[tuple[float, float, float, float], list[TextSpan]] = {}
        for span in spans:
            if not is_table_text_span(block, span):
                continue
            if span.table_cell_bbox is None:
                continue
            groups.setdefault(self.table_cell_bbox_key(span.table_cell_bbox), []).append(span)

        cells: list[_CellText] = []
        for cell_spans in groups.values():
            source_spans = tuple(
                sorted(
                    cell_spans,
                    key=lambda item: (-(item.bbox.y1 + item.bbox.y2) / 2, item.bbox.x1),
                )
            )
            ordered_spans = tuple(
                span for span in source_spans
                if (span.translated_text or "").strip()
            )
            if not ordered_spans:
                continue
            text = self._join_cell_translations(ordered_spans)
            if not text:
                continue
            bbox = source_spans[0].table_cell_bbox
            if bbox is None:
                continue
            if not self._cell_has_reasonable_vertical_fit(bbox, source_spans):
                continue
            base_font_size = self._resolve_cell_base_font_size(source_spans)
            if base_font_size <= 0.0:
                continue
            cells.append(
                _CellText(
                    bbox=bbox,
                    spans=ordered_spans,
                    text=text,
                    base_font_size=base_font_size,
                    source_spans=source_spans,
                )
            )
        return cells

    def _cell_has_reasonable_vertical_fit(
        self,
        bbox: BBox,
        spans: Sequence[TextSpan],
    ) -> bool:
        if not spans:
            return False
        cell_height = bbox.y2 - bbox.y1
        if cell_height <= 0.0:
            return False
        span_top = max(span.bbox.y2 for span in spans)
        span_bottom = min(span.bbox.y1 for span in spans)
        span_band_height = max(span_top - span_bottom, 0.0)
        median_font = self._calculate_median_value(
            [span.font_size for span in spans if span.font_size and span.font_size > 0.0]
        )
        if median_font is None:
            median_font = 10.0

        max_reasonable_height = max(span_band_height * 3.0, median_font * 5.0)
        if cell_height > max_reasonable_height:
            return False

        centers = sorted((span.bbox.y1 + span.bbox.y2) / 2 for span in spans)
        if len(centers) > 1:
            max_gap = max(next_center - current for current, next_center in zip(centers, centers[1:]))
            if max_gap > max(median_font * 2.8, 18.0):
                return False
        return True

    @staticmethod
    def _join_cell_translations(spans: Sequence[TextSpan]) -> str:
        parts: list[str] = []
        for span in spans:
            text = (span.translated_text or "").strip()
            if not text:
                continue
            if parts and text == parts[-1]:
                continue
            parts.append(text)
        return " ".join(parts)

    def _resolve_cell_base_font_size(self, spans: Sequence[TextSpan]) -> float:
        sizes = [span.font_size for span in spans if span.font_size and span.font_size > 0.0]
        median_size = self._calculate_median_value(sizes)
        if median_size is not None:
            return max(median_size, 0.1)
        if spans:
            return self._determine_font_size(spans[0])
        return 0.0

    def _resolve_shared_font_size(
        self,
        cells: Sequence[_CellText],
        font_subsets: dict[FontLanguage, FontSubsetData],
        config: TableRenderConfig,
    ) -> float:
        base_size = self._calculate_median_value([cell.base_font_size for cell in cells])
        if base_size is None:
            return config.min_font_size

        threshold = max(1, int(len(cells) * config.shared_fit_ratio + 0.999))
        for candidate in self._font_size_tiers(base_size, config.min_font_size):
            fit_count = 0
            for cell in cells:
                if self._layout_cell_text(cell.text, cell.bbox, candidate, font_subsets, config) is not None:
                    fit_count += 1
            if fit_count >= threshold:
                return candidate
        return max(config.min_font_size, 0.1)

    def _select_cell_layout(
        self,
        cell: _CellText,
        shared_font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
        config: TableRenderConfig,
    ) -> _CellLayout | None:
        base_size = max(cell.base_font_size, shared_font_size)
        candidates = [shared_font_size]
        candidates.extend(
            size for size in self._font_size_tiers(base_size, config.min_font_size)
            if size < shared_font_size - 0.01
        )
        seen: set[float] = set()
        for candidate in candidates:
            normalized = round(candidate, 2)
            if normalized in seen:
                continue
            seen.add(normalized)
            layout = self._layout_cell_text(cell.text, cell.bbox, candidate, font_subsets, config)
            if layout is not None:
                return layout
        return None

    def _build_single_line_layout(
        self,
        cell: _CellText,
        font_subsets: dict[FontLanguage, FontSubsetData],
        config: TableRenderConfig,
    ) -> _CellLayout | None:
        segments = segment_text(cell.text)
        if not segments:
            return None
        font_size = self.fit_table_cell_single_line(
            segments,
            font_subsets,
            font_size=cell.base_font_size,
            max_width=max(cell.bbox.x2 - cell.bbox.x1, 0.0),
            min_font_size=config.min_font_size,
        )
        if font_size <= 0.0:
            return None
        return _CellLayout(
            lines=(cell.text,),
            font_size=font_size,
            line_height_factor=self._resolve_table_line_height_factor(cell.text, config.line_height_factor),
        )

    def _should_render_single_span_cell(
        self,
        cell: _CellText,
        layout: _CellLayout,
        font_subsets: dict[FontLanguage, FontSubsetData],
        unsafe_single_cell_keys: set[tuple[float, float, float, float]],
    ) -> bool:
        """单 span 单元格只在明确能利用单元格换行时接管，避免误伤怪表格。"""

        if len(cell.spans) != 1:
            return True
        if self.table_cell_bbox_key(cell.bbox) in unsafe_single_cell_keys:
            return False
        span = cell.spans[0]
        cell_width = max(cell.bbox.x2 - cell.bbox.x1, 0.0)
        if cell_width <= 0.0:
            return False
        base_width = self._calculate_text_width_by_language(
            cell.text,
            font_subsets,
            max(span.font_size or cell.base_font_size, 0.1),
        )
        if len(layout.lines) < 2:
            return len(cell.source_spans) > 1 and base_width > cell_width * 1.02
        return base_width > cell_width * 1.02

    def _find_unsafe_single_span_cell_keys(
        self,
        cells: Sequence[_CellText],
        all_spans: Sequence[TextSpan],
    ) -> set[tuple[float, float, float, float]]:
        unsafe = self._find_overlapping_single_span_cell_keys(cells)
        single_cells = [cell for cell in cells if len(cell.spans) == 1]
        for cell in single_cells:
            key = self.table_cell_bbox_key(cell.bbox)
            if key in unsafe:
                continue
            owner_span = cell.spans[0]
            source_span_ids = {id(span) for span in cell.source_spans}
            for span in all_spans:
                if span is owner_span:
                    continue
                if id(span) in source_span_ids:
                    continue
                if not self._span_has_renderable_text(span):
                    continue
                if self._span_lives_inside_bbox(span, cell.bbox):
                    unsafe.add(key)
                    break
        return unsafe

    @staticmethod
    def _span_has_renderable_text(span: TextSpan) -> bool:
        return bool((span.translated_text or "").strip())

    @classmethod
    def _span_lives_inside_bbox(cls, span: TextSpan, bbox: BBox) -> bool:
        span_bbox = span.bbox
        center_x = (span_bbox.x1 + span_bbox.x2) / 2
        center_y = (span_bbox.y1 + span_bbox.y2) / 2
        if bbox.x1 <= center_x <= bbox.x2 and bbox.y1 <= center_y <= bbox.y2:
            return True
        return cls._bbox_overlap_ratio(span_bbox, bbox) > 0.35

    def _find_overlapping_single_span_cell_keys(
        self,
        cells: Sequence[_CellText],
    ) -> set[tuple[float, float, float, float]]:
        single_cells = [cell for cell in cells if len(cell.spans) == 1]
        overlapping: set[tuple[float, float, float, float]] = set()
        for index, cell in enumerate(single_cells):
            for other in single_cells[index + 1 :]:
                if self._bbox_overlap_ratio(cell.bbox, other.bbox) <= 0.2:
                    continue
                overlapping.add(self.table_cell_bbox_key(cell.bbox))
                overlapping.add(self.table_cell_bbox_key(other.bbox))
        return overlapping

    @staticmethod
    def _bbox_overlap_ratio(first: BBox, second: BBox) -> float:
        x1 = max(first.x1, second.x1)
        y1 = max(first.y1, second.y1)
        x2 = min(first.x2, second.x2)
        y2 = min(first.y2, second.y2)
        overlap_width = max(x2 - x1, 0.0)
        overlap_height = max(y2 - y1, 0.0)
        overlap_area = overlap_width * overlap_height
        if overlap_area <= 0.0:
            return 0.0
        first_area = max(first.x2 - first.x1, 0.0) * max(first.y2 - first.y1, 0.0)
        second_area = max(second.x2 - second.x1, 0.0) * max(second.y2 - second.y1, 0.0)
        min_area = min(first_area, second_area)
        if min_area <= 0.0:
            return 0.0
        return overlap_area / min_area

    def _layout_cell_text(
        self,
        text: str,
        bbox: BBox,
        font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
        config: TableRenderConfig,
    ) -> _CellLayout | None:
        if font_size <= 0.0:
            return None
        line_height_factor = self._resolve_table_line_height_factor(text, config.line_height_factor)
        lines = tuple(
            line for line in self._wrap_text_to_lines(text, bbox, font_size, font_subsets)
            if line.strip()
        )
        if not lines:
            return None
        if not self._lines_fit_width(lines, bbox, font_size, font_subsets):
            return None
        if not self._lines_fit_bbox(lines, bbox, font_size, line_height_factor):
            return None
        return _CellLayout(lines=lines, font_size=font_size, line_height_factor=line_height_factor)

    def _render_cell_lines(
        self,
        lines: Sequence[str],
        bbox: BBox,
        font_size: float,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        line_height_factor: float,
    ) -> list[str]:
        if not lines or font_size <= 0.0:
            return []
        line_height = font_size * line_height_factor
        total_height = len(lines) * line_height
        bbox_height = max(bbox.y2 - bbox.y1, 0.0)
        start_y = bbox.y2 - font_size
        if bbox_height > total_height:
            start_y -= (bbox_height - total_height) / 2

        commands: list[str] = []
        for index, line in enumerate(lines):
            line_text = line.strip()
            if not line_text:
                continue
            command = self._build_single_line_command(
                segment_text(line_text),
                font_resource_names,
                font_subsets,
                font_size,
                color_command,
                bbox.x1,
                start_y - index * line_height,
                100.0,
            )
            if command:
                commands.append(command)
        return commands

    def _wrap_text_to_lines(
        self,
        text: str,
        bbox: BBox,
        font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
    ) -> list[str]:
        max_width = max(bbox.x2 - bbox.x1, 0.0)
        if max_width <= 0.0:
            return []
        paragraphs = text.splitlines() or [text]
        lines: list[str] = []
        for paragraph in paragraphs:
            if not paragraph:
                lines.append("")
                continue
            tokens = self._split_text_for_wrap(paragraph)
            current_line = ""
            current_width = 0.0
            for token in tokens:
                if not token:
                    continue
                token_width = self._calculate_text_width_by_language(token, font_subsets, font_size)
                if token_width > max_width * 1.05 and len(token) > 1:
                    if self._is_word_wrap_token(token):
                        if current_line:
                            lines.append(current_line.rstrip())
                        current_line = token.lstrip()
                        current_width = self._calculate_text_width_by_language(
                            current_line,
                            font_subsets,
                            font_size,
                        )
                        continue
                    for char in token:
                        char_width = self._calculate_text_width_by_language(char, font_subsets, font_size)
                        if current_line and current_width + char_width > max_width * 1.01:
                            lines.append(current_line.rstrip())
                            current_line = char
                            current_width = char_width
                        else:
                            current_line += char
                            current_width += char_width
                    continue
                if current_line and current_width + token_width > max_width * 1.01:
                    lines.append(current_line.rstrip())
                    current_line = token.lstrip()
                    current_width = self._calculate_text_width_by_language(current_line, font_subsets, font_size)
                    continue
                current_line += token
                current_width += token_width
            if current_line:
                lines.append(current_line.rstrip())
        return lines

    @staticmethod
    def _split_text_for_wrap(text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char.isspace():
                if tokens:
                    tokens[-1] += char
                index += 1
                continue

            word_match = TableTextRenderer._WRAP_WORD_PATTERN.match(text, index)
            if word_match:
                tokens.append(word_match.group(0))
                index = word_match.end()
                continue

            if char in TableTextRenderer._WRAP_CLOSING_PUNCTUATION and tokens:
                tokens[-1] += char
            else:
                tokens.append(char)
            index += 1

        return tokens

    @classmethod
    def _is_word_wrap_token(cls, token: str) -> bool:
        """拉丁词不按字符硬切，避免窄表头出现 Comm/itment 这类断词。"""

        stripped = token.strip()
        if not stripped:
            return False
        stripped = stripped.strip("\"'“”‘’")
        stripped = stripped.rstrip("".join(cls._WRAP_CLOSING_PUNCTUATION))
        return bool(cls._WRAP_WORD_PATTERN.fullmatch(stripped))

    def _calculate_text_width_by_language(
        self,
        text: str,
        font_subsets: dict[FontLanguage, FontSubsetData],
        font_size: float,
    ) -> float:
        if not text:
            return 0.0
        return self._calculate_segments_width(segment_text(text), font_subsets, font_size)

    @classmethod
    def _resolve_table_line_height_factor(cls, text: str, base_factor: float) -> float:
        sanitized = text.replace("\r", "").replace("\n", "").strip()
        if not sanitized:
            return max(min(base_factor, 1.02), 0.9)
        language = infer_text_language(sanitized, mode="majority")
        if language in cls._CJK_LANGUAGES:
            return max(min(base_factor, 1.08), 1.0)
        return max(min(base_factor, 1.02), 0.9)

    @staticmethod
    def _lines_fit_bbox(
        lines: Sequence[str],
        bbox: BBox,
        font_size: float,
        line_height_factor: float,
    ) -> bool:
        if not lines or font_size <= 0.0:
            return False
        total_height = len(lines) * font_size * line_height_factor
        return total_height <= max(bbox.y2 - bbox.y1, 0.0) + 0.1

    def _lines_fit_width(
        self,
        lines: Sequence[str],
        bbox: BBox,
        font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
    ) -> bool:
        if not lines or font_size <= 0.0:
            return False
        max_width = max(bbox.x2 - bbox.x1, 0.0)
        if max_width <= 0.0:
            return False
        for line in lines:
            line_width = self._calculate_text_width_by_language(
                line.rstrip(),
                font_subsets,
                font_size,
            )
            if line_width > max_width * 1.02 + 0.1:
                return False
        return True

    @staticmethod
    def _font_size_tiers(base_size: float, min_font_size: float) -> list[float]:
        guarded_base = max(base_size, 0.1)
        guarded_min = max(min_font_size, 0.1)
        raw_sizes = [
            guarded_base,
            guarded_base * 0.96,
            guarded_base * 0.92,
            guarded_base * 0.88,
            guarded_base * 0.84,
            guarded_base * 0.80,
            guarded_base * 0.74,
            guarded_base * 0.68,
            guarded_min,
        ]
        sizes: list[float] = []
        seen: set[float] = set()
        for size in raw_sizes:
            normalized = round(max(size, guarded_min), 2)
            if normalized in seen:
                continue
            seen.add(normalized)
            sizes.append(max(size, guarded_min))
        return sorted(sizes, reverse=True)

    @staticmethod
    def _calculate_median_value(values: Sequence[float]) -> float | None:
        if not values:
            return None
        sorted_values = sorted(values)
        mid = len(sorted_values) // 2
        if len(sorted_values) % 2 == 0:
            return (sorted_values[mid - 1] + sorted_values[mid]) / 2
        return sorted_values[mid]
