"""提供不访问网络的确定性 translator, 供格式结构测试使用。"""

from collections.abc import Callable, Sequence

from modules.translation.contracts import (
    TranslationBatchItem,
    TranslationBatchResult,
)


class StubBatchTranslator:
    """为 pipeline 结构测试提供可重复的确定性 translator。"""

    def __init__(self, transform: Callable[[str], str] | None = None) -> None:
        """接收一个纯函数; 未传入时使用稳定的可见前缀。"""

        self._transform = transform or (lambda text: f"[translated] {text}")

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """按输入顺序应用 transform, 并返回完整 index 映射。"""

        del source_language, target_language, format_hint, read_only_context, repair_candidates
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(index=index, text=self._transform(text))
                for index, text in enumerate(texts)
            )
        )
