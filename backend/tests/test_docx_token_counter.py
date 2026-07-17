"""验证 DOCX batch 的 token 分组 contract。"""

from modules.docx.token_counter import _FallbackEncoding, count_tokens, group_by_token_limit


class FixedEncoding:
    """按输入长度返回稳定 token 数, 便于覆盖分组边界。"""

    def encode(self, text: str) -> list[int]:
        """把每个字符视为一个 token。"""

        return list(range(len(text)))


def test_grouping_matches_joto_overflow_boundary() -> None:
    """不足 min_tokens 的单段允许与下一段组成一个超过 max 的 group。"""

    items = ("a" * 400, "b" * 500, "c" * 100)

    assert group_by_token_limit(items, lambda item: item, encoding=FixedEncoding()) == (
        (items[0], items[1]),
        (items[2],),
    )


def test_oversized_item_is_kept_as_its_own_group() -> None:
    """单个超长段落不拆分, 避免 marker 与 run 映射失效。"""

    items = ("a" * 100, "b" * 900, "c" * 100)

    assert group_by_token_limit(items, lambda item: item, encoding=FixedEncoding()) == (
        (items[0],),
        (items[1],),
        (items[2],),
    )


def test_offline_fallback_keeps_joto_word_symbol_counting() -> None:
    """离线 fallback 按 word 和标点计数, 保持稳定退化行为。"""

    assert count_tokens("Hello, world!", encoding=_FallbackEncoding()) == 4
