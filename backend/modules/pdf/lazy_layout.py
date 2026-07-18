# ruff: noqa: RUF002 -- 中文说明保留自然标点。
"""延迟创建 PDF layout detector，避免 API 启动先加载整套 native runtime。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .resource_installer import PdfResourcesNotReadyError

if TYPE_CHECKING:
    import numpy as np

    from .entities import DocumentLayout


class _LayoutDelegate(Protocol):
    """描述 lazy wrapper 真正调用的 layout detector 接口。"""

    def ensure_model_available(self) -> None:
        """校验布局模型是否可用。"""
        ...

    async def detect_layout_batch(
        self,
        input_images: list[np.ndarray],
    ) -> list[DocumentLayout]:
        """对一批页面执行布局检测。"""
        ...


DetectorFactory = Callable[..., _LayoutDelegate]
ResourceValidator = Callable[[], object]


class LazyLayoutDetector:
    """直到首个 PDF 任务才 import 并创建 app-scoped LayoutDetector。"""

    def __init__(
        self,
        model_path: Path,
        *,
        max_concurrency: int = 1,
        intra_op_threads: int | None = None,
        detector_factory: DetectorFactory | None = None,
        resource_validator: ResourceValidator | None = None,
        verify_model_checksum: bool = True,
    ) -> None:
        """保存轻量配置与资源 gate，不在 sidecar ready 前加载 native runtime。"""

        self.model_path = Path(model_path)
        self._max_concurrency = max_concurrency
        self._intra_op_threads = intra_op_threads
        self._detector_factory = detector_factory
        self._resource_validator = resource_validator
        self._verify_model_checksum = verify_model_checksum
        self._delegate: _LayoutDelegate | None = None
        self._delegate_lock = threading.Lock()

    def ensure_model_available(self) -> None:
        """在 PDF pipeline 真正启动时先校验资源 snapshot，再创建 delegate。"""

        if self._resource_validator is not None:
            try:
                self._resource_validator()
            except PdfResourcesNotReadyError as error:
                from .layout import LayoutModelError

                raise LayoutModelError("pdf_resources_not_ready") from error
        self._get_delegate().ensure_model_available()

    async def detect_layout_batch(
        self,
        input_images: list[np.ndarray],
    ) -> list[DocumentLayout]:
        """复用唯一 delegate 完成批量布局检测。"""

        return await self._get_delegate().detect_layout_batch(input_images)

    def _get_delegate(self) -> _LayoutDelegate:
        """用双重检查保证并发 PDF job 只创建一个 detector。"""

        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._delegate_lock:
            if self._delegate is None:
                factory = self._detector_factory
                if factory is None:
                    from .layout import LayoutDetector

                    factory = LayoutDetector
                self._delegate = factory(
                    self.model_path,
                    max_concurrency=self._max_concurrency,
                    intra_op_threads=self._intra_op_threads,
                    verify_checksum=self._verify_model_checksum,
                )
            return self._delegate
