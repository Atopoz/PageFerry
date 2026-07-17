"""提取 slide、table 与 speaker notes 文本, 同时保留精确的 run 位置。"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.presentation import Presentation as PresentationType
from pptx.shapes.group import GroupShape

from .entities import PptxSegment, PptxSegmentScope
from .markup import mark_span
from .run_normalizer import PptxRunNormalizer


class PptxExtractor:
    """提取 slide 和 speaker notes 段落, 不创建新的 notes part."""

    def __init__(self, run_normalizer: PptxRunNormalizer | None = None) -> None:
        """使用传入的 normalizer, 未传入时使用 PPTX 安全默认值."""

        self._run_normalizer = run_normalizer or PptxRunNormalizer()

    def extract(
        self,
        source: str | Path | PresentationType,
        *,
        include_notes: bool = True,
    ) -> tuple[PptxSegment, ...]:
        """提取可翻译的 shape 文本和已经存在的 speaker notes."""

        presentation = _load_presentation(source)
        segments: list[PptxSegment] = []
        for slide_index, slide in enumerate(presentation.slides, 1):
            segments.extend(
                self._extract_shapes(slide.shapes, slide_index=slide_index, parent_path=())
            )
            # 直接读取 slide.notes_slide 可能创建新的 package part. 先检查
            # has_notes_slide, 才不会让原本没有 notes 的文件平白改变结构.
            if include_notes and slide.has_notes_slide:
                notes_text_frame = slide.notes_slide.notes_text_frame
                if notes_text_frame is not None:
                    segments.extend(
                        self._extract_text_frame(
                            notes_text_frame,
                            scope="notes",
                            slide_index=slide_index,
                            shape_path=None,
                        )
                    )
        return tuple(segments)

    def _extract_shapes(
        self,
        shapes: Iterable[Any],
        *,
        slide_index: int,
        parent_path: tuple[int, ...],
    ) -> list[PptxSegment]:
        """递归遍历嵌套 group shape, 并保留稳定的 one-based 路径."""

        segments: list[PptxSegment] = []
        for shape_index, shape in enumerate(shapes, 1):
            shape_path = (*parent_path, shape_index)
            if isinstance(shape, GroupShape):
                # group shape 拥有独立的 shape tree. 如果先拍平, formatter 就无法在
                # 不重建 XML 的前提下找到原始子 shape.
                segments.extend(
                    self._extract_shapes(
                        shape.shapes,
                        slide_index=slide_index,
                        parent_path=shape_path,
                    )
                )
                continue
            if getattr(shape, "has_text_frame", False):
                segments.extend(
                    self._extract_text_frame(
                        shape.text_frame,
                        scope="shape",
                        slide_index=slide_index,
                        shape_path=shape_path,
                    )
                )
        return segments

    def _extract_text_frame(
        self,
        text_frame: Any,
        *,
        scope: PptxSegmentScope,
        slide_index: int,
        shape_path: tuple[int, ...] | None,
    ) -> list[PptxSegment]:
        """把 text frame 中每个非空段落转换为独立 segment."""

        segments: list[PptxSegment] = []
        for paragraph_index, paragraph in enumerate(text_frame.paragraphs):
            segment = segment_from_paragraph(
                paragraph,
                scope=scope,
                slide_index=slide_index,
                paragraph_index=paragraph_index,
                shape_path=shape_path,
                run_normalizer=self._run_normalizer,
            )
            if segment is not None:
                segments.append(segment)
        return segments


def segment_from_paragraph(
    paragraph: Any,
    *,
    scope: PptxSegmentScope,
    slide_index: int,
    paragraph_index: int,
    shape_path: tuple[int, ...] | None,
    run_normalizer: PptxRunNormalizer,
    row_index: int | None = None,
    column_index: int | None = None,
) -> PptxSegment | None:
    """根据视觉 run group 构建 segment, 不修改原段落."""

    normalized_runs = run_normalizer.merge_runs(list(paragraph.runs))
    if not normalized_runs:
        return None
    marked_text = "".join(mark_span(run.text) for run in normalized_runs)
    return PptxSegment(
        scope=scope,
        slide_index=slide_index,
        shape_path=shape_path,
        row_index=row_index,
        column_index=column_index,
        paragraph_index=paragraph_index,
        original_runs=normalized_runs,
        marked_text=marked_text,
    )


def _load_presentation(source: str | Path | PresentationType) -> PresentationType:
    """从路径打开 presentation, 或复用已经打开的实例."""

    if isinstance(source, (str, Path)):
        return Presentation(str(source))
    return source
