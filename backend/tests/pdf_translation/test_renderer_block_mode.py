"""Paragraph block rendering tests.

Run:
    pytest tests/pdf_translation/test_renderer_block_mode.py
    python tests/pdf_translation/test_renderer_block_mode.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pikepdf
import pytest

from modules.pdf import renderer as renderer_module
from modules.pdf.entities import BBox, PageInfo, TextBlock, TextSpan
from modules.pdf.font_manager import (
    PDF_FONT_PREPARE_FAILED,
    PDF_FONT_RESOURCE_MISSING,
    FontLanguage,
    FontSubsetData,
    PdfFontResourceError,
)
from modules.pdf.renderer import FillOptions, FillRenderMode, ParagraphRenderer


def _build_test_subset(
    chars: str = "abcdefghijklmnopqrstuvwxyz ",
    language: FontLanguage = FontLanguage.ENGLISH,
) -> FontSubsetData:
    """构造这个 PDF parity test 需要的替代数据。"""
    char_to_cid = {char: index + 1 for index, char in enumerate(chars)}
    cid_to_unicode = {
        index + 1: char.encode("utf-16-be").hex().upper() for index, char in enumerate(chars)
    }
    cid_widths = {index + 1: 500 for index in range(len(chars))}
    return FontSubsetData(
        language=language,
        font_bytes=b"",
        postscript_name="TestFont",
        char_to_cid=char_to_cid,
        cid_to_unicode=cid_to_unicode,
        cid_widths=cid_widths,
        default_width=500,
        ascent=800,
        descent=-200,
        cap_height=700,
        bbox=(0, 0, 1000, 1000),
        units_per_em=1000,
        is_cff=False,
    )


def test_block_mode_builds_multiline_stream() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        spans = [
            TextSpan(span_id=0, bbox=BBox(0, 0, 40, 10), text="hello", font_size=10.0),
            TextSpan(span_id=1, bbox=BBox(0, 0, 40, 10), text="world", font_size=10.0),
        ]
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 40, 40),
            text="hello world",
            spans=spans,
            layout_label="text",
            translation_mode="block",
            translated_text="hello world",
        )
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"BT" in data


def test_bilingual_stack_includes_source_and_translation_in_font_subset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """锁定该 PDF 场景的兼容行为。"""
    captured_texts: list[str] = []

    def fake_build_font_subsets_for_texts(texts, **_kwargs):
        """构造这个 PDF parity test 需要的替代数据。"""
        captured_texts.extend(texts)
        return {FontLanguage.ENGLISH: _build_test_subset("abcdefghijklmnopqrstuvwxyz ")}

    def fake_register_font(self, pdf, subset, *, resource_name=None):
        """构造这个 PDF parity test 需要的替代数据。"""
        return resource_name or pikepdf.Name("/FTR_EN"), pdf.make_indirect(pikepdf.Dictionary())

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _create_blank_pdf(source_path)
    monkeypatch.setattr(
        renderer_module, "build_font_subsets_for_texts", fake_build_font_subsets_for_texts
    )
    monkeypatch.setattr(ParagraphRenderer, "_register_font", fake_register_font)

    span = TextSpan(
        span_id=0,
        bbox=BBox(0, 0, 120, 12),
        text="source text",
        translated_text="target text",
        font_size=10.0,
    )
    page_info = PageInfo(
        page_index=0,
        texts=[
            TextBlock(
                block_id=0,
                bbox=BBox(0, 0, 120, 12),
                text="source text",
                spans=[span],
            )
        ],
        preserved_texts=[],
    )

    ParagraphRenderer(source_path).apply(
        output_path,
        [page_info],
        options=FillOptions(render_mode=FillRenderMode.BILINGUAL_STACK),
        target_language="en",
    )

    assert "source text" in captured_texts
    assert "target text" in captured_texts
    assert output_path.exists()


def test_apply_allocates_font_resource_name_when_source_pdf_already_uses_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """锁定该 PDF 场景的兼容行为。"""

    def fake_build_font_subsets_for_texts(texts, **_kwargs):
        """构造这个 PDF parity test 需要的替代数据。"""
        return {FontLanguage.ENGLISH: _build_test_subset("abc ")}

    def fake_register_font(self, pdf, subset, *, resource_name=None):
        """构造这个 PDF parity test 需要的替代数据。"""
        return resource_name or pikepdf.Name("/FTR_EN"), pdf.make_indirect(pikepdf.Dictionary())

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        page.Resources = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary(
                    {
                        "/FTR_EN": pdf.make_indirect(pikepdf.Dictionary()),
                    }
                )
            }
        )
        pdf.save(source_path)

    monkeypatch.setattr(
        renderer_module, "build_font_subsets_for_texts", fake_build_font_subsets_for_texts
    )
    monkeypatch.setattr(ParagraphRenderer, "_register_font", fake_register_font)

    span = TextSpan(
        span_id=0,
        bbox=BBox(10, 10, 80, 20),
        text="abc",
        translated_text="abc",
        font_size=10.0,
    )
    page_info = PageInfo(
        page_index=0,
        texts=[
            TextBlock(
                block_id=0,
                bbox=BBox(10, 10, 80, 20),
                text="abc",
                spans=[span],
            )
        ],
        preserved_texts=[],
    )

    ParagraphRenderer(source_path).apply(output_path, [page_info], target_language="en")

    with pikepdf.Pdf.open(output_path) as pdf:
        page = pdf.pages[0]
        font_names = {str(name) for name in page.Resources.Font}
        assert "/FTR_EN" in font_names
        assert "/FTR_EN_0" in font_names
        content_streams = (
            list(page.Contents) if isinstance(page.Contents, pikepdf.Array) else [page.Contents]
        )
        rendered_content = "\n".join(
            stream.read_bytes().decode("latin-1") for stream in content_streams
        )
        assert "/FTR_EN_0" in rendered_content


def test_bilingual_stack_draws_nearly_identical_span_once() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ")
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 120, 12),
            text="Same Text",
            translated_text="same text",
            font_size=10.0,
        )
        commands = renderer._build_bilingual_commands(
            span,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            "0.1000 0.3500 0.8000 rg",
            FillOptions(render_mode=FillRenderMode.BILINGUAL_STACK),
        )

    assert len(commands) == 1
    assert "0.0000 0.0000 0.0000 rg" in commands[0]
    assert "0.1000 0.3500 0.8000 rg" not in commands[0]


def test_bilingual_stack_draws_nearly_identical_block_with_source_color() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ")
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 120, 30),
            text="Same Text",
            spans=[TextSpan(span_id=0, bbox=BBox(0, 0, 120, 12), text="Same Text", font_size=10.0)],
            translation_mode="block",
            translated_text="same text",
        )
        commands = renderer._build_block_translation_commands(
            block,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            "0.0000 0.0000 0.0000 rg",
            "0.1000 0.3500 0.8000 rg",
            FillOptions(render_mode=FillRenderMode.BILINGUAL_STACK),
            10.0,
            {},
        )

    assert commands
    assert "0.0000 0.0000 0.0000 rg" in commands[0]
    assert "0.1000 0.3500 0.8000 rg" not in commands[0]


def test_block_mode_bilingual_stack_wraps_source_and_translation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset("abcdefghijklmnopqrstuvwxyz ")
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        commands = renderer._build_block_bilingual_commands(
            "hello world",
            "target words",
            BBox(0, 0, 42, 64),
            10.0,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            "0.1000 0.3500 0.8000 rg",
            FillOptions(render_mode=FillRenderMode.BILINGUAL_STACK),
        )

    assert len(commands) >= 2
    assert any("0.0000 0.0000 0.0000 rg" in command for command in commands)
    assert any("0.1000 0.3500 0.8000 rg" in command for command in commands)


def test_translation_stream_uses_block_dominant_color_when_enabled() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="hello",
            translated_text="hello",
            font_size=10.0,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="hello",
            spans=[span],
            layout_label="text",
            dominant_color=(0.9, 0.7, 0.1),
        )
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(reuse_block_dominant_color=True),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"0.9000 0.7000 0.1000 rg" in data


def _create_blank_pdf(path: Path) -> None:
    """构造这个 PDF parity test 需要的替代数据。"""
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(200, 200))
        pdf.save(path)


def _build_page_info(*, translated_text: str) -> PageInfo:
    """构造一个可用于 apply 失败路径的单 span 页面。"""

    span = TextSpan(
        span_id=0,
        bbox=BBox(10, 10, 80, 20),
        text="source",
        translated_text=translated_text,
        font_size=10.0,
    )
    return PageInfo(
        page_index=0,
        texts=[
            TextBlock(
                block_id=0,
                bbox=BBox(10, 10, 80, 20),
                text="source",
                spans=[span],
            )
        ],
        preserved_texts=[],
    )


def test_apply_fails_closed_when_font_preparation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字体准备异常不能被吞掉并产生空白输出。"""

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _create_blank_pdf(source_path)

    def fail_font_preparation(*_args, **_kwargs):
        """模拟字体文件损坏或子集化失败。"""

        raise OSError("broken font")

    monkeypatch.setattr(
        renderer_module,
        "build_font_subsets_for_texts",
        fail_font_preparation,
    )

    with pytest.raises(PdfFontResourceError) as raised:
        ParagraphRenderer(source_path).apply(
            output_path,
            [_build_page_info(translated_text="target")],
            target_language="en",
        )

    assert raised.value.code == PDF_FONT_PREPARE_FAILED
    assert not output_path.exists()


