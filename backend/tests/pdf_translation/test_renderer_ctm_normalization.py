"""Paragraph renderer CTM normalization tests.

Run:
    pytest tests/pdf_translation/test_renderer_ctm_normalization.py
    python tests/pdf_translation/test_renderer_ctm_normalization.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pikepdf
import pytest

from modules.pdf.entities import BBox, PageInfo, TextBlock, TextSpan
from modules.pdf.renderer import FillOptions, ParagraphRenderer


def _build_renderer_with_blank_pdf() -> tuple[ParagraphRenderer, str]:
    """构造这个 PDF parity test 需要的替代数据。"""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
        temp_path = temp_file.name
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(100, 100))
    pdf.save(temp_path)
    pdf.close()
    return ParagraphRenderer(temp_path), temp_path


def test_append_stream_normalizes_residual_page_ctm() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    renderer, pdf_path = _build_renderer_with_blank_pdf()
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            page = pdf.pages[0]
            page.Contents = pikepdf.Stream(pdf, b"0.25 0 0 -0.25 0 100 cm\n")

            appended = pikepdf.Stream(pdf, b"BT\n1 0 0 1 10 20 Tm\nET\n")
            renderer._append_stream(pdf, page, appended)

            assert isinstance(page.Contents, pikepdf.Array)
            appended_stream = page.Contents[1]
            data = appended_stream.read_bytes()
            assert b"4 0 0 -4" in data
            assert b"400 cm" in data
            assert b"1 0 0 1 10 20 Tm" in data
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def test_append_preserved_ops_to_xobject_normalizes_residual_ctm() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    renderer, pdf_path = _build_renderer_with_blank_pdf()
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            page = pdf.pages[0]
            form_stream = pikepdf.Stream(pdf, b"0.5 0 0 -0.5 0 50 cm\n")
            form_stream[pikepdf.Name("/Type")] = pikepdf.Name("/XObject")
            form_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/Form")
            form_stream[pikepdf.Name("/BBox")] = pikepdf.Array([0, 0, 100, 100])
            form_ref = pdf.make_indirect(form_stream)

            page.Resources = pikepdf.Dictionary(
                {
                    "/XObject": pikepdf.Dictionary(
                        {
                            "/FX1": form_ref,
                        }
                    )
                }
            )

            renderer._append_preserved_ops_to_xobject(
                pdf,
                page,
                ("FX1",),
                ["BT", "1 0 0 1 10 20 Tm", "ET"],
            )

            data = form_stream.read_bytes()
            assert b"2 0 0 -2" in data
            assert b"100 cm" in data
            assert b"1 0 0 1 10 20 Tm" in data
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def test_build_translation_stream_normalizes_page_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁定该 PDF 场景的兼容行为。"""
    renderer, pdf_path = _build_renderer_with_blank_pdf()
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            page = pdf.pages[0]
            page.Rotate = 90

            monkeypatch.setattr(
                renderer,
                "_build_translation_only_commands",
                lambda *args, **kwargs: ["BT\n1 0 0 1 10 20 Tm\nET\n"],
            )

            stream = renderer._build_translation_stream(
                pdf,
                PageInfo(
                    page_index=0,
                    texts=[
                        TextBlock(
                            block_id=0,
                            bbox=BBox(10, 20, 30, 40),
                            text="hello",
                            spans=[
                                TextSpan(
                                    span_id=0,
                                    bbox=BBox(10, 20, 30, 40),
                                    text="hello",
                                    translated_text="world",
                                )
                            ],
                        )
                    ],
                    preserved_texts=[],
                ),
                FillOptions(),
                {object(): pikepdf.Name("/FTR_TEST")},
                {object(): object()},
                page=page,
            )

            assert stream is not None
            data = stream.read_bytes()
            assert b"0 1 -1 0 100 0 cm" in data
            assert b"1 0 0 1 10 20 Tm" in data
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def test_remove_existing_text_keeps_untracked_xobject_text() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    renderer, pdf_path = _build_renderer_with_blank_pdf()
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            page = pdf.pages[0]
            page.Contents = pikepdf.Stream(pdf, b"/FX1 Do\n")
            form_stream = pikepdf.Stream(pdf, b"BT\n1 0 0 1 10 20 Tm\n(keep me) Tj\nET\n")
            form_stream[pikepdf.Name("/Type")] = pikepdf.Name("/XObject")
            form_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/Form")
            form_stream[pikepdf.Name("/BBox")] = pikepdf.Array([0, 0, 100, 100])
            form_ref = pdf.make_indirect(form_stream)
            page.Resources = pikepdf.Dictionary(
                {
                    "/XObject": pikepdf.Dictionary({"/FX1": form_ref}),
                }
            )

            pages = [
                PageInfo(
                    page_index=0,
                    texts=[
                        TextBlock(
                            block_id=0,
                            bbox=BBox(0, 0, 10, 10),
                            text="hello",
                            spans=[TextSpan(span_id=0, bbox=BBox(0, 0, 10, 10), text="hello")],
                        )
                    ],
                    preserved_texts=[],
                )
            ]

            renderer._remove_existing_text(pdf, pages)

            assert b"BT" in form_stream.read_bytes()
            assert b"(keep me) Tj" in form_stream.read_bytes()
    finally:
        Path(pdf_path).unlink(missing_ok=True)


def test_remove_existing_text_clears_tracked_xobject_text() -> None:
    """锁定该 PDF 场景的兼容行为。"""
    renderer, pdf_path = _build_renderer_with_blank_pdf()
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            page = pdf.pages[0]
            page.Contents = pikepdf.Stream(pdf, b"/FX1 Do\n")
            form_stream = pikepdf.Stream(pdf, b"BT\n1 0 0 1 10 20 Tm\n(clear me) Tj\nET\n")
            form_stream[pikepdf.Name("/Type")] = pikepdf.Name("/XObject")
            form_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/Form")
            form_stream[pikepdf.Name("/BBox")] = pikepdf.Array([0, 0, 100, 100])
            form_ref = pdf.make_indirect(form_stream)
            page.Resources = pikepdf.Dictionary(
                {
                    "/XObject": pikepdf.Dictionary({"/FX1": form_ref}),
                }
            )

            pages = [
                PageInfo(
                    page_index=0,
                    texts=[
                        TextBlock(
                            block_id=0,
                            bbox=BBox(0, 0, 10, 10),
                            text="hello",
                            spans=[
                                TextSpan(
                                    span_id=0,
                                    bbox=BBox(0, 0, 10, 10),
                                    text="hello",
                                    translated_text="world",
                                    ops_xobject_paths=[("FX1",)],
                                )
                            ],
                        )
                    ],
                    preserved_texts=[],
                )
            ]

            renderer._remove_existing_text(pdf, pages)

            assert b"BT" not in form_stream.read_bytes()
            assert b"(clear me) Tj" not in form_stream.read_bytes()
    finally:
        Path(pdf_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_append_stream_normalizes_residual_page_ctm()
    test_append_preserved_ops_to_xobject_normalizes_residual_ctm()
    test_build_translation_stream_normalizes_page_rotation(pytest.MonkeyPatch())
    test_remove_existing_text_keeps_untracked_xobject_text()
    test_remove_existing_text_clears_tracked_xobject_text()
