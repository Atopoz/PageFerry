"""按结构与 token 上限切分 TXT/Markdown 文本。"""

import math
import re
from dataclasses import dataclass
from itertools import pairwise

from modules.plain_text.markdown_table import table_cell_count, table_row_signature
from modules.plain_text.models import TextSegment

_BLANK_SEPARATOR_RE = re.compile(r"((?:\r?\n)[ \t]*(?:\r?\n)+)")
_HEADING_RE = re.compile(r"^(#{1,6}[ \t]+)(.*?)(\r?\n?)$")
_BLOCKQUOTE_RE = re.compile(r"^((?:>[ \t]*)+)(.*?)(\r?\n?)$")
_LIST_ITEM_RE = re.compile(r"^(\s*(?:[-+*]|\d+\.)\s+(?:\[[ xX]\]\s+)?)(.*?)(\r?\n?)$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$")
_PROTECTED_BLOCK_RE = re.compile(r"^%%PROTECTED_[A-Z0-9_]+%%(?:\r?\n)?$")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？; .!?;])(?=\s|$)")  # noqa: RUF001


@dataclass(frozen=True, slots=True)
class _Span:
    """表示工作文本中的半开区间。"""

    start: int
    end: int


def estimate_tokens(text: str) -> int:
    """用 CJK/ASCII 加权估算 token, 避免轻量客户端引入 tokenizer runtime。"""

    weight = sum(1.0 if ord(char) > 127 else 0.25 for char in text)
    return max(1, math.ceil(weight))


class TextSegmenter:
    """生成可逆 offset segment, 并按 800 token 上限组批。"""

    max_batch_tokens = 800
    max_segment_tokens = 700
    max_sentence_chars = 1200

    def txt(self, text: str) -> list[TextSegment]:
        """按空段切分 TXT, 并进一步拆分过长句子。"""

        segments: list[TextSegment] = []
        cursor = 0
        for index, part in enumerate(_BLANK_SEPARATOR_RE.split(text)):
            part_start = cursor
            cursor += len(part)
            if index % 2 or not part.strip():
                continue
            leading = len(part) - len(part.lstrip())
            trailing = len(part) - len(part.rstrip())
            core_start = part_start + leading
            core_end = part_start + len(part) - trailing
            for span in self._split_spans(text[core_start:core_end], core_start):
                self._append(segments, "txt", "paragraph", text, span.start, span.end)
        return segments

    def markdown(self, text: str) -> list[TextSegment]:
        """保留 Markdown 前缀与分隔行, 只切出用户可见文本。"""

        segments: list[TextSegment] = []
        paragraph: list[tuple[int, str]] = []
        cursor = 0
        lines = text.splitlines(keepends=True)
        table_line_indexes = self._table_line_indexes(lines)

        def flush() -> None:
            """把连续普通行提交为一个保持 offset 的段落 segment。"""

            if not paragraph:
                return
            start = paragraph[0][0]
            raw = "".join(value for _, value in paragraph)
            leading = len(raw) - len(raw.lstrip())
            trailing = len(raw) - len(raw.rstrip())
            self._append(
                segments,
                "md",
                "paragraph",
                text,
                start + leading,
                start + len(raw) - trailing,
            )
            paragraph.clear()

        for line_index, line in enumerate(lines):
            start = cursor
            cursor += len(line)
            stripped = line.strip()
            if (
                not stripped
                or _PROTECTED_BLOCK_RE.fullmatch(stripped)
                or _TABLE_SEPARATOR_RE.fullmatch(stripped)
            ):
                flush()
                continue

            if line_index in table_line_indexes:
                flush()
                suffix_length = 2 if line.endswith("\r\n") else 1 if line.endswith("\n") else 0
                self._append(
                    segments,
                    "md",
                    "table_row",
                    text,
                    start,
                    start + len(line) - suffix_length,
                )
                continue

            for pattern, kind in (
                (_HEADING_RE, "heading"),
                (_BLOCKQUOTE_RE, "blockquote"),
                (_LIST_ITEM_RE, "list_item"),
            ):
                match = pattern.match(line)
                if match is None:
                    continue
                flush()
                prefix, content, _ = match.groups()
                content_start = start + len(prefix)
                self._append(
                    segments,
                    "md",
                    kind,
                    text,
                    content_start,
                    content_start + len(content),
                )
                break
            else:
                paragraph.append((start, line))

        flush()
        return segments

    @staticmethod
    def _table_line_indexes(lines: list[str]) -> set[int]:
        """根据 separator 上下文找出 GFM table 的 header 与 data row。"""

        indexes: set[int] = set()
        for separator_index in range(1, len(lines)):
            separator = lines[separator_index].rstrip("\r\n")
            if _TABLE_SEPARATOR_RE.fullmatch(separator) is None:
                continue

            header = lines[separator_index - 1].rstrip("\r\n")
            header_signature = table_row_signature(header)
            separator_signature = table_row_signature(separator)
            if (
                header_signature is None
                or separator_signature is None
                or header_signature[2] == 0
                or table_cell_count(header_signature) != table_cell_count(separator_signature)
            ):
                continue

            indexes.add(separator_index - 1)
            # GFM table body 延续到首个没有未转义 delimiter 的普通行。
            for body_index in range(separator_index + 1, len(lines)):
                body = lines[body_index].rstrip("\r\n")
                body_signature = table_row_signature(body)
                if body_signature is None or body_signature[2] == 0:
                    break
                indexes.add(body_index)
        return indexes

    def batches(self, segments: list[TextSegment]) -> list[list[TextSegment]]:
        """保持原顺序, 将 segment 贪心分组到 token 上限内。"""

        groups: list[list[TextSegment]] = []
        current: list[TextSegment] = []
        tokens = 0
        for segment in segments:
            segment_tokens = estimate_tokens(segment.source_text)
            if current and tokens + segment_tokens > self.max_batch_tokens:
                groups.append(current)
                current = []
                tokens = 0
            current.append(segment)
            tokens += segment_tokens
        if current:
            groups.append(current)
        return groups

    def _split_spans(self, text: str, base_offset: int) -> list[_Span]:
        """优先按句界拆分长文本, 必要时再按字符窗口兜底。"""

        if estimate_tokens(text) <= self.max_segment_tokens:
            return [_Span(base_offset, base_offset + len(text))]
        boundaries = [
            0,
            *[match.start() for match in _SENTENCE_BOUNDARY_RE.finditer(text)],
            len(text),
        ]
        spans: list[_Span] = []
        for start, end in pairwise(boundaries):
            while estimate_tokens(text[start:end]) > self.max_segment_tokens:
                split_at = min(end, start + self.max_sentence_chars)
                while split_at > start and split_at < end and not text[split_at - 1].isspace():
                    split_at -= 1
                if split_at == start:
                    split_at = min(end, start + self.max_sentence_chars)
                spans.append(_Span(base_offset + start, base_offset + split_at))
                start = split_at
            if start < end:
                spans.append(_Span(base_offset + start, base_offset + end))
        return spans

    @staticmethod
    def _append(
        segments: list[TextSegment],
        prefix: str,
        kind: str,
        text: str,
        start: int,
        end: int,
    ) -> None:
        """忽略空范围并追加一个具有稳定 id 的 segment。"""

        if start >= end or not text[start:end].strip():
            return
        order = len(segments)
        segments.append(
            TextSegment(
                segment_id=f"{prefix.upper()}_{order:04d}",
                kind=kind,
                source_text=text[start:end],
                original_order=order,
                start_offset=start,
                end_offset=end,
            )
        )
