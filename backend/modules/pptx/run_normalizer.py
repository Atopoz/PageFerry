"""归一化无害的 PPTX run 切分, 同时保留可见文字效果。"""

from typing import Any, ClassVar

from pptx.oxml.ns import qn

from .entities import PptxRun


class PptxRunNormalizer:
    """合并相邻且视觉等价的 run, 同时保护复杂文字效果."""

    _COMPLEX_EFFECT_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "effectLst",
            "effectDag",
            "glow",
            "outerShdw",
            "innerShdw",
            "reflection",
            "blur",
            "softEdge",
            "prstTxWarp",
            "ln",
            "gradFill",
            "pattFill",
            "patternFill",
        }
    )
    _HYPERLINK_TAGS: ClassVar[frozenset[str]] = frozenset({"hlinkClick", "hlinkMouseOver"})
    _FONT_SLOT_TAGS: ClassVar[tuple[str, ...]] = ("latin", "ea", "cs")

    def merge_runs(self, runs: list[Any]) -> tuple[PptxRun, ...]:
        """只合并相邻且格式在视觉上等价的 run."""

        merged: list[dict[str, Any]] = []
        for run_index, run in enumerate(runs):
            text = run.text
            if text is None or text == "":
                continue
            format_info = self._extract_visual_format(run)
            can_merge = not self._has_complex_effects(run)
            if (
                merged
                and can_merge
                and merged[-1]["can_merge"]
                and merged[-1]["format_info"] == format_info
                and self._are_adjacent_xml_runs(merged[-1]["last_run"], run)
            ):
                merged[-1]["text"] += text
                merged[-1]["indices"].append(run_index)
                merged[-1]["last_run"] = run
                continue
            merged.append(
                {
                    "text": text,
                    "format_info": format_info,
                    "run_index": run_index,
                    "indices": [run_index],
                    "can_merge": can_merge,
                    "last_run": run,
                }
            )
        return tuple(
            PptxRun(
                text=item["text"],
                format_info=item["format_info"],
                run_index=item["run_index"],
                source_run_indices=tuple(item["indices"]),
            )
            for item in merged
        )

    def _extract_visual_format(self, run: Any) -> dict[str, Any]:
        """提取会影响 run 渲染结果的格式字段."""

        font = run.font
        return {
            "bold": bool(font.bold) if font.bold is not None else False,
            "italic": bool(font.italic) if font.italic is not None else False,
            "underline": self._normalize_underline(font.underline),
            "font_name": font.name,
            "font_size": font.size.pt if font.size else None,
            "color": self._extract_color(getattr(font, "color", None)),
            "strike": self._safe_bool(font, "strike"),
            "all_caps": self._safe_bool(font, "all_caps"),
            "small_caps": self._safe_bool(font, "small_caps"),
            "shadow": self._safe_bool(font, "shadow"),
            "outline": self._safe_bool(font, "outline"),
            # Font.name 只代表 a:latin。a:ea/a:cs 以及 charset、panose 等
            # DrawingML 属性不同的 run 不能共享写回 span。
            "font_slots": self._extract_font_slot_semantics(run),
            # hyperlink 不只是视觉样式。即使字体完全相同, 不同的 rId、action、
            # tooltip 或 mouse-over 行为也必须形成独立回写边界。
            "hyperlink_semantics": self._extract_hyperlink_semantics(run),
        }

    @classmethod
    def _extract_font_slot_semantics(
        cls,
        run: Any,
    ) -> tuple[tuple[str, tuple[Any, ...] | None], ...]:
        """返回 ``a:latin``、``a:ea`` 与 ``a:cs`` 的完整语义签名。"""

        run_properties = getattr(getattr(run, "_r", None), "rPr", None)
        signatures: list[tuple[str, tuple[Any, ...] | None]] = []
        for slot in cls._FONT_SLOT_TAGS:
            element = run_properties.find(qn(f"a:{slot}")) if run_properties is not None else None
            signatures.append(
                (
                    slot,
                    cls._xml_semantic_signature(element) if element is not None else None,
                )
            )
        return tuple(signatures)

    @classmethod
    def _extract_hyperlink_semantics(cls, run: Any) -> tuple[tuple[Any, ...], ...]:
        """返回 hyperlink XML 的稳定语义签名, 供 merge key 比较。"""

        run_properties = getattr(getattr(run, "_r", None), "rPr", None)
        if run_properties is None:
            return ()
        return tuple(
            cls._xml_semantic_signature(child)
            for child in list(run_properties)
            if cls._strip_namespace(child.tag) in cls._HYPERLINK_TAGS
        )

    @classmethod
    def _xml_semantic_signature(cls, element: Any) -> tuple[Any, ...]:
        """递归记录 OOXML 节点的 tag、属性、文字和子节点。"""

        return (
            cls._strip_namespace(element.tag),
            tuple(sorted((name, value) for name, value in element.attrib.items())),
            element.text or "",
            tuple(cls._xml_semantic_signature(child) for child in list(element)),
        )

    @staticmethod
    def _normalize_underline(value: Any) -> str | bool:
        """把 python-pptx 的 underline enum 转换为可比较值."""

        if value is None:
            return False
        return value if isinstance(value, bool) else str(value)

    @staticmethod
    def _safe_bool(obj: Any, name: str) -> bool:
        """安全读取可选的布尔字体属性."""

        value = getattr(obj, name, None)
        return bool(value) if value is not None else False

    @staticmethod
    def _extract_color(color: Any) -> str | None:
        """返回稳定的颜色描述, 用于判断视觉等价性."""

        if color is None:
            return None
        try:
            if color.rgb:
                return f"RGB({color.rgb})"
            if color.theme_color:
                return f"THEME({color.theme_color})"
            return str(color.type) if color.type is not None else None
        except (AttributeError, TypeError, ValueError):
            return None

    def _has_complex_effects(self, run: Any) -> bool:
        """识别带复杂效果的 run, 避免合并后破坏 WordArt 样式."""

        run_properties = getattr(getattr(run, "_r", None), "rPr", None)
        if run_properties is None:
            return False
        return any(
            self._strip_namespace(child.tag) in self._COMPLEX_EFFECT_TAGS
            for child in list(run_properties)
        )

    @staticmethod
    def _are_adjacent_xml_runs(previous_run: Any, current_run: Any) -> bool:
        """仅当两个 run 在段落 XML 中真实相邻时允许合并。"""

        previous_element = getattr(previous_run, "_r", None)
        current_element = getattr(current_run, "_r", None)
        if previous_element is None or current_element is None:
            return False
        if previous_element.getparent() is not current_element.getparent():
            return False
        # paragraph.runs 只暴露 a:r, 会跳过 a:br、a:fld 等节点。直接检查 XML
        # sibling 才能保证不会把换行或动态字段两侧的文字错误折叠到同一个 span。
        return previous_element.getnext() is current_element

    @staticmethod
    def _strip_namespace(tag: str) -> str:
        """返回 OOXML 元素不含 namespace 的本地 tag 名称."""

        return tag.split("}", 1)[-1]
