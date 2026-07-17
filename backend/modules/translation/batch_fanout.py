"""为同步文档 pipeline 提供按原顺序提交的单 job 有界 batch fan-out。"""

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class BatchFanoutOutcome[ResultT]:
    """保存一个 group 已收敛的结果或隔离后的异常。"""

    value: ResultT | None = None
    error: Exception | None = None


def translator_per_job_concurrency(translator: object) -> int:
    """读取 translator 的单 job 上限, 未暴露或值无效时保持串行。"""

    try:
        configured = getattr(translator, "per_job_concurrency", 1)
    except Exception:
        return 1
    if isinstance(configured, bool) or not isinstance(configured, int):
        return 1
    return max(1, configured)


def run_batch_fanout[GroupT, ResultT](
    groups: Sequence[GroupT],
    worker: Callable[[GroupT], ResultT],
    *,
    max_concurrency: int,
    on_group_settled: Callable[
        [int, GroupT, BatchFanoutOutcome[ResultT]],
        None,
    ],
) -> tuple[BatchFanoutOutcome[ResultT], ...]:
    """并发执行 group, 并在主线程按输入顺序提交已收敛结果。

    worker 应包含一次 group 的完整 provider、repair 与 fallback 前置计算。
    ``on_group_settled`` 只在 group 已得到结果或明确异常后调用, 因此调用方可在
    回调里安全写回结果并推进 progress。后完成的 group 会等待此前 group 收敛,
    避免并发完成顺序改变文档写回与可观察进度顺序。
    """

    if not groups:
        return ()

    worker_count = min(max(1, max_concurrency), len(groups))
    outcomes: list[BatchFanoutOutcome[ResultT] | None] = [None] * len(groups)

    def settle(group_index: int, outcome: BatchFanoutOutcome[ResultT]) -> None:
        """记录单个完成项, 实际顺序提交由外层连续前缀控制。"""

        outcomes[group_index] = outcome

    if worker_count == 1:
        for group_index, group in enumerate(groups):
            outcome = _run_group(worker, group)
            settle(group_index, outcome)
            on_group_settled(group_index, group, outcome)
        return cast(tuple[BatchFanoutOutcome[ResultT], ...], tuple(outcomes))

    next_to_commit = 0
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="pageferry-batch",
    ) as executor:
        futures: dict[Future[BatchFanoutOutcome[ResultT]], int] = {}
        next_to_submit = 0

        def submit_next() -> None:
            """只补一个空闲 worker, 避免为整份大文档预建无界 futures。"""

            nonlocal next_to_submit
            if next_to_submit >= len(groups):
                return
            group_index = next_to_submit
            next_to_submit += 1
            future = executor.submit(_run_group, worker, groups[group_index])
            futures[future] = group_index

        for _ in range(worker_count):
            submit_next()

        while futures:
            completed, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                settle(futures.pop(future), future.result())
                # 完成一个再补一个, 排队与执行中的 future 总数始终不超过
                # worker_count, 不随文档 group 数量增长。
                submit_next()
            # 只提交已经连续收敛的 group 前缀。这样既保留并发吞吐, 也不会让
            # 后完成顺序改变写回和 progress 的可观察行为。
            while next_to_commit < len(groups):
                outcome = outcomes[next_to_commit]
                if outcome is None:
                    break
                on_group_settled(next_to_commit, groups[next_to_commit], outcome)
                next_to_commit += 1

    return cast(tuple[BatchFanoutOutcome[ResultT], ...], tuple(outcomes))


def _run_group[GroupT, ResultT](
    worker: Callable[[GroupT], ResultT],
    group: GroupT,
) -> BatchFanoutOutcome[ResultT]:
    """把一个 group 的异常留在自己的 outcome, 避免取消其他 group。"""

    try:
        return BatchFanoutOutcome(value=worker(group))
    except Exception as error:
        return BatchFanoutOutcome(error=error)
