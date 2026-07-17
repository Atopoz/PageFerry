# ruff: noqa: RUF001, RUF002, RUF003 -- 测试中的中文句式与译文保留全角标点。
"""验证 PDF adapter 的 ONNX contract、文本回写、fallback 与输入边界。"""

import asyncio
import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pikepdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from PIL import Image

from modules.pdf.entities import DocumentLayout
from modules.pdf.errors import PdfPipelineError
from modules.pdf.extractor import LAYOUT_BEHAVIOR_LAYOUT, ParagraphExtractor, get_layout_behavior
from modules.pdf.layout import LayoutDetector, LayoutModelError
from modules.pdf.pipeline import PdfPipeline
from modules.pdf.rasterizer import PDFToImageConverter
from modules.translation.contracts import (
    TranslationBatchItem,
    TranslationBatchResult,
    TranslationProgress,
    TranslationRequest,
)
from vendor.pdfminerex.high_level import extract_text


class EmptyLayoutDetector:
    """为 pipeline 测试返回逐页空 layout，让 pdfminer textbox 负责分块。"""

    def ensure_model_available(self) -> None:
        """测试 detector 不需要外部模型。"""

    async def detect_layout_batch(self, images: list[np.ndarray]) -> list[DocumentLayout]:
        """按输入页数返回空 layout contract。"""

        return [DocumentLayout(page_index=index, layouts=[]) for index, _image in enumerate(images)]


class RecordingPageLayoutDetector(EmptyLayoutDetector):
    """记录 extractor 每次交给 layout runtime 的页面数。"""

    def __init__(self) -> None:
        """初始化空 batch 记录。"""

        self.batch_sizes: list[int] = []

    async def detect_layout_batch(self, images: list[np.ndarray]) -> list[DocumentLayout]:
        """记录 batch 后复用空 layout contract。"""

        self.batch_sizes.append(len(images))
        return await super().detect_layout_batch(images)


class ReplacingTranslator:
    """把固定英文短句替换为中文，同时保留 PDF marker。"""

    per_job_concurrency = 2

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """返回与输入 index 对齐的 marker-preserving 候选。"""

        del source_language, target_language, read_only_context, repair_candidates
        assert format_hint == "pdf"
        return TranslationBatchResult(
            items=tuple(
                TranslationBatchItem(
                    index=index,
                    text=text.replace("Hello world", "你好，世界"),
                )
                for index, text in enumerate(texts)
            )
        )


class BrokenMarkerTranslator(ReplacingTranslator):
    """模拟模型删除结构 marker 的不可信结果。"""

    def translate_batch(
        self,
        *,
        texts: Sequence[str],
        source_language: str | None,
        target_language: str,
        format_hint: str,
        read_only_context: Sequence[str] = (),
        repair_candidates: Sequence[str] = (),
    ) -> TranslationBatchResult:
        """故意只返回正文，验证当前 chunk 会安全回退。"""

        del texts, source_language, target_language, format_hint
        del read_only_context, repair_candidates
        return TranslationBatchResult(items=(TranslationBatchItem(index=0, text="你好，世界"),))


class RecordingSession:
    """记录三个 ONNX input，并返回可预测的 V3 output tensors。"""

    def __init__(self) -> None:
        """初始化空 input 记录。"""

        self.inputs: dict[str, np.ndarray] = {}

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        """返回 text、低置信 table 与无效 class 三行结果。"""

        assert output_names == ("fetch_name_0", "fetch_name_1")
        self.inputs = input_feed
        rows = np.asarray(
            [
                [22, 0.9, 10, 20, 110, 220, 2],
                [21, 0.65, 120, 5, 190, 90, 1],
                [-1, 0.99, 0, 0, 10, 10, 0],
            ],
            dtype=np.float32,
        )
        return [rows, np.asarray([3], dtype=np.int32)]


