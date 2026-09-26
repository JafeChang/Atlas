"""T-106 Feed 查询模型：筛选 / 排序 / 分页（**纯逻辑，零 I/O，不碰 HTTP**）。

本模块是 feed 的**只读投影**语义所在：

- 只做 `筛选 + 稳定排序 + 分页`，**不产生事实**：不写 raw、不写标签、不写 Proposed。
  整个 `atlas.feed` 包的写路径为零（见 `tests/test_feed_http.py::test_feed_package_has_no_write_path`）。
- 数据来源是**注入**的 `atlas.feed.repository.FeedSource`（Protocol），本模块只依赖
  `atlas.contracts.RawRecord` 这一个共享类型，**不 import** `atlas.labels` / `atlas.archive` /
  `atlas.registry`（SPEC §4.0；T-108 正在并行写 `atlas.labels`，此处刻意解耦）。

两种粒度（`FeedQuery.granularity`）—— T-109 补完时新增
======================================================

SPEC §6.3 裁决 B 的落地：`raw_id` 既可以标识**一份 feed**（容器），也可以标识
**一篇文章**（条目）。浏览界面要看的是**条目**，而旧的 `/feed` 契约看的是**文档**。
因此新增粒度开关，**默认值不变**（`"document"`）：

| `granularity` | 一条 `FeedItem` 是什么 | 谁在用 |
|---|---|---|
| `"document"`（**默认**） | 一条 `raw_records` 记录（原契约，逐字节未改） | 机器接口 / 既有调用方 |
| `"entry"` | 一个**可浏览单元**：容器的派生条目，或本身即条目的整篇文档 | T-109 前端（SPEC §6.5） |

条目模式的**容器口径**（SPEC §6.3，`_entry_units` 实现）：

| raw 种类 | 判据 | 在条目列表里 |
|---|---|---|
| **容器**（8 条 feed-Raw） | `RawRecord.entry_kind == "feed"` | **本身不出现**，它的派生条目出现 |
| **本身即条目**（65 条 article-Raw） | `entry_kind` 不是 `"feed"` | 整篇作为一个条目出现 |
| 内容无法条目化（2 条无内容） | 同上 | 仍作为整篇条目出现（**不静默消失**），并带 `problems` |

> ⚠️ 判定容器**不看"解析是否成功"**，只看 `entry_kind`。反过来会让"解析器换了以后
> 某份 feed 解析失败"变成"它整份变成一个条目"——一个解析回归会静默改变列表语义。
> `entry_kind` 缺失（`None`，含全部既有记录）一律解释为"不是容器"，
> 与 `RawRecord` 的字段文档一致。

`entries_of` 注入
-----------------

本包**不 import** `atlas.entries`（T-130 不在 T-106 的白名单里，见
`tests/test_feed_http.py::test_feed_package_does_not_import_labels_or_registry_impl`）。
条目由调用方以 `entries_of(raw_record) -> Sequence[EntryView]` 注入；
`EntryView` 是**结构契约**（Protocol），只列本模块真正用到的字段。
未注入却请求条目粒度 ⇒ **响亮失败**，绝不返回"没有条目"的空结果。

排序稳定性与分页不重不漏
------------------------

文档模式排序键 = `(fetched_at, raw_id)`；条目模式 = `(timestamp, raw_id, char_start)`，
其中条目 `timestamp` = 条目自身的 `published_at`（缺失则回落到 Raw 的 `fetched_at`，
**绝不编造**）。两者都是**全序** ⇒ 分页切片不会出现"同一批数据两次排序结果不同"。


`RawRecord.fetched_at` 由 T-103 的归档层存为 UTC ISO-8601，读回来是 aware datetime；
若注入源给出 naive datetime，本模块**按 UTC 解释**（`_ensure_aware`），规则写死在这里，
不做"看情况"的隐式处理。

分页保护
--------

- `limit` 必须落在 `[1, MAX_LIMIT]`，越界**拒绝**（`InvalidQueryError`），不静默截断；
- `offset` 必须 `>= 0`；
- 未知筛选维度、重复单值参数、非法布尔/时间/排序值一律**拒绝**，不静默忽略。

`total` 是**筛选后**的总数。因为行业（`industry_of`）与标签（`label_lookup`）不在
`raw_records` 里，精确 `total` 需要全量扫描一次数据源；SPEC §1.5 的规模是"一个人每天看一次"，
全量扫描是刻意的取舍（`_scan_source` 以 `SOURCE_PAGE_SIZE` 分页拉取，并有硬上限保护）。

排序为什么分两步（两条路径共用同一手法）
----------------------------------------

1. 先按**并列判据**（文档模式 `raw_id` / 条目模式 `(raw_id, char_start)`）升序排一遍；
2. 再用**稳定排序**按时间排，`order="desc"` 时 `reverse=True`。

由于 Python 的排序是稳定的，第 2 步不会打乱第 1 步定下的同名次顺序，因此
**同一时间戳内的次序恒为并列判据升序**，与 `order` 无关。这保证全序 ⇒
`offset` 翻页**不重不漏**（`tests/test_feed_query.py` 用全量拼接对比验证）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from atlas.contracts import RawRecord

__all__ = [
    "DEFAULT_LIMIT",
    "ENTRY_GRANULARITY",
    "DOCUMENT_GRANULARITY",
    "GRANULARITIES",
    "MAX_LIMIT",
    "MAX_SCAN_RECORDS",
    "SOURCE_PAGE_SIZE",
    "EntriesLookup",
    "EntryView",
    "FeedEntry",
    "FeedItem",
    "FeedQuery",
    "FeedQueryError",
    "FeedResult",
    "InvalidQueryError",
    "LabelLookup",
    "SourceContractError",
    "run_query",
    "title_from_endpoint",
]

#: 默认页大小。
DEFAULT_LIMIT = 50
#: 页大小上限：防止一次拉爆（越界拒绝而不是截断）。
MAX_LIMIT = 200
#: 单次向数据源请求的页大小。
SOURCE_PAGE_SIZE = 200
#: 全量扫描的硬上限：超过即报错，绝不静默截断成"看起来正常"的结果。
MAX_SCAN_RECORDS = 200_000

#: 标签查询的可注入契约：`raw_id -> 该文档已有的标签键`。
#: T-108（Confirmed 存储）完成后由调用方接入；本包**不依赖** T-108 的实现。
LabelLookup = Callable[[str], Sequence[str]]

#: 两种粒度。`DOCUMENT_GRANULARITY` 是**默认值**：既有契约逐字节不变。
DOCUMENT_GRANULARITY = "document"
#: 条目粒度：一条 `FeedItem` 是一个可浏览单元（容器的派生条目 / 整篇即条目的文档）。
ENTRY_GRANULARITY = "entry"
GRANULARITIES = (DOCUMENT_GRANULARITY, ENTRY_GRANULARITY)

_ORDERS = ("asc", "desc")
_TRUTHY = ("true", "1")
_FALSY = ("false", "0")

#: 允许出现的查询参数（其余一律拒绝）。
KNOWN_PARAMS = frozenset(
    {
        "industry",
        "channel",
        "since",
        "until",
        "labels",
        "labeled",
        "order",
        "limit",
        "offset",
        "granularity",
    }
)

#: 只允许出现一次的查询参数（重复即语义不明 → 拒绝）。
SINGLE_VALUED_PARAMS = frozenset(
    {"since", "until", "labeled", "order", "limit", "offset", "granularity"}
)


class FeedQueryError(ValueError):
    """feed 查询层的基类异常。"""


class InvalidQueryError(FeedQueryError):
    """非法查询参数：**响亮拒绝**，不静默忽略、不静默截断。"""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(f"参数 {parameter!r} 非法：{message}")
        self.parameter = parameter
        self.message = message


class SourceContractError(FeedQueryError):
    """注入的数据源违反 `FeedSource` 契约（例如不按 limit 返回、扫描超上限）。"""


def _ensure_aware(moment: datetime) -> datetime:
    """`fetched_at` 统一成 aware UTC；naive 一律按 UTC 解释（写死的规则，不做猜测）。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _clean_ids(values: Iterable[str], parameter: str) -> Tuple[str, ...]:
    """去空白、去空串、去重（保序）。空值属于非法参数。"""
    cleaned: list[str] = []
    for raw in values:
        text = raw.strip()
        if not text:
            raise InvalidQueryError(parameter, "不允许空值")
        if text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned)


