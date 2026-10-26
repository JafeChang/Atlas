"""T-207 最小调度器：**按 `interval_seconds` 只采到期渠道**。

本包补上 SPEC §6.7 记录的第四个缺口（"两个'周期性'能力，没有任何调度器"），
并因此让 `channel.interval_seconds` 从"已配置但无效"变成真正决定行为的参数。
设计、边界约定、系统 cron 示例与"调度 vs 幂等"的两层职责，全部写在
`atlas.schedule.core` 的模块文档里（单一事实来源）。

对外只用三样东西：

::

    from atlas.schedule import evaluate_schedule, open_last_collection_source

    source = open_last_collection_source("data/store")     # 只读
    decision = evaluate_schedule(channels, source.last_collected_at(), now)
    decision.due_ids                                        # 这一轮该试哪些渠道

`now` 必须由调用方注入（纯函数，见 `core.due_channels`）。
"""

from __future__ import annotations

from .core import (
    DEFAULT_DB_FILENAME,
    RAW_RECORDS_TABLE,
    ChannelSchedule,
    DueDecision,
    LastCollectionSource,
    MalformedTimestampError,
    NoSchedulableChannelError,
    ScheduleError,
    SqliteLastCollectionSource,
    due_channels,
    evaluate_schedule,
    open_last_collection_source,
    parse_timestamp,
    read_last_collected,
)

__all__ = [
    "DEFAULT_DB_FILENAME",
    "RAW_RECORDS_TABLE",
    "ChannelSchedule",
    "DueDecision",
    "LastCollectionSource",
    "MalformedTimestampError",
    "NoSchedulableChannelError",
    "ScheduleError",
    "SqliteLastCollectionSource",
    "due_channels",
    "evaluate_schedule",
    "open_last_collection_source",
    "parse_timestamp",
    "read_last_collected",
]
