"""定义不与具体 provider 耦合的 DOCX 段落、run 与表格翻译数据。"""

from dataclasses import dataclass, field
from typing import Any, Literal

StoryKind = Literal["body", "header", "footer", "footnote", "endnote"]


@dataclass(frozen=True, slots=True)
class DocxRun:
    """描述一个可见格式 run; 它可能由相邻 XML run 合并而来。"""

    text: str
    format_info: dict[str, Any]
    run_index: int
    source_run_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DocxSegment:
    """描述正文或辅助 story 中的一个可翻译段落。"""

    paragraph_idx: int
    original_runs: tuple[DocxRun, ...]
    marked_text: str
    batch_token_text: str | None = None
    story_kind: StoryKind = "body"
    story_index: int = 0
    note_id: str | None = None
    paragraph_style: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[StoryKind, int, str | None, int]:
        """返回用于匹配提取与回填阶段的稳定位置。"""

        return self.story_kind, self.story_index, self.note_id, self.paragraph_idx

    @property
    def marker(self) -> str:
        """返回模型必须原样保留的位置 marker。"""

        if self.story_kind == "body":
            return f"PARA_{self.paragraph_idx}"
        if self.story_kind in {"header", "footer"}:
            return f"{self.story_kind.upper()}_{self.story_index}_PARA_{self.paragraph_idx}"
        return f"{self.story_kind.upper()}_{self.note_id or 'UNKNOWN'}_PARA_{self.paragraph_idx}"

    @property
    def translation_text(self) -> str:
        """返回带位置 marker 的 provider payload。"""

        return f"[{self.marker}]{self.marked_text}"

    @property
    def token_group_text(self) -> str:
        """返回只用于稳定 batch 边界的 token 计数文本。"""

        return self.batch_token_text or self.translation_text


@dataclass(frozen=True, slots=True)
class TableCell:
    """描述去重后的表格单元格及其直属段落 run。"""

    row_index: int
    col_index: int
    original_text: str
    marked_text: str
    skip_translation: bool = False
    paragraph_runs: tuple[tuple[DocxRun, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class TableRow:
    """描述完成合并单元格去重后的一行。"""

    row_index: int
    cells: tuple[TableCell, ...]
    marked_text: str


@dataclass(frozen=True, slots=True)
class TableData:
    """描述一个顶层或嵌套表格及其子表格。"""

    table_index: int
    rows: tuple[TableRow, ...]
    nesting_level: int = 0
    nested_tables: tuple["TableData", ...] = ()


@dataclass(frozen=True, slots=True)
class TableRowSegment:
    """按统一标记文本 contract 暴露一行可翻译表格。"""

    table_index: int
    row_index: int
    marked_text: str
    cell_span_counts: tuple[int, ...] = ()

    @property
    def key(self) -> tuple[int, int]:
        """返回有效表格索引和行索引。"""

        return self.table_index, self.row_index

    @property
    def marker(self) -> str:
        """返回模型必须保留的表格行 marker。"""

        return f"TABLE_{self.table_index}][ROW_{self.row_index}"

    @property
    def translation_text(self) -> str:
        """返回同时带表格和行 marker 的 provider payload。"""

        return f"[TABLE_{self.table_index}][ROW_{self.row_index}]{self.marked_text}"

    @property
    def cell_span_boundaries(self) -> frozenset[int]:
        """返回不能跨越补 English 空格的 cell span 累计边界。"""

        boundaries: set[int] = set()
        consumed = 0
        for count in self.cell_span_counts[:-1]:
            consumed += count
            boundaries.add(consumed)
        return frozenset(boundaries)
