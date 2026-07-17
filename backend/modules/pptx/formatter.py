"""把译文 span 写回现有 PPTX run, 不重建 shape 或 notes relationship。"""

from collections.abc import Mapping, Sequence
from typing import Any

from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.presentation import Presentation as PresentationType
from pptx.shapes.group import GroupShape
from pptx.util import Pt

from modules.translation.office_fonts import (
    OfficeScriptSlot,
    resolve_office_fallback_font,
    resolve_office_script_slot,
)

from .entities import PptxSegment, PptxSegmentKey
from .markup import decode_span_value, parse_marked_spans


class PptxFormatter:
    """把译文写入现有 run, 只在确有需要时缩小字体."""

    MIN_FONT_PT = 8.0
    DEFAULT_BASE_PT = 16.0
    MIN_SCALE = 0.75
    _PROXY_PUNCTUATION = ",.;:!?()[]{}-\u2013\u2014\"'"

    def apply(
        self,
        presentation: PresentationType,
        segments: Sequence[PptxSegment],
        translations: Mapping[PptxSegmentKey, str],
        *,
        target_language: str | None = None,
    ) -> None:
        """把所有 shape 和 notes 译文应用到已打开的 presentation."""

        for segment in segments:
            translated = translations.get(segment.key)
            if translated is None:
                continue
            paragraph = self._resolve_paragraph(presentation, segment)
            self.apply_to_paragraph(
                paragraph,
                segment,
                translated,
                target_language=target_language,
            )

    def _resolve_paragraph(self, presentation: PresentationType, segment: PptxSegment) -> Any:
        """定位 segment 对应的原段落, 不创建新的 part."""

        slide = presentation.slides[segment.slide_index - 1]
        if segment.scope == "notes":
            # 只有提取阶段确认 relationship 已存在时才回写 notes. 这样不会给无关
            # slide 静默增加 notes part.
            if not slide.has_notes_slide:
                raise ValueError(f"slide {segment.slide_index} lost its notes relationship")
            text_frame = slide.notes_slide.notes_text_frame
            if text_frame is None:
                raise ValueError(f"slide {segment.slide_index} lost its notes text frame")
            return text_frame.paragraphs[segment.paragraph_index]

        if segment.scope != "shape" or segment.shape_path is None:
            raise ValueError(f"unsupported segment scope: {segment.scope}")
        shape = resolve_shape_path(slide.shapes, segment.shape_path)
        if not getattr(shape, "has_text_frame", False):
            raise ValueError(f"shape {segment.shape_path} lost its text frame")
        return shape.text_frame.paragraphs[segment.paragraph_index]

    def apply_to_paragraph(
        self,
        paragraph: Any,
        segment: PptxSegment,
        translated: str,
        *,
        target_language: str | None = None,
    ) -> None:
        """把译文 span 映射回源 run group, 并在需要时缩小字体."""

        encoded_parts = parse_marked_spans(translated)
        if encoded_parts is None or len(encoded_parts) != len(segment.original_runs):
            raise ValueError(f"invalid translated spans for {segment.key}")

        original_text = "".join(run.text or "" for run in paragraph.runs)
        translated_parts = [
            decode_span_value(value, source_run.text)
            for value, source_run in zip(encoded_parts, segment.original_runs, strict=True)
        ]
        if not self._apply_with_run_mapping(paragraph, translated_parts, segment):
            self._apply_sequentially(paragraph, translated_parts)
        self._apply_font_fallbacks(paragraph, target_language)
        self._optimize_font_size(paragraph, original_text, "".join(translated_parts))

    def _apply_font_fallbacks(self, paragraph: Any, target_language: str | None) -> None:
        """为已写回的 run 设置目标脚本需要的 DrawingML 字体。"""

        for run in paragraph.runs:
            script_slot = resolve_office_script_slot(
                target_language=target_language,
                translated_text=run.text,
            )
            if script_slot is None:
                continue
            fallback = resolve_office_fallback_font(
                target_language=target_language,
                translated_text=run.text,
                current_font_name=self._run_font_name(run, script_slot),
            )
            if fallback is None:
                continue
            self._set_drawingml_font_slot(run, script_slot, fallback)

    @staticmethod
    def _run_font_name(run: Any, script_slot: OfficeScriptSlot) -> str | None:
        """读取 DrawingML 中目标 script 对应的字体名。"""

        run_properties = run._r.find(qn("a:rPr"))
        if run_properties is None:
            return None
        tags = {
            "latin": ("a:latin",),
            "east_asian": ("a:ea", "a:latin", "a:cs"),
            "complex_script": ("a:cs", "a:latin", "a:ea"),
        }[script_slot]
        # DrawingML 允许未声明 a:ea/a:cs 的 run 继续使用 a:latin。读取这种
        # 有效 fallback 不等于写回所有 slot; 真正替换时仍只写目标 slot。
        for tag in tags:
            typeface = run_properties.find(qn(tag))
            if typeface is not None and typeface.get("typeface"):
                return typeface.get("typeface")
        return None

    @staticmethod
    def _set_drawingml_font_slot(
        run: Any,
        script_slot: OfficeScriptSlot,
        font_name: str,
    ) -> None:
        """只写目标 DrawingML script slot, 保留其它字体声明。"""

        run_properties = run._r.get_or_add_rPr()
        if script_slot == "latin":
            run_properties.get_or_add_latin().set("typeface", font_name)
            return

        east_asian = run_properties.find(qn("a:ea"))
        complex_script = run_properties.find(qn("a:cs"))
        if script_slot == "east_asian":
            if east_asian is None:
                east_asian = OxmlElement("a:ea")
                run_properties.insert_element_before(
                    east_asian,
                    "a:cs",
                    "a:sym",
                    "a:hlinkClick",
                    "a:hlinkMouseOver",
                    "a:rtl",
                    "a:extLst",
                )
            east_asian.set("typeface", font_name)
            return

        if complex_script is None:
            complex_script = OxmlElement("a:cs")
            run_properties.insert_element_before(
                complex_script,
                "a:sym",
                "a:hlinkClick",
                "a:hlinkMouseOver",
                "a:rtl",
                "a:extLst",
            )
        complex_script.set("typeface", font_name)

    @staticmethod
    def _apply_with_run_mapping(
        paragraph: Any, translated_parts: list[str], segment: PptxSegment
    ) -> bool:
        """把每个译文 span 写入对应源 run group 的第一个 run."""

        paragraph_runs = list(paragraph.runs)
        if not paragraph_runs:
            return False
        for source_run in segment.original_runs:
            indexes = source_run.source_run_indices or (source_run.run_index,)
            if not indexes or indexes[0] >= len(paragraph_runs):
                return False
        for translated, source_run in zip(translated_parts, segment.original_runs, strict=True):
            indexes = source_run.source_run_indices or (source_run.run_index,)
            paragraph_runs[indexes[0]].text = translated
            for extra_index in indexes[1:]:
                if extra_index < len(paragraph_runs):
                    paragraph_runs[extra_index].text = ""
        return True

    @staticmethod
    def _apply_sequentially(paragraph: Any, translated_parts: list[str]) -> None:
        """源索引失配时使用保守的顺序映射."""

        paragraph_runs = list(paragraph.runs)
        if not paragraph_runs:
            raise ValueError("cannot apply translated text to a paragraph without runs")
        for index, run in enumerate(paragraph_runs):
            run.text = translated_parts[index] if index < len(translated_parts) else ""
        if len(translated_parts) > len(paragraph_runs):
            paragraph_runs[-1].text += "".join(translated_parts[len(paragraph_runs) :])

    def _optimize_font_size(self, paragraph: Any, original_text: str, translated_text: str) -> None:
        """译文变长时缩小字体, 但绝不放大原字号."""

        original_length = self._proxy_length(original_text)
        translated_length = self._proxy_length(translated_text)
        if original_length < 1 or translated_length <= original_length:
            return

        base_size = self._base_font_size(paragraph)
        scale_floor = max(self.MIN_FONT_PT / max(base_size, 1e-6), self.MIN_SCALE)
        scale = max(
            scale_floor,
            min(1.0, (original_length / translated_length) ** 0.5),
        )
        for run in paragraph.runs:
            current_size = run.font.size.pt if run.font.size else base_size
            candidate = max(self.MIN_FONT_PT, round(current_size * scale))
            # 短译文不应借机改写 deck 的排版. 只有内容膨胀需要空间时才缩小字号,
            # 原有的大字号保持不变.
            if candidate < current_size:
                run.font.size = Pt(candidate)

    @classmethod
    def _proxy_length(cls, text: str) -> float:
        """估算视觉宽度, 对 CJK 和大写字形使用更高权重."""

        weight = 0.0
        for character in text:
            codepoint = ord(character)
            if 0x3400 <= codepoint <= 0x4DBF or 0x4E00 <= codepoint <= 0x9FFF:
                weight += 1.8
            elif "A" <= character <= "Z":
                weight += 1.15
            elif "a" <= character <= "z":
                weight += 1.0
            elif "0" <= character <= "9":
                weight += 0.9
            elif character.isspace() or character in cls._PROXY_PUNCTUATION:
                weight += 0.4
            else:
                weight += 1.0
        return weight

    @classmethod
    def _base_font_size(cls, paragraph: Any) -> float:
        """返回首个显式字号, 找不到时使用稳定的 fallback 字号."""

        for run in paragraph.runs:
            if run.font.size:
                return run.font.size.pt
        paragraph_font = getattr(paragraph, "font", None)
        if paragraph_font is not None and paragraph_font.size:
            return paragraph_font.size.pt
        return cls.DEFAULT_BASE_PT


def resolve_shape_path(shapes: Any, shape_path: tuple[int, ...]) -> Any:
    """沿 one-based 路径查找嵌套 group shape tree 中的目标 shape."""

    if not shape_path:
        raise ValueError("shape path cannot be empty")
    current_shapes = shapes
    shape: Any = None
    for depth, one_based_index in enumerate(shape_path):
        if one_based_index < 1 or one_based_index > len(current_shapes):
            raise ValueError(f"shape path no longer exists: {shape_path}")
        shape = current_shapes[one_based_index - 1]
        if depth < len(shape_path) - 1:
            # 沿现有 group tree 查找可以保留 shape relationship, 避免先拍平再重建
            # 子 shape 这种破坏性方案.
            if not isinstance(shape, GroupShape):
                raise ValueError(f"shape path crosses a non-group shape: {shape_path}")
            current_shapes = shape.shapes
    return shape