def test_apply_fails_closed_when_injected_runtime_font_is_missing(
    tmp_path: Path,
) -> None:
    """renderer 必须使用注入目录且在缺字体时拒绝保存。"""

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    font_directory = tmp_path / "app-data" / "pdf-fonts"
    font_directory.mkdir(parents=True)
    _create_blank_pdf(source_path)

    with pytest.raises(PdfFontResourceError) as raised:
        ParagraphRenderer(
            source_path,
            font_directory=font_directory,
        ).apply(
            output_path,
            [_build_page_info(translated_text="target")],
            target_language="en",
        )

    assert raised.value.code == PDF_FONT_RESOURCE_MISSING
    assert raised.value.font_name == "NotoSans-Regular.ttf"
    assert not output_path.exists()


def test_apply_fails_closed_when_translation_stream_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非空译文没有生成 stream 时必须中止保存。"""

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _create_blank_pdf(source_path)

    def fake_build_font_subsets_for_texts(_texts, **_kwargs):
        """返回最小字体子集以便只覆盖 stream 失败。"""

        return {FontLanguage.ENGLISH: _build_test_subset("abcdefghijklmnopqrstuvwxyz ")}

    def fake_register_font(self, pdf, subset, *, resource_name=None):
        """注册测试字体资源。"""

        del self, subset
        return resource_name or pikepdf.Name("/FTR_EN"), pdf.make_indirect(pikepdf.Dictionary())

    monkeypatch.setattr(
        renderer_module,
        "build_font_subsets_for_texts",
        fake_build_font_subsets_for_texts,
    )
    monkeypatch.setattr(ParagraphRenderer, "_register_font", fake_register_font)
    monkeypatch.setattr(
        ParagraphRenderer,
        "_build_translation_stream",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="未生成 translation stream"):
        ParagraphRenderer(source_path).apply(
            output_path,
            [_build_page_info(translated_text="target")],
            target_language="en",
        )

    assert not output_path.exists()


def test_apply_keeps_document_when_page_has_no_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法的无译文页不需要字体准备且不应被删除原文字层。"""

    source_path = tmp_path / "source.pdf"
    output_path = tmp_path / "output.pdf"
    _create_blank_pdf(source_path)
    monkeypatch.setattr(
        renderer_module,
        "build_font_subsets_for_texts",
        lambda *_args, **_kwargs: pytest.fail("无译文页不应准备字体"),
    )
    monkeypatch.setattr(
        ParagraphRenderer,
        "_remove_existing_text",
        lambda *_args, **_kwargs: pytest.fail("无译文页不应清理原文字层"),
    )

    ParagraphRenderer(source_path).apply(
        output_path,
        [_build_page_info(translated_text="")],
        target_language="en",
    )

    assert output_path.exists()


