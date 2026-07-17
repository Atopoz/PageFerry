"""
PDF 文本处理 Part4: 段落渲染
"""

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import pikepdf
from loguru import logger

from vendor.pdfminerex.utils import MATRIX_IDENTITY, inverse_matrix, mult_matrix

from .entities import BBox, PageInfo, PreservedBlock, TextBlock, TextSpan
from .extractor import ParagraphExtractor
from .font_manager import (
    PDF_FONT_PREPARE_FAILED,
    FontLanguage,
    FontSubsetData,
    PdfFontResourceError,
    build_font_subsets_for_texts,
    collect_unique_span_texts,
    infer_text_language,
    parse_target_language,
    segment_text,
)
from .table_renderer import TableRenderConfig, TableTextRenderer, is_table_text_span

PDFMatrix = tuple[float, float, float, float, float, float]


class FillRenderMode(str, Enum):
    """控制文字绘制模式。"""

    TRANSLATION_ONLY = "translation_only"
    BILINGUAL_STACK = "bilingual_stack"


@dataclass(frozen=True)
class FillOptions:
    """控制译文绘制行为的配置。"""

    text_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bilingual_source_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bilingual_target_color: tuple[float, float, float] = (0.1, 0.35, 0.8)
    render_mode: FillRenderMode = FillRenderMode.TRANSLATION_ONLY
    line_height_factor: float = 1.1
    height_tolerance: float = 0.25
    min_translation_font_size: float = 6.0
    allow_horizontal_scaling: bool = False
    min_horizontal_scaling: float = 80.0
    table_min_translation_font_size: float = 4.0
    min_bilingual_font_size: float = 4.0
    bilingual_paragraph_gap_factor: float = 0.35
    reuse_block_dominant_color: bool = False


@dataclass(frozen=True)
class BilingualLayout:
    """描述双语堆叠的排版参数。"""

    font_size: float
    line_height: float
    original_baseline: float
    translation_baseline: float


