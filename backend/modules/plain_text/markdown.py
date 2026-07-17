"""在翻译前保护 Markdown 结构, 并在写回时恢复原始标记。"""

import re

import mistune

from modules.plain_text.models import (
    PreparedMarkdownDocument,
    ProtectedSpan,
    TextSegment,
)

_FRONT_MATTER_RE = re.compile(r"\A---\s*\r?\n.*?\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", re.DOTALL)
_FENCED_CODE_RE = re.compile(r"(?ms)^(```|~~~).*?^\1[^\n\r]*(?:\r?\n|$)")
_INDENTED_CODE_RE = re.compile(r"(?m)(?:^(?: {4}|\t).*(?:\r?\n|$))+")
_HTML_BLOCK_RE = re.compile(r"(?ms)^<[A-Za-z!/][^>]*>.*?(?:\r?\n\r?\n|$)")
_REFERENCE_LINK_RE = re.compile(r"(?m)^\s*\[[^\]]+\]:\s+\S.*$")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_PLACEHOLDER_RE = re.compile(r"%%PROTECTED_[A-Z_]+_\d{4}%%")


class MarkdownProtector:
    """在翻译前替换不可改写 span, 并在翻译后做结构校验。"""

    def __init__(self) -> None:
        """创建只用于结构校验的 Mistune AST parser。"""

        self._parse = mistune.create_markdown(renderer="ast", plugins=["table", "strikethrough"])

    def prepare(self, text: str) -> PreparedMarkdownDocument:
        """将不可改写内容替换成占位符, 并收集最多五条只读语境。"""

        working = text
        spans: list[ProtectedSpan] = []
        context_snippets: list[str] = []

        def protect_match(match: re.Match[str], kind: str) -> str:
            """登记一个受保护 span, 并返回不会与正文冲突的占位符。"""

            placeholder = f"%%PROTECTED_{kind.upper()}_{len(spans):04d}%%"
            content = match.group(0)
            spans.append(ProtectedSpan(placeholder, content, kind))
            if kind in {"front_matter", "code_block"}:
                context_snippets.append(self._compact_context(content))
            return placeholder

        front_matter = _FRONT_MATTER_RE.match(working)
        if front_matter is not None:
            replacement = protect_match(front_matter, "front_matter")
            working = replacement + working[front_matter.end() :]

        for pattern, kind in (
            (_FENCED_CODE_RE, "code_block"),
            (_INDENTED_CODE_RE, "code_block"),
            (_HTML_BLOCK_RE, "html_block"),
            (_REFERENCE_LINK_RE, "reference_link"),
            (_INLINE_CODE_RE, "inline_code"),
        ):
            working = pattern.sub(lambda match, kind=kind: protect_match(match, kind), working)

        working = self._protect_link_destinations(working, spans)
        self._parse(working)
        return PreparedMarkdownDocument(
            working,
            tuple(spans),
            tuple(context_snippets[:5]),
        )

    def restore(
        self,
        *,
        prepared: PreparedMarkdownDocument,
        segments: list[TextSegment],
        translations: dict[str, str],
    ) -> str:
        """逆序回填译文、恢复 protected span, 并重新解析 Markdown。"""

        # 占位符必须恰好出现一次; 少一个或多一个都意味着模型破坏了结构。
        working = _replace_segments(prepared.working_text, segments, translations)
        for span in prepared.protected_spans:
            if working.count(span.placeholder) != 1:
                raise ValueError("markdown_placeholder_mismatch")
            working = working.replace(span.placeholder, span.content)

        if _PLACEHOLDER_RE.search(working):
            raise ValueError("markdown_placeholder_leaked")
        if working.count("```") % 2 or working.count("~~~") % 2:
            raise ValueError("markdown_fence_mismatch")
        self._parse(working)
        return working

    @staticmethod
    def placeholders(text: str) -> tuple[str, ...]:
        """提取 segment 内所有受保护占位符, 供模型结果对比。"""

        return tuple(_PLACEHOLDER_RE.findall(text))

    @staticmethod
    def _protect_link_destinations(text: str, spans: list[ProtectedSpan]) -> str:
        """保护括号配平的 Markdown link destination, 包括嵌套括号。"""
        replacements: list[tuple[int, int, str]] = []
        cursor = 0
        while cursor < len(text) - 1:
            start = text.find("](", cursor)
            if start < 0:
                break
            destination_start = start + 2
            depth = 1
            index = destination_start
            escaped = False
            while index < len(text):
                char = text[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        destination = text[destination_start:index]
                        placeholder = f"%%PROTECTED_LINK_DEST_{len(spans):04d}%%"
                        spans.append(ProtectedSpan(placeholder, destination, "link_destination"))
                        replacements.append((destination_start, index, placeholder))
                        cursor = index + 1
                        break
                index += 1
            else:
                break

        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        return text

    @staticmethod
    def _compact_context(text: str) -> str:
        """压平受保护内容, 限制长度以免只读语境挤占 batch token。"""

        return " ".join(text.strip().split())[:180]


def _replace_segments(
    text: str,
    segments: list[TextSegment],
    translations: dict[str, str],
) -> str:
    """按 offset 从后向前替换, 避免较早替换改变后续 segment 坐标。"""

    working = text
    for segment in sorted(segments, key=lambda item: item.start_offset, reverse=True):
        translated = translations.get(segment.segment_id, segment.source_text)
        working = working[: segment.start_offset] + translated + working[segment.end_offset :]
    return working
