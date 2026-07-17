"""PDF 翻译领域实体"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Sequence


PdfBilingualLayoutMode = Literal["side_by_side", "inline_stack"]

PDF_BILINGUAL_LAYOUT_SIDE_BY_SIDE: PdfBilingualLayoutMode = "side_by_side"
PDF_BILINGUAL_LAYOUT_INLINE_STACK: PdfBilingualLayoutMode = "inline_stack"


def normalize_pdf_bilingual_layout(value: object) -> PdfBilingualLayoutMode:
    """规范化 PDF 双语版式，旧数据默认使用双页拼接。"""
    if not isinstance(value, str):
        return PDF_BILINGUAL_LAYOUT_SIDE_BY_SIDE

    normalized = value.strip().lower().replace("-", "_")
    if normalized == PDF_BILINGUAL_LAYOUT_INLINE_STACK:
        return PDF_BILINGUAL_LAYOUT_INLINE_STACK
    return PDF_BILINGUAL_LAYOUT_SIDE_BY_SIDE


@dataclass
class BBox:
    """边界框"""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        self.x1 = float(self.x1)
        self.y1 = float(self.y1)
        self.x2 = float(self.x2)
        self.y2 = float(self.y2)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """获取边界框元组"""
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def list(self) -> list[float]:
        """将边界框转换为列表"""
        return [self.x1, self.y1, self.x2, self.y2]

    def pdfminer_bounds(self, scale: float, height: float) -> tuple[float, float, float, float]:
        """将布局结果转为 pdfminer 坐标"""
        x1 = self.x1 / scale
        y1 = (height - self.y2) / scale
        x2 = self.x2 / scale
        y2 = (height - self.y1) / scale
        return (x1, y1, x2, y2)


@dataclass
class LayoutResult:
    """布局检测结果"""

    cls_id: int
    label: str
    shape: tuple[int, int, int]
    bbox: BBox
    score: float
    is_filtered: bool = False

    def pdfminer_bbox(self, scale: float = 2.0) -> tuple[float, float, float, float]:
        """转换为 pdfminer 坐标"""
        height = self.shape[0]
        return self.bbox.pdfminer_bounds(scale, height)


@dataclass
class DocumentLayout:
    """单页布局检测结果"""

    page_index: int
    layouts: list[LayoutResult]


@dataclass
class TextCharBox:
    """文本字符及其边界框"""

    char: str
    bbox: BBox


@dataclass
class TextSpan:
    """文本行（LTTextLine 级别）"""

    span_id: int
    bbox: BBox
    text: str
    translated_text: str = ""
    adjusted_font_size: float | None = None
    font_name: str | None = None
    font_size: float | None = None
    is_bold: bool = False
    color: tuple[float, float, float] | None = None
    stroke_color: tuple[float, float, float] | None = None
    text_render_mode: int = 0
    is_visible: bool = True
    has_transparency: bool = False
    ops: list[bytes] = field(default_factory=list)
    ops_xobject_paths: list[tuple[str, ...]] = field(default_factory=list)
    char_boxes: list[TextCharBox] = field(default_factory=list)

    source_textbox_id: int | None = None
    source_textbox_bbox: BBox | None = None
    table_cell_bbox: BBox | None = None

    layout_label: str | None = None
    layout_behavior: str | None = None
    matched_layout: LayoutResult | None = None
    matched_layout_idx: int | None = None
    is_preserved: bool = False


@dataclass
class TextBlock:
    """文本块（Block 级别）"""

    block_id: int
    bbox: BBox
    text: str
    spans: list[TextSpan]

    source: str = "pdfminer"
    layout_type: str = "text"
    layout_label: str | None = None
    layout_score: float | None = None
    translation_mode: Literal["span", "block"] = "span"
    translated_text: str = ""
    adjusted_font_size: float | None = None
    is_bold: bool = False
    dominant_color: tuple[float, float, float] | None = None
    dominant_stroke_color: tuple[float, float, float] | None = None
    dominant_text_render_mode: int = 0


@dataclass
class PreservedBlock:
    """保留的文本块"""

    original_block_id: int
    bbox: BBox
    text: str
    spans: list[TextSpan]
    source: str = "pdfminer"
    layout_label: str | None = None
    layout_score: float | None = None


@dataclass
class PageInfo:
    """页面信息"""

    page_index: int
    texts: list[TextBlock]
    preserved_texts: list[PreservedBlock]


def collect_all_spans(pages: Sequence[PageInfo]) -> list[TextSpan]:
    """收集所有页面的 spans"""
    spans: list[TextSpan] = []
    for page in pages:
        for block in page.texts:
            spans.extend(block.spans)
    return spans
