"""从同一份 DOCX 翻译结果生成段内原文与译文堆叠的双语派生物。"""

from copy import deepcopy
from typing import Any

from docx.document import Document as DocumentType
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .entities import DocxSegment
from .xml_helpers import iter_body_paragraphs


class DocxBilingualFormatter:
    """把正文源段落与已翻译段落合成一段, 避免重复编号和段落样式。"""

    def apply(
        self,
        source_document: DocumentType,
        translated_document: DocumentType,
        segments: tuple[DocxSegment, ...],
    ) -> None:
        """只合并正文段落; 表格与辅助 story 保留既有译文结构。"""

        source_paragraphs = iter_body_paragraphs(source_document)
        translated_paragraphs = iter_body_paragraphs(translated_document)
        body_segments = tuple(segment for segment in segments if segment.story_kind == "body")

        # 每次替换都会改变底层 XML sibling, 但预先取得的 Paragraph 仍指向原节点;
        # 逆序处理还能避免内容控件中的段落位置在异常文档里产生连锁偏移。
        for segment in reversed(body_segments):
            paragraph_index = segment.paragraph_idx
            if not 0 <= paragraph_index < len(source_paragraphs):
                raise ValueError(f"docx_bilingual_source_missing:{segment.marker}")
            if paragraph_index >= len(translated_paragraphs):
                raise ValueError(f"docx_bilingual_translation_missing:{segment.marker}")

            source_paragraph = source_paragraphs[paragraph_index]
            translated_paragraph = translated_paragraphs[paragraph_index]
            bilingual_element = self._build_paragraph(
                source_paragraph._p,
                translated_paragraph._p,
            )
            parent = translated_paragraph._p.getparent()
            if parent is None:
                raise ValueError(f"docx_bilingual_parent_missing:{segment.marker}")
            parent.replace(translated_paragraph._p, bilingual_element)

    @staticmethod
    def _build_paragraph(source_element: Any, translated_element: Any) -> Any:
        """复制源段落属性, 并在一个换行后追加译文 inline 内容。"""

        bilingual_element = deepcopy(source_element)
        break_run = OxmlElement("w:r")
        break_run.append(OxmlElement("w:br"))
        bilingual_element.append(break_run)
        for child in translated_element:
            if child.tag != qn("w:pPr"):
                bilingual_element.append(deepcopy(child))
        return bilingual_element
