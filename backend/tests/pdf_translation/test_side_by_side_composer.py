"""验证 PDF 左右拼页合成器的页面几何与原子发布边界。"""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
import pytest

from modules.pdf.side_by_side_composer import PdfSideBySideComposer


def test_side_by_side_composer_keeps_page_count_and_places_pages_left_to_right(
    tmp_path: Path,
) -> None:
    """每组原文和译文页应合成一页, 并保留固定 gutter。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_blank_pdf(source, [(100, 200), (120, 150)])
    _write_blank_pdf(translated, [(90, 200), (110, 160)])

    PdfSideBySideComposer().compose(source, translated, output)

    with pikepdf.Pdf.open(output) as pdf:
        assert len(pdf.pages) == 2
        assert list(pdf.pages[0].mediabox) == [0, 0, 214, 200]
        assert list(pdf.pages[1].mediabox) == [0, 0, 254, 160]
        assert _translation_matrices(pdf.pages[0]) == [(0.0, 0.0), (124.0, 0.0)]
        assert _translation_matrices(pdf.pages[1]) == [(0.0, 5.0), (144.0, 0.0)]


def test_side_by_side_composer_rejects_mismatched_page_counts_without_output(
    tmp_path: Path,
) -> None:
    """页数无法一一对应时不能生成看似成功的双语文件。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_blank_pdf(source, [(100, 200), (100, 200)])
    _write_blank_pdf(translated, [(100, 200)])

    with pytest.raises(ValueError, match="pdf_bilingual_page_count_mismatch"):
        PdfSideBySideComposer().compose(source, translated, output)

    assert not output.exists()
    assert list(tmp_path.glob(".bilingual.pdf.*.tmp.pdf")) == []


def test_side_by_side_composer_validates_before_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构校验失败时不得 replace 目标, 也不能遗留同目录临时文件。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_blank_pdf(source, [(100, 200)])
    _write_blank_pdf(translated, [(100, 200)])
    composer = PdfSideBySideComposer()

    def reject_output(
        _path: Path,
        _expected_pages: tuple[tuple[float, float], ...],
    ) -> None:
        """模拟临时 PDF 重新打开后的结构校验失败。"""

        raise ValueError("invalid_composed_pdf")

    monkeypatch.setattr(composer, "_validate_output", reject_output)

    with pytest.raises(ValueError, match="invalid_composed_pdf"):
        composer.compose(source, translated, output)

    assert not output.exists()
    assert list(tmp_path.glob(".bilingual.pdf.*.tmp.pdf")) == []


def test_side_by_side_composer_refuses_to_overwrite_an_input(tmp_path: Path) -> None:
    """输出路径与任一输入相同时必须在创建临时文件前拒绝。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    _write_blank_pdf(source, [(100, 200)])
    _write_blank_pdf(translated, [(100, 200)])

    with pytest.raises(ValueError, match="pdf_bilingual_output_would_overwrite_input"):
        PdfSideBySideComposer().compose(source, translated, source)


def test_side_by_side_composer_uses_visible_geometry_for_rotated_pages(
    tmp_path: Path,
) -> None:
    """横向显示的旋转页不能按未旋转 MediaBox 拼接后被裁掉。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_blank_pdf(source, [(100, 200)], rotation=90)
    _write_blank_pdf(translated, [(100, 200)], rotation=90)

    PdfSideBySideComposer().compose(source, translated, output)

    with pikepdf.Pdf.open(output) as pdf:
        assert list(pdf.pages[0].mediabox) == [0, 0, 424, 100]
        assert _translation_matrices(pdf.pages[0]) == [(0.0, 0.0), (224.0, 0.0)]


def test_side_by_side_composer_normalizes_non_zero_media_box_origins(
    tmp_path: Path,
) -> None:
    """非零 MediaBox 原点必须被平移回左右两侧的预期位置。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_offset_box_pdf(source, (10, 20, 110, 220))
    _write_offset_box_pdf(translated, (-5, -10, 85, 190))

    PdfSideBySideComposer().compose(source, translated, output)

    with pikepdf.Pdf.open(output) as pdf:
        assert list(pdf.pages[0].mediabox) == [0, 0, 214, 200]
        assert _translation_matrices(pdf.pages[0]) == [(-10.0, -20.0), (129.0, 10.0)]


def test_side_by_side_composer_uses_crop_box_as_viewer_boundary(tmp_path: Path) -> None:
    """被 CropBox 隐藏的页边不能重新撑大双语输出画布。"""

    source = tmp_path / "source.pdf"
    translated = tmp_path / "translated.pdf"
    output = tmp_path / "bilingual.pdf"
    _write_cropped_pdf(source, media_box=(0, 0, 200, 100), crop_box=(50, 0, 150, 100))
    _write_cropped_pdf(
        translated,
        media_box=(0, 0, 200, 100),
        crop_box=(50, 0, 150, 100),
    )

    PdfSideBySideComposer().compose(source, translated, output)

    with pikepdf.Pdf.open(output) as pdf:
        assert list(pdf.pages[0].mediabox) == [0, 0, 224, 100]
        assert _translation_matrices(pdf.pages[0]) == [(-50.0, 0.0), (74.0, 0.0)]


def _write_blank_pdf(
    path: Path,
    page_sizes: list[tuple[float, float]],
    *,
    rotation: int = 0,
) -> None:
    """生成指定页面尺寸的最小可打开 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        for page_size in page_sizes:
            page = pdf.add_blank_page(page_size=page_size)
            if rotation:
                page.Rotate = rotation
        pdf.save(path)


def _write_offset_box_pdf(
    path: Path,
    media_box: tuple[float, float, float, float],
) -> None:
    """生成带非零 MediaBox 原点的单页 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(
            page_size=(media_box[2] - media_box[0], media_box[3] - media_box[1])
        )
        page.MediaBox = media_box
        pdf.save(path)


def _write_cropped_pdf(
    path: Path,
    *,
    media_box: tuple[float, float, float, float],
    crop_box: tuple[float, float, float, float],
) -> None:
    """生成 MediaBox 与用户可见 CropBox 不同的单页 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(
            page_size=(media_box[2] - media_box[0], media_box[3] - media_box[1])
        )
        page.MediaBox = media_box
        page.CropBox = crop_box
        pdf.save(path)


def _translation_matrices(page: pikepdf.Page) -> list[tuple[float, float]]:
    """读取合成页中两个 Form XObject 的平移位置。"""

    contents = page.Contents.read_bytes().decode("latin-1", errors="replace")
    matches = re.findall(r"1 0 0 1 (-?[0-9.]+) (-?[0-9.]+) cm", contents)
    return [(float(x), float(y)) for x, y in matches]
