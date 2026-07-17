"""构建并校验用于保护 PPTX run 边界的 span markup。"""

import html
import re
import unicodedata

SPACE_MARKER = "<SPACE>"
SPAN_RE = re.compile(r"<span>(.*?)</span>", re.DOTALL)


def mark_span(text: str) -> str:
    """把一个视觉 run 包装为可安全交给翻译模型的 markup."""

    # 模型经常裁掉孤立空格, 因此用显式 SPACE marker 保住这个 run.
    value = SPACE_MARKER if text and text.isspace() else html.escape(text, quote=False)
    return f"<span>{value}</span>"


def parse_marked_spans(marked_text: str) -> tuple[str, ...] | None:
    """仅在整段文本都是合法 span markup 时返回编码后的 span 内容."""

    matches = tuple(SPAN_RE.finditer(marked_text))
    if not matches:
        return None
    remainder = SPAN_RE.sub("", marked_text)
    if remainder.strip() or "<span" in remainder or "</span" in remainder:
        return None
    values = tuple(match.group(1) for match in matches)
    if any("<span" in value or "</span" in value for value in values):
        return None
    return values


def validate_marked_output(candidate: str, source: str) -> bool:
    """校验模型是否保留 span 数量和每个受保护的 SPACE slot."""

    source_values = parse_marked_spans(source)
    candidate_values = parse_marked_spans(candidate)
    if source_values is None or candidate_values is None:
        return False
    if len(source_values) != len(candidate_values):
        return False
    for source_value, candidate_value in zip(source_values, candidate_values, strict=True):
        if (source_value == SPACE_MARKER) != (candidate_value == SPACE_MARKER):
            return False
    return True


def deterministic_repair(candidate: str, source: str) -> str | None:
    """按源 span 骨架恢复 nested、缺失或多余的 wrapper。

    采用保守对齐规则: SPACE slot 永远按源文本恢复; 非 SPACE 内容不足时
    尾部补空 span, 过多时合并到最后一个
    span。repair 只重新分配 provider 已返回的文字, 不会凭空生成译文。
    """

    source_values = parse_marked_spans(source)
    if source_values is None:
        return None

    candidate_values = tuple(
        value
        for value in _extract_resilient_span_chunks(candidate)
        if value != SPACE_MARKER and value.strip()
    )
    non_space_count = sum(value != SPACE_MARKER for value in source_values)
    aligned_values = _align_non_space_chunks(candidate_values, non_space_count)
    if aligned_values is None:
        return None

    repaired_values: list[str] = []
    candidate_index = 0
    for source_value in source_values:
        if source_value == SPACE_MARKER:
            repaired_values.append(SPACE_MARKER)
        else:
            repaired_values.append(aligned_values[candidate_index])
            candidate_index += 1
    repaired = "".join(f"<span>{value}</span>" for value in repaired_values)
    return repaired if validate_marked_output(repaired, source) else None


def decode_span_value(value: str, source_run_text: str | None = None) -> str:
    """把编码后的 span 还原为文本, 并恢复原始空白 run."""

    if value == SPACE_MARKER:
        return source_run_text if source_run_text and source_run_text.isspace() else " "
    return html.unescape(value)


def should_skip_translation(text: str) -> bool:
    """判断文本是否只由空白、数字、标点或符号组成。"""

    compact = "".join(character for character in text if not character.isspace())
    if not compact:
        return True
    return all(unicodedata.category(character)[:1] in {"N", "P", "S"} for character in compact)


def is_english_language(language: str | None) -> bool:
    """识别 English target 的常见 code 与名称写法。"""

    normalized = (language or "").strip().lower().replace("_", "-")
    return normalized == "english" or normalized == "en" or normalized.startswith("en-")