def _write_test_font(path: Path, characters: str) -> None:
    """生成只供 renderer 测试使用的极小 TrueType 字体。"""

    glyph_names = {character: f"u{ord(character):04X}" for character in sorted(set(characters))}
    glyph_order = [".notdef", *glyph_names.values()]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(
        {ord(character): glyph_name for character, glyph_name in glyph_names.items()}
    )
    glyphs = {}
    metrics = {}
    for glyph_name in glyph_order:
        pen = TTGlyphPen(None)
        if glyph_name not in {".notdef", "u0020"}:
            pen.moveTo((80, 0))
            pen.lineTo((520, 0))
            pen.lineTo((520, 700))
            pen.lineTo((80, 700))
            pen.closePath()
        glyphs[glyph_name] = pen.glyph()
        metrics[glyph_name] = (600, 0)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupNameTable(
        {
            "familyName": "PageFerry Test Sans",
            "styleName": "Regular",
            "uniqueFontIdentifier": "PageFerryTestSans-Regular",
            "fullName": "PageFerry Test Sans Regular",
            "psName": "PageFerryTestSans-Regular",
        }
    )
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


@pytest.fixture
def pdf_font_directory(tmp_path: Path) -> Path:
    """创建不依赖正式下载资源的 PDF pipeline 测试字体目录。"""

    directory = tmp_path / "pdf-fonts"
    directory.mkdir()
    _write_test_font(
        directory / "NotoSans-Regular.ttf",
        " Hello worldFirstSecondpage123",
    )
    _write_test_font(
        directory / "NotoSansSC-Regular.ttf",
        "你好，世界",
    )
    return directory


def _write_text_pdf(path: Path, text: str | None = "Hello world") -> None:
    """生成一页不依赖 reportlab 的小型 Type1 文本 PDF fixture。"""

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        if text is not None:
            font = pikepdf.Dictionary(
                Type=pikepdf.Name("/Font"),
                Subtype=pikepdf.Name("/Type1"),
                BaseFont=pikepdf.Name("/Helvetica"),
            )
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            page.Contents = pikepdf.Stream(
                pdf,
                f"BT /F1 18 Tf 72 700 Td ({escaped}) Tj ET".encode("latin-1"),
            )
        pdf.save(path)


def _write_two_page_text_pdf(
    path: Path,
    texts: tuple[bytes, bytes] = (b"First page", b"Second page"),
) -> None:
    """生成两页原生文本 PDF, 用于验证逐页 layout inference。"""

    with pikepdf.Pdf.new() as pdf:
        font = pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"),
        )
        font_ref = pdf.make_indirect(font)
        for text in texts:
            page = pdf.add_blank_page(page_size=(612, 792))
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font_ref))
            page.Contents = pikepdf.Stream(pdf, b"BT /F1 18 Tf 72 700 Td (" + text + b") Tj ET")
        pdf.save(path)


def _write_mixed_native_and_scan_pdf(path: Path) -> None:
    """生成一页原生文本加一页全幅 Image XObject 的混合 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        text_page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"),
        )
        text_page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=pdf.make_indirect(font))
        )
        text_page.Contents = pikepdf.Stream(
            pdf,
            b"BT /F1 18 Tf 72 700 Td (Hello world) Tj ET",
        )

        scan_page = pdf.add_blank_page(page_size=(612, 792))
        pixels = bytes([220, 220, 220]) * 100 * 100
        image = pikepdf.Stream(pdf, pixels)
        image.Type = pikepdf.Name("/XObject")
        image.Subtype = pikepdf.Name("/Image")
        image.Width = 100
        image.Height = 100
        image.ColorSpace = pikepdf.Name("/DeviceRGB")
        image.BitsPerComponent = 8
        scan_page.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im1=pdf.make_indirect(image))
        )
        scan_page.Contents = pikepdf.Stream(
            pdf,
            b"q 612 0 0 792 0 0 cm /Im1 Do Q",
        )
        pdf.save(path)


def _write_invisible_ocr_pdf(path: Path) -> None:
    """生成只含 render mode 3 search layer 的 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"),
        )
        page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)))
        page.Contents = pikepdf.Stream(
            pdf,
            b"BT /F1 28 Tf 3 Tr 72 700 Td (Hidden OCR text) Tj ET",
        )
        pdf.save(path)


