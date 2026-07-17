"""编排 PPTX 提取、结构化翻译、fallback、package 校验和原子落盘。"""

import os
import re
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.presentation import Presentation as PresentationType

from modules.translation.contracts import (
    BatchTranslator,
    TranslationBatchResult,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)

from .entities import PptxSegment, PptxSegmentKey
from .extractor import PptxExtractor
from .formatter import PptxFormatter
from .markup import (
    deterministic_repair,
    is_english_language,
    normalize_english_span_spacing,
    validate_marked_output,
)
from .table_extractor import PptxTableExtractor
from .table_formatter import PptxTableFormatter

MIN_GROUP_TOKENS = 500
MAX_GROUP_TOKENS = 800


@dataclass(slots=True)
class _TranslationStats:
    """累计正文, notes 和 table batch 的处理与结果计数."""

    processed_segments: int = 0
    translated_segments: int = 0
    fallback_segments: int = 0
    warning_codes: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _PresentationSignature:
    """表示替换目标文件前需要校验的小型结构指纹."""

    slide_count: int
    slide_width: int
    slide_height: int
    shapes_per_slide: tuple[int, ...]
    tables_per_slide: tuple[int, ...]
    notes_relationships: tuple[tuple[tuple[str, str], ...], ...]


class PptxPipeline:
    """提供结构 fallback 和原子落盘的同步 PPTX 翻译 pipeline."""

    document_kind = "pptx"

    def __init__(
        self,
        translator: BatchTranslator,
        *,
        translate_tables: bool = True,
        translate_notes: bool = True,
    ) -> None:
        """使用注入的 provider-neutral translator 配置 pipeline."""

        self._translator = translator
        self._translate_tables = translate_tables
        self._translate_notes = translate_notes
        self._extractor = PptxExtractor()
        self._table_extractor = PptxTableExtractor()
        self._formatter = PptxFormatter()
        self._table_formatter = PptxTableFormatter(self._formatter)

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """翻译共享 request, 并生成确定性的输出文件名."""

        language_suffix = _safe_path_component(request.target_language)
        output_path = request.output_dir / (
            f"{request.source_path.stem}.translated-{language_suffix}.pptx"
        )
        return self.translate_to(
            source_path=request.source_path,
            output_path=output_path,
            source_language=request.source_language,
            target_language=request.target_language,
            report_progress=report_progress,
        )

    def translate_to(
        self,
        *,
        source_path: str | Path,
        output_path: str | Path,
        source_language: str | None,
        target_language: str,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """把一个源 presentation 翻译到显式指定的输出路径."""

        if report_progress is not None:
            report_progress(TranslationProgress(stage="extracting"))
        source = Path(source_path)
        output = Path(output_path)
        self._validate_paths(source, output)
        output.parent.mkdir(parents=True, exist_ok=True)

        source_presentation = Presentation(str(source))
        expected_signature = _presentation_signature(source_presentation)
        extracted = self._extractor.extract(
            source_presentation, include_notes=self._translate_notes
        )
        shape_segments = tuple(segment for segment in extracted if segment.scope == "shape")
        notes_segments = tuple(segment for segment in extracted if segment.scope == "notes")
        table_segments = (
            self._table_extractor.extract(source_presentation) if self._translate_tables else ()
        )
        total_segments = len(shape_segments) + len(notes_segments) + len(table_segments)
        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="translating",
                    total_segments=total_segments,
                )
            )

        translations: dict[PptxSegmentKey, str] = {}
        stats = _TranslationStats()
        self._translate_segments(
            shape_segments,
            format_hint="pptx",
            fallback_warning="pptx_segment_fallback",
            source_language=source_language,
            target_language=target_language,
            translations=translations,
            stats=stats,
            total_segments=total_segments,
            report_progress=report_progress,
        )
        self._translate_segments(
            notes_segments,
            # notes 文本往往需要和页面正文不同的语气, 因此使用独立 hint. 位置 marker
            # 仍把每条 note 绑定到所属 slide.
            format_hint="pptx_notes",
            fallback_warning="pptx_notes_fallback",
            source_language=source_language,
            target_language=target_language,
            translations=translations,
            stats=stats,
            total_segments=total_segments,
            report_progress=report_progress,
        )
        self._translate_segments(
            table_segments,
            format_hint="pptx_table",
            fallback_warning="pptx_table_fallback",
            source_language=source_language,
            target_language=target_language,
            translations=translations,
            stats=stats,
            total_segments=total_segments,
            report_progress=report_progress,
        )

        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="formatting",
                    processed_segments=stats.processed_segments,
                    total_segments=total_segments,
                )
            )

        target_presentation = Presentation(str(source))
        self._formatter.apply(
            target_presentation,
            (*shape_segments, *notes_segments),
            translations,
            target_language=target_language,
        )
        self._table_formatter.apply(
            target_presentation,
            table_segments,
            translations,
            target_language=target_language,
        )
        self._write_atomically(
            target_presentation,
            output,
            expected_signature=expected_signature,
        )
        return TranslationResult(
            output_path=output,
            document_kind="pptx",
            translated_segments=stats.translated_segments,
            fallback_segments=stats.fallback_segments,
            warning_codes=tuple(sorted(stats.warning_codes)),
        )

    def _translate_segments(
        self,
        segments: Sequence[PptxSegment],
        *,
        format_hint: str,
        fallback_warning: str,
        source_language: str | None,
        target_language: str,
        translations: dict[PptxSegmentKey, str],
        stats: _TranslationStats,
        total_segments: int,
        report_progress: TranslationProgressReporter | None,
    ) -> None:
        """翻译 slide 内部的 group, 并为每个 segment 保留安全结果."""

        for group in _group_segments(segments):
            try:
                batch_result = self._translator.translate_batch(
                    texts=[segment.translation_text for segment in group],
                    source_language=source_language,
                    target_language=target_language,
                    format_hint=format_hint,
                )
                candidates = _index_batch_result(batch_result, len(group))
            except Exception:
                # 整批 provider 调用失败时没有可修复的译文。直接 fallback, 避免为
                # group 中每个 segment 再发一次无意义的 repair, 形成 N+1 调用。
                candidates = {}

            accepted: dict[int, str] = {}
            malformed: list[tuple[int, PptxSegment, str]] = []
            for index, segment in enumerate(group):
                candidate = candidates.get(index)
                # 缺失或空 candidate 同样没有可修复内容。只有 provider 真正返回了
                # 非空但结构损坏的文本, 才允许消耗一次 repair 调用。
                if candidate is not None and candidate.strip():
                    translated = self._accept_strict_candidate(segment, candidate)
                    if translated is not None:
                        accepted[index] = translated
                    else:
                        malformed.append((index, segment, candidate))

            # 同一批 malformed 结果一次送入 repair, 而不是为每个 segment 发单独
            # 请求。这样可避免文档越坏调用数越接近
            # N+1, 同时仍让缺失 candidate 直接 fallback。
            accepted.update(
                self._repair_batch(
                    malformed,
                    format_hint=format_hint,
                    source_language=source_language,
                    target_language=target_language,
                )
            )

            for index, segment in enumerate(group):
                translated = accepted.get(index)
                if translated is None:
                    # 异常模型响应绝不能把文本错写进其他 run. 原始 marked text 是
                    # 唯一安全的最终 fallback.
                    translated = segment.marked_text
                    stats.fallback_segments += 1
                    stats.warning_codes.add(fallback_warning)
                else:
                    if is_english_language(target_language):
                        # 分散在相邻视觉 run 的 English 译文常被模型无空格拼接。
                        # 只在结构校验或 repair 已通过后补空格, 不触碰 marker 骨架。
                        translated = normalize_english_span_spacing(translated)
                    stats.translated_segments += 1
                translations[segment.key] = translated
            # repair 与 fallback 都已收敛为最终安全结果后, 这一批才能计入完成数。
            stats.processed_segments += len(group)
            if report_progress is not None:
                report_progress(
                    TranslationProgress(
                        stage="translating",
                        processed_segments=stats.processed_segments,
                        total_segments=total_segments,
                    )
                )

    def _repair_batch(
        self,
        malformed: Sequence[tuple[int, PptxSegment, str]],
        *,
        format_hint: str,
        source_language: str | None,
        target_language: str,
    ) -> dict[int, str]:
        """批量重试结构损坏的 segment, 再做确定性 wrapper repair。"""

        if not malformed:
            return {}

        repaired_candidates: dict[int, str] = {}
        try:
            # repair 明确限制为一次 batch provider 调用。损坏的 marker 不能触发
            # 无限重试, 也不能制造随 segment 数线性增长的额外调用。
            repair_result = self._translator.translate_batch(
                texts=[segment.translation_text for _, segment, _ in malformed],
                source_language=source_language,
                target_language=target_language,
                format_hint=f"{format_hint}_repair",
                repair_candidates=tuple(candidate for _, _, candidate in malformed),
            )
            repaired_candidates = _index_batch_result(repair_result, len(malformed))
        except Exception:
            pass

        repaired: dict[int, str] = {}
        for repair_index, (group_index, segment, initial_candidate) in enumerate(malformed):
            repaired_candidate = repaired_candidates.get(repair_index)
            strict_repair = self._accept_strict_candidate(segment, repaired_candidate)
            if strict_repair is not None:
                repaired[group_index] = strict_repair
                continue

            for candidate in (repaired_candidate, initial_candidate):
                if candidate is None or not candidate.strip():
                    continue
                content = _candidate_content(segment, candidate, require_marker=False)
                # 确定性 repair 仅重排 provider 已返回的文本并恢复 SPACE slot;
                # repair 后仍需通过完整 span 骨架校验才能进入 formatter。
                deterministic = deterministic_repair(content, segment.marked_text)
                if deterministic is not None:
                    repaired[group_index] = deterministic
                    break
        return repaired

    @staticmethod
    def _accept_strict_candidate(segment: PptxSegment, candidate: str | None) -> str | None:
        """只有位置和 span 骨架均未变化时才接受 candidate."""

        if candidate is None:
            return None
        content = _candidate_content(segment, candidate, require_marker=True)
        return content if validate_marked_output(content, segment.marked_text) else None

    @staticmethod
    def _validate_paths(source: Path, output: Path) -> None:
        """尽早拒绝缺失, 格式错误或会覆盖源文件的路径."""

        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".pptx":
            raise ValueError("PPTX pipeline only accepts .pptx input")
        if output.suffix.lower() != ".pptx":
            raise ValueError("PPTX output path must end in .pptx")
        if source.resolve() == output.resolve():
            raise ValueError("PPTX output must not overwrite the source file")

    def _write_atomically(
        self,
        presentation: PresentationType,
        output_path: Path,
        *,
        expected_signature: _PresentationSignature,
    ) -> None:
        """保存, 重新打开并校验 presentation, 最后原子发布."""

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.",
            suffix=".tmp.pptx",
            dir=output_path.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        try:
            presentation.save(str(temporary_path))
            # 在目标文件可见前重新打开, 可以先拦住损坏的 OOXML. 校验通过后再用
            # os.replace 一次性发布完整 package.
            self._validate_output(temporary_path, expected_signature)
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _validate_output(
        self,
        temporary_path: Path,
        expected_signature: _PresentationSignature,
    ) -> None:
        """确认落盘后的 package 保留 slide, shape, table 和 notes relationship."""

        reopened = Presentation(str(temporary_path))
        actual_signature = _presentation_signature(reopened)
        if actual_signature != expected_signature:
            raise ValueError(
                "translated PPTX failed structural validation: "
                f"expected {expected_signature}, got {actual_signature}"
            )