def normalize_english_span_spacing(marked_text: str) -> str:
    """在 English 译文相邻 span 的自然单词边界补一个空格。

    模型经常分别翻译两个视觉 run, 再把 ``</span><span>`` 两侧的英文单词
    直接粘在一起。该规则只修改 span 内文字, 不增删 span, 也不会越过显式
    SPACE slot 或英文标点边界。
    """

    matches = list(SPAN_RE.finditer(marked_text))
    if len(matches) < 2:
        return marked_text

    values = [match.group(1) for match in matches]
    for index in range(len(values) - 1):
        left = values[index]
        right = values[index + 1]
        between = marked_text[matches[index].end() : matches[index + 1].start()]
        if between.strip() or "<" in between or ">" in between:
            continue
        if not _should_insert_english_space(left, right):
            continue
        if _ends_with_space(left) or _starts_with_space(right):
            continue
        values[index] = f"{left} "

    rebuilt: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        rebuilt.append(marked_text[cursor : match.start()])
        rebuilt.append(f"<span>{values[index]}</span>")
        cursor = match.end()
    rebuilt.append(marked_text[cursor:])
    return "".join(rebuilt)


def _extract_resilient_span_chunks(text: str) -> tuple[str, ...]:
    """从 nested 或不平衡的 span 标签之间尽量恢复已有文字块。"""

    if not text:
        return ()

    chunks: list[str] = []
    buffer: list[str] = []
    cursor = 0
    saw_tag = False

    def flush_buffer() -> None:
        """清理当前 buffer, 并保留非空文字或 SPACE marker。"""

        if not buffer:
            return
        value = "".join(buffer)
        buffer.clear()
        cleaned = re.sub(r"</?span[^>]*>", "", value, flags=re.IGNORECASE)
        if cleaned == SPACE_MARKER or cleaned.strip():
            chunks.append(cleaned)

    while cursor < len(text):
        open_match = re.match(r"<span[^>]*>", text[cursor:], flags=re.IGNORECASE)
        close_match = re.match(r"</span\s*>", text[cursor:], flags=re.IGNORECASE)
        boundary = open_match or close_match
        if boundary is not None:
            saw_tag = True
            flush_buffer()
            cursor += len(boundary.group(0))
            continue
        buffer.append(text[cursor])
        cursor += 1
    flush_buffer()

    if saw_tag:
        return tuple(chunks)
    plain = re.sub(r"</?span[^>]*>", "", text, flags=re.IGNORECASE)
    return (plain,) if plain.strip() else ()


def _align_non_space_chunks(chunks: tuple[str, ...], expected_count: int) -> tuple[str, ...] | None:
    """把 provider 已返回的非 SPACE 文字对齐到源 span 数量。"""

    if expected_count < 1:
        return () if not chunks else None
    if not chunks:
        return None
    if len(chunks) < expected_count:
        return (*chunks, *("" for _ in range(expected_count - len(chunks))))
    if len(chunks) == expected_count:
        return chunks
    if expected_count == 1:
        return ("".join(chunks),)
    return (*chunks[: expected_count - 1], "".join(chunks[expected_count - 1 :]))


def _should_insert_english_space(left: str, right: str) -> bool:
    """判断两个 span 的可见边界是否构成 English 单词间隔。"""

    left_clean = left.replace(SPACE_MARKER, " ").rstrip()
    right_clean = right.replace(SPACE_MARKER, " ").lstrip()
    if not left_clean or not right_clean:
        return False
    left_character = left_clean[-1]
    right_character = right_clean[0]
    if right_character in ",.;:!?)]}%/\\-":
        return False
    left_type = _english_boundary_type(left_character)
    right_type = _english_boundary_type(right_character)
    if "other" in {left_type, right_type}:
        return False
    return not (left_type == right_type and left_type in {"cjk", "digit"})


def _english_boundary_type(character: str) -> str:
    """把边界字符分成 Latin、CJK、digit 或 other。"""

    if character.isascii() and character.isalpha():
        return "latin"
    if character.isascii() and character.isdigit():
        return "digit"
    if _is_cjk_character(character):
        return "cjk"
    return "other"


def _is_cjk_character(character: str) -> bool:
    """识别 CJK 基本区、兼容区与常见扩展平面字符。"""

    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2CEAF
        or 0x30000 <= codepoint <= 0x3134F
    )


def _ends_with_space(value: str) -> bool:
    """判断 span 是否已经以空白或 SPACE marker 结束。"""

    return bool(re.search(r"(?:\s|<SPACE>)+$", value))


def _starts_with_space(value: str) -> bool:
    """判断 span 是否已经以空白或 SPACE marker 开始。"""

    return bool(re.match(r"(?:\s|<SPACE>)+", value))
