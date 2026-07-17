from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Dict, Mapping, Sequence, TYPE_CHECKING
import logging

from fontTools import subset
from fontTools.ttLib import TTFont

from .entities import PageInfo

if TYPE_CHECKING:
    from .translator import TranslationPipelineResult

DEFAULT_FONT_NAME = "NotoSansSC-Regular.ttf"

PDF_FONT_DIRECTORY_MISSING = "pdf_font_directory_missing"
PDF_FONT_RESOURCE_MISSING = "pdf_font_resource_missing"
PDF_FONT_PREPARE_FAILED = "pdf_font_prepare_failed"


class PdfFontResourceError(RuntimeError):
    """表示 PDF runtime 字体目录或字体文件不可用。"""

    def __init__(
        self,
        code: str,
        *,
        font_name: str | None = None,
    ) -> None:
        """保存供 pipeline 稳定识别的错误码与可选字体文件名。"""

        self.code = code
        self.font_name = font_name
        message = code if font_name is None else f"{code}: {font_name}"
        super().__init__(message)


class FontLanguage(str, Enum):
    """支持的语言标识。"""

    CHINESE = "zh"
    CHINESE_TAIWAN = "zh_tw"
    CHINESE_HONG_KONG = "zh_hk"
    ENGLISH = "en"
    RUSSIAN = "ru"
    JAPANESE = "ja"
    KOREAN = "ko"
    BENGALI = "bn"
    KHMER = "km"
    VIETNAMESE = "vi"
    MATH = "math"


FONT_FILE_NAMES: Dict[FontLanguage, str] = {
    FontLanguage.CHINESE: "NotoSansSC-Regular.ttf",
    FontLanguage.CHINESE_TAIWAN: "NotoSansTC-Regular.ttf",
    FontLanguage.CHINESE_HONG_KONG: "NotoSansHK-Regular.ttf",
    FontLanguage.ENGLISH: "NotoSans-Regular.ttf",
    FontLanguage.RUSSIAN: "NotoSans-Regular.ttf",
    FontLanguage.JAPANESE: "NotoSansJP-Regular.ttf",
    FontLanguage.KOREAN: "NotoSansKR-Regular.ttf",
    FontLanguage.BENGALI: "NotoSansBengali-Regular.ttf",
    FontLanguage.KHMER: "NotoSansKhmer-Regular.ttf",
    FontLanguage.VIETNAMESE: "NotoSans-Regular.ttf",
    FontLanguage.MATH: "NotoSansMath-Regular.ttf",
}

FONT_FILE_NAMES_BOLD: Dict[FontLanguage, str] = {
    FontLanguage.CHINESE: "NotoSansSC-Bold.ttf",
    FontLanguage.CHINESE_TAIWAN: "NotoSansTC-Bold.ttf",
    FontLanguage.CHINESE_HONG_KONG: "NotoSansHK-Bold.ttf",
    FontLanguage.ENGLISH: "NotoSans-Bold.ttf",
    FontLanguage.RUSSIAN: "NotoSans-Bold.ttf",
    FontLanguage.JAPANESE: "NotoSansJP-Bold.ttf",
    FontLanguage.KOREAN: "NotoSansKR-Bold.ttf",
    FontLanguage.BENGALI: "NotoSansBengali-Bold.ttf",
    FontLanguage.KHMER: "NotoSansKhmer-Bold.ttf",
    FontLanguage.VIETNAMESE: "NotoSans-Bold.ttf",
    # NotoSansMath 当前仅提供常规字重，粗体场景回退到 Regular
    FontLanguage.MATH: "NotoSansMath-Regular.ttf",
}

# 屏蔽 fontTools 在子集化过程中的冗余 INFO 日志（如 Glyph names / Glyph IDs）
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)
logging.getLogger("fontTools.subset.timer").setLevel(logging.WARNING)