def _parse_moment(text: str, parameter: str) -> datetime:
    value = text.strip()
    if not value:
        raise InvalidQueryError(parameter, "不允许空值")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        hint = ""
        if " " in value:
            # 表单/前端常见的坑：'+' 未 URL 编码 → 被解码成空格 → 时区信息丢失
            hint = "（时区偏移里的 '+' 必须 URL 编码为 %2B，否则会被解码成空格）"
        raise InvalidQueryError(
            parameter, f"不是 ISO-8601 时间（收到 {text!r}）：{exc}{hint}"
        ) from exc
    if parsed.tzinfo is None:
        raise InvalidQueryError(
            parameter,
            "必须带时区偏移（例如 2026-01-01T00:00:00Z 或 +00:00），"
            "naive 时间含义不明，拒绝猜测",
        )
    return parsed.astimezone(timezone.utc)


def _parse_bool(text: str, parameter: str) -> bool:
    value = text.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise InvalidQueryError(parameter, f"只接受 true/false/1/0（收到 {text!r}）")


def _parse_int(text: str, parameter: str) -> int:
    value = text.strip()
    try:
        return int(value, 10)
    except ValueError as exc:
        raise InvalidQueryError(parameter, f"不是十进制整数（收到 {text!r}）") from exc


@dataclass(frozen=True)
class FeedQuery:
    """一次 feed 查询的**全部**输入。构造即校验，非法组合直接抛错。"""

    industries: Tuple[str, ...] = ()
    channels: Tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    labels: Tuple[str, ...] = ()
    labeled: bool | None = None
    order: str = "desc"
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    #: 粒度。默认 `"document"`（原契约）；`"entry"` 见模块 docstring。
    granularity: str = DOCUMENT_GRANULARITY

    def __post_init__(self) -> None:
        if self.granularity not in GRANULARITIES:
            raise InvalidQueryError(
                "granularity",
                f"只接受 {'/'.join(GRANULARITIES)}（收到 {self.granularity!r}）",
            )
        if self.order not in _ORDERS:
            raise InvalidQueryError("order", f"只接受 {'/'.join(_ORDERS)}（收到 {self.order!r}）")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool):
            raise InvalidQueryError("limit", f"必须是整数（收到 {self.limit!r}）")
        if self.limit < 1:
            raise InvalidQueryError("limit", f"必须 ≥ 1（收到 {self.limit}）")
        if self.limit > MAX_LIMIT:
            raise InvalidQueryError("limit", f"必须 ≤ {MAX_LIMIT}（收到 {self.limit}）")
        if not isinstance(self.offset, int) or isinstance(self.offset, bool):
            raise InvalidQueryError("offset", f"必须是整数（收到 {self.offset!r}）")
        if self.offset < 0:
            raise InvalidQueryError("offset", f"必须 ≥ 0（收到 {self.offset}）")
        if self.since is not None and self.since.tzinfo is None:
            raise InvalidQueryError("since", "必须是带时区的时间")
        if self.until is not None and self.until.tzinfo is None:
            raise InvalidQueryError("until", "必须是带时区的时间")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise InvalidQueryError("since", "不得晚于 until")
        if self.labels and self.labeled is False:
            raise InvalidQueryError(
                "labeled", "labeled=false（未打标）与 labels 筛选互相矛盾"
            )

    # ------------------------------------------------------------------
    # 参数解析（纯函数；HTTP 层只负责把异常翻成 400）
    # ------------------------------------------------------------------
    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> "FeedQuery":
        """从查询参数构造。`params` 的值可以是 `str` 或 `Sequence[str]`。

        未知参数、重复的单值参数、非法取值一律抛 `InvalidQueryError`。
        """
        values: Dict[str, list[str]] = {}
        for key, raw in params.items():
            if key not in KNOWN_PARAMS:
                raise InvalidQueryError(
                    key, f"未知参数（允许：{', '.join(sorted(KNOWN_PARAMS))}）"
                )
            items = _as_text_list(raw)
            if key in SINGLE_VALUED_PARAMS and len(items) > 1:
                raise InvalidQueryError(key, "该参数只允许出现一次")
            values[key] = items

        for key, items in values.items():
            for text in items:
                if not text.strip():
                    raise InvalidQueryError(key, "不允许空值")

        def multi(key: str) -> Tuple[str, ...]:
            return _clean_ids(_split_csv(values.get(key, [])), key)

        query = cls(
            industries=multi("industry"),
            channels=multi("channel"),
            since=_parse_moment(values["since"][0], "since") if "since" in values else None,
            until=_parse_moment(values["until"][0], "until") if "until" in values else None,
            labels=multi("labels"),
            labeled=_parse_bool(values["labeled"][0], "labeled") if "labeled" in values else None,
            order=values["order"][0].strip().lower() if "order" in values else "desc",
            limit=_parse_int(values["limit"][0], "limit") if "limit" in values else DEFAULT_LIMIT,
            offset=_parse_int(values["offset"][0], "offset") if "offset" in values else 0,
            granularity=(
                values["granularity"][0].strip().lower()
                if "granularity" in values
                else DOCUMENT_GRANULARITY
            ),
        )
        return query

    # ------------------------------------------------------------------
    @property
    def is_entry_mode(self) -> bool:
        return self.granularity == ENTRY_GRANULARITY

    def sort_description(self) -> Dict[str, str]:
        if self.is_entry_mode:
            return {
                "column": "timestamp",
                "order": self.order,
                "tie_break": "raw_id asc, char_start asc",
            }
        return {"column": "fetched_at", "order": self.order, "tie_break": "raw_id asc"}

    def filters_description(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "industries": list(self.industries),
            "channels": list(self.channels),
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "labels": list(self.labels),
            "labeled": self.labeled,
        }
        if self.is_entry_mode:
            # 只在条目模式出现：文档模式的 filters 逐字段不变（对外契约向后兼容）。
            data["granularity"] = self.granularity
        return data


