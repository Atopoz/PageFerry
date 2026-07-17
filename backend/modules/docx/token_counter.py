"""按稳定 token 规则把 DOCX 段落组织成 500-800 token 批次。

分组会改变模型可见的相邻上下文, 从而影响术语、空格和句式。这里直接保留
``cl100k_base`` 计数与既有分组算法, 避免用字符估算产生不同 batch。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Protocol

import tiktoken


class _Encoding(Protocol):
    """声明 token encoder 在本 module 中需要的最小 contract。"""

    def encode(self, text: str) -> list[int] | list[str]:
        """把文本编码为仅用于计数的 token 序列。"""


class _FallbackEncoding:
    """在 ``cl100k_base`` 资源不可用时执行离线 fallback 分词。"""

    _token_pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def encode(self, text: str) -> list[str]:
        """按 word 与非空白符号切分文本。"""

        if not text:
            return []
        return self._token_pattern.findall(text)


def load_token_encoding(encoding_name: str = "cl100k_base") -> _Encoding:
    """加载 tiktoken encoder, 失败时切换到确定性的离线 fallback。"""

    try:
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        # 离线 fallback 是已有 runtime 行为; 不能让 tokenizer 下载失败阻断翻译。
        return _FallbackEncoding()


def count_tokens(text: str, *, encoding: _Encoding | None = None) -> int:
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
    encoding: _Encoding | None = None,
) -> tuple[tuple[T, ...], ...]:
    """按兼容性 contract 执行 DOCX 段落分组。

    当一个不足 ``min_tokens`` 的单段与下一段合并后超过 ``max_tokens`` 时,
    仍会把这两段放进同一组; 这是为了维持现有可观察的 batch 边界。
    """

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
