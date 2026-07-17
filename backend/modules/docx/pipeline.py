"""编排 DOCX 提取、结构翻译、回退校验、package 校验与原子落盘。"""

import os
import re
import tempfile
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocumentType
from lxml import etree

from modules.translation.batch_fanout import (
    BatchFanoutOutcome,
    run_batch_fanout,
    translator_per_job_concurrency,
)
from modules.translation.contracts import (
    BatchTranslator,
    TranslationArtifact,
    TranslationBatchResult,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)

from .bilingual_formatter import DocxBilingualFormatter
from .entities import DocxSegment, StoryKind, TableRowSegment
from .extractor import DocxExtractor
from .formatter import DocxFormatter
from .markup import (
    candidate_content,
    deterministic_repair,
    is_english_language,
    normalize_english_span_spacing,
    validate_marked_output,
)
from .table_extractor import DocxTableExtractor
from .table_formatter import DocxTableFormatter
from .token_counter import group_by_token_limit
from .xml_helpers import parse_xml

_SAFE_LANGUAGE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_AUXILIARY_STORY_RE = re.compile(
    r"^word/(?:document|header\d+|footer\d+|footnotes|endnotes|comments\d*)\.xml$"
)


@dataclass(slots=True)
class _TranslationStats:
    """累计处理完成、成功、回退和可见 warning 统计。"""

    processed_segments: int = 0
    translated_segments: int = 0
    fallback_segments: int = 0
    warning_codes: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _GroupTranslation:
    """保存一个 DOCX group 已通过 repair/fallback 收敛的安全结果。"""

    items: tuple[tuple[Any, str], ...]
    translated_segments: int
    fallback_segments: int
    warning_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PackageSignature:
    """记录翻译不允许改变的 story 结构与 relationship 图。"""

    story_structures: tuple[tuple[str, tuple[str, ...]], ...]
    relationships: tuple[tuple[str, str, str, str, str], ...]


