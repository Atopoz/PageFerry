"""定义 XLSX 单元格与 Excel Table 表头的翻译数据。"""

from dataclasses import dataclass
from html import escape


@dataclass(frozen=True, slots=True)
class XlsxTableHeaderRef:
    """记录一个单元格对应的 Excel Table 列。"""

    table_name: str
    column_index: int


@dataclass(frozen=True, slots=True)
class XlsxCellSegment:
    """描述一个可以安全交给模型翻译的字符串单元格。"""

    sheet_index: int
    sheet_name: str
    row_index: int
    col_index: int
    cell_ref: str
    original_text: str
    table_headers: tuple[XlsxTableHeaderRef, ...] = ()

    @property
    def key(self) -> tuple[int, str]:
        """返回工作表序号和 A1 坐标组成的稳定写回位置。"""

        return self.sheet_index, self.cell_ref

    @property
    def marker(self) -> str:
        """返回模型必须原样保留的位置 marker。"""

        return f"SHEET_{self.sheet_index}][CELL_{self.cell_ref}"

    @property
    def marked_text(self) -> str:
        """把源文本 escape 后放入唯一的 span 槽。"""

        return f"<span>{escape(self.original_text, quote=False)}</span>"

    @property
    def translation_text(self) -> str:
        """返回同时带 sheet 与 cell marker 的 provider payload。"""

        return f"[SHEET_{self.sheet_index}][CELL_{self.cell_ref}]{self.marked_text}"
