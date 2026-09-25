"""T-110 执行器：按拓扑序执行、幂等跳过、有界重试、日志、失败可见。

对应 SPEC §3 的四条任务规范，逐条落到代码：

- **幂等**：执行前用 `AtlasTask.idempotency_key(inputs, config)` 查执行记录存储；
  同键已有成功记录则**跳过**，并写一条 `skipped` 结果（含跳过的原因与来源键），
  **不静默跳过**——跳过会出现在日志、`TaskRunResult.reason` 与 `RunReport` 里。
- **可重试、失败不留半成品**：顺序固定为
  `run()` → **成功后**才 `record()` 提交执行记录。
  幂等键先算出来、产物先拿到，提交是最后一步且只提交原子一条记录；
  因此"重试"面对的状态永远是"要么没有任何记录（干净），要么有一条完整的成功记录"，
  不存在"跑了一半、记录半成品"的中间态。失败的那次尝试**不写任何记录**。
- **失败可见**：重试次数用尽后抛 `TaskFailedError`，并**向上传播**；
  该任务的全部下游节点被标记为 `blocked`（依赖未成功，绝不执行）。
  `finally` 里只做"记录日志 + 汇总结果"这类不会掩盖异常的事，
  且不捕获异常本身（`except` 只用于在重抛前补上下文，且必定 `raise`）。
- **不伪造结果**：只有 `task.execute()` 真的返回了 `TaskOutput` 才可能产生
  `succeeded` 记录；异常路径、跳过路径都不会产生成功记录。

执行记录的存储是**可插入的**（`ExecutionRecordStore`），默认提供进程内实现
（`InMemoryExecutionRecordStore`）；T-103/T-108 落地后换成 sqlite 实现即可，
执行器不需要改。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from pydantic import Field

from atlas.contracts import AtlasTask, ContractError, ContractModel, Snapshot, TaskOutput

from .graph import TaskGraph

__all__ = [
    "ExecutionRecord",
    "ExecutionRecordStore",
    "InMemoryExecutionRecordStore",
    "RunReport",
    "TaskFailedError",
    "TaskRunResult",
    "TaskRunner",
]

logger = logging.getLogger("atlas.runner")

#: 结果状态。只用这四个，且互斥：没有"部分成功"这种含糊状态。
STATUS_SUCCEEDED = "succeeded"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"


class ExecutionRecord(ContractModel):
    """一条已完成执行的记录（只增不改）。

    只有**真的跑完并拿到产物**的任务才会产生这条记录；失败与跳过都不产生它。
    幂等查询只认这张表，因此"没有记录"与"有记录"是判断"要不要跑"的唯一依据。
    """

    task_name: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    output: TaskOutput
    attempt: int = Field(ge=1)
    duration_seconds: float = Field(ge=0.0)

    def artifacts(self) -> Dict[str, Any]:
        return dict(self.output.artifacts)


class ExecutionRecordStore(Protocol):
    """执行记录存储的最小接口。

    刻意保持最小：`get` / `put` 两个方法。这样换成 sqlite（T-103/T-108 的存储层）
    或换成外部编排引擎的状态后端时，只换实现，不改执行器。
    """

    def get(self, idempotency_key: str) -> Optional[ExecutionRecord]:
        """按键取记录；不存在返回 `None`（不抛异常、不返回假记录）。"""
        ...

    def put(self, record: ExecutionRecord) -> None:
        """提交一条完整记录（原子）。不允许覆盖同键的已有记录。"""
        ...


class InMemoryExecutionRecordStore:
    """进程内实现，用于测试与首次真实验证；不作为持久化方案。"""

    def __init__(self, records: Optional[Iterable[ExecutionRecord]] = None) -> None:
        self._records: Dict[str, ExecutionRecord] = {}
        for record in records or ():
            self.put(record)

    def get(self, idempotency_key: str) -> Optional[ExecutionRecord]:
        return self._records.get(idempotency_key)

    def put(self, record: ExecutionRecord) -> None:
        existing = self._records.get(record.idempotency_key)
        if existing is not None:
            raise ContractError(
                f"幂等键 {record.idempotency_key[:12]}… 已有执行记录"
                f"（任务 {existing.task_name}），拒绝覆盖："
                "同键重复提交意味着幂等判定被绕过或键不稳定"
            )
        self._records[record.idempotency_key] = record

    def __len__(self) -> int:
        return len(self._records)

    def keys(self) -> Tuple[str, ...]:
        return tuple(self._records)

    def drop(self, idempotency_key: str) -> Optional[ExecutionRecord]:
        """删除一条记录（**仅测试 / 恢复场景**）。

        生产路径上记录只增不改；这个方法存在是为了让"某个下游节点需要重算而
        上游仍有效"这种情况可被显式构造出来，从而验证幂等跳过不会误伤下游。
        """
        return self._records.pop(idempotency_key, None)


class TaskFailedError(ContractError):
    """任务在有界重试内未能成功。携带任务名、尝试次数与最后一次异常。

    **向上传播**：执行器不吞掉它（CLAUDE.md 硬规则 2）。

    `partial_report` 是失败时刻的报告：含已成功/已跳过的节点，以及失败节点本身
    与被它阻塞的下游节点。这样"失败可见"不必依赖调用方把执行器换成宽容模式。
    """

    def __init__(
        self,
        task_name: str,
        attempts: int,
        last_error: BaseException,
        partial_report: Optional["RunReport"] = None,
    ) -> None:
        self.task_name = task_name
        self.attempts = attempts
        self.last_error = last_error
        self.partial_report = partial_report
        super().__init__(
            f"任务 {task_name!r} 在 {attempts} 次尝试后仍失败："
            f"{type(last_error).__name__}: {last_error}"
        )


class TaskRunResult(ContractModel):
    """单个节点的执行结论。`status` 只有三种取值，见模块常量。"""

    task_name: str
    status: str
    attempts: int = Field(ge=0)
    idempotency_key: str
    reason: str
    output: Optional[TaskOutput] = None
    error: Optional[str] = None

    def succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    def failed(self) -> bool:
        return self.status == STATUS_FAILED


class RunReport(ContractModel):
    """一次整图执行的汇总。用于"失败可见"与验收取证。"""

    results: Tuple[TaskRunResult, ...] = ()
    order: Tuple[str, ...] = ()

    def result(self, task_name: str) -> TaskRunResult:
        for item in self.results:
            if item.task_name == task_name:
                return item
        raise KeyError(f"报告中不含任务 {task_name!r}")

    def by_status(self, status: str) -> Tuple[TaskRunResult, ...]:
        return tuple(item for item in self.results if item.status == status)

    @property
    def succeeded(self) -> Tuple[TaskRunResult, ...]:
        return self.by_status(STATUS_SUCCEEDED)

    @property
    def skipped(self) -> Tuple[TaskRunResult, ...]:
        return self.by_status(STATUS_SKIPPED)

    @property
    def blocked(self) -> Tuple[TaskRunResult, ...]:
        return self.by_status(STATUS_BLOCKED)

    @property
    def failed(self) -> Tuple[TaskRunResult, ...]:
        return self.by_status(STATUS_FAILED)

    def all_succeeded(self) -> bool:
        return len(self.succeeded) == len(self.results)


class TaskRunner:
    """按拓扑序执行整张图。引擎无关、无全局状态。"""

    def __init__(
        self,
        graph: TaskGraph,
        store: ExecutionRecordStore,
        *,
        config: Optional[Snapshot] = None,
        max_retries: int = 1,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        """
        Args:
            graph: 依赖图（本身不执行任务）。
            store: 执行记录存储，幂等判定的唯一依据。
            config: 传给每个任务的配置快照（默认空快照）。同一配置快照进入幂等键，
                因此"配置变了 → 键变了 → 会重跑"，这正是 SPEC §3 想要的语义。
            max_retries: **重试**次数（默认 1 次重试 ⇒ 最多 2 次尝试）。
                必须 >= 0；负数无意义，直接抛 `ValueError`。
        """
        if max_retries < 0:
            raise ValueError(f"max_retries 必须 >= 0，收到 {max_retries}")
        self._graph = graph
        self._store = store
        self._config = config or Snapshot()
        self._max_retries = max_retries
        self._logger = logger_ or logger

    # ------------------------------------------------------------------ 查询

    @property
    def graph(self) -> TaskGraph:
        return self._graph

    @property
    def store(self) -> ExecutionRecordStore:
        return self._store

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def max_attempts(self) -> int:
        """总尝试次数上限 = 1 次首跑 + `max_retries` 次重试。"""
        return self._max_retries + 1

    # ------------------------------------------------------------------ 执行

    def run(
        self,
        inputs: Mapping[str, Snapshot],
        *,
        targets: Optional[Iterable[str]] = None,
    ) -> RunReport:
        """按拓扑序执行。

        Args:
            inputs: 节点名 → 输入快照。缺省时该节点用空快照
                （空快照也是确定输入，不是"随机默认值"）。
            targets: 只跑这些节点及其传递先决条件；`None` 表示整图。

        Returns:
            `RunReport`，含每个节点的结论与真实执行顺序。

        Raises:
            TaskFailedError: 某节点重试用尽仍失败。**异常向上传播**；异常上的
                `partial_report` 会说明哪些节点已成功/跳过、哪个节点失败、
                哪些下游节点因此被 `blocked`，失败因此完全可见。
            ContractError 子类: 图本身非法（成环、目标不存在等）。
        """
        order = self._execution_order(targets)
        results: List[TaskRunResult] = []
        done: set[str] = set()

        for name in order:
            task = self._graph.task(name)
            failures = [dep for dep in self._graph.dependencies(name) if dep not in done]
            if failures:
                results.append(self._blocked_result(name, failures))
                continue

            try:
                result = self._run_one(name, task, inputs.get(name, Snapshot()))
            except TaskFailedError as exc:
                results.append(self._failed_result(name, exc, inputs.get(name, Snapshot())))
                # 失败节点自身不再重试。只有**真的依赖失败节点**的节点才标 blocked：
                # 与失败无依赖关系的分支被标成 blocked 是谎报，所以用传递后继算清楚。
                broken = self._graph.descendants(name)
                for later in order[order.index(name) + 1 :]:
                    if later in done or later not in broken:
                        continue
                    results.append(
                        self._blocked_result(
                            later, [name], cause=f"任务 {name!r} 执行失败"
                        )
                    )
                partial = RunReport(results=tuple(results), order=tuple(order))
                self._log_summary(partial)
                exc.partial_report = partial
                raise

            results.append(result)
            if result.succeeded() or result.status == STATUS_SKIPPED:
                done.add(name)

        report = RunReport(results=tuple(results), order=tuple(order))
        self._log_summary(report)
        return report

    def run_task(self, name: str, inputs: Optional[Snapshot] = None) -> TaskRunResult:
        """只跑单个节点，**不检查依赖**（依赖由调用方保证）。

        存在只为让幂等 / 重试的语义可被单独验证；整图执行请用 `run()`。
        """
        return self._run_one(name, self._graph.task(name), inputs or Snapshot())

    # ------------------------------------------------------------------ 内部

    def _blocked_result(
        self,
        name: str,
        failed_upstream: Iterable[str],
        *,
        cause: Optional[str] = None,
    ) -> TaskRunResult:
        """构造 `blocked` 结论：依赖未满足时绝不执行节点本身。"""
        upstream = sorted(dict.fromkeys(failed_upstream))
        reason = (
            cause or f"上游未成功：{upstream}"
        ) + "；依赖未满足时绝不执行，避免基于缺失输入产出伪造结果"
        self._logger.error("任务 %s 被阻塞：%s", name, reason)
        return TaskRunResult(
            task_name=name,
            status=STATUS_BLOCKED,
            attempts=0,
            idempotency_key="",
            reason=reason,
            error=reason,
        )

    def _failed_result(
        self, name: str, error: TaskFailedError, inputs: Snapshot
    ) -> TaskRunResult:
        """把失败如实写进报告；这里既不是 `succeeded` 也不是 `skipped`。"""
        reason = f"执行失败（{error.attempts} 次尝试均未成功）：{error.last_error}"
        return TaskRunResult(
            task_name=name,
            status=STATUS_FAILED,
            attempts=error.attempts,
            idempotency_key=self._graph.task(name).idempotency_key(inputs, self._config),
            reason=reason,
            error=f"{type(error.last_error).__name__}: {error.last_error}",
        )

    def _execution_order(self, targets: Optional[Iterable[str]]) -> List[str]:
        full = self._graph.topological_order()  # 成环在这里抛 CycleError
        if targets is None:
            return full
        wanted = list(dict.fromkeys(targets))
        unknown = [name for name in wanted if name not in self._graph]
        if unknown:
            raise KeyError(f"图中没有任务 {sorted(unknown)}")
        keep: set[str] = set()
        for name in wanted:
            keep.add(name)
            keep.update(self._graph.ancestors(name))
        return [name for name in full if name in keep]

    def _run_one(self, name: str, task: AtlasTask, inputs: Snapshot) -> TaskRunResult:
        key = task.idempotency_key(inputs, self._config)

        # 幂等：先查记录，同键已完成就跳过，并说清跳过原因。
        existing = self._store.get(key)
        if existing is not None:
            reason = (
                f"幂等跳过：幂等键 {key[:12]}… 已有成功执行记录"
                f"（任务 {existing.task_name}，第 {existing.attempt} 次尝试，"
                f"耗时 {existing.duration_seconds:.3f}s）；输入与配置均未变化，不重复执行"
            )
            self._logger.info("任务 %s 跳过：%s", name, reason)
            return TaskRunResult(
                task_name=name,
                status=STATUS_SKIPPED,
                attempts=0,
                idempotency_key=key,
                reason=reason,
                output=existing.output,
            )

        last_error: Optional[BaseException] = None
        for attempt in range(1, self.max_attempts + 1):
            self._logger.info(
                "任务 %s 开始（第 %d/%d 次尝试，幂等键 %s…）",
                name,
                attempt,
                self.max_attempts,
                key[:12],
            )
            started = _monotonic()
            try:
                output = task.execute(inputs, self._config)
            except Exception as exc:  # 重抛前的上下文补充，不是吞异常
                last_error = exc
                self._logger.error(
                    "任务 %s 第 %d/%d 次尝试失败：%s: %s；未提交任何执行记录（状态干净）",
                    name,
                    attempt,
                    self.max_attempts,
                    type(exc).__name__,
                    exc,
                )
                if attempt < self.max_attempts:
                    self._logger.warning("任务 %s 将重试（剩余 %d 次）", name, self.max_attempts - attempt)
                continue

            duration = _monotonic() - started
            record = ExecutionRecord(
                task_name=name,
                idempotency_key=key,
                output=output,
                attempt=attempt,
                duration_seconds=duration,
            )
            # 提交是最后一步，且只有一条原子记录：重试因此从干净状态出发。
            self._store.put(record)
            self._logger.info(
                "任务 %s 结束：成功（第 %d 次尝试，耗时 %.3fs，产物字段 %s）",
                name,
                attempt,
                duration,
                sorted(output.artifacts),
            )
            return TaskRunResult(
                task_name=name,
                status=STATUS_SUCCEEDED,
                attempts=attempt,
                idempotency_key=key,
                reason=f"执行成功（第 {attempt} 次尝试）",
                output=output,
            )

        assert last_error is not None  # 循环至少跑一次，无异常则必然已 return
        self._logger.error(
            "任务 %s 失败：%d 次尝试均未成功，异常向上传播", name, self.max_attempts
        )
        raise TaskFailedError(name, self.max_attempts, last_error)

    def _log_summary(self, report: RunReport) -> None:
        self._logger.info(
            "执行汇总：成功 %d，跳过 %d，失败 %d，阻塞 %d，顺序 %s",
            len(report.succeeded),
            len(report.skipped),
            len(report.failed),
            len(report.blocked),
            list(report.order),
        )


def _monotonic() -> float:
    return time.monotonic()
