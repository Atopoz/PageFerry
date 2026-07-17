"""以 app-scoped ONNX Runtime session 提供 PP-DocLayoutV3 矩形布局结果。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from PIL import Image
from platformdirs import user_data_path

from .assets import (
    find_pdf_asset,
    load_default_pdf_asset_manifest,
    pdf_asset_pack_path,
    pdf_asset_path,
)
from .constants import PP_DOCLAYOUT_V3_INPUT_SIZE, PP_DOCLAYOUT_V3_SCORE_THRESHOLD
from .entities import BBox, DocumentLayout, LayoutResult

logger = logging.getLogger(__name__)

PP_DOCLAYOUT_V3_LABELS = (
    "abstract",
    "algorithm",
    "aside_text",
    "chart",
    "content",
    "display_formula",
    "doc_title",
    "figure_title",
    "footer",
    "footer_image",
    "footnote",
    "formula_number",
    "header",
    "header_image",
    "image",
    "inline_formula",
    "number",
    "paragraph_title",
    "reference",
    "reference_content",
    "seal",
    "table",
    "text",
    "vertical_text",
    "vision_footnote",
)

_MIN_SCORE_BY_LABEL = {
    "table": 0.7,
    "reference": 0.8,
    "formula_number": 0.8,
    "number": 0.7,
    "vision_footnote": 0.6,
    "image": 0.5,
}


class _InferenceSession(Protocol):
    """描述 adapter 实际使用的 ONNX session 最小接口。"""

    def run(
        self,
        output_names: Sequence[str] | None,
        input_feed: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        """执行一次同步 inference。"""
        ...


SessionFactory = Callable[[Path, int], _InferenceSession]


class LayoutModelError(RuntimeError):
    """表示 layout 模型缺失、损坏或无法由 CPU runtime 加载。"""


class LayoutDetector:
    """复用一个 CPU ONNX session, 并把 V3 输出收敛到 PDF layout contract。"""

    def __init__(
        self,
        model_path: Path | None = None,
        *,
        max_concurrency: int = 1,
        intra_op_threads: int | None = None,
        session_factory: SessionFactory | None = None,
        verify_checksum: bool = True,
    ) -> None:
        """绑定模型资产与受控 CPU 并发, 模型保持 lazy load。"""

        manifest = load_default_pdf_asset_manifest()
        model_asset = find_pdf_asset(manifest, "pp-doclayout-v3-onnx")
        default_model_path = pdf_asset_path(
            pdf_asset_pack_path(
                Path(user_data_path("PageFerry", appauthor=False, roaming=False)),
                manifest,
            ),
            model_asset,
        )
        configured_path = os.environ.get("PAGEFERRY_LAYOUT_MODEL_PATH")
        self.model_path = Path(model_path or configured_path or default_model_path)
        self._expected_size = model_asset.size_bytes
        self._expected_sha256 = model_asset.sha256
        self._intra_op_threads = max(1, intra_op_threads or _default_intra_op_threads())
        self._session_factory = session_factory or _create_onnx_session
        self._verify_checksum = verify_checksum
        self._model_verified = False
        self._verification_lock = threading.Lock()
        self._session: _InferenceSession | None = None
        self._session_lock = threading.Lock()
        # ORT 在 D950 上 batch>1 更慢且内存更高; 跨 job 也只允许小并发。
        self._inference_slots = threading.BoundedSemaphore(max(1, max_concurrency))

    def ensure_model_available(self) -> None:
        """在开始光栅化前拒绝缺失或 checksum 不符的模型资产。"""

        if self._model_verified:
            return
        with self._verification_lock:
            if self._model_verified:
                return
            if not self.model_path.is_file():
                raise LayoutModelError("layout_model_missing")
            if self._verify_checksum:
                stat = self.model_path.stat()
                if stat.st_size != self._expected_size:
                    raise LayoutModelError("layout_model_size_mismatch")
                digest = hashlib.sha256()
                with self.model_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != self._expected_sha256:
                    raise LayoutModelError("layout_model_checksum_mismatch")
            self._model_verified = True

    async def detect_layout_batch(self, input_images: list[np.ndarray]) -> list[DocumentLayout]:
        """在 worker thread 中按 batch_size=1 顺序执行页面布局推理。"""

        if not input_images:
            return []
        return await asyncio.to_thread(self._detect_layout_batch_sync, input_images)

    def _detect_layout_batch_sync(self, input_images: list[np.ndarray]) -> list[DocumentLayout]:
        """在一个受控 inference slot 内处理整份文档, 避免跨 job 抢满 CPU。"""

        with self._inference_slots:
            session = self._get_session()
            return [
                self._detect_page(session, page_index, image)
                for page_index, image in enumerate(input_images)
            ]

    def _get_session(self) -> _InferenceSession:
        """双重检查并只创建一次 ONNX session。"""

        session = self._session
        if session is not None:
            return session
        with self._session_lock:
            if self._session is None:
                self.ensure_model_available()
                try:
                    self._session = self._session_factory(
                        self.model_path,
                        self._intra_op_threads,
                    )
                except Exception as error:
                    raise LayoutModelError("layout_model_load_failed") from error
            return self._session

    def _detect_page(
        self,
        session: _InferenceSession,
        page_index: int,
        bgr_image: np.ndarray,
    ) -> DocumentLayout:
        """完成一页的 RGB resize、ORT inference 与矩形 contract 转换。"""

        image_tensor, image_shape, scale_factor = _prepare_image(bgr_image)
        outputs = session.run(
            ("fetch_name_0", "fetch_name_1"),
            {
                "image": image_tensor,
                "im_shape": image_shape,
                "scale_factor": scale_factor,
            },
        )
        if len(outputs) < 2:
            raise LayoutModelError("layout_output_missing")
        rows = np.asarray(outputs[0])
        counts = np.asarray(outputs[1]).reshape(-1)
        if rows.ndim != 2 or rows.shape[1] < 7 or counts.size != 1:
            raise LayoutModelError("layout_output_shape_invalid")

        height, width = bgr_image.shape[:2]
        layouts: list[LayoutResult] = []
        valid_count = min(int(counts[0]), len(rows))
        # 第七列是模型给出的 raw reading order; 先按它排序, 再丢弃 V2 contract
        # 不消费的字段, 避免不同后处理实现改变 block 的可观察顺序。
        ordered_rows = sorted(rows[:valid_count], key=lambda row: float(row[6]))
        for row in ordered_rows:
            class_id = int(row[0])
            score = float(row[1])
            if class_id < 0 or class_id >= len(PP_DOCLAYOUT_V3_LABELS):
                continue
            if not np.isfinite(row[:7]).all() or score <= PP_DOCLAYOUT_V3_SCORE_THRESHOLD:
                continue
            x1, y1, x2, y2 = _clamped_box(row[2:6], width=width, height=height)
            if x2 <= x1 or y2 <= y1:
                continue
            bbox = BBox(x1=x1, y1=y1, x2=x2, y2=y2)
            label = PP_DOCLAYOUT_V3_LABELS[class_id]
            too_small = (x2 - x1) < 6 or (y2 - y1) < 6
            min_score = _MIN_SCORE_BY_LABEL.get(label, PP_DOCLAYOUT_V3_SCORE_THRESHOLD)
            layouts.append(
                LayoutResult(
                    cls_id=class_id,
                    label=label,
                    shape=tuple(cast(tuple[int, int, int], bgr_image.shape)),
                    bbox=bbox,
                    score=score,
                    is_filtered=too_small or score < min_score,
                )
            )
        return DocumentLayout(
            page_index=page_index,
            layouts=_filter_overlap_layouts(layouts, width=width, height=height),
        )


def _prepare_image(image: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """按官方 ONNX export contract 生成三个 float32 input tensor。"""

    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("layout_image_must_be_bgr")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError("layout_image_is_empty")
    rgb = np.ascontiguousarray(image[:, :, ::-1])
    resized = Image.fromarray(rgb).resize(
        (PP_DOCLAYOUT_V3_INPUT_SIZE, PP_DOCLAYOUT_V3_INPUT_SIZE),
        resample=Image.Resampling.BICUBIC,
    )
    tensor = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    tensor = np.transpose(tensor, (2, 0, 1))[None, ...]
    im_shape = np.asarray(
        [[PP_DOCLAYOUT_V3_INPUT_SIZE, PP_DOCLAYOUT_V3_INPUT_SIZE]],
        dtype=np.float32,
    )
    scale_factor = np.asarray(
        [[PP_DOCLAYOUT_V3_INPUT_SIZE / height, PP_DOCLAYOUT_V3_INPUT_SIZE / width]],
        dtype=np.float32,
    )
    return tensor, im_shape, scale_factor


def _clamped_box(
    coordinates: np.ndarray,
    *,
    width: int,
    height: int,
) -> tuple[float, float, float, float]:
    """把模型已经映回原图的坐标 round 并限制在页面边界内。"""

    x1, y1, x2, y2 = (round(float(value)) for value in coordinates)
    return (
        float(min(max(x1, 0), width)),
        float(min(max(y1, 0), height)),
        float(min(max(x2, 0), width)),
        float(min(max(y2, 0), height)),
    )


def _filter_overlap_layouts(
    layouts: list[LayoutResult],
    *,
    width: int,
    height: int,
) -> list[LayoutResult]:
    """复刻官方 rect 后处理, 去掉 reference 容器、小框与明显重叠框。"""

    candidates = [layout for layout in layouts if layout.label != "reference"]
    dropped: set[int] = set()
    page_area = width * height
    image_area_limit = 0.82 if width > height else 0.93
    for index, layout in enumerate(candidates):
        box = layout.bbox
        area = max(0.0, box.x2 - box.x1) * max(0.0, box.y2 - box.y1)
        if (box.x2 - box.x1) < 6 or (box.y2 - box.y1) < 6:
            dropped.add(index)
        if layout.label == "image" and len(candidates) > 1 and area > image_area_limit * page_area:
            dropped.add(index)
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            if left_index in dropped or right_index in dropped:
                continue
            right = candidates[right_index]
            overlap = _small_box_overlap(left.bbox, right.bbox)
            if overlap > 0.5 and "inline_formula" in {left.label, right.label}:
                if left.label == "inline_formula":
                    dropped.add(left_index)
                if right.label == "inline_formula":
                    dropped.add(right_index)
                continue
            if overlap <= 0.7:
                continue
            labels = {left.label, right.label}
            if (
                labels & {"image", "table", "seal", "chart"}
                and len(labels) > 1
                and ("table" not in labels or labels <= {"table", "image", "seal", "chart"})
            ):
                continue
            left_area = _box_area(left.bbox)
            right_area = _box_area(right.bbox)
            dropped.add(right_index if left_area >= right_area else left_index)
    return [layout for index, layout in enumerate(candidates) if index not in dropped]


def _small_box_overlap(left: BBox, right: BBox) -> float:
    """计算交集占较小 bbox 面积的比例。"""

    intersection_width = max(0.0, min(left.x2, right.x2) - max(left.x1, right.x1))
    intersection_height = max(0.0, min(left.y2, right.y2) - max(left.y1, right.y1))
    smaller_area = min(_box_area(left), _box_area(right))
    if smaller_area <= 0:
        return 0.0
    return intersection_width * intersection_height / smaller_area


def _box_area(box: BBox) -> float:
    """返回一个规范矩形的非负面积。"""

    return max(0.0, box.x2 - box.x1) * max(0.0, box.y2 - box.y1)


def _default_intra_op_threads() -> int:
    """为 UI 和 LLM 请求保留 CPU, 不让单次 layout inference 吃满整机。"""

    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _create_onnx_session(model_path: Path, intra_op_threads: int) -> _InferenceSession:
    """延迟 import ONNX Runtime, 并明确只使用 CPUExecutionProvider。"""

    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = intra_op_threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    logger.info("Loading PP-DocLayoutV3 ONNX model on CPU")
    return cast(
        _InferenceSession,
        ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        ),
    )
