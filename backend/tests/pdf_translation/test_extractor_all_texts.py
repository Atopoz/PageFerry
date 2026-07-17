"""Paragraph extractor figure-text configuration tests."""

from __future__ import annotations

from PIL import Image

from modules.pdf.entities import DocumentLayout
from modules.pdf.extractor import ParagraphExtractor


class _StubLayoutDetector:
    """提供 _StubLayoutDetector 使用的 PDF parity test 替身。"""

    async def detect_layout_batch(self, input_images):
        """返回这个 PDF parity test 约定的替代结果。"""
        return [DocumentLayout(page_index=i, layouts=[]) for i in range(len(input_images))]


class _StubImageConverter:
    """提供 _StubImageConverter 使用的 PDF parity test 替身。"""

    def __init__(self, pdf_path: str) -> None:
        """保存这个测试替身需要的状态。"""
        self.pdf_path = pdf_path

    def convert_pdf_to_images(self):
        """返回这个 PDF parity test 约定的替代结果。"""
        return [type("_ImageInfo", (), {"image": Image.new("RGB", (8, 8), "white")})()]


def test_extract_page_info_enables_all_texts_for_figures(monkeypatch) -> None:
    """锁定该 PDF 场景的兼容行为。"""
    captured = {}

    def fake_extract_pages(pdf_path, laparams=None):
        """构造这个 PDF parity test 需要的替代数据。"""
        captured["pdf_path"] = pdf_path
        captured["all_texts"] = getattr(laparams, "all_texts", None)
        return [[]]

    monkeypatch.setattr(
        "modules.pdf.extractor.extract_pages",
        fake_extract_pages,
    )

    extractor = ParagraphExtractor(
        "dummy.pdf",
        layout_detector=_StubLayoutDetector(),
        pdf_to_image_converter_cls=_StubImageConverter,
    )
    monkeypatch.setattr(extractor, "_extract_all_textlines", lambda page: [])

    import asyncio

    pages = asyncio.run(extractor.extract_page_info_with_layout())

    assert captured["pdf_path"] == "dummy.pdf"
    assert captured["all_texts"] is True
    assert len(pages) == 1
