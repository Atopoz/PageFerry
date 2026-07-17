"""在进程生命周期内共享同一模型的并发预算。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Lock

DEFAULT_PER_JOB_CONCURRENCY = 6
DEFAULT_GLOBAL_CONCURRENCY = 15
MAX_MODEL_CONCURRENCY = 32

ModelRuntimeKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ModelConcurrencySnapshot:
    """暴露一个模型 limiter 的只读调试状态。"""

    limit: int
    active: int


class _ModelCapacity:
    """用 condition 支持运行中收紧或放宽并发上限。"""

    def __init__(self, limit: int) -> None:
        """保存初始容量并创建独立等待队列。"""

        self._condition = Condition()
        self._limit = _validate_limit(limit)
        self._active = 0

    def configure(self, limit: int) -> None:
        """更新容量; 已在途请求自然完成, 新请求遵守新上限。"""

        normalized = _validate_limit(limit)
        with self._condition:
            self._limit = normalized
            self._condition.notify_all()

    @contextmanager
    def slot(self) -> Iterator[None]:
        """等待并占用一个请求槽, 离开上下文时可靠归还。"""

        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def snapshot(self) -> ModelConcurrencySnapshot:
        """返回不会泄露 provider 配置的容量与占用数。"""

        with self._condition:
            return ModelConcurrencySnapshot(limit=self._limit, active=self._active)


class ModelConcurrencyRegistry:
    """按 provider 与 upstream model 共享跨 job limiter。"""

    def __init__(self) -> None:
        """初始化线程安全的空 limiter registry。"""

        self._lock = Lock()
        self._capacities: dict[ModelRuntimeKey, _ModelCapacity] = {}

    def configure(self, key: ModelRuntimeKey, limit: int) -> None:
        """创建或更新一个模型的全局并发容量。"""

        capacity = self._capacity(key, default_limit=limit)
        capacity.configure(limit)

    @contextmanager
    def slot(
        self,
        key: ModelRuntimeKey,
        *,
        default_limit: int = DEFAULT_GLOBAL_CONCURRENCY,
    ) -> Iterator[None]:
        """占用同一模型跨所有 job 共享的一个请求槽。"""

        capacity = self._capacity(key, default_limit=default_limit)
        with capacity.slot():
            yield

    def snapshot(self, key: ModelRuntimeKey) -> ModelConcurrencySnapshot | None:
        """读取已创建 limiter 的状态, 未使用过的模型返回空值。"""

        with self._lock:
            capacity = self._capacities.get(key)
        return capacity.snapshot() if capacity is not None else None

    def _capacity(self, key: ModelRuntimeKey, *, default_limit: int) -> _ModelCapacity:
        """原子取得 limiter, 首次使用时按有效默认值创建。"""

        normalized_key = _validate_key(key)
        normalized_limit = _validate_limit(default_limit)
        with self._lock:
            capacity = self._capacities.get(normalized_key)
            if capacity is None:
                capacity = _ModelCapacity(normalized_limit)
                self._capacities[normalized_key] = capacity
            return capacity


def _validate_key(key: ModelRuntimeKey) -> ModelRuntimeKey:
    """拒绝会让不同上游请求意外共享或绕过 limiter 的空 key。"""

    if len(key) != 2 or any(not value or not value.strip() for value in key):
        raise ValueError("model runtime key must contain provider and upstream model ids")
    return key


def _validate_limit(limit: int) -> int:
    """把 limiter 容量限制在 UI 与数据库共同支持的安全范围。"""

    if isinstance(limit, bool) or not 1 <= limit <= MAX_MODEL_CONCURRENCY:
        raise ValueError(f"model concurrency must be between 1 and {MAX_MODEL_CONCURRENCY}")
    return limit
