"""把 PPTX table 译文写回原始 cell 段落。"""

from collections.abc import Mapping, Sequence

from pptx.presentation import Presentation as PresentationType

from .entities import PptxSegment, PptxSegmentKey
from .formatter import PptxFormatter, resolve_shape_path


class PptxTableFormatter:
    """应用 table 段落译文, 不重建 cell 或 table."""

    def __init__(self, paragraph_formatter: PptxFormatter | None = None) -> None:
        """复用普通 shape 的 run 映射和字号调整逻辑."""

        self._paragraph_formatter = paragraph_formatter or PptxFormatter()

    def apply(
        self,
        presentation: PresentationType,
        segments: Sequence[PptxSegment],
        translations: Mapping[PptxSegmentKey, str],
        *,
        target_language: str | None = None,
    ) -> None:
        """按照稳定的 shape, cell 和段落坐标应用译文."""

        for segment in segments:
            translated = translations.get(segment.key)
            if translated is None:
                continue
            if (
                segment.scope != "table"
                or segment.shape_path is None
                or segment.row_index is None
                or segment.column_index is None
            ):
                raise ValueError(f"invalid table segment location: {segment.key}")
            slide = presentation.slides[segment.slide_index - 1]
            shape = resolve_shape_path(slide.shapes, segment.shape_path)
            if not getattr(shape, "has_table", False):
                raise ValueError(f"shape {segment.shape_path} lost its table")
            cell = shape.table.cell(segment.row_index, segment.column_index)
            paragraph = cell.text_frame.paragraphs[segment.paragraph_index]
            self._paragraph_formatter.apply_to_paragraph(
                paragraph,
                segment,
                translated,
                target_language=target_language,
            )
