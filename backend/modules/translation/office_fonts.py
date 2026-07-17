# ruff: noqa: RUF001 -- locale aliases 必须接受用户输入的全角括号。
"""按目标脚本与来源字体能力为 DOCX/PPTX 选择 Office fallback 字体。

字体选择会直接改变 Word/PowerPoint 的 glyph metrics, 进而影响换行、行高和分页。
本 module 使用固定语种 preset, 但不会只因字体名与 preset 不同就替换:
当前 run 字体已知能覆盖目标脚本时保持原样, 只有不兼容或未知时才写入 preset。
具体的 ``w:rFonts`` 与 ``a:ea`` 写回仍由各文档 module 完成。
"""

from __future__ import annotations

import re
from typing import Literal

type OfficeScriptSlot = Literal["latin", "east_asian", "complex_script"]

_CYRILLIC_RE = re.compile(r"[\u0400-\u052F\u2DE0-\u2DFF\uA640-\uA69F]")
_LATIN_RE = re.compile(r"[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]")
_LATIN_EXTENDED_RE = re.compile(r"[\u00C0-\u024F\u1E00-\u1EFF]")
_JAPANESE_RE = re.compile(r"[\u3040-\u30FF\u31F0-\u31FF]")
_KOREAN_RE = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7AF]")
_CHINESE_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\U00020000-\U0002CEAF\U00030000-\U0003134F]")
_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")
_KHMER_RE = re.compile(r"[\u1780-\u17FF\u19E0-\u19FF]")
_VIETNAMESE_RE = re.compile(
    r"[\u0102\u0103\u00C2\u00E2\u00CA\u00EA\u00D4\u00F4\u01A0\u01A1\u01AF\u01B0"
    r"\u0110\u0111\u1EA0-\u1EFF]"
)

# symbol font 连 basic Latin 都不能按普通字母渲染, 必须无条件 fallback。
_SYMBOL_FONT_KEYWORDS: tuple[str, ...] = (
    "wingdings",
    "webdings",
    "symbol",
)

# 这些字体通常能渲染 basic Latin, 但不能据此假设覆盖 Latin Extended/Cyrillic。
_LIMITED_LATIN_FONT_KEYWORDS: tuple[str, ...] = (
    "simsun",
    "simhei",
    "fangsong",
    "kaiti",
    "dengxian",
    "microsoft yahei",
    "msyh",
    "heiti",
    "songti",
    "noto sans sc",
    "source han sans",
    "noto serif sc",
    "ms mincho",
    "ms gothic",
    "meiryo",
    "hiragino",
    "yu gothic",
    "noto sans jp",
    "malgun",
    "gulim",
    "batang",
    "dotum",
    "noto sans kr",
)

# preset 不能按执行平台随意换名字, 否则同一个源文件会因字体 metrics 不同而
# 产生不可预测的重排版。
_TARGET_FALLBACK_FONT: dict[str, str] = {
    "zh-CN": "Noto Sans SC",
    "zh-TW": "Noto Sans TC",
    "zh-HK": "Noto Sans HK",
    "en": "Calibri",
    "fr": "Calibri",
    "de": "Calibri",
    "es": "Calibri",
    "ru": "Calibri",
    "ja": "Yu Gothic",
    "ko": "Malgun Gothic",
    "bn": "Nirmala UI",
    "km": "Khmer UI",
    "vi": "Calibri",
}