def test_translation_stream_falls_back_to_default_color_when_disabled() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="hello",
            translated_text="hello",
            font_size=10.0,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="hello",
            spans=[span],
            layout_label="text",
            dominant_color=(0.9, 0.7, 0.1),
        )
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(reuse_block_dominant_color=False),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"0.0000 0.0000 0.0000 rg" in data
        assert b"0.9000 0.7000 0.1000 rg" not in data


def test_translation_stream_uses_stroke_color_as_fill_for_stroke_only_text() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="outline",
            translated_text="outline",
            font_size=10.0,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="outline",
            spans=[span],
            layout_label="text",
            dominant_color=(1.0, 1.0, 1.0),
        )
        block.dominant_stroke_color = (0.1, 0.1, 0.1)
        block.dominant_text_render_mode = 1
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(reuse_block_dominant_color=True),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"0.1000 0.1000 0.1000 rg" in data
        assert b"0.1000 0.1000 0.1000 RG" not in data
        assert b"1 Tr" not in data


def test_translation_stream_does_not_reuse_fill_and_stroke_render_mode() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="normal",
            translated_text="normal",
            font_size=10.0,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 80, 12),
            text="normal",
            spans=[span],
            layout_label="text",
            dominant_color=(0.0, 0.0, 0.0),
        )
        block.dominant_stroke_color = (0.0, 0.0, 0.0)
        block.dominant_text_render_mode = 2
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(reuse_block_dominant_color=True),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"0.0000 0.0000 0.0000 rg" in data
        assert b"0.0000 0.0000 0.0000 RG" not in data
        assert b"2 Tr" not in data


