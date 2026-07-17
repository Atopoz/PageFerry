"""PDF 文本处理 Part3: 段落翻译"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from modules.translation.batch_fanout import (
    BatchFanoutOutcome,
    run_batch_fanout,
    translator_per_job_concurrency,
)
from modules.translation.contracts import BatchTranslator

from .entities import PageInfo, TextSpan
from .formatter import (
    TaggedTextBlock,
    build_tagged_text_blocks,
    render_tagged_text,
    render_tagged_text_chunks,
)


@dataclass(frozen=True)
class ChunkingConfig:
    """控制 formatter chunk 大小的配置。"""

    max_tokens: int = 1200
    min_tokens: int = 0


@dataclass
class TranslatorConfig:
    """翻译流程整体配置。"""

    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    min_font_ratio: float = 0.4
    min_font_size: float = 6.0
    use_weighted_length: bool = True
    adaptive_min_ratio: bool = True
    language_scale_factors: Dict[str, float] = field(
        default_factory=lambda: {
            "zh": 1.0,
            "ja": 0.85,
            "ko": 0.88,
            "en": 1.0,
            "fr": 0.97,
            "de": 0.95,
            "es": 0.97,
            "ru": 0.95,
            "bn": 1.0,
            "km": 0.95,
            "vi": 0.9,
            "中文": 1.0,
            "日文": 0.85,
            "韩文": 0.88,
            "法文": 0.97,
            "法语": 0.97,
            "德文": 0.95,
            "德语": 0.95,
            "西班牙文": 0.97,
            "西班牙语": 0.97,
            "俄文": 0.95,
            "俄语": 0.95,
            "孟加拉语": 1.0,
            "柬埔寨语": 0.95,
            "越南语": 0.9,
        }
    )


@dataclass(frozen=True)
class TranslationChunk:
    """用于记录每个请求块的数据。"""

    index: int
    content: str
    prompt: str
    block_refs: Tuple[Tuple[int, str], ...]


@dataclass(frozen=True)
class ChunkTranslation:
    """翻译后的块结果。"""

    index: int
    content: str
    prompt: str
    translation: str | None
    block_refs: Tuple[Tuple[int, str], ...]
    fallback: bool = False


@dataclass(frozen=True)
class TranslationPipelineResult:
    """翻译流程的完整输出。"""

    pages: Sequence[PageInfo]
    blocks: Sequence[TaggedTextBlock]
    chunks: Sequence[ChunkTranslation]


@dataclass(frozen=True)
class _ParsedTranslationChunk:
    """保存完整解析后的 page 顺序与 block 回填值。"""

    pages: Tuple[int, ...]
    blocks: Tuple[Tuple[Tuple[int, str], Dict[str, str]], ...]


class ParagraphTranslator:
    """面向段落抽取结果的翻译组件。"""

    _page_line_pattern = re.compile(r"^\[PAGE_(\d+)]$")
    _block_line_pattern = re.compile(r"^\[(BLOCK_\d+)]")
    _span_open_pattern = re.compile(r"<(SPAN_\d+)>")
    _span_close_pattern = re.compile(r"</(SPAN_\d+)>")
    _plain_span_key = "__plain__"

    def __init__(self, *, config: TranslatorConfig | None = None) -> None:
        self.config = config or TranslatorConfig()

    def translate(
        self,
        pages: Sequence[PageInfo],
        blocks: Sequence[TaggedTextBlock],
        source_lang: str | None,
        target_lang: str,
        translator: BatchTranslator,
        *,
        config: TranslatorConfig | None = None,
        on_chunk_settled: Callable[[int, int, bool], None] | None = None,
    ) -> TranslationPipelineResult:
        """有界并发翻译 chunk，结构异常或 provider 失败时只回退当前 chunk。"""

        active_config = config or self.config
        if not pages:
            return TranslationPipelineResult(pages=[], blocks=blocks, chunks=[])

        effective_blocks = list(blocks) or build_tagged_text_blocks(pages)
        chunk_texts = self._render_chunks(effective_blocks, active_config.chunking)
        if not chunk_texts:
            return TranslationPipelineResult(pages=deepcopy(pages), blocks=effective_blocks, chunks=[])

        chunks = self._build_chunks(chunk_texts)
        chunk_results: list[ChunkTranslation] = []
        processed = 0
        if on_chunk_settled is not None:
            on_chunk_settled(0, len(chunks), False)

        def translate_chunk(chunk: TranslationChunk) -> str:
            """调用一次 provider，并拒绝丢失或重排 PDF marker 的候选。"""

            result = translator.translate_batch(
                texts=(chunk.content,),
                source_language=source_lang,
                target_language=target_lang,
                format_hint="pdf",
            )
            candidates = [item.text for item in result.items if item.index == 0]
            if len(candidates) != 1:
                raise ValueError("pdf_translation_index_mismatch")
            candidate = candidates[0]
            if not self._chunk_translation_is_usable(chunk.content, candidate):
                raise ValueError("pdf_marker_mismatch")
            return candidate

        def commit_chunk(
            _chunk_index: int,
            chunk: TranslationChunk,
            outcome: BatchFanoutOutcome[str],
        ) -> None:
            """按输入顺序提交已验证结果并报告真实完成进度。"""

            nonlocal processed
            fallback = outcome.value is None
            translation = chunk.content if fallback else outcome.value
            chunk_results.append(
                ChunkTranslation(
                    index=chunk.index,
                    content=chunk.content,
                    prompt=chunk.prompt,
                    translation=translation,
                    block_refs=chunk.block_refs,
                    fallback=fallback,
                )
            )
            processed += 1
            if on_chunk_settled is not None:
                on_chunk_settled(processed, len(chunks), fallback)

        run_batch_fanout(
            chunks,
            translate_chunk,
            max_concurrency=translator_per_job_concurrency(translator),
            on_group_settled=commit_chunk,
        )
        updated_pages = deepcopy(pages)
        self._apply_translations(updated_pages, effective_blocks, chunk_results, target_lang)
        return TranslationPipelineResult(pages=updated_pages, blocks=effective_blocks, chunks=chunk_results)

    def _render_chunks(
        self,
        blocks: Sequence[TaggedTextBlock],
        chunk_config: ChunkingConfig,
    ) -> List[str]:
        """仅负责将 blocks 渲染成 chunk 文本列表。"""
        if chunk_config.max_tokens <= 0:
            raise ValueError("max_tokens 必须为正数")

        chunk_texts = render_tagged_text_chunks(
            blocks,
            max_tokens=chunk_config.max_tokens,
            min_tokens=chunk_config.min_tokens,
        )
        if not chunk_texts and blocks:
            combined = render_tagged_text(blocks)
            chunk_texts = [combined] if combined else []
        return chunk_texts

    def _build_chunks(
        self,
        chunk_texts: List[str],
    ) -> List[TranslationChunk]:
        """把 formatter 文本连同可验证的 block refs 固化为 chunk。"""

        chunks: list[TranslationChunk] = []
        for idx, chunk_text in enumerate(chunk_texts):
            block_refs = tuple(self._extract_block_refs_from_chunk(chunk_text))
            chunks.append(
                TranslationChunk(
                    index=idx,
                    content=chunk_text,
                    prompt=chunk_text,
                    block_refs=block_refs,
                )
            )
        return chunks

    def _chunk_translation_is_usable(self, source: str, candidate: str) -> bool:
        """完整解析候选，并要求每个源 block/span 都有唯一非空回填值。"""

        if not candidate.strip():
            return False
        source_structure = self._parse_translation_chunk_structure(source)
        candidate_structure = self._parse_translation_chunk_structure(candidate)
        if source_structure is None or candidate_structure is None:
            return False
        if source_structure.pages != candidate_structure.pages:
            return False

        source_blocks = source_structure.blocks
        candidate_blocks = candidate_structure.blocks
        if tuple(key for key, _span_map in source_blocks) != tuple(
            key for key, _span_map in candidate_blocks
        ):
            return False

        for (_source_key, source_span_map), (_candidate_key, candidate_span_map) in zip(
            source_blocks,
            candidate_blocks,
            strict=True,
        ):
            source_span_markers = tuple(
                marker for marker in source_span_map if marker != self._plain_span_key
            )
            if source_span_markers:
                if tuple(candidate_span_map) != source_span_markers:
                    return False
                if any(not value.strip() for value in candidate_span_map.values()):
                    return False
                continue

            # formatter 会把单 span 与 block 模式都渲染为 plain block。模型若为
            # 它补上规范、无残余的 SPAN 包装，仍按一个 block 译文合并回填。
            candidate_span_markers = tuple(
                marker for marker in candidate_span_map if marker != self._plain_span_key
            )
            if candidate_span_markers != tuple(
                sorted(candidate_span_markers, key=self._span_sort_key)
            ):
                return False
            translated_text = self._resolve_plain_block_translation(candidate_span_map)
            if translated_text is None or not translated_text.strip():
                return False
        return True

    def _parse_translation_chunk_structure(
        self,
        translation: str,
    ) -> _ParsedTranslationChunk | None:
        """按完整 chunk 语法解析 page、block 与 block 内容，任何游离文本都判失败。"""

        pages: list[int] = []
        blocks: list[Tuple[Tuple[int, str], Dict[str, str]]] = []
        current_page: int | None = None
        current_block_key: Tuple[int, str] | None = None
        current_lines: list[str] = []
        current_page_has_block = False

        def flush_current_block() -> bool:
            """把当前 block 严格解析后加入结果，返回其结构是否完整。"""

            nonlocal current_block_key, current_lines
            if current_block_key is None:
                return True
            parsed = self._parse_strict_block_translation_content("\n".join(current_lines))
            if parsed is None:
                return False
            blocks.append((current_block_key, parsed))
            current_block_key = None
            current_lines = []
            return True

        for raw_line in translation.splitlines():
            line = raw_line.strip()
            page_match = self._page_line_pattern.fullmatch(line)
            if page_match:
                if not flush_current_block():
                    return None
                if current_page is not None and not current_page_has_block:
                    return None
                current_page = int(page_match.group(1))
                pages.append(current_page)
                current_page_has_block = False
                continue

            block_match = self._block_line_pattern.match(line)
            if block_match:
                if current_page is None or not flush_current_block():
                    return None
                block_marker = f"[{block_match.group(1)}]"
                current_block_key = (current_page, block_marker)
                trailing = line[block_match.end() :]
                current_lines = [trailing] if trailing else []
                current_page_has_block = True
                continue

            if current_block_key is None:
                if line:
                    return None
                continue
            current_lines.append(line)

        if not flush_current_block():
            return None
        if current_page is None or not current_page_has_block:
            return None
        return _ParsedTranslationChunk(pages=tuple(pages), blocks=tuple(blocks))

    def _parse_strict_block_translation_content(self, block_content: str) -> Dict[str, str] | None:
        """完整消费 block 内容，拒绝空值、重复、嵌套、交叉标签与游离正文。"""

        content = block_content.strip()
        if not content:
            return None
        if not self._contains_span_tag_fragment(content):
            if self._contains_chunk_marker_fragment(content):
                return None
            return {self._plain_span_key: content}

        span_texts: Dict[str, str] = {}
        cursor = 0
        while cursor < len(content):
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
            if cursor >= len(content):
                break

            open_match = self._span_open_pattern.match(content, cursor)
            if open_match is None:
                return None
            span_marker = open_match.group(1)
            if span_marker in span_texts:
                return None

            value_start = open_match.end()
            next_open = self._span_open_pattern.search(content, value_start)
            close_match = self._span_close_pattern.search(content, value_start)
            if close_match is None:
                return None
            if next_open is not None and next_open.start() < close_match.start():
                return None
            if close_match.group(1) != span_marker:
                return None

            value = content[value_start : close_match.start()]
            if (
                not value.strip()
                or self._contains_span_tag_fragment(value)
                or self._contains_chunk_marker_fragment(value)
            ):
                return None
            span_texts[span_marker] = value
            cursor = close_match.end()

        return span_texts or None

    def _apply_translations(
        self,
        pages: Sequence[PageInfo],
        blocks: Sequence[TaggedTextBlock],
        chunk_results: Sequence[ChunkTranslation],
        target_language: str,
    ) -> None:
        """根据翻译结果更新 PageInfo 副本的 span 文本。"""
        if not chunk_results:
            return

        block_lookup: Dict[Tuple[int, str], TaggedTextBlock] = {
            (block.page_index, block.marker): block for block in blocks
        }
        textblock_lookup: Dict[Tuple[int, int], object] = {}
        span_lookup: Dict[Tuple[int, int, int], TextSpan] = {}
        for page_index, page in enumerate(pages):
            for text_block in page.texts:
                textblock_lookup[(page_index, text_block.block_id)] = text_block
                for span in text_block.spans:
                    span_lookup[(page_index, text_block.block_id, span.span_id)] = span

        aggregated: Dict[Tuple[int, str], Dict[str, str]] = {}
        for chunk in chunk_results:
            if not chunk.translation:
                continue
            parsed = self._parse_translation_text(chunk.translation)
            for key, span_map in parsed.items():
                aggregated[key] = span_map

        for key, span_map in aggregated.items():
            tagged_block = block_lookup.get(key)
            if tagged_block is None:
                continue
            text_block = textblock_lookup.get((tagged_block.page_index, tagged_block.original_block_id))
            if tagged_block.plain_text is not None and not tagged_block.spans:
                if text_block is None:
                    continue
                translated_text = self._resolve_plain_block_translation(span_map)
                if not translated_text:
                    translated_text = tagged_block.plain_text
                text_block.translated_text = translated_text
                text_block.translation_mode = "block"
                continue
            if tagged_block.plain_text is not None and tagged_block.spans:
                translated_text = self._resolve_plain_block_translation(span_map)
                if not translated_text:
                    translated_text = tagged_block.plain_text
                self._apply_plain_span_group_translation(
                    tagged_block,
                    translated_text,
                    span_lookup,
                    target_language,
                )
                continue
            for tagged_span in tagged_block.spans:
                translated_text = span_map.get(tagged_span.marker)
                if translated_text is None and len(tagged_block.spans) == 1:
                    translated_text = span_map.get(self._plain_span_key)
                if translated_text is None:
                    continue
                original_block_id = tagged_span.original_block_id
                if original_block_id is None:
                    original_block_id = tagged_block.original_block_id
                text_span = span_lookup.get(
                    (tagged_block.page_index, original_block_id, tagged_span.original_span_id)
                )
                if text_span is None:
                    continue
                if getattr(text_span, "is_preserved", False):
                    continue
                text_span.translated_text = translated_text
                text_span.adjusted_font_size = self._calculate_adjusted_font_size(
                    text_span, translated_text, self.config, target_language
                )

    def _apply_plain_span_group_translation(
        self,
        tagged_block: TaggedTextBlock,
        translated_text: str,
        span_lookup: Dict[Tuple[int, int, int], TextSpan],
        target_language: str,
    ) -> None:
        """将一个逻辑单元格的整句译文写回首个 span，其余碎片不再绘制。"""

        target_span: TextSpan | None = None
        skipped_spans: list[TextSpan] = []
        for tagged_span in tagged_block.spans:
            original_block_id = tagged_span.original_block_id
            if original_block_id is None:
                original_block_id = tagged_block.original_block_id
            text_span = span_lookup.get(
                (tagged_block.page_index, original_block_id, tagged_span.original_span_id)
            )
            if text_span is None or getattr(text_span, "is_preserved", False):
                continue
            if target_span is None:
                target_span = text_span
            else:
                skipped_spans.append(text_span)

        if target_span is None:
            return

        target_span.translated_text = translated_text
        target_span.adjusted_font_size = self._calculate_adjusted_font_size(
            target_span, translated_text, self.config, target_language
        )
        for span in skipped_spans:
            span.translated_text = ""
            span.adjusted_font_size = None

    def _extract_block_refs_from_chunk(self, chunk_text: str) -> Iterable[Tuple[int, str]]:
        """解析 chunk 文本中的页面与块标识。"""
        current_page: int | None = None
        for raw_line in chunk_text.splitlines():
            line = raw_line.strip()
            page_match = self._page_line_pattern.match(line)
            if page_match:
                current_page = int(page_match.group(1))
                continue
            block_match = self._block_line_pattern.match(line)
            if block_match and current_page is not None:
                block_marker = f"[{block_match.group(1)}]"
                yield current_page, block_marker

    def _parse_translation_text(self, translation: str) -> Dict[Tuple[int, str], Dict[str, str]]:
        """解析单个翻译结果，返回块内 span 翻译映射。"""
        results: Dict[Tuple[int, str], Dict[str, str]] = {}
        current_page: int | None = None
        current_block_key: Tuple[int, str] | None = None
        current_lines: List[str] = []

        def flush_current_block() -> None:
            nonlocal current_block_key, current_lines
            if current_block_key is None:
                return
            block_content = "\n".join(current_lines).strip()
            results[current_block_key] = self._parse_block_translation_content(block_content)
            current_block_key = None
            current_lines = []

        for raw_line in translation.splitlines():
            line = raw_line.strip()
            page_match = self._page_line_pattern.match(line)
            if page_match:
                flush_current_block()
                current_page = int(page_match.group(1))
                continue
            block_match = self._block_line_pattern.match(line)
            if block_match and current_page is not None:
                flush_current_block()
                block_marker = f"[{block_match.group(1)}]"
                trailing = line[block_match.end() :]
                current_block_key = (current_page, block_marker)
                current_lines = [trailing] if trailing else []
                continue
            if current_block_key is not None and line:
                current_lines.append(line)
        flush_current_block()
        return results

    def _parse_block_translation_content(self, block_content: str) -> Dict[str, str]:
        """解析单个 block；结构异常时返回空映射，避免不完整结果参与回写。"""

        return self._parse_strict_block_translation_content(block_content) or {}

    def _resolve_plain_block_translation(self, span_map: Dict[str, str]) -> str | None:
        """为无 SPAN 的 block 提取可用译文，兼容模型误加的 span 包装。"""
        plain_text = span_map.get(self._plain_span_key)
        if plain_text:
            if self._contains_span_tag_fragment(plain_text):
                return None
            return plain_text
        if not span_map:
            return None

        ordered_items = sorted(
            span_map.items(),
            key=lambda item: self._span_sort_key(item[0]),
        )
        merged = "".join(text for _, text in ordered_items).strip()
        return merged or None

    @staticmethod
    def _span_sort_key(span_marker: str) -> tuple[int, str]:
        match = re.match(r"SPAN_(\d+)$", span_marker)
        if match:
            return int(match.group(1)), span_marker
        return 10**9, span_marker

    @staticmethod
    def _contains_span_tag_fragment(text: str) -> bool:
        return "<SPAN_" in text or "</SPAN_" in text

    @staticmethod
    def _contains_chunk_marker_fragment(text: str) -> bool:
        return "[PAGE_" in text or "[BLOCK_" in text

    @staticmethod
    def _calculate_weighted_length(text: str) -> float:
        """计算文本的加权长度，考虑不同字符的实际宽度。"""
        if not text:
            return 0.0

        weighted_length = 0.0
        for char in text:
            codepoint = ord(char)
            if (
                0x4E00 <= codepoint <= 0x9FFF
                or 0x3400 <= codepoint <= 0x4DBF
                or 0x20000 <= codepoint <= 0x2A6DF
                or 0x2A700 <= codepoint <= 0x2B73F
                or 0x2B740 <= codepoint <= 0x2B81F
                or 0x2B820 <= codepoint <= 0x2CEAF
                or 0x2CEB0 <= codepoint <= 0x2EBEF
                or 0x30000 <= codepoint <= 0x3134F
            ):
                weighted_length += 1.0
            elif 0x3040 <= codepoint <= 0x309F or 0x30A0 <= codepoint <= 0x30FF or 0x31F0 <= codepoint <= 0x31FF:
                weighted_length += 1.0
            elif 0xFF66 <= codepoint <= 0xFF9D:
                weighted_length += 0.5
            elif (
                0x1100 <= codepoint <= 0x11FF
                or 0x3130 <= codepoint <= 0x318F
                or 0xA960 <= codepoint <= 0xA97F
                or 0xAC00 <= codepoint <= 0xD7A3
                or 0xD7B0 <= codepoint <= 0xD7FF
            ):
                weighted_length += 1.0
            elif (
                0x0400 <= codepoint <= 0x04FF
                or 0x0500 <= codepoint <= 0x052F
                or 0x2DE0 <= codepoint <= 0x2DFF
                or 0xA640 <= codepoint <= 0xA69F
                or 0x1C80 <= codepoint <= 0x1C8F
            ):
                weighted_length += 0.58
            elif 0x0980 <= codepoint <= 0x09FF:
                weighted_length += 0.6
            elif 0x1780 <= codepoint <= 0x17FF or 0x19E0 <= codepoint <= 0x19FF:
                weighted_length += 0.7
            elif (
                0x0041 <= codepoint <= 0x005A
                or 0x0061 <= codepoint <= 0x007A
                or 0x0030 <= codepoint <= 0x0039
                or 0x00C0 <= codepoint <= 0x024F
                or 0x1E00 <= codepoint <= 0x1EFF
            ):
                weighted_length += 0.52
            elif codepoint == 0x0020:
                weighted_length += 0.25
            elif codepoint == 0x3000:
                weighted_length += 1.0
            elif 0xFF01 <= codepoint <= 0xFF5E:
                weighted_length += 1.0
            elif 0xFF66 <= codepoint <= 0xFF9D:
                weighted_length += 0.5
            elif 0x3000 <= codepoint <= 0x303F:
                weighted_length += 0.8
            elif 0xFF5F <= codepoint <= 0xFF65 or 0xFF9E <= codepoint <= 0xFFEF:
                weighted_length += 0.8
            elif (
                0x0021 <= codepoint <= 0x002F
                or 0x003A <= codepoint <= 0x0040
                or 0x005B <= codepoint <= 0x0060
                or 0x007B <= codepoint <= 0x007E
            ):
                weighted_length += 0.3
            else:
                weighted_length += 1.0
        return weighted_length

    @classmethod
    def _calculate_adjusted_font_size(
        cls,
        span: TextSpan,
        translated_text: str,
        config: TranslatorConfig,
        target_language: str | None,
    ) -> float | None:
        """根据译文长度对字体大小进行智能缩放，避免溢出。"""
        if getattr(span, "is_preserved", False):
            return span.font_size
        base_size = span.font_size
        if base_size is None:
            return None
        original_text = (span.text or "").strip()
        translated_text = translated_text.strip()
        if not original_text or not translated_text:
            return base_size

        if config.use_weighted_length:
            original_len = cls._calculate_weighted_length(original_text)
            translated_len = cls._calculate_weighted_length(translated_text)
        else:
            original_len = len(original_text)
            translated_len = len(translated_text)

        if translated_len <= 0:
            return base_size
        if translated_len <= original_len:
            return base_size

        ratio = original_len / translated_len
        scaled_size = base_size * ratio

        if target_language and target_language in config.language_scale_factors:
            language_factor = config.language_scale_factors[target_language]
            scaled_size *= language_factor

        if config.adaptive_min_ratio and translated_len > original_len * 2:
            length_expansion = translated_len / original_len
            if length_expansion >= 4.0:
                adaptive_ratio = 0.25
            elif length_expansion >= 3.0:
                adaptive_ratio = 0.30
            elif length_expansion >= 2.5:
                adaptive_ratio = 0.35
            else:
                adaptive_ratio = 0.38
            min_ratio = adaptive_ratio
        else:
            min_ratio = config.min_font_ratio

        min_size_by_ratio = base_size * min_ratio
        min_size_absolute = config.min_font_size
        min_size = max(min_size_by_ratio, min_size_absolute)
        return max(scaled_size, min_size)
