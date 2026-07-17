"""测试目录树符号的字体路由。

运行方式:
    pytest tests/pdf_translation/test_font_manager_symbol_routing.py
    python tests/pdf_translation/test_font_manager_symbol_routing.py
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from modules.pdf.font_manager import (
    PDF_FONT_DIRECTORY_MISSING,
    PDF_FONT_RESOURCE_MISSING,
    FontLanguage,
    PdfFontResourceError,
    _infer_character_language,
    build_font_subsets_for_texts,
    parse_target_language,
    resolve_font_path,
    segment_text,
)

TREE_BRANCH = "\u251c\u2500\u2500"
TREE_PIPE = "\u2502"
TREE_LAST = "\u2514\u2500\u2500"


def test_line_art_char_is_routed_to_cjk_font() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    assert _infer_character_language("\u251c") == FontLanguage.CHINESE
    assert _infer_character_language(TREE_PIPE) == FontLanguage.CHINESE
    assert _infer_character_language("\u2514") == FontLanguage.CHINESE


def test_segment_text_splits_line_art_and_english() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    text = f"src {TREE_BRANCH} app {TREE_LAST} tests"
    segments = segment_text(text)
    assert segments
    assert [lang for _, lang in segments] == [
        FontLanguage.ENGLISH,
        FontLanguage.CHINESE,
        FontLanguage.ENGLISH,
        FontLanguage.CHINESE,
        FontLanguage.ENGLISH,
    ]
    assert segments[1][0] == f"{TREE_BRANCH} "
    assert segments[3][0] == f"{TREE_LAST} "


def test_cyrillic_char_is_routed_to_russian_font() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    assert _infer_character_language("\u0416") == FontLanguage.RUSSIAN


def test_segment_text_splits_english_and_russian() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    text = "CPU \u043f\u0430\u043c\u044f\u0442\u044c"
    segments = segment_text(text)
    assert segments
    assert [lang for _, lang in segments] == [FontLanguage.ENGLISH, FontLanguage.RUSSIAN]


def test_parse_target_language_maps_western_and_russian_codes() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    assert parse_target_language("zh-CN") == FontLanguage.CHINESE
    assert parse_target_language("zh-TW") == FontLanguage.CHINESE_TAIWAN
    assert parse_target_language("zh-HK") == FontLanguage.CHINESE_HONG_KONG
    assert parse_target_language("fr") == FontLanguage.ENGLISH
    assert parse_target_language("de") == FontLanguage.ENGLISH
    assert parse_target_language("es") == FontLanguage.ENGLISH
    assert parse_target_language("ru") == FontLanguage.RUSSIAN


def test_build_font_subsets_ignores_target_language_for_common_prefix() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    texts = ["2024年"]
    with (
        patch(
            "modules.pdf.font_manager.resolve_font_path",
            return_value=Path("/tmp/dummy.ttf"),
        ),
        patch(
            "modules.pdf.font_manager.build_font_subset",
            return_value=object(),
        ),
    ):
        subsets_for_zh = build_font_subsets_for_texts(texts, target_language=FontLanguage.CHINESE)
        subsets_for_ru = build_font_subsets_for_texts(texts, target_language=FontLanguage.RUSSIAN)

    expected_languages = {FontLanguage.ENGLISH, FontLanguage.CHINESE}
    assert set(subsets_for_zh.keys()) == expected_languages
    assert set(subsets_for_ru.keys()) == expected_languages


def test_build_font_subsets_uses_target_chinese_variant_font_for_cjk_text() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    resolved_languages: list[FontLanguage] = []
    resolved_directories: list[Path] = []

    def fake_resolve_font_path(
        language: FontLanguage,
        *,
        bold: bool = False,
        font_directory: str | Path | None = None,
    ) -> Path:
        """构造这个 PDF parity test 需要的替代数据。"""
        del bold
        resolved_languages.append(language)
        assert font_directory is not None
        resolved_directories.append(Path(font_directory))
        return Path(f"/tmp/{language.value}.ttf")

    with (
        patch(
            "modules.pdf.font_manager.resolve_font_path",
            side_effect=fake_resolve_font_path,
        ),
        patch(
            "modules.pdf.font_manager.build_font_subset",
            return_value=object(),
        ),
    ):
        subsets = build_font_subsets_for_texts(
            ["產品名稱"],
            font_directory=Path("/tmp/app-data/pdf-fonts"),
            target_language=FontLanguage.CHINESE_HONG_KONG,
        )

    assert set(subsets.keys()) == {FontLanguage.CHINESE}
    assert resolved_languages == [FontLanguage.CHINESE_HONG_KONG]
    assert resolved_directories == [Path("/tmp/app-data/pdf-fonts")]


def test_resolve_font_path_uses_injected_runtime_directory(tmp_path: Path) -> None:
    """字体只能从显式注入的 runtime 目录解析。"""

    font_directory = tmp_path / "app-data" / "pdf-fonts"
    font_directory.mkdir(parents=True)
    regular_font = font_directory / "NotoSans-Regular.ttf"
    regular_font.write_bytes(b"font")

    assert resolve_font_path(FontLanguage.ENGLISH, font_directory=font_directory) == regular_font
    # 首版可不分发粗体。原有路由仍会回退同语种 Regular。
    assert (
        resolve_font_path(
            FontLanguage.ENGLISH,
            bold=True,
            font_directory=font_directory,
        )
        == regular_font
    )


def test_resolve_font_path_rejects_missing_runtime_directory() -> None:
    """未注入 runtime 目录时返回稳定错误码。"""

    with pytest.raises(PdfFontResourceError) as raised:
        resolve_font_path(FontLanguage.CHINESE)

    assert raised.value.code == PDF_FONT_DIRECTORY_MISSING
    assert str(raised.value) == PDF_FONT_DIRECTORY_MISSING


def test_resolve_font_path_rejects_missing_font_file(tmp_path: Path) -> None:
    """runtime 目录缺少目标字体时返回字体名和稳定错误码。"""

    with pytest.raises(PdfFontResourceError) as raised:
        resolve_font_path(FontLanguage.CHINESE, font_directory=tmp_path)

    assert raised.value.code == PDF_FONT_RESOURCE_MISSING
    assert raised.value.font_name == "NotoSansSC-Regular.ttf"


if __name__ == "__main__":
    test_line_art_char_is_routed_to_cjk_font()
    test_segment_text_splits_line_art_and_english()
    test_cyrillic_char_is_routed_to_russian_font()
    test_segment_text_splits_english_and_russian()
    test_parse_target_language_maps_western_and_russian_codes()
    test_build_font_subsets_ignores_target_language_for_common_prefix()
    test_build_font_subsets_uses_target_chinese_variant_font_for_cjk_text()
