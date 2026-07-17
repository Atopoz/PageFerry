"""定义 PPTX 文本提取、翻译和回写过程中使用的稳定实体。"""

from dataclasses import dataclass, field
from typing import Any, Literal

PptxSegmentScope = Literal["shape", "notes", "table"]
PptxSegmentKey = tuple[
    PptxSegmentScope,
    int,
    tuple[int, ...] | None,
    int | None,
    int | None,
    int,
]


@dataclass(frozen=True, slots=True)
class PptxRun:
    """表示一个视觉样式独立的源 run, 并记录合并前的 run 索引."""

    text: str
    format_info: dict[str, Any]
    run_index: int
    source_run_indices: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PptxSegment:
    """表示一个可翻译的 PPTX 段落及其在 package 中的稳定位置."""

    scope: PptxSegmentScope
    slide_index: int
    paragraph_index: int
    original_runs: tuple[PptxRun, ...]
    marked_text: str
    shape_path: tuple[int, ...] | None = None
    row_index: int | None = None
    column_index: int | None = None

    @property
    def key(self) -> PptxSegmentKey:
        """返回提取和回写阶段共同使用的精确位置 key."""

        return (
            self.scope,
            self.slide_index,
            self.shape_path,
            self.row_index,
            self.column_index,
            self.paragraph_index,
        )

    @property
    def marker(self) -> str:
        """把段落位置编码为模型必须保留的 marker."""

        prefix = f"[SLIDE_{self.slide_index}]"
        if self.scope == "notes":
            return f"{prefix}[NOTES][PARA_{self.paragraph_index}]"

        if self.shape_path is None:
            raise ValueError(f"{self.scope} segment is missing a shape path")
        # one-based 路径与 PowerPoint 可见 shape 顺序一致. 递归 group 时无需依赖
        # 私有 shape ID, 提取和回写两端仍能得到相同路径.
        shape_path = ".".join(str(index) for index in self.shape_path)
        if self.scope == "shape":
            return f"{prefix}[SHAPE_{shape_path}][PARA_{self.paragraph_index}]"

        if self.row_index is None or self.column_index is None:
            raise ValueError("table segment is missing a cell location")
        return (
            f"{prefix}[TABLE_{shape_path}]"
            f"[CELL_{self.row_index}_{self.column_index}]"
            f"[PARA_{self.paragraph_index}]"
        )

    @property
    def translation_text(self) -> str:
        """返回同时包含位置 marker 和 run 边界 marker 的模型 payload."""

        return f"{self.marker}{self.marked_text}"