# OOXML 只保存字体名, 不携带可供 Python 查询的完整 glyph coverage。这里采用
# 保守的已知 Office 字体 catalog: 命中即保留; 未知字体回到目标语种 preset。
_COMPATIBLE_FONT_HINTS: dict[str, tuple[str, ...]] = {
    "zh-CN": (
        "simsun",
        "宋体",
        "simhei",
        "黑体",
        "fangsong",
        "仿宋",
        "kaiti",
        "楷体",
        "dengxian",
        "等线",
        "microsoft yahei",
        "微软雅黑",
        "songti sc",
        "pingfang sc",
        "heiti sc",
        "stfangsong",
        "stkaiti",
        "stheiti",
        "stsong",
        "noto sans sc",
        "noto serif sc",
        "noto sans cjk sc",
        "noto serif cjk sc",
        "source han sans sc",
        "source han serif sc",
        "思源黑体",
        "思源宋体",
        "arial unicode ms",
    ),
    "zh-TW": (
        "pmingliu",
        "mingliu",
        "新細明體",
        "細明體",
        "microsoft jhenghei",
        "微軟正黑體",
        "songti tc",
        "pingfang tc",
        "heiti tc",
        "noto sans tc",
        "noto serif tc",
        "noto sans cjk tc",
        "noto serif cjk tc",
        "source han sans tc",
        "source han serif tc",
        "arial unicode ms",
    ),
    "zh-HK": (
        "mingliu_hkscs",
        "mingliu hkscs",
        "pingfang hk",
        "noto sans hk",
        "noto serif hk",
        "noto sans cjk hk",
        "noto serif cjk hk",
        "source han sans hk",
        "source han serif hk",
        "arial unicode ms",
    ),
    "ja": (
        "yu gothic",
        "yu mincho",
        "meiryo",
        "ms gothic",
        "ms mincho",
        "hiragino",
        "osaka",
        "noto sans jp",
        "noto serif jp",
        "noto sans cjk jp",
        "noto serif cjk jp",
        "source han sans jp",
        "source han serif jp",
        "游ゴシック",
        "游明朝",
        "メイリオ",
        "arial unicode ms",
    ),
    "ko": (
        "malgun",
        "gulim",
        "batang",
        "dotum",
        "apple sd gothic",
        "applemyungjo",
        "noto sans kr",
        "noto serif kr",
        "noto sans cjk kr",
        "noto serif cjk kr",
        "source han sans kr",
        "source han serif kr",
        "맑은 고딕",
        "굴림",
        "바탕",
        "돋움",
        "arial unicode ms",
    ),
    "bn": ("nirmala ui", "vrinda", "noto sans bengali", "solaimanlipi"),
    "km": ("khmer ui", "daunpenh", "noto sans khmer"),
}

_LANGUAGE_ALIASES: dict[str, str] = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh_hans": "zh-CN",
    "chinese": "zh-CN",
    "中文": "zh-CN",
    "简体中文": "zh-CN",
    "簡體中文": "zh-CN",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh_hant": "zh-TW",
    "繁体中文台湾": "zh-TW",
    "繁體中文台灣": "zh-TW",
    "繁体中文（台湾）": "zh-TW",
    "繁體中文（台灣）": "zh-TW",
    "traditional chinese taiwan": "zh-TW",
    "traditional chinese (taiwan)": "zh-TW",
    "zh-hk": "zh-HK",
    "zh_hk": "zh-HK",
    "繁体中文香港": "zh-HK",
    "繁體中文香港": "zh-HK",
    "繁体中文（香港）": "zh-HK",
    "繁體中文（香港）": "zh-HK",
    "traditional chinese hong kong": "zh-HK",
    "traditional chinese (hong kong)": "zh-HK",
    "english": "en",
    "英语": "en",
    "french": "fr",
    "francais": "fr",
    "français": "fr",
    "法语": "fr",
    "法文": "fr",
    "german": "de",
    "deutsch": "de",
    "德语": "de",
    "德文": "de",
    "spanish": "es",
    "español": "es",
    "espanol": "es",
    "西班牙语": "es",
    "西班牙文": "es",
    "russian": "ru",
    "русский": "ru",
    "俄语": "ru",
    "俄文": "ru",
    "japanese": "ja",
    "日语": "ja",
    "日文": "ja",
    "korean": "ko",
    "韩语": "ko",
    "韩文": "ko",
    "bengali": "bn",
    "孟加拉语": "bn",
    "孟加拉文": "bn",
    "khmer": "km",
    "cambodian": "km",
    "高棉语": "km",
    "高棉文": "km",
    "柬埔寨语": "km",
    "柬埔寨文": "km",
    "vietnamese": "vi",
    "越南语": "vi",
    "越南文": "vi",
}


