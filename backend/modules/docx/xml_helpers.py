"""遍历 DOCX 正文与辅助 story, 同时保护 Word 的结构型 run 边界。"""

from collections.abc import Iterator
from typing import Any

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.table import Table, _Row
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree

from .entities import StoryKind

STRUCTURAL_RUN_TAGS = {
    "footnoteReference",
    "endnoteReference",
    "commentReference",
    "drawing",
    "object",
    "fldChar",
    "instrText",
    "delInstrText",
    "ruby",
    "tab",
    "br",
    "cr",
    "lastRenderedPageBreak",
}


def parse_xml(content: bytes) -> Any:
    """用禁用 DTD、entity 和网络访问的 parser 读取 package XML。"""

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    return etree.fromstring(content, parser=parser)


def iter_body_paragraphs(document: Any) -> list[Paragraph]:
    """遍历正文直属段落, 并透明展开块级内容控件。"""

    return [
        Paragraph(paragraph_element, document)
        for paragraph_element in _iter_block_paragraph_elements(document.element.body)
    ]


def iter_document_tables(document: Any) -> list[Table]:
    """遍历正文直属表格, 并包含块级内容控件包裹的表格。"""

    return [
        Table(table_element, document)
        for table_element in _iter_block_table_elements(document.element.body)
    ]


def iter_cell_paragraphs(cell: Any) -> list[Paragraph]:
    """遍历单元格直属段落, 并包含内容控件中的段落。"""

    return [
        Paragraph(paragraph_element, cell)
        for paragraph_element in _iter_block_paragraph_elements(cell._tc)
    ]


def iter_cell_tables(cell: Any) -> list[Table]:
    """遍历单元格直属嵌套表格, 并包含内容控件中的表格。"""

    return [Table(table_element, cell) for table_element in _iter_block_table_elements(cell._tc)]


def iter_table_rows(table: Any) -> list[_Row]:
    """遍历表格行, 并透明展开包裹行的内容控件。"""

    return [_Row(row_element, table) for row_element in _iter_table_row_elements(table._tbl)]


def iter_document_story_paragraphs(
    document: Any,
) -> list[tuple[StoryKind, int, Paragraph]]:
    """按稳定顺序返回正文、header 与 footer 的直属段落。"""

    result: list[tuple[StoryKind, int, Paragraph]] = [
        ("body", 0, paragraph) for paragraph in iter_body_paragraphs(document)
    ]
    for story_kind, relationship_type in (("header", RT.HEADER), ("footer", RT.FOOTER)):
        # 直接遍历已有 relationship, 避免访问空的 section.header 时意外创建新 part。
        parts = {
            str(relationship.target_part.partname): relationship.target_part
            for relationship in document.part.rels.values()
            if relationship.reltype == relationship_type and not relationship.is_external
        }
        for story_index, part_name in enumerate(sorted(parts)):
            part = parts[part_name]
            result.extend(
                (story_kind, story_index, Paragraph(element, part))
                for element in part.element.iter(qn("w:p"))
            )
    return result


def iter_text_runs(paragraph: Any) -> list[Run]:
    """遍历段落中含可安全编辑 ``w:t`` 的可见 run。"""

    runs: list[Run] = []
    for run_element in paragraph._p.iter(qn("w:r")):
        if not _has_editable_text_slot(run_element):
            continue
        run = Run(run_element, paragraph)
        if xml_run_text(run_element):
            runs.append(run)
    return runs


def get_run_structure_key(run: Any) -> tuple[tuple[int, ...], int]:
    """返回 inline 容器及结构型 run 分隔序号组成的签名。"""

    element = getattr(run, "_element", run)
    paragraph = _ancestor_paragraph(element)
    return _container_key(element, paragraph), _structural_boundary_index(element, paragraph)


def is_structural_run_element(run_element: Any) -> bool:
    """判断 run 是否承载字段、引用、图片或显式换行等结构。"""

    return any(str(node.tag).split("}")[-1] in STRUCTURAL_RUN_TAGS for node in run_element.iter())


def iter_xml_text_runs(paragraph_element: Any) -> list[Any]:
    """遍历 note XML 段落中含可安全编辑 ``w:t`` 的 run。"""

    return [
        run_element
        for run_element in paragraph_element.iter(qn("w:r"))
        if _has_editable_text_slot(run_element) and xml_run_text(run_element)
    ]