def _as_text_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (bytes, bytearray)):
        return [raw.decode("utf-8")]
    if isinstance(raw, Sequence):
        return [str(item) for item in raw]
    return [str(raw)]


def _split_csv(items: Iterable[str]) -> list[str]:
    """兼容 `?industry=ai&industry=web` 与 `?industry=ai,web` 两种写法。"""
    out: list[str] = []
    for item in items:
        out.extend(part for part in item.split(","))
    return out


@dataclass(frozen=True)
class FeedItem:
    """投影出的一条 feed 记录（**只读视图**，不是事实本体）。"""

    raw_id: str
    channel_id: str
    industry: str | None
    endpoint: str
    content_sha256: str
    byte_length: int
    fetched_at: datetime
    http_status: int | None
    labels: Tuple[str, ...]

    @classmethod
    def of(
        cls,
        record: RawRecord,
        *,
        industry: str | None,
        labels: Sequence[str] = (),
    ) -> "FeedItem":
        return cls(
            raw_id=record.raw_id,
            channel_id=record.channel_id,
            industry=industry,
            endpoint=record.endpoint,
            content_sha256=record.content_sha256,
            byte_length=record.byte_length,
            fetched_at=_ensure_aware(record.fetched_at),
            http_status=record.http_status,
            labels=tuple(sorted(set(labels))),
        )


