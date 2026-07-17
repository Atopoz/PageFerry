"""从 DOCX 正文、header、footer、footnote 与 endnote 提取 run 骨架。"""

import zipfile
from pathlib import Path
from typing import Any

from docx.document import Document as DocumentType
from docx.oxml.ns import qn
from docx.text.run import Run
from lxml import etree

from .entities import DocxRun, DocxSegment, StoryKind
from .markup import marked_span
from .xml_helpers import (
    get_run_structure_key,
    get_xml_run_structure_key,
    iter_document_story_paragraphs,
    iter_note_paragraph_elements,
    iter_text_runs,
    iter_xml_text_runs,
    parse_xml,
    xml_run_text,
)

NOTE_PARTS: dict[StoryKind, str] = {
    "footnote": "word/footnotes.xml",
    "endnote": "word/endnotes.xml",
}

_BATCH_STRUCTURAL_RUN_TAGS = {
    "footnoteReference",
    "endnoteReference",
    "commentReference",
    "drawing",
    "object",
    "fldChar",
    "instrText",
    "delInstrText",
    "ruby",
}


class DocxExtractor:
    """把可翻译段落转换为 provider-neutral 的 DOCX segment。"""

    def extract(self, document: DocumentType, source_path: Path) -> tuple[DocxSegment, ...]:
        """提取正文、header、footer、footnote 和 endnote 段落。"""

        segments: list[DocxSegment] = []
        paragraph_indices: dict[tuple[StoryKind, int], int] = {}
        for story_kind, story_index, paragraph in iter_document_story_paragraphs(document):
            counter_key = (story_kind, story_index)
            paragraph_idx = paragraph_indices.get(counter_key, 0)
            paragraph_indices[counter_key] = paragraph_idx + 1
            segment = self._extract_python_paragraph(
                paragraph,
                paragraph_idx=paragraph_idx,
                story_kind=story_kind,
                story_index=story_index,
            )
            if segment is not None:
                segments.append(segment)

        segments.extend(self._extract_note_segments(source_path))
        return tuple(segments)

    def _extract_python_paragraph(
        self,
        paragraph: Any,
        *,
        paragraph_idx: int,
        story_kind: StoryKind,
        story_index: int,
    ) -> DocxSegment | None:
        """提取一个由 python-docx 持有的正文或 header/footer 段落。"""

        visible_runs = iter_text_runs(paragraph)
        if not "".join(xml_run_text(run._element) for run in visible_runs).strip():
            return None
        run_info = [
            {
                "text": xml_run_text(run._element),
                "format": self._extract_python_run_format(run),
                "run_index": run_index,
                "structure_key": get_run_structure_key(run),
            }
            for run_index, run in enumerate(visible_runs)
        ]
        merged = self._merge_visually_equivalent_runs(run_info)
        original_runs = self._to_entities(merged)
        return DocxSegment(
            paragraph_idx=paragraph_idx,
            original_runs=original_runs,
            marked_text="".join(marked_span(run.text) for run in original_runs),
            batch_token_text=(
                self._build_batch_token_projection(paragraph, paragraph_idx)
                if story_kind == "body"
                else None
            ),
            story_kind=story_kind,
            story_index=story_index,
            paragraph_style={
                "style": paragraph.style.name if paragraph.style else None,
                "alignment": str(paragraph.alignment) if paragraph.alignment else None,
            },
        )

    def _extract_note_segments(self, source_path: Path) -> list[DocxSegment]:
        """从 package XML 提取 python-docx 未公开的 footnote/endnote 段落。"""

        segments: list[DocxSegment] = []
        roots = load_note_roots(source_path)
        for story_kind in ("footnote", "endnote"):
            root = roots.get(story_kind)
            if root is None:
                continue
            note_tag = qn(f"w:{story_kind}")
            for note_element in root.findall(note_tag):
                note_id = note_element.get(qn("w:id"))
                note_type = note_element.get(qn("w:type"))
                if note_id is None or note_type in {"separator", "continuationSeparator"}:
                    continue
                try:
                    if int(note_id) < 0:
                        continue
                except ValueError:
                    pass
                for paragraph_idx, paragraph in enumerate(
                    iter_note_paragraph_elements(note_element)
                ):
                    segment = self._extract_xml_paragraph(
                        paragraph,
                        paragraph_idx=paragraph_idx,
                        story_kind=story_kind,
                        note_id=note_id,
                    )
                    if segment is not None:
                        segments.append(segment)
        return segments

    def _extract_xml_paragraph(
        self,
        paragraph: Any,
        *,
        paragraph_idx: int,
        story_kind: StoryKind,
        note_id: str,
    ) -> DocxSegment | None:
        """提取 footnote/endnote XML 中的一个段落。"""

        visible_runs = iter_xml_text_runs(paragraph)
        if not "".join(xml_run_text(run) for run in visible_runs).strip():
            return None
        run_info = [
            {
                "text": xml_run_text(run),
                "format": self._extract_xml_run_format(run),
                "run_index": run_index,
                "structure_key": get_xml_run_structure_key(run),
            }
            for run_index, run in enumerate(visible_runs)
        ]
        merged = self._merge_visually_equivalent_runs(run_info)
        original_runs = self._to_entities(merged)
        return DocxSegment(
            paragraph_idx=paragraph_idx,
            original_runs=original_runs,
            marked_text="".join(marked_span(run.text) for run in original_runs),
            story_kind=story_kind,
            note_id=note_id,
        )

    def extract_runs(self, paragraph: Any) -> tuple[DocxRun, ...]:
        """提取表格段落的 run, 供表格提取器复用。"""

        visible_runs = iter_text_runs(paragraph)
        run_info = [
            {
                "text": xml_run_text(run._element),
                "format": self._extract_python_run_format(run),
                "run_index": run_index,
                "structure_key": get_run_structure_key(run),
            }
            for run_index, run in enumerate(visible_runs)
            if xml_run_text(run._element)
        ]
        return self._to_entities(self._merge_visually_equivalent_runs(run_info))

    def _build_batch_token_projection(self, paragraph: Any, paragraph_idx: int) -> str:
        """构造兼容既有边界、但绝不参与写回的 token 计数投影。

        计数投影会把 ``w:tab``/``w:br`` 留在 ``Run.text`` 并允许相同格式的 run
        跨这些节点合并。实际写回不能使用该投影, 否则会丢 tab; 它只负责让正文
        相邻上下文保持稳定。
        """

        run_info: list[dict[str, Any]] = []
        for run_index, run_element in enumerate(paragraph._p.iter(qn("w:r"))):
            if _is_batch_projection_structural_run(run_element):
                continue
            run = Run(run_element, paragraph)
            if not run.text:
                continue
            run_info.append(
                {
                    "text": run.text,
                    "format": self._extract_python_run_format(
                        run,
                        include_font_slots=False,
                    ),
                    "run_index": run_index,
                    # 计数投影只保护 inline container, 不把 tab/br 当作 merge boundary。
                    "structure_key": get_run_structure_key(run)[0],
                }
            )
        merged = self._merge_visually_equivalent_runs(run_info)
        marked_text = "".join(f"<span>{run['text']}</span>" for run in merged)
        return f"[PARA_{paragraph_idx}]{marked_text}"

    def _extract_python_run_format(
        self,
        run: Any,
        *,
        include_font_slots: bool = True,
    ) -> dict[str, Any]:
        """提取用于判定视觉等价的 python-docx run 属性。

        ``include_font_slots`` 只在 token projection 中关闭。真实写回边界必须
        比较完整 ``w:rFonts`` 语义, projection 则需保持既有 batch 切分。
        """

        color = None
        try:
            color = str(run.font.color.rgb) if run.font.color.rgb is not None else None
        except (AttributeError, TypeError, ValueError):
            color = None
        format_info = {
            "bold": run.bold or False,
            "italic": run.italic or False,
            "underline": run.underline or False,
            "font_name": run.font.name,
            "font_size": run.font.size.pt if run.font.size else None,
            "color": color,
            "highlight": str(run.font.highlight_color) if run.font.highlight_color else None,
            "strike": run.font.strike or False,
            "double_strike": run.font.double_strike or False,
            "all_caps": run.font.all_caps or False,
            "small_caps": run.font.small_caps or False,
            "shadow": run.font.shadow or False,
            "outline": run.font.outline or False,
        }
        if include_font_slots:
            format_info["font_slots"] = self._extract_word_font_slots(run)
        return format_info

    @staticmethod
    def _extract_word_font_slots(run: Any) -> tuple[tuple[str, str], ...]:
        """返回 ``w:rFonts`` 全部显式属性的稳定语义签名。

        除 ``ascii``、``hAnsi``、``eastAsia`` 与 ``cs`` 外, theme slot 和
        ``hint`` 同样会改变 Office 的字体解析。直接记录全部属性可避免未来新增
        slot 时再次出现跨字体合并。
        """

        run_properties = run._element.find(qn("w:rPr"))
        if run_properties is None:
            return ()
        run_fonts = run_properties.find(qn("w:rFonts"))
        if run_fonts is None:
            return ()
        return tuple(sorted((str(name), value) for name, value in run_fonts.attrib.items()))

    def _extract_xml_run_format(self, run_element: Any) -> dict[str, Any]:
        """序列化 note run 的 rPr, 作为保守的视觉等价依据。"""

        run_properties = run_element.find(qn("w:rPr"))
        serialized = (
            etree.tostring(run_properties, encoding="unicode") if run_properties is not None else ""
        )
        return {"rPr": serialized}

    def _merge_visually_equivalent_runs(
        self,
        runs_info: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """只合并相邻、同容器且视觉属性相同的 run。"""

        merged: list[dict[str, Any]] = []
        for run in runs_info:
            if not merged or not self._can_merge_runs(merged[-1], run):
                copied = run.copy()
                copied["run_indices"] = [run["run_index"]]
                merged.append(copied)
                continue
            merged[-1]["text"] += run["text"]
            merged[-1]["run_indices"].append(run["run_index"])
        return merged

    def _can_merge_runs(
        self,
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> bool:
        """判断两个相邻 run 是否能共享一个 span。"""

        return (
            first["structure_key"] == second["structure_key"]
            and first["format"] == second["format"]
        )

    def _to_entities(self, merged: list[dict[str, Any]]) -> tuple[DocxRun, ...]:
        """把内部字典转换为只读领域实体。"""

        return tuple(
            DocxRun(
                text=run["text"],
                format_info=run["format"],
                run_index=run["run_index"],
                source_run_indices=tuple(run["run_indices"]),
            )
            for run in merged
        )


def load_note_roots(source_path: Path) -> dict[StoryKind, Any]:
    """读取 package 中已有的 footnotes.xml 与 endnotes.xml。"""

    roots: dict[StoryKind, Any] = {}
    with zipfile.ZipFile(source_path) as package:
        names = set(package.namelist())
        for story_kind, part_name in NOTE_PARTS.items():
            if part_name in names:
                roots[story_kind] = parse_xml(package.read(part_name))
    return roots


def _is_batch_projection_structural_run(run_element: Any) -> bool:
    """判断一个 run 是否完全不进入兼容性 token 计数投影。"""

    return any(
        str(node.tag).split("}")[-1] in _BATCH_STRUCTURAL_RUN_TAGS for node in run_element.iter()
    )
