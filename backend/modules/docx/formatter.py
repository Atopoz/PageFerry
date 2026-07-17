"""把 DOCX 译文写回原有 run, 并处理缺少高层 API 的 note story。"""

import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from docx.document import Document as DocumentType
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from modules.translation.office_fonts import (
    OfficeScriptSlot,
    resolve_office_fallback_font,
    resolve_office_script_slot,
)

from .entities import DocxRun, DocxSegment, StoryKind
from .extractor import NOTE_PARTS
from .markup import span_contents
from .xml_helpers import (
    is_structural_run_element,
    iter_document_story_paragraphs,
    iter_note_paragraph_elements,
    iter_text_runs,
    iter_xml_text_runs,
    parse_xml,
    set_xml_run_text,
)


class DocxFormatter:
    """在原段落节点内分配译文, 保留 hyperlink、SDT 与字段节点。"""

    def apply(
        self,
        document: DocumentType,
        segments: tuple[DocxSegment, ...],
        translations: dict[tuple[StoryKind, int, str | None, int], str],
        *,
        target_language: str | None = None,
    ) -> None:
        """把正文、header 与 footer 译文写回当前 document 对象。"""

        targets: dict[tuple[StoryKind, int, str | None, int], Any] = {}
        paragraph_indices: dict[tuple[StoryKind, int], int] = {}
        for story_kind, story_index, paragraph in iter_document_story_paragraphs(document):
            counter_key = (story_kind, story_index)
            paragraph_idx = paragraph_indices.get(counter_key, 0)
            paragraph_indices[counter_key] = paragraph_idx + 1
            targets[(story_kind, story_index, None, paragraph_idx)] = paragraph

        for segment in segments:
            if segment.story_kind in {"footnote", "endnote"}:
                continue
            paragraph = targets.get(segment.key)
            if paragraph is None:
                raise ValueError(f"docx_paragraph_missing:{segment.marker}")
            translated = translations.get(segment.key, segment.marked_text)
            self.rebuild_paragraph(
                paragraph,
                segment.original_runs,
                translated,
                target_language=target_language,
            )

    def rebuild_paragraph(
        self,
        paragraph: Any,
        original_runs: tuple[DocxRun, ...],
        translated_text: str,
        *,
        target_language: str | None = None,
    ) -> None:
        """在既有可见 run 中按源分组写入 span 内容。"""

        parts = span_contents(translated_text)
        if not original_runs:
            # 空段落用一个空 span 占位, 但没有可写 run; 保留原段落即可。
            if parts == ("",):
                return
            raise ValueError("docx_empty_paragraph_changed")
        if len(parts) != len(original_runs):
            raise ValueError("docx_span_count_mismatch")
        visible_runs = iter_text_runs(paragraph)
        groups = self._build_run_groups(len(visible_runs), original_runs)
        for translated_part, group in zip(parts, groups, strict=True):
            if not group:
                raise ValueError("docx_run_group_missing")
            target_run = visible_runs[group[0]]
            self._set_python_run_text(target_run, translated_part)
            self._apply_run_font_fallback(target_run, translated_part, target_language)
            # 只清空同一源 span 合并来的额外 run; 段落和容器节点本身绝不能重建。
            for run_index in group[1:]:
                self._set_python_run_text(visible_runs[run_index], "")

    def apply_note_translations(
        self,
        package_path: Path,
        segments: tuple[DocxSegment, ...],
        translations: dict[tuple[StoryKind, int, str | None, int], str],
        *,
        target_language: str | None = None,
    ) -> None:
        """在临时 DOCX package 中写回 footnote 与 endnote 译文。"""

        note_segments = tuple(
            segment for segment in segments if segment.story_kind in {"footnote", "endnote"}
        )
        if not note_segments:
            return

        updated_parts: dict[str, bytes] = {}
        with zipfile.ZipFile(package_path) as package:
            names = set(package.namelist())
            for story_kind in ("footnote", "endnote"):
                part_name = NOTE_PARTS[story_kind]
                story_segments = tuple(
                    segment for segment in note_segments if segment.story_kind == story_kind
                )
                if not story_segments or part_name not in names:
                    continue
                root = parse_xml(package.read(part_name))
                targets = self._note_paragraph_targets(root, story_kind)
                for segment in story_segments:
                    paragraph = targets.get(segment.key)
                    if paragraph is None:
                        raise ValueError(f"docx_note_paragraph_missing:{segment.marker}")
                    translated = translations.get(segment.key, segment.marked_text)
                    self._rebuild_xml_paragraph(
                        paragraph,
                        segment.original_runs,
                        translated,
                        target_language=target_language,
                    )
                updated_parts[part_name] = etree.tostring(
                    root,
                    encoding="UTF-8",
                    xml_declaration=True,
                    standalone=True,
                )
        if updated_parts:
            self._rewrite_package(package_path, updated_parts)

    def _note_paragraph_targets(
        self,
        root: Any,
        story_kind: StoryKind,
    ) -> dict[tuple[StoryKind, int, str | None, int], Any]:
        """按 note id 与段落序号索引 package XML 中的目标段落。"""

        targets: dict[tuple[StoryKind, int, str | None, int], Any] = {}
        note_tag = qn(f"w:{story_kind}")
        for note_element in root.findall(note_tag):
            note_id = note_element.get(qn("w:id"))
            if note_id is None:
                continue
            for paragraph_idx, paragraph in enumerate(iter_note_paragraph_elements(note_element)):
                targets[(story_kind, 0, note_id, paragraph_idx)] = paragraph
        return targets

    def _rebuild_xml_paragraph(
        self,
        paragraph: Any,
        original_runs: tuple[DocxRun, ...],
        translated_text: str,
        *,
        target_language: str | None = None,
    ) -> None:
        """在 note XML 原 run 节点中写入译文。"""

        parts = span_contents(translated_text)
        if len(parts) != len(original_runs):
            raise ValueError("docx_note_span_count_mismatch")
        visible_runs = iter_xml_text_runs(paragraph)
        groups = self._build_run_groups(len(visible_runs), original_runs)
        for translated_part, group in zip(parts, groups, strict=True):
            if not group:
                raise ValueError("docx_note_run_group_missing")
            target_run = visible_runs[group[0]]
            set_xml_run_text(target_run, translated_part)
            self._apply_xml_run_font_fallback(target_run, translated_part, target_language)
            for run_index in group[1:]:
                set_xml_run_text(visible_runs[run_index], "")

    def _apply_run_font_fallback(
        self,
        run: Any,
        translated_text: str,
        target_language: str | None,
    ) -> None:
        """只为 python-docx run 的目标 script slot 设置 fallback。"""

        script_slot = resolve_office_script_slot(
            target_language=target_language,
            translated_text=translated_text,
        )
        if script_slot is None:
            return
        fallback = self._fallback_for_fonts(
            target_language=target_language,
            translated_text=translated_text,
            current_font_names=self._run_font_names(run, script_slot, translated_text),
        )
        if fallback is None:
            return
        run_properties = run._element.get_or_add_rPr()
        self._set_xml_font_slot(run_properties, script_slot, fallback)

    @staticmethod
    def _set_python_run_text(run: Any, text: str) -> None:
        """写回 python-docx run, mixed structural run 只改直属 ``w:t``。"""

        if is_structural_run_element(run._element):
            set_xml_run_text(run._element, text)
        else:
            run.text = text

    @classmethod
    def _run_font_names(
        cls,
        run: Any,
        script_slot: OfficeScriptSlot,
        translated_text: str,
    ) -> tuple[str | None, ...]:
        """读取 python-docx run 中目标 script 实际会使用的字体名。"""

        run_properties = run._element.find(qn("w:rPr"))
        return cls._xml_font_names(run_properties, script_slot, translated_text)

    def _apply_xml_run_font_fallback(
        self,
        run: Any,
        translated_text: str,
        target_language: str | None,
    ) -> None:
        """为 footnote/endnote 的底层 ``w:r`` 写入相同字体 fallback。"""

        script_slot = resolve_office_script_slot(
            target_language=target_language,
            translated_text=translated_text,
        )
        if script_slot is None:
            return
        run_properties = run.find(qn("w:rPr"))
        fallback = self._fallback_for_fonts(
            target_language=target_language,
            translated_text=translated_text,
            current_font_names=self._xml_font_names(
                run_properties,
                script_slot,
                translated_text,
            ),
        )
        if fallback is None:
            return
        if run_properties is None:
            run_properties = OxmlElement("w:rPr")
            run.insert(0, run_properties)
        self._set_xml_font_slot(run_properties, script_slot, fallback)

    @classmethod
    def _xml_font_names(
        cls,
        run_properties: Any | None,
        script_slot: OfficeScriptSlot,
        translated_text: str,
    ) -> tuple[str | None, ...]:
        """从底层 ``w:rPr`` 读取目标 script 对应的字体名。"""

        if run_properties is None:
            return (None,)
        fonts = run_properties.find(qn("w:rFonts"))
        if fonts is None:
            return (None,)
        if script_slot == "east_asian":
            # Word 常用 w:hint="eastAsia" 配合 hAnsi/ascii 保存 CJK 字体,
            # 一些文档只声明这些 fallback slot; 缺少显式 eastAsia 不代表字体未知。
            attributes = ("eastAsia", "hAnsi", "ascii", "cs")
            return (cls._first_xml_font_name(fonts, attributes),)
        if script_slot == "complex_script":
            attributes = ("cs", "hAnsi", "ascii")
            return (cls._first_xml_font_name(fonts, attributes),)
        return cls._latin_font_names(fonts, translated_text)

    @staticmethod
    def _first_xml_font_name(fonts: Any, attributes: tuple[str, ...]) -> str | None:
        """按 Word 的有效 fallback 顺序返回首个显式字体名。"""

        for attribute in attributes:
            value = fonts.get(qn(f"w:{attribute}"))
            if value:
                return value
        return None

    @staticmethod
    def _latin_font_names(fonts: Any, translated_text: str) -> tuple[str | None, ...]:
        """按 ASCII 与 High ANSI 字符选择 Word Latin slot 的有效字体。"""

        ascii_font = fonts.get(qn("w:ascii"))
        high_ansi_font = fonts.get(qn("w:hAnsi"))
        names: list[str | None] = []
        if any(character.isascii() and character.isalpha() for character in translated_text):
            names.append(ascii_font or high_ansi_font)
        if any(not character.isascii() and character.isalpha() for character in translated_text):
            names.append(high_ansi_font or ascii_font)
        # resolver 只有发现 Latin/Cyrillic 字母才会走到这里。保底仍返回未知字体,
        # 避免异常 Unicode 分类让 fallback 被静默跳过。
        if not names:
            names.append(ascii_font or high_ansi_font)
        return tuple(dict.fromkeys(names))

    @staticmethod
    def _fallback_for_fonts(
        *,
        target_language: str | None,
        translated_text: str,
        current_font_names: tuple[str | None, ...],
    ) -> str | None:
        """任一实际使用字体不兼容时返回语种 preset。"""

        for current_font_name in current_font_names:
            fallback = resolve_office_fallback_font(
                target_language=target_language,
                translated_text=translated_text,
                current_font_name=current_font_name,
            )
            if fallback is not None:
                return fallback
        return None

    @staticmethod
    def _set_xml_font_slot(
        run_properties: Any,
        script_slot: OfficeScriptSlot,
        font_name: str,
    ) -> None:
        """只写目标 script slot, 保留 run 的其它字体声明。"""

        fonts = run_properties.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            run_properties.insert(0, fonts)
        if script_slot == "latin":
            # 同一 Latin run 可以同时包含 ASCII 与 Extended Latin。判定需要
            # fallback 后统一两个 Latin slot, 才不会让 ``Caf`` 和 ``é`` 分裂字体。
            attributes = ("ascii", "hAnsi")
        elif script_slot == "east_asian":
            attributes = ("eastAsia",)
        else:
            attributes = ("cs",)
        for attribute in attributes:
            fonts.set(qn(f"w:{attribute}"), font_name)

    def _build_run_groups(
        self,
        run_count: int,
        original_runs: tuple[DocxRun, ...],
    ) -> tuple[tuple[int, ...], ...]:
        """把合并 span 映射回当前段落中的原始可见 run 索引。"""

        groups: list[tuple[int, ...]] = []
        for original_run in original_runs:
            source_indices = original_run.source_run_indices or (original_run.run_index,)
            groups.append(tuple(index for index in source_indices if 0 <= index < run_count))
        return tuple(groups)

    def _rewrite_package(self, package_path: Path, updated_parts: dict[str, bytes]) -> None:
        """用同目录临时 ZIP 替换 package 中指定的 XML part。"""

        descriptor, temp_name = tempfile.mkstemp(
            dir=package_path.parent,
            prefix=f".{package_path.name}.notes.",
            suffix=".docx",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            with zipfile.ZipFile(package_path) as source, zipfile.ZipFile(temporary, "w") as target:
                target.comment = source.comment
                for info in source.infolist():
                    target.writestr(info, updated_parts.get(info.filename, source.read(info)))
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            # 临时 package 与目标同目录, os.replace 才不会跨文件系统失去原子性。
            os.replace(temporary, package_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