_VIETNAMESE_BASE_CODEPOINTS: frozenset[int] = frozenset(
    ord(char)
    for char in "ĂăÂâÊêÔôƠơƯưĐđ"
)
_VIETNAMESE_SYMBOL_CODEPOINTS: frozenset[int] = frozenset({0x20AB})  # Vietnamese Dong
_KOREAN_COMPATIBILITY_RANGES: tuple[tuple[int, int], ...] = (
    (0x3200, 0x321E),  # Enclosed CJK Letters and Months (Hangul)
    (0x3260, 0x327F),  # Enclosed CJK Letters and Months (Hangul circled)
    (0xFFA0, 0xFFDC),  # Halfwidth Hangul Jamo
)
_JAPANESE_COMPATIBILITY_RANGES: tuple[tuple[int, int], ...] = (
    (0x32D0, 0x32FE),  # Enclosed Katakana
)
_CHINESE_ADDITIONAL_RANGES: tuple[tuple[int, int], ...] = (
    (0x31350, 0x323AF),  # CJK Unified Ideographs Extension H
)
_LINE_ART_RANGES: tuple[tuple[int, int], ...] = (
    (0x2500, 0x257F),  # Box Drawing
    (0x2580, 0x259F),  # Block Elements
)
_MATH_SYMBOL_RANGES: tuple[tuple[int, int], ...] = (
    (0x0370, 0x03FF),  # Greek and Coptic
    (0x1F00, 0x1FFF),  # Greek Extended
    (0x2070, 0x209F),  # Superscripts and Subscripts
    (0x2100, 0x214F),  # Letterlike Symbols (e.g., ℝ ℕ)
    (0x2190, 0x21FF),  # Arrows
    (0x2200, 0x22FF),  # Mathematical Operators
    (0x2300, 0x23FF),  # Miscellaneous Technical
    (0x27C0, 0x27EF),  # Misc Mathematical Symbols-A
    (0x27F0, 0x27FF),  # Supplemental Arrows-A
    (0x2900, 0x297F),  # Supplemental Arrows-B
    (0x2980, 0x29FF),  # Misc Mathematical Symbols-B
    (0x2A00, 0x2AFF),  # Supplemental Mathematical Operators
    (0x2B00, 0x2BFF),  # Misc Symbols and Arrows
    (0x1D400, 0x1D7FF),  # Mathematical Alphanumeric Symbols
)
_CYRILLIC_RANGES: tuple[tuple[int, int], ...] = (
    (0x0400, 0x04FF),  # Cyrillic
    (0x0500, 0x052F),  # Cyrillic Supplement
    (0x2DE0, 0x2DFF),  # Cyrillic Extended-A
    (0xA640, 0xA69F),  # Cyrillic Extended-B
    (0x1C80, 0x1C8F),  # Cyrillic Extended-C
)
_CHINESE_VARIANT_FONT_LANGUAGES: frozenset[FontLanguage] = frozenset(
    {
        FontLanguage.CHINESE_TAIWAN,
        FontLanguage.CHINESE_HONG_KONG,
    }
)


def _codepoint_in_ranges(codepoint: int, ranges: Sequence[tuple[int, int]]) -> bool:
    """检查 codepoint 是否落在任意给定区间内。"""
    return any(start <= codepoint <= end for start, end in ranges)

@dataclass(frozen=True)
class FontSubsetData:
    """字体子集及元信息。"""

    language: FontLanguage
    font_bytes: bytes
    postscript_name: str
    char_to_cid: Dict[str, int]
    cid_to_unicode: Dict[int, str]
    cid_widths: Dict[int, int]
    default_width: int
    ascent: int
    descent: int
    cap_height: int
    bbox: tuple[int, int, int, int]
    units_per_em: int
    is_cff: bool  # True if CFF/PostScript outlines, False if TrueType


def collect_unique_span_texts(
    pages: Sequence[PageInfo],
    *,
    include_source: bool = False,
    include_preserved: bool = False,
) -> list[str]:
    """收集页面中 span 的去重文本（保持首次出现顺序）。"""
    unique_texts: list[str] = []
    seen: set[str] = set()
    for page in pages:
        if include_preserved:
            preserved_blocks = getattr(page, "preserved_texts", []) or []
            for block in preserved_blocks:
                for span in block.spans:
                    text = (span.text or "").replace("\r", "").replace("\n", "")
                    if not text or "(cid:" in text:
                        continue
                    normalized = text.strip()
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    unique_texts.append(normalized)
        for block in page.texts:
            if getattr(block, "translation_mode", "span") == "block":
                candidates: list[str] = []
                translated = getattr(block, "translated_text", "") or ""
                if include_source:
                    candidates.append(block.text)
                    if translated:
                        candidates.append(translated)
                else:
                    if translated:
                        candidates.append(translated)
                    else:
                        candidates.append(block.text)

                for text in candidates:
                    if not text:
                        continue
                    normalized = text.replace("\r", "").replace("\n", "")
                    if not normalized.strip():
                        continue
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    unique_texts.append(normalized)
                continue

            for span in block.spans:
                candidates: list[str] = []
                translated = span.translated_text or ""
                if include_source:
                    candidates.append(span.text)
                    if translated:
                        candidates.append(translated)
                else:
                    if translated:
                        candidates.append(translated)
                    else:
                        candidates.append(span.text)

                for text in candidates:
                    if not text:
                        continue
                    normalized = text.replace("\r", "").replace("\n", "")
                    if not normalized.strip():
                        continue
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    unique_texts.append(normalized)
    return unique_texts