class ParagraphRenderer:
    """基于段落提取结果在原始 PDF 上写入译文文本。"""

    _CJK_LINE_HEIGHT_FACTOR = 1.32
    _WRAP_WORD_PATTERN = re.compile(
        r"[A-Za-z0-9\u00C0-\u024F\u0400-\u052F]+"
        r"(?:[._'’/-][A-Za-z0-9\u00C0-\u024F\u0400-\u052F]+)*"
    )
    _WRAP_CLOSING_PUNCTUATION = frozenset(
        ",.;:!?%)]}，。；：！？、）】》」』"
    )
    _CJK_LANGUAGES = frozenset(
        {
            FontLanguage.CHINESE,
            FontLanguage.JAPANESE,
            FontLanguage.KOREAN,
        }
    )

    def __init__(
        self,
        pdf_path: str | Path,
        *,
        font_directory: str | Path | None = None,
    ) -> None:
        """绑定只读源 PDF，并接收 app-data 中的 runtime 字体目录。"""

        self._pdf_path = Path(pdf_path)
        self._font_directory = (
            Path(font_directory).expanduser().resolve()
            if font_directory is not None
            else None
        )
        if not self._pdf_path.exists():
            raise FileNotFoundError(f"PDF 文件不存在: {self._pdf_path}")

    @staticmethod
    def _collect_unique_bold_block_texts(
        pages: Sequence[PageInfo],
        *,
        include_source: bool = False,
    ) -> list[str]:
        """仅收集粗体 block 对应的去重文本，减少粗体字体子集大小。"""
        unique_texts: list[str] = []
        seen: set[str] = set()
        for page in pages:
            for block in page.texts:
                if not getattr(block, "is_bold", False):
                    continue
                if getattr(block, "translation_mode", "span") == "block":
                    candidates: list[str] = []
                    translated = (block.translated_text or "").strip()
                    source = (block.text or "").strip()
                    if include_source and source:
                        candidates.append(source)
                    if translated:
                        candidates.append(translated)
                    elif source:
                        candidates.append(source)
                else:
                    candidates = []
                    for span in block.spans:
                        translated = (span.translated_text or "").strip()
                        source = (span.text or "").strip()
                        if include_source and source:
                            candidates.append(source)
                        if translated:
                            candidates.append(translated)
                        elif source:
                            candidates.append(source)
                for text in candidates:
                    normalized = text.replace("\r", "").replace("\n", "")
                    if not normalized or not normalized.strip():
                        continue
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    unique_texts.append(normalized)
        return unique_texts

    def apply(
        self,
        output_path: str | Path,
        pages: Sequence[PageInfo] | None = None,
        *,
        options: FillOptions | None = None,
        target_language: str | None = None,
    ) -> None:
        """根据提取的文本片段在原 PDF 上绘制译文后输出新的 PDF。

        Args:
            output_path: 生成的新 PDF 路径。
            pages: 外部传入的提取结果；如不提供，将内部调用 `ParagraphExtractor`。
            options: 覆盖配置。
            target_language: 目标语言（如 "中文"、"日文"、"韩文"），用于强制使用单一字体。
        """
        opts = options or FillOptions()
        self._validate_color(opts.text_color, "text_color")
        self._validate_color(opts.bilingual_source_color, "bilingual_source_color")
        self._validate_color(opts.bilingual_target_color, "bilingual_target_color")
        if opts.line_height_factor <= 0.0:
            raise ValueError("line_height_factor 必须大于 0")
        if opts.height_tolerance < 0.0:
            raise ValueError("height_tolerance 不能为负数")
        if opts.min_translation_font_size <= 0.0:
            raise ValueError("min_translation_font_size 必须大于 0")
        if not 0.0 < opts.min_horizontal_scaling <= 100.0:
            raise ValueError("min_horizontal_scaling 必须位于 (0, 100] 区间")
        if opts.table_min_translation_font_size <= 0.0:
            raise ValueError("table_min_translation_font_size 必须大于 0")
        if opts.min_bilingual_font_size <= 0.0:
            raise ValueError("min_bilingual_font_size 必须大于 0")
        if opts.bilingual_paragraph_gap_factor < 0.0:
            raise ValueError("bilingual_paragraph_gap_factor 不能为负数")

        page_info_list = list(pages) if pages is not None else None
        if page_info_list is None:
            extractor = ParagraphExtractor(str(self._pdf_path))
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                page_info_list = asyncio.run(extractor.extract_page_info_with_layout())
            else:
                raise RuntimeError("apply() 在已有事件循环中必须显式传入 pages")
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        forced_target_language = parse_target_language(target_language) if target_language else None
        pending_translation_pages = [
            self._page_has_pending_translation(page_info) for page_info in page_info_list
        ]

        with pikepdf.Pdf.open(str(self._pdf_path)) as pdf:
            if len(pdf.pages) != len(page_info_list):
                raise ValueError("提取页数与 PDF 页数不符")
            if not any(pending_translation_pages):
                pdf.save(str(output))
                return
            font_subsets: dict[FontLanguage, FontSubsetData] = {}
            bold_font_subsets: dict[FontLanguage, FontSubsetData] = {}
            font_resource_map: dict[FontLanguage, tuple[pikepdf.Name, pikepdf.Object]] = {}
            bold_font_resource_map: dict[FontLanguage, tuple[pikepdf.Name, pikepdf.Object]] = {}
            try:
                include_source = opts.render_mode == FillRenderMode.BILINGUAL_STACK
                unique_texts = collect_unique_span_texts(
                    page_info_list,
                    include_source=include_source,
                    include_preserved=True,
                )
                if unique_texts:
                    # 1. 自动识别并构建所有语种的字体子集
                    font_subsets = build_font_subsets_for_texts(
                        unique_texts,
                        font_directory=self._font_directory,
                        target_language=forced_target_language,
                    )
                    # 2. 按 block 粗体众数单独构建粗体子集（math 自动回退到 regular）
                    bold_texts = self._collect_unique_bold_block_texts(
                        page_info_list,
                        include_source=include_source,
                    )
                    if bold_texts:
                        bold_font_subsets = build_font_subsets_for_texts(
                            bold_texts,
                            font_directory=self._font_directory,
                            target_language=forced_target_language,
                            bold=True,
                        )

                    used_font_resource_names = self._collect_page_font_resource_names(pdf)

                    # 3. 注册常规字体，并使用不与原 PDF 冲突的语种感知资源名
                    for language, subset in font_subsets.items():
                        resource_name = self._allocate_font_resource_name(
                            used_font_resource_names,
                            f"/FTR_{language.value.upper()}",
                        )
                        _, font_ref = self._register_font(pdf, subset, resource_name=resource_name)
                        font_resource_map[language] = (resource_name, font_ref)
                    # 4. 注册粗体字体资源（如 /FTRB_ZH, /FTRB_EN）
                    for language, subset in bold_font_subsets.items():
                        resource_name = self._allocate_font_resource_name(
                            used_font_resource_names,
                            f"/FTRB_{language.value.upper()}",
                        )
                        _, font_ref = self._register_font(pdf, subset, resource_name=resource_name)
                        bold_font_resource_map[language] = (resource_name, font_ref)
            except PdfFontResourceError:
                logger.error("PDF runtime 字体资源不可用，已中止输出")
                raise
            except Exception as exc:
                logger.exception("PDF 字体子集构建或注册失败，已中止输出")
                raise PdfFontResourceError(PDF_FONT_PREPARE_FAILED) from exc

            if not font_subsets or not font_resource_map:
                raise PdfFontResourceError(PDF_FONT_PREPARE_FAILED)

            # 字体准备完成后才删除原文字层，任何准备失败都不会产生空白结果。
            self._remove_existing_text(pdf, page_info_list)

            font_resource_names: dict[FontLanguage, pikepdf.Name] = {
                language: resource_name for language, (resource_name, _) in font_resource_map.items()
            }
            bold_font_resource_names: dict[FontLanguage, pikepdf.Name] = {
                language: resource_name for language, (resource_name, _) in bold_font_resource_map.items()
            }

            for index, page in enumerate(pdf.pages):
                page_info = page_info_list[index]
                if not pending_translation_pages[index]:
                    # 该页没有译文时保持原 content stream，不重复追加 preserved text。
                    continue
                for resource_name, resource_ref in font_resource_map.values():
                    self._ensure_font_resource(page, resource_name, resource_ref)
                for resource_name, resource_ref in bold_font_resource_map.values():
                    self._ensure_font_resource(page, resource_name, resource_ref)

                # 核心修复：分离保留文本和翻译文本到不同的 Stream
                # 1. 先追加保留文本 Stream
                preserved_stream = self._build_preserved_stream(
                    pdf,
                    page,
                    page_info,
                    font_resource_names,
                    font_subsets,
                    opts,
                )
                if preserved_stream:
                    self._append_stream(pdf, page, preserved_stream)

                # 2. 再追加翻译文本 Stream
                translation_stream = self._build_translation_stream(
                    pdf,
                    page_info,
                    opts,
                    font_resource_names,
                    font_subsets,
                    page=page,
                    bold_font_resource_names=bold_font_resource_names,
                    bold_font_subsets=bold_font_subsets,
                )
                if pending_translation_pages[index] and translation_stream is None:
                    raise RuntimeError(
                        f"PDF 第 {index + 1} 页译文渲染失败：未生成 translation stream"
                    )
                if translation_stream:
                    self._append_stream(pdf, page, translation_stream)
            pdf.save(str(output))

    @staticmethod
    def _page_has_pending_translation(page_info: PageInfo) -> bool:
        """判断页面是否存在待写入的非空译文。"""

        for block in page_info.texts:
            if getattr(block, "translation_mode", "span") == "block":
                if (block.translated_text or "").strip():
                    return True
                continue
            if any((span.translated_text or "").strip() for span in block.spans):
                return True
        return False

    def _build_preserved_stream(
        self,
        pdf: pikepdf.Pdf,
        page: pikepdf.Page,
        page_info: PageInfo,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        opts: FillOptions,
    ) -> pikepdf.Stream | None:
        """构建保留文本的独立 Stream"""
        commands: list[str] = []

        preserved_blocks: Sequence[PreservedBlock] = getattr(page_info, "preserved_texts", []) or []
        preserved_ops_by_path = self._collect_preserved_ops_by_path(preserved_blocks, page_info.texts)
        if preserved_ops_by_path is None:
            if getattr(page_info, "preserve_original_xobject", False):
                return self._build_preserved_fallback_stream(
                    pdf,
                    page,
                    preserved_blocks,
                    font_resource_names,
                    font_subsets,
                    opts,
                    skip_small_ops=True,
                )
            return self._build_preserved_fallback_stream(
                pdf,
                page,
                preserved_blocks,
                font_resource_names,
                font_subsets,
                opts,
            )
        page_ops = preserved_ops_by_path.pop((), [])
        for op in page_ops:
            commands.append(op)
        for xobject_path, ops in preserved_ops_by_path.items():
            self._append_preserved_ops_to_xobject(pdf, page, xobject_path, ops)

        if not commands:
            return None

        # 使用独立的隔离栈，确保不受其他绘制指令影响
        prefix = " q\n"
        suffix = "\nQ "
        full_command = prefix + "\n".join(commands) + suffix
        encoded = full_command.encode("latin-1")
        return pikepdf.Stream(pdf, encoded)

    def _collect_preserved_ops_by_path(
        self,
        preserved_blocks: Sequence[PreservedBlock],
        translated_blocks: Sequence[TextBlock] | None = None,
    ) -> dict[tuple[str, ...], list[str]] | None:
        """收集可安全回放的原始指令流，返回 None 表示回放不安全。"""
        ops_by_path: dict[tuple[str, ...], list[bytes]] = {}
        for block in preserved_blocks:
            for span in block.spans:
                if not hasattr(span, "ops") or not span.ops:
                    continue
                paths = getattr(span, "ops_xobject_paths", [])
                if len(paths) != len(span.ops):
                    paths = [()] * len(span.ops)
                for op_bytes, path in zip(span.ops, paths):
                    # 原始指令流已包含 BT/ET 和 Relative CTM (q ... cm ... Q)
                    ops_by_path.setdefault(tuple(path), []).append(op_bytes)

        if not ops_by_path:
            return {}

        translated_ops: set[bytes] = set()
        for block in translated_blocks or []:
            for span in block.spans:
                if hasattr(span, "ops") and span.ops:
                    for op_bytes in span.ops:
                        translated_ops.add(op_bytes)
        if any(op_bytes in translated_ops for items in ops_by_path.values() for op_bytes in items):
            logger.warning("Preserved ops share text block with translated content; fallback to safe rendering")
            return None

        normalized: dict[tuple[str, ...], list[bytes]] = {}
        for path, items in ops_by_path.items():
            unique_ops: list[bytes] = []
            seen: set[bytes] = set()
            for op_bytes in items:
                if op_bytes in seen:
                    continue
                seen.add(op_bytes)
                unique_ops.append(op_bytes)
            if not unique_ops:
                continue
            op_sizes = [len(item) for item in unique_ops]
            max_size = max(op_sizes)
            total_ops = len(op_sizes)
            unique_hashes = {hash(item) for item in unique_ops}
            unique_ratio = len(unique_hashes) / total_ops if total_ops else 1.0

            # 判断是否为整页级别的指令流回放
            if max_size >= 8000:
                logger.warning("Preserved ops too large, fallback to safe rendering", extra={"max_size": max_size})
                return None
            if max_size >= 4000 and unique_ratio <= 0.3 and total_ops >= 3:
                logger.warning(
                    "Preserved ops highly duplicated, fallback to safe rendering",
                    extra={"max_size": max_size, "unique_ratio": unique_ratio, "total_ops": total_ops},
                )
                return None
            if len(unique_hashes) == 1 and total_ops >= 3 and max_size >= 2000:
                logger.warning(
                    "Preserved ops identical across spans, fallback to safe rendering",
                    extra={"max_size": max_size, "total_ops": total_ops},
                )
                return None
            normalized[path] = unique_ops

        return {path: [item.decode("latin-1") for item in items] for path, items in normalized.items()}

    def _collect_preserved_ops(
        self,
        preserved_blocks: Sequence[PreservedBlock],
        translated_blocks: Sequence[TextBlock] | None = None,
    ) -> list[str] | None:
        """兼容旧接口，仅返回页面级别的保留指令。"""
        ops_by_path = self._collect_preserved_ops_by_path(preserved_blocks, translated_blocks)
        if ops_by_path is None:
            return None
        return ops_by_path.get((), [])

    def _append_preserved_ops_to_xobject(
        self,
        pdf: pikepdf.Pdf,
        page: pikepdf.Page,
        xobject_path: tuple[str, ...],
        ops: list[str],
    ) -> None:
        if not ops or not xobject_path:
            return
        stream = self._resolve_xobject_stream(pdf, page, xobject_path)
        if stream is None:
            logger.warning(
                "Failed to resolve xobject for preserved ops",
                extra={"xobject_path": "/".join(xobject_path)},
            )
            return
        prefix = " q\n"
        suffix = "\nQ "
        full_command = prefix + "\n".join(ops) + suffix
        encoded = full_command.encode("latin-1")
        try:
            existing = stream.read_bytes()
        except Exception:
            existing = b""
        residual_ctm = self._compute_residual_ctm(stream)
        wrapped = self._wrap_bytes_with_inverse_ctm(encoded, residual_ctm)
        stream.write(existing + b"\n" + wrapped)

    @staticmethod
    def _resolve_xobject_stream(
        pdf: pikepdf.Pdf,
        page: pikepdf.Page,
        xobject_path: tuple[str, ...],
    ) -> pikepdf.Stream | None:
        if not xobject_path:
            return None
        resources = page.get("/Resources")
        current_stream: pikepdf.Stream | None = None
        for name in xobject_path:
            if resources is None:
                return None
            xobj_dict = resources.get(pikepdf.Name("/XObject")) if isinstance(resources, pikepdf.Dictionary) else None
            if not xobj_dict:
                return None
            key = pikepdf.Name(f"/{name.lstrip('/')}")
            obj = xobj_dict.get(key)
            if obj is None:
                return None
            try:
                stream = pdf.get_object(obj)
            except Exception:
                stream = obj
            if not isinstance(stream, pikepdf.Stream):
                return None
            current_stream = stream
            resources = stream.get("/Resources")
        return current_stream

    def _build_preserved_fallback_stream(
        self,
        pdf: pikepdf.Pdf,
        page: pikepdf.Page,
        preserved_blocks: Sequence[PreservedBlock],
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        opts: FillOptions,
        *,
        skip_small_ops: bool = False,
    ) -> pikepdf.Stream | None:
        if not preserved_blocks:
            return None
        if not font_resource_names or not font_subsets:
            return None
        color_command = self._build_rgb_color_command(opts.text_color)
        commands: list[str] = []
        for block in preserved_blocks:
            for span in block.spans:
                if skip_small_ops and span.ops:
                    max_len = max(len(item) for item in span.ops if item is not None)
                    if max_len <= 2000:
                        continue
                text = (span.text or "").strip()
                if not text or "(cid:" in text:
                    continue
                segments = segment_text(text)
                if not segments:
                    continue
                font_size = self._determine_font_size(span)
                if font_size <= 0:
                    continue
                command = self._build_single_line_command(
                    segments,
                    font_resource_names,
                    font_subsets,
                    font_size,
                    color_command,
                    span.bbox.x1,
                    span.bbox.y1,
                )
                if command:
                    commands.append(command)
        if not commands:
            return None
        prefix = " q\n"
        suffix = "\nQ "
        full_command = prefix + "\n".join(commands) + suffix
        encoded = full_command.encode("latin-1")
        stream = pikepdf.Stream(pdf, encoded)
        return self._normalize_stream_for_page_extraction_ctm(pdf, page, stream)

    def _build_translation_stream(
        self,
        pdf: pikepdf.Pdf,
        page_info: PageInfo,
        opts: FillOptions,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        *,
        page: pikepdf.Page | None = None,
        bold_font_resource_names: dict[FontLanguage, pikepdf.Name] | None = None,
        bold_font_subsets: dict[FontLanguage, FontSubsetData] | None = None,
    ) -> pikepdf.Stream | None:
        """构建翻译文本的独立 Stream"""
        commands: list[str] = []

        if font_resource_names and font_subsets:
            color_command = self._build_text_style_command(opts.text_color)
            source_color_command = color_command
            target_color_command = color_command
            if opts.render_mode == FillRenderMode.BILINGUAL_STACK:
                source_color_command = self._build_text_style_command(opts.bilingual_source_color)
                target_color_command = self._build_text_style_command(opts.bilingual_target_color)
            page_base_font_size = self._compute_page_base_font_size(page_info)
            page_label_font_sizes = self._compute_page_label_base_font_sizes(page_info)
            for block in page_info.texts:
                use_bold_font = bool(getattr(block, "is_bold", False))
                active_font_resource_names = self._resolve_effective_font_resources(
                    font_resource_names,
                    bold_font_resource_names,
                    use_bold_font,
                )
                active_font_subsets = self._resolve_effective_font_subsets(
                    font_subsets,
                    bold_font_subsets,
                    use_bold_font,
                )
                block_color_command = color_command
                if opts.render_mode == FillRenderMode.TRANSLATION_ONLY and opts.reuse_block_dominant_color:
                    block_color_command = self._resolve_block_color_command(block, color_command)
                if getattr(block, "translation_mode", "span") == "block":
                    block_commands = self._build_block_translation_commands(
                        block,
                        active_font_resource_names,
                        active_font_subsets,
                        block_color_command,
                        source_color_command,
                        target_color_command,
                        opts,
                        page_base_font_size,
                        page_label_font_sizes,
                    )
                    if (block.translated_text or "").strip() and not block_commands:
                        raise RuntimeError(
                            f"PDF 译文渲染失败：block {block.block_id} 未生成绘制指令"
                        )
                    commands.extend(block_commands)
                    continue
                spans = getattr(block, "spans", None)
                if not spans:
                    continue
                grouped_table_span_ids: set[int] = set()
                if opts.render_mode == FillRenderMode.TRANSLATION_ONLY:
                    table_commands, grouped_table_span_ids = self._build_table_cell_group_commands(
                        block,
                        spans,
                        active_font_resource_names,
                        active_font_subsets,
                        block_color_command,
                        opts,
                    )
                    commands.extend(table_commands)
                for span in spans:
                    if id(span) in grouped_table_span_ids:
                        continue
                    if opts.render_mode == FillRenderMode.BILINGUAL_STACK:
                        span_commands = self._build_bilingual_commands(
                            span,
                            active_font_resource_names,
                            active_font_subsets,
                            source_color_command,
                            target_color_command,
                            opts,
                        )
                    else:
                        span_commands = self._build_translation_only_commands(
                            span,
                            active_font_resource_names,
                            active_font_subsets,
                            block_color_command,
                            opts,
                            table_cell_fit=self._is_table_text_span(block, span),
                        )
                    if (span.translated_text or "").strip() and not span_commands:
                        raise RuntimeError(
                            f"PDF 译文渲染失败：span {span.span_id} 未生成绘制指令"
                        )
                    commands.extend(span_commands)

        if not commands:
            return None

        # 使用独立的隔离栈
        prefix = " q\n"
        suffix = "\nQ "
        full_command = prefix + "\n".join(commands) + suffix
        encoded = full_command.encode("latin-1")
        stream = pikepdf.Stream(pdf, encoded)
        if page is None:
            return stream
        return self._normalize_stream_for_page_extraction_ctm(pdf, page, stream)

    @staticmethod
    def _build_rgb_color_command(color: tuple[float, float, float]) -> str:
        """将 RGB 三元组转换为 PDF 颜色指令。"""
        r, g, b = color
        return f"{r:.4f} {g:.4f} {b:.4f} rg"

    @staticmethod
    def _build_stroke_color_command(color: tuple[float, float, float]) -> str:
        """将 RGB 三元组转换为 PDF 描边颜色指令。"""
        r, g, b = color
        return f"{r:.4f} {g:.4f} {b:.4f} RG"

    def _build_text_style_command(
        self,
        fill_color: tuple[float, float, float],
        *,
        stroke_color: tuple[float, float, float] | None = None,
        render_mode: int = 0,
    ) -> str:
        """构建文字绘制样式指令，包含填充色、描边色和渲染模式。"""
        commands = [self._build_rgb_color_command(fill_color)]
        if self._is_valid_color_tuple(stroke_color):
            commands.append(self._build_stroke_color_command(stroke_color))
        normalized_render_mode = self._normalize_text_render_mode(render_mode)
        if normalized_render_mode in {1, 2}:
            commands.append(f"{normalized_render_mode} Tr")
        return " ".join(commands)

    def _resolve_block_color_command(self, block: TextBlock, fallback_command: str) -> str:
        """根据 block 主样式解析最终可见颜色，译文始终使用普通填充绘制。"""
        fill_color = getattr(block, "dominant_color", None)
        stroke_color = getattr(block, "dominant_stroke_color", None)
        render_mode = getattr(block, "dominant_text_render_mode", 0)
        visible_color = self._resolve_visible_text_color(
            fill_color,
            stroke_color,
            render_mode,
        )
        if not self._is_valid_color_tuple(visible_color):
            return fallback_command
        return self._build_rgb_color_command(visible_color)

    def _resolve_visible_text_color(
        self,
        fill_color: object,
        stroke_color: object,
        render_mode: object,
    ) -> tuple[float, float, float] | None:
        """从原文填充/描边样式中推导译文应使用的可见填充色。"""
        has_fill = self._is_valid_color_tuple(fill_color)
        has_stroke = self._is_valid_color_tuple(stroke_color)
        mode = self._normalize_text_render_mode(render_mode)

        if mode == 1:
            if has_stroke:
                return stroke_color
            return fill_color if has_fill else None

        if mode == 2:
            if has_fill and has_stroke and self._should_use_stroke_as_visible_fill(
                fill_color,
                stroke_color,
            ):
                return stroke_color
            if has_fill:
                return fill_color
            return stroke_color if has_stroke else None

        return fill_color if has_fill else None

    @staticmethod
    def _should_use_stroke_as_visible_fill(
        fill_color: tuple[float, float, float],
        stroke_color: tuple[float, float, float],
    ) -> bool:
        """当填充近白且描边更深时，使用描边色避免白字消失。"""
        fill_luminance = ParagraphRenderer._relative_luminance(fill_color)
        stroke_luminance = ParagraphRenderer._relative_luminance(stroke_color)
        fill_is_near_white = fill_luminance >= 0.9 and all(component >= 0.86 for component in fill_color)
        stroke_is_visible = stroke_luminance <= fill_luminance - 0.2
        return fill_is_near_white and stroke_is_visible

    @staticmethod
    def _relative_luminance(color: tuple[float, float, float]) -> float:
        r, g, b = color
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    @staticmethod
    def _is_valid_color_tuple(color: object) -> bool:
        if not isinstance(color, tuple):
            return False
        if len(color) != 3:
            return False
        return all(isinstance(component, (int, float)) and 0.0 <= float(component) <= 1.0 for component in color)

    @staticmethod
    def _normalize_text_render_mode(render_mode: object) -> int:
        try:
            normalized = int(render_mode)
        except (TypeError, ValueError):
            return 0
        normalized %= 4
        return normalized if normalized in {1, 2} else 0

    @staticmethod
    def _resolve_effective_font_resources(
        regular_resources: dict[FontLanguage, pikepdf.Name],
        bold_resources: dict[FontLanguage, pikepdf.Name] | None,
        use_bold_font: bool,
    ) -> dict[FontLanguage, pikepdf.Name]:
        """根据块级字重决策合并可用字体资源。"""
        if not use_bold_font or not bold_resources:
            return regular_resources
        merged = dict(regular_resources)
        merged.update(bold_resources)
        return merged

    @staticmethod
    def _resolve_effective_font_subsets(
        regular_subsets: dict[FontLanguage, FontSubsetData],
        bold_subsets: dict[FontLanguage, FontSubsetData] | None,
        use_bold_font: bool,
    ) -> dict[FontLanguage, FontSubsetData]:
        """根据块级字重决策合并可用字体子集。"""
        if not use_bold_font or not bold_subsets:
            return regular_subsets
        merged = dict(regular_subsets)
        merged.update(bold_subsets)
        return merged

    def _build_block_translation_commands(
        self,
        block: TextBlock,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        source_color_command: str,
        target_color_command: str,
        opts: FillOptions,
        page_base_font_size: float | None,
        page_label_font_sizes: dict[str, float],
    ) -> list[str]:
        if not block.translated_text:
            return []

        text = block.translated_text.strip()
        if not text:
            return []

        bbox = block.bbox
        max_width = max(bbox.x2 - bbox.x1, 0.0)
        if max_width <= 0.0:
            return []

        base_size = self._determine_block_font_size(
            block,
            page_base_font_size,
            page_label_font_sizes,
        )
        if base_size <= 0.0:
            return []

        if opts.render_mode == FillRenderMode.BILINGUAL_STACK and block.text:
            if self._is_nearly_identical(block.text, text):
                return self._build_block_single_language_commands(
                    block.text,
                    bbox,
                    base_size,
                    font_resource_names,
                    font_subsets,
                    source_color_command,
                    opts,
                )
            return self._build_block_bilingual_commands(
                block.text,
                text,
                bbox,
                base_size,
                font_resource_names,
                font_subsets,
                source_color_command,
                target_color_command,
                opts,
            )

        return self._build_block_single_language_commands(
            text,
            bbox,
            base_size,
            font_resource_names,
            font_subsets,
            color_command,
            opts,
        )

    def _build_block_single_language_commands(
        self,
        text: str,
        bbox: "BBox",
        base_font_size: float,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        opts: FillOptions,
    ) -> list[str]:
        line_height_factor = self._resolve_line_height_factor(text, opts.line_height_factor)
        lines, font_size = self._fit_text_to_bbox(
            text,
            bbox,
            base_font_size,
            min_font_size=4.0,
            font_subsets=font_subsets,
            line_height_factor=line_height_factor,
        )
        if not lines or font_size <= 0:
            return []
        block_commands = self._render_lines(
            lines,
            bbox,
            font_size,
            font_resource_names,
            font_subsets,
            color_command,
            line_height_factor,
        )
        return block_commands

    def _build_block_bilingual_commands(
        self,
        source_text: str,
        target_text: str,
        bbox: "BBox",
        base_font_size: float,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        source_color_command: str,
        target_color_command: str,
        opts: FillOptions,
    ) -> list[str]:
        line_height_factor = self._resolve_line_height_factor(
            "\n".join(part for part in (source_text, target_text) if part.strip()),
            opts.line_height_factor,
        )
        result = self._fit_bilingual_text_to_bbox(
            source_text,
            target_text,
            bbox,
            base_font_size,
            min_font_size=opts.min_bilingual_font_size,
            font_subsets=font_subsets,
            line_height_factor=line_height_factor,
            paragraph_gap_factor=opts.bilingual_paragraph_gap_factor,
        )
        if result is None:
            return []
        source_lines, target_lines, font_size = result
        if not source_lines and not target_lines:
            return []
        return self._render_bilingual_lines(
            source_lines,
            target_lines,
            bbox,
            font_size,
            font_resource_names,
            font_subsets,
            source_color_command,
            target_color_command,
            line_height_factor,
            opts.bilingual_paragraph_gap_factor,
        )

    def _fit_text_to_bbox(
        self,
        text: str,
        bbox: "BBox",
        base_font_size: float,
        *,
        min_font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
        line_height_factor: float,
    ) -> tuple[list[str], float]:
        if base_font_size <= 0:
            return [], 0.0
        bbox_height = max(bbox.y2 - bbox.y1, 0.0)
        min_size = min_font_size
        max_size = base_font_size
        best_lines: list[str] = []
        best_size = min_size
        if bbox_height <= 0:
            lines = self._wrap_text_to_lines(text, bbox, base_font_size, font_subsets)
            return lines, base_font_size

        for _ in range(10):
            size = (min_size + max_size) / 2
            lines = self._wrap_text_to_lines(text, bbox, size, font_subsets)
            total_height = len(lines) * size * line_height_factor
            if total_height <= bbox_height + 0.1 and lines:
                best_lines = lines
                best_size = size
                min_size = size
            else:
                max_size = size
        if not best_lines:
            best_lines = self._wrap_text_to_lines(text, bbox, min_font_size, font_subsets)
            best_size = min_font_size
        return best_lines, best_size

    def _fit_bilingual_text_to_bbox(
        self,
        source_text: str,
        target_text: str,
        bbox: "BBox",
        base_font_size: float,
        *,
        min_font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
        line_height_factor: float,
        paragraph_gap_factor: float,
    ) -> tuple[list[str], list[str], float] | None:
        if base_font_size <= 0:
            return None
        bbox_height = max(bbox.y2 - bbox.y1, 0.0)
        min_size = min_font_size
        max_size = base_font_size
        best_source: list[str] = []
        best_target: list[str] = []
        best_size = min_size
        if bbox_height <= 0:
            source_lines = self._wrap_text_to_lines(source_text, bbox, base_font_size, font_subsets)
            target_lines = self._wrap_text_to_lines(target_text, bbox, base_font_size, font_subsets)
            return source_lines, target_lines, base_font_size

        for _ in range(10):
            size = (min_size + max_size) / 2
            source_lines = self._wrap_text_to_lines(source_text, bbox, size, font_subsets)
            target_lines = self._wrap_text_to_lines(target_text, bbox, size, font_subsets)
            total_lines = len(source_lines) + len(target_lines)
            gap = size * line_height_factor * paragraph_gap_factor if source_lines and target_lines else 0.0
            total_height = total_lines * size * line_height_factor + gap
            if total_height <= bbox_height + 0.1 and total_lines > 0:
                best_source = source_lines
                best_target = target_lines
                best_size = size
                min_size = size
            else:
                max_size = size

        if not best_source and not best_target:
            best_source = self._wrap_text_to_lines(source_text, bbox, min_font_size, font_subsets)
            best_target = self._wrap_text_to_lines(target_text, bbox, min_font_size, font_subsets)
            best_size = min_font_size
        return best_source, best_target, best_size

    def _render_lines(
        self,
        lines: Sequence[str],
        bbox: "BBox",
        font_size: float,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        line_height_factor: float,
    ) -> list[str]:
        if not lines or font_size <= 0:
            return []
        line_height = font_size * line_height_factor
        start_y = bbox.y2 - font_size
        min_y = start_y - (len(lines) - 1) * line_height
        if min_y < bbox.y1:
            start_y += bbox.y1 - min_y
        commands: list[str] = []
        for index, line in enumerate(lines):
            line_text = line.strip()
            y = start_y - index * line_height
            if not line_text:
                continue
            segments = segment_text(line_text)
            command = self._build_single_line_command(
                segments,
                font_resource_names,
                font_subsets,
                font_size,
                color_command,
                bbox.x1,
                y,
            )
            if command:
                commands.append(command)
        return commands

    def _render_bilingual_lines(
        self,
        source_lines: Sequence[str],
        target_lines: Sequence[str],
        bbox: "BBox",
        font_size: float,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        source_color_command: str,
        target_color_command: str,
        line_height_factor: float,
        paragraph_gap_factor: float,
    ) -> list[str]:
        total_lines = len(source_lines) + len(target_lines)
        if total_lines <= 0 or font_size <= 0:
            return []
        line_height = font_size * line_height_factor
        gap = line_height * paragraph_gap_factor if source_lines and target_lines else 0.0
        start_y = bbox.y2 - font_size
        min_y = start_y - (total_lines - 1) * line_height - gap
        if min_y < bbox.y1:
            start_y += bbox.y1 - min_y
        total_height = total_lines * line_height + gap
        bbox_height = max(bbox.y2 - bbox.y1, 0.0)
        if bbox_height > total_height:
            start_y -= (bbox_height - total_height) / 2
        commands: list[str] = []
        y = start_y
        for line in source_lines:
            line_text = line.strip()
            if not line_text:
                continue
            segments = segment_text(line_text)
            command = self._build_single_line_command(
                segments,
                font_resource_names,
                font_subsets,
                font_size,
                source_color_command,
                bbox.x1,
                y,
            )
            if command:
                commands.append(command)
            y -= line_height

        if source_lines and target_lines:
            y -= gap

        for line in target_lines:
            line_text = line.strip()
            if not line_text:
                continue
            segments = segment_text(line_text)
            command = self._build_single_line_command(
                segments,
                font_resource_names,
                font_subsets,
                font_size,
                target_color_command,
                bbox.x1,
                y,
            )
            if command:
                commands.append(command)
            y -= line_height

        return commands

    def _wrap_text_to_lines(
        self,
        text: str,
        bbox: "BBox",
        font_size: float,
        font_subsets: dict[FontLanguage, FontSubsetData],
    ) -> list[str]:
        max_width = max(bbox.x2 - bbox.x1, 0.0)
        if max_width <= 0.0:
            return []
        paragraphs = text.splitlines() or [text]
        lines: list[str] = []
        for paragraph in paragraphs:
            if not paragraph:
                lines.append("")
                continue
            tokens = self._split_text_for_wrap(paragraph)
            current_line = ""
            current_width = 0.0
            for token in tokens:
                if not token:
                    continue
                token_width = self._calculate_text_width_by_language(token, font_subsets, font_size)
                if token_width > max_width * 1.05 and len(token) > 1:
                    for char in token:
                        char_width = self._calculate_text_width_by_language(char, font_subsets, font_size)
                        if current_line and current_width + char_width > max_width * 1.01:
                            lines.append(current_line.rstrip())
                            current_line = char
                            current_width = char_width
                        else:
                            current_line += char
                            current_width += char_width
                    continue
                if current_line and current_width + token_width > max_width * 1.01:
                    lines.append(current_line.rstrip())
                    current_line = token.lstrip()
                    current_width = self._calculate_text_width_by_language(current_line, font_subsets, font_size)
                    continue
                current_line += token
                current_width += token_width
            if current_line:
                lines.append(current_line.rstrip())
        return lines

    @staticmethod
    def _split_text_for_wrap(text: str) -> list[str]:
        if not text:
            return []
        tokens: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char.isspace():
                if tokens:
                    tokens[-1] += char
                index += 1
                continue

            word_match = ParagraphRenderer._WRAP_WORD_PATTERN.match(text, index)
            if word_match:
                tokens.append(word_match.group(0))
                index = word_match.end()
                continue

            if char in ParagraphRenderer._WRAP_CLOSING_PUNCTUATION and tokens:
                tokens[-1] += char
            else:
                tokens.append(char)
            index += 1

        return tokens

    @classmethod
    def _resolve_line_height_factor(cls, text: str, base_factor: float) -> float:
        """按文本脚本动态调整 block 行高，避免 CJK 译文过于拥挤。"""
        sanitized = text.replace("\r", "").replace("\n", "").strip()
        if not sanitized:
            return base_factor
        language = infer_text_language(sanitized, mode="majority")
        if language in cls._CJK_LANGUAGES:
            return max(base_factor, cls._CJK_LINE_HEIGHT_FACTOR)
        return base_factor

    def _calculate_text_width_by_language(
        self,
        text: str,
        font_subsets: dict[FontLanguage, FontSubsetData],
        font_size: float,
    ) -> float:
        if not text:
            return 0.0
        segments = segment_text(text)
        return self._calculate_segments_width(segments, font_subsets, font_size)

    @staticmethod
    def _compute_page_base_font_size(page_info: PageInfo) -> float | None:
        sizes: list[float] = []
        for block in page_info.texts:
            if getattr(block, "translation_mode", "span") != "block":
                continue
            for span in block.spans:
                if span.font_size:
                    sizes.append(span.font_size)
        if not sizes:
            return None
        sizes.sort()
        mid = len(sizes) // 2
        if len(sizes) % 2 == 0:
            return (sizes[mid - 1] + sizes[mid]) / 2
        return sizes[mid]

    @staticmethod
    def _compute_page_label_base_font_sizes(page_info: PageInfo) -> dict[str, float]:
        label_sizes: dict[str, list[float]] = {}
        for block in page_info.texts:
            if getattr(block, "translation_mode", "span") != "block":
                continue
            label_key = ParagraphRenderer._resolve_block_label_key(block)
            if not label_key:
                continue
            for span in block.spans:
                if span.font_size:
                    label_sizes.setdefault(label_key, []).append(span.font_size)

        medians: dict[str, float] = {}
        for key, sizes in label_sizes.items():
            median_value = ParagraphRenderer._calculate_median_value(sizes)
            if median_value is not None:
                medians[key] = median_value
        return medians

    @staticmethod
    def _resolve_block_label_key(block: TextBlock) -> str | None:
        raw_label = block.layout_label or block.layout_type
        if not raw_label:
            return None
        normalized = raw_label.strip().lower()
        return normalized or None

    @staticmethod
    def _calculate_median_value(values: Sequence[float]) -> float | None:
        if not values:
            return None
        sorted_values = sorted(values)
        mid = len(sorted_values) // 2
        if len(sorted_values) % 2 == 0:
            return (sorted_values[mid - 1] + sorted_values[mid]) / 2
        return sorted_values[mid]

    @staticmethod
    def _determine_block_font_size(
        block: TextBlock,
        page_base_font_size: float | None,
        page_label_font_sizes: dict[str, float] | None = None,
    ) -> float:
        if block.adjusted_font_size is not None:
            return max(block.adjusted_font_size, 0.1)
        label_key = ParagraphRenderer._resolve_block_label_key(block)
        if page_label_font_sizes and label_key:
            label_base_font_size = page_label_font_sizes.get(label_key)
            if label_base_font_size is not None:
                return max(label_base_font_size, 0.1)
        if page_base_font_size is not None:
            return max(page_base_font_size, 0.1)
        font_sizes = [span.font_size for span in block.spans if span.font_size]
        median_font_size = ParagraphRenderer._calculate_median_value(font_sizes)
        if median_font_size is not None:
            return max(median_font_size, 0.1)
        return 12.0

    def _build_page_stream(
        self,
        pdf: pikepdf.Pdf,
        page_info: PageInfo,
        opts: FillOptions,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
    ) -> pikepdf.Stream | None:
        """已弃用：保留以兼容旧代码，但现在应使用 _build_preserved_stream 和 _build_translation_stream"""
        text_commands = self._collect_text_commands(
            page_info, font_resource_names, font_subsets, opts
        )
        if not text_commands:
            return None

        prefix = " q\n"
        suffix = "\nQ "
        full_command = prefix + "\n".join(text_commands) + suffix
        encoded = full_command.encode("latin-1")
        return pikepdf.Stream(pdf, encoded)

    def _collect_text_commands(
        self,
        page_info: PageInfo,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        opts: FillOptions,
    ) -> list[str]:
        commands: list[str] = []

        # 关键修复：调整渲染顺序
        # 1. 先处理需要原样保留的原始指令流 (PreservedBlock)
        # 这样保留文本不会受到翻译文本字体状态的影响
        preserved_blocks: Sequence[PreservedBlock] = getattr(page_info, "preserved_texts", []) or []
        preserved_ops = self._collect_preserved_ops(preserved_blocks, page_info.texts)
        if preserved_ops:
            commands.extend(preserved_ops)

        # 2. 然后处理需要翻译的文本
        if font_resource_names and font_subsets:
            color_command = self._build_text_style_command(opts.text_color)
            source_color_command = color_command
            target_color_command = color_command
            if opts.render_mode == FillRenderMode.BILINGUAL_STACK:
                source_color_command = self._build_text_style_command(opts.bilingual_source_color)
                target_color_command = self._build_text_style_command(opts.bilingual_target_color)
            for block in page_info.texts:
                spans = getattr(block, "spans", None)
                if not spans:
                    continue
                grouped_table_span_ids: set[int] = set()
                if opts.render_mode == FillRenderMode.TRANSLATION_ONLY:
                    table_commands, grouped_table_span_ids = self._build_table_cell_group_commands(
                        block,
                        spans,
                        font_resource_names,
                        font_subsets,
                        color_command,
                        opts,
                    )
                    commands.extend(table_commands)
                for span in spans:
                    if id(span) in grouped_table_span_ids:
                        continue
                    if opts.render_mode == FillRenderMode.BILINGUAL_STACK:
                        commands.extend(
                            self._build_bilingual_commands(
                                span,
                                font_resource_names,
                                font_subsets,
                                source_color_command,
                                target_color_command,
                                opts,
                            )
                        )
                    else:
                        commands.extend(
                            self._build_translation_only_commands(
                                span,
                                font_resource_names,
                                font_subsets,
                                color_command,
                                opts,
                                table_cell_fit=self._is_table_text_span(block, span),
                            )
                        )

        return commands

    def _build_table_cell_group_commands(
        self,
        block: TextBlock,
        spans: Sequence[TextSpan],
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        opts: FillOptions,
    ) -> tuple[list[str], set[int]]:
        return self._build_table_text_renderer().build_cell_group_commands(
            block,
            spans,
            font_resource_names,
            font_subsets,
            color_command,
            self._build_table_render_config(opts),
        )

    def _build_table_text_renderer(self) -> TableTextRenderer:
        return TableTextRenderer(
            build_single_line_command=self._build_single_line_command,
            calculate_segments_width=self._calculate_segments_width,
            determine_font_size=self._determine_font_size,
        )

    @staticmethod
    def _build_table_render_config(opts: FillOptions) -> TableRenderConfig:
        return TableRenderConfig(
            line_height_factor=opts.line_height_factor,
            min_font_size=opts.table_min_translation_font_size,
        )

    @staticmethod
    def _table_cell_bbox_key(bbox: BBox) -> tuple[float, float, float, float]:
        return TableTextRenderer.table_cell_bbox_key(bbox)

    def _build_translation_only_commands(
        self,
        span: TextSpan,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        color_command: str,
        opts: FillOptions,
        *,
        text_override: str | None = None,
        table_cell_fit: bool = False,
    ) -> list[str]:
        text = (text_override if text_override is not None else span.translated_text or "").strip()
        if not text:
            return []

        segments = segment_text(text)
        if not segments:
            return []

        font_size = self._determine_font_size(span)
        if font_size <= 0.0:
            return []
        if table_cell_fit and span.font_size:
            font_size = max(font_size, span.font_size)

        # 宽度溢出检测与自适应缩放 (Font Size + Horizontal Scaling)
        # 增加最小字号与最小横向缩放护栏，避免文本被压缩得过窄影响可读性
        # 优先使用所属 Textbox 的右边界作为最大宽度限制，这在表格和表单场景下能提供更多余地
        h_scale = 100.0
        fit_bbox = self._resolve_span_fit_bbox(span, table_cell_fit=table_cell_fit)
        x = span.bbox.x1
        if fit_bbox is not None:
            x = max(span.bbox.x1, fit_bbox.x1)
            max_width = max(0.0, fit_bbox.x2 - x)
        else:
            max_width = max(0.0, span.bbox.x2 - span.bbox.x1)

        if table_cell_fit:
            font_size = self._fit_table_cell_single_line(
                segments,
                font_subsets,
                font_size=font_size,
                max_width=max_width,
                min_font_size=opts.table_min_translation_font_size,
            )
        else:
            font_size, h_scale = self._fit_single_line_font_and_scaling(
                segments,
                font_subsets,
                font_size=font_size,
                max_width=max_width,
                min_font_size=opts.min_translation_font_size,
                allow_horizontal_scaling=opts.allow_horizontal_scaling,
                min_horizontal_scaling=opts.min_horizontal_scaling,
            )

        y = span.bbox.y1
        command = self._build_single_line_command(
            segments,
            font_resource_names,
            font_subsets,
            font_size,
            color_command,
            x,
            y,
            horizontal_scaling=h_scale,
        )
        return [command] if command else []

    @staticmethod
    def _resolve_span_fit_bbox(span: TextSpan, *, table_cell_fit: bool) -> BBox | None:
        if table_cell_fit and span.table_cell_bbox is not None:
            return span.table_cell_bbox
        return span.source_textbox_bbox

    @staticmethod
    def _is_table_text_span(block: TextBlock, span: TextSpan) -> bool:
        """表格保持 span 级渲染，但溢出时使用单元格专用收敛策略。"""
        return is_table_text_span(block, span)

    def _build_bilingual_commands(
        self,
        span: TextSpan,
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        source_color_command: str,
        target_color_command: str,
        opts: FillOptions,
    ) -> list[str]:
        target_text = (span.translated_text or "").strip()
        source_text = (span.text or "").strip()
        if not target_text:
            return []
        if not source_text:
            return self._build_translation_only_commands(
                span,
                font_resource_names,
                font_subsets,
                target_color_command,
                opts,
            )
        if self._is_nearly_identical(source_text, target_text):
            return self._build_translation_only_commands(
                span,
                font_resource_names,
                font_subsets,
                source_color_command,
                opts,
                text_override=source_text,
            )

        base_size = self._determine_font_size(span)
        if base_size <= 0.0:
            return []

        layout = self._compute_bilingual_layout(span, base_size, opts)
        if layout is None:
            return []

        font_size = layout.font_size
        source_segments = segment_text(source_text)
        target_segments = segment_text(target_text)

        # 宽度溢出检测与自动缩放 (双语模式)
        h_scale = 100.0
        if span.source_textbox_bbox is not None:
            max_width = max(0.0, span.source_textbox_bbox.x2 - span.bbox.x1)
        else:
            max_width = max(0.0, span.bbox.x2 - span.bbox.x1)

        widest_segments = source_segments
        source_width = self._calculate_segments_width(source_segments, font_subsets, font_size)
        target_width = self._calculate_segments_width(target_segments, font_subsets, font_size)
        if target_width > source_width:
            widest_segments = target_segments

        font_size, h_scale = self._fit_single_line_font_and_scaling(
            widest_segments,
            font_subsets,
            font_size=font_size,
            max_width=max_width,
            min_font_size=opts.min_bilingual_font_size,
            allow_horizontal_scaling=opts.allow_horizontal_scaling,
            min_horizontal_scaling=opts.min_horizontal_scaling,
        )

        # 重新计算布局以适配调整后的字号，确保基线位置正确
        layout = self._compute_bilingual_layout(span, font_size, opts)
        if layout is None:
            return []

        x = span.bbox.x1

        # 原文
        original_command = self._build_single_line_command(
            source_segments,
            font_resource_names,
            font_subsets,
            layout.font_size,
            source_color_command,
            x,
            layout.original_baseline,
            horizontal_scaling=h_scale,
        )

        # 译文
        translation_command = self._build_single_line_command(
            target_segments,
            font_resource_names,
            font_subsets,
            layout.font_size,
            target_color_command,
            x,
            layout.translation_baseline,
            horizontal_scaling=h_scale,
        )

        res = []
        if original_command:
            res.append(original_command)
        if translation_command:
            res.append(translation_command)
        return res

    def _fit_single_line_font_and_scaling(
        self,
        segments: list[tuple[str, FontLanguage]],
        font_subsets: dict[FontLanguage, FontSubsetData],
        *,
        font_size: float,
        max_width: float,
        min_font_size: float,
        allow_horizontal_scaling: bool,
        min_horizontal_scaling: float,
    ) -> tuple[float, float]:
        """为单行文本计算字体与水平缩放，带最小可读性护栏。"""
        if font_size <= 0.0 or max_width <= 0.0 or not segments:
            return font_size, 100.0

        guarded_min_font = min(max(min_font_size, 0.1), font_size)
        guarded_min_h_scale = min(max(min_horizontal_scaling, 1.0), 100.0)
        current_width = self._calculate_segments_width(segments, font_subsets, font_size)
        if current_width <= max_width * 1.02 or current_width <= 0.0:
            return font_size, 100.0

        scale = max_width / current_width
        if not allow_horizontal_scaling:
            adjusted_size = max(font_size * scale, guarded_min_font)
            return adjusted_size, 100.0

        adjusted_size = font_size
        adjusted_h_scale = 100.0

        if scale < 0.85:
            size_ratio_floor = guarded_min_font / font_size
            font_scale = max(scale**0.5, size_ratio_floor)
            adjusted_size = max(font_size * font_scale, guarded_min_font)
            adjusted_width = self._calculate_segments_width(segments, font_subsets, adjusted_size)
            if adjusted_width > 0.0:
                required_h_scale = (max_width / adjusted_width) * 100.0
                adjusted_h_scale = min(100.0, max(required_h_scale, guarded_min_h_scale))

                # 当触发横向缩放下限时，再尝试缩小字号（但不低于最小字号）。
                if required_h_scale < guarded_min_h_scale and adjusted_size > guarded_min_font:
                    extra_scale = required_h_scale / guarded_min_h_scale
                    adjusted_size = max(adjusted_size * extra_scale, guarded_min_font)
                    adjusted_width = self._calculate_segments_width(segments, font_subsets, adjusted_size)
                    if adjusted_width > 0.0:
                        required_h_scale = (max_width / adjusted_width) * 100.0
                        adjusted_h_scale = min(100.0, max(required_h_scale, guarded_min_h_scale))
        else:
            adjusted_size = max(font_size * scale, guarded_min_font)
            if adjusted_size <= guarded_min_font:
                adjusted_width = self._calculate_segments_width(segments, font_subsets, adjusted_size)
                if adjusted_width > max_width * 1.02 and adjusted_width > 0.0:
                    required_h_scale = (max_width / adjusted_width) * 100.0
                    adjusted_h_scale = min(100.0, max(required_h_scale, guarded_min_h_scale))

        return adjusted_size, adjusted_h_scale

    def _fit_table_cell_single_line(
        self,
        segments: list[tuple[str, FontLanguage]],
        font_subsets: dict[FontLanguage, FontSubsetData],
        *,
        font_size: float,
        max_width: float,
        min_font_size: float,
    ) -> float:
        """表格单元格不能扩列：译文溢出时只缩字号，不使用 Tz 横向压字。"""
        return self._build_table_text_renderer().fit_table_cell_single_line(
            segments,
            font_subsets,
            font_size=font_size,
            max_width=max_width,
            min_font_size=min_font_size,
        )

    @staticmethod
    def _is_nearly_identical(s1: str, s2: str) -> bool:
        """判断两段文本是否几乎一致（忽略空白和大小写）。"""
        if s1 == s2:
            return True
        n1 = "".join(s1.split()).lower()
        n2 = "".join(s2.split()).lower()
        return n1 == n2

    def _build_single_line_command(
        self,
        segments: list[tuple[str, FontLanguage]],
        font_resource_names: dict[FontLanguage, pikepdf.Name],
        font_subsets: dict[FontLanguage, FontSubsetData],
        font_size: float,
        color_command: str,
        x: float,
        y: float,
        horizontal_scaling: float = 100.0,
    ) -> str:
        if not segments:
            return ""

        horizontal_scaling = min(max(horizontal_scaling, 1.0), 100.0)

        # 优化方案：在一个 BT 块内流式绘制，仅在切换字体时更新 Tf
        # 初始位置由 Tm 确定，后续片段自动接着绘制，利用 PDF 渲染器的字距处理
        commands = [f"BT {color_command}"]

        # 设置水平缩放 (Tz)
        if abs(horizontal_scaling - 100.0) > 0.1:
            commands.append(f"{horizontal_scaling:.1f} Tz")

        # 设置初始位置
        commands.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")

        for text, lang in segments:
            resource_name = font_resource_names.get(lang)
            subset = font_subsets.get(lang)
            if not resource_name or not subset:
                continue

            encoded = self._encode_text(text, subset)
            if not encoded:
                continue

            # 切换字体并绘制
            # 注意：不再每个片段都重置 Tm，利用 Tj 的自然推进
            # 修正：resource_name 已经是 /FTR_XX 格式，不需要再加 /
            cmd = (
                f"{str(resource_name)} {font_size:.2f} Tf "
                f"<{encoded}> Tj"
            )
            commands.append(cmd)

        commands.append("ET\n")
        return " ".join(commands)

    @staticmethod
    def _validate_color(color: tuple[float, float, float], name: str) -> None:
        if len(color) != 3:
            raise ValueError(f"{name} 必须提供 RGB 三分量")
        if not all(0.0 <= component <= 1.0 for component in color):
            raise ValueError(f"{name} 分量必须位于 [0, 1] 区间")

    @staticmethod
    def _calculate_text_width(text: str, subset: FontSubsetData, font_size: float) -> float:
        """根据字体的 CID 宽度表计算字符串的实际显示宽度。"""
        total_width_units = 0
        for char in text:
            cid = subset.char_to_cid.get(char)
            if cid is not None:
                width = subset.cid_widths.get(cid, subset.default_width or 1000)
                total_width_units += width
            else:
                total_width_units += subset.default_width or 1000
        return (total_width_units / 1000.0) * font_size

    def _calculate_segments_width(
        self,
        segments: list[tuple[str, FontLanguage]],
        font_subsets: dict[FontLanguage, FontSubsetData],
        font_size: float,
    ) -> float:
        """计算多语种片段组合后的总显示宽度。"""
        total_width = 0.0
        for text, lang in segments:
            subset = font_subsets.get(lang)
            if subset:
                total_width += self._calculate_text_width(text, subset, font_size)
        return total_width

    def _compute_bilingual_layout(
        self,
        span: TextSpan,
        base_font_size: float,
        opts: FillOptions,
    ) -> BilingualLayout | None:
        if base_font_size <= 0.0:
            return None

        bbox = span.bbox
        bbox_height = max(0.0, bbox.y2 - bbox.y1)
        line_height_factor = max(opts.line_height_factor, 0.1)
        font_size = base_font_size
        line_height = font_size * line_height_factor
        total_height = font_size + line_height

        if bbox_height > 0.0:
            tolerance_multiplier = 1.0 + opts.height_tolerance
            allowed_height = bbox_height * tolerance_multiplier
            if allowed_height > 0.0 and total_height > allowed_height:
                scale = allowed_height / total_height
                font_size = max(scale * base_font_size, opts.min_bilingual_font_size)
                line_height = font_size * line_height_factor
                total_height = font_size + line_height

            vertical_offset = max(0.0, (bbox_height - total_height) / 2.0)
            translation_baseline = bbox.y1 + vertical_offset
        else:
            translation_baseline = bbox.y1

        original_baseline = translation_baseline + line_height

        if bbox_height > 0.0:
            max_top = bbox.y2 + bbox_height * opts.height_tolerance
            if original_baseline > max_top:
                shift = original_baseline - max_top
                translation_baseline -= shift
                original_baseline -= shift

            min_bottom = bbox.y1 - bbox_height * opts.height_tolerance
            if translation_baseline < min_bottom:
                shift = min_bottom - translation_baseline
                translation_baseline += shift
                original_baseline += shift

        return BilingualLayout(
            font_size=font_size,
            line_height=line_height,
            original_baseline=original_baseline,
            translation_baseline=translation_baseline,
        )

    def _remove_existing_text(self, pdf: pikepdf.Pdf, pages: Sequence[PageInfo]) -> None:
        """
        彻底清除原页面的文本层 (BT...ET)。
        使用解析后的指令流删除，避免正则误截断。
        """
        text_ops = {
            # 文本对象边界
            pikepdf.Operator("BT"),
            pikepdf.Operator("ET"),
            # 文本展示
            pikepdf.Operator("Tj"),
            pikepdf.Operator("TJ"),
            pikepdf.Operator("'"),
            pikepdf.Operator("\""),
            # 文本状态
            pikepdf.Operator("Tc"),
            pikepdf.Operator("Tw"),
            pikepdf.Operator("Tz"),
            pikepdf.Operator("TL"),
            pikepdf.Operator("Tf"),
            pikepdf.Operator("Tr"),
            pikepdf.Operator("Ts"),
            # 文本定位
            pikepdf.Operator("Td"),
            pikepdf.Operator("TD"),
            pikepdf.Operator("Tm"),
            pikepdf.Operator("T*"),
        }

        def strip_text(stream: pikepdf.Stream) -> bytes | None:
            try:
                instructions = pikepdf.parse_content_stream(stream)
            except Exception as exc:
                logger.warning(
                    "Failed to parse content stream for text removal",
                    extra={"error": str(exc)},
                )
                return None
            if not instructions:
                return None
            new_instructions = []
            for inst in instructions:
                # 移除文本相关指令，保留文本对象内的非文本绘制指令
                if inst.operator in text_ops:
                    continue
                new_instructions.append(inst)
            if new_instructions == instructions:
                return None
            return pikepdf.unparse_content_stream(new_instructions)

        def collect_touched_xobject_paths(page_info: PageInfo | None) -> set[tuple[str, ...]]:
            if page_info is None:
                return set()
            touched_paths: set[tuple[str, ...]] = set()
            preserved_blocks = getattr(page_info, "preserved_texts", []) or []
            all_blocks = [*preserved_blocks, *page_info.texts]
            for block in all_blocks:
                for span in getattr(block, "spans", []) or []:
                    for path in getattr(span, "ops_xobject_paths", []) or []:
                        if path:
                            touched_paths.add(tuple(path))
            return touched_paths

        def clean_form_xobjects(
            xobj_dict: pikepdf.Object,
            *,
            allow_clear: bool,
            touched_paths: set[tuple[str, ...]],
            prefix: tuple[str, ...] = (),
        ) -> None:
            if not allow_clear or not touched_paths:
                return
            if not isinstance(xobj_dict, pikepdf.Dictionary):
                xobj_dict = pikepdf.Dictionary(xobj_dict)
            for name in list(xobj_dict.keys()):
                obj = xobj_dict.get(name)
                path_component = str(name).lstrip("/")
                current_path = (*prefix, path_component)
                child_paths = {path for path in touched_paths if path[: len(current_path)] == current_path}
                if not child_paths:
                    continue
                try:
                    stream = pdf.get_object(obj)
                except Exception:
                    stream = obj
                if not isinstance(stream, pikepdf.Stream):
                    continue
                subtype = stream.get(pikepdf.Name("/Subtype"))
                if subtype != pikepdf.Name("/Form"):
                    continue
                if current_path in touched_paths:
                    cleaned = strip_text(stream)
                    if cleaned is not None:
                        stream.write(cleaned)
                resources = stream.get(pikepdf.Name("/Resources"))
                if resources and pikepdf.Name("/XObject") in resources:
                    clean_form_xobjects(
                        resources[pikepdf.Name("/XObject")],
                        allow_clear=allow_clear,
                        touched_paths=child_paths,
                        prefix=current_path,
                    )

        for page_index, page in enumerate(pdf.pages):
            page_info = pages[page_index] if page_index < len(pages) else None
            allow_clear = bool(page_info and self._page_has_pending_translation(page_info))
            if not allow_clear:
                continue
            touched_xobject_paths = collect_touched_xobject_paths(page_info)

            contents = page.get("/Contents")
            if contents is None:
                continue

            form_xobject_text_size = 0
            if isinstance(contents, pikepdf.Array):
                for obj in contents:
                    try:
                        stream = pdf.get_object(obj)
                    except Exception:
                        stream = obj
                    if not isinstance(stream, pikepdf.Stream):
                        continue
                    cleaned = strip_text(stream)
                    if cleaned is not None:
                        stream.write(cleaned)
            else:
                cleaned = strip_text(contents)
                if cleaned is not None:
                    contents.write(cleaned)

            resources = page.get("/Resources")
            if resources and pikepdf.Name("/XObject") in resources:
                clean_form_xobjects(
                    resources[pikepdf.Name("/XObject")],
                    allow_clear=allow_clear,
                    touched_paths=touched_xobject_paths,
                )

    def _register_font(
        self,
        pdf: pikepdf.Pdf,
        subset: FontSubsetData,
        *,
        resource_name: pikepdf.Name | None = None,
    ) -> tuple[pikepdf.Name, pikepdf.Object]:
        resource_name = resource_name or pikepdf.Name(f"/FTR_{subset.language.value.upper()}")

        # 根据字体格式选择正确的嵌入方式
        if subset.is_cff:
            # CFF 字体使用 FontFile3 with OpenType subtype
            font_file_stream = pdf.make_stream(
                subset.font_bytes,
                filter=pikepdf.Name("/FlateDecode"),
            )
            font_file_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/OpenType")
        else:
            # TrueType 字体使用 FontFile2
            font_file_stream = pikepdf.Stream(
                pdf,
                subset.font_bytes,
                {
                    "/Length1": len(subset.font_bytes),
                },
            )

        font_file_ref = pdf.make_indirect(font_file_stream)

        font_descriptor = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/FontDescriptor"),
                "/FontName": pikepdf.Name(f"/{subset.postscript_name}"),
                "/Flags": 4,
                "/Ascent": subset.ascent,
                "/Descent": subset.descent,
                "/CapHeight": subset.cap_height,
                "/ItalicAngle": 0,
                "/StemV": 80,
                "/FontBBox": pikepdf.Array(list(subset.bbox)),
            }
        )
        # 根据字体格式选择正确的 FontFile 键
        if subset.is_cff:
            font_descriptor[pikepdf.Name("/FontFile3")] = font_file_ref
        else:
            font_descriptor[pikepdf.Name("/FontFile2")] = font_file_ref

        font_descriptor_ref = pdf.make_indirect(font_descriptor)

        # 根据字体格式选择 CIDFont 类型
        cid_font_subtype = pikepdf.Name("/CIDFontType0") if subset.is_cff else pikepdf.Name("/CIDFontType2")

        cid_font = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": cid_font_subtype,
                "/BaseFont": pikepdf.Name(f"/{subset.postscript_name}"),
                "/CIDSystemInfo": pikepdf.Dictionary(
                    {
                        "/Registry": "Adobe",
                        "/Ordering": "Identity",
                        "/Supplement": 0,
                    }
                ),
                "/FontDescriptor": font_descriptor_ref,
                "/DW": subset.default_width or 1000,
            }
        )

        # 只有 TrueType-based CIDFont (Type2) 才需要 CIDToGIDMap
        if not subset.is_cff:
            cid_font[pikepdf.Name("/CIDToGIDMap")] = pikepdf.Name("/Identity")

        width_array = self._build_width_array(subset)
        if width_array is not None:
            cid_font[pikepdf.Name("/W")] = width_array

        cid_font_ref = pdf.make_indirect(cid_font)

        to_unicode_ref = self._build_to_unicode_stream(pdf, resource_name, subset)

        font_dict = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type0"),
                "/BaseFont": pikepdf.Name(f"/{subset.postscript_name}"),
                "/Encoding": pikepdf.Name("/Identity-H"),
                "/DescendantFonts": pikepdf.Array([cid_font_ref]),
            }
        )
        if to_unicode_ref is not None:
            font_dict[pikepdf.Name("/ToUnicode")] = to_unicode_ref

        font_ref = pdf.make_indirect(font_dict)
        return resource_name, font_ref

    @staticmethod
    def _collect_page_font_resource_names(pdf: pikepdf.Pdf) -> set[str]:
        """收集页面级字体资源名，避免新增字体覆盖原 PDF 资源。"""
        names: set[str] = set()
        for page in pdf.pages:
            resources = page.get(pikepdf.Name("/Resources"))
            if not isinstance(resources, pikepdf.Dictionary):
                continue
            fonts = resources.get(pikepdf.Name("/Font"))
            if not isinstance(fonts, pikepdf.Dictionary):
                continue
            names.update(str(name) for name in fonts.keys())
        return names

    @staticmethod
    def _allocate_font_resource_name(used_names: set[str], preferred_name: str) -> pikepdf.Name:
        """分配不冲突的字体资源名。"""
        normalized = preferred_name if preferred_name.startswith("/") else f"/{preferred_name}"
        if normalized not in used_names:
            used_names.add(normalized)
            return pikepdf.Name(normalized)

        index = 0
        while True:
            candidate = f"{normalized}_{index}"
            if candidate not in used_names:
                used_names.add(candidate)
                return pikepdf.Name(candidate)
            index += 1

    @staticmethod
    def _build_width_array(subset: FontSubsetData) -> pikepdf.Array | None:
        if not subset.cid_widths:
            return None
        sorted_items = sorted(subset.cid_widths.items())
        entries: list[pikepdf.Object] = []
        start_cid = sorted_items[0][0]
        widths: list[int] = [sorted_items[0][1]]
        prev_cid = start_cid
        for cid, width in sorted_items[1:]:
            if cid == prev_cid + 1:
                widths.append(width)
            else:
                entries.extend([start_cid, pikepdf.Array(widths)])
                start_cid = cid
                widths = [width]
            prev_cid = cid
        entries.extend([start_cid, pikepdf.Array(widths)])
        return pikepdf.Array(entries)

    def _build_to_unicode_stream(
        self,
        pdf: pikepdf.Pdf,
        resource_name: pikepdf.Name,
        subset: FontSubsetData,
    ) -> pikepdf.Object | None:
        if not subset.cid_to_unicode:
            return None
        entries = sorted(subset.cid_to_unicode.items())
        cmap_name = str(resource_name)[1:] + "-UCS2"
        lines = [
            "/CIDInit /ProcSet findresource begin",
            "12 dict begin",
            "begincmap",
            "/CIDSystemInfo",
            "<< /Registry (Adobe)",
            "/Ordering (UCS)",
            "/Supplement 0",
            ">> def",
            f"/CMapName /{cmap_name} def",
            "/CMapType 2 def",
            "1 begincodespacerange",
            "<0000> <FFFF>",
            "endcodespacerange",
        ]
        chunk: list[tuple[int, str]] = []
        for cid, unicode_hex in entries:
            chunk.append((cid, unicode_hex))
            if len(chunk) == 100:
                lines.append(f"{len(chunk)} beginbfchar")
                for cid_value, unicode_value in chunk:
                    lines.append(f"<{cid_value:04X}> <{unicode_value}>")
                lines.append("endbfchar")
                chunk = []
        if chunk:
            lines.append(f"{len(chunk)} beginbfchar")
            for cid_value, unicode_value in chunk:
                lines.append(f"<{cid_value:04X}> <{unicode_value}>")
            lines.append("endbfchar")
        lines.extend(
            [
                "endcmap",
                "CMapName currentdict /CMap defineresource pop",
                "end",
                "end",
            ]
        )
        data = "\n".join(lines).encode("utf-8")
        stream = pikepdf.Stream(pdf, data, {"/Length": len(data)})
        return pdf.make_indirect(stream)

    def _ensure_font_resource(
        self,
        page: pikepdf.Page,
        resource_name: pikepdf.Name,
        font_ref: pikepdf.Object,
    ) -> None:
        # 核心修复：保持原始 Resources 的完整性
        # 确保不破坏原有字体资源的间接引用关系
        if pikepdf.Name("/Resources") not in page:
            page[pikepdf.Name("/Resources")] = pikepdf.Dictionary()

        # 直接操作 page.Resources，不创建副本
        if pikepdf.Name("/Font") not in page.Resources:
            page.Resources[pikepdf.Name("/Font")] = pikepdf.Dictionary()

        # 仅在新字体不存在时才添加
        if resource_name not in page.Resources[pikepdf.Name("/Font")]:
            page.Resources[pikepdf.Name("/Font")][resource_name] = font_ref

    def _generate_font_resource_name(self, pdf: pikepdf.Pdf) -> pikepdf.Name:
        existing: set[str] = set()
        for page in pdf.pages:
            resources = page.Resources
            font_resources = resources.get(pikepdf.Name("/Font"), None)
            if font_resources is None:
                continue
            font_dict = pikepdf.Dictionary(font_resources)
            existing.update(name for name in font_dict.keys())

        index = 0
        while True:
            candidate = pikepdf.Name(f"/FTR{index}")
            if candidate not in existing:
                return candidate
            index += 1

    @staticmethod
    def _encode_text(text: str, subset: FontSubsetData) -> str:
        codes: list[str] = []
        for char in text:
            cid = subset.char_to_cid.get(char)
            if cid is None:
                continue
            codes.append(f"{cid:04X}")
        return "".join(codes)

    @staticmethod
    def _determine_font_size(span: TextSpan) -> float:
        if span.adjusted_font_size is not None:
            return max(span.adjusted_font_size, 0.1)
        if span.font_size is not None and span.font_size > 0.0:
            return span.font_size
        return 12.0

    @staticmethod
    def _extract_matrix_operands(operands: Sequence[object]) -> PDFMatrix | None:
        if len(operands) != 6:
            return None
        try:
            a, b, c, d, e, f = (float(value) for value in operands)
        except (TypeError, ValueError):
            return None
        return (a, b, c, d, e, f)

    @staticmethod
    def _is_identity_matrix(matrix: PDFMatrix, tolerance: float = 1e-6) -> bool:
        identity = MATRIX_IDENTITY
        return all(abs(current - expected) <= tolerance for current, expected in zip(matrix, identity))

    @classmethod
    def _compute_residual_ctm(cls, container: pikepdf.Page | pikepdf.Stream) -> PDFMatrix:
        try:
            instructions = pikepdf.parse_content_stream(container)
        except Exception as exc:
            logger.warning(
                "Failed to parse content stream for CTM normalization",
                extra={"error": str(exc)},
            )
            return MATRIX_IDENTITY

        current_ctm: PDFMatrix = MATRIX_IDENTITY
        ctm_stack: list[PDFMatrix] = []
        op_q = pikepdf.Operator("q")
        op_Q = pikepdf.Operator("Q")
        op_cm = pikepdf.Operator("cm")

        for instruction in instructions:
            if instruction.operator == op_q:
                ctm_stack.append(current_ctm)
                continue
            if instruction.operator == op_Q:
                if ctm_stack:
                    current_ctm = ctm_stack.pop()
                continue
            if instruction.operator != op_cm:
                continue
            matrix = cls._extract_matrix_operands(instruction.operands)
            if matrix is None:
                continue
            current_ctm = mult_matrix(matrix, current_ctm)

        return current_ctm

    @classmethod
    def _wrap_bytes_with_inverse_ctm(cls, content: bytes, residual_ctm: PDFMatrix) -> bytes:
        if cls._is_identity_matrix(residual_ctm):
            return content
        try:
            inverse_ctm = inverse_matrix(residual_ctm)
        except Exception as exc:
            logger.warning(
                "Failed to invert residual CTM; keep appended stream unchanged",
                extra={"error": str(exc), "residual_ctm": residual_ctm},
            )
            return content

        a, b, c, d, e, f = inverse_ctm
        prefix = f" q\n{a:.12g} {b:.12g} {c:.12g} {d:.12g} {e:.12g} {f:.12g} cm\n".encode("latin-1")
        suffix = b"\nQ "
        return prefix + content + suffix

    @classmethod
    def _compute_page_extraction_ctm(cls, page: pikepdf.Page) -> PDFMatrix:
        """复用 pdfminer 的页面归一化 CTM，便于将提取坐标逆变换回原始页面坐标系。"""
        mediabox = page.get("/MediaBox")
        if mediabox is None:
            return MATRIX_IDENTITY

        try:
            x0, y0, x1, y1 = (float(value) for value in mediabox)
        except Exception:
            logger.warning("Failed to parse page mediabox for extraction CTM normalization")
            return MATRIX_IDENTITY

        rotate_obj = page.get("/Rotate", 0)
        try:
            rotate = (int(rotate_obj) + 360) % 360
        except Exception:
            rotate = 0

        if rotate == 90:
            return (0.0, -1.0, 1.0, 0.0, -y0, x1)
        if rotate == 180:
            return (-1.0, 0.0, 0.0, -1.0, x1, y1)
        if rotate == 270:
            return (0.0, 1.0, -1.0, 0.0, y1, -x0)
        return (1.0, 0.0, 0.0, 1.0, -x0, -y0)

    @classmethod
    def _normalize_stream_for_page_extraction_ctm(
        cls,
        pdf: pikepdf.Pdf,
        page: pikepdf.Page,
        stream: pikepdf.Stream,
    ) -> pikepdf.Stream:
        extraction_ctm = cls._compute_page_extraction_ctm(page)
        if cls._is_identity_matrix(extraction_ctm):
            return stream
        payload = cls._wrap_bytes_with_inverse_ctm(stream.read_bytes(), extraction_ctm)
        return pikepdf.Stream(pdf, payload)

    @classmethod
    def _normalize_stream_for_page_ctm(cls, pdf: pikepdf.Pdf, page: pikepdf.Page, stream: pikepdf.Stream) -> pikepdf.Stream:
        residual_ctm = cls._compute_residual_ctm(page)
        if cls._is_identity_matrix(residual_ctm):
            return stream
        payload = cls._wrap_bytes_with_inverse_ctm(stream.read_bytes(), residual_ctm)
        return pikepdf.Stream(pdf, payload)

    @classmethod
    def _append_stream(cls, pdf: pikepdf.Pdf, page: pikepdf.Page, stream: pikepdf.Stream) -> None:
        normalized_stream = cls._normalize_stream_for_page_ctm(pdf, page, stream)
        existing = page.get("/Contents")
        if existing is None:
            page.Contents = normalized_stream
            return
        if isinstance(existing, pikepdf.Stream):
            page.Contents = pikepdf.Array([existing, normalized_stream])
            return
        if isinstance(existing, pikepdf.Array):
            existing.append(normalized_stream)
            page.Contents = existing
            return
        page.Contents = pikepdf.Array([existing, normalized_stream])
