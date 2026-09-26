"""T-109 补完（feed 条目化）的验收判据 —— **先定义后实现**（照 SPEC §4.1 T-002 判据的写法）。

背景：SPEC §6.5 的复审结论是"真正该做的下一个任务不是那四个受限任务，而是把 T-130 的
条目接进 feed 与前端"。这不是新增范围，而是 **T-109 自己那句"feed 浏览"的完成**。

本文件是**判据的正文**。实现写在 `src/atlas/contracts/states.py`、`src/atlas/feed/`、
`src/atlas/webui/`、`src/atlas/webapp.py`。判据逐条如下。

判据 1 —— 不带 anchor 的 `human()` 与现在**逐字段相同**
    构造 `ConfirmedLabel.human(raw_id=…, label_key=…, label_value=…, actor=…)` 与
    `ConfirmedLabel(raw_id=…, …, anchor=None)` 逐字段相等；`label_id` 与
    `label_id_for(raw_id, label_key, label_value, actor)` 相同（内容寻址口径未变）。

判据 2 —— 既有标签读写不退化
    不带 anchor 的标签写入 `SqliteConfirmedStore` 后 `all_for` / `latest_value` /
    `count` / 独立重开回读全部逐字段一致；导出/回流的 `label_id` 校验仍然通过。

判据 3 —— 锚点约束：`anchor.raw_id != raw_id` 必须**响亮失败**（**配活对照**）
    同一调用路径（`ConfirmedLabel.human`）对**相等**的 `raw_id` 必须**成功**，
    否则"被拒绝"可能只是签名不匹配 / 异常类型不对伪装的。

判据 4 —— 条目视图的容器口径（SPEC §6.3 裁决 B）
    - 容器（`entry_kind="feed"`）**本身不出现**在条目列表里；
    - 它派生出的条目**出现**；
    - `entry_kind is None` 的 Raw（本身就是条目）**整篇作为一条出现**；
    - 条目粒度未注入 `entries_of` 时**响亮失败**，绝不返回空列表。

判据 5 —— 锚点四元组必须一起出现在条目投影里
    每个条目都带 `raw_id` / `raw_sha256` / `char_start` / `char_end`；
    `raw_sha256` 与原文指纹一致；区间落在原文内且非空。
    （少了 `raw_sha256` 前端就构造不出 `EvidenceAnchor`，SPEC §6.3 明确要求。）

判据 6 —— 排序与分页在条目粒度下仍是全序、不重不漏
    全量拼接 == 逐页拼接；同时间戳内次序恒为 `(raw_id, char_start)` 升序。

判据 7 —— `contract_version` 的升版与向后兼容
    新增条目粒度后 `CONTRACT_VERSION == 2`；**文档粒度**的响应体在
    `items` 字段集与语义上逐字段不变（只有 `filters` 多一个 `granularity` 键）。

判据 8 —— 零新增 Python 依赖
    `pyproject.toml` 的依赖集合与基线一致（本文件不新增 import 之外的任何依赖）。

判据 9 —— 不改 `atlas.entries` / `atlas.migrate` / `atlas.search` / `atlas.chunk` /
    `atlas.registry` / `atlas.compose` / `atlas.archive` / `atlas.cognition`
    用 AST 静态检查：本次改动的包不 import 它们的新符号（`atlas.feed` 的既有边界测试
    继续在 `test_feed_http.py` 里钉死）。

判据 10 —— 真实数据（`data/store`）上的实测数字，见
    `tests/test_feed_entries_realdata.py`：归档 **75** 条 = 容器 **8** 条（本身不作为条目
    出现）+ 本身即条目 **67** 条；派生条目 **830**；**可浏览条目总数 897**。
    ⚠️ 实测与任务书写的 895（65 + 830）差 2：那 2 条不是"无内容"，而是
    `ai-news-blog`（552,714 字符的 HTML 页面）与 `hacker-news-frontpage`（JSON API 响应）——
    它们是归档里**有内容**的记录，按"整篇即条目"出现，否则它们会**不可浏览、不可打标**
    （正是 §6.3 要消灭的那种静默消失）。逐条证据见该文件的模块 docstring。

> 判据 1–9 在合成夹具上跑；判据 10 需要真实 `data/`，因此**自跳过**
> （照 `tests/test_search_realdata.py` 的先例）。
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Sequence, Tuple

import pytest

from atlas.contracts import ConfirmedLabel, EvidenceAnchor, RawRecord
from atlas.contracts.anchors import AnchorError
from atlas.contracts.ids import content_sha256, label_id_for
from atlas.feed import (
    CONTRACT_VERSION,
    DOCUMENT_GRANULARITY,
    ENTRY_GRANULARITY,
    FeedEntry,
    FeedQuery,
    FeedQueryError,
    StaticFeedSource,
    feed_payload,
    run_query,
)
from atlas.labels import open_store

REPO_ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
BASE = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)

FEED_TEXT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<rss version=\"2.0\"><channel><title>demo</title>"
    "<item><title>Alpha &amp; Beta</title><link>https://example.test/alpha</link>"
    "<pubDate>Mon, 02 Mar 2026 10:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>alpha body</p>]]></description></item>"
    "<item><title>Gamma</title><link>https://example.test/gamma</link>"
    "<pubDate>Mon, 02 Mar 2026 11:00:00 +0000</pubDate>"
    "<description><![CDATA[<p>gamma body</p>]]></description></item>"
    "</channel></rss>"
)
ARTICLE_TEXT = "This article page is one whole entry, with no <item> elements at all."

FEED_RAW = "raw-feed-0001"
ARTICLE_RAW = "raw-article-0002"
EMPTY_RAW = "raw-empty-0003"


# ---------------------------------------------------------------------- #
# 夹具：一份"容器" + 一篇"本身就是条目" + 一条"没有内容"
# ---------------------------------------------------------------------- #
class _FakeEntry:
    """`atlas.feed.query.EntryView` 的结构实现（本测试不 import `atlas.entries`）。

    刻意手写而不是复用 T-130：这样判据 4/5 断言的是 **T-106 的投影语义**
    （容器口径、锚点四元组），而不是把 T-130 的输出再抄一遍。
    """

    def __init__(
        self,
        *,
        raw_id: str,
        raw_sha256: str,
        index: int,
        char_start: int,
        char_end: int,
        title: str,
        link: str,
        published_at: str | None,
        text_length: int,
    ) -> None:
        self.entry_id = f"ent_{index:032x}"
        self.raw_id = raw_id
        self.raw_sha256 = raw_sha256
        self.index = index
        self.kind = "rss"
        self.char_start = char_start
        self.char_end = char_end
        self.title = title
        self.link = link
        self.published_at = published_at
        self.text_length = text_length

    def anchor(self) -> EvidenceAnchor:
        return EvidenceAnchor.create(
            raw_id=self.raw_id,
            raw_sha256=self.raw_sha256,
            char_start=self.char_start,
            char_end=self.char_end,
        )


class _View:
    """`atlas.feed.query.RawEntriesView` 的结构实现。"""

    def __init__(self, entries: Sequence[Any], text_length: int) -> None:
        self.entries = tuple(entries)
        self.text_length = text_length


def _records() -> Tuple[RawRecord, ...]:
    return (
        RawRecord(
            raw_id=FEED_RAW,
            channel_id="chan-feed",
            endpoint="https://example.test/feed.xml",
            content_sha256=content_sha256(FEED_TEXT.encode("utf-8")),
            byte_length=len(FEED_TEXT.encode("utf-8")),
            fetched_at=BASE,
            http_status=200,
            entry_kind="feed",
        ),
        RawRecord(
            raw_id=ARTICLE_RAW,
            channel_id="chan-article",
            endpoint="https://example.test/an-article",
            content_sha256=content_sha256(ARTICLE_TEXT.encode("utf-8")),
            byte_length=len(ARTICLE_TEXT.encode("utf-8")),
            fetched_at=BASE + timedelta(minutes=5),
            http_status=200,
            entry_kind=None,
        ),
        RawRecord(
            raw_id=EMPTY_RAW,
            channel_id="chan-article",
            endpoint="https://example.test/empty",
            content_sha256=content_sha256(b""),
            byte_length=0,
            fetched_at=BASE + timedelta(minutes=10),
            http_status=200,
            entry_kind=None,
        ),
    )


def _source() -> StaticFeedSource:
    return StaticFeedSource(
        _records(),
        industry_of={"chan-feed": "ai", "chan-article": "web"},
        entry_kind_of={},  # 记录自身已带 entry_kind，注入不覆盖
    )


def _entries_of(record: RawRecord) -> _View:
    """容器 → 两个派生条目；其余 → 零条目 + 原文长度。"""
    if record.raw_id != FEED_RAW:
        text = ARTICLE_TEXT if record.raw_id == ARTICLE_RAW else ""
        return _View(entries=(), text_length=len(text))
    text = FEED_TEXT
    first = text.index("<item>")
    second = text.index("<item>", first + 1)
    return _View(
        entries=(
            _FakeEntry(
                raw_id=FEED_RAW,
                raw_sha256=record.content_sha256,
                index=0,
                char_start=first,
                char_end=second,
                title="Alpha & Beta",
                link="https://example.test/alpha",
                published_at="2026-03-02T10:00:00.000000+00:00",
                text_length=len(text),
            ),
            _FakeEntry(
                raw_id=FEED_RAW,
                raw_sha256=record.content_sha256,
                index=1,
                char_start=second,
                char_end=text.index("</channel>"),
                title="Gamma",
                link="https://example.test/gamma",
                published_at="2026-03-02T11:00:00.000000+00:00",
                text_length=len(text),
            ),
        ),
        text_length=len(text),
    )


def _entry_result(**kwargs: Any):
    query = FeedQuery(granularity=ENTRY_GRANULARITY, limit=200, **kwargs)
    return run_query(_source(), query, entries_of=_entries_of)


# ===================================================================== #
# 判据 1：不带 anchor 的 human() 与现在逐字段相同
# ===================================================================== #
def test_criterion_1_human_without_anchor_is_field_identical() -> None:
    """文档级直判的字段与内容寻址口径**逐字段**未变。"""
    made = ConfirmedLabel.human(
        raw_id=ARTICLE_RAW, label_key="industry", label_value="ai", actor="me"
    )
    direct = ConfirmedLabel(
        label_id=label_id_for(ARTICLE_RAW, "industry", "ai", "me"),
        raw_id=ARTICLE_RAW,
        label_key="industry",
        label_value="ai",
        actor="me",
        anchor=None,
    )
    assert made.model_dump(exclude={"created_at"}) == direct.model_dump(
        exclude={"created_at"}
    ), "除 created_at（构造时刻）外必须逐字段相同"
    assert made.anchor is None
    assert made.label_id == label_id_for(ARTICLE_RAW, "industry", "ai", "me")
    # 活对照：旧签名（不带 anchor 关键字）也照样工作
    legacy = ConfirmedLabel.human(
        raw_id=ARTICLE_RAW, label_key="industry", label_value="ai", actor="me"
    )
    assert legacy.label_id == made.label_id


# ===================================================================== #
# 判据 2：既有标签读写不退化
# ===================================================================== #
def test_criterion_2_existing_label_round_trip_is_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "labels.db"
    with open_store(db) as store:
        stored = store.add(
            ConfirmedLabel.human(
                raw_id=ARTICLE_RAW, label_key="valid", label_value="valid", actor="me"
            )
        )
        assert stored.anchor is None
        assert store.count() == 1
        assert store.latest_value(ARTICLE_RAW, "valid") == "valid"
        assert store.keys_for(ARTICLE_RAW) == ["valid"]
        # 幂等：同判断重复写不产生第二条
        assert store.add(stored).label_id == stored.label_id
        assert store.count() == 1
    # 独立重开：字段逐一致
    with open_store(db) as reopened:
        rows = reopened.all_for(ARTICLE_RAW)
        assert len(rows) == 1
        assert rows[0] == stored
        # 导出的 label_id 校验仍然通过（导出侧按四元组重算）
        assert rows[0].label_id == label_id_for(ARTICLE_RAW, "valid", "valid", "me")
        assert json.loads(reopened.dumps_json())["count"] == 1


# ===================================================================== #
# 判据 3：锚点约束 —— 响亮失败 + 活对照
# ===================================================================== #
def test_criterion_3_anchor_on_a_different_document_is_refused_loudly() -> None:
    """`anchor.raw_id != raw_id` ⇒ `AnchorError`（**同一条路径**对相等时必须成功）。"""
    stray = EvidenceAnchor.create(
        raw_id=FEED_RAW,
        raw_sha256=content_sha256(FEED_TEXT.encode("utf-8")),
        char_start=0,
        char_end=10,
    )
    with pytest.raises(AnchorError) as excinfo:
        ConfirmedLabel.human(
            raw_id=ARTICLE_RAW,
            label_key="valid",
            label_value="valid",
            actor="me",
            anchor=stray,
        )
    assert ARTICLE_RAW in str(excinfo.value) and FEED_RAW in str(excinfo.value)

    # ---- 活对照：同一调用路径、同一形状，只把 raw_id 换成相等的那个 → 必须成功 ----
    good = EvidenceAnchor.create(
        raw_id=ARTICLE_RAW,
        raw_sha256=content_sha256(ARTICLE_TEXT.encode("utf-8")),
        char_start=0,
        char_end=11,
    )
    label = ConfirmedLabel.human(
        raw_id=ARTICLE_RAW,
        label_key="valid",
        label_value="valid",
        actor="me",
        anchor=good,
    )
    assert label.anchor == good
    assert label.raw_id == ARTICLE_RAW
    assert label.label_id == label_id_for(ARTICLE_RAW, "valid", "valid", "me")


def test_criterion_3b_entry_level_label_lands_with_anchor(tmp_path: Path) -> None:
    """条目级标签真的带锚点落库，且能被**独立**读回。"""
    db = tmp_path / "entry-labels.db"
    anchor = EvidenceAnchor.create(
        raw_id=FEED_RAW,
        raw_sha256=content_sha256(FEED_TEXT.encode("utf-8")),
        char_start=100,
        char_end=200,
    )
    with open_store(db) as store:
        store.add(
            ConfirmedLabel.human(
                raw_id=FEED_RAW,
                label_key="industry",
                label_value="ai",
                actor="e2e-t109",
                anchor=anchor,
            )
        )
    with open_store(db) as reopened:
        rows = reopened.all_for(FEED_RAW)
        assert len(rows) == 1
        assert rows[0].anchor is not None
        assert (
            rows[0].anchor.raw_id,
            rows[0].anchor.raw_sha256,
            rows[0].anchor.char_start,
            rows[0].anchor.char_end,
        ) == (FEED_RAW, anchor.raw_sha256, 100, 200)


# ===================================================================== #
# 判据 4：容器 vs 条目（SPEC §6.3 裁决 B）
# ===================================================================== #
def test_criterion_4_container_is_expanded_and_never_listed_itself() -> None:
    result = _entry_result()
    kinds = [(item.raw_id, item.from_feed) for item in result.items]
    assert (FEED_RAW, False) not in kinds, "容器本身不得作为条目出现"
    assert kinds.count((FEED_RAW, True)) == 2, "容器的派生条目必须出现"
    assert (ARTICLE_RAW, False) in kinds, "本身即条目的 Raw 必须整篇出现"
    assert (EMPTY_RAW, False) in kinds, "没有内容的记录不得静默消失"
    assert result.total == 4
    assert all(isinstance(item, FeedEntry) for item in result.items)


def test_criterion_4b_entry_granularity_without_lookup_fails_loudly() -> None:
    """未注入 `entries_of` ⇒ **响亮失败**，绝不返回"一个条目都没有"的空结果。"""
    with pytest.raises(FeedQueryError) as excinfo:
        run_query(
            _source(),
            FeedQuery(granularity=ENTRY_GRANULARITY, limit=10),
        )
    assert "entries_of" in str(excinfo.value)
    # 活对照：同一 source 在**文档粒度**下正常返回（三条 Raw 都在）
    document = run_query(_source(), FeedQuery(limit=10))
    assert document.total == 3


def test_criterion_4c_document_granularity_is_still_the_default() -> None:
    """默认粒度不变 ⇒ 既有调用方不受影响。"""
    assert FeedQuery().granularity == DOCUMENT_GRANULARITY
    assert not FeedQuery().is_entry_mode
    result = run_query(_source(), FeedQuery(limit=10))
    assert result.total == 3
    assert all(item.raw_id in {FEED_RAW, ARTICLE_RAW, EMPTY_RAW} for item in result.items)


def test_criterion_4d_whole_document_entry_is_flagged_not_silent() -> None:
    """整篇条目必须带 `from_feed=False` 且区间是整篇；容器回落时**带问题说明**。"""
    result = _entry_result()
    article = next(item for item in result.items if item.raw_id == ARTICLE_RAW)
    assert article.from_feed is False
    assert article.entry_id is None, "整篇条目没有条目层 ID（不该编造一个）"
    assert (article.char_start, article.char_end) == (0, len(ARTICLE_TEXT))
    assert article.raw_sha256 == content_sha256(ARTICLE_TEXT.encode("utf-8"))
    assert article.problems == ()


def test_criterion_4e_container_without_parsed_entries_is_reported() -> None:
    """声明是容器却解析不出条目 ⇒ 回落，但理由必须出现在 `problems` 里。"""
    result = run_query(
        _source(),
        FeedQuery(granularity=ENTRY_GRANULARITY, limit=200),
        entries_of=lambda record: _View(entries=(), text_length=10),
    )
    fallback = [item for item in result.items if item.raw_id == FEED_RAW]
    assert len(fallback) == 1 and fallback[0].from_feed is False
    assert fallback[0].problems, "容器解析失败不得静默：必须有理由"


# ===================================================================== #
# 判据 5：锚点四元组
# ===================================================================== #
def test_criterion_5_every_entry_carries_the_full_anchor_tuple() -> None:
    result = _entry_result()
    by_raw = {record.raw_id: record for record in _records()}
    for item in result.items:
        assert item.raw_id and item.raw_sha256 and item.title
        assert item.raw_sha256 == by_raw[item.raw_id].content_sha256
        assert 0 <= item.char_start < item.char_end
        anchor = item.anchor_dict
        assert set(anchor) == {"raw_id", "raw_sha256", "char_start", "char_end"}
        # 构造出来的锚点是合法的（这就是前端的用法）
        built = EvidenceAnchor.create(**anchor)
        assert built.length == item.char_end - item.char_start
    derived = [item for item in result.items if item.from_feed]
    assert len(derived) == 2
    assert all(item.char_end <= len(FEED_TEXT) for item in derived)


def test_criterion_5b_entry_json_payload_has_the_anchor_tuple() -> None:
    payload = feed_payload(_entry_result())
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["filters"]["granularity"] == ENTRY_GRANULARITY
    for item in payload["items"]:
        assert {"raw_id", "raw_sha256", "char_start", "char_end"} <= set(item)
        assert {"entry_id", "title", "link", "published_at", "ordinal", "from_feed"} <= set(item)


# ===================================================================== #
# 判据 6：条目粒度的排序与分页
# ===================================================================== #
def test_criterion_6_entry_paging_is_complete_and_disjoint() -> None:
    everything = _entry_result()
    assert everything.total == 4
    collected: List[Tuple[str, int]] = []
    offset = 0
    while True:
        page = run_query(
            _source(),
            FeedQuery(granularity=ENTRY_GRANULARITY, limit=3, offset=offset),
            entries_of=_entries_of,
        )
        collected.extend((item.raw_id, item.char_start) for item in page.items)
        if not page.has_more or page.next_offset is None:
            break
        offset = page.next_offset
    expected = [(item.raw_id, item.char_start) for item in everything.items]
    assert collected == expected
    assert len(set(collected)) == len(collected)


def test_criterion_6b_entry_order_is_total_and_reproducible() -> None:
    ascending = _entry_result(order="asc")
    descending = _entry_result(order="desc")
    assert [i.raw_id for i in ascending.items] != [i.raw_id for i in descending.items]
    again = _entry_result(order="asc")
    assert [(i.raw_id, i.char_start) for i in ascending.items] == [
        (i.raw_id, i.char_start) for i in again.items
    ]
    # 同时间戳内次序恒为 (raw_id, char_start) 升序，与 order 无关
    same_time = [i for i in descending.items if i.timestamp == descending.items[0].timestamp]
    keys = [(i.raw_id, i.char_start) for i in same_time]
    assert keys == sorted(keys)


# ===================================================================== #
# 判据 7：契约版本与向后兼容
# ===================================================================== #
def test_criterion_7_contract_version_bumped_with_backward_compatible_documents() -> None:
    """条目粒度是新形状 ⇒ 升版；文档粒度的 `items` 字段集**逐字段不变**。"""
    assert CONTRACT_VERSION == 2, "新增条目粒度后对外 JSON 契约必须升版（SPEC §2.13）"
    payload = feed_payload(run_query(_source(), FeedQuery(limit=10)))
    assert payload["contract_version"] == 2
    assert set(payload) == {"contract_version", "items", "page", "sort", "filters"}
    assert set(payload["filters"]) == {
        "industries",
        "channels",
        "since",
        "until",
        "labels",
        "labeled",
    }, "文档粒度的 filters 不得多出键（granularity 只在条目粒度出现）"
    assert payload["sort"] == {
        "column": "fetched_at",
        "order": "desc",
        "tie_break": "raw_id asc",
    }
    expected_fields = {
        "raw_id",
        "channel_id",
        "industry",
        "endpoint",
        "content_sha256",
        "byte_length",
        "fetched_at",
        "http_status",
        "labels",
    }
    for item in payload["items"]:
        assert set(item) == expected_fields, "文档粒度的 items 形状必须逐字段不变"
    # 条目粒度的 sort 描述必须是另一套（时间列不同）
    entry_payload = feed_payload(_entry_result())
    assert entry_payload["sort"] == {
        "column": "timestamp",
        "order": "desc",
        "tie_break": "raw_id asc, char_start asc",
    }


# ===================================================================== #
# 判据 8：零新增 Python 依赖
# ===================================================================== #
def test_criterion_8_no_new_python_dependencies() -> None:
    """零新增依赖：`pyproject.toml` 声明的第三方包**一个都没变**（照 T-001 的 12 项基线）。

    比"grep 一下没有 flask"强的地方：这里把**声明集合**钉死，任何新增/删除都会失败。
    """
    declared = _declared_dependencies()
    assert declared == BASELINE_DEPENDENCIES, (
        "本任务不得改动依赖声明（SPEC §2.11 / CLAUDE.md 的依赖最小化）；"
        f"新增/删除：{sorted(declared ^ BASELINE_DEPENDENCIES)}"
    )
    # 本任务的全部 import 都来自 stdlib 与仓库内包：用 AST 检查被改动的文件
    allowed_roots = {
        "__future__",
        "argparse",
        "atlas",
        "dataclasses",
        "datetime",
        "hashlib",
        "html",
        "http",
        "json",
        "logging",
        "os",
        "pathlib",
        "re",
        "string",
        "sys",
        "threading",
        "typing",
        "urllib",
    }
    for relative in (
        "src/atlas/feed/query.py",
        "src/atlas/feed/http.py",
        "src/atlas/feed/repository.py",
        "src/atlas/webui/app.py",
        "src/atlas/webui/pages.py",
        "src/atlas/webapp.py",
    ):
        roots = _imported_roots(REPO_ROOT / relative)
        assert roots <= allowed_roots, (relative, sorted(roots - allowed_roots))


#: T-001 精简后的依赖声明（`pyproject.toml` 的 `dependencies` 列表逐项）。
#: 本任务是纯 stdlib 接线，因此这张表**必须**原样不变。
BASELINE_DEPENDENCIES = frozenset(
    {
        "requests>=2.31.0",
        "httpx>=0.25.0",
        "beautifulsoup4>=4.12.0",
        "lxml>=4.9.0",
        "feedparser>=6.0.0",
        "pydantic>=2.5.0",
        "pydantic-settings>=2.12.0",
        "pyyaml>=6.0.0",
        "python-dotenv>=1.0.0",
        "loguru>=0.7.0",
        "aiofiles>=23.0.0",
        "minio>=7.2.20",
    }
)


def _declared_dependencies() -> frozenset:
    """从 `pyproject.toml` 读出 `dependencies` 列表（不 import tomllib：3.11+ 才有的模块）。"""
    import re

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.DOTALL | re.MULTILINE)
    assert match is not None, "pyproject.toml 里找不到 dependencies 列表"
    items = re.findall(r'"([^"]+)"', match.group(1))
    return frozenset(item.strip() for item in items)


def _imported_modules(path: Path) -> set:
    """该文件 import 的**完整模块名**（相对 import 记作 `<relative>`）。"""
    modules = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add("<relative>" if node.level else (node.module or ""))
    return modules


def _imported_roots(path: Path) -> set:
    roots = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


# ===================================================================== #
# 判据 9：受保护模块未被进口新符号
# ===================================================================== #
#: 本任务**不得改动**的包（任务书明列）。它们可以出现在组合根的 import 里
#: —— 例如 SPEC §2.5 明确要求"行业配置 = feed 的筛选维度"由**组合根**从
#: `atlas.registry` 读出来注入；那是接线，不是改它们。
FORBIDDEN_MODULES = (
    "atlas.migrate",
    "atlas.search",
    "atlas.chunk",
    "atlas.compose",
    "atlas.archive.blobstore",
    "atlas.cognition",
)

#: 只有组合根允许 import 的模块（SPEC §2.5 的 C8 闭环接线点）。
COMPOSITION_ROOT_ONLY = ("atlas.registry",)


def test_criterion_9_protected_modules_are_not_imported_by_the_changed_packages() -> None:
    """`atlas.feed` / `atlas.webui` 不得 import 受保护模块（组合根例外，见下）。"""
    for relative in (
        "src/atlas/feed/query.py",
        "src/atlas/feed/http.py",
        "src/atlas/feed/repository.py",
        "src/atlas/webui/app.py",
        "src/atlas/webui/pages.py",
    ):
        path = REPO_ROOT / relative
        modules = _imported_modules(path)
        for forbidden in FORBIDDEN_MODULES + COMPOSITION_ROOT_ONLY:
            assert forbidden not in modules, (relative, forbidden)
    # `atlas.feed` / `atlas.webui` 的既有 AST 边界测试仍然有效（它们在原文件里）


def test_criterion_9a_composition_root_only_imports_the_c8_wiring() -> None:
    """组合根（`atlas/webapp.py`）只允许 import 受保护模块里的 `atlas.registry`。

    理由：SPEC §2.5 规定"行业配置 = AI 分类的标签空间 = feed 的筛选维度 = 打标时的
    修正对象"四者引用同一份配置，而 feed 刻意不 import registry ⇒ **必须**由组合根
    读出来注入。`atlas.compose` 就是这么做的（`industry_of = {c.id: c.industry_id ...}`）。
    """ 
    modules = _imported_modules(REPO_ROOT / "src" / "atlas" / "webapp.py")
    imported = {name for name in modules if name.startswith("atlas")}
    for forbidden in FORBIDDEN_MODULES:
        assert forbidden not in imported, forbidden
    assert "atlas.registry" in imported, "C8 闭环必须由组合根接上（SPEC §2.5）"
    # 组件的实现细节不许穿透：只用包的公开入口
    assert "atlas.archive.blobstore" not in imported


def test_criterion_9b_entry_layer_is_only_wired_by_the_composition_root() -> None:
    """`atlas.entries` 只允许被组合根（`atlas/webapp.py`）碰。

    `atlas.webui` 的 import 白名单只允许 `atlas` 根，`atlas.feed` 只允许
    `atlas.contracts` / `atlas.archive` —— 两处都有既有测试钉死；这里补一条正向断言：
    接线**确实**发生在组合根，而不是"谁都没接"。
    """
    composition = (REPO_ROOT / "src" / "atlas" / "webapp.py").read_text(encoding="utf-8")
    assert "from atlas.entries import" in composition
    assert "entries_lookup=" in composition
    assert "locate_entry=" in composition
    for relative in ("src/atlas/webui/app.py", "src/atlas/webui/pages.py"):
        imported = {
            name
            for name in _imported_modules(REPO_ROOT / relative)
            if name.startswith("atlas")
        }
        assert "atlas.entries" not in imported, f"{relative} 不得 import atlas.entries"