def collect_unique_texts_from_result(result: "TranslationPipelineResult") -> list[str]:
    """基于翻译结果收集去重文本。"""
    return collect_unique_span_texts(result.pages)


def collect_unique_characters(texts: Sequence[str]) -> str:
    """聚合去重后的字符集合，保持 Unicode 排序。"""
    characters: set[str] = set()
    for text in texts:
        characters.update(text)
    if not characters:
        return ""
    return "".join(sorted(characters))


def _infer_character_language(char: str) -> FontLanguage | str:
    """根据字符判断所属语言脚本。

    返回 FontLanguage 枚举，或者 "common" (表示空格、数字、标点等通用字符)。
    """
    codepoint = ord(char)

    # Basic ASCII symbols, digits, and spaces
    if char.isspace() or (0x0020 <= codepoint <= 0x0040) or (0x005B <= codepoint <= 0x0060) or (0x007B <= codepoint <= 0x007E):
        return "common"

    # General Punctuation (like EM DASH, Smart Quotes)
    if 0x2000 <= codepoint <= 0x206F:
        return "common"

    # 目录树/线框字符（如 ├──、│、└──）路由到 CJK 字体，避免英文字体缺字
    if _codepoint_in_ranges(codepoint, _LINE_ART_RANGES):
        return FontLanguage.CHINESE

    # 数学符号与数学字母（优先匹配，避免被当作英文）
    if _codepoint_in_ranges(codepoint, _MATH_SYMBOL_RANGES):
        return FontLanguage.MATH

    # Cyrillic blocks（俄语等）
    if _codepoint_in_ranges(codepoint, _CYRILLIC_RANGES):
        return FontLanguage.RUSSIAN

    # Vietnamese-specific letters & diacritics
    if codepoint in _VIETNAMESE_BASE_CODEPOINTS:
        return FontLanguage.VIETNAMESE
    if 0x1E00 <= codepoint <= 0x1EFF:
        return FontLanguage.VIETNAMESE
    if codepoint in _VIETNAMESE_SYMBOL_CODEPOINTS:
        return FontLanguage.VIETNAMESE

    # Bengali blocks
    if 0x0980 <= codepoint <= 0x09FF:
        return FontLanguage.BENGALI

    # Khmer blocks
    if 0x1780 <= codepoint <= 0x17FF:
        return FontLanguage.KHMER
    if 0x19E0 <= codepoint <= 0x19FF:
        return FontLanguage.KHMER

    # Korean blocks
    if 0x1100 <= codepoint <= 0x11FF:
        return FontLanguage.KOREAN
    if 0x3130 <= codepoint <= 0x318F:
        return FontLanguage.KOREAN
    if 0xA960 <= codepoint <= 0xA97F:
        return FontLanguage.KOREAN
    if 0xAC00 <= codepoint <= 0xD7A3:
        return FontLanguage.KOREAN
    if 0xD7B0 <= codepoint <= 0xD7FF:
        return FontLanguage.KOREAN
    if _codepoint_in_ranges(codepoint, _KOREAN_COMPATIBILITY_RANGES):
        return FontLanguage.KOREAN

    # Japanese blocks
    if 0x3040 <= codepoint <= 0x309F:
        return FontLanguage.JAPANESE  # Hiragana
    if 0x30A0 <= codepoint <= 0x30FF:
        return FontLanguage.JAPANESE  # Katakana
    if 0x31F0 <= codepoint <= 0x31FF:
        return FontLanguage.JAPANESE  # Katakana Phonetic Extensions
    if 0xFF66 <= codepoint <= 0xFF9D:
        return FontLanguage.JAPANESE  # Halfwidth Katakana
    if _codepoint_in_ranges(codepoint, _JAPANESE_COMPATIBILITY_RANGES):
        return FontLanguage.JAPANESE

    # Chinese CJK Unified Ideographs & extensions
    if 0x3400 <= codepoint <= 0x4DBF:
        return FontLanguage.CHINESE
    if 0x4E00 <= codepoint <= 0x9FFF:
        return FontLanguage.CHINESE
    if 0x20000 <= codepoint <= 0x2A6DF:
        return FontLanguage.CHINESE
    if 0x2A700 <= codepoint <= 0x2B73F:
        return FontLanguage.CHINESE
    if 0x2B740 <= codepoint <= 0x2B81F:
        return FontLanguage.CHINESE
    if 0x2B820 <= codepoint <= 0x2CEAF:
        return FontLanguage.CHINESE
    if 0x2CEB0 <= codepoint <= 0x2EBEF:
        return FontLanguage.CHINESE
    if 0x30000 <= codepoint <= 0x3134F:
        return FontLanguage.CHINESE

    # CJK Symbols, Punctuation and Fullwidth Forms (commonly used in Chinese/Japanese)
    # 0x3000-0x303F: CJK Symbols and Punctuation (e.g. ， 。 、)
    # 0xFF00-0xFFEF: Halfwidth and Fullwidth Forms (e.g. （ ） ： ！）
    if 0x3000 <= codepoint <= 0x303F or 0xFF00 <= codepoint <= 0xFFEF:
        # Defaults to CHINESE as NotoSansSC usually has better coverage for these
        return FontLanguage.CHINESE

    if _codepoint_in_ranges(codepoint, _CHINESE_ADDITIONAL_RANGES):
        return FontLanguage.CHINESE

    return FontLanguage.ENGLISH