def xml_run_text(run_element: Any) -> str:
    """读取 run 的直属 ``w:t``, 不把 drawing 等子结构文字当正文。"""

    return "".join(node.text or "" for node in run_element.findall(qn("w:t")))


def set_xml_run_text(run_element: Any, text: str) -> None:
    """替换 run 的直属 ``w:t``, 并保留同级 tab、field 或引用节点。"""

    if is_structural_run_element(run_element):
        text_nodes = run_element.findall(qn("w:t"))
        if len(text_nodes) != 1:
            raise ValueError("docx_mixed_structural_run_has_multiple_text_slots")
        _set_text_element(text_nodes[0], text)
        return

    # 普通 note run 没有 python-docx 高层对象; 只保留 rPr 后重建 w:t, 避免旧文本残片。
    for child in list(run_element):
        if child.tag != qn("w:rPr"):
            run_element.remove(child)
    text_element = run_element.makeelement(qn("w:t"))
    _set_text_element(text_element, text)
    run_element.append(text_element)


def get_xml_run_structure_key(run_element: Any) -> tuple[tuple[int, ...], int]:
    """返回 note XML run 的容器和结构分隔签名。"""

    paragraph = _ancestor_paragraph(run_element)
    return _container_key(run_element, paragraph), _structural_boundary_index(
        run_element, paragraph
    )


def iter_note_paragraph_elements(note_element: Any) -> list[Any]:
    """遍历一个 footnote 或 endnote 中的全部段落。"""

    # Note 和 header 常用 table 做布局; 这里进入 cell, 但仍由原 paragraph 节点原位回填。
    return list(note_element.iter(qn("w:p")))


def _ancestor_paragraph(element: Any) -> Any | None:
    """向上查找包含指定节点的 w:p 元素。"""

    parent = element.getparent()
    while parent is not None and parent.tag != qn("w:p"):
        parent = parent.getparent()
    return parent


def _container_key(element: Any, paragraph: Any | None) -> tuple[int, ...]:
    """生成 run 到段落之间的 inline 容器签名。"""

    parent = element.getparent()
    container_ids: list[int] = []
    while parent is not None and parent is not paragraph:
        container_ids.append(id(parent))
        parent = parent.getparent()
    return tuple(reversed(container_ids))


def _structural_boundary_index(element: Any, paragraph: Any | None) -> int:
    """给普通 run 与结构混排 run 分配不会相互合并的边界序号。"""

    if paragraph is None:
        return 0
    boundary = 0
    for candidate in paragraph.iter(qn("w:r")):
        if candidate is element:
            break
        if is_structural_run_element(candidate):
            boundary += 1
    # 奇数专门留给自身含结构节点的 mixed run。这样前一 run、mixed run 与后一
    # run 即使视觉格式完全相同也不会合并, 回填就不会把文字移到 tab 的另一侧。
    return boundary * 2 + int(is_structural_run_element(element))


def _has_editable_text_slot(run_element: Any) -> bool:
    """确认 run 至少有正文, 且 mixed structural run 只有一个可写 text slot。"""

    text_nodes = run_element.findall(qn("w:t"))
    if not text_nodes:
        return False
    return not is_structural_run_element(run_element) or len(text_nodes) == 1


def _set_text_element(text_element: Any, text: str) -> None:
    """写入一个 ``w:t`` 并正确维护 ``xml:space``。"""

    space_attribute = "{http://www.w3.org/XML/1998/namespace}space"
    if text[:1].isspace() or text[-1:].isspace():
        text_element.set(space_attribute, "preserve")
    else:
        text_element.attrib.pop(space_attribute, None)
    text_element.text = text


def _iter_block_paragraph_elements(container: Any) -> Iterator[Any]:
    """遍历容器直属段落, 不进入已发现的表格内部。"""

    for child in container.iterchildren():
        if child.tag == qn("w:p"):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_block_paragraph_elements(content)


def _iter_block_table_elements(container: Any) -> Iterator[Any]:
    """遍历容器直属表格, 不进入已发现的表格内部。"""

    for child in container.iterchildren():
        if child.tag == qn("w:tbl"):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_block_table_elements(content)


def _iter_table_row_elements(container: Any) -> Iterator[Any]:
    """遍历表格直属行, 并透明展开块级内容控件。"""

    for child in container.iterchildren():
        if child.tag == qn("w:tr"):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_table_row_elements(content)
