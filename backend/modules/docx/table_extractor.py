"""提取 DOCX 表格行, 并保留单元格内的段落与 run 骨架。"""

from typing import Any

from docx.document import Document as DocumentType

from .entities import TableCell, TableData, TableRow, TableRowSegment
from .extractor import DocxExtractor
from .markup import marked_span, should_skip_translation
from .xml_helpers import (
    iter_cell_paragraphs,
    iter_cell_tables,
    iter_document_tables,
    iter_table_rows,
    iter_text_runs,
)


class DocxTableExtractor:
    """按行提取顶层和嵌套表格, 并对合并单元格去重。"""

    MAX_NESTING_DEPTH = 3
    TABLE_INDEX_BASE = 1000

    def __init__(self, paragraph_extractor: DocxExtractor | None = None) -> None:
        """复用段落提取器的视觉等价 run 规则。"""

        self._paragraph_extractor = paragraph_extractor or DocxExtractor()

    def extract(self, document: DocumentType) -> tuple[TableData, ...]:
        """提取正文中的顶层表格及最多三层嵌套表格。"""

        return tuple(
            self._extract_table(table, table_index, nesting_level=0)
            for table_index, table in enumerate(iter_document_tables(document))
        )

    def translatable_rows(
        self,
        tables: tuple[TableData, ...],
    ) -> tuple[TableRowSegment, ...]:
        """按有效表格索引展平含可翻译单元格的行。"""

        result: list[TableRowSegment] = []
        self._collect_translatable_rows(tables, result, parent_index=None)
        return tuple(result)

    def _extract_table(
        self,
        table: Any,
        table_index: int,
        *,
        nesting_level: int,
    ) -> TableData:
        """提取一个表格, 并按文档顺序递归收集其子表格。"""

        rows: list[TableRow] = []
        nested_tables: list[TableData] = []
        nested_table_index = 0
        for row_index, row in enumerate(iter_table_rows(table)):
            cells: list[TableCell] = []
            for col_index, cell in self._unique_cells(row):
                cells.append(self._extract_cell(cell, row_index, col_index))
                if nesting_level < self.MAX_NESTING_DEPTH:
                    # 深度上限阻止恶意或损坏文档造成无界递归, 同时覆盖 0..3 级嵌套表格。
                    for nested_table in iter_cell_tables(cell):
                        nested_tables.append(
                            self._extract_table(
                                nested_table,
                                nested_table_index,
                                nesting_level=nesting_level + 1,
                            )
                        )
                        nested_table_index += 1
            if cells:
                rows.append(
                    TableRow(
                        row_index=row_index,
                        cells=tuple(cells),
                        marked_text="".join(cell.marked_text for cell in cells),
                    )
                )
        return TableData(
            table_index=table_index,
            rows=tuple(rows),
            nesting_level=nesting_level,
            nested_tables=tuple(nested_tables),
        )

    def _extract_cell(self, cell: Any, row_index: int, col_index: int) -> TableCell:
        """提取单元格直属段落, 空段落也保留一个空 span。"""

        paragraph_runs = []
        paragraph_marked_texts: list[str] = []
        paragraph_texts: list[str] = []
        for paragraph in iter_cell_paragraphs(cell):
            text = "".join(run.text for run in iter_text_runs(paragraph))
            runs = self._paragraph_extractor.extract_runs(paragraph)
            paragraph_runs.append(runs)
            paragraph_texts.append(text)
            paragraph_marked_texts.append(
                "".join(marked_span(run.text) for run in runs) if runs else marked_span("")
            )

        if not paragraph_marked_texts:
            paragraph_runs = [()]
            paragraph_texts = [""]
            paragraph_marked_texts = [marked_span("")]

        original_text = "".join(paragraph_texts)
        skip = should_skip_translation(original_text)
        if skip:
            # 跳过的 cell 仍占据同一标记槽, 避免后续可翻译 cell 发生位置漂移。
            paragraph_marked_texts = [marked_span(text) for text in paragraph_texts]
        marked_text = (
            "".join(f"<p>{value}</p>" for value in paragraph_marked_texts)
            if len(paragraph_marked_texts) > 1
            else paragraph_marked_texts[0]
        )
        return TableCell(
            row_index=row_index,
            col_index=col_index,
            original_text=original_text,
            marked_text=marked_text,
            skip_translation=skip,
            paragraph_runs=tuple(paragraph_runs),
        )

    def _unique_cells(self, row: Any) -> list[tuple[int, Any]]:
        """按底层 w:tc 身份对横向或纵向合并单元格去重。"""

        result: list[tuple[int, Any]] = []
        seen: set[int] = set()
        for col_index, cell in enumerate(row.cells):
            identity = id(cell._tc)
            if identity in seen:
                continue
            seen.add(identity)
            result.append((col_index, cell))
        return result

    def _collect_translatable_rows(
        self,
        tables: tuple[TableData, ...],
        result: list[TableRowSegment],
        *,
        parent_index: int | None,
    ) -> None:
        """递归收集行, 并编码不会与顶层表格冲突的有效索引。"""

        for table in tables:
            effective_index = self._effective_index(table, parent_index)
            for row in table.rows:
                if any(not cell.skip_translation for cell in row.cells):
                    result.append(
                        TableRowSegment(
                            table_index=effective_index,
                            row_index=row.row_index,
                            marked_text=row.marked_text,
                            cell_span_counts=tuple(
                                cell.marked_text.count("<span>") for cell in row.cells
                            ),
                        )
                    )
            self._collect_translatable_rows(
                table.nested_tables,
                result,
                parent_index=effective_index,
            )

    def _effective_index(self, table: TableData, parent_index: int | None) -> int:
        """按固定基数计算嵌套表格的有效索引。"""

        if table.nesting_level == 0 or parent_index is None:
            return table.table_index
        return (parent_index + 1) * self.TABLE_INDEX_BASE + table.table_index