class DocxPipeline:
    """同步执行文件进、文件出的结构保真 DOCX 翻译。"""

    document_kind = "docx"

    def __init__(
        self,
        translator: BatchTranslator,
        *,
        translate_tables: bool = True,
        bilingual: bool = False,
    ) -> None:
        """绑定 provider-neutral translator 与表格开关。"""

        self._translator = translator
        self._translate_tables = translate_tables
        self._bilingual = bilingual
        self._extractor = DocxExtractor()
        self._table_extractor = DocxTableExtractor(self._extractor)
        self._formatter = DocxFormatter()
        self._table_formatter = DocxTableFormatter(self._formatter)
        self._bilingual_formatter = DocxBilingualFormatter()

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """翻译共享 request, 并生成不会覆盖源文件的确定性输出名。"""

        language = _safe_path_component(request.target_language)
        output_path = request.output_dir / f"{request.source_path.stem}.{language}.docx"
        bilingual_output_path = (
            request.output_dir / f"{request.source_path.stem}.bilingual-{language}.docx"
            if self._bilingual
            else None
        )
        return self.translate_to(
            source_path=request.source_path,
            output_path=output_path,
            source_language=request.source_language,
            target_language=request.target_language,
            bilingual_output_path=bilingual_output_path,
            report_progress=report_progress,
        )

    def translate_to(
        self,
        *,
        source_path: str | Path,
        output_path: str | Path,
        source_language: str | None,
        target_language: str,
        bilingual_output_path: str | Path | None = None,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """把一个 DOCX 翻译到显式目标路径。"""

        if report_progress is not None:
            report_progress(TranslationProgress(stage="extracting"))
        source = Path(source_path)
        output = Path(output_path)
        bilingual_output = Path(bilingual_output_path) if bilingual_output_path else None
        self._validate_paths(source, output)
        if bilingual_output is not None:
            self._validate_paths(source, bilingual_output)
            if bilingual_output.resolve() == output.resolve():
                raise ValueError("docx_artifact_paths_collide")
        output.parent.mkdir(parents=True, exist_ok=True)
        expected_signature = _package_signature(source)

        document = Document(str(source))
        segments = self._extractor.extract(document, source)
        tables = self._table_extractor.extract(document) if self._translate_tables else ()
        table_rows = self._table_extractor.translatable_rows(tables)
        total_segments = len(segments) + len(table_rows)
        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="translating",
                    total_segments=total_segments,
                )
            )

        paragraph_translations: dict[tuple[StoryKind, int, str | None, int], str] = {}
        table_translations: dict[tuple[int, int], str] = {}
        stats = _TranslationStats()
        self._translate_segments(
            segments,
            translations=paragraph_translations,
            stats=stats,
            format_hint="docx",
            fallback_warning="docx_segment_fallback",
            source_language=source_language,
            target_language=target_language,
            total_segments=total_segments,
            report_progress=report_progress,
        )
        self._translate_segments(
            table_rows,
            translations=table_translations,
            stats=stats,
            format_hint="docx_table",
            fallback_warning="docx_table_fallback",
            source_language=source_language,
            target_language=target_language,
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

        self._formatter.apply(
            document,
            segments,
            paragraph_translations,
            target_language=target_language,
        )
        self._table_formatter.apply(
            document,
            tables,
            table_translations,
            target_language=target_language,
        )
        self._write_atomically(
            document,
            output,
            expected_signature=expected_signature,
            note_segments=segments,
            paragraph_translations=paragraph_translations,
            target_language=target_language,
        )
        artifacts = [TranslationArtifact(kind="translated", path=output)]
        if self._bilingual:
            if bilingual_output is None:
                bilingual_output = output.with_name(f"{output.stem}.bilingual{output.suffix}")
            try:
                self._write_bilingual_atomically(
                    source,
                    output,
                    bilingual_output,
                    segments=segments,
                    source_signature=expected_signature,
                )
            except Exception:
                # 请求的派生物必须完整交付; 双语版失败时不留下看似成功的半套结果。
                output.unlink(missing_ok=True)
                bilingual_output.unlink(missing_ok=True)
                raise
            artifacts.append(TranslationArtifact(kind="bilingual", path=bilingual_output))
        return TranslationResult(
            output_path=output,
            document_kind="docx",
            artifacts=tuple(artifacts),
            translated_segments=stats.translated_segments,
            fallback_segments=stats.fallback_segments,
            warning_codes=tuple(sorted(stats.warning_codes)),
        )

    def _translate_segments(
        self,
        segments: Sequence[DocxSegment] | Sequence[TableRowSegment],
        *,
        translations: dict[Any, str],
        stats: _TranslationStats,
        format_hint: str,
        fallback_warning: str,
        source_language: str | None,
        target_language: str,
        total_segments: int,
        report_progress: TranslationProgressReporter | None,
    ) -> None:
        """按 500-800 token 分组并有界并发, 为每个 segment 留下安全结果。"""

        groups = _translation_groups(segments)

        def translate_group(
            group: tuple[DocxSegment | TableRowSegment, ...],
        ) -> _GroupTranslation:
            """让一个 worker 完成首轮、repair 与 fallback 的完整收敛。"""

            return self._translate_group(
                group,
                format_hint=format_hint,
                fallback_warning=fallback_warning,
                source_language=source_language,
                target_language=target_language,
            )

        def commit_group(
            _group_index: int,
            group: tuple[DocxSegment | TableRowSegment, ...],
            outcome: BatchFanoutOutcome[_GroupTranslation],
        ) -> None:
            """按原 group 顺序写回已收敛结果, 再推进真实完成进度。"""

            group_result = outcome.value
            if group_result is None:
                # worker 之外的异常仍只影响自己的 group, 其他并发结果继续提交。
                group_result = _GroupTranslation(
                    items=tuple((segment.key, segment.marked_text) for segment in group),
                    translated_segments=0,
                    fallback_segments=len(group),
                    warning_codes=(fallback_warning,),
                )
            translations.update(group_result.items)
            stats.translated_segments += group_result.translated_segments
            stats.fallback_segments += group_result.fallback_segments
            stats.warning_codes.update(group_result.warning_codes)
            stats.processed_segments += len(group)
            if report_progress is not None:
                report_progress(
                    TranslationProgress(
                        stage="translating",
                        processed_segments=stats.processed_segments,
                        total_segments=total_segments,
                    )
                )

        run_batch_fanout(
            groups,
            translate_group,
            max_concurrency=translator_per_job_concurrency(self._translator),
            on_group_settled=commit_group,
        )

    def _translate_group(
        self,
        group: Sequence[DocxSegment | TableRowSegment],
        *,
        format_hint: str,
        fallback_warning: str,
        source_language: str | None,
        target_language: str,
    ) -> _GroupTranslation:
        """完成一个 DOCX group 的 provider 调用、repair 与安全 fallback。"""

        result: TranslationBatchResult | None
        try:
            result = self._translator.translate_batch(
                texts=[segment.translation_text for segment in group],
                source_language=source_language,
                target_language=target_language,
                format_hint=format_hint,
            )
        except Exception:
            result = None
        candidates = _index_batch_result(result, len(group))
        items: list[tuple[Any, str]] = []
        translated_segments = 0
        fallback_segments = 0
        warning_codes: set[str] = set()
        for index, segment in enumerate(group):
            initial_candidate = candidates.get(index)
            translated = self._accept_strict_candidate(segment, initial_candidate)
            repair_warning: str | None = None
            if translated is None and initial_candidate and initial_candidate.strip():
                # 只有 provider 确实返回了非空坏结果才 repair; batch 异常或缺项直接
                # fallback, 避免一次故障膨胀成每个 segment 一次额外 API 请求。
                translated, repair_warning = self._repair_once(
                    segment,
                    initial_candidate=initial_candidate,
                    format_hint=format_hint,
                    source_language=source_language,
                    target_language=target_language,
                )
            if translated is None:
                # 写错 run 比保留原文更危险; 最终边界只能回退完整源骨架。
                translated = segment.marked_text
                fallback_segments += 1
                warning_codes.add(fallback_warning)
            else:
                if is_english_language(target_language):
                    # 成功译文写回前补齐跨 run 的 English 词间空格; 表格 cell
                    # 是独立文本槽, 不能跨 cell 补。
                    blocked_boundaries = (
                        segment.cell_span_boundaries
                        if isinstance(segment, TableRowSegment)
                        else frozenset()
                    )
                    translated = normalize_english_span_spacing(
                        translated,
                        blocked_boundaries=blocked_boundaries,
                    )
                translated_segments += 1
                if repair_warning is not None:
                    warning_codes.add(repair_warning)
            items.append((segment.key, translated))
        return _GroupTranslation(
            items=tuple(items),
            translated_segments=translated_segments,
            fallback_segments=fallback_segments,
            warning_codes=tuple(sorted(warning_codes)),
        )

    def _repair_once(
        self,
        segment: DocxSegment | TableRowSegment,
        *,
        initial_candidate: str | None,
        format_hint: str,
        source_language: str | None,
        target_language: str,
    ) -> tuple[str | None, str | None]:
        """最多调用模型修复一次, 再尝试不猜文本分界的确定性修复。"""

        repaired_candidate: str | None = None
        try:
            # 修复固定为一次, 避免坏 marker 触发无界请求和不可控 API 成本。
            repair_result = self._translator.translate_batch(
                texts=[segment.translation_text],
                source_language=source_language,
                target_language=target_language,
                format_hint=f"{format_hint}_repair",
                # source skeleton 是权威输入, 坏候选按同一 index 作为不可信数据传入。
                # provider 可复用其译文措辞, 但不能把 candidate 当 instruction 执行。
                repair_candidates=(initial_candidate or "",),
            )
            repaired_candidate = _index_batch_result(repair_result, 1).get(0)
            strict = self._accept_strict_candidate(segment, repaired_candidate)
            if strict is not None:
                return strict, "docx_model_repair"
        except Exception:
            pass

        for candidate in (repaired_candidate, initial_candidate):
            content = candidate_content(candidate, segment.marker, require_marker=False)
            if content is None:
                continue
            deterministic = deterministic_repair(content, segment.marked_text)
            if deterministic is not None:
                return deterministic, "docx_deterministic_repair"
        return None, None

    def _accept_strict_candidate(
        self,
        segment: DocxSegment | TableRowSegment,
        candidate: str | None,
    ) -> str | None:
        """仅接受 marker 和 span/p 骨架都完整的候选译文。"""

        content = candidate_content(candidate, segment.marker, require_marker=True)
        if content is None:
            return None
        return content if validate_marked_output(content, segment.marked_text) else None

    def _write_atomically(
        self,
        document: DocumentType,
        output: Path,
        *,
        expected_signature: _PackageSignature,
        note_segments: tuple[DocxSegment, ...],
        paragraph_translations: dict[tuple[StoryKind, int, str | None, int], str],
        target_language: str,
    ) -> None:
        """保存、重写 note、校验完整 package 后再原子发布。"""

        descriptor, temp_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp.docx",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            document.save(str(temporary))
            self._formatter.apply_note_translations(
                temporary,
                note_segments,
                paragraph_translations,
                target_language=target_language,
            )
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_output(temporary, expected_signature)
            # 同目录临时文件保证 os.replace 不跨文件系统, 已有目标只会在校验通过后被替换。
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _validate_output(
        self,
        temporary_path: Path,
        expected_signature: _PackageSignature,
    ) -> None:
        """确认 ZIP、python-docx 解析和 story 结构签名均有效。"""

        with zipfile.ZipFile(temporary_path) as package:
            if package.testzip() is not None:
                raise ValueError("docx_zip_crc_failure")
            names = set(package.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValueError("docx_required_part_missing")
            for part_name in names:
                if _AUXILIARY_STORY_RE.match(part_name):
                    parse_xml(package.read(part_name))
        Document(str(temporary_path))
        if _package_signature(temporary_path) != expected_signature:
            raise ValueError("docx_structure_signature_changed")

    def _write_bilingual_atomically(
        self,
        source: Path,
        translated: Path,
        output: Path,
        *,
        segments: tuple[DocxSegment, ...],
        source_signature: _PackageSignature,
    ) -> None:
        """不再次调用模型, 从译文版生成并校验双语派生文件。"""

        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp.docx",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            source_document = Document(str(source))
            bilingual_document = Document(str(translated))
            self._bilingual_formatter.apply(source_document, bilingual_document, segments)
            bilingual_document.save(str(temporary))
            # 双语布局会有意增加 run 与换行, 结构签名以布局完成后的临时包为准;
            # relationship 图仍必须和源文件完全一致。
            layout_signature = _package_signature(temporary)
            expected_signature = _PackageSignature(
                story_structures=layout_signature.story_structures,
                relationships=source_signature.relationships,
            )
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_output(temporary, expected_signature)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_paths(source: Path, output: Path) -> None:
        """提前拒绝缺失、格式错误和覆盖源文件的路径。"""

        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".docx":
            raise ValueError("document_kind_mismatch")
        if source.resolve() == output.resolve():
            raise ValueError("output_would_overwrite_source")


def _safe_path_component(value: str) -> str:
    """把目标语言规范为安全且非空的文件名片段。"""

    return _SAFE_LANGUAGE_RE.sub("-", value).strip("-") or "translated"


def _index_batch_result(
    result: TranslationBatchResult | None,
    expected_count: int,
) -> dict[int, str]:
    """只保留范围内且恰好出现一次的 provider 结果 index。"""

    if result is None:
        return {}
    counts = Counter(item.index for item in result.items)
    return {
        item.index: item.text
        for item in result.items
        if 0 <= item.index < expected_count and counts[item.index] == 1
    }


def _translation_groups(
    segments: Sequence[DocxSegment] | Sequence[TableRowSegment],
) -> tuple[tuple[DocxSegment | TableRowSegment, ...], ...]:
    """按正文兼容投影分组, 并把不同 story 隔离成独立 batch。

    Header、footer 与 note 若混入正文最后一组会改变正文上下文边界, 因此每个
    story 实例单独计数和分组。
    """

    if not segments:
        return ()
    if isinstance(segments[0], TableRowSegment):
        return group_by_token_limit(segments, lambda segment: segment.translation_text)

    story_segments: dict[tuple[StoryKind, int, str | None], list[DocxSegment]] = {}
    for segment in segments:
        if not isinstance(segment, DocxSegment):
            raise TypeError("mixed_docx_segment_types")
        scope = (segment.story_kind, segment.story_index, segment.note_id)
        story_segments.setdefault(scope, []).append(segment)

    groups: list[tuple[DocxSegment | TableRowSegment, ...]] = []
    for scoped_segments in story_segments.values():
        groups.extend(
            group_by_token_limit(
                scoped_segments,
                lambda segment: segment.token_group_text,
            )
        )
    return tuple(groups)


def _package_signature(path: Path) -> _PackageSignature:
    """读取 story XML 节点序列和所有 relationship, 忽略可翻译文本。"""

    story_structures: list[tuple[str, tuple[str, ...]]] = []
    relationships: list[tuple[str, str, str, str, str]] = []
    with zipfile.ZipFile(path) as package:
        for part_name in sorted(package.namelist()):
            if _AUXILIARY_STORY_RE.match(part_name):
                root = parse_xml(package.read(part_name))
                # 排除文本与字体声明。字体 fallback 会合法新增 w:rFonts, 但 bold、
                # size、color 等其他 run properties 仍属于必须保持的结构。
                structure = tuple(
                    etree.QName(element).localname
                    for element in root.iter()
                    if _include_in_structure_signature(element)
                )
                story_structures.append((part_name, structure))
            if part_name.endswith(".rels"):
                root = parse_xml(package.read(part_name))
                for relationship in root:
                    relationships.append(
                        (
                            part_name,
                            relationship.get("Id", ""),
                            relationship.get("Type", ""),
                            relationship.get("Target", ""),
                            relationship.get("TargetMode", ""),
                        )
                    )
    return _PackageSignature(
        story_structures=tuple(story_structures),
        relationships=tuple(sorted(relationships)),
    )


def _include_in_structure_signature(element: Any) -> bool:
    """保留除译文与允许变化的字体声明之外的 story 结构。"""

    local_name = etree.QName(element).localname
    if local_name in {"t", "rFonts"}:
        return False
    if local_name != "rPr":
        return True
    # 一个只为 fallback 新增 w:rFonts 的 w:rPr 不应改变签名; 只要还包含 bold、
    # size、color 等其他子节点, rPr 就必须继续参与结构校验。
    return any(etree.QName(child).localname != "rFonts" for child in element)
