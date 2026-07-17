"""验证单 job batch fan-out 的并发、顺序提交与异常隔离 contract。"""

from threading import Event, Lock, Thread

import pytest

from modules.translation.batch_fanout import (
    BatchFanoutOutcome,
    run_batch_fanout,
    translator_per_job_concurrency,
)


class _ConfiguredTranslator:
    """暴露测试指定的单 job 并发上限。"""

    per_job_concurrency = 6


class _RaisingTranslator:
    """模拟读取并发配置时自身失败的第三方 translator。"""

    @property
    def per_job_concurrency(self) -> int:
        """抛出异常以验证调度层稳定回退串行。"""

        raise RuntimeError("broken concurrency property")


class _TrackingGroups:
    """记录调度层实际取出的 group, 用于观察 future 滑动窗口。"""

    def __init__(self, count: int) -> None:
        """保存虚拟 group 数并初始化线程安全访问记录。"""

        self._count = count
        self._lock = Lock()
        self._accessed: list[int] = []

    def __len__(self) -> int:
        """返回虚拟 group 总数。"""

        return self._count

    def __getitem__(self, index: int) -> int:
        """按需返回 group index, 越界时遵守 sequence contract。"""

        if index < 0 or index >= self._count:
            raise IndexError(index)
        with self._lock:
            self._accessed.append(index)
        return index

    def accessed(self) -> tuple[int, ...]:
        """返回当前已被调度层取出的 group index 快照。"""

        with self._lock:
            return tuple(self._accessed)


def test_translator_concurrency_is_opt_in_and_invalid_values_fall_back_to_one() -> None:
    """只有显式的正整数配置才允许 pipeline 开启并发。"""

    assert translator_per_job_concurrency(_ConfiguredTranslator()) == 6
    assert translator_per_job_concurrency(object()) == 1
    assert translator_per_job_concurrency(_RaisingTranslator()) == 1

    for invalid in (None, True, 0, -3, 2.5, "4"):
        translator = type("InvalidTranslator", (), {"per_job_concurrency": invalid})()
        assert translator_per_job_concurrency(translator) == 1


def test_fanout_never_exceeds_configured_concurrency() -> None:
    """阻塞首批 worker, 确认后续 group 不会越过 permit 提前运行。"""

    lock = Lock()
    release = Event()
    reached_limit = Event()
    active = 0
    peak = 0
    stored: dict[str, tuple[BatchFanoutOutcome[int], ...]] = {}
    groups = _TrackingGroups(6)

    def worker(value: int) -> int:
        """记录活动 worker 数, 并让首批任务停在可观察边界。"""

        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                reached_limit.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return value * 10

    def run() -> None:
        """在辅助线程运行同步 fan-out, 让测试线程检查阻塞中的并发数。"""

        stored["outcomes"] = run_batch_fanout(
            groups,
            worker,
            max_concurrency=2,
            on_group_settled=lambda _index, _group, _outcome: None,
        )

    runner = Thread(target=run)
    runner.start()
    try:
        assert reached_limit.wait(timeout=2)
        with lock:
            assert active == 2
            assert peak == 2
        # 首批 worker 全部阻塞时只能取出两个 group。若一次性 submit 全文,
        # 这里会已经访问全部六个 index。
        assert groups.accessed() == (0, 1)
    finally:
        release.set()
        runner.join(timeout=2)

    assert not runner.is_alive()
    assert [outcome.value for outcome in stored["outcomes"]] == [0, 10, 20, 30, 40, 50]


def test_fanout_commits_in_input_order_and_isolates_one_group_error() -> None:
    """后续成功项不能越序提交, 单项异常也不能取消其他 group。"""

    later_group_finished = Event()
    committed: list[tuple[int, str, int | None, str | None]] = []

    def worker(group: str) -> int:
        """强制第三组先于第一组完成, 并让中间组抛出异常。"""

        if group == "first":
            assert later_group_finished.wait(timeout=2)
            return 1
        if group == "broken":
            raise RuntimeError("synthetic group failure")
        later_group_finished.set()
        return 3

    def commit(
        index: int,
        group: str,
        outcome: BatchFanoutOutcome[int],
    ) -> None:
        """记录调度层对 pipeline 暴露的提交顺序与隔离结果。"""

        committed.append(
            (
                index,
                group,
                outcome.value,
                str(outcome.error) if outcome.error is not None else None,
            )
        )

    outcomes = run_batch_fanout(
        ("first", "broken", "third"),
        worker,
        max_concurrency=3,
        on_group_settled=commit,
    )

    assert committed == [
        (0, "first", 1, None),
        (1, "broken", None, "synthetic group failure"),
        (2, "third", 3, None),
    ]
    assert outcomes[0].value == 1
    assert isinstance(outcomes[1].error, RuntimeError)
    assert outcomes[2].value == 3


@pytest.mark.parametrize("configured", [1, 2, 8])
def test_empty_fanout_does_not_invoke_callback(configured: int) -> None:
    """空文档不创建 worker, 也不伪造 progress 完成项。"""

    called = False

    def commit(
        _index: int,
        _group: str,
        _outcome: BatchFanoutOutcome[str],
    ) -> None:
        """若空输入错误触发回调, 就留下可断言标记。"""

        nonlocal called
        called = True

    assert (
        run_batch_fanout(
            (),
            lambda group: group,
            max_concurrency=configured,
            on_group_settled=commit,
        )
        == ()
    )
    assert called is False