class EntryView(Protocol):
    """条目派生层（T-130）的**结构契约**：本模块只读这几个字段。

    为什么不直接 import `atlas.entries.Entry`：`atlas.feed` 的 import 白名单是
    `atlas.contracts` / `atlas.archive`（有 AST 测试钉死），把 T-130 拉进来会破坏
    §4.0 的包边界。用 Protocol 描述"我需要什么"，适配由调用方在组合根完成——
    这与 `FeedSource` 是同一手法，不是新的抽象。

    `anchor()` 必须返回一个与 `raw_id` / `raw_sha256` / 区间**一致**的
    `EvidenceAnchor`（`atlas.entries.Entry.anchor()` 就是这个）。本模块只把它转发，
    不做坐标运算（SPEC §2.2：坐标只能由确定性匹配/确定性切片产出）。

    `text_length` 是**解码后原文的字符数**（`EntrySet.feed_text` 的长度），
    整篇条目的区间 `[0, text_length)` 直接由它给出。刻意**不用 `byte_length`**：
    两者在非 ASCII 内容上不相等（UTF-8 下一个汉字 3 字节），
    拿字节数当字符数是本项目明令禁止的那类静默错位。
    """

    entry_id: str
    raw_id: str
    raw_sha256: str
    index: int
    kind: str
    char_start: int
    char_end: int
    title: str
    link: str
    published_at: Optional[str]
    text_length: int

    def anchor(self) -> Any: ...


