"""T-207 最小调度器：到期判定、只读状态源、三态区分（**本模块即判据**）。

本任务要闭合的缺口（SPEC §6.7）
-------------------------------

`channel.interval_seconds` 原先全仓只被读一处，而那里的表达式在契约下限
（`interval_seconds >= 60`）时代数上恒等于全局下限 ⇒ 配 1800 还是 7200 行为相同，
且没有任何东西按它轮询。**本任务判据就是"这个参数现在真的改变行为"**，
因此下面每一条都指向一个可观察的行为差异，而不是"有个函数存在"。

判定（哪些测试能证明"真的生效"）
--------------------------------

| # | 判据 | 落在哪个用例 |
|---|---|---|
| A1 | **同一个 `now` 下，只差 `interval_seconds` 的两个渠道得到不同的到期结论** | `test_interval_seconds_actually_changes_the_verdict` |
| A2 | 边界写死且两侧都测：`now - last == interval` ⇒ **到期**；差 1 微秒 ⇒ 未到期 | `test_boundary_equality_is_due_and_one_microsecond_earlier_is_not` |
| A3 | **没有** `last_collected_at` 记录 ⇒ 到期（"从未采集"绝不能变成"永远不采"） | `test_channel_never_collected_is_due` |
| A4 | 纯函数、时间注入：同输入同输出；顺序按 `channel.id` 显式排序 | `test_pure_function_is_deterministic_and_ordered_by_channel_id` |
| A5 | 时间格式：带偏移 / 微秒 / `Z` 都解析；**naive 按 UTC** | `test_parses_offsets_microseconds_and_z_suffix`、`test_naive_timestamp_is_interpreted_as_utc` |
| A6 | 坏时间**响亮失败**，绝不静默当成"到期"或"未到期" | `test_malformed_timestamp_fails_loudly`、`test_malformed_timestamp_type_fails_loudly` |
| A7 | 状态源**只读**既有 `raw_records` 表，同一渠道多条取 `MAX(fetched_at)`；不新建表/文件 | `test_state_source_reads_max_fetched_at_per_channel_from_existing_table`、`test_state_source_never_writes_or_creates_anything` |
| A8 | 缺库 / 缺表 **响亮失败**（不是"全都没采过"） | `test_missing_database_fails_loudly`、`test_missing_table_fails_loudly` |
| A9 | 三态：没有可采集渠道 = **配置问题**（异常）；有渠道但都未到期 = **正常**（`is_idle`） | `test_no_enabled_channels_is_a_configuration_error`、`test_all_channels_not_due_is_idle_and_not_an_error` |

**否定性断言必须有活对照**（CLAUDE.md 硬规则 4）：本模块里所有 `pytest.raises`
都配了同一调用路径对合法输入成功的对照 —— 见 A6 / A8 / A9 各用例末段，
以及 `test_duplicate_channel_id_fails_loudly` 里"换成唯一 id 就成功"的那一步。
否则签名不匹配 / 异常类型不对会伪装成"拒绝成功"。

刻意**不**在这里验证的东西
--------------------------

- 调度与幂等窗口的交互（"到期了但同窗口被跳过"）：那属于组合根，见
  `tests/test_compose_pipeline.py` 的 due-only 用例。
- 真实注册表 / 真实库的数字：见 `tests/test_schedule_realdata.py`。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.registry.schema import Channel, FetchSpec, FetchType
from atlas.schedule import (
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

UTC = timezone.utc

#: 固定的"现在"。**注入**而不是读时钟：判定必须可复现（A4）。
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

ENDPOINT = "http://127.0.0.1:9/t207.txt"

USED_ENDPOINTS: List[str] = []


def _endpoint(tag: str) -> str:
    """给每个渠道一个**唯一**的（离线）endpoint，顺手记下来做健全性断言。"""
    url = f"http://127.0.0.1:9/t207-{tag}.txt"
    USED_ENDPOINTS.append(url)
    return url


def _channel(channel_id: str, interval: int, *, tag: Optional[str] = None) -> Channel:
    """一个**合法**渠道（走真实 schema 校验，`interval_seconds >= 60`）。"""
    return Channel(
        id=channel_id,
        industry_id="ai",
        type=FetchType.RSS,
        endpoint=_endpoint(tag or channel_id),
        fetch_spec=FetchSpec(type=FetchType.RSS),
        interval_seconds=interval,
        enabled=True,
    )


def _by_id(schedules) -> Dict[str, object]:
    return {item.channel_id: item for item in schedules}


def _seed_collected(root: Path, entries) -> None:
    """把 `(channel_id, fetched_at)` 写进**真实归档**（只增不改的事实表）。

    走 `ArchiveStore.put`（真实的 T-103 写路径）而不是手写 INSERT：这样测试读到的
    行与生产里 `collect → archive` 落下的行结构完全一致。
    """
    archive = open_archive(root)
    try:
        for index, (channel_id, fetched_at) in enumerate(entries):
            content = f"t207 {channel_id} #{index}".encode("utf-8")
            record = RawRecord.create(
                channel_id=channel_id,
                endpoint=f"http://127.0.0.1:9/seed-{channel_id}-{index}.txt",
                content=content,
                fetched_at=fetched_at,
                http_status=200,
            )
            archive.put(record, content)
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# A1：**最重要的那条** —— interval_seconds 真的改变行为
# --------------------------------------------------------------------------- #


def test_interval_seconds_actually_changes_the_verdict() -> None:
    """同一个 `now`、只差 `interval_seconds` 的两个渠道 ⇒ **不同**的到期结论。

    这正是 SPEC §6.7 说"配 1800 还是 7200 毫无差别"的反面：在本包之前两者都不会被
    任何东西读到；现在短周期到期、长周期未到期，差异**可观察**。
    """
    short = _channel("chan-short", 1800)  # 30 分钟
    long = _channel("chan-long", 7200)  # 2 小时
    last = NOW - timedelta(minutes=45)  # 距 now 45 分钟

    schedules = _by_id(due_channels([short, long], {"chan-short": last, "chan-long": last}, NOW))

    assert schedules["chan-short"].due is True, "距上次 45 分钟 > 30 分钟 ⇒ 必须到期"
    assert schedules["chan-long"].due is False, "距上次 45 分钟 < 2 小时 ⇒ 必须未到期"
    assert schedules["chan-short"].due != schedules["chan-long"].due, (
        "两个渠道只差 interval_seconds，到期结论必须不同（否则本任务什么都没修）"
    )

    # 再钉住"同一个渠道，只改 interval_seconds，结论就翻转" —— 差异来自参数本身
    flipped = _by_id(due_channels([_channel("chan-short", 7200)], {"chan-short": last}, NOW))
    assert flipped["chan-short"].due is False, "同渠道 interval=7200 时必须变成未到期"


def test_next_due_at_is_last_plus_interval() -> None:
    """`next_due_at = last_collected_at + interval_seconds`（可用来排下一轮 cron）。"""
    channel = _channel("chan-next", 3600)
    last = NOW - timedelta(minutes=10)
    item = due_channels([channel], {"chan-next": last}, NOW)[0]
    assert item.next_due_at == last + timedelta(seconds=3600)


# --------------------------------------------------------------------------- #
# A2：边界写死，两侧都测
# --------------------------------------------------------------------------- #


def test_boundary_equality_is_due_and_one_microsecond_earlier_is_not() -> None:
    """边界约定：`now - last >= interval` ⇒ 到期。**等号算到期**，差 1 微秒不算。"""
    channel = _channel("chan-boundary", 3600)

    exactly = NOW - timedelta(seconds=3600)
    just_short = exactly + timedelta(microseconds=1)  # 距 now 比 interval 少 1 微秒

    at_boundary = due_channels([channel], {"chan-boundary": exactly}, NOW)[0]
    before_boundary = due_channels([channel], {"chan-boundary": just_short}, NOW)[0]
    after_boundary = due_channels(
        [channel], {"chan-boundary": exactly - timedelta(microseconds=1)}, NOW
    )[0]

    assert at_boundary.due is True, "now - last == interval 必须算到期（边界取等号）"
    assert before_boundary.due is False, "差 1 微秒未到期"
    assert after_boundary.due is True, "超过 1 微秒到期"
    assert at_boundary.next_due_at == NOW


def test_future_last_collected_is_not_due_and_is_not_an_error() -> None:
    """`last` 晚于 `now`（时钟回拨 / 灌入未来时间）⇒ 未到期，且**不是**错误。"""
    channel = _channel("chan-future", 3600)
    item = due_channels([channel], {"chan-future": NOW + timedelta(hours=3)}, NOW)[0]
    assert item.due is False
    assert item.next_due_at == NOW + timedelta(hours=4)


# --------------------------------------------------------------------------- #
# A3：从未采集 ⇒ 到期
# --------------------------------------------------------------------------- #


def test_channel_never_collected_is_due() -> None:
    """没有 `last_collected_at` 记录 ⇒ **到期**（状态必须是 due，绝不是 never）。"""
    channel = _channel("chan-never", 86400)
    (item,) = due_channels([channel], {}, NOW)
    assert item.last_collected_at is None
    assert item.due is True, "从未采集的渠道必须被采（否则它永远不会被采）"
    assert item.next_due_at == NOW

    # 活对照：同一个调用，一旦给了记录就不再是"从未采集"，且可以判成未到期
    (withrecord,) = due_channels([channel], {"chan-never": NOW}, NOW)
    assert withrecord.last_collected_at == NOW
    assert withrecord.due is False


def test_archived_channel_missing_from_registry_is_ignored() -> None:
    """归档里有历史、但配置里已删掉的渠道 ⇒ 忽略（不是错误，也不出现在结果里）。"""
    channel = _channel("chan-keep", 3600)
    schedules = due_channels(
        [channel],
        {"chan-keep": NOW, "chan-deleted-long-ago": NOW - timedelta(days=400)},
        NOW,
    )
    assert [item.channel_id for item in schedules] == ["chan-keep"]
    assert schedules[0].due is False


# --------------------------------------------------------------------------- #
# A4：纯函数、确定顺序
# --------------------------------------------------------------------------- #


def test_pure_function_is_deterministic_and_ordered_by_channel_id() -> None:
    """输入顺序不影响输出：结果按 `channel.id` 排序，重复调用逐字段相同。"""
    channels = [
        _channel("chan-zulu", 1800),
        _channel("chan-alpha", 7200),
        _channel("chan-mike", 3600),
    ]
    last = {"chan-zulu": NOW - timedelta(hours=2), "chan-mike": NOW - timedelta(hours=1)}

    forward = due_channels(channels, last, NOW)
    backward = due_channels(list(reversed(channels)), dict(reversed(list(last.items()))), NOW)

    assert [item.channel_id for item in forward] == ["chan-alpha", "chan-mike", "chan-zulu"]
    assert forward == backward, "同一输入必须给出逐字段相同的判定（纯函数）"
    order = [item.channel_id for item in forward]
    assert order == sorted(order), "输出顺序必须是显式的 channel.id 排序"
    assert _by_id(forward)["chan-zulu"].due is True  # 距上次 2h > 30min
    assert _by_id(forward)["chan-mike"].due is True  # 距上次 1h >= 1h（边界取等号）
    assert _by_id(forward)["chan-alpha"].due is True  # 没有记录 ⇒ 从未采集 ⇒ 到期


def test_no_clock_reads_input_snapshot_is_not_mutated() -> None:
    """判定不读时钟、不改调用方给的状态映射（`now` 之外没有任何时间来源）。"""
    channel = _channel("chan-pure", 3600)
    last: Dict[str, datetime] = {"chan-pure": NOW}
    snapshot = dict(last)
    due_channels([channel], last, NOW)
    assert last == snapshot, "调度判定必须是只读的"

    # 只把**注入的 now** 往后拨满一个间隔，结论就翻转 ⇒ 时间只来自 `now`
    assert due_channels([channel], last, NOW)[0].due is False
    assert due_channels([channel], last, NOW + timedelta(seconds=3600))[0].due is True
    assert due_channels([channel], last, NOW + timedelta(seconds=3599))[0].due is False


def test_duplicate_channel_id_fails_loudly() -> None:
    """同一 id 出现两次 ⇒ 响亮失败（重复 id 会让判定有歧义）。"""
    first = _channel("chan-dup", 1800, tag="dup-a")
    second = _channel("chan-dup", 7200, tag="dup-b")
    with pytest.raises(ScheduleError) as caught:
        due_channels([first, second], {}, NOW)
    assert "chan-dup" in str(caught.value)

    # 活对照：同样的调用，id 改成唯一就成功
    ok = due_channels(
        [_channel("chan-dup-a", 1800, tag="dup-a2"), _channel("chan-dup-b", 7200, tag="dup-b2")],
        {},
        NOW,
    )
    assert [item.channel_id for item in ok] == ["chan-dup-a", "chan-dup-b"]


def test_interval_seconds_type_is_validated() -> None:
    """`interval_seconds` 不是非负整数 ⇒ 响亮失败（不静默当成 0）。"""

    class _Bad:
        id = "chan-bad-interval"
        interval_seconds = None

    with pytest.raises(ScheduleError) as caught:
        due_channels([_Bad()], {}, NOW)
    assert "interval_seconds" in str(caught.value)

    class _Good:
        id = "chan-good-interval"
        interval_seconds = 60

    (item,) = due_channels([_Good()], {}, NOW)  # 活对照：合法输入照常工作
    assert item.due is True and item.interval_seconds == 60


# --------------------------------------------------------------------------- #
# A5：时间解析
# --------------------------------------------------------------------------- #


def test_parses_offsets_microseconds_and_z_suffix() -> None:
    """`fetched_at` 是带 UTC 偏移的 ISO-8601：含微秒形式与 `Z` 后缀。"""
    assert parse_timestamp("2026-09-26T01:00:00+00:00") == datetime(
        2026, 9, 26, 1, 0, 0, tzinfo=UTC
    )
    micro = parse_timestamp("2025-12-21T10:12:10.003183+00:00")
    assert micro == datetime(2025, 12, 21, 10, 12, 10, 3183, tzinfo=UTC)
    assert micro.microsecond == 3183
    assert parse_timestamp("2026-09-26T01:00:00Z") == datetime(2026, 9, 26, 1, 0, 0, tzinfo=UTC)
    # 非 UTC 偏移换算到 UTC（同一时刻）
    assert parse_timestamp("2026-09-26T09:00:00+08:00") == datetime(
        2026, 9, 26, 1, 0, 0, tzinfo=UTC
    )
    # 微秒真的参与判定：差 1 微秒就越过边界（interval=60s，now=12:00:00）
    channel = _channel("chan-micro", 60)
    exactly = due_channels([channel], {"chan-micro": "2026-09-26T11:59:00.000000+00:00"}, NOW)[0]
    one_micro_short = due_channels(
        [channel], {"chan-micro": "2026-09-26T11:59:00.000001+00:00"}, NOW
    )[0]
    assert exactly.due is True, "整整 60.000000 秒 ⇒ 到期（边界取等号）"
    assert one_micro_short.due is False, "59.999999 秒 ⇒ 未到期（微秒没有被截断）"


def test_naive_timestamp_is_interpreted_as_utc() -> None:
    """naive 时间**按 UTC 解释**（与 `atlas.feed` / `compose.tasks.parse_window` 同规则）。"""
    naive = parse_timestamp("2026-09-26T01:00:00")
    assert naive == datetime(2026, 9, 26, 1, 0, 0, tzinfo=UTC)
    assert naive.tzinfo is UTC

    # 判定层面同样成立：naive 的 last 与 aware 的 last 给出同一个结论
    channel = _channel("chan-naive", 3600)
    naive_items = due_channels([channel], {"chan-naive": "2026-09-26T11:00:00"}, NOW)
    aware_items = due_channels(
        [channel], {"chan-naive": "2026-09-26T11:00:00+00:00"}, NOW
    )
    assert naive_items == aware_items
    assert naive_items[0].last_collected_at.tzinfo is UTC


# --------------------------------------------------------------------------- #
# A6：坏时间响亮失败（含活对照）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-timestamp",
        "2026-13-45T99:99:99+00:00",
        "",
        "   ",
    ],
)
def test_malformed_timestamp_fails_loudly(bad: str) -> None:
    """坏字符串 ⇒ `MalformedTimestampError`，**绝不**静默当成到期 / 未到期。"""
    channel = _channel("chan-bad", 3600)
    with pytest.raises(MalformedTimestampError) as caught:
        due_channels([channel], {"chan-bad": bad}, NOW)
    message = str(caught.value)
    assert "chan-bad" in message
    assert repr(bad) in message or bad.strip() in message, message

    # 活对照：同一个调用路径，换成合法时间就成功（否则"被拒绝"可能是签名不匹配）
    (item,) = due_channels([channel], {"chan-bad": "2026-09-26T11:00:00+00:00"}, NOW)
    assert item.due is True


def test_malformed_timestamp_type_fails_loudly() -> None:
    """类型不是 str / datetime（例如 int）⇒ 同样响亮失败。"""
    channel = _channel("chan-bad-type", 3600)
    with pytest.raises(MalformedTimestampError) as caught:
        due_channels([channel], {"chan-bad-type": 12345}, NOW)
    assert "int" in str(caught.value)

    # 活对照：datetime 对象是被接受的输入形态
    (item,) = due_channels([channel], {"chan-bad-type": NOW - timedelta(hours=2)}, NOW)
    assert item.due is True


def test_datetime_input_naive_is_utc_and_aware_is_converted() -> None:
    """`last_collected` 允许直接给 `datetime`：naive 按 UTC，aware 换算到 UTC。"""
    channel = _channel("chan-dt", 3600)
    naive = datetime(2026, 9, 26, 11, 0, 0)
    aware = datetime(2026, 9, 26, 19, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert due_channels([channel], {"chan-dt": naive}, NOW)[0].last_collected_at == aware
    assert due_channels([channel], {"chan-dt": aware}, NOW) == due_channels(
        [channel], {"chan-dt": naive}, NOW
    )


# --------------------------------------------------------------------------- #
# A7：只读状态源 = 既有 raw_records 表
# --------------------------------------------------------------------------- #


def test_state_source_reads_max_fetched_at_per_channel_from_existing_table(
    tmp_path: Path,
) -> None:
    """同一渠道多条记录 ⇒ 取 `MAX(fetched_at)`；从未采集的渠道不出现。"""
    root = tmp_path / "store"
    older = datetime(2026, 9, 20, 1, 0, 0, tzinfo=UTC)
    newest = datetime(2026, 9, 25, 23, 30, 0, 3183, tzinfo=UTC)
    _seed_collected(
        root,
        [
            ("chan-many", older),
            ("chan-many", newest),
            ("chan-many", older + timedelta(hours=3)),
            ("chan-single", newest - timedelta(days=1)),
        ],
    )

    source = open_last_collection_source(root)
    collected = source.last_collected_at()

    assert collected == {
        "chan-many": newest,
        "chan-single": newest - timedelta(days=1),
    }
    assert collected["chan-many"].microsecond == 3183, "微秒不能被截断"

    # 与便捷函数一致，且它是"读"不是"算"
    assert read_last_collected(root / "atlas.db") == collected

    # 判定层：chan-many 刚采过（未到期），chan-never 从未采集（到期）
    decision = evaluate_schedule(
        [_channel("chan-many", 3600), _channel("chan-never", 3600)],
        collected,
        newest + timedelta(minutes=5),
    )
    assert decision.due_ids == ("chan-never",)
    assert decision.enabled_channels == 2
    assert decision.not_due_ids == ("chan-many",)


def test_state_source_never_writes_or_creates_anything(tmp_path: Path) -> None:
    """状态源**只读**：读多次不改变库文件字节，也不新建状态文件（SPEC §2.10）。"""
    root = tmp_path / "store"
    _seed_collected(root, [("chan-ro", NOW - timedelta(hours=1))])
    db_path = root / "atlas.db"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    files_before = sorted(p.name for p in root.iterdir())

    source = SqliteLastCollectionSource(db_path)
    first = source.last_collected_at()
    second = SqliteLastCollectionSource(db_path).last_collected_at()

    assert first == second
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in root.iterdir()) == files_before, (
        "调度器不得新建表 / 状态文件；状态就是既有的 raw_records"
    )

    # 只读连接真的拒绝写入（不是靠"我们没写"的自觉）
    with pytest.raises(sqlite3.OperationalError):
        source_connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            source_connection.execute("CREATE TABLE t207_should_not_exist (x INTEGER)")
        finally:
            source_connection.close()


# --------------------------------------------------------------------------- #
# A8：缺库 / 缺表响亮失败（含活对照）
# --------------------------------------------------------------------------- #


def test_missing_database_fails_loudly(tmp_path: Path) -> None:
    """库文件不存在 ⇒ 响亮失败。**不**当成"全都没采过"（那会让全部渠道判成到期）。"""
    missing = tmp_path / "nope" / "atlas.db"
    with pytest.raises(ScheduleError) as caught:
        SqliteLastCollectionSource(missing).last_collected_at()
    assert "不存在" in str(caught.value)

    # 活对照：同一个类，库文件存在就成功
    root = tmp_path / "store"
    _seed_collected(root, [("chan-ok", NOW)])
    assert SqliteLastCollectionSource(root / "atlas.db").last_collected_at() == {"chan-ok": NOW}


def test_missing_table_fails_loudly(tmp_path: Path) -> None:
    """库在但 `raw_records` 表不在（空库 / 别的库）⇒ 响亮失败，不返回空映射。"""
    db_path = tmp_path / "empty.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE unrelated (x INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(ScheduleError) as caught:
        SqliteLastCollectionSource(db_path).last_collected_at()
    assert "raw_records" in str(caught.value)

    # 活对照：同一路径建出真实的 raw_records（走归档写路径）就能读到
    root = tmp_path / "store"
    _seed_collected(root, [("chan-ok", NOW)])
    assert read_last_collected(root / "atlas.db") == {"chan-ok": NOW}


def test_open_last_collection_source_uses_store_root_layout(tmp_path: Path) -> None:
    """状态位置就是 SPEC §2.10 的 `<store_root>/atlas.db`（不新增文件）。"""
    source = open_last_collection_source(tmp_path / "store")
    assert source.path == tmp_path / "store" / "atlas.db"
    assert source.table == "raw_records"


# --------------------------------------------------------------------------- #
# A9：三态区分
# --------------------------------------------------------------------------- #


def test_no_enabled_channels_is_a_configuration_error() -> None:
    """**一个可采集渠道都没有** ⇒ 配置问题，响亮失败（不是"空闲"）。"""
    with pytest.raises(NoSchedulableChannelError) as caught:
        evaluate_schedule([], {}, NOW)
    assert "配置" in str(caught.value)

    # 活对照：同一个调用，给一个渠道就成功（哪怕它未到期）
    decision = evaluate_schedule([_channel("chan-one", 3600)], {"chan-one": NOW}, NOW)
    assert decision.enabled_channels == 1 and decision.is_idle


def test_all_channels_not_due_is_idle_and_not_an_error() -> None:
    """有渠道但都未到期 ⇒ **正常状态**（5 分钟一次的 cron 大多数轮次如此）。"""
    channels = [_channel("chan-a", 3600), _channel("chan-b", 7200)]
    fresh = {"chan-a": NOW - timedelta(minutes=1), "chan-b": NOW - timedelta(minutes=1)}

    idle = evaluate_schedule(channels, fresh, NOW)
    assert idle.is_idle is True
    assert idle.due_ids == ()
    assert idle.enabled_channels == 2
    assert idle.not_due_ids == ("chan-a", "chan-b")
    assert idle.as_dict()["idle"] is True

    # 到期的混合情况：只有该到期的那个被选中
    mixed = evaluate_schedule(
        channels, {"chan-a": NOW - timedelta(hours=2), "chan-b": NOW - timedelta(minutes=1)}, NOW
    )
    assert mixed.is_idle is False
    assert mixed.due_ids == ("chan-a",)
    assert [item.channel_id for item in mixed.due] == ["chan-a"]
    assert mixed.schedule_of("chan-a").last_collected_at == NOW - timedelta(hours=2)
    with pytest.raises(ScheduleError):
        mixed.schedule_of("chan-not-in-decision")


def test_decision_as_dict_is_json_safe() -> None:
    """`as_dict()` 是 CLI `plan` 的输出形状：只含 JSON 原生类型。"""
    import json

    decision = evaluate_schedule(
        [_channel("chan-json", 1800)], {"chan-json": NOW - timedelta(minutes=45)}, NOW
    )
    payload = decision.as_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert payload["due_channels"] == ["chan-json"]
    assert payload["per_channel"][0]["due"] is True
    assert payload["per_channel"][0]["interval_seconds"] == 1800
    assert payload["per_channel"][0]["last_collected_at"] == (
        NOW - timedelta(minutes=45)
    ).isoformat()
    assert payload["per_channel"][0]["next_due_at"] == (
        NOW - timedelta(minutes=45) + timedelta(minutes=30)
    ).isoformat()
    assert payload["now"] == NOW.isoformat()
