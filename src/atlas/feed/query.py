"""T-106 Feed 查询模型：筛选 / 排序 / 分页（**纯逻辑，零 I/O，不碰 HTTP**）。

本模块是 feed 的**只读投影**语义所在：

- 只做 `筛选 + 稳定排序 + 分页`，**不产生事实**：不写 raw、不写标签、不写 Proposed。
  整个 `atlas.feed` 包的写路径为零（见 `tests/test_feed_http.py::test_feed_package_has_no_write_path`）。
- 数据来源是**注入**的 `atlas.feed.repository.FeedSource`（Protocol），本模块只依赖
  `atlas.contracts.RawRecord` 这一个共享类型，**不 import** `atlas.labels` / `atlas.archive` /
  `atlas.registry`（SPEC §4.0；T-108 正在并行写 `atlas.labels`，此处刻意解耦）。

排序稳定性与分页不重不漏
------------------------

排序键 = `(fetched_at, raw_id)`：

1. 先按 `raw_id` **升序**排一遍（`sorted`，稳定）；
2. 再用**稳定排序**按 `fetched_at` 排，`order="desc"` 时 `reverse=True`。

由于 Python 的排序是稳定的，第 2 步不会打乱第 1 步定下的同名次顺序，因此
**同一时间戳内的次序恒为 `raw_id` 升序**，与 `order` 无关。这保证：

- 全序（total order）→ 分页切片不会出现"同一批数据两次排序结果不同"；
- 因此 `offset` 翻页**不重不漏**（`tests/test_feed_query.py` 用全量拼接对比验证）。

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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence, Tuple

from atlas.contracts import RawRecord

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_SCAN_RECORDS",
    "SOURCE_PAGE_SIZE",
    "FeedItem",
    "FeedQuery",
    "FeedQueryError",
    "FeedResult",
    "InvalidQueryError",
    "LabelLookup",
    "SourceContractError",
    "run_query",
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
    }
)

#: 只允许出现一次的查询参数（重复即语义不明 → 拒绝）。
SINGLE_VALUED_PARAMS = frozenset({"since", "until", "labeled", "order", "limit", "offset"})


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

    def __post_init__(self) -> None:
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
        )
        return query

    # ------------------------------------------------------------------
    def sort_description(self) -> Dict[str, str]:
        return {"column": "fetched_at", "order": self.order, "tie_break": "raw_id asc"}

    def filters_description(self) -> Dict[str, Any]:
        return {
            "industries": list(self.industries),
            "channels": list(self.channels),
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "labels": list(self.labels),
            "labeled": self.labeled,
        }


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


@dataclass(frozen=True)
class FeedResult:
    """一页结果 + 分页元数据。`total` 是**筛选后**的总数。"""

    query: FeedQuery
    items: Tuple[FeedItem, ...]
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
) -> FeedResult:
    """执行一次只读查询。

    `source` 必须满足 `atlas.feed.repository.FeedSource`（`list_raw` / `industry_of`）。
    `label_lookup` 为 `None` 时：不做标签筛选，且 `FeedItem.labels` 一律为空元组；
    一旦查询要求标签信息（`labeled` 或 `labels`），则**响亮失败**——绝不假装"都没打标"。
    """
    if query is None:  # pragma: no cover - 防御性
        raise FeedQueryError("query 不能为 None")

    wants_labels = query.labeled is not None or bool(query.labels)
    if wants_labels and label_lookup is None:
        raise FeedQueryError(
            "该查询需要标签信息（labeled / labels 筛选），但未注入 label_lookup；"
            "拒绝返回把一切都当成未打标的结果（T-108 完成后由调用方接入）"
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
