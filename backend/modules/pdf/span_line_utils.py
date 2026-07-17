"""PDF span 逻辑行分组工具。"""

from __future__ import annotations

from statistics import median
from typing import Sequence

from .entities import TextSpan


def group_spans_into_lines(spans: Sequence[TextSpan]) -> list[list[TextSpan]]:
    """按逻辑行聚合 spans，并尽量把上标/下标类碎片挂回正文行。"""
    if not spans:
        return []

    sorted_spans = sorted(
        spans,
        key=lambda span: (-(span.bbox.y1 + span.bbox.y2) / 2, span.bbox.x1),
    )
    font_sizes = [span.font_size for span in spans if span.font_size and span.font_size > 0]
    median_font = median(font_sizes) if font_sizes else 10.0
    base_tolerance = max(2.0, median_font * 0.4)
    inline_tolerance = max(base_tolerance, median_font * 0.8)

    lines: list[list[TextSpan]] = []
    current: list[TextSpan] = []
    current_y: float | None = None

    def append_to_current(span: TextSpan, center_y: float) -> None:
        nonlocal current_y
        current.append(span)
        current_y = center_y if current_y is None else (current_y * (len(current) - 1) + center_y) / len(current)

    def flush_current() -> None:
        if current:
            lines.append(sorted(current, key=lambda span: span.bbox.x1))

    for span in sorted_spans:
        center_y = (span.bbox.y1 + span.bbox.y2) / 2
        if current_y is None or abs(center_y - current_y) <= base_tolerance:
            append_to_current(span, center_y)
            continue
        if _should_attach_inline_fragment(current, span, current_y, median_font, inline_tolerance):
            append_to_current(span, center_y)
            continue
        flush_current()
        current = [span]
        current_y = center_y

    flush_current()
    return lines


def sort_spans_by_reading_order(spans: Sequence[TextSpan]) -> list[TextSpan]:
    """按逻辑行排序 span，避免公式碎片因纵向偏移被插入错误位置。"""
    ordered: list[TextSpan] = []
    for line in group_spans_into_lines(spans):
        ordered.extend(sorted(line, key=lambda span: span.bbox.x1))
    return ordered


def _should_attach_inline_fragment(
    current_line: Sequence[TextSpan],
    candidate: TextSpan,
    current_y: float,
    median_font: float,
    inline_tolerance: float,
) -> bool:
    """判断候选 span 是否应作为行内小碎片并入当前逻辑行。"""
    if not current_line:
        return False

    candidate_font = candidate.font_size or median_font
    if candidate_font > median_font * 0.92:
        return False

    candidate_center_y = (candidate.bbox.y1 + candidate.bbox.y2) / 2
    if abs(candidate_center_y - current_y) > inline_tolerance:
        return False

    line_x1 = min(span.bbox.x1 for span in current_line)
    line_x2 = max(span.bbox.x2 for span in current_line)
    gap_tolerance = max(10.0, median_font * 1.2)

    if candidate.bbox.x1 > line_x2 + gap_tolerance:
        return False
    if candidate.bbox.x2 < line_x1 - gap_tolerance:
        return False

    compact_text = "".join((candidate.text or "").split())
    if compact_text and len(compact_text) <= 18:
        return True

    return candidate_font <= median_font * 0.8
