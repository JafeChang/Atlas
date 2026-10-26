"""T-110 最小任务运行器：依赖图 + 幂等执行 + 有界重试 + 日志。

引擎无关——只用 stdlib 与已装依赖（pydantic），不引入任何编排引擎。
"""

from .graph import (
    CycleError,
    DuplicateTaskError,
    GraphError,
    MissingDependencyError,
    TaskGraph,
)
from .runner import (
    ExecutionRecord,
    ExecutionRecordStore,
    InMemoryExecutionRecordStore,
    RunReport,
    TaskFailedError,
    TaskRunner,
    TaskRunResult,
)

__all__ = [
    # graph
    "CycleError",
    "DuplicateTaskError",
    "GraphError",
    "MissingDependencyError",
    "TaskGraph",
    # runner
    "ExecutionRecord",
    "ExecutionRecordStore",
    "InMemoryExecutionRecordStore",
    "RunReport",
    "TaskFailedError",
    "TaskRunner",
    "TaskRunResult",
]
