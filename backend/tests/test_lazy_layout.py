# ruff: noqa: RUF002 -- 中文测试说明保留自然标点。
"""验证布局 runtime 只在首个 PDF 操作时初始化。"""

import asyncio
from pathlib import Path

import pytest

from modules.pdf.layout import LayoutModelError
from modules.pdf.lazy_layout import LazyLayoutDetector
from modules.pdf.resource_installer import PdfResourcesNotReadyError


class StubLayoutDetector:
    """记录 lazy wrapper 对 delegate 的最小调用。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.ensure_calls = 0
        self.detect_calls = 0

    def ensure_model_available(self) -> None:
        """记录模型校验调用。"""

        self.ensure_calls += 1

    async def detect_layout_batch(self, input_images: list[object]) -> list[object]:
        """原样返回输入，便于验证 delegate 复用。"""

        self.detect_calls += 1
        return input_images


def test_lazy_layout_detector_creates_one_delegate_on_first_use(tmp_path: Path) -> None:
    """构造 wrapper 不加载 delegate，后续校验和检测复用同一实例。"""

    created: list[StubLayoutDetector] = []

    def create_detector(*_: object, **__: object) -> StubLayoutDetector:
        """创建并记录测试 delegate。"""

        detector = StubLayoutDetector()
        created.append(detector)
        return detector

    model_path = tmp_path / "layout.onnx"
    detector = LazyLayoutDetector(model_path, detector_factory=create_detector)

    assert detector.model_path == model_path
    assert created == []

    detector.ensure_model_available()
    result = asyncio.run(detector.detect_layout_batch([object()]))

    assert len(created) == 1
    assert created[0].ensure_calls == 1
    assert created[0].detect_calls == 1
    assert len(result) == 1


def test_lazy_layout_detector_validates_resources_before_creating_delegate(
    tmp_path: Path,
) -> None:
    """资源 gate 失败时必须在加载模型 runtime 前停止，且不会触发下载。"""

    created: list[StubLayoutDetector] = []
    validation_calls = 0

    def reject_resources() -> None:
        """模拟 required pack 未通过 manifest 校验。"""

        nonlocal validation_calls
        validation_calls += 1
        raise PdfResourcesNotReadyError(("layout",))

    def create_detector(*_: object, **__: object) -> StubLayoutDetector:
        """失败路径不应调用 factory。"""

        detector = StubLayoutDetector()
        created.append(detector)
        return detector

    detector = LazyLayoutDetector(
        tmp_path / "layout.onnx",
        detector_factory=create_detector,
        resource_validator=reject_resources,
        verify_model_checksum=False,
    )

    with pytest.raises(LayoutModelError, match="pdf_resources_not_ready"):
        detector.ensure_model_available()

    assert validation_calls == 1
    assert created == []


def test_lazy_layout_detector_rechecks_only_the_cached_resource_snapshot(
    tmp_path: Path,
) -> None:
    """每个任务都经过 gate，但 delegate 只创建一次，checksum cache 由 installer 复用。"""

    validation_calls = 0
    factory_options: list[dict[str, object]] = []

    def validate_resources() -> None:
        """记录每个任务进入模型前的 snapshot 检查。"""

        nonlocal validation_calls
        validation_calls += 1

    def create_detector(*_: object, **options: object) -> StubLayoutDetector:
        """记录 canonical pack 已关闭 delegate 内的重复 checksum。"""

        factory_options.append(options)
        return StubLayoutDetector()

    detector = LazyLayoutDetector(
        tmp_path / "layout.onnx",
        detector_factory=create_detector,
        resource_validator=validate_resources,
        verify_model_checksum=False,
    )

    detector.ensure_model_available()
    detector.ensure_model_available()

    assert validation_calls == 2
    assert len(factory_options) == 1
    assert factory_options[0]["verify_checksum"] is False
