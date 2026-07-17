"""把 PageFerry 共用 token encoder 适配成 PDF formatter 的索引分组接口。"""

from modules.translation.token_groups import TokenEncoding, count_tokens, load_token_encoding


class PdfTokenCounter:
    """按 token 上限返回 formatter 所需的原始文本 index group。"""

    def __init__(self, encoding: TokenEncoding | None = None) -> None:
        """复用一个离线可 fallback 的 encoder, 避免每个 chunk 重复加载。"""

        self._encoding = encoding or load_token_encoding()

    def count_tokens(self, text: str) -> int:
        """计算 marker 或正文的 token 数。"""

        return count_tokens(text, encoding=self._encoding)

    def group_texts_by_token_limit(
        self,
        texts: list[str],
        min_tokens: int,
        max_tokens: int,
    ) -> list[list[int]]:
        """保持 PDF formatter 的分组边界与 index 返回格式。"""

        if min_tokens < 0 or max_tokens <= 0 or min_tokens > max_tokens:
            raise ValueError("invalid_token_limits")
        groups: list[list[int]] = []
        current: list[int] = []
        current_tokens = 0
        for index, text in enumerate(texts):
            tokens = self.count_tokens(text)
            if tokens > max_tokens:
                if current:
                    groups.append(current)
                    current = []
                    current_tokens = 0
                groups.append([index])
                continue
            if current and current_tokens + tokens > max_tokens:
                if current_tokens < min_tokens and len(current) == 1:
                    current.append(index)
                    groups.append(current)
                    current = []
                    current_tokens = 0
                else:
                    groups.append(current)
                    current = [index]
                    current_tokens = tokens
                continue
            current.append(index)
            current_tokens += tokens
        if current:
            groups.append(current)
        return groups


token_counter = PdfTokenCounter()
