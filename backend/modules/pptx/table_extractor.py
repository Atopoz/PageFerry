"""提取 PPTX table cell 段落, 不重建 table 结构。"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pptx.presentation import Presentation as PresentationType
from pptx.shapes.group import GroupShape

from .entities import PptxSegment
from .extractor import _load_presentation, segment_from_paragraph
from .markup import should_skip_translation
from .run_normalizer import PptxRunNormalizer


class PptxTableExtractor:
    """提取 table cell 段落, 包括嵌套在 group shape 中的 table."""

    def __init__(self, run_normalizer: PptxRunNormalizer | None = None) -> None:
        """使用传入的 normalizer, 未传入时使用 PPTX 安全默认值."""

        self._run_normalizer = run_normalizer or PptxRunNormalizer()

    def extract(self, source: str | Path | PresentationType) -> tuple[PptxSegment, ...]:
        """从普通 table 和 group 内 table 中提取所有可见段落."""

        presentation = _load_presentation(source)
        segments: list[PptxSegment] = []
        for slide_index, slide in enumerate(presentation.slides, 1):
            segments.extend(
                self._extract_shapes(slide.shapes, slide_index=slide_index, parent_path=())
            )
        return tuple(segments)

    def _extract_shapes(
        self,
        shapes: Iterable[Any],
        *,
        slide_index: int,
        parent_path: tuple[int, ...],
    ) -> list[PptxSegment]:
        """递归查找 table, 并保留 shape 路径和 cell 坐标."""

        segments: list[PptxSegment] = []
        for shape_index, shape in enumerate(shapes, 1):
            shape_path = (*parent_path, shape_index)
            if isinstance(shape, GroupShape):
                # 路径必须包含每一层 group 边界. 即使多个 group 中存在同名 shape,
                # formatter 也能确定性地找到原位置.
                segments.extend(
                    self._extract_shapes(
                        shape.shapes,
                        slide_index=slide_index,
                        parent_path=shape_path,
                    )
                )
                continue
            if not getattr(shape, "has_table", False):
                continue
            for row_index, row in enumerate(shape.table.rows):
                for column_index, cell in enumerate(row.cells):
                    # merged cell 会暴露在多个 grid 坐标上. 只处理 merge origin,
                    # 避免同一段文本被重复翻译和回写.
                    if getattr(cell, "is_spanned", False) and not getattr(
                        cell, "is_merge_origin", False
                    ):
                        continue
                    # 数字、金额、日期、标点和符号没有翻译价值, 交给模型反而可能
                    # 被改写; 多段落 cell
                    # 只要含有自然语言, 就整体保留上下文并正常提取。
                    cell_text = "".join(paragraph.text for paragraph in cell.text_frame.paragraphs)
                    if should_skip_translation(cell_text):
                        continue
                    for paragraph_index, paragraph in enumerate(cell.text_frame.paragraphs):
                        segment = segment_from_paragraph(
                            paragraph,
                            scope="table",
                            slide_index=slide_index,
                            paragraph_index=paragraph_index,
                            shape_path=shape_path,
                            row_index=row_index,
                            column_index=column_index,
                            run_normalizer=self._run_normalizer,
                        )
                        if segment is not None:
                            segments.append(segment)
        return segments
