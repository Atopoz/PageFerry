"""编排 TXT/Markdown 的读取、保护、翻译、恢复与原子落盘。"""

import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from modules.plain_text.markdown import MarkdownProtector, _replace_segments
from modules.plain_text.markdown_table import table_row_signature
from modules.plain_text.models import TextSegment
from modules.plain_text.reader import TextFileReader
from modules.plain_text.segmenter import TextSegmenter
from modules.translation.contracts import (
    BatchTranslator,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)

PlainTextKind = Literal["txt", "md"]
_SAFE_LANGUAGE_RE = re.compile(r"[^A-Za-z0-9_-]+")


class PlainTextPipeline:
    """运行 TXT 或 Markdown 的本地文件进、文件出翻译流程。"""

    def __init__(self, document_kind: PlainTextKind, translator: BatchTranslator) -> None:
        """绑定格式与 provider-neutral translator。"""

        self.document_kind = document_kind
        self._translator = translator
        self._reader = TextFileReader()
        self._segmenter = TextSegmenter()
        self._markdown = MarkdownProtector()

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """翻译一个文本文件, 结构异常时保留原文并显式记录 fallback。"""

        if report_progress is not None:
            report_progress(TranslationProgress(stage="extracting"))
        source = request.source_path.resolve()
        if source.suffix.lower() != f".{self.document_kind}":
            raise ValueError("document_kind_mismatch")
        read = self._reader.read(source)
        prepared = self._markdown.prepare(read.text) if self.document_kind == "md" else None
        working_text = prepared.working_text if prepared is not None else read.text
        segments = (
            self._segmenter.markdown(working_text)
            if self.document_kind == "md"
            else self._segmenter.txt(working_text)
        )
        total_segments = len(segments)
        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="translating",
                    total_segments=total_segments,
                )
            )
        translations, fallback_count = self._translate_segments(
            segments,
            source_language=request.source_language,
            target_language=request.target_language,
            line_ending=read.line_ending,
            read_only_context=prepared.context_snippets if prepared is not None else (),
            report_progress=report_progress,
        )

        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="formatting",
                    processed_segments=total_segments,
                    total_segments=total_segments,
                )
            )

        warning_codes: list[str] = []
        if fallback_count:
            warning_codes.append("segment_fallback")
        try:
            output_text = (
                self._markdown.restore(
                    prepared=prepared,
                    segments=segments,
                    translations=translations,
                )
                if prepared is not None
                else _replace_segments(read.text, segments, translations)
            )
        except ValueError:
            output_text = read.text
            fallback_count = len(segments)
            warning_codes = ["markdown_structure_fallback"]

        output = self._output_path(request)
        self._atomic_write(output, output_text, read.encoding)
        return TranslationResult(
            output_path=output,
            document_kind=self.document_kind,
            translated_segments=len(segments) - fallback_count,
            fallback_segments=fallback_count,
            warning_codes=tuple(warning_codes),
        )

    def _translate_segments(
        self,
        segments: list[TextSegment],
        *,
        source_language: str | None,
        target_language: str,
        line_ending: str,
        read_only_context: tuple[str, ...],
        report_progress: TranslationProgressReporter | None,
    ) -> tuple[dict[str, str], int]:
        """逐批调用模型, 并把缺 index 或破坏 marker 的结果回退为原文。"""

        translations: dict[str, str] = {}
        fallback_count = 0
        processed_segments = 0
        total_segments = len(segments)
        for group in self._segmenter.batches(segments):
            try:
                result = self._translator.translate_batch(
                    texts=[segment.source_text for segment in group],
                    source_language=source_language,
                    target_language=target_language,
                    format_hint=self.document_kind,
                    read_only_context=read_only_context,
                )
                indexed = {item.index: item.text for item in result.items}
                if set(indexed) != set(range(len(group))):
                    raise ValueError("translation_index_mismatch")
            except Exception:
                indexed = {}

            for index, segment in enumerate(group):
                translated = indexed.get(index)
                if translated is None or not self._candidate_is_usable(segment, translated):
                    translations[segment.segment_id] = segment.source_text
                    fallback_count += 1
                    continue
                translations[segment.segment_id] = self._normalize_line_endings(
                    translated,
                    line_ending,
                )
            # 一个 batch 的所有候选都已接受或 fallback 后才上报, 避免把尚未落定的
            # provider 返回计入完成数。
            processed_segments += len(group)
            if report_progress is not None:
                report_progress(
                    TranslationProgress(
                        stage="translating",
                        processed_segments=processed_segments,
                        total_segments=total_segments,
                    )
                )
        return translations, fallback_count

    def _candidate_is_usable(self, segment: TextSegment, translated: str) -> bool:
        """拒绝空译文及破坏 Markdown marker 或 table 骨架的结果。"""

        # segment 的源文本一定非空; 接受空白结果会静默清空用户正文。
        if not translated.strip():
            return False
        if not self._markers_preserved(segment.source_text, translated):
            return False
        if self.document_kind == "md" and segment.kind == "table_row":
            # table row 以未转义 pipe 划分 cell; escaped pipe 只是 cell 内正文。
            return table_row_signature(segment.source_text) == table_row_signature(translated)
        return True

    def _markers_preserved(self, source: str, translated: str) -> bool:
        """确认 Markdown protected placeholder 没有被模型增删。"""

        if self.document_kind != "md":
            return True
        return sorted(self._markdown.placeholders(source)) == sorted(
            self._markdown.placeholders(translated)
        )

    @staticmethod
    def _normalize_line_endings(text: str, line_ending: str) -> str:
        """把模型返回的混合换行统一成源文档风格。"""

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.replace("\n", line_ending)

    def _output_path(self, request: TranslationRequest) -> Path:
        """生成不会与源文件重合的确定性输出路径。"""

        request.output_dir.mkdir(parents=True, exist_ok=True)
        language = _SAFE_LANGUAGE_RE.sub("-", request.target_language).strip("-") or "translated"
        output_name = f"{request.source_path.stem}.{language}{request.source_path.suffix}"
        output = request.output_dir / output_name
        if output.resolve() == request.source_path.resolve():
            raise ValueError("output_would_overwrite_source")
        return output

    @staticmethod
    def _atomic_write(output: Path, text: str, encoding: str) -> None:
        """先 fsync 临时文件, 再用 os.replace 原子发布完整结果。"""

        # 临时文件和最终文件位于同一目录, 确保 os.replace 不跨文件系统。
        descriptor, temp_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
        )
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode(encoding))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, output)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
