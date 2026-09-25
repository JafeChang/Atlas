"""T-106 查询逻辑的**纯单元测试**：筛选 / 排序 / 稳定排序 / 分页边界 / limit 上限。

约束（对应任务书）：
- **不碰 HTTP、不碰文件系统**：数据源是本地假对象 `FakeSource`；
- 「非法参数必须被拒绝」逐项覆盖，且断言**没有被静默忽略**；
- 「分页不重不漏」用全量拼接与一次性结果对比验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

import pytest

from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256
from atlas.feed.query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SOURCE_PAGE_SIZE,
    FeedQuery,
    FeedQueryError,
    InvalidQueryError,
    SourceContractError,
    run_query,
)
from atlas.feed.repository import (
    EMPTY_LABEL_LOOKUP,
    ArchiveFeedSource,
    FeedSource,
    StaticFeedSource,
    labels_from_mapping,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- #
# 测试替身
# ---------------------------------------------------------------------- #
def make_record(
    raw_id: str,
    *,
    channel_id: str = "chan-1",
    fetched_at: Optional[datetime] = None,
    endpoint: Optional[str] = None,
    byte_length: int = 10,
    http_status: Optional[int] = 200,
) -> RawRecord:
    """直接构造 `RawRecord`（raw_id 由测试指定，便于断言顺序）。"""
    endpoint = endpoint or f"https://example.test/{raw_id}"
    payload = f"{raw_id}|{channel_id}".encode()
    return RawRecord(
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=endpoint,
        content_sha256=content_sha256(payload),
        byte_length=byte_length,
        fetched_at=fetched_at or BASE,
        http_status=http_status,
    )


class FakeSource:
    """最小 `FeedSource` 假实现（**只读**，记录被调用的分页参数）。"""

    def __init__(
        self,
        records: Iterable[RawRecord],
        *,
        industries: Optional[Dict[str, str]] = None,
        over_return: bool = False,
        ignore_offset: bool = False,
    ) -> None:
        self.records: List[RawRecord] = list(records)
        self.industries = dict(industries or {})
        self.calls: List[tuple[int, int]] = []
        self._over_return = over_return
        self._ignore_offset = ignore_offset

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]:
        self.calls.append((limit, offset))
        ordered = sorted(self.records, key=lambda record: record.raw_id)
        if self._ignore_offset:
            return ordered[:limit]  # 故意违约：无视 offset，永远返回第一页
        page = ordered[offset : offset + limit]
        if self._over_return:
            return page + [make_record(f"extra-{index}") for index in range(limit)]
        return page

    def industry_of(self, channel_id: str) -> Optional[str]:
        return self.industries.get(channel_id)


def lookup_of(mapping: Dict[str, Sequence[str]]):
    return lambda raw_id: tuple(mapping.get(raw_id, ()))


def ids(result) -> List[str]:
    return [item.raw_id for item in result.items]


# ---------------------------------------------------------------------- #
# 排序
# ---------------------------------------------------------------------- #
def test_default_order_is_newest_first() -> None:
    records = [
        make_record("r-old", fetched_at=BASE),
        make_record("r-new", fetched_at=BASE + timedelta(days=2)),
        make_record("r-mid", fetched_at=BASE + timedelta(days=1)),
    ]
    result = run_query(FakeSource(records), FeedQuery())
    assert ids(result) == ["r-new", "r-mid", "r-old"]
    assert result.query.order == "desc"
    assert result.total == 3
    assert result.has_more is False
    assert result.next_offset is None


def test_ascending_order_supported() -> None:
    records = [
        make_record("r-old", fetched_at=BASE),
        make_record("r-new", fetched_at=BASE + timedelta(days=2)),
    ]
    result = run_query(FakeSource(records), FeedQuery(order="asc"))
    assert ids(result) == ["r-old", "r-new"]
    assert result.query.sort_description() == {
        "column": "fetched_at",
        "order": "asc",
        "tie_break": "raw_id asc",
    }


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_tie_break_is_raw_id_ascending_regardless_of_order(order: str) -> None:
    """同一时间戳时以 raw_id 兜底 → 全序，翻页才可能不重不漏。"""
    same = BASE + timedelta(hours=1)
    records = [make_record("r-c", fetched_at=same), make_record("r-a", fetched_at=same),
               make_record("r-b", fetched_at=same)]
    result = run_query(FakeSource(records), FeedQuery(order=order, limit=MAX_LIMIT))
    assert ids(result) == ["r-a", "r-b", "r-c"]


def test_naive_fetched_at_is_interpreted_as_utc() -> None:
    """naive 时间按 UTC 解释（写死的规则），因此与 aware UTC 同刻可比较、可筛选。"""
    naive = datetime(2026, 1, 1, 12, 0, 0)
    records = [
        make_record("r-naive", fetched_at=naive),
        make_record("r-aware", fetched_at=BASE + timedelta(seconds=1)),
    ]
    result = run_query(
        FakeSource(records), FeedQuery(since=BASE, until=BASE + timedelta(minutes=5))
    )
    assert ids(result) == ["r-aware", "r-naive"]
    assert result.items[1].fetched_at == BASE


# ---------------------------------------------------------------------- #
# 分页
# ---------------------------------------------------------------------- #
def _many(count: int, *, repeats: int = 4) -> List[RawRecord]:
    """`repeats` 条记录共用同一时间戳 → 制造大量并列键，压测稳定排序。"""
    return [
        make_record(
            f"r-{index:04d}",
            channel_id=f"chan-{index % 3}",
            fetched_at=BASE + timedelta(minutes=index // repeats),
        )
        for index in range(count)
    ]


def test_pagination_is_complete_and_disjoint() -> None:
    records = _many(93)
    source = FakeSource(records)
    everything = run_query(source, FeedQuery(limit=MAX_LIMIT))
    assert everything.total == 93

    page_size = 7
    collected: List[str] = []
    offset = 0
    while True:
        page = run_query(source, FeedQuery(limit=page_size, offset=offset))
        assert page.total == everything.total  # total 恒为筛选后的总数，与分页无关
        collected.extend(ids(page))
        if not page.has_more:
            assert page.next_offset is None
            break
        assert page.next_offset == offset + len(page.items)
        offset = page.next_offset

    assert collected == ids(everything)  # 不重不漏且次序一致
    assert len(set(collected)) == len(collected)


def test_scan_follows_source_pages_and_stops_at_short_page() -> None:
    records = _many(SOURCE_PAGE_SIZE + 30)
    source = FakeSource(records)
    result = run_query(source, FeedQuery(limit=DEFAULT_LIMIT))
    assert result.total == SOURCE_PAGE_SIZE + 30
    assert source.calls == [(SOURCE_PAGE_SIZE, 0), (SOURCE_PAGE_SIZE, SOURCE_PAGE_SIZE)]


def test_offset_beyond_total_returns_empty_page() -> None:
    result = run_query(FakeSource(_many(5)), FeedQuery(limit=10, offset=99))
    assert result.items == ()
    assert result.total == 5
    assert result.has_more is False
    assert result.next_offset is None


def test_last_page_reports_has_more_false() -> None:
    result = run_query(FakeSource(_many(10)), FeedQuery(limit=5, offset=5))
    assert result.total == 10
    assert len(result.items) == 5
    assert result.has_more is False


def test_source_returning_more_than_requested_fails_loudly() -> None:
    """违约源必须响亮失败，而不是被静默截断成"看起来正常的一页"。"""
    source = FakeSource(_many(SOURCE_PAGE_SIZE), over_return=True)
    with pytest.raises(SourceContractError, match="违反契约"):
        run_query(source, FeedQuery())


def test_source_ignoring_offset_is_stopped_by_scan_guard() -> None:
    """无视 offset 的源不得把服务拖进死循环：扫描硬上限响亮失败。"""
    source = FakeSource(_many(SOURCE_PAGE_SIZE), ignore_offset=True)
    with pytest.raises(SourceContractError, match="扫描上限"):
        run_query(source, FeedQuery())


# ---------------------------------------------------------------------- #
# limit / offset 保护
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": MAX_LIMIT + 1},
        {"offset": -1},
        {"order": "newest"},
        {"limit": "50"},  # type: ignore[dict-item]
        {"offset": True},  # type: ignore[dict-item]
    ],
)
def test_illegal_query_object_is_rejected(kwargs: Dict[str, object]) -> None:
    with pytest.raises(InvalidQueryError):
        FeedQuery(**kwargs)  # type: ignore[arg-type]


def test_limit_at_upper_bound_is_accepted() -> None:
    result = run_query(FakeSource(_many(3)), FeedQuery(limit=MAX_LIMIT))
    assert result.limit == MAX_LIMIT


def test_default_limit_is_used_when_absent() -> None:
    assert FeedQuery.from_params({}).limit == DEFAULT_LIMIT


# ---------------------------------------------------------------------- #
# 参数解析：非法一律拒绝
# ---------------------------------------------------------------------- #
def test_unknown_parameter_is_rejected() -> None:
    with pytest.raises(InvalidQueryError) as excinfo:
        FeedQuery.from_params({"sector": ["ai"]})
    assert excinfo.value.parameter == "sector"
    assert "未知参数" in excinfo.value.message


def test_duplicate_single_valued_parameter_is_rejected() -> None:
    with pytest.raises(InvalidQueryError) as excinfo:
        FeedQuery.from_params({"limit": ["5", "10"]})
    assert excinfo.value.parameter == "limit"


@pytest.mark.parametrize(
    "params, parameter",
    [
        ({"since": ["2026-01-01"]}, "since"),               # 缺时区
        ({"since": ["2026-01-01T00:00:00"]}, "since"),      # naive → 拒绝
        ({"since": ["not-a-time"]}, "since"),
        ({"until": ["2026-13-45T00:00:00Z"]}, "until"),
        ({"labeled": ["maybe"]}, "labeled"),
        ({"limit": ["five"]}, "limit"),
        ({"offset": [""]}, "offset"),
        ({"industry": [""]}, "industry"),
        ({"channel": ["a", ""]}, "channel"),
    ],
)
def test_malformed_parameter_values_are_rejected(
    params: Dict[str, Sequence[str]], parameter: str
) -> None:
    with pytest.raises(InvalidQueryError) as excinfo:
        FeedQuery.from_params(params)
    assert excinfo.value.parameter == parameter


def test_since_after_until_is_rejected() -> None:
    with pytest.raises(InvalidQueryError) as excinfo:
        FeedQuery.from_params(
            {"since": ["2026-02-01T00:00:00Z"], "until": ["2026-01-01T00:00:00Z"]}
        )
    assert excinfo.value.parameter == "since"


def test_labels_with_labeled_false_is_rejected_as_contradiction() -> None:
    with pytest.raises(InvalidQueryError) as excinfo:
        FeedQuery.from_params({"labeled": ["false"], "labels": ["valid"]})
    assert excinfo.value.parameter == "labeled"


def test_params_are_parsed_from_strings_and_repeated_keys() -> None:
    query = FeedQuery.from_params(
        {
            "industry": ["ai", "web,devices"],
            "channel": ["chan-1"],
            "since": ["2026-01-01T00:00:00Z"],
            "until": ["2026-01-02T00:00:00+00:00"],
            "labels": ["valid"],
            "labeled": ["true"],
            "order": ["ASC"],
            "limit": ["5"],
            "offset": ["10"],
        }
    )
    assert query.industries == ("ai", "web", "devices")
    assert query.channels == ("chan-1",)
    assert query.labels == ("valid",)
    assert query.labeled is True
    assert query.order == "asc"
    assert (query.limit, query.offset) == (5, 10)
    assert query.since == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- #
# 筛选
# ---------------------------------------------------------------------- #
def test_channel_filter() -> None:
    records = [
        make_record("r-1", channel_id="chan-1"),
        make_record("r-2", channel_id="chan-2"),
    ]
    result = run_query(FakeSource(records), FeedQuery(channels=("chan-2",)))
    assert ids(result) == ["r-2"]


def test_industry_filter_uses_injected_lookup() -> None:
    records = [
        make_record("r-1", channel_id="chan-1"),
        make_record("r-2", channel_id="chan-2"),
        make_record("r-3", channel_id="chan-3"),  # 无行业归属
    ]
    source = FakeSource(records, industries={"chan-1": "ai", "chan-2": "web"})
    result = run_query(source, FeedQuery(industries=("ai",)))
    assert ids(result) == ["r-1"]

    result = run_query(source, FeedQuery(industries=("ai", "web")))
    assert ids(result) == ["r-1", "r-2"]  # 同刻 → raw_id 升序兜底

    # 无行业归属的渠道在按行业筛选时被排除（不猜、不归属到"其它"）
    result = run_query(source, FeedQuery(industries=("ai", "web")), label_lookup=None)
    assert "r-3" not in ids(result)
    assert result.items[0].industry in {"ai", "web"}


def test_time_range_is_inclusive_on_both_ends() -> None:
    records = [
        make_record("r-before", fetched_at=BASE - timedelta(seconds=1)),
        make_record("r-since", fetched_at=BASE),
        make_record("r-until", fetched_at=BASE + timedelta(hours=1)),
        make_record("r-after", fetched_at=BASE + timedelta(hours=1, seconds=1)),
    ]
    result = run_query(
        FakeSource(records), FeedQuery(since=BASE, until=BASE + timedelta(hours=1))
    )
    assert sorted(ids(result)) == ["r-since", "r-until"]


def test_industry_is_exposed_on_items_without_industry_filter() -> None:
    records = [make_record("r-1", channel_id="chan-1")]
    source = FakeSource(records, industries={"chan-1": "ai"})
    result = run_query(source, FeedQuery())
    assert result.items[0].industry == "ai"

    no_index = run_query(FakeSource(records), FeedQuery())
    assert no_index.items[0].industry is None  # 没有行业源就是 None，不编造


def test_label_metadata_is_sorted_and_deduplicated() -> None:
    records = [make_record("r-1")]
    lookup = lookup_of({"r-1": ("valid", "ai", "valid")})
    result = run_query(FakeSource(records), FeedQuery(), label_lookup=lookup)
    assert result.items[0].labels == ("ai", "valid")


def test_items_have_empty_labels_without_injected_lookup() -> None:
    result = run_query(FakeSource([make_record("r-1")]), FeedQuery())
    assert result.items[0].labels == ()


# ---------------------------------------------------------------------- #
# 标签筛选：必须由注入的 label_lookup 提供，缺了就响亮失败
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("query", [FeedQuery(labeled=True), FeedQuery(labels=("valid",))])
def test_label_filters_without_lookup_fail_loudly(query: FeedQuery) -> None:
    with pytest.raises(FeedQueryError, match="label_lookup"):
        run_query(FakeSource([make_record("r-1")]), query)


def test_labeled_true_selects_only_labeled_records() -> None:
    records = [make_record("r-1"), make_record("r-2"), make_record("r-3")]
    lookup = lookup_of({"r-2": ("valid",)})
    result = run_query(FakeSource(records), FeedQuery(labeled=True), label_lookup=lookup)
    assert ids(result) == ["r-2"]
    assert result.total == 1


def test_labeled_false_selects_only_unlabeled_records() -> None:
    records = [make_record("r-1"), make_record("r-2"), make_record("r-3")]
    lookup = lookup_of({"r-2": ("valid",)})
    result = run_query(FakeSource(records), FeedQuery(labeled=False), label_lookup=lookup)
    assert ids(result) == ["r-1", "r-3"]


def test_labels_filter_has_or_semantics() -> None:
    records = [make_record("r-1"), make_record("r-2"), make_record("r-3")]
    lookup = lookup_of({"r-1": ("valid",), "r-2": ("spam",), "r-3": ("ai",)})
    result = run_query(
        FakeSource(records), FeedQuery(labels=("valid", "spam")), label_lookup=lookup
    )
    assert ids(result) == ["r-1", "r-2"]


def test_filters_combine_as_conjunction() -> None:
    records = [
        make_record("r-1", channel_id="chan-1", fetched_at=BASE + timedelta(hours=2)),
        make_record("r-2", channel_id="chan-1", fetched_at=BASE),
        make_record("r-3", channel_id="chan-2", fetched_at=BASE + timedelta(hours=2)),
    ]
    source = FakeSource(records, industries={"chan-1": "ai", "chan-2": "ai"})
    lookup = lookup_of({"r-1": ("valid",), "r-3": ("valid",)})
    result = run_query(
        source,
        FeedQuery(
            industries=("ai",),
            channels=("chan-1",),
            since=BASE + timedelta(hours=1),
            labeled=True,
        ),
        label_lookup=lookup,
    )
    assert ids(result) == ["r-1"]


# ---------------------------------------------------------------------- #
# 注入接缝：FeedSource Protocol 与标签查询
# ---------------------------------------------------------------------- #
class StubArchive:
    """只实现归档层的两个**读**方法，用于证明 `ArchiveFeedSource` 是鸭子类型适配。"""

    def __init__(self, records: Iterable[RawRecord]) -> None:
        self._by_id = {record.raw_id: record for record in records}

    def all_raw_ids(self) -> List[str]:
        return sorted(self._by_id)

    def get(self, raw_id: str) -> RawRecord:
        return self._by_id[raw_id]


def test_fake_source_satisfies_feed_source_protocol() -> None:
    assert isinstance(FakeSource([]), FeedSource)
    assert isinstance(StaticFeedSource([]), FeedSource)


def test_archive_feed_source_is_a_read_only_adapter() -> None:
    records = [
        make_record("r-2", channel_id="chan-1", fetched_at=BASE),
        make_record("r-1", channel_id="chan-2", fetched_at=BASE),
    ]
    source = ArchiveFeedSource(
        StubArchive(records),  # type: ignore[arg-type]
        industry_of={"chan-1": "ai"},
    )
    assert isinstance(source, FeedSource)
    assert [record.raw_id for record in source.list_raw(10, 0)] == ["r-1", "r-2"]
    assert [record.raw_id for record in source.list_raw(1, 1)] == ["r-2"]
    assert source.industry_of("chan-1") == "ai"
    assert source.industry_of("chan-unknown") is None  # 未归行业 → None，不编造

    with pytest.raises(ValueError):
        source.list_raw(0, 0)
    with pytest.raises(ValueError):
        source.list_raw(10, -1)


def test_labels_from_mapping_drives_label_filters() -> None:
    records = [make_record("r-1"), make_record("r-2")]
    lookup = labels_from_mapping({"r-1": ["valid"]})
    result = run_query(FakeSource(records), FeedQuery(labeled=True), label_lookup=lookup)
    assert ids(result) == ["r-1"]
    assert result.items[0].labels == ("valid",)

    empty = run_query(FakeSource(records), FeedQuery(), label_lookup=EMPTY_LABEL_LOOKUP)
    assert all(item.labels == () for item in empty.items)