def infer_text_language(text: str, mode: str = "greedy") -> FontLanguage:
    """推断文本语言。

    Args:
        text: 待检测文本
        mode: "greedy" (只要有就返回) 或 "majority" (取出现频率最高)
    """
    sanitized = text.replace("\r", "").replace("\n", "")
    if not sanitized:
        return FontLanguage.CHINESE

    counts: Dict[FontLanguage, int] = {language: 0 for language in FontLanguage}
    for char in sanitized:
        if char.isspace() or not char.isalnum():
            continue
        res = _infer_character_language(char)
        if isinstance(res, FontLanguage) and res != FontLanguage.MATH:
            counts[res] += 1

    if mode == "majority":
        # 排除 ENGLISH 后，看哪个非英语种最多
        # 因为中日韩字体通常都包含英文，所以如果有明显的 CJK 字符，应优先返回 CJK
        cjk_counts = {
            lang: counts[lang]
            for lang in [
                FontLanguage.CHINESE,
                FontLanguage.JAPANESE,
                FontLanguage.KOREAN,
                FontLanguage.RUSSIAN,
                FontLanguage.BENGALI,
                FontLanguage.KHMER,
                FontLanguage.VIETNAMESE,
            ]
        }
        max_lang = max(counts, key=counts.get)
        # 如果最多的不是英语，或者英语虽多但 CJK 也有相当比例，则取 CJK
        if max_lang != FontLanguage.ENGLISH:
            return max_lang

        # 检查是否有其它强势语种（占比超过 10%）
        total_alnum = sum(counts.values()) or 1
        for lang, count in cjk_counts.items():
            if count / total_alnum > 0.1:
                return lang
        return FontLanguage.ENGLISH

    # Greedy 模式（原有逻辑，主要用于渲染时的字体选择）
    if counts[FontLanguage.VIETNAMESE] > 0: return FontLanguage.VIETNAMESE
    if counts[FontLanguage.KHMER] > 0: return FontLanguage.KHMER
    if counts[FontLanguage.BENGALI] > 0: return FontLanguage.BENGALI
    if counts[FontLanguage.RUSSIAN] > 0: return FontLanguage.RUSSIAN
    if counts[FontLanguage.KOREAN] > 0: return FontLanguage.KOREAN
    if counts[FontLanguage.JAPANESE] > 0: return FontLanguage.JAPANESE
    if counts[FontLanguage.CHINESE] > 0: return FontLanguage.CHINESE
    return FontLanguage.ENGLISH


