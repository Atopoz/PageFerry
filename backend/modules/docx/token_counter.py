"""兼容旧 DOCX import 路径, 实际 token 分组由共享 translation module 维护。"""

from modules.translation.token_groups import (
    FallbackEncoding,
    TokenEncoding,
    count_tokens,
    group_by_token_limit,
    load_token_encoding,
)

_Encoding = TokenEncoding
_FallbackEncoding = FallbackEncoding

__all__ = ["count_tokens", "group_by_token_limit", "load_token_encoding"]