def test_block_mode_uses_label_specific_font_size_profile() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        title_block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 200, 40),
            text="Title",
            spans=[
                TextSpan(span_id=0, bbox=BBox(0, 20, 200, 40), text="Title", font_size=18.0),
                TextSpan(span_id=1, bbox=BBox(0, 0, 200, 20), text="Subtitle", font_size=18.0),
            ],
            layout_label="paragraph_title",
            translation_mode="block",
            translated_text="Translated title",
        )
        text_block = TextBlock(
            block_id=1,
            bbox=BBox(0, 0, 200, 40),
            text="Body line one Body line two",
            spans=[
                TextSpan(
                    span_id=2, bbox=BBox(0, 20, 200, 40), text="Body line one", font_size=10.0
                ),
                TextSpan(span_id=3, bbox=BBox(0, 0, 200, 20), text="Body line two", font_size=10.0),
            ],
            layout_label="text",
            translation_mode="block",
            translated_text="Translated paragraph",
        )
        page_info = PageInfo(page_index=0, texts=[title_block, text_block], preserved_texts=[])

        page_base_font_size = renderer._compute_page_base_font_size(page_info)
        page_label_font_sizes = renderer._compute_page_label_base_font_sizes(page_info)

        assert page_base_font_size == 14.0
        assert page_label_font_sizes["paragraph_title"] == 18.0
        assert page_label_font_sizes["text"] == 10.0

        resolved_title_size = renderer._determine_block_font_size(
            title_block,
            page_base_font_size,
            page_label_font_sizes,
        )
        resolved_text_size = renderer._determine_block_font_size(
            text_block,
            page_base_font_size,
            page_label_font_sizes,
        )
        assert resolved_title_size == 18.0
        assert resolved_text_size == 10.0