def parse_target_language(target_lang_str: str) -> FontLanguage:
    """将目标语言字符串转换为FontLanguage枚举。

    Args:
        target_lang_str: 目标语言字符串，如 "中文"、"日文"、"韩文"、"英文"、
            "法语"、"德语"、"西班牙语"、"俄语"、"孟加拉语"、"柬埔寨语"、"越南语"

    Returns:
        对应的FontLanguage枚举值，默认返回CHINESE
    """
    lang_map = {
        "中文": FontLanguage.CHINESE,
        "简体中文": FontLanguage.CHINESE,
        "繁体中文": FontLanguage.CHINESE_TAIWAN,
        "繁體中文": FontLanguage.CHINESE_TAIWAN,
        "繁体中文（台湾）": FontLanguage.CHINESE_TAIWAN,
        "繁體中文（台灣）": FontLanguage.CHINESE_TAIWAN,
        "繁体中文（香港）": FontLanguage.CHINESE_HONG_KONG,
        "繁體中文（香港）": FontLanguage.CHINESE_HONG_KONG,
        "chinese": FontLanguage.CHINESE,
        "simplified chinese": FontLanguage.CHINESE,
        "traditional chinese": FontLanguage.CHINESE_TAIWAN,
        "traditional chinese (taiwan)": FontLanguage.CHINESE_TAIWAN,
        "traditional chinese (hong kong)": FontLanguage.CHINESE_HONG_KONG,
        "zh": FontLanguage.CHINESE,
        "zh-cn": FontLanguage.CHINESE,
        "zh_cn": FontLanguage.CHINESE,
        "zh-tw": FontLanguage.CHINESE_TAIWAN,
        "zh_tw": FontLanguage.CHINESE_TAIWAN,
        "zh-hk": FontLanguage.CHINESE_HONG_KONG,
        "zh_hk": FontLanguage.CHINESE_HONG_KONG,
        "日文": FontLanguage.JAPANESE,
        "日语": FontLanguage.JAPANESE,
        "japanese": FontLanguage.JAPANESE,
        "ja": FontLanguage.JAPANESE,
        "韩文": FontLanguage.KOREAN,
        "韩语": FontLanguage.KOREAN,
        "korean": FontLanguage.KOREAN,
        "ko": FontLanguage.KOREAN,
        "英文": FontLanguage.ENGLISH,
        "英语": FontLanguage.ENGLISH,
        "english": FontLanguage.ENGLISH,
        "en": FontLanguage.ENGLISH,
        "法文": FontLanguage.ENGLISH,
        "法语": FontLanguage.ENGLISH,
        "french": FontLanguage.ENGLISH,
        "fr": FontLanguage.ENGLISH,
        "德文": FontLanguage.ENGLISH,
        "德语": FontLanguage.ENGLISH,
        "german": FontLanguage.ENGLISH,
        "de": FontLanguage.ENGLISH,
        "西班牙文": FontLanguage.ENGLISH,
        "西班牙语": FontLanguage.ENGLISH,
        "spanish": FontLanguage.ENGLISH,
        "es": FontLanguage.ENGLISH,
        "俄文": FontLanguage.RUSSIAN,
        "俄语": FontLanguage.RUSSIAN,
        "russian": FontLanguage.RUSSIAN,
        "ru": FontLanguage.RUSSIAN,
        "孟加拉语": FontLanguage.BENGALI,
        "孟加拉文": FontLanguage.BENGALI,
        "bengali": FontLanguage.BENGALI,
        "bn": FontLanguage.BENGALI,
        "柬埔寨语": FontLanguage.KHMER,
        "柬埔寨文": FontLanguage.KHMER,
        "高棉语": FontLanguage.KHMER,
        "khmer": FontLanguage.KHMER,
        "km": FontLanguage.KHMER,
        "越南语": FontLanguage.VIETNAMESE,
        "越南文": FontLanguage.VIETNAMESE,
        "vietnamese": FontLanguage.VIETNAMESE,
        "vi": FontLanguage.VIETNAMESE,
    }
    normalized = target_lang_str.lower().strip()
    return lang_map.get(normalized, FontLanguage.CHINESE)


def _resolve_subset_font_language(language: FontLanguage, target_language: FontLanguage | None) -> FontLanguage:
    """根据目标语种选择实际用于子集化的字体语种。"""
    if language == FontLanguage.CHINESE and target_language in _CHINESE_VARIANT_FONT_LANGUAGES:
        return target_language
    return language


