"""校验并修复 DOCX 的轻量 markup, 且只恢复能够确定的 run 骨架。"""

import re
import unicodedata
from collections.abc import Sequence
from html import escape

_TAG_RE = re.compile(r"</?(?:span|p)>")
_SPAN_RE = re.compile(r"<span>(.*?)</span>", re.DOTALL)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_MARKER_RE = re.compile(r"^(?:\[[A-Z0-9_]+\])+")
_SPACE_MARKER = "<SPACE>"


def candidate_content(
    candidate: str | None,
    marker: str,
    *,
    require_marker: bool,
) -> str | None:
    """从模型结果中移除且仅移除预期位置 marker。"""

    if candidate is None:
        return None
    value = candidate.strip("\r\n")
    prefix = f"[{marker}]"
    if value.startswith(prefix):
        return value[len(prefix) :]
    if require_marker:
        return None
    # 修复阶段允许 marker 已丢失, 但不能吞掉正文中的方括号内容。
    return _MARKER_RE.sub("", value, count=1)


def marked_span(text: str) -> str:
    """把用户文本 escape 后包进唯一可识别的 span 骨架。"""

    # 先 escape 源文本, 字面 </span>、<p> 或 & 才不会被后续 regex 误认成结构。
    return f"<span>{escape(text, quote=False)}</span>"


def validate_marked_output(candidate: str, source: str) -> bool:
    """确认 span、段落骨架和受保护空白槽均未被模型改变。"""

    source_tags = _TAG_RE.findall(source)
    candidate_tags = _TAG_RE.findall(candidate)
    if source_tags != candidate_tags:
        return False

    source_spans = _SPAN_RE.findall(source)
    candidate_spans = _SPAN_RE.findall(candidate)
    if len(source_spans) != len(candidate_spans):
        return False

    if _outside_markup(source).strip() or _outside_markup(candidate).strip():
        return False

    for source_text, candidate_text in zip(source_spans, candidate_spans, strict=True):
        source_is_blank = not source_text or source_text.isspace()
        candidate_is_blank = not candidate_text or candidate_text.isspace()
        if source_is_blank and candidate_text != source_text:
            return False
        # 结构完整不等于内容安全。模型曾在真实合同中保留两个 span, 却把
        # 两段银行地址都改成单个空格; 若只校验 tag 数量会静默清空正文。
        if not source_is_blank and candidate_is_blank:
            return False
    return True


def deterministic_repair(candidate: str, source: str) -> str | None:
    """按源 span/p 骨架恢复 provider 已返回的可见译文。

    对 nested、缺失或多余的 span, 先提取已有文字, 再按源 slot 顺序对齐。
    候选 slot 不足时把完整译文留在前面的 slot, 后续 slot
    置空; 这会牺牲少量 run 级格式分布, 但不会像整段 fallback 那样留下原文。
    """

    source_spans = _SPAN_RE.findall(source)
    if not source_spans:
        return None

    candidate_chunks = tuple(
        chunk
        for chunk in _extract_resilient_span_chunks(candidate)
        if chunk.strip() and chunk != _SPACE_MARKER
    )
    non_blank_count = sum(bool(span.strip()) for span in source_spans)
    aligned_chunks = _align_non_blank_chunks(candidate_chunks, non_blank_count)
    if aligned_chunks is None or any("<" in chunk or ">" in chunk for chunk in aligned_chunks):
        return None

    repaired_parts: list[str] = []
    candidate_index = 0
    for source_text in source_spans:
        if not source_text or source_text.isspace():
            repaired_parts.append(source_text)
            continue
        repaired_parts.append(aligned_chunks[candidate_index])
        candidate_index += 1

    repaired = _replace_span_contents(source, repaired_parts)
    return repaired if _validate_repaired_output(repaired, source) else None


def span_contents(marked_text: str) -> tuple[str, ...]:
    """按顺序返回标记文本中完成 unescape 的 span 内容。"""

    return tuple(_unescape_markup_text(value) for value in _SPAN_RE.findall(marked_text))


def is_english_language(language: str | None) -> bool:
    """判断目标语种是否属于 English locale。"""

    normalized = (language or "").strip().lower().replace("_", "-")
    return normalized == "en" or normalized.startswith("en-")


def normalize_english_span_spacing(
    marked_text: str,
    *,
    blocked_boundaries: frozenset[int] = frozenset(),
) -> str:
    """在相邻 span 的英文单词边界补一个空格。

    LLM 会逐 span 返回译文, 但两个源 run 的边界不一定自带英文空格。空格
    必须写进其中一个既有 span, 不能插到标签外, 否则会破坏 formatter 的
    一一回填 contract。
    ``blocked_boundaries`` 使用一开始的 span 数量作为累计位置, 防止表格行在
    两个 cell 之间补出尾随空格, 因此必须显式保护 cell 边界。
    """

    matches = list(_SPAN_RE.finditer(marked_text))
    if len(matches) < 2:
        return marked_text

    contents = [match.group(1) for match in matches]
    for index in range(len(contents) - 1):
        if index + 1 in blocked_boundaries:
            continue
        left = contents[index]
        right = contents[index + 1]
        between = marked_text[matches[index].end() : matches[index + 1].start()]
        # ``</p><p>`` 是段落边界, 不允许跨段补空格。
        if between and ("<" in between or ">" in between):
            continue
        if between.strip() or not _should_insert_english_space(left, right):
            continue
        if _ends_with_space(left) or _starts_with_space(right):
            continue
        if left.strip():
            contents[index] = f"{left} "
        else:
            contents[index + 1] = f" {right}"

    result: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        result.append(marked_text[cursor : match.start()])
        result.append(f"<span>{contents[index]}</span>")
        cursor = match.end()
    result.append(marked_text[cursor:])
    return "".join(result)