class RawEntriesView(Protocol):
    """条目层对**一份原文**的结论（`entries_of` 的返回类型）。

    | 字段 | 含义 |
    |---|---|
    | `entries` | 这份原文的派生条目；**不是容器**时为空序列 |
    | `text_length` | 解码后原文的字符数（整篇条目的区间靠它定；见 `EntryView`） |

    为什么必须单独带 `text_length` 而不是从 `entries` 里取：**非容器本来就没有条目**，
    而整篇条目的区间仍然需要"这份原文有多长"。缺了它只能用字节数顶替，
    那正是要避免的静默错位。
    """

    entries: Sequence[EntryView]
    text_length: int


#: 条目查询的可注入契约：`RawRecord -> 这份原文的条目层结论`。
#: 只对 `RawRecord.entry_kind == "feed"` 的**容器**要求给出条目；
#: 其余返回空 `entries`，但**任何情况下都要给出 `text_length`**。
EntriesLookup = Callable[[RawRecord], RawEntriesView]


@dataclass(frozen=True)
class FeedEntry:
    """投影出的一个**可浏览单元**（条目粒度的 `FeedItem` 等价物）。

    它是**只读视图**，而且是**派生**视图：字段全部来自 `RawRecord` + 条目派生层，
    本类不新增任何事实。`granularity="entry"` 时一个 `FeedEntry` 就是列表里的一行。

    锚点四元组必须**一起**出现在这里（SPEC §6.3 集成路线图的硬要求）：

    | 字段 | 为什么必须有 |
    |---|---|
    | `raw_id` | 标签依附的不可变原文 |
    | `raw_sha256` | `EvidenceAnchor` 的必需字段（没有它前端构造不出锚点） |
    | `char_start` / `char_end` | SPEC §2.2 的真值区间 |

    只给 `entry_id` 是**不够**的：`entry_id` 是派生量（掺了解析器版本），
    SPEC §6.3 / T-130 明确禁止拿它当标签锚点（`Entry.as_anchor()` 永远抛）。
    因此 `entry_id` 在这里只是**展示与幂等**用的标识，**不作锚点**。
    """

    #: 派生标识；整篇即条目的 Raw 没有条目层 ID ⇒ `None`（用 `raw_id` 标识）。
    entry_id: str | None
    raw_id: str
    raw_sha256: str
    title: str
    link: str
    #: 条目自身的发布时间（定宽 UTC 串）；缺失时 `None` —— 由 `timestamp` 回落。
    published_at: str | None
    char_start: int
    char_end: int
    #: 该原文内部的文档序（0 起）；整篇即条目时为 0。
    ordinal: int
    #: `True` = 由容器（feed）派生；`False` = 整篇原文本身就是一个条目。
    from_feed: bool
    #: 文档上下文（筛选与展示都用得上，不重复请求）。
    channel_id: str
    industry: str | None
    endpoint: str
    content_sha256: str
    byte_length: int
    fetched_at: datetime
    http_status: int | None
    labels: Tuple[str, ...]
    #: 条目化过程中的**非致命**问题（例如"声明是 feed 但解析失败，已回落成整篇条目"）。
    #: 绝不静默：调用方（前端）必须能把它显出来。
    problems: Tuple[str, ...] = ()

    @property
    def timestamp(self) -> datetime:
        """排序与展示用的时间：条目发布时间优先，缺失则回落 Raw 的抓取时间。"""
        if self.published_at:
            try:
                return _ensure_aware(datetime.fromisoformat(self.published_at))
            except ValueError:  # pragma: no cover - 条目层已保证是 ISO 串
                pass
        return _ensure_aware(self.fetched_at)

    @property
    def published_from_entry(self) -> bool:
        """`timestamp` 是否来自条目自身的发布时间（`False` = 回落到了抓取时间）。"""
        return bool(self.published_at)

    @property
    def anchor_dict(self) -> Dict[str, Any]:
        """锚点四元组的对外形状（前端表单字段直接用它，不再自己拼）。"""
        return {
            "raw_id": self.raw_id,
            "raw_sha256": self.raw_sha256,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass(frozen=True)
class FeedResult:
    """一页结果 + 分页元数据。`total` 是**筛选后**的总数。

    `items` 的元素类型由 `query.granularity` 决定：文档模式是 `FeedItem`，
    条目模式是 `FeedEntry`。两者都只暴露只读视图字段。
    """

    query: FeedQuery
    items: Tuple[Any, ...]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None


def run_query(
    source: Any,
    query: FeedQuery,
    *,
    label_lookup: LabelLookup | None = None,
    entries_of: EntriesLookup | None = None,
) -> FeedResult:
    """执行一次只读查询。

    `source` 必须满足 `atlas.feed.repository.FeedSource`（`list_raw` / `industry_of`）。
    `label_lookup` 为 `None` 时：不做标签筛选，且 `FeedItem.labels` 一律为空元组；
    一旦查询要求标签信息（`labeled` 或 `labels`），则**响亮失败**——绝不假装"都没打标"。
    `entries_of` 只在 `query.granularity == "entry"` 时被需要；未注入就请求条目粒度
    同样**响亮失败**，绝不返回"一个条目都没有"的空结果。
    """
    if query is None:  # pragma: no cover - 防御性
        raise FeedQueryError("query 不能为 None")

    wants_labels = query.labeled is not None or bool(query.labels)
    if wants_labels and label_lookup is None:
        raise FeedQueryError(
            "该查询需要标签信息（labeled / labels 筛选），但未注入 label_lookup；"
            "拒绝返回把一切都当成未打标的结果（T-108 完成后由调用方接入）"
        )
    if query.is_entry_mode and entries_of is None:
        raise FeedQueryError(
            "granularity=entry 需要条目派生层，但未注入 entries_of；"
            "拒绝返回把容器本身当成条目的结果（SPEC §6.3 裁决 B）"
        )

    scanned = _scan_source(source)
    industries: Dict[str, str | None] = {}
    filtered: list[RawRecord] = []
    for record in scanned:
        industry = _industry_of(source, record.channel_id)
        industries[record.raw_id] = industry
        if not _matches(record, query, industry, label_lookup, wants_labels):
            continue
        filtered.append(record)

    if query.is_entry_mode:
        return _run_entry_query(
            filtered,
            industries,
            query,
            label_lookup=label_lookup,
            entries_of=entries_of,
        )

    ordered = _stable_order(filtered, query.order)
    total = len(ordered)
    page = ordered[query.offset : query.offset + query.limit]

    items = tuple(
        FeedItem.of(
            record,
            industry=industries.get(record.raw_id),
            labels=() if label_lookup is None else label_lookup(record.raw_id),
        )
        for record in page
    )
    returned = len(items)
    has_more = query.offset + returned < total
    return FeedResult(
        query=query,
        items=items,
        total=total,
        limit=query.limit,
        offset=query.offset,
        has_more=has_more,
        next_offset=(query.offset + returned) if has_more else None,
    )


# ---------------------------------------------------------------------- #
# 条目粒度
# ---------------------------------------------------------------------- #


def _require_text_length(record: RawRecord, view: Any) -> int:
    """取"解码后原文的字符数"；条目层没报就**响亮失败**。

    用 `byte_length` 顶替会在非 ASCII 内容上静默错位（UTF-8 下一个汉字 3 字节），
    而错位会直接变成"高亮错位 + 锚点错位"——正是本项目最不能容忍的那类故障。
    """
    value = getattr(view, "text_length", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceContractError(
            f"条目层违反契约：{record.raw_id} 的 text_length 必须是 ≥0 的整数"
            f"（解码后原文的字符数），收到 {value!r}；"
            "缺了它整篇条目的区间就只能靠字节数猜，拒绝返回可能错位的结果"
        )
    return value


def _run_entry_query(
    records: Sequence[RawRecord],
    industries: Mapping[str, str | None],
    query: FeedQuery,
    *,
    label_lookup: LabelLookup | None,
    entries_of: EntriesLookup | None,
) -> FeedResult:
    """条目粒度：把筛选后的 Raw 展开成可浏览单元，再排序 / 分页。"""
    assert entries_of is not None  # 由 run_query 保证
    units: list[FeedEntry] = []
    for record in records:
        labels = () if label_lookup is None else tuple(label_lookup(record.raw_id))
        view = entries_of(record)
        units.extend(
            _entry_units(
                record,
                tuple(view.entries),
                text_length=_require_text_length(record, view),
                industry=industries.get(record.raw_id),
                labels=labels,
            )
        )

    ordered = _stable_entry_order(units, query.order)
    total = len(ordered)
    page = ordered[query.offset : query.offset + query.limit]
    returned = len(page)
    has_more = query.offset + returned < total
    return FeedResult(
        query=query,
        items=tuple(page),
        total=total,
        limit=query.limit,
        offset=query.offset,
        has_more=has_more,
        next_offset=(query.offset + returned) if has_more else None,
    )


def _entry_units(
    record: RawRecord,
    entries: Sequence[EntryView],
    *,
    text_length: int,
    industry: str | None,
    labels: Sequence[str],
) -> list[FeedEntry]:
    """一个 Raw → 它在条目列表里的可浏览单元（SPEC §6.3 的容器口径）。

    - **容器**（`entry_kind == "feed"`）：展开成派生条目；**容器本身不出现**。
      若声明是容器却一条都没解析出来（解析失败 / 格式变了），**回落成整篇条目**
      并把理由写进 `problems` —— 不静默消失，也不是静默成功。
    - **其余**：整篇原文就是一个条目，区间取 `[0, text_length)`。

    锚点四元组一律**原样搬用**真实数据：容器走条目自己的
    `raw_id` / `raw_sha256` / 区间，整篇条目走 `RawRecord` 的
    `raw_id` / `content_sha256` 与整篇区间。本函数不做任何坐标推算。
    """
    sorted_labels = tuple(sorted(set(labels)))
    if record.entry_kind == "feed" and entries:
        return [
            FeedEntry(
                entry_id=entry.entry_id,
                raw_id=entry.raw_id,
                raw_sha256=entry.raw_sha256,
                title=entry.title,
                link=entry.link,
                published_at=entry.published_at,
                char_start=int(entry.char_start),
                char_end=int(entry.char_end),
                ordinal=int(entry.index),
                from_feed=True,
                channel_id=record.channel_id,
                industry=industry,
                endpoint=record.endpoint,
                content_sha256=record.content_sha256,
                byte_length=record.byte_length,
                fetched_at=_ensure_aware(record.fetched_at),
                http_status=record.http_status,
                labels=sorted_labels,
            )
            for entry in entries
        ]

    problems: Tuple[str, ...] = ()
    if record.entry_kind == "feed":
        problems = (
            "该 Raw 声明为 feed 容器（entry_kind=feed），但条目层没有产出任何条目"
            f"（返回 {len(entries)} 条）；已回落成整篇条目，理由需查条目层的 problems",
        )
    elif entries:
        # 非容器却拿到了条目：调用方把条目层用错了。不静默忽略这条信息。
        problems = (
            f"该 Raw 不是容器（entry_kind={record.entry_kind!r}），但条目层返回了 "
            f"{len(entries)} 条条目；已按整篇条目处理（容器口径见 SPEC §6.3）",
        )
    return [
        FeedEntry(
            entry_id=None,
            raw_id=record.raw_id,
            raw_sha256=record.content_sha256,
            title=title_from_endpoint(record.endpoint),
            link=record.endpoint,
            published_at=None,
            char_start=0,
            char_end=max(1, int(text_length)),
            ordinal=0,
            from_feed=False,
            channel_id=record.channel_id,
            industry=industry,
            endpoint=record.endpoint,
            content_sha256=record.content_sha256,
            byte_length=record.byte_length,
            fetched_at=_ensure_aware(record.fetched_at),
            http_status=record.http_status,
            labels=sorted_labels,
            problems=problems,
        )
    ]


#: 端点 URL 去掉结尾斜杠用的字符集（不是"generic 末段"集合，见下）。
_ENDPOINT_TAIL_STRIP = "/"

#: 端点 URL 的末段若是这些词，就没有识别力（"这份文档叫什么"回答不出来）。
#: 命中时回退成"主机 + 路径"，否则列表里会出现一堆叫 `feed` 的条目（真实数据实测）。
_GENERIC_ENDPOINT_TAILS = frozenset(
    {
        "",
        "feed",
        "feed.xml",
        "feeds",
        "rss",
        "rss.xml",
        "atom",
        "atom.xml",
        "index",
        "index.html",
        "index.php",
        "api",
        "search_by_date",
    }
)


def title_from_endpoint(endpoint: str) -> str:
    """整篇条目没有标题字段时的**派生**展示名（确定性、可重建）。

    这是一个**明确的回退**，不是猜标题：

    - 旧语料（T-131 导入的 65 篇）的 `content.bin` 是**正文纯文本**，里外都没有标题
      （实测：`<title>` / `og:title` / `<h1>` **一个都没有**，见交付报告）；
    - 因此这里**不**从正文里"提取"标题（那是编造），只用**原文链接本身**当展示名。

    规则（按顺序）：去掉协议、去掉 `?query` 与 `#fragment`、去掉结尾 `/`；
    取最后一段；若那一段没有识别力（空 / `feed` / `index.html` / …，见
    `_GENERIC_ENDPOINT_TAILS`），则退回"主机 + 路径"整串。
    实测这条规则把 `https://artificialintelligence-news.com/feed/` 这种
    "末段是 `feed`"的真实记录显示成 `artificialintelligence-news.com/feed`，
    而不是一个叫 `feed` 的条目。
    """
    text = endpoint.strip()
    if not text:
        return endpoint
    without_scheme = text.split("://", 1)[-1]
    identity = without_scheme.split("?", 1)[0].split("#", 1)[0].rstrip(_ENDPOINT_TAIL_STRIP)
    tail = identity.rsplit("/", 1)[-1] if "/" in identity else identity
    if tail and tail.lower() not in _GENERIC_ENDPOINT_TAILS:
        return tail
    return identity or text


def _stable_entry_order(entries: Sequence[FeedEntry], order: str) -> list[FeedEntry]:
    """先按并列判据 `(raw_id, char_start)` 升序，再稳定地按 `timestamp` 排 → 全序。"""
    by_position = sorted(entries, key=lambda item: (item.raw_id, item.char_start))
    return sorted(
        by_position,
        key=lambda item: item.timestamp,
        reverse=(order == "desc"),
    )


# ---------------------------------------------------------------------- #
# 内部
# ---------------------------------------------------------------------- #


def _scan_source(source: Any) -> list[RawRecord]:
    """按 `SOURCE_PAGE_SIZE` 分页拉完整个数据源（有硬上限保护）。"""
    collected: list[RawRecord] = []
    offset = 0
    while True:
        page = list(source.list_raw(SOURCE_PAGE_SIZE, offset))
        if len(page) > SOURCE_PAGE_SIZE:
            raise SourceContractError(
                f"数据源违反契约：请求 limit={SOURCE_PAGE_SIZE}，返回 {len(page)} 条"
            )
        collected.extend(page)
        if len(collected) > MAX_SCAN_RECORDS:
            raise SourceContractError(
                f"数据源记录数超过扫描上限 {MAX_SCAN_RECORDS}；"
                "请改用带索引的查询实现，而不是静默截断结果"
            )
        if len(page) < SOURCE_PAGE_SIZE:
            return collected
        offset += len(page)


def _industry_of(source: Any, channel_id: str) -> str | None:
    industry = source.industry_of(channel_id)
    if industry is not None and not isinstance(industry, str):
        raise SourceContractError(
            f"数据源违反契约：industry_of({channel_id!r}) 返回 {type(industry).__name__}"
        )
    return industry


def _matches(
    record: RawRecord,
    query: FeedQuery,
    industry: str | None,
    label_lookup: LabelLookup | None,
    wants_labels: bool,
) -> bool:
    if query.channels and record.channel_id not in query.channels:
        return False
    if query.industries and industry not in query.industries:
        return False

    moment = _ensure_aware(record.fetched_at)
    if query.since is not None and moment < query.since:
        return False
    if query.until is not None and moment > query.until:
        return False

    if not wants_labels:
        return True
    assert label_lookup is not None  # 由 run_query 保证
    keys = tuple(label_lookup(record.raw_id))
    if query.labels and not set(keys) & set(query.labels):
        return False
    if query.labeled is not None and bool(keys) is not query.labeled:
        return False
    return True


def _stable_order(records: Sequence[RawRecord], order: str) -> list[RawRecord]:
    """先按 `raw_id` 升序，再稳定地按 `fetched_at` 排 → 全序且与翻页无关。"""
    by_id = sorted(records, key=lambda record: record.raw_id)
    return sorted(
        by_id,
        key=lambda record: _ensure_aware(record.fetched_at),
        reverse=(order == "desc"),
    )