def resolve_font_path(
    language: FontLanguage,
    *,
    bold: bool = False,
    font_directory: str | Path | None = None,
) -> Path:
    """从显式 runtime 目录解析语言和字重对应的字体文件。"""

    if font_directory is None:
        raise PdfFontResourceError(PDF_FONT_DIRECTORY_MISSING)

    directory = Path(font_directory).expanduser().resolve()
    if not directory.is_dir():
        raise PdfFontResourceError(PDF_FONT_DIRECTORY_MISSING)

    primary_map = FONT_FILE_NAMES_BOLD if bold else FONT_FILE_NAMES
    font_name = primary_map[language]
    font_path = directory / font_name
    if font_path.is_file():
        return font_path

    # 粗体字体缺失时回退常规字体，保证渲染流程不中断。
    fallback_path = directory / FONT_FILE_NAMES[language]
    if fallback_path.is_file():
        return fallback_path

    raise PdfFontResourceError(
        PDF_FONT_RESOURCE_MISSING,
        font_name=font_name,
    )


def build_font_subsets_for_texts(
    texts: Sequence[str],
    *,
    font_paths: Mapping[FontLanguage, str | Path] | None = None,
    font_directory: str | Path | None = None,
    target_language: FontLanguage | None = None,
    bold: bool = False,
) -> Dict[FontLanguage, FontSubsetData]:
    """
    为文本集合按语言生成多个子集字体。

    Args:
        texts: 需要渲染的文本列表
        font_paths: 可选的字体路径映射
        font_directory: app-data 中的 PDF runtime 字体目录
        target_language: 目标语言（可选，当前仅为兼容调用方保留）
        bold: 是否优先构建粗体子集字体
    """
    characters_by_language: Dict[FontLanguage, set[str]] = {language: set() for language in FontLanguage}
    for text in texts:
        sanitized = text.replace("\r", "").replace("\n", "")
        if not sanitized:
            continue

        # 使用默认分段逻辑收集字符语种，保持与渲染链路一致。
        segments = segment_text(sanitized)
        for seg_text, lang in segments:
            characters_by_language[lang].update(seg_text)

    subsets: Dict[FontLanguage, FontSubsetData] = {}
    for language, characters in characters_by_language.items():
        if not characters:
            continue
        sorted_chars = "".join(sorted(characters))
        override_path = font_paths.get(language) if font_paths else None
        font_language = _resolve_subset_font_language(language, target_language)
        subset_data = build_font_subset(
            [sorted_chars],
            font_path=override_path
            or resolve_font_path(
                font_language,
                bold=bold,
                font_directory=font_directory,
            ),
            language=language,
        )
        subsets[language] = subset_data

    return subsets


def segment_text(
    text: str,
    default_language: FontLanguage = FontLanguage.ENGLISH,
) -> list[tuple[str, FontLanguage]]:
    """将文本切分为不同语言的片段。"""
    if not text:
        return []

    segments: list[tuple[str, FontLanguage]] = []
    current_text = ""
    current_lang: FontLanguage | None = None

    for char in text:
        res = _infer_character_language(char)

        # 语言决策逻辑：
        if res == "common":
            # 通用字符（空格、数字、标点）：
            # 如果已有当前语种，则维持当前语种，避免因符号导致的频繁切换和渲染碎片化。
            # 这也能解决 EM DASH (—) 等符号在西文字体中缺失或排版不一致的问题。
            lang = current_lang if current_lang is not None else default_language
        else:
            # 明确的语种（中、英、日、韩等）
            lang = res

        if current_lang is None:
            current_lang = lang
            current_text = char
        elif lang != current_lang:
            # 发生语种切换
            if current_text:
                segments.append((current_text, current_lang))
            current_text = char
            current_lang = lang
        else:
            current_text += char

    if current_text:
        segments.append((current_text, current_lang))

    return segments


