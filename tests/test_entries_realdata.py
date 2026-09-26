"""T-130 真实数据流证据（硬规则 1）：对 `data/store` 里**真实归档**跑条目化。

数据从哪来（**只读**）
----------------------

直接读真实存储（SPEC §2.10 的目录布局）：

```
data/store/raw/<raw_id>/content.bin   原始字节（事实来源）
data/store/raw/<raw_id>/meta.json     元数据可读副本（channel_id / endpoint / sha256）
```

`data/` **不进 git**（SPEC §8.1），因此干净 worktree 里没有它 —— 真实数据用例
`skip`（照 `tests/test_search_realdata.py` 的先例），真实数字由主工作区那次运行给出。
`data/` 一个字节都不写：整树 sha256 前后比对钉死。

**实测规模（本文件每次运行都会打印）**：`data/store/raw` 里有 **75** 条归档记录，
其中 **10 条是 feed 归档**（SPEC §6.3 记录的那 10 份），另外 **65 条是**后续任务按篇
抓取归档的**单独文章页**（T-102/T-103 的产物，`content.bin` 是 HTML 正文）。
十份 feed 里只有 **8 份真的能拆出条目**：`ai-news-blog` 拿到的是 HTML 页面、
`hacker-news-frontpage` 拿到的是 JSON。因此本文件同时钉死两类行为：

| 输入类别 | 条数 | 本层行为 |
|---|---|---|
| 8 份**真 feed**（4× arXiv RSS + google-ai-blog Atom + synced-review / marktechpost / kdnuggets RSS） | 8 | 拆出 **830** 条条目（见下） |
| `ai-news-blog`（HTML 页面，HTTP 200） | 1 | `EntryParseError`（**响亮失败**） |
| `hacker-news-frontpage`（JSON API 响应） | 1 | `EntryParseError`（**响亮失败**） |
| 按篇归档的单独文章页（HTML 正文） | 65 | `EntryParseError`（**响亮失败**）—— 文章已经是 Raw，不需要再条目化 |

> ⚠️ 最后一行是本文件对**当前存储状态**的如实记录，不是 T-130 的设计目标：
> 这些文章页是"T-130 之后才出现"的输入类别（SPEC §6.3 写的是"10 条归档记录"）。
> T-130 的契约是"**feed** → 条目"，因此对它们响亮失败是**正确行为**，
> 不是缺陷；哪条记录是 feed 由**结构**判定（真实数据里没有 content-type 元数据）。

830 这个预期数字
----------------

SPEC §6.3 的表格逐渠道列出 `<item>`/`<entry>` 数：331 / 199 / 191 / 54 / 25 / 10 / 10 / 10
= **830**，另两渠道 0。本文件把**实测值逐渠道钉死**：数字变了就响亮失败，
逼操作者重新测量，而不是沿用陈旧结论。

**预期与实测的关系必须说清楚**：830 不是"随便数到 830 就通过"，而是
"哪八份 feed、每份几条"这个具体断言的合计；断言写成逐渠道相等 + 合计相等。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from atlas.entries import (
    EntryParseError,
    FeedKind,
    parse_entries,
    verify_ids,
    verify_offsets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_RAW = REPO_ROOT / "data" / "store" / "raw"

#: SPEC §6.3 记录的十份 feed：`endpoint → (channel_id, 形态, <item>/<entry> 数)`。
#: 这是"先定义后实现"的验收数字，实测不符即响亮失败。
#:
#: 用 `endpoint` 而不是 `raw_id` 作键：`raw_id` 是内容寻址的，内容一变它就变；
#: 而"哪个端点是一份 feed"是稳定的事实。
EXPECTED_FEEDS: dict[str, tuple[str, str, int]] = {
    "https://arxiv.org/rss/cs.LG": ("arxiv-machine-learning", FeedKind.RSS, 331),
    "https://arxiv.org/rss/cs.CV": ("arxiv-computer-vision", FeedKind.RSS, 199),
    "https://arxiv.org/rss/cs.CL": ("arxiv-natural-language", FeedKind.RSS, 191),
    "https://arxiv.org/rss/stat.ML": ("arxiv-statistical-learning", FeedKind.RSS, 54),
    "https://googleaiblog.blogspot.com/atom.xml": ("google-ai-blog", FeedKind.ATOM, 25),
    "https://syncedreview.com/feed": ("synced-review", FeedKind.RSS, 10),
    "https://www.marktechpost.com/feed": ("marktechpost", FeedKind.RSS, 10),
    "https://www.kdnuggets.com/feed": ("kdnuggets", FeedKind.RSS, 10),
}

#: 两份**不是 feed** 的真实输入：`endpoint → 错误信息里必须出现的关键词`。
EXPECTED_NON_FEEDS: dict[str, str] = {
    "https://hn.algolia.com/api/v1/search_by_date?tags=story": "JSON",
    "https://artificialintelligence-news.com/feed/": "HTML",
}

#: SPEC §6.3 的合计预期（`sum(count for _c, _k, count in EXPECTED_FEEDS.values())`）。
EXPECTED_TOTAL = 830

#: 实测：**拆得出条目**的 feed 记录数（8 —— 十份 feed 里 `ai-news-blog` 与
#: `hacker-news-frontpage` 根本不是 feed，见 `EXPECTED_NON_FEEDS`）。
MEASURED_ENTRY_BEARING_FEEDS = 8

#: 实测的存储规模（`data/store` 每次落盘都会变；这两个数字变了**不是失败**，
#: 但必须是"如实测到"的，因此打印出来并由专门的测试记录当前值）。
MEASURED_FEED_RECORDS = 10
MEASURED_TOTAL_RECORDS = 75


class Record(NamedTuple):
    raw_id: str
    channel_id: str
    endpoint: str
    content: bytes

    @property
    def is_feed(self) -> bool:
        return self.endpoint in EXPECTED_FEEDS


def _store_records() -> list[Record]:
    """全部真实归档记录（只读），按 raw_id 排序。"""
    records: list[Record] = []
    if not STORE_RAW.is_dir():
        return records
    for entry_dir in sorted(STORE_RAW.iterdir()):
        meta_path = entry_dir / "meta.json"
        content_path = entry_dir / "content.bin"
        if not entry_dir.is_dir() or not meta_path.is_file() or not content_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        records.append(
            Record(
                raw_id=meta["raw_id"],
                channel_id=meta["channel_id"],
                endpoint=meta["endpoint"],
                content=content_path.read_bytes(),
            )
        )
    return records


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


RECORDS = _store_records()
FEEDS = [record for record in RECORDS if record.is_feed]
NON_FEEDS = [record for record in RECORDS if not record.is_feed]

requires_real_data = pytest.mark.skipif(
    len(RECORDS) < MEASURED_FEED_RECORDS,
    reason="本地真实归档存储缺失或不足 10 条（data/ 不进 git，见 SPEC §8.1）",
)

# =========================================================================== #
# 存储现状（先钉死"数据还在"）
# =========================================================================== #


@requires_real_data
def test_real_store_shape_is_reported() -> None:
    """先如实报出存储规模与两类输入的构成（数字变了要看得见，而不是悄悄漂移）。"""
    print(
        f"\n[T-130 真实] data/store/raw 归档记录 {len(RECORDS)} 条："
        f"feed {len(FEEDS)} 条 + 非 feed {len(NON_FEEDS)} 条"
        f"（按篇归档的文章页 = 非 feed 的一部分）"
    )
    by_channel: dict[str, int] = {}
    for record in RECORDS:
        by_channel[record.channel_id] = by_channel.get(record.channel_id, 0) + 1
    for channel in sorted(by_channel):
        print(f"[T-130 真实]   渠道 {channel:28s} {by_channel[channel]:>3} 条归档")

    assert len(FEEDS) == MEASURED_ENTRY_BEARING_FEEDS, (
        f"拆得出条目的 feed 记录数变了：实测 {len(FEEDS)}，"
        f"记录值 {MEASURED_ENTRY_BEARING_FEEDS}；"
        f"端点={sorted(record.endpoint for record in FEEDS)}"
    )
    assert {record.endpoint for record in FEEDS} == set(EXPECTED_FEEDS)
    # 分区必须完备且互斥：feed 端点在 EXPECTED_FEEDS 里的记录进 FEEDS，
    # 其余全部进 NON_FEEDS —— 不允许有记录既不是 feed 也没被算进非 feed。
    assert len(FEEDS) + len(NON_FEEDS) == len(RECORDS)
    assert {record.raw_id for record in FEEDS}.isdisjoint(
        {record.raw_id for record in NON_FEEDS}
    )
    assert {record.endpoint for record in NON_FEEDS} >= set(EXPECTED_NON_FEEDS)
    assert len(RECORDS) >= MEASURED_TOTAL_RECORDS, (
        f"归档记录总数比实测过的 {MEASURED_TOTAL_RECORDS} 还少：{len(RECORDS)}"
    )
    for record in RECORDS:
        assert record.raw_id.startswith("raw_") and record.content


# =========================================================================== #
# 核心实测：830
# =========================================================================== #


@requires_real_data
def test_real_feeds_are_tokenized_with_the_expected_counts() -> None:
    """**核心实测**：逐渠道条目数 + 合计 830 + 全条目回环校验 + ID 独立重算。"""
    before = _tree_digest(STORE_RAW)

    per_channel: dict[str, int] = {}
    kinds: dict[str, str] = {}
    span_failures: list[str] = []
    id_failures: list[str] = []
    lines: list[str] = []

    for record in FEEDS:
        channel, expected_kind, _expected_count = EXPECTED_FEEDS[record.endpoint]
        entrieset = parse_entries(record.content, "", raw_id=record.raw_id)

        assert entrieset.kind == expected_kind, (channel, entrieset.kind)
        assert entrieset.feed_text, channel
        assert entrieset.problems == (), (
            f"{channel} 报了 {len(entrieset.problems)} 个问题：{entrieset.problems[:3]}"
        )
        per_channel[channel] = entrieset.entry_count
        kinds[channel] = entrieset.kind

        span_report = verify_offsets(
            raw_id=record.raw_id, raw_bytes=record.content, entries=entrieset
        )
        if not span_report.ok:
            span_failures.extend(f"{channel}: {item}" for item in span_report.failures)
        id_report = verify_ids(
            raw_id=record.raw_id, raw_bytes=record.content, entries=entrieset
        )
        if not id_report.ok:
            id_failures.extend(f"{channel}: {item}" for item in id_report.mismatches)

        lines.append(
            f"[T-130 真实] {channel:28s} {entrieset.kind:5s} {entrieset.encoding:8s} "
            f"{entrieset.entry_count:4d} 条  bytes={len(record.content):>8}  "
            f"span_ok={span_report.ok} ({span_report.passed}/{span_report.checked})  "
            f"ids_ok={id_report.ok}  problems={len(entrieset.problems)}"
        )
        assert all(entry.title.strip() for entry in entrieset.entries), channel
        assert all(entry.link.startswith("http") for entry in entrieset.entries), channel
        assert all(entry.published_at for entry in entrieset.entries), channel

    print("\n" + "\n".join(lines))

    total = sum(per_channel.values())
    checked = sum(1 for _ in FEEDS)
    print(
        f"[T-130 真实] 合计：{checked} 份 feed 拆出 **{total}** 条条目"
        f"（SPEC §6.3 预期 {EXPECTED_TOTAL}，差 {total - EXPECTED_TOTAL}）"
    )
    print(
        f"[T-130 真实] 回环校验：{total - len(span_failures)}/{total} 条通过，"
        f"失败 {len(span_failures)}；ID 独立重算失败 {len(id_failures)}"
    )

    expected_counts = {
        channel: count for channel, _kind, count in EXPECTED_FEEDS.values()
    }
    assert per_channel == expected_counts, (
        "真实 feed 的条目数变了："
        f"实测 {per_channel}，记录值 {expected_counts}。"
        "请重新测量并更新 EXPECTED_FEEDS / EXPECTED_TOTAL 与交付报告，不要沿用陈旧数字"
    )
    assert total == EXPECTED_TOTAL == sum(expected_counts.values())
    assert span_failures == [], f"真实数据的区间回环校验失败：{span_failures[:5]}"
    assert id_failures == [], f"真实数据的 ID 独立重算失败：{id_failures[:5]}"
    assert set(kinds.values()) == {FeedKind.RSS, FeedKind.ATOM}, kinds

    # 真实数据一个字节都没被改动（判据 A3）
    assert _tree_digest(STORE_RAW) == before, "读取真实数据时改动了 data/store"


@requires_real_data
def test_real_sample_titles_links_and_slices_are_shown_and_consistent() -> None:
    """至少展示几个**真实条目**的标题与链接，并与原文切片对照。

    对照方式是判据 3 的同一判据：切片去标记后必须包含标题；链接必须能在切片里
    逐字符找到。这些断言在**真实数据**上跑，不只是合成夹具。
    """
    shown = 0
    for record in FEEDS:
        channel, _kind, _count = EXPECTED_FEEDS[record.endpoint]
        entrieset = parse_entries(record.content, "", raw_id=record.raw_id)
        for entry in entrieset.entries[:2]:
            slice_text = entry.raw_slice(entrieset.feed_text)
            plain = entry.content_slice(entrieset.feed_text)
            collapsed = " ".join(plain.split())
            title = " ".join(entry.title.split())
            assert title in collapsed, (channel, entry.title, collapsed[:200])
            assert entry.link in slice_text, (channel, entry.title, entry.link)
            assert slice_text.startswith("<")
            assert 0 <= entry.char_start < entry.char_end <= len(entrieset.feed_text)
            print(
                f"[T-130 真实] {channel} entry[{entry.index}] "
                f"span=[{entry.char_start},{entry.char_end}) "
                f"title={entry.title[:70]!r}\n"
                f"             link={entry.link}\n"
                f"             published_at={entry.published_at} id={entry.entry_id}"
            )
            shown += 1
    assert shown >= 16, f"实际只展示了 {shown} 条真实条目"


@requires_real_data
def test_real_entry_ids_are_unique_across_all_feeds() -> None:
    """条目 ID 必须**全局唯一**：跨 feed 也不能撞（ID 里含 raw_id 与 raw_sha256）。"""
    seen: dict[str, str] = {}
    total = 0
    for record in FEEDS:
        channel, _kind, _count = EXPECTED_FEEDS[record.endpoint]
        entrieset = parse_entries(record.content, "", raw_id=record.raw_id)
        local: set[str] = set()
        for entry in entrieset.entries:
            assert entry.entry_id not in local, f"{channel} 内部条目 ID 重复"
            local.add(entry.entry_id)
            assert entry.entry_id not in seen, (
                f"{channel} 与 {seen.get(entry.entry_id)} 的条目 ID 撞了：{entry.entry_id}"
            )
            seen[entry.entry_id] = channel
        total += entrieset.entry_count
    assert total == len(seen) == EXPECTED_TOTAL
    print(f"\n[T-130 真实] {EXPECTED_TOTAL} 个条目 ID 跨 {len(FEEDS)} 份 feed 全局唯一")


@requires_real_data
def test_real_feeds_rebuild_byte_identically() -> None:
    """可重建（真实数据）：丢掉产物重跑，条目序列与 ID 逐字段相同。"""
    for record in FEEDS:
        channel, _kind, _count = EXPECTED_FEEDS[record.endpoint]
        first = parse_entries(record.content, "", raw_id=record.raw_id)
        second = parse_entries(record.content, "", raw_id=record.raw_id)
        assert first == second, f"{channel} 重跑结果不一致"
        assert [e.entry_id for e in first.entries] == [e.entry_id for e in second.entries]
        assert first.parser_fingerprint == second.parser_fingerprint
    print(f"\n[T-130 真实] {len(FEEDS)} 份 feed 重跑：产物与 ID 逐字段相同")


@requires_real_data
def test_real_spans_are_monotonic_and_cover_each_item_once() -> None:
    """真实区间必须严格单调、不重叠；条目数 == 原文里 `<item>`/`<entry>` 的出现次数。"""
    for record in FEEDS:
        channel, kind, count = EXPECTED_FEEDS[record.endpoint]
        entrieset = parse_entries(record.content, "", raw_id=record.raw_id)
        text = entrieset.feed_text
        previous_end = 0
        for entry in entrieset.entries:
            assert entry.char_start >= previous_end, (channel, entry.index)
            assert entry.raw_slice(text).startswith("<"), (channel, entry.index)
            previous_end = entry.char_end
        marker = "<item" if kind == FeedKind.RSS else "<entry"
        assert entrieset.entry_count == count == text.count(marker), (
            f"{channel}: 条目数 {entrieset.entry_count} != 记录值 {count} != "
            f"原文里 {marker} 的计数 {text.count(marker)}"
        )


# =========================================================================== #
# 非 feed / 零条目输入的真实行为（判据 5 的真实数据版）
# =========================================================================== #


@requires_real_data
def test_real_hacker_news_json_is_refused_loudly() -> None:
    """真实 **JSON API 响应**：`EntryParseError`，且错误信息里点明这是 JSON。"""
    record = next(
        item
        for item in RECORDS
        if item.endpoint == "https://hn.algolia.com/api/v1/search_by_date?tags=story"
    )
    assert record.content.lstrip().startswith(b"{")
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(record.content, "", raw_id=record.raw_id)
    message = str(excinfo.value)
    assert "JSON" in message and "json_api" in message
    print(f"\n[T-130 真实] hacker-news-frontpage：0 条 + 明确原因 -> {message[:130]}")
    # 活对照：同一次运行里的真实 feed 成功解析
    control = next(item for item in FEEDS if item.channel_id == "kdnuggets")
    assert parse_entries(control.content, "", raw_id=control.raw_id).entry_count == 10


@requires_real_data
def test_real_ai_news_html_page_is_refused_loudly() -> None:
    """真实 **HTML 页面（HTTP 200）**：`EntryParseError`，且错误信息里点明这是 HTML。"""
    record = next(
        item
        for item in RECORDS
        if item.endpoint == "https://artificialintelligence-news.com/feed/"
    )
    assert record.content.lstrip().lower().startswith(b"<!doctype html")
    with pytest.raises(EntryParseError) as excinfo:
        parse_entries(record.content, "", raw_id=record.raw_id)
    message = str(excinfo.value)
    assert "HTML" in message
    print(f"\n[T-130 真实] ai-news-blog：0 条 + 明确原因 -> {message[:130]}")
    control = next(item for item in FEEDS if item.channel_id == "synced-review")
    assert parse_entries(control.content, "", raw_id=control.raw_id).entry_count == 10


@requires_real_data
def test_real_article_shaped_records_are_refused_loudly() -> None:
    """**按篇归档的单独文章页**（当前存储里 65 条）：不是 feed → 响亮失败。

    这是本文件对当前存储状态的**如实记录**：这些记录是"T-130 之后才出现"的输入
    类别（SPEC §6.3 写的是 10 条归档记录）。T-130 的契约是"**feed** → 条目"，
    文章页已经是一篇 Raw，再条目化没有意义 —— 因此这里要求**响亮失败**，
    而不是返回 0 条"看起来成功"。

    断言不写死 65：只要求"非 feed 记录全部响亮失败"，因此存储继续增长时这条
    测试不会虚假失败；但**每一条**都必须失败，且理由必须说清是 HTML。
    """
    article_shaped = [record for record in NON_FEEDS if record.endpoint not in EXPECTED_NON_FEEDS]
    assert article_shaped, "当前存储里应当有按篇归档的文章页；若已清空请更新本测试"
    failures: list[str] = []
    for record in article_shaped:
        try:
            entrieset = parse_entries(record.content, "", raw_id=record.raw_id)
        except EntryParseError as exc:
            if "HTML" not in str(exc) and "良构" not in str(exc):
                failures.append(f"{record.endpoint}: 理由不清（{str(exc)[:80]}）")
            continue
        failures.append(
            f"{record.endpoint}: 竟然成功返回 {entrieset.entry_count} 条"
            f"（kind={entrieset.kind}）"
        )
    assert failures == [], f"{len(failures)} 条文章页没有响亮失败：{failures[:3]}"
    print(
        f"\n[T-130 真实] 按篇归档的文章页 {len(article_shaped)} 条：全部响亮失败"
        f"（不是 feed，不需要再条目化）"
    )


@requires_real_data
def test_real_data_is_read_only_after_the_whole_file_ran() -> None:
    """整文件跑完后，`data/store/raw` 树的 sha256 必须与开始时相同（只读纪律）。"""
    before = _tree_digest(STORE_RAW)
    for record in RECORDS:
        if record.is_feed:
            parse_entries(record.content, "", raw_id=record.raw_id)
            continue
        with pytest.raises(EntryParseError):
            parse_entries(record.content, "", raw_id=record.raw_id)
    assert _tree_digest(STORE_RAW) == before
