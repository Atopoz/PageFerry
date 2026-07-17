"""提供 Markdown table row 识别与结构校验共用的轻量工具。"""


def unescaped_pipe_offsets(text: str) -> tuple[int, ...]:
    """定位未被奇数个反斜杠转义的 pipe, 避免把 cell 正文当 delimiter。"""

    offsets: list[int] = []
    backslashes = 0
    for offset, char in enumerate(text):
        if char == "\\":
            backslashes += 1
            continue
        if char == "|" and backslashes % 2 == 0:
            offsets.append(offset)
        backslashes = 0
    return tuple(offsets)


def table_row_signature(text: str) -> tuple[bool, bool, int] | None:
    """返回 outer pipe 形态与未转义 delimiter 数, 多行结果直接判为无效。"""

    if "\n" in text or "\r" in text:
        return None
    stripped = text.strip(" \t")
    pipe_offsets = unescaped_pipe_offsets(stripped)
    return (
        bool(pipe_offsets) and pipe_offsets[0] == 0,
        bool(pipe_offsets) and pipe_offsets[-1] == len(stripped) - 1,
        len(pipe_offsets),
    )


def table_cell_count(signature: tuple[bool, bool, int]) -> int:
    """按 optional outer pipe 规则把 delimiter signature 换算为 cell 数。"""

    has_leading_pipe, has_trailing_pipe, delimiter_count = signature
    return delimiter_count + 1 - int(has_leading_pipe) - int(has_trailing_pipe)