def subset_font_for_texts(
    texts: Sequence[str],
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    font_directory: str | Path | None = None,
    language: FontLanguage | None = None,
) -> Path:
    """使用默认字体为指定文本生成字体子集文件。"""
    subset_data = build_font_subset(
        texts,
        font_path=font_path,
        font_directory=font_directory,
        language=language,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(subset_data.font_bytes)
    return destination


def build_font_subset(
    texts: Sequence[str],
    *,
    font_path: str | Path | None = None,
    font_directory: str | Path | None = None,
    language: FontLanguage | None = None,
) -> FontSubsetData:
    """创建字体子集并返回元信息。"""
    unique_chars = collect_unique_characters(texts)
    if not unique_chars:
        raise ValueError("缺少可用于生成子集的字符。")

    resolved_language = language or FontLanguage.CHINESE
    source_font = (
        Path(font_path)
        if font_path
        else resolve_font_path(
            resolved_language,
            font_directory=font_directory,
        )
    )
    if not source_font.is_file():
        raise PdfFontResourceError(
            PDF_FONT_RESOURCE_MISSING,
            font_name=source_font.name,
        )

    options = subset.Options()
    options.layout_features = ["*"]
    subset_font = subset.load_font(str(source_font), options)
    buffer = BytesIO()
    try:
        subsetter = subset.Subsetter(options)
        subsetter.populate(text=unique_chars)
        subsetter.subset(subset_font)
        subset.save_font(subset_font, buffer, options)
    finally:
        subset_font.close()

    font_bytes = buffer.getvalue()
    tt_font = TTFont(BytesIO(font_bytes))

    # 检测字体格式：是CFF还是TrueType
    is_cff = "CFF " in tt_font or "CFF2" in tt_font

    cmap = tt_font.getBestCmap() or {}
    glyph_order = tt_font.getGlyphOrder()

    # 对于Type 0（CID-keyed）字体，不能简单使用glyph order索引作为CID
    # 应该使用字形在字体中的实际位置
    # 但为了保持与PDF兼容，使用cmap中的原始映射
    glyph_name_to_cid = {name: idx for idx, name in enumerate(glyph_order)}

    char_to_cid: Dict[str, int] = {}
    cid_to_unicode: Dict[int, str] = {}
    cid_widths: Dict[int, int] = {}

    units_per_em = tt_font["head"].unitsPerEm
    scale_factor = 1000.0 / units_per_em
    hmtx = tt_font["hmtx"].metrics

    # 默认宽度设为更合理的 500 (半宽) 或按比例缩放
    default_width = 500

    for codepoint, glyph_name in cmap.items():
        cid = glyph_name_to_cid.get(glyph_name)
        if cid is None:
            continue
        char = chr(codepoint)
        char_to_cid[char] = cid
        unicode_hex = char.encode("utf-16-be").hex().upper()
        cid_to_unicode[cid] = unicode_hex
        width_units = hmtx.get(glyph_name, (units_per_em, 0))[0]
        # 严格按比例缩放到 1000 units/em
        width = int(round(width_units * scale_factor))
        cid_widths[cid] = width

    # 关键修复：必须将所有度量统一缩放到 1000，否则 PDF 间距会错乱
    ascent = int(round(tt_font["OS/2"].sTypoAscender * scale_factor))
    descent = int(round(tt_font["OS/2"].sTypoDescender * scale_factor))
    cap_height = int(round(getattr(tt_font["OS/2"], "sCapHeight", ascent) * scale_factor))
    head = tt_font["head"]
    bbox = (
        int(round(head.xMin * scale_factor)),
        int(round(head.yMin * scale_factor)),
        int(round(head.xMax * scale_factor)),
        int(round(head.yMax * scale_factor))
    )
    # 规范化 PostScript 名称
    raw_ps_name = tt_font["name"].getDebugName(6) or DEFAULT_FONT_NAME
    ps_name = "".join(c for c in raw_ps_name if c.isalnum() or c in "-_")
    tt_font.close()

    return FontSubsetData(
        language=resolved_language,
        font_bytes=font_bytes,
        postscript_name=ps_name,
        char_to_cid=char_to_cid,
        cid_to_unicode=cid_to_unicode,
        cid_widths=cid_widths,
        default_width=default_width,
        ascent=ascent,
        descent=descent,
        cap_height=cap_height,
        bbox=bbox,
        units_per_em=units_per_em,
        is_cff=is_cff,
    )


def subset_font_to_bytes(
    texts: Sequence[str],
    *,
    font_path: str | Path | None = None,
    font_directory: str | Path | None = None,
    language: FontLanguage | None = None,
) -> bytes:
    """生成仅驻留内存的字体子集并返回字节内容。"""
    subset_data = build_font_subset(
        texts,
        font_path=font_path,
        font_directory=font_directory,
        language=language,
    )
    return subset_data.font_bytes


def subset_font_for_pages(
    pages: Sequence[PageInfo],
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    font_directory: str | Path | None = None,
) -> Path:
    """直接基于页面数据生成子集字体。"""
    texts = collect_unique_span_texts(pages)
    return subset_font_for_texts(
        texts,
        output_path,
        font_path=font_path,
        font_directory=font_directory,
    )
