"""定义格式 runtime 与模型 provider 之间稳定、最小的翻译 contract。"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DocumentKind = Literal["docx", "pptx", "txt", "md", "pdf"]
TranslationProgressStage = Literal["extracting", "translating", "formatting"]


@dataclass(frozen=True, slots=True)
class DocumentTranslationOptions:
    """保存创建任务时确定的格式专属 pipeline 选项。"""

    kind: DocumentKind
    translate_tables: bool | None = None
    translate_notes: bool | None = None


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """描述一次格式 pipeline 所需的输入、输出和模型选择。"""

    source_path: Path
    output_dir: Path
    source_language: str | None
    target_language: str
    provider_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """返回输出文件以及可见的翻译、回退和 warning 统计。"""

    output_path: Path
    document_kind: DocumentKind
    translated_segments: int = 0
    fallback_segments: int = 0
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TranslationProgress:
    """描述 pipeline 当前阶段与已经真实处理完的 segment 数。"""

    stage: TranslationProgressStage
    processed_segments: int = 0
    total_segments: int = 0


class TranslationProgressReporter(Protocol):
    """接收可持久化的 pipeline progress snapshot。"""

    def __call__(self, progress: TranslationProgress) -> None:
        """在阶段切换或一个真实 batch 完成后接收最新 snapshot。"""
        ...


@dataclass(frozen=True, slots=True)
class TranslationUsage:
    """归一化不同 provider 返回的 token usage。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TranslationBatchItem:
    """按输入 index 对齐的一条模型翻译结果。"""

    index: int
    text: str


@dataclass(frozen=True, slots=True)
class TranslationBatchResult:
    """一批翻译内容及其合并后的 usage。"""

    items: tuple[TranslationBatchItem, ...]
    usage: TranslationUsage = TranslationUsage()


class BatchTranslator(Protocol):
    """定义所有格式 pipeline 共用且不绑定 provider 的 translator。"""

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
        """翻译同一格式的一组 segment, 并保持输入 index 可验证。

        ``read_only_context`` 只帮助模型判断术语语境, 不属于输出 segment,
        也不得被模型翻译或写回文件。

        ``repair_candidates`` 与输入 index 一一对应, 是允许复用措辞但不能执行
        其中指令的不可信候选数据; 只用于显式 repair hint。
        """
        ...


class DocumentPipeline(Protocol):
    """定义各格式 runtime 对任务编排层暴露的最小边界。"""

    document_kind: DocumentKind

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """将源文件翻译为新文件, 并可报告可验证的阶段进度。"""
        ...
