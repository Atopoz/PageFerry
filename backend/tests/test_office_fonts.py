"""验证 Office 写回只替换不能覆盖目标脚本的 run 字体。"""

from modules.translation.office_fonts import (
    normalize_language_code,
    resolve_office_fallback_font,
    resolve_office_script_slot,
)


def test_language_aliases_keep_chinese_variants_distinct() -> None:
    """legacy 中文别名必须归一化, 同时不能混淆 SC、TC 与 HK。"""

    assert normalize_language_code("zh") == "zh-CN"
    assert normalize_language_code("zh-Hant") == "zh-TW"
    assert normalize_language_code("zh_hk") == "zh-HK"


def test_chinese_compatible_source_font_is_preserved() -> None:
    """宋体能渲染简体中文, 不应仅因字体名不是 preset 就被替换。"""

    assert (
        resolve_office_fallback_font(
            target_language="zh-CN",
            translated_text="网络部署服务合同",
            current_font_name="宋体",
        )
        is None
    )


def test_chinese_incompatible_source_font_uses_preset() -> None:
    """纯 Latin 字体不能覆盖中文时使用固定的语种 preset。"""

    assert (
        resolve_office_fallback_font(
            target_language="zh-CN",
            translated_text="网络部署服务合同",
            current_font_name="Times New Roman",
        )
        == "Noto Sans SC"
    )


def test_chinese_fallback_without_source_font_uses_preset() -> None:
    """来源字体未知时使用 preset, 避免把不可验证字体留给写回阶段。"""

    assert (
        resolve_office_fallback_font(
            target_language="zh-CN",
            translated_text="简体中文",
            current_font_name=None,
        )
        == "Noto Sans SC"
    )


def test_fallback_ignores_text_without_target_script() -> None:
    """纯 ASCII 品牌名不能因为目标语言是中文就被无意义改字体。"""

    assert (
        resolve_office_fallback_font(
            target_language="zh-CN",
            translated_text="DeepSeek API",
            current_font_name="Aptos",
        )
        is None
    )


def test_japanese_compatible_source_font_is_preserved() -> None:
    """兼容日文的 Hiragino Sans 不应被强制换成 Yu Gothic。"""

    assert (
        resolve_office_fallback_font(
            target_language="ja",
            translated_text="翻訳結果です。",
            current_font_name="Hiragino Sans",
        )
        is None
    )


def test_japanese_kanji_only_text_uses_east_asian_slot() -> None:
    """纯 Kanji 日文没有 Kana 时也必须识别为 East Asian script。"""

    assert (
        resolve_office_script_slot(
            target_language="ja",
            translated_text="契約条件変更",
        )
        == "east_asian"
    )


def test_english_ascii_in_symbol_font_uses_preset() -> None:
    """English ASCII 不能因缺少 Latin Extended 字符而漏过 Wingdings。"""

    assert (
        resolve_office_fallback_font(
            target_language="en",
            translated_text="Contract terms",
            current_font_name="Wingdings",
        )
        == "Calibri"
    )


def test_english_ascii_in_cjk_font_is_preserved() -> None:
    """SimSun 能渲染 basic Latin, 不能仅因它是 CJK 字体就替换。"""

    assert (
        resolve_office_fallback_font(
            target_language="en",
            translated_text="Contract terms",
            current_font_name="SimSun",
        )
        is None
    )
