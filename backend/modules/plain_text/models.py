"""定义纯文本 runtime 的 segment、保护区与解码结果模型。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextSegment:
    """记录一个可翻译文本片段在工作文本中的精确位置。"""

    segment_id: str
    kind: str
    source_text: str
    original_order: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True, slots=True)
class ProtectedSpan:
    """记录 Markdown 中不得交给模型改写的原始内容。"""

    placeholder: str
    content: str
    kind: str


@dataclass(frozen=True, slots=True)
class TextReadResult:
    """保留读取后的正文、编码和原换行风格。"""

    text: str
    encoding: str
    line_ending: str


@dataclass(frozen=True, slots=True)
class PreparedMarkdownDocument:
    """保存占位保护后的 Markdown 与恢复所需的 span。"""

    working_text: str
    protected_spans: tuple[ProtectedSpan, ...]
    context_snippets: tuple[str, ...] = ()