def normalize_language_code(language: str | None) -> str:
    """把常见语言名与 locale 别名归一化为内部语言代码。"""

    normalized = (language or "").strip().lower()
    if not normalized:
        return ""
    return _LANGUAGE_ALIASES.get(normalized, normalized)


def resolve_office_fallback_font(
    *,
    target_language: str | None,
    translated_text: str,
    current_font_name: str | None,
) -> str | None:
    """当前字体不能覆盖译文脚本时返回目标语种 preset, 否则返回 None。"""

    normalized_language = normalize_language_code(target_language)
    fallback_font = _TARGET_FALLBACK_FONT.get(normalized_language)
    if fallback_font is None:
        return None

    text = (translated_text or "").strip()
    script_slot = resolve_office_script_slot(
        target_language=normalized_language,
        translated_text=text,
    )
    if script_slot is None:
        return None

    if script_slot == "latin":
        if current_font_name and not _needs_latin_fallback(current_font_name, text):
            return None
        return fallback_font

    if script_slot in {"east_asian", "complex_script"}:
        return _fallback_for_script_font(
            normalized_language,
            current_font_name,
            fallback_font,
        )

    return None


def resolve_office_script_slot(
    *,
    target_language: str | None,
    translated_text: str,
) -> OfficeScriptSlot | None:
    """按目标语种与译文字符确定应检查的 Office script slot。"""

    language = normalize_language_code(target_language)
    text = (translated_text or "").strip()
    if not text:
        return None

    if language in {"zh-CN", "zh-TW", "zh-HK"}:
        return "east_asian" if _CHINESE_RE.search(text) else None
    if language == "ja":
        # 日文译文可以只有 Kanji, 不能只靠 Hiragana/Katakana 判断 a:ea/w:eastAsia。
        return "east_asian" if _JAPANESE_RE.search(text) or _CHINESE_RE.search(text) else None
    if language == "ko":
        # 韩文偶尔会保留 Hanja, 与日文纯 Kanji 一样仍属于 East Asian slot。
        return "east_asian" if _KOREAN_RE.search(text) or _CHINESE_RE.search(text) else None
    if language == "ru":
        return "latin" if _CYRILLIC_RE.search(text) else None
    if language in {"en", "fr", "de", "es"}:
        return "latin" if _LATIN_RE.search(text) else None
    if language == "vi":
        return "latin" if _LATIN_RE.search(text) or _VIETNAMESE_RE.search(text) else None
    if language == "bn":
        return "complex_script" if _BENGALI_RE.search(text) else None
    if language == "km":
        return "complex_script" if _KHMER_RE.search(text) else None
    return None


def _fallback_for_script_font(
    language: str,
    current_font_name: str | None,
    fallback_font: str,
) -> str | None:
    """用已知字体 catalog 判断当前字体是否能覆盖目标脚本。"""

    if _is_compatible_script_font(language, current_font_name):
        return None
    return fallback_font


def _is_compatible_script_font(language: str, font_name: str | None) -> bool:
    """按字体名识别已知能渲染目标脚本的 Office 字体。"""

    if not font_name or not font_name.strip():
        return False
    normalized = font_name.strip().casefold()
    hints = _COMPATIBLE_FONT_HINTS.get(language, ())
    return any(hint.casefold() in normalized for hint in hints)


def _needs_latin_fallback(font_name: str, translated_text: str) -> bool:
    """按译文范围判断当前字体是否需要 Latin script fallback。"""

    normalized = font_name.strip().lower()
    if not normalized:
        return True
    if any(keyword in normalized for keyword in _SYMBOL_FONT_KEYWORDS):
        return True
    needs_extended_coverage = bool(
        _LATIN_EXTENDED_RE.search(translated_text) or _CYRILLIC_RE.search(translated_text)
    )
    if not needs_extended_coverage:
        return False
    return any(keyword in normalized for keyword in _LIMITED_LATIN_FONT_KEYWORDS)
