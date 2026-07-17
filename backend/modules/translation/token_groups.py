"""按稳定 token 规则把文档 segment 组织成 500-800 token 批次。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

import tiktoken


class TokenEncoding(Protocol):
    """声明 token encoder 在分组逻辑中需要的最小 contract。"""

    def encode(self, text: str) -> list[int] | list[str]:
        """把文本编码为仅用于计数的 token 序列。"""
        ...


class FallbackEncoding:
    """在 ``cl100k_base`` 资源不可用时执行离线 fallback 分词。"""

    _token_pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def encode(self, text: str) -> list[str]:
        """按 word 与非空白符号切分文本。"""

        if not text:
            return []
        return self._token_pattern.findall(text)


def load_token_encoding(encoding_name: str = "cl100k_base") -> TokenEncoding:
    """加载 tiktoken encoder, 失败时切换到确定性的离线 fallback。"""

    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return FallbackEncoding()


def count_tokens(text: str, *, encoding: TokenEncoding | None = None) -> int:
    """用 ``cl100k_base`` 计算一段标记文本的 token 数。"""

    if not text:
        return 0
    active_encoding = encoding or load_token_encoding()
    return len(active_encoding.encode(text))


def group_by_token_limit[T](
    items: Sequence[T],
    text_of: Callable[[T], str],
    *,
    min_tokens: int = 500,
    max_tokens: int = 800,
    encoding: TokenEncoding | None = None,
) -> tuple[tuple[T, ...], ...]:
    """按兼容性 contract 把输入分成有稳定边界的小组。"""

    if min_tokens <= 0 or max_tokens < min_tokens:
        raise ValueError("invalid_token_limits")
    if not items:
        return ()

    active_encoding = encoding or load_token_encoding()
    item_tokens = [count_tokens(text_of(item), encoding=active_encoding) for item in items]
    groups: list[tuple[T, ...]] = []
    current_group: list[T] = []
    current_tokens = 0

    for item, token_count in zip(items, item_tokens, strict=True):
        if token_count > max_tokens:
            if current_group:
                groups.append(tuple(current_group))
                current_group = []
                current_tokens = 0
            groups.append((item,))
            continue

        if current_tokens + token_count > max_tokens and current_group:
            if current_tokens < min_tokens and len(current_group) == 1:
                current_group.append(item)
                groups.append(tuple(current_group))
                current_group = []
                current_tokens = 0
            else:
                groups.append(tuple(current_group))
                current_group = [item]
                current_tokens = token_count
        else:
            current_group.append(item)
            current_tokens += token_count

    if current_group:
        groups.append(tuple(current_group))
    return tuple(groups)
