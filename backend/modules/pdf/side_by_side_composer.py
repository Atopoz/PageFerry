"""把原文页与译文页左右拼接为独立的双语 PDF。"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import pikepdf


class PdfSideBySideComposer:
    """按页合并原文 PDF 与译文 PDF, 不改写任一输入文件。"""

    DEFAULT_GUTTER = 24.0

    def __init__(self, gutter: float = DEFAULT_GUTTER) -> None:
        """设置两页之间固定的 PDF point 间距。"""

        if not math.isfinite(gutter) or gutter < 0:
            raise ValueError("gutter 必须是非负有限数")
        self._gutter = float(gutter)

    def compose(
        self,
        source_pdf_path: str | Path,
        translated_pdf_path: str | Path,
        output_path: str | Path,
    ) -> None:
        """生成原文在左、译文在右且页数不变的双语 PDF。"""

        source = Path(source_pdf_path).expanduser().resolve()
        translated = Path(translated_pdf_path).expanduser().resolve()
        output = Path(output_path).expanduser().resolve()
        self._validate_paths(source, translated, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_pdf_path(output)
        try:
            expected_pages = self._compose_to(source, translated, temporary)
            _fsync_file(temporary)
            self._validate_output(temporary, expected_pages)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _compose_to(
        self,
        source: Path,
        translated: Path,
        output: Path,
    ) -> tuple[tuple[float, float], ...]:
        """把两份输入按页放入临时文件并返回预期页面尺寸。"""

        expected_pages: list[tuple[float, float]] = []
        with (
            pikepdf.Pdf.open(source) as source_pdf,
            pikepdf.Pdf.open(translated) as translated_pdf,
            pikepdf.Pdf.new() as output_pdf,
        ):
            if len(source_pdf.pages) != len(translated_pdf.pages):
                raise ValueError("pdf_bilingual_page_count_mismatch")
            if not source_pdf.pages:
                raise ValueError("pdf_bilingual_empty_document")

            for source_page, translated_page in zip(
                source_pdf.pages,
                translated_pdf.pages,
                strict=True,
            ):
                source_width, source_height = _page_size(source_page)
                translated_width, translated_height = _page_size(translated_page)
                output_width = source_width + self._gutter + translated_width
                output_height = max(source_height, translated_height)
                expected_pages.append((output_width, output_height))
                output_page = output_pdf.add_blank_page(page_size=(output_width, output_height))

                source_y = (output_height - source_height) / 2
                translated_x = source_width + self._gutter
                translated_y = (output_height - translated_height) / 2
                output_page.add_overlay(
                    source_page,
                    pikepdf.Rectangle(
                        0,
                        source_y,
                        source_width,
                        source_y + source_height,
                    ),
                    shrink=False,
                    expand=False,
                )
                output_page.add_overlay(
                    translated_page,
                    pikepdf.Rectangle(
                        translated_x,
                        translated_y,
                        translated_x + translated_width,
                        translated_y + translated_height,
                    ),
                    shrink=False,
                    expand=False,
                )

            output_pdf.save(output)
        return tuple(expected_pages)

    @staticmethod
    def _validate_paths(source: Path, translated: Path, output: Path) -> None:
        """拒绝缺失输入和任何会覆盖输入 PDF 的目标路径。"""

        if not source.is_file():
            raise FileNotFoundError(source)
        if not translated.is_file():
            raise FileNotFoundError(translated)
        if source.suffix.lower() != ".pdf" or translated.suffix.lower() != ".pdf":
            raise ValueError("pdf_bilingual_input_kind_mismatch")
        if output in {source, translated}:
            raise ValueError("pdf_bilingual_output_would_overwrite_input")

    @staticmethod
    def _validate_output(
        output: Path,
        expected_pages: tuple[tuple[float, float], ...],
    ) -> None:
        """重新打开临时文件并验证页数、页面几何与 content stream。"""

        with pikepdf.Pdf.open(output) as pdf:
            if len(pdf.pages) != len(expected_pages):
                raise ValueError("pdf_bilingual_output_page_count_changed")
            if pdf.check_pdf_syntax():
                raise ValueError("pdf_bilingual_output_syntax_invalid")
            for page, expected_size in zip(pdf.pages, expected_pages, strict=True):
                actual_size = _page_size(page)
                if not all(
                    math.isclose(actual, expected, abs_tol=0.01)
                    for actual, expected in zip(actual_size, expected_size, strict=True)
                ):
                    raise ValueError("pdf_bilingual_output_geometry_changed")
                # parse_content_stream 会实际读取并解析页面操作符, 避免只验证到 xref 外壳。
                pikepdf.parse_content_stream(page)


def _page_size(page: pikepdf.Page) -> tuple[float, float]:
    """读取考虑 CropBox、Rotate 与 UserUnit 后的可见宽高。"""

    # Page.as_form_xobject 同样以 CropBox 为 BBox; 这里必须保持一致,
    # 否则被 viewer 裁掉的区域会重新占据双语页宽度。
    visible_box = page.cropbox
    user_unit = float(page.get("/UserUnit", 1.0))
    width = float(visible_box[2] - visible_box[0]) * user_unit
    height = float(visible_box[3] - visible_box[1]) * user_unit
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("pdf_bilingual_invalid_page_geometry")
    rotation = int(page.get("/Rotate", 0))
    if rotation % 90 != 0:
        raise ValueError("pdf_bilingual_invalid_page_rotation")
    if rotation % 360 in {90, 270}:
        return height, width
    return width, height


def _temporary_pdf_path(output: Path) -> Path:
    """在目标目录创建用于校验与原子 replace 的临时 PDF。"""

    descriptor, name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp.pdf",
    )
    os.close(descriptor)
    return Path(name)


def _fsync_file(path: Path) -> None:
    """把完整临时 PDF 刷入磁盘后再允许原子 replace。"""

    with path.open("r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
