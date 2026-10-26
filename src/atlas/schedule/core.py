"""T-207 最小调度器：**按 `interval_seconds` 只采到期渠道**（SPEC §6.7 缺口的闭合）。

缺口是什么（SPEC §6.7，已被证明，此处不重新论证）
------------------------------------------------

1. `channel.interval_seconds` 原先全仓只被读一处（`atlas.collect.throttle`），
   而那里的表达式在 `interval_seconds >= 60`（§2.12 的契约下限）时代数上恒等于
   全局下限 ⇒ **配 1800 还是 7200 对抓取行为毫无差别**。
2. `src/atlas/` 下没有任何周期性执行采集的东西：采集只在有人手工敲
   `python -m atlas.compose run` 时发生。

本包补上第 2 条里的"**什么时候该再采一次**"，并因此让第 1 条真正生效：
`interval_seconds` 从"已配置但无效"变成"决定这一轮采哪些渠道"。

两层职责必须分开（**这是设计，不是缺陷**）
------------------------------------------

| 层 | 决定什么 | 依据 |
|---|---|---|
| **本包（调度器）** | **试哪些渠道** | `channel.interval_seconds` 是否已到期 |
| `atlas.runner` 的幂等 | **这一轮是否真的发请求** | 同渠道载荷 + 同窗口 id ⇒ 跳过 |

于是有一个**有意保留**的交互：默认窗口是 UTC 整点小时桶
（`atlas.compose.tasks.utc_hour_window`），窗口内重跑会因幂等被跳过。
一个 `interval_seconds=1800` 的渠道在**同一个整点小时内**被调度器判为到期
（距上次采集 ≥ 30 分钟），但 `collect` 节点仍会因"同窗口同载荷"幂等跳过 ——
**这不是 bug**：调度器只保证"该试的会去试"，不保证"每次都真的打到网络"。
要让 1800 秒的周期真的落到网络请求上，就需要把窗口缩短到 ≤1800 秒
（例如 `--window` 传当前半小时桶），或改用比窗口更细的调度粒度。
**本包刻意不改运行器的窗口 / 幂等语义**（那会让"同输入同配置 → 同输出"失效）。

纯函数 + 注入时钟
-----------------

`due_channels()` 是**纯函数**：不读时钟、不做 I/O，`now` 必须由调用方注入；
输出顺序按 `channel.id` 显式排序（不依赖注册表顺序）且可复现。
唯一的 I/O 在 `read_last_collected()`：它以**只读**方式打开既有的
`atlas.db`，从**既有的 `raw_records` 表**（T-103 的事实表）取
`MAX(fetched_at) GROUP BY channel_id`。**不新增表、不新增状态文件**
（SPEC §2.10 的表归属登记因此不需要改动）。

到期判定（写死的约定）
----------------------

::

    due  ⟺  now - last_collected_at ≥ interval_seconds

即**边界取等号算到期**（`now == last + interval` ⇒ 到期）。
`last_collected_at` 缺失（该渠道从未采集过任何一条原文）⇒ **到期**：
"从未采集"必须是"到期"，绝不能是"永远不采"。

时间解析：`fetched_at` 是带 UTC 偏移的 ISO-8601 字符串
（`2026-09-26T01:00:00+00:00`，也可是 `2025-12-21T10:12:10.003183+00:00`
这种微秒形式，或 `Z` 结尾）。**naive 时间一律按 UTC 解释**（与
`atlas.feed` / `atlas.compose.tasks.parse_window` 同一规则，不做猜测）；
解析不了的一律抛 `MalformedTimestampError` —— 绝不静默当成"到期"或"未到期"
（CLAUDE.md 硬规则 2）。

"没有到期的渠道"是**正常状态**，不是错误
----------------------------------------

5 分钟一次的系统 cron 意味着"大多数轮次无事可做"。把这种轮次当成失败
（退出码非零）会让 cron 每小时报 11 次假故障。本包用 `DueDecision` 把三种状态
**显式区分**开：

| 状态 | `enabled_channels` | `due` | 含义 / 调用方应有的动作 |
|---|---|---|---|
| 一个可采集渠道都没有 | `0` | `()` | **配置问题** ⇒ `NoSchedulableChannelError`（响亮失败） |
| 有渠道、都还没到期 | `>0` | `()` | **正常** ⇒ 什么都不做，退出码 0（`DueDecision.is_idle`） |
| 有渠道到期 | `>0` | 非空 | 注册这些渠道并跑流水线 |

任何情况下都**不会**用"报告成功"来假装发生了采集。

系统 cron 示例（**文档，不安装**）
---------------------------------

本模块不安装、不修改任何 crontab，也不往仓库里放 shell 脚本；下面只是给操作者
抄进 `crontab -e` 的一行（每 5 分钟跑一次 due-only 采集）::

    # 每 5 分钟：只采到期的渠道；没有到期的渠道时退出码 0（不是故障）
    */5 * * * * cd /home/me/Atlas && ATLAS_LIVE=1 ATLAS_STORE_ROOT=data/store \\
        .venv-new/bin/python -m atlas.compose run --due-only \\
        >> logs/compose-run.log 2>&1

需要的环境：

- `ATLAS_LIVE=1` —— **必需**。没有它，`run` 以退出码 2 拒绝执行
  （SPEC §2.12 的合规默认值是"不主动抓"，不是"默默抓了"）。
- `ATLAS_STORE_ROOT`（或 `--store-root`）—— 存储根，默认 `data/store`；
  `atlas.db` 与 `normalized/` 都在它下面。cron 的 `PATH` 很干净，
  因此 `cd` 到仓库根再用绝对/相对路径调用解释器，不要指望 cron 有你的 shell 环境。
- 相对路径对 cron 不可靠：`ATLAS_STORE_ROOT` 建议写绝对路径。

**不要**把 `ATLAS_LIVE=1` 写进仓库里的文件（`.env` 亦然）—— 合规默认值必须保持
"默认关闭"，由操作者的 crontab 显式打开。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Mapping, Optional, Protocol, Tuple

from atlas.contracts import ContractError

if TYPE_CHECKING:  # 只依赖 atlas.registry.schema 的**类型**（与 collect.throttle 同一先例）
    from atlas.registry.schema import Channel

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

#: `atlas.db` 相对存储根的文件名（SPEC §2.10 的目录布局）。
DEFAULT_DB_FILENAME = "atlas.db"

#: 只读的事实表：原文元数据归 T-103（SPEC §2.10 表归属登记）。调度器**不新建表**。
RAW_RECORDS_TABLE = "raw_records"


# --------------------------------------------------------------------------- #
# 失败必须响亮可见
# --------------------------------------------------------------------------- #


class ScheduleError(ContractError):
    """调度器层面的输入 / 状态错误。向上传播，不吞、不降级。"""


class MalformedTimestampError(ScheduleError):
    """`fetched_at` 不是可解析的 ISO-8601 时间。

    绝不把它当成"到期"或"未到期"：两种猜测都会让调度静默走向错误的方向
    （前者每次轮询都重抓，后者永远不抓）。
    """

    def __init__(self, value: object, *, channel_id: Optional[str] = None, reason: str = ""):
        where = f"渠道 {channel_id!r} 的 " if channel_id is not None else ""
        detail = f"（{reason}）" if reason else ""
        super().__init__(
            f"{where}raw_records.fetched_at 不是可解析的 ISO-8601 时间：{value!r}{detail}；"
            "拒绝把它猜成'到期'或'未到期'（那会让调度静默走向错误的方向）"
        )


class NoSchedulableChannelError(ScheduleError):
    """一个可采集渠道都没有（启用渠道 × 启用行业为空）——配置问题，不是"空闲"。"""


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #


def parse_timestamp(value: str, *, channel_id: Optional[str] = None) -> datetime:
    """把 `raw_records.fetched_at` 解析成 **aware UTC** `datetime`。

    - 带偏移（`+00:00` / `Z`）→ 换算到 UTC；
    - **naive → 按 UTC 解释**（`atlas.feed` / `compose.tasks.parse_window` 的同一条规则）；
    - 解析失败 / 不是字符串 / 空串 → `MalformedTimestampError`（**响亮失败**）。
    """
    if not isinstance(value, str) or not value.strip():
        raise MalformedTimestampError(value, channel_id=channel_id, reason="空值或非字符串")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MalformedTimestampError(
            value, channel_id=channel_id, reason=str(exc)
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc(moment: datetime, *, what: str) -> datetime:
    """`now` / `last` 一律归一成 aware UTC；naive 按 UTC 解释（同上规则）。"""
    if not isinstance(moment, datetime):
        raise ScheduleError(f"{what} 必须是 datetime，收到 {type(moment).__name__}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 只读状态源：既有 raw_records 表的 MAX(fetched_at)
# --------------------------------------------------------------------------- #


class LastCollectionSource(Protocol):
    """只读状态源：给出 `channel_id → 最近一次采集时刻`（从未采集的渠道不出现）。

    做成 Protocol 而不是写死具体类，是为了让 `due_channels()` 的测试可以用
    内存实现注入状态，**零数据库**；生产实现是下面的 `SqliteLastCollectionSource`。
    """

    def last_collected_at(self) -> Mapping[str, datetime]:
        ...  # pragma: no cover - 协议声明


class SqliteLastCollectionSource:
    """**只读**打开既有的 `atlas.db`，取 `MAX(fetched_at) GROUP BY channel_id`。

    三条纪律：

    1. **只读**：`file:...?mode=ro` + `uri=True`。调度器永远不写这个库，
       也因此不会与采集 / 归档 / 注册表的写入争用。
    2. **不新建表、不新建状态文件**：状态就是 T-103 的 `raw_records`，
       "上次什么时候采的"本来就在里面（SPEC §2.10 表归属登记因此无需改动）。
    3. **缺库 / 缺表响亮失败**：绝不把"库不存在"当成"什么都没采过"——
       那会让每个渠道都判成到期，看起来像"调度器在工作"，实际是状态读不到。
    """

    def __init__(
        self, db_path: str | Path, *, table: str = RAW_RECORDS_TABLE
    ) -> None:
        self._path = Path(db_path)
        self._table = table
        self._cache: Optional[Dict[str, datetime]] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def table(self) -> str:
        return self._table

    def last_collected_at(self) -> Mapping[str, datetime]:
        """`{channel_id: 最近采集时刻(UTC)}`；从未采集的渠道**不出现在映射里**。"""
        if self._cache is None:
            self._cache = self._read()
        return dict(self._cache)

    # ------------------------------------------------------------------
    def _read(self) -> Dict[str, datetime]:
        if not self._path.is_file():
            raise ScheduleError(
                f"存储库不存在：{self._path}；调度状态就在 {self._table} 表里，"
                "读不到它不是'什么都没采过'（那会把全部渠道判成到期）"
            )
        uri = f"file:{self._path.as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:  # pragma: no cover - 取决于环境
            raise ScheduleError(f"无法以只读方式打开 {self._path}：{exc}") from exc
        connection.row_factory = sqlite3.Row
        try:
            try:
                rows = connection.execute(
                    f"SELECT channel_id AS channel_id, MAX(fetched_at) AS last_at "
                    f"FROM {self._table} GROUP BY channel_id ORDER BY channel_id"
                ).fetchall()
            except sqlite3.Error as exc:
                raise ScheduleError(
                    f"读取 {self._path} 的 {self._table} 表失败：{exc}；"
                    "调度状态取自既有的事实表，读不到即响亮失败（不做'全都没采过'的猜测）"
                ) from exc
        finally:
            connection.close()

        collected: Dict[str, datetime] = {}
        for row in rows:
            channel_id = row["channel_id"]
            last_at = row["last_at"]
            if channel_id is None:
                raise ScheduleError(
                    f"{self._table} 里有 channel_id 为 NULL 的行；"
                    "调度无法把它归属到任何渠道（数据损坏，拒绝静默跳过）"
                )
            collected[str(channel_id)] = parse_timestamp(
                last_at, channel_id=str(channel_id)
            )
        return collected


def open_last_collection_source(store_root: str | Path) -> SqliteLastCollectionSource:
    """按 SPEC §2.10 的目录布局，从存储根定位调度状态（`<store_root>/atlas.db`）。"""
    root = Path(store_root)
    if not str(root).strip() or str(root) in (".", ""):
        raise ScheduleError(f"store_root 不得为空，收到 {store_root!r}")
    return SqliteLastCollectionSource(root / DEFAULT_DB_FILENAME)


def read_last_collected(db_path: str | Path) -> Dict[str, datetime]:
    """便捷函数：直接从库文件读出 `{channel_id: 最近采集时刻}`（只读，不缓存）。"""
    return dict(SqliteLastCollectionSource(db_path).last_collected_at())


# --------------------------------------------------------------------------- #
# 纯函数：谁到期了
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChannelSchedule:
    """一个渠道的调度判定。

    `last_collected_at is None` 表示**从未采集过**（`raw_records` 里没有该渠道的行），
    此时 `next_due_at` 语义上是"过去"（早就该采），实现上取 `now`：判定的依据是
    "没有记录"，不是一个编造出来的时间点。
    """

    channel_id: str
    interval_seconds: int
    last_collected_at: Optional[datetime]
    due: bool
    next_due_at: datetime

    def as_dict(self) -> Dict[str, object]:
        """JSON 安全的判定（CLI 的 `plan` 直接用它）。"""
        return {
            "id": self.channel_id,
            "interval_seconds": self.interval_seconds,
            "last_collected_at": (
                self.last_collected_at.isoformat() if self.last_collected_at else None
            ),
            "due": self.due,
            "next_due_at": self.next_due_at.isoformat(),
        }


def due_channels(
    channels: Iterable["Channel"],
    last_collected: Mapping[str, datetime],
    now: datetime,
) -> Tuple[ChannelSchedule, ...]:
    """纯函数：每个渠道是否到期。**不读时钟、不做 I/O**，`now` 由调用方注入。

    判定约定（边界取等号算到期）::

        due  ⟺  now - last_collected_at >= timedelta(seconds=interval_seconds)

    - **没有** last 记录的渠道 ⇒ **到期**（从未采集 ⇒ 该采，绝不是"永远不采"）。
    - `last` 比 `now` 还晚（时钟回拨 / 手工灌入未来时间）⇒ 未到期，
      并且**不是**错误：它是一个明确的"还没到时候"。
    - `last_collected` 里出现了不在 `channels` 里的渠道 ⇒ 忽略（归档里有历史、
      而配置里已删掉该渠道，是正常的历史遗留）。
    - `last_collected` 里某个渠道的时间无法解析 ⇒ `MalformedTimestampError`。这里
      兼容 `str` 与 `datetime`：`str` 走 `parse_timestamp()`（naive 按 UTC），
      `datetime` 走 `_as_utc()`（naive 按 UTC）。
    - 返回顺序**按 `channel.id` 排序**，与注册表 / 调用方给的顺序无关（可复现）。
    - 同一 `channel.id` 出现两次 ⇒ `ScheduleError`（重复 id 会让判定有歧义）。

    Args:
        channels: 候选渠道（生产上是 `RegistryService.fetchable_channels()`）。
        last_collected: `{channel_id: 最近采集时刻}`；缺失 = 从未采集。
        now: 当前时刻（注入；naive 按 UTC 解释）。

    Raises:
        ScheduleError: 渠道集合为空、id 重复、`now` 不是 datetime、缺 `interval_seconds`。
        MalformedTimestampError: `last_collected` 里有解析不了的时间。
    """
    moment = _as_utc(now, what="now")
    ordered = sorted(channels, key=lambda channel: channel.id)

    seen: set[str] = set()
    schedules: list[ChannelSchedule] = []
    for channel in ordered:
        channel_id = str(channel.id)
        if channel_id in seen:
            raise ScheduleError(
                f"渠道 id 重复：{channel_id!r}；重复 id 会让到期判定有歧义，"
                "拒绝在其中任选一个"
            )
        seen.add(channel_id)

        interval = getattr(channel, "interval_seconds", None)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 0:
            raise ScheduleError(
                f"渠道 {channel_id!r} 的 interval_seconds 必须是非负整数，收到 {interval!r}"
            )

        last: Optional[datetime] = None
        if channel_id in last_collected:
            raw_last = last_collected[channel_id]
            if isinstance(raw_last, datetime):
                last = _as_utc(raw_last, what=f"渠道 {channel_id!r} 的 last_collected_at")
            elif isinstance(raw_last, str):
                last = parse_timestamp(raw_last, channel_id=channel_id)
            else:
                raise MalformedTimestampError(
                    raw_last,
                    channel_id=channel_id,
                    reason=f"类型 {type(raw_last).__name__} 既不是 str 也不是 datetime",
                )

        window = timedelta(seconds=interval)
        if last is None:
            # 从未采集 ⇒ 到期；next_due_at 取 now（"早就该采"），不编造历史时间点。
            schedules.append(
                ChannelSchedule(
                    channel_id=channel_id,
                    interval_seconds=int(interval),
                    last_collected_at=None,
                    due=True,
                    next_due_at=moment,
                )
            )
            continue

        elapsed = moment - last
        schedules.append(
            ChannelSchedule(
                channel_id=channel_id,
                interval_seconds=int(interval),
                last_collected_at=last,
                due=elapsed >= window,
                next_due_at=last + window,
            )
        )
    return tuple(schedules)


@dataclass(frozen=True)
class DueDecision:
    """一轮调度的完整判定：把"没有渠道"与"没有到期渠道"**显式分开**。

    `enabled_channels == 0` 由 `evaluate_schedule()` 直接抛
    `NoSchedulableChannelError`，因此这里只承载"有渠道可谈"的情形。
    """

    now: datetime
    enabled_channels: int
    schedules: Tuple[ChannelSchedule, ...]
    due_ids: Tuple[str, ...]

    @property
    def due(self) -> Tuple[ChannelSchedule, ...]:
        """到期的渠道判定，按 channel id 排序。"""
        wanted = set(self.due_ids)
        return tuple(item for item in self.schedules if item.channel_id in wanted)

    @property
    def is_idle(self) -> bool:
        """有可采集渠道，但这一轮**一个都还没到期** —— 正常状态，不是错误。"""
        return not self.due_ids

    @property
    def not_due_ids(self) -> Tuple[str, ...]:
        return tuple(
            item.channel_id for item in self.schedules if item.channel_id not in set(self.due_ids)
        )

    def schedule_of(self, channel_id: str) -> ChannelSchedule:
        for item in self.schedules:
            if item.channel_id == channel_id:
                return item
        raise ScheduleError(f"判定里没有渠道 {channel_id!r}")

    def as_dict(self) -> Dict[str, object]:
        return {
            "now": self.now.isoformat(),
            "enabled_channels": self.enabled_channels,
            "due_channels": list(self.due_ids),
            "idle": self.is_idle,
            "per_channel": [item.as_dict() for item in self.schedules],
        }


def evaluate_schedule(
    channels: Iterable["Channel"],
    last_collected: Mapping[str, datetime],
    now: datetime,
) -> DueDecision:
    """`due_channels()` + 三态区分（见模块文档）。

    Raises:
        NoSchedulableChannelError: **一个可采集渠道都没有**（配置问题，必须响亮失败——
            与"这一轮到期的都采完了"是完全不同的两件事）。
        ScheduleError / MalformedTimestampError: 见 `due_channels()`。
    """
    candidates = tuple(channels)
    if not candidates:
        raise NoSchedulableChannelError(
            "注册表里没有可采集的渠道（启用渠道 × 启用行业 为空）："
            "这是**配置问题**，不是'这一轮没有到期的渠道'。"
            "先配置渠道（`python -m atlas.compose register ...`）再跑。"
        )
    moment = _as_utc(now, what="now")
    schedules = due_channels(candidates, last_collected, moment)
    return DueDecision(
        now=moment,
        enabled_channels=len(schedules),
        schedules=schedules,
        due_ids=tuple(item.channel_id for item in schedules if item.due),
    )
