"""编排文本型 PDF 的布局、翻译、内容流回写、校验与原子发布。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pypdfium2 import PdfiumError

from modules.translation.contracts import (
    BatchTranslator,
    TranslationArtifact,
    TranslationProgress,
    TranslationProgressReporter,
    TranslationRequest,
    TranslationResult,
)

from .constants import PDF_INVISIBLE_TEXT_RENDER_MODES, PDF_SCAN_IMAGE_COVERAGE_THRESHOLD
from .entities import PageInfo, TextSpan
from .errors import PdfPipelineError
from .extractor import ParagraphExtractor
from .font_manager import PDF_FONT_DIRECTORY_MISSING, PdfFontResourceError
from .formatter import build_tagged_text_blocks
from .layout import LayoutDetector, LayoutModelError
from .rasterizer import page_image_coverage_ratios
from .renderer import FillOptions, FillRenderMode, ParagraphRenderer
from .side_by_side_composer import PdfSideBySideComposer
from .translator import ParagraphTranslator

_SAFE_LANGUAGE_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class _PdfStructureSignature:
    """记录翻译不允许改变的页面几何与内嵌图片。"""

    pages: tuple[tuple[tuple[float, ...], int], ...]
    images: tuple[tuple[int, tuple[str, ...], int, int, int, str], ...]


class PdfPipeline:
    """同步执行原生文本型 PDF 的文件进、文件出翻译。"""

    document_kind = "pdf"

    def __init__(
        self,
        translator: BatchTranslator,
        layout_detector: LayoutDetector,
        *,
        font_directory: Path,
        bilingual: bool = False,
        side_by_side_composer: PdfSideBySideComposer | None = None,
    ) -> None:
        """绑定 translator、layout detector、字体目录与左右拼页开关。"""

        self._translator = translator
        self._layout_detector = layout_detector
        self._font_directory = font_directory.expanduser().resolve()
        self._bilingual = bilingual
        self._side_by_side_composer = side_by_side_composer or PdfSideBySideComposer()

    def translate(
        self,
        request: TranslationRequest,
        *,
        report_progress: TranslationProgressReporter | None = None,
    ) -> TranslationResult:
        """翻译文本层并原样保留图片, 扫描件或损坏输入以稳定错误失败。"""

        source = request.source_path.resolve()
        output = self._output_path(request)
        bilingual_output = self._bilingual_output_path(request) if self._bilingual else None
        self._validate_paths(source, output)
        if bilingual_output is not None:
            self._validate_paths(source, bilingual_output)
            if bilingual_output.resolve() == output.resolve():
                raise ValueError("pdf_artifact_paths_collide")
        if not self._font_directory.is_dir():
            raise PdfPipelineError(PDF_FONT_DIRECTORY_MISSING)
        source_digest = _file_sha256(source)
        expected_structure = self._preflight(source)
        try:
            self._layout_detector.ensure_model_available()
        except LayoutModelError as error:
            raise PdfPipelineError("pdf_layout_model_missing") from error

        if report_progress is not None:
            report_progress(TranslationProgress(stage="extracting"))
        extractor = ParagraphExtractor(
            str(source),
            layout_detector=self._layout_detector,
        )
        try:
            pages = asyncio.run(extractor.extract_page_info_with_layout())
        except pikepdf.PasswordError as error:
            raise PdfPipelineError("pdf_encrypted") from error
        except (pikepdf.PdfError, PdfiumError, ValueError) as error:
            raise PdfPipelineError("pdf_corrupt") from error
        _validate_supported_text_pages(source, pages)
        blocks = build_tagged_text_blocks(pages)
        if not blocks:
            raise PdfPipelineError("pdf_no_text_layer")

        fallback_chunks = 0

        def report_chunk(processed: int, total: int, fallback: bool) -> None:
            """把 chunk 收敛进度映射到现有 translating stage。"""

            nonlocal fallback_chunks
            if fallback and processed > 0:
                fallback_chunks += 1
            if report_progress is not None:
                report_progress(
                    TranslationProgress(
                        stage="translating",
                        processed_segments=processed,
                        total_segments=total,
                    )
                )

        translation = ParagraphTranslator().translate(
            pages,
            blocks,
            request.source_language,
            request.target_language,
            self._translator,
            on_chunk_settled=report_chunk,
        )
        total_chunks = len(translation.chunks)
        if report_progress is not None:
            report_progress(
                TranslationProgress(
                    stage="formatting",
                    processed_segments=total_chunks,
                    total_segments=total_chunks,
                )
            )

        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_pdf_path(output)
        try:
            ParagraphRenderer(source, font_directory=self._font_directory).apply(
                temporary,
                translation.pages,
                options=FillOptions(
                    render_mode=FillRenderMode.TRANSLATION_ONLY,
                    reuse_block_dominant_color=True,
                ),
                target_language=request.target_language,
            )
            _fsync_file(temporary)
            actual_structure = _pdf_structure_signature(temporary)
            if actual_structure != expected_structure:
                raise ValueError("pdf_structure_signature_changed")
            if _file_sha256(source) != source_digest:
                raise ValueError("source_pdf_changed_during_translation")
            os.replace(temporary, output)
        except PdfFontResourceError as error:
            temporary.unlink(missing_ok=True)
            raise PdfPipelineError(error.code) from error
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        warning_codes: list[str] = []
        if fallback_chunks:
            warning_codes.append("pdf_chunk_fallback")
        if extractor.layout_fallback_used:
            warning_codes.append("pdf_layout_fallback")
        artifacts = [TranslationArtifact(kind="translated", path=output)]
        if bilingual_output is not None:
            try:
                # 双语版只复用已经落盘的译文 PDF, 不能再次调用模型或启用页内堆叠渲染。
                self._side_by_side_composer.compose(source, output, bilingual_output)
                if _file_sha256(source) != source_digest:
                    raise ValueError("source_pdf_changed_during_bilingual_composition")
            except Exception:
                # 请求的两个派生文件必须完整交付, 双语合成失败时不留下半套结果。
                output.unlink(missing_ok=True)
                bilingual_output.unlink(missing_ok=True)
                raise
            artifacts.append(TranslationArtifact(kind="bilingual", path=bilingual_output))
        return TranslationResult(
            output_path=output,
            document_kind="pdf",
            artifacts=tuple(artifacts),
            translated_segments=total_chunks - fallback_chunks,
            fallback_segments=fallback_chunks,
            warning_codes=tuple(warning_codes),
        )

    def _output_path(self, request: TranslationRequest) -> Path:
        """生成不会与源文件重合的确定性 PDF 输出路径。"""

        language = _SAFE_LANGUAGE_RE.sub("-", request.target_language).strip("-")
        language = language or "translated"
        return request.output_dir / f"{request.source_path.stem}.{language}.pdf"

    def _bilingual_output_path(self, request: TranslationRequest) -> Path:
        """生成与译文版同目录且不会覆盖源文件的双语输出路径。"""

        language = _SAFE_LANGUAGE_RE.sub("-", request.target_language).strip("-")
        language = language or "translated"
        return request.output_dir / f"{request.source_path.stem}.bilingual-{language}.pdf"

    @staticmethod
    def _validate_paths(source: Path, output: Path) -> None:
        """拒绝缺失、格式错误和覆盖源 PDF 的路径。"""

        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() != ".pdf":
            raise PdfPipelineError("pdf_unsupported")
        if source == output.resolve():
            raise ValueError("output_would_overwrite_source")

    @staticmethod
    def _preflight(source: Path) -> _PdfStructureSignature:
        """在模型推理前拒绝加密或损坏 PDF, 并记录结构基线。"""

        try:
            with pikepdf.Pdf.open(source) as pdf:
                if pdf.is_encrypted:
                    raise PdfPipelineError("pdf_encrypted")
                if not pdf.pages:
                    raise PdfPipelineError("pdf_corrupt")
            return _pdf_structure_signature(source)
        except PdfPipelineError:
            raise
        except pikepdf.PasswordError as error:
            raise PdfPipelineError("pdf_encrypted") from error
        except pikepdf.PdfError as error:
            raise PdfPipelineError("pdf_corrupt") from error


def _temporary_pdf_path(output: Path) -> Path:
    """在最终输出同目录创建可被 renderer 使用的临时 PDF 路径。"""

    descriptor, name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp.pdf",
    )
    os.close(descriptor)
    return Path(name)


def _validate_supported_text_pages(source: Path, pages: list[PageInfo]) -> None:
    """拒绝没有可见原生文字的扫描页, 并隔离 invisible OCR search layer。"""

    try:
        coverage_ratios = page_image_coverage_ratios(source)
    except PdfiumError as error:
        raise PdfPipelineError("pdf_corrupt") from error
    if len(coverage_ratios) != len(pages):
        raise PdfPipelineError("pdf_corrupt")

    document_has_visible_text = False
    for page_position, page in enumerate(pages):
        spans = tuple(_iter_page_spans(page))
        visible_text = any(
            (span.text or "").strip()
            and span.is_visible
            and span.text_render_mode not in PDF_INVISIBLE_TEXT_RENDER_MODES
            for span in spans
        )
        invisible_text = any(
            (span.text or "").strip()
            and (not span.is_visible or span.text_render_mode in PDF_INVISIBLE_TEXT_RENDER_MODES)
            for span in spans
        )
        document_has_visible_text = document_has_visible_text or visible_text
        if coverage_ratios[page_position] >= PDF_SCAN_IMAGE_COVERAGE_THRESHOLD and (
            not visible_text or invisible_text
        ):
            raise PdfPipelineError("pdf_no_text_layer")

    if not document_has_visible_text:
        raise PdfPipelineError("pdf_no_text_layer")


def _iter_page_spans(page: PageInfo) -> list[TextSpan]:
    """同时收集待翻译与保留 block 的 span, 避免漏掉 invisible OCR。"""

    return [span for block in (*page.texts, *page.preserved_texts) for span in block.spans]


def _fsync_file(path: Path) -> None:
    """在原子 replace 前把完整临时 PDF 刷入磁盘。"""

    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _file_sha256(path: Path) -> str:
    """流式计算文件 hash, 避免把大 PDF 整体读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pdf_structure_signature(path: Path) -> _PdfStructureSignature:
    """读取页面边界与递归 Image XObject 内容, 验证图片没有被改写。"""

    page_signatures: list[tuple[tuple[float, ...], int]] = []
    image_signatures: list[tuple[int, tuple[str, ...], int, int, int, str]] = []
    with pikepdf.Pdf.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            media_box = tuple(float(value) for value in page.MediaBox)
            rotation = int(page.get("/Rotate", 0))
            page_signatures.append((media_box, rotation))
            resources = page.get("/Resources")
            _collect_image_signatures(
                resources,
                page_index=page_index,
                path=(),
                output=image_signatures,
                seen=set(),
            )
    return _PdfStructureSignature(
        pages=tuple(page_signatures),
        images=tuple(sorted(image_signatures)),
    )