def _write_transparent_ocr_pdf(path: Path) -> None:
    """生成全幅扫描图加 ExtGState alpha=0 搜索层的 PDF。"""

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        font = pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"),
            Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"),
        )
        image = pikepdf.Stream(pdf, bytes([255, 255, 255]) * 100 * 100)
        image.Type = pikepdf.Name("/XObject")
        image.Subtype = pikepdf.Name("/Image")
        image.Width = 100
        image.Height = 100
        image.ColorSpace = pikepdf.Name("/DeviceRGB")
        image.BitsPerComponent = 8
        graphic_state = pikepdf.Dictionary(Type=pikepdf.Name("/ExtGState"))
        graphic_state[pikepdf.Name("/ca")] = 0
        graphic_state[pikepdf.Name("/CA")] = 0
        page.Resources = pikepdf.Dictionary(
            Font=pikepdf.Dictionary(F1=pdf.make_indirect(font)),
            XObject=pikepdf.Dictionary(Im1=pdf.make_indirect(image)),
            ExtGState=pikepdf.Dictionary(GS0=pdf.make_indirect(graphic_state)),
        )
        page.Contents = pikepdf.Stream(
            pdf,
            b"q 612 0 0 792 0 0 cm /Im1 Do Q "
            b"q /GS0 gs BT /F1 28 Tf 72 700 Td (Hidden alpha OCR) Tj ET Q",
        )
        pdf.save(path)


def _request(source: Path, output_dir: Path) -> TranslationRequest:
    """构造 PDF pipeline 共用的英翻中 request。"""

    return TranslationRequest(
        source_path=source,
        output_dir=output_dir,
        source_language="en",
        target_language="zh-CN",
        provider_id="test",
        model_id="identity",
    )


