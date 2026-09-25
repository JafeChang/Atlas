"""T-120 端到端集成与接线（组合根，SPEC §4.0 / §4.2）。

本包是**唯一**允许 import 各域**实现**的地方：它的职责不是实现能力，而是把已经
提交的包接成一条**可运行的流水线**，并用真实数据把它跑通（SPEC 硬规则 1）。

```
collect ──▶ archive ──▶ normalize ──┬──▶ feed
（T-102）   （T-103）    （T-104）    └──▶ label（T-108，人工产物）
                          │
                          └── atlas.contracts 的三态类型在阶段之间传递
```

接线方式
--------

1. 每个阶段是一个 `atlas.contracts.AtlasTask`，用 `atlas.runner.TaskGraph` 声明依赖边，
   由 `atlas.runner.TaskRunner` 按拓扑序执行（幂等跳过 / 有界重试 / 失败可见都由 T-110 负责）。
2. 阶段的**输入快照**由 `NodeInputs` 从**上游节点的执行记录**里取（内容寻址的 `identity`
   部分），因此换解析器 / 换窗口会重算、内容不变则幂等跳过；跳过时上游产物仍可被下游读到。
3. 所有外部依赖（fetcher / robots / 限速 / 时钟）都是**构造注入**的；默认装配是真实实现
   （`UrllibFetcher` + `RobotsCache` + `DomainThrottle`），测试注入假 fetcher，一条网络都不打。
4. 失败一律向上传播：采集阶段任何渠道失败就抛 `CollectionFailedError`，
   由 `TaskRunner` 包装成 `TaskFailedError` 并附上 `partial_report`（失败节点 + 被阻塞的下游）。
   **没有任何路径会吞掉异常后报告成功。**

最小用法::

    from atlas.compose import build_pipeline

    with build_pipeline(store_root="data/store", actor="me") as pipeline:
        report = pipeline.run()          # 真实采集：走 robots + 限速
        report.result("feed").output.artifacts["identity"]["items"]

真实抓取默认关闭（CLI 要求 `ATLAS_LIVE=1`，见 `atlas.compose.cli`）。
"""

from __future__ import annotations

from .cli import main
from .pipeline import (
    NODE_ARCHIVE,
    NODE_COLLECT,
    NODE_FEED,
    NODE_LABEL,
    NODE_NORMALIZE,
    ComposeDependencies,
    FileExecutionRecordStore,
    LabelAssignment,
    NodeInputs,
    Pipeline,
    PipelineConfig,
    build_pipeline,
)
from .tasks import (
    COMPOSE_CODE_VERSION,
    ArchiveStage,
    CollectStage,
    CollectionFailedError,
    FeedStage,
    LabelStage,
    NormalizeStage,
    NormalizeStageError,
    PipelineError,
    StageInputError,
    StageLabelError,
    parse_window,
    utc_hour_window,
)

__all__ = [
    "COMPOSE_CODE_VERSION",
    "NODE_ARCHIVE",
    "NODE_COLLECT",
    "NODE_FEED",
    "NODE_LABEL",
    "NODE_NORMALIZE",
    # pipeline
    "ComposeDependencies",
    "FileExecutionRecordStore",
    "LabelAssignment",
    "NodeInputs",
    "Pipeline",
    "PipelineConfig",
    "build_pipeline",
    # tasks
    "ArchiveStage",
    "CollectStage",
    "CollectionFailedError",
    "FeedStage",
    "LabelStage",
    "NormalizeStage",
    "NormalizeStageError",
    "PipelineError",
    "StageInputError",
    "StageLabelError",
    "parse_window",
    "utc_hour_window",
    # cli
    "main",
]