def _collect_image_signatures(
    resources: pikepdf.Object | None,
    *,
    page_index: int,
    path: tuple[str, ...],
    output: list[tuple[int, tuple[str, ...], int, int, int, str]],
    seen: set[tuple[int, int]],
) -> None:
    """递归遍历 page/Form resources, 并对每个 Image XObject 记录 decoded hash。"""

    if resources is None:
        return
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return
    for raw_name, stream in xobjects.items():
        name = str(raw_name)
        object_id = tuple(stream.objgen)
        if object_id != (0, 0) and object_id in seen:
            continue
        if object_id != (0, 0):
            seen.add(object_id)
        subtype = stream.get("/Subtype")
        current_path = (*path, name)
        if subtype == "/Image":
            try:
                payload = stream.read_bytes()
            except pikepdf.PdfError:
                payload = stream.read_raw_bytes()
            output.append(
                (
                    page_index,
                    current_path,
                    int(stream.get("/Width", 0)),
                    int(stream.get("/Height", 0)),
                    int(stream.get("/BitsPerComponent", 0)),
                    hashlib.sha256(payload).hexdigest(),
                )
            )
        elif subtype == "/Form":
            _collect_image_signatures(
                stream.get("/Resources"),
                page_index=page_index,
                path=current_path,
                output=output,
                seen=seen,
            )