def test_pdf_pipeline_translates_text_without_changing_source(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """完整 pipeline 应回写中文、保留原文件并报告三阶段进度。"""

    source = tmp_path / "source.pdf"
    _write_text_pdf(source)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    progress: list[TranslationProgress] = []

    result = PdfPipeline(
        ReplacingTranslator(),
        EmptyLayoutDetector(),
        font_directory=pdf_font_directory,
    ).translate(
        _request(source, tmp_path / "outputs"),
        report_progress=progress.append,
    )

    assert result.output_path.is_file()
    assert "你好，世界" in extract_text(str(result.output_path))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert result.translated_segments == 1
    assert result.fallback_segments == 0
    assert progress == [
        TranslationProgress(stage="extracting"),
        TranslationProgress(stage="translating", total_segments=1),
        TranslationProgress(stage="translating", processed_segments=1, total_segments=1),
        TranslationProgress(stage="formatting", processed_segments=1, total_segments=1),
    ]


def test_pdf_pipeline_rejects_missing_font_pack_before_provider_call(tmp_path: Path) -> None:
    """字体 pack 未安装时必须在读取模型和调用 provider 前失败。"""

    source = tmp_path / "source.pdf"
    _write_text_pdf(source)

    with pytest.raises(PdfPipelineError) as raised:
        PdfPipeline(
            ReplacingTranslator(),
            EmptyLayoutDetector(),
            font_directory=tmp_path / "missing-fonts",
        ).translate(_request(source, tmp_path / "outputs"))

    assert raised.value.code == "pdf_font_directory_missing"
    assert not (tmp_path / "outputs").exists()


def test_pdf_pipeline_falls_back_when_model_drops_markers(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """marker 不完整时只能保留当前 chunk 原文，不能清空页面文字。"""

    source = tmp_path / "source.pdf"
    _write_text_pdf(source)

    result = PdfPipeline(
        BrokenMarkerTranslator(),
        EmptyLayoutDetector(),
        font_directory=pdf_font_directory,
    ).translate(_request(source, tmp_path / "outputs"))

    assert "Hello world" in extract_text(str(result.output_path))
    assert result.translated_segments == 0
    assert result.fallback_segments == 1
    assert result.warning_codes == ("pdf_chunk_fallback",)


def test_pdf_pipeline_does_not_duplicate_page_without_pending_translation(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """同一文档中没有待写译文的页面必须保持原 content stream 一份。"""

    source = tmp_path / "mixed-text.pdf"
    _write_two_page_text_pdf(source, texts=(b"Hello world", b"123"))

    result = PdfPipeline(
        ReplacingTranslator(),
        EmptyLayoutDetector(),
        font_directory=pdf_font_directory,
    ).translate(_request(source, tmp_path / "outputs"))

    output_text = extract_text(str(result.output_path))
    assert "你好，世界" in output_text
    assert output_text.count("123") == 1


def test_pdf_pipeline_rejects_page_without_text_layer(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """没有原生文本层的页面不能伪装成成功译文。"""

    source = tmp_path / "scan-like.pdf"
    _write_text_pdf(source, text=None)

    with pytest.raises(PdfPipelineError) as raised:
        PdfPipeline(
            ReplacingTranslator(),
            EmptyLayoutDetector(),
            font_directory=pdf_font_directory,
        ).translate(_request(source, tmp_path / "outputs"))

    assert raised.value.code == "pdf_no_text_layer"
    assert not (tmp_path / "outputs").exists()


def test_pdf_pipeline_rejects_scan_page_mixed_with_native_text(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """一页原生文本不能掩盖另一页全幅扫描图没有可见文本层。"""

    source = tmp_path / "mixed-scan.pdf"
    _write_mixed_native_and_scan_pdf(source)

    with pytest.raises(PdfPipelineError) as raised:
        PdfPipeline(
            ReplacingTranslator(),
            EmptyLayoutDetector(),
            font_directory=pdf_font_directory,
        ).translate(_request(source, tmp_path / "outputs"))

    assert raised.value.code == "pdf_no_text_layer"
    assert not (tmp_path / "outputs").exists()


def test_pdf_pipeline_rejects_invisible_ocr_search_layer(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """invisible OCR 不能被当作原生正文翻译后重绘成可见文字。"""

    source = tmp_path / "hidden-ocr.pdf"
    _write_invisible_ocr_pdf(source)

    with pytest.raises(PdfPipelineError) as raised:
        PdfPipeline(
            ReplacingTranslator(),
            EmptyLayoutDetector(),
            font_directory=pdf_font_directory,
        ).translate(_request(source, tmp_path / "outputs"))

    assert raised.value.code == "pdf_no_text_layer"
    assert not (tmp_path / "outputs").exists()


def test_pdf_pipeline_rejects_transparent_extgstate_ocr(
    tmp_path: Path,
    pdf_font_directory: Path,
) -> None:
    """ExtGState alpha=0 的 Tr 0 搜索层也不能被重绘成可见文字。"""

    source = tmp_path / "transparent-ocr.pdf"
    _write_transparent_ocr_pdf(source)
    detector = EmptyLayoutDetector()

    pages = asyncio.run(
        ParagraphExtractor(str(source), layout_detector=detector).extract_page_info_with_layout()
    )
    spans = [span for block in pages[0].preserved_texts for span in block.spans]
    assert len(spans) == 1
    assert spans[0].is_visible is False
    assert spans[0].has_transparency is True

    with pytest.raises(PdfPipelineError) as raised:
        PdfPipeline(
            ReplacingTranslator(),
            detector,
            font_directory=pdf_font_directory,
        ).translate(_request(source, tmp_path / "outputs"))

    assert raised.value.code == "pdf_no_text_layer"
    assert not (tmp_path / "outputs").exists()


def test_extractor_runs_layout_inference_one_page_at_a_time(tmp_path: Path) -> None:
    """长文档不能先把全部 PIL 与 BGR 页面双份常驻内存。"""

    source = tmp_path / "two-pages.pdf"
    _write_two_page_text_pdf(source)
    detector = RecordingPageLayoutDetector()

    pages = asyncio.run(
        ParagraphExtractor(str(source), layout_detector=detector).extract_page_info_with_layout()
    )

    assert len(pages) == 2
    assert detector.batch_sizes == [1, 1]


def test_pdfium_rasterizer_uses_two_x_page_scale(tmp_path: Path) -> None:
    """layout 页面图必须保持 PDF pipeline 的 144 DPI / scale 2 坐标 contract。"""

    source = tmp_path / "source.pdf"
    _write_text_pdf(source)

    pages = PDFToImageConverter(source).convert_pdf_to_images()

    assert len(pages) == 1
    assert pages[0].page_index == 1
    assert pages[0].size == (1224, 1584)
    assert pages[0].image.mode == "RGB"


def test_layout_detector_uses_official_v3_tensor_contract(tmp_path: Path) -> None:
    """adapter 应输出 800 方图、原图 scale factor 与按 raw order 排序的矩形框。"""

    model = tmp_path / "inference.onnx"
    model.write_bytes(b"fake")
    session = RecordingSession()

    def session_factory(_model_path: Path, _threads: int) -> RecordingSession:
        """向 detector 注入不依赖真实 ORT 的 recording session。"""

        return session

    detector = LayoutDetector(
        model,
        session_factory=session_factory,
        verify_checksum=False,
    )
    bgr = np.zeros((100, 200, 3), dtype=np.uint8)
    bgr[:, :, 0] = 255

    documents = asyncio.run(detector.detect_layout_batch([bgr]))

    assert session.inputs["image"].shape == (1, 3, 800, 800)
    assert session.inputs["image"].dtype == np.float32
    # BGR 蓝色必须转换成 RGB，进入 CHW 后第 3 channel 为 1。
    assert float(session.inputs["image"][0, 0, 0, 0]) == 0.0
    assert float(session.inputs["image"][0, 2, 0, 0]) == 1.0
    assert session.inputs["im_shape"].tolist() == [[800.0, 800.0]]
    assert session.inputs["scale_factor"].tolist() == [[8.0, 4.0]]
    assert [layout.label for layout in documents[0].layouts] == ["table", "text"]
    assert documents[0].layouts[0].is_filtered is True
    assert documents[0].layouts[1].bbox.bounds == (10.0, 20.0, 110.0, 100.0)


def test_layout_detector_rejects_missing_model_before_session_load(tmp_path: Path) -> None:
    """模型缺失应给 pipeline 一个可映射的本地错误，而不是静默联网。"""

    detector = LayoutDetector(tmp_path / "missing.onnx")

    with pytest.raises(LayoutModelError, match="layout_model_missing"):
        detector.ensure_model_available()


def test_header_and_footer_text_use_one_document_independent_policy() -> None:
    """页眉页脚的原生文本统一翻译，不能用 D950 bbox 特判重写 V3 label。"""

    assert get_layout_behavior("header") == LAYOUT_BEHAVIOR_LAYOUT
    assert get_layout_behavior("footer") == LAYOUT_BEHAVIOR_LAYOUT


def test_layout_preprocess_keeps_bicubic_rgb_values_stable(tmp_path: Path) -> None:
    """单色输入 resize 后不应因 channel 或归一化错误产生漂移。"""

    source = tmp_path / "solid.png"
    Image.new("RGB", (20, 10), (64, 128, 255)).save(source)
    rgb = np.asarray(Image.open(source))
    bgr = rgb[:, :, ::-1].copy()
    model = tmp_path / "inference.onnx"
    model.write_bytes(b"fake")
    session = RecordingSession()

    def session_factory(_model_path: Path, _threads: int) -> RecordingSession:
        """返回用于检查 resize 后像素的 session。"""

        return session

    detector = LayoutDetector(model, session_factory=session_factory, verify_checksum=False)
    asyncio.run(detector.detect_layout_batch([bgr]))

    pixel = session.inputs["image"][0, :, 400, 400]
    assert np.allclose(pixel, np.asarray([64, 128, 255], dtype=np.float32) / 255.0)