def _candidate_content(segment: PptxSegment, candidate: str, *, require_marker: bool) -> str:
    """移除精确的位置 marker, 并可要求 marker 必须位于开头."""

    stripped = candidate.strip()
    if stripped.startswith(segment.marker):
        return stripped[len(segment.marker) :].strip()
    if require_marker:
        return ""
    marker_position = stripped.find(segment.marker)
    if marker_position >= 0:
        return stripped[marker_position + len(segment.marker) :].strip()
    return stripped


def _index_batch_result(
    result: TranslationBatchResult | None, expected_count: int
) -> dict[int, str]:
    """只保留范围内且唯一的结果索引, 防止文本在 segment 间错位."""

    if result is None:
        return {}
    counts = Counter(item.index for item in result.items)
    return {
        item.index: item.text
        for item in result.items
        if 0 <= item.index < expected_count and counts[item.index] == 1
    }


def _group_segments(
    segments: Sequence[PptxSegment],
) -> tuple[tuple[PptxSegment, ...], ...]:
    """构建保守的 500-800 token batch, 不跨越 slide 边界."""

    groups: list[tuple[PptxSegment, ...]] = []
    current: list[PptxSegment] = []
    current_tokens = 0
    current_slide: int | None = None
    for segment in segments:
        token_count = _estimate_tokens(segment.translation_text)
        crosses_slide = current_slide is not None and segment.slide_index != current_slide
        if crosses_slide:
            # slide 内 batching 与 PowerPoint 的阅读上下文一致. 重复出现的 shape 和
            # paragraph marker 对模型及 formatter 都不会产生歧义.
            if current:
                groups.append(tuple(current))
            current = []
            current_tokens = 0

        if token_count > MAX_GROUP_TOKENS:
            if current:
                groups.append(tuple(current))
                current = []
                current_tokens = 0
            # 单个段落无法在不虚构 run 边界的前提下拆分. 与其冒险错误重建,
            # 不如保留一个超出上限的独立 batch.
            groups.append((segment,))
            current_slide = segment.slide_index
            continue

        if current and current_tokens + token_count > MAX_GROUP_TOKENS:
            if current_tokens < MIN_GROUP_TOKENS and len(current) == 1:
                # 保持源 pipeline 行为: 一个过小段落可以和相邻段落合批, 即使略微超过
                # max, 也比产生病态的小调用更合理.
                current.append(segment)
                groups.append(tuple(current))
                current = []
                current_tokens = 0
                current_slide = segment.slide_index
                continue
            groups.append(tuple(current))
            current = []
            current_tokens = 0

        current.append(segment)
        current_tokens += token_count
        current_slide = segment.slide_index
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _estimate_tokens(text: str) -> int:
    """估算 token 数量, 不引入特定模型的 tokenizer 依赖."""

    visible = re.sub(r"\[[^\]]+\]|</?span>|<SPACE>", "", text)
    return max(1, (len(visible) + 3) // 4)


def _presentation_signature(presentation: PresentationType) -> _PresentationSignature:
    """记录翻译过程中不允许新增, 丢失或重连的结构."""

    shapes_per_slide: list[int] = []
    tables_per_slide: list[int] = []
    notes_relationships: list[tuple[tuple[str, str], ...]] = []
    for slide in presentation.slides:
        shapes_per_slide.append(_count_shapes(slide.shapes))
        tables_per_slide.append(_count_tables(slide.shapes))
        relationships = tuple(
            sorted(
                (
                    relationship.rId,
                    str(relationship.target_part.partname),
                )
                for relationship in slide.part.rels.values()
                # rId 和 target part 共同证明 note 仍连接到原 slide, 而不是在
                # package 的其他位置被重新创建.
                if relationship.reltype.endswith("/notesSlide")
            )
        )
        notes_relationships.append(relationships)
    return _PresentationSignature(
        slide_count=len(presentation.slides),
        slide_width=presentation.slide_width,
        slide_height=presentation.slide_height,
        shapes_per_slide=tuple(shapes_per_slide),
        tables_per_slide=tuple(tables_per_slide),
        notes_relationships=tuple(notes_relationships),
    )


def _count_shapes(shapes: Any) -> int:
    """递归统计顶层 shape 和 group 内 shape."""

    count = 0
    for shape in shapes:
        count += 1
        if hasattr(shape, "shapes"):
            count += _count_shapes(shape.shapes)
    return count


def _count_tables(shapes: Any) -> int:
    """递归统计 table, 包括嵌套在 group 中的 table."""

    count = 0
    for shape in shapes:
        if getattr(shape, "has_table", False):
            count += 1
        if hasattr(shape, "shapes"):
            count += _count_tables(shape.shapes)
    return count


def _safe_path_component(value: str) -> str:
    """把语言代码转换为跨平台安全的文件名后缀."""

    component = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    return component or "translated"