def test_translation_stream_uses_bold_font_resource_for_bold_block() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    regular_subset = _build_test_subset()
    bold_subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: regular_subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    bold_font_subsets = {FontLanguage.ENGLISH: bold_subset}
    bold_font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTRB_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 120, 12),
            text="headline",
            translated_text="headline",
            font_size=10.0,
            is_bold=True,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 120, 12),
            text="headline",
            spans=[span],
            layout_label="text",
            is_bold=True,
        )
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(),
            font_resource_names,
            font_subsets,
            bold_font_resource_names=bold_font_resource_names,
            bold_font_subsets=bold_font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"/FTRB_EN" in data


def test_translation_stream_falls_back_to_regular_font_when_bold_missing() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    regular_subset = _build_test_subset()
    font_subsets = {FontLanguage.ENGLISH: regular_subset}
    font_resource_names = {FontLanguage.ENGLISH: pikepdf.Name("/FTR_EN")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        span = TextSpan(
            span_id=0,
            bbox=BBox(0, 0, 120, 12),
            text="headline",
            translated_text="headline",
            font_size=10.0,
            is_bold=True,
        )
        block = TextBlock(
            block_id=0,
            bbox=BBox(0, 0, 120, 12),
            text="headline",
            spans=[span],
            layout_label="text",
            is_bold=True,
        )
        page_info = PageInfo(page_index=0, texts=[block], preserved_texts=[])
        pdf = pikepdf.Pdf.new()
        stream = renderer._build_translation_stream(
            pdf,
            page_info,
            FillOptions(),
            font_resource_names,
            font_subsets,
        )
        assert stream is not None
        data = stream.read_bytes()
        assert b"/FTR_EN" in data


def test_resolve_line_height_factor_boosts_cjk_text() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        assert renderer._resolve_line_height_factor("hello world", 1.1) == 1.1
        assert renderer._resolve_line_height_factor("人工智能必须拥抱专业化", 1.1) == 1.32


def test_block_mode_uses_adaptive_line_height_for_cjk_translation() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    subset = _build_test_subset(
        chars="人工智能必须通过超人类自适应智能拥抱专业化 ",
        language=FontLanguage.CHINESE,
    )
    font_subsets = {FontLanguage.CHINESE: subset}
    font_resource_names = {FontLanguage.CHINESE: pikepdf.Name("/FTR_ZH")}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_file:
        renderer = ParagraphRenderer(temp_file.name)
        commands = renderer._build_block_single_language_commands(
            "人工智能必须通过超人类自适应智能拥抱专业化",
            BBox(0, 0, 60, 40),
            10.0,
            font_resource_names,
            font_subsets,
            "0.0000 0.0000 0.0000 rg",
            FillOptions(line_height_factor=1.1),
        )
        assert len(commands) >= 2

        first_font_match = re.search(r"/FTR_ZH ([0-9.]+) Tf", commands[0])
        first_y_match = re.search(r"1 0 0 1 [0-9.]+ ([0-9.]+) Tm", commands[0])
        second_y_match = re.search(r"1 0 0 1 [0-9.]+ ([0-9.]+) Tm", commands[1])

        assert first_font_match is not None
        assert first_y_match is not None
        assert second_y_match is not None

        font_size = float(first_font_match.group(1))
        delta = float(first_y_match.group(1)) - float(second_y_match.group(1))
        assert delta / font_size >= 1.31


if __name__ == "__main__":
    test_block_mode_builds_multiline_stream()
    test_translation_stream_uses_block_dominant_color_when_enabled()
    test_translation_stream_falls_back_to_default_color_when_disabled()
    test_translation_stream_uses_stroke_color_as_fill_for_stroke_only_text()
    test_translation_stream_does_not_reuse_fill_and_stroke_render_mode()
    test_block_mode_uses_label_specific_font_size_profile()
    test_translation_stream_uses_bold_font_resource_for_bold_block()
    test_translation_stream_falls_back_to_regular_font_when_bold_missing()
    test_resolve_line_height_factor_boosts_cjk_text()
    test_block_mode_uses_adaptive_line_height_for_cjk_translation()
