"""用内置 PDFium 逐页光栅化并读取图片覆盖率, 不依赖系统 Poppler。"""

from collections.abc import Iterator
from ctypes import byref, c_float
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pypdfium2 as pdfium
from PIL.Image import Image

from .constants import PDF_RASTER_SCALE


@dataclass(frozen=True, slots=True)
class PDFPageImage:
    """保存一页仅供 layout 推理使用的内存图像。"""

    page_index: int
    image: Image
    size: tuple[int, int]


class PDFToImageConverter:
    """按固定 144 DPI 将 PDF 页面转换为 RGB PIL 图像。"""

    def __init__(self, pdf_path: str | Path) -> None:
        """绑定只读源文件路径。"""

        self.pdf_path = Path(pdf_path)

    def convert_pdf_to_images(self) -> list[PDFPageImage]:
        """兼容需要完整列表的调用方; layout runtime 应优先使用逐页 iterator。"""

        return list(self.iter_pdf_images())

    def page_count(self) -> int:
        """只读取页数, 供 inference 失败时构造逐页 fallback。"""

        document = pdfium.PdfDocument(str(self.pdf_path))
        try:
            return len(document)
        finally:
            document.close()

    def iter_pdf_images(self) -> Iterator[PDFPageImage]:
        """每次只持有一页 RGB 图像, 消费完成后再渲染下一页。"""

        document = pdfium.PdfDocument(str(self.pdf_path))
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    bitmap = page.render(scale=PDF_RASTER_SCALE)
                    try:
                        raw_image = bitmap.to_pil()
                        image = raw_image.convert("RGB").copy()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                yield PDFPageImage(
                    page_index=page_index + 1,
                    image=image,
                    size=image.size,
                )
        finally:
            document.close()


def page_image_coverage_ratios(pdf_path: str | Path) -> tuple[float, ...]:
    """计算每页 Image XObject 在页面坐标中的矩形并集占比。"""

    document = pdfium.PdfDocument(str(pdf_path))
    ratios: list[float] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            try:
                width, height = page.get_size()
                rectangles: list[tuple[float, float, float, float]] = []
                for page_object in page.get_objects(
                    filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE],
                    max_depth=8,
                ):
                    left = c_float()
                    bottom = c_float()
                    right = c_float()
                    top = c_float()
                    if not pdfium.raw.FPDFPageObj_GetBounds(
                        page_object.raw,
                        byref(left),
                        byref(bottom),
                        byref(right),
                        byref(top),
                    ):
                        continue
                    x1 = min(max(min(left.value, right.value), 0.0), width)
                    x2 = min(max(max(left.value, right.value), 0.0), width)
                    y1 = min(max(min(bottom.value, top.value), 0.0), height)
                    y2 = min(max(max(bottom.value, top.value), 0.0), height)
                    if x2 > x1 and y2 > y1:
                        rectangles.append((x1, y1, x2, y2))
                page_area = width * height
                ratio = _rectangle_union_area(rectangles) / page_area if page_area > 0 else 0.0
                ratios.append(min(max(ratio, 0.0), 1.0))
            finally:
                page.close()
    finally:
        document.close()
    return tuple(ratios)


def _rectangle_union_area(rectangles: list[tuple[float, float, float, float]]) -> float:
    """用 x 轴扫描线计算少量图片矩形的精确并集面积。"""

    if not rectangles:
        return 0.0
    x_coordinates = sorted(
        {value for rectangle in rectangles for value in (rectangle[0], rectangle[2])}
    )
    area = 0.0
    for left, right in pairwise(x_coordinates):
        if right <= left:
            continue
        intervals = sorted(
            (bottom, top) for x1, bottom, x2, top in rectangles if x1 < right and x2 > left
        )
        if not intervals:
            continue
        covered_height = 0.0
        current_bottom, current_top = intervals[0]
        for bottom, top in intervals[1:]:
            if bottom <= current_top:
                current_top = max(current_top, top)
            else:
                covered_height += current_top - current_bottom
                current_bottom, current_top = bottom, top
        covered_height += current_top - current_bottom
        area += (right - left) * covered_height
    return area
