"""验证跨 job 共享的模型并发预算。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

import pytest

from modules.translation.model_runtime import ModelConcurrencyRegistry


def test_registry_caps_parallel_slots_for_the_same_model() -> None:
    """不同线程模拟的 job 不得突破同一模型的共享上限。"""

    registry = ModelConcurrencyRegistry()
    key = ("deepseek", "deepseek-v4-flash")
    registry.configure(key, 3)
    barrier = Barrier(8)
    lock = Lock()
    active = 0
    peak = 0

    def run_request() -> None:
        """同步起跑后占用模型槽并记录峰值。"""

        nonlocal active, peak
        barrier.wait()
        with registry.slot(key):
            with lock:
                active += 1
                peak = max(peak, active)
            sleep(0.02)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(run_request) for _ in range(8)]
        for future in futures:
            future.result()

    assert peak == 3
    assert registry.snapshot(key) is not None
    assert registry.snapshot(key).active == 0  # type: ignore[union-attr]


def test_registry_keeps_different_models_independent() -> None:
    """一个模型的满载不能占用另一个模型的独立预算。"""

    registry = ModelConcurrencyRegistry()
    first = ("deepseek", "deepseek-v4-flash")
    second = ("deepseek", "deepseek-v4-pro")
    registry.configure(first, 1)
    registry.configure(second, 4)

    assert registry.snapshot(first).limit == 1  # type: ignore[union-attr]
    assert registry.snapshot(second).limit == 4  # type: ignore[union-attr]


@pytest.mark.parametrize("limit", [0, 33, True])
def test_registry_rejects_unsafe_limits(limit: int) -> None:
    """数据库或调用方不能绕过统一的并发范围。"""

    registry = ModelConcurrencyRegistry()

    with pytest.raises(ValueError, match="between 1 and 32"):
        registry.configure(("provider", "model"), limit)
