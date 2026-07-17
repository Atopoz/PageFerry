"""把 DOCX 表格行译文写回原单元格, 并保留合并与嵌套结构。"""

import re
from typing import Any

from docx.document import Document as DocumentType

from .entities import TableCell, TableData, TableRow
from .formatter import DocxFormatter
from .markup import paragraph_contents
from .xml_helpers import (
    iter_cell_paragraphs,
    iter_cell_tables,
    iter_document_tables,
    iter_table_rows,
)


class DocxTableFormatter:
    """按提取时的有效索引递归回填顶层与嵌套表格。"""

    MAX_NESTING_DEPTH = 3
    TABLE_INDEX_BASE = 1000

    def __init__(self, paragraph_formatter: DocxFormatter | None = None) -> None:
        """复用段落 formatter 的原 run 回填规则。"""

        self._paragraph_formatter = paragraph_formatter or DocxFormatter()

    def apply(
        self,
        document: DocumentType,
        tables: tuple[TableData, ...],
        translations: dict[tuple[int, int], str],
        *,
        target_language: str | None = None,
    ) -> None:
        """把所有存在译文的表格行写回 document。"""

        document_tables = iter_document_tables(document)
        for table_data in tables:
            if table_data.table_index >= len(document_tables):
                raise ValueError("docx_table_missing")
            self._apply_recursive(
                document_tables[table_data.table_index],
                table_data,
                translations,
                parent_index=None,
                target_language=target_language,
            )

    def _apply_recursive(
        self,
        document_table: Any,
        table_data: TableData,
        translations: dict[tuple[int, int], str],
        *,
        parent_index: int | None,
        target_language: str | None,
    ) -> None:
        """回填一个表格, 然后按相同遍历顺序处理其子表格。"""

        effective_index = self._effective_index(table_data, parent_index)
        document_rows = iter_table_rows(document_table)
        for row_data in table_data.rows:
            translated = translations.get((effective_index, row_data.row_index))
            if translated is None:
                continue
            if row_data.row_index >= len(document_rows):
                raise ValueError("docx_table_row_missing")
            self._apply_row(
                document_rows[row_data.row_index],
                row_data,
                translated,
                target_language=target_language,
            )

        if table_data.nesting_level >= self.MAX_NESTING_DEPTH:
            # extractor 到 depth=3 就不再进入子表; formatter 必须在同一边界停止,
            # 否则真实存在的更深层表格会被误报为 count mismatch。
            return

        nested_document_tables = self._collect_nested_tables(document_table)
        if len(nested_document_tables) != len(table_data.nested_tables):
            raise ValueError("docx_nested_table_count_mismatch")
        for nested_data, nested_document_table in zip(
            table_data.nested_tables,
            nested_document_tables,
            strict=True,
        ):
            self._apply_recursive(
                nested_document_table,
                nested_data,
                translations,
                parent_index=effective_index,
                target_language=target_language,
            )

    def _apply_row(
        self,
        document_row: Any,
        row_data: TableRow,
        translated: str,
        *,
        target_language: str | None,
    ) -> None:
        """按单元格标记长度切分一行译文并逐 cell 回填。"""

        actual_cells = [cell for _, cell in self._unique_cells(document_row)]
        if len(actual_cells) != len(row_data.cells):
            raise ValueError("docx_table_cell_count_mismatch")
        remaining = translated
        for cell_data, document_cell in zip(row_data.cells, actual_cells, strict=True):
            cell_translation, remaining = self._take_cell_markup(
                remaining,
                cell_data.marked_text,
            )
            if not cell_data.skip_translation:
                self._apply_cell(
                    document_cell,
                    cell_data,
                    cell_translation,
                    target_language=target_language,
                )
        if remaining.strip():
            raise ValueError("docx_table_row_markup_overflow")

    def _apply_cell(
        self,
        document_cell: Any,
        cell_data: TableCell,
        translated: str,
        *,
        target_language: str | None,
    ) -> None:
        """把单元格译文按原段落数量写回。"""

        paragraphs = iter_cell_paragraphs(document_cell)
        translated_paragraphs = paragraph_contents(translated)
        if len(paragraphs) != len(cell_data.paragraph_runs):
            raise ValueError("docx_table_paragraph_count_changed")
        if len(translated_paragraphs) != len(cell_data.paragraph_runs):
            raise ValueError("docx_table_translation_paragraph_mismatch")
        for paragraph, original_runs, paragraph_translation in zip(
            paragraphs,
            cell_data.paragraph_runs,
            translated_paragraphs,
            strict=True,
        ):
            self._paragraph_formatter.rebuild_paragraph(
                paragraph,
                original_runs,
                paragraph_translation,
                target_language=target_language,
            )

    def _take_cell_markup(self, remaining: str, source_cell: str) -> tuple[str, str]:
        """从行首消费与源 cell 骨架等量的 p 或 span。"""

        if "<p>" in source_cell:
            count = source_cell.count("<p>")
            pattern = re.compile(r"<p>.*?</p>", re.DOTALL)
        else:
            count = source_cell.count("<span>")
            pattern = re.compile(r"<span>.*?</span>", re.DOTALL)
        parts: list[str] = []
        for _index in range(count):
            # 使用 search 可容忍模型在相邻 tag 之间插入换行。
            # 这里仍坚持从行首消费, 只跳过 validator 已确认无害的纯空白。
            remaining = remaining.lstrip()
            match = pattern.match(remaining)
            if match is None:
                raise ValueError("docx_table_cell_markup_mismatch")
            parts.append(match.group(0))
            remaining = remaining[match.end() :]
        # 逐 cell 消费而不是按文本长度切片, 因为翻译后字符长度必然变化。
        return "".join(parts), remaining

    def _collect_nested_tables(self, document_table: Any) -> list[Any]:
        """按提取顺序收集当前表格直属单元格里的子表格。"""

        nested: list[Any] = []
        for row in iter_table_rows(document_table):
            for _, cell in self._unique_cells(row):
                nested.extend(iter_cell_tables(cell))
        return nested

    def _unique_cells(self, row: Any) -> list[tuple[int, Any]]:
        """按 w:tc 元素身份返回去重后的单元格。"""

        result: list[tuple[int, Any]] = []
        seen: set[int] = set()
        for col_index, cell in enumerate(row.cells):
            identity = id(cell._tc)
            if identity in seen:
                continue
            seen.add(identity)
            result.append((col_index, cell))
        return result

    def _effective_index(self, table: TableData, parent_index: int | None) -> int:
        """按提取器相同的基数计算嵌套表格有效索引。"""

        if table.nesting_level == 0 or parent_index is None:
            return table.table_index
        return (parent_index + 1) * self.TABLE_INDEX_BASE + table.table_index
