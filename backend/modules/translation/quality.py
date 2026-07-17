"""识别 model 合法返回但明显遗漏翻译的 segment, 供 provider 做一次定向 retry。"""

import re

from modules.translation.office_fonts import normalize_language_code

_POSITION_MARKER_RE = re.compile(
    r"\[(?:PARA|SLIDE|SHAPE|TABLE|CELL|ROW|NOTE|HEADER|FOOTER)[^\]]*\]",
    re.IGNORECASE,
)
_STRUCTURE_TAG_RE = re.compile(r"</?(?:span|p)>", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"⟪PF_[^⟫]+⟫")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF]")


def should_retry_unchanged_translation(
    *,
    source_text: str,
    translated_text: str,
    source_language: str | None,
    target_language: str,
) -> bool:
    """判断完全未变化的结果是否仍包含明显应翻译的自然语言。

    这里只处理高置信度情况: 英文多词标题到中文、中文正文到英文, 以及英文
    多词内容到其他显式拉丁目标。混合内容只要已含目标脚本就保守接受, 避免把
    品牌名、URL 或 identifier 当作漏译。
    """

    visible = _visible_text(source_text)
    translated_visible = _visible_text(translated_text)
    if not visible or _normalized_text(visible) != _normalized_text(translated_visible):
        return False

    source = normalize_language_code(source_language)
    target = normalize_language_code(target_language)
    if source and source == target:
        return False

    cjk_count = len(_CJK_RE.findall(visible))
    latin_words = _LATIN_WORD_RE.findall(visible)
    if target in {"zh-CN", "zh-TW", "zh-HK"}:
        return cjk_count == 0 and len(latin_words) >= 2
    if target == "en":
        return cjk_count >= 2
    if source == "en" and target in {"fr", "de", "es", "ru", "vi"}:
        return len(latin_words) >= 2
    return False


def _visible_text(text: str) -> str:
    """移除 PageFerry 结构 marker, 只保留用于质量判断的可见正文。"""

    without_markers = _POSITION_MARKER_RE.sub(" ", text)
    without_tags = _STRUCTURE_TAG_RE.sub(" ", without_markers)
    return _PLACEHOLDER_RE.sub(" ", without_tags).strip()


def _normalized_text(text: str) -> str:
    """折叠模型可能改写的排版空白, 比较实际可见文字是否变化。"""

    return " ".join(text.split())