def paragraph_contents(marked_text: str) -> tuple[str, ...]:
    """返回表格单元格中的段落内容; 单段落时返回原值。"""

    paragraphs = tuple(re.findall(r"<p>(.*?)</p>", marked_text, re.DOTALL))
    return paragraphs or (marked_text,)


def should_skip_translation(text: str) -> bool:
    """判断文本是否只包含空白、数字、标点或符号。"""

    if not text or not text.strip():
        return True
    return all(char.isspace() or unicodedata.category(char)[:1] in {"N", "P", "S"} for char in text)


def _should_insert_english_space(left: str, right: str) -> bool:
    """判断两个 span 的可见边界是否需要英文词间空格。"""

    left_clean = left.replace(_SPACE_MARKER, " ").rstrip()
    right_clean = right.replace(_SPACE_MARKER, " ").lstrip()
    if not left_clean or not right_clean:
        return False

    left_type = _boundary_char_type(left_clean[-1])
    right_char = right_clean[0]
    right_type = _boundary_char_type(right_char)
    if right_char in ",.;:!?)]}%/\\-":
        return False
    if "other" in {left_type, right_type}:
        return False
    return not (left_type == right_type and left_type in {"cjk", "digit"})


def _boundary_char_type(char: str) -> str:
    """把边界字符归类为 Latin、CJK、数字或其他字符。"""

    if char.isascii() and char.isalpha():
        return "latin"
    if char.isascii() and char.isdigit():
        return "digit"
    if _CJK_RE.fullmatch(char):
        return "cjk"
    return "other"


def _ends_with_space(text: str) -> bool:
    """判断 span 末尾是否已有空白或显式 SPACE marker。"""

    return bool(re.search(r"(?:\s|<SPACE>)+$", text))


def _starts_with_space(text: str) -> bool:
    """判断 span 开头是否已有空白或显式 SPACE marker。"""

    return bool(re.match(r"(?:\s|<SPACE>)+", text))


def _outside_markup(marked_text: str) -> str:
    """移除合法 span 与 p 标签, 暴露任何游离文本。"""

    without_spans = _SPAN_RE.sub("", marked_text)
    return re.sub(r"</?p>", "", without_spans)


def _replace_span_contents(source: str, contents: Sequence[str]) -> str:
    """把候选内容依次放回源 span 骨架。"""

    iterator = iter(contents)
    return _SPAN_RE.sub(lambda _match: f"<span>{next(iterator)}</span>", source)


def _extract_resilient_span_chunks(text: str) -> tuple[str, ...]:
    """在 span/p 边界切分不可靠候选, 尽量保留已有可见文字。"""

    if not text:
        return ()
    chunks: list[str] = []
    buffer: list[str] = []
    cursor = 0
    saw_tag = False
    boundary_pattern = re.compile(r"</?(?:span|p)(?:\s[^>]*)?>", re.IGNORECASE)

    def flush_buffer() -> None:
        """移除残留已知 tag, 并提交当前非空文字块。"""

        if not buffer:
            return
        value = "".join(buffer)
        buffer.clear()
        cleaned = boundary_pattern.sub("", value)
        if cleaned == _SPACE_MARKER or cleaned.strip():
            chunks.append(cleaned)

    while cursor < len(text):
        boundary = boundary_pattern.match(text, cursor)
        if boundary is not None:
            saw_tag = True
            flush_buffer()
            cursor = boundary.end()
            continue
        buffer.append(text[cursor])
        cursor += 1
    flush_buffer()

    if saw_tag:
        return tuple(chunks)
    plain = boundary_pattern.sub("", text)
    return (plain,) if plain.strip() else ()


def _align_non_blank_chunks(
    chunks: tuple[str, ...],
    expected_count: int,
) -> tuple[str, ...] | None:
    """把已有候选文字对齐到源非空 slot 数量, 不虚构新的译文。"""

    if expected_count < 1 or not chunks:
        return None
    if len(chunks) < expected_count:
        return (*chunks, *("" for _index in range(expected_count - len(chunks))))
    if len(chunks) == expected_count:
        return chunks
    if expected_count == 1:
        return ("".join(chunks),)
    return (*chunks[: expected_count - 1], "".join(chunks[expected_count - 1 :]))


def _validate_repaired_output(candidate: str, source: str) -> bool:
    """验证 deterministic repair 的骨架与总可见内容, 允许尾部空 slot。"""

    if _TAG_RE.findall(candidate) != _TAG_RE.findall(source):
        return False
    source_spans = _SPAN_RE.findall(source)
    candidate_spans = _SPAN_RE.findall(candidate)
    if len(source_spans) != len(candidate_spans):
        return False
    if _outside_markup(candidate).strip():
        return False

    repaired_visible: list[str] = []
    for source_text, candidate_text in zip(source_spans, candidate_spans, strict=True):
        if not source_text or source_text.isspace():
            if candidate_text != source_text:
                return False
            continue
        repaired_visible.append(candidate_text)
    return bool("".join(repaired_visible).strip())


def _unescape_markup_text(value: str) -> str:
    """只还原本模块写入的三种 entity, 保留模型生成的其他字面 entity。"""

    # &amp; 必须最后处理, 否则源文本中的字面 &lt; 会被多解码成字符 <。
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
