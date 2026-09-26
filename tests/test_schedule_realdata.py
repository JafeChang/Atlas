"""T-207 真实数据证据：对**真实注册表 + 真实归档库**算"这一轮该采哪些渠道"。

数据从哪来（**只读，一个字节都不写**）
--------------------------------------

- 注册表：`data/store/atlas.db` 的 `channels` 表（T-101 的物化投影）。
- 调度状态：同一库里 `raw_records`（T-103 的事实表）的
  `MAX(fetched_at) GROUP BY channel_id`。**没有新表、没有新状态文件**。
- 全程 `file:...?mode=ro`；测试开始 / 结束各取一次库文件 sha256，必须相同。

首次测量（2026-09-26，`data/store/atlas.db`）
---------------------------------------------

| 项 | 真实值 |
|---|---|
| 可采集渠道 | **13** 个（启用渠道 × 启用行业） |
| `interval_seconds` 分布 | **1800s × 3**、**3600s × 6**、**7200s × 4** |
| `raw_records` 总数 | **75** 行，覆盖 **11** 个渠道 |
| 从未采集（⇒ 必然到期） | **2** 个：`openai-blog`、`venturebeat-ai` |
| `now = 2026-09-26T02:00:00+00:00` 时到期 | **9 / 13** |

最后一行是本次任务的**核心证据**：7 个 `last_collected_at = 2026-09-26T01:00:00+00:00`
的渠道里，`interval=3600` 的到期、`interval=7200` 的未到期 —— **同一时刻、同一个
`last_collected_at`，只因 `interval_seconds` 不同就给出不同结论**。在本包之前，
`interval_seconds` 恒等于全局下限，这两个渠道的行为毫无差别（SPEC §6.7）。

一个必须说明的落差（如实报数，不粉饰）
--------------------------------------

真实库里 7 个渠道的 `last_collected_at` 落在这个"未来"时刻（`2026-09-26T01:00:00`），
而写这份测试时的系统时钟早于它。因此**用真实时钟**跑时，那 7 个渠道都判"未到期"
（`now < last`，属 `due_channels()` 明确支持的正常情形），只有 2 个从未采集的渠道到期。
本测试因此**显式注入 `now`**：判定可复现，而且能把"到达下一个整点后确实有 9 个到期"
这件事钉住。两种 `now` 都有断言（见下面两个用例）。

数据不在时自跳过
----------------

`data/` 不进 git（SPEC §8.1），干净 worktree 里没有它 —— 真实数字那几条 `skip`
（不是失败）。**不依赖 `data/` 的端到端证据照常真跑**：
`tests/test_schedule_core.py` 在 `tmp_path` 里用真实归档写路径造出真实
`raw_records`，再走真实磁盘读路径（不是内存替身）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

from atlas.registry import RegistryService, open_store as open_registry
from atlas.schedule import evaluate_schedule, open_last_collection_source

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DB = REPO_ROOT / "data" / "store" / "atlas.db"
REAL_STORE_ROOT = REPO_ROOT / "data" / "store"

#: 首次实测的注册表分布（2026-09-26）。变了就**响亮失败**，逼操作者重新测量并
#: 更新本节数字，而不是让陈旧结论悄悄留在测试里（照 `test_search_realdata.py` 的先例）。
MEASURED_CHANNELS = 13
MEASURED_INTERVALS = {1800: 3, 3600: 6, 7200: 4}
MEASURED_NEVER_COLLECTED = ("openai-blog", "venturebeat-ai")

#: 全部 11 个有记录的渠道都落在这一刻（窗口起点）；`now` 落在它之后一个整点。
MEASURED_LAST_COLLECTED = datetime(2026, 9, 26, 1, 0, 0, tzinfo=UTC)
MEASURED_DUE_AT_NEXT_HOUR = 9

NEXT_HOUR = datetime(2026, 9, 26, 2, 0, 0, tzinfo=UTC)

requires_real_store = pytest.mark.skipif(
    not REAL_DB.is_file(),
    reason=(
        "本地真实存储缺失（data/ 不进 git，见 SPEC §8.1）："
        "需要 data/store/atlas.db（T-101 注册表 + T-103 raw_records）"
    ),
)


def _db_digest() -> str:
    return hashlib.sha256(REAL_DB.read_bytes()).hexdigest()


def _sql_max_fetched_at() -> Dict[str, str]:
    """**独立**用 SQL 读一遍 `MAX(fetched_at)`（口径与实现不同源，便于交叉核对）。"""
    connection = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT channel_id, MAX(fetched_at) AS last_at FROM raw_records "
            "GROUP BY channel_id ORDER BY channel_id"
        ).fetchall()
    finally:
        connection.close()
    return {row["channel_id"]: row["last_at"] for row in rows}


@pytest.fixture(scope="module")
def real_registry():
    """真实注册表（只读打开、用完关闭）；库缺失时整个模块跳过。"""
    if not REAL_DB.is_file():
        pytest.skip("data/store/atlas.db 缺失")
    store = open_registry(REAL_DB, author="t207-realdata")
    try:
        yield RegistryService(store)
    finally:
        store.close()


@requires_real_store
def test_real_registry_intervals_are_the_ones_the_gap_was_about(real_registry) -> None:
    """真实注册表就是 SPEC §6.7 描述的那份：13 个渠道 / 1800 / 3600 / 7200。"""
    channels = real_registry.fetchable_channels()
    from collections import Counter

    distribution = Counter(channel.interval_seconds for channel in channels)
    print(
        f"\n[T-207 真实] 可采集渠道 {len(channels)} 个；interval 分布 "
        f"{dict(sorted(distribution.items()))}；config_version={real_registry.config_version}"
    )
    assert len(channels) == MEASURED_CHANNELS, (
        f"真实可采集渠道数变了：{len(channels)} != {MEASURED_CHANNELS}。"
        "请重新测量并更新本模块 docstring 与 MEASURED_* 常量。"
    )
    assert dict(sorted(distribution.items())) == MEASURED_INTERVALS, (
        f"真实 interval 分布变了：{dict(sorted(distribution.items()))} != {MEASURED_INTERVALS}。"
        "请重新测量并更新本模块里的数字。"
    )
    assert min(channel.interval_seconds for channel in channels) >= 60


@requires_real_store
def test_real_due_verdict_comes_from_the_real_raw_records_table(real_registry) -> None:
    """真实判定 == 从真实 SQL 独立算出来的判定（两层交叉核对 + 核心行为证据）。"""
    before = _db_digest()
    channels = real_registry.fetchable_channels()
    source = open_last_collection_source(REAL_STORE_ROOT)
    collected = source.last_collected_at()
    decision = evaluate_schedule(channels, collected, NEXT_HOUR)

    # ---- 交叉核对一：状态源 == 直接 SQL 读出的 MAX(fetched_at) ---------------
    sql_state = _sql_max_fetched_at()
    assert {key: value.isoformat() for key, value in collected.items()} == sql_state, (
        "调度状态源与直接 SQL 的结果不一致（读的不是同一张表 / 同一个口径）"
    )
    print(
        f"[T-207 真实] raw_records 覆盖 {len(sql_state)} 个渠道"
        f"（共 {sum(1 for _ in channels)} 个可采集渠道）"
    )

    # ---- 交叉核对二：判定 == 用独立 timedelta 重算的判定 --------------------
    interval_of = {channel.id: channel.interval_seconds for channel in channels}
    expectations = {}
    for channel_id in interval_of:
        if channel_id not in sql_state:
            expectations[channel_id] = True  # 从未采集 ⇒ 到期
            continue
        last = datetime.fromisoformat(sql_state[channel_id])
        expectations[channel_id] = (NEXT_HOUR - last) >= timedelta(
            seconds=interval_of[channel_id]
        )
    assert {item.channel_id: item.due for item in decision.schedules} == expectations
    assert set(decision.due_ids) == {
        channel_id for channel_id, due in expectations.items() if due
    }

    # ---- 首次实测的数字被钉住 ---------------------------------------------
    assert decision.enabled_channels == MEASURED_CHANNELS
    assert len(collected) == 11, f"有采集记录的渠道数变了：{len(collected)}"
    assert decision.not_due_ids and len(decision.due_ids) == MEASURED_DUE_AT_NEXT_HOUR, (
        f"now={NEXT_HOUR.isoformat()} 时到期渠道数变了：{len(decision.due_ids)} "
        f"!= {MEASURED_DUE_AT_NEXT_HOUR}；到期={list(decision.due_ids)}"
    )
    never = tuple(
        sorted(item.channel_id for item in decision.schedules if item.last_collected_at is None)
    )
    assert never == MEASURED_NEVER_COLLECTED, f"从未采集的渠道变了：{never}"
    # 11 个有记录的渠道里，10 个落在窗口起点；`ai-techpark` 是旧系统留下的真实历史
    # （2025-12-21），因此它的 last 不同 —— 如实承认，不强行统一。
    at_window_start = sorted(
        item.channel_id
        for item in decision.schedules
        if item.last_collected_at == MEASURED_LAST_COLLECTED
    )
    assert len(at_window_start) == 10, f"落在窗口起点的渠道数变了：{at_window_start}"
    older = {
        item.channel_id: item.last_collected_at.isoformat()
        for item in decision.schedules
        if item.last_collected_at is not None
        and item.last_collected_at != MEASURED_LAST_COLLECTED
    }
    assert older == {"ai-techpark": "2025-12-21T12:11:10.781894+00:00"}, older
    print(f"[T-207 真实] last 落在窗口起点的渠道 {len(at_window_start)} 个；历史渠道 {older}")

    print(
        f"[T-207 真实] now={NEXT_HOUR.isoformat()}：到期 {len(decision.due_ids)}"
        f"/{decision.enabled_channels} → {list(decision.due_ids)}"
    )
    print(f"[T-207 真实] 未到期 {len(decision.not_due_ids)} → {list(decision.not_due_ids)}")
    print(f"[T-207 真实] 从未采集（必然到期）→ {list(never)}")

    # ---- **核心证据**：同 last、不同 interval ⇒ 不同结论 -------------------
    schedules_3600 = [
        item
        for item in decision.schedules
        if item.interval_seconds == 3600 and item.last_collected_at == MEASURED_LAST_COLLECTED
    ]
    schedules_7200 = [
        item
        for item in decision.schedules
        if item.interval_seconds == 7200 and item.last_collected_at == MEASURED_LAST_COLLECTED
    ]
    assert schedules_3600, "没有 interval=3600 且 last 相同的渠道，核心证据不成立"
    assert schedules_7200, "没有 interval=7200 且 last 相同的渠道，核心证据不成立"
    assert all(item.due for item in schedules_3600), "3600s 的渠道距上次 1 小时 ⇒ 必须到期"
    assert not any(item.due for item in schedules_7200), "7200s 的渠道距上次 1 小时 ⇒ 必须未到期"
    print(
        f"[T-207 真实] 核心证据：last 同为 {MEASURED_LAST_COLLECTED.isoformat()} 时，"
        f"{len(schedules_3600)} 个 3600s 渠道"
        f"（{sorted(item.channel_id for item in schedules_3600)}）全部到期、"
        f"{len(schedules_7200)} 个 7200s 渠道"
        f"（{sorted(item.channel_id for item in schedules_7200)}）全部未到期"
        f" ⇒ interval_seconds 真的改变行为"
    )

    # 只读：库文件字节未变
    assert _db_digest() == before, "真实库被改动了（调度器必须只读）"


@requires_real_store
def test_real_clock_run_reports_unreached_intervals_honestly(real_registry) -> None:
    """用**真实系统时钟**跑一次：判定的形状与口径仍然成立（如实报数）。

    真实库里的 `last_collected_at` 落在未来，因此真实时钟下多半是"未到期 + 两个
    从未采集的到期"。这不是缺陷：`now < last` 是 `due_channels()` 明确支持的正常情形，
    这里把它**当成数据点的形状**来断言，而不是期待某个具体条数。
    """
    channels = real_registry.fetchable_channels()
    collected = open_last_collection_source(REAL_STORE_ROOT).last_collected_at()
    now = datetime.now(UTC)
    decision = evaluate_schedule(channels, collected, now)

    assert decision.enabled_channels == MEASURED_CHANNELS
    assert set(decision.due_ids) == {
        item.channel_id for item in decision.schedules if item.due
    }
    assert set(decision.due_ids) | set(decision.not_due_ids) == {
        item.channel_id for item in decision.schedules
    }
    # 从未采集的渠道在**任何**时刻都必须到期（真实时钟下也一样）
    assert set(MEASURED_NEVER_COLLECTED) <= set(decision.due_ids)
    print(
        f"\n[T-207 真实] 真实时钟 now={now.isoformat()}：到期 {len(decision.due_ids)}"
        f"/{decision.enabled_channels} → {list(decision.due_ids)}"
    )
    for item in decision.schedules:
        last = item.last_collected_at.isoformat() if item.last_collected_at else "（从未采集）"
        print(
            f"[T-207 真实]   {'到期' if item.due else '未到期'} {item.channel_id:<26}"
            f" interval={item.interval_seconds:>5}s last={last}"
        )
