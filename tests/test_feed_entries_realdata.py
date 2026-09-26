"""T-109 补完的**真实数据流证据**（硬规则 1）：真实 `data/store` 上的条目数与容器口径。

只读，**一个字节都不写**（整树 sha256 前后比对钉死）。`data/` 不进 git，
因此干净 worktree 里没有它 —— 用例**自跳过**（照 `tests/test_search_realdata.py` 的先例），
真实数字由主工作区那次运行给出。

实测数字（SPEC §6.3 的容器/条目口径，本文件逐项钉死）
----------------------------------------------------

| 项 | 实测 | 说明 |
|---|---|---|
| 归档记录 | **75** | `data/store/raw` 下的目录数与 `raw_records` 行数 |
| **容器**（feed-Raw） | **8** | `endpoint` 是 feed 且内容真能解析出条目 |
| 属于容器的**派生条目** | **830** | 4×arXiv(331/199/191/54) + google-ai-blog(25) + synced-review/marktechpost/kdnuggets(各 10) |
| **本身即条目**的 Raw（article-Raw） | **65** | T-131 导入的按篇文章 + 1 条 JSON 响应 + 1 条 HTML 页面 |
| **可浏览条目总数** | **895** | 830 派生 + 65 直接（= §6.3 的结论） |

> ⚠️ 与 §6.3 表格的一处**如实修正**：§6.3 说"8 条 feed-Raw"与"65 条 article-Raw"，
> 合计 73，而归档实际是 **75** 条。差的两条是 `hacker-news-frontpage`（JSON API 响应）
> 与 `ai-news-blog`（HTML 页面）—— 它们既不是容器、也没有可解析内容，
> 在条目列表里**仍然作为整篇条目出现**（不静默消失）。
> 因此"本身即条目"的实测是 **67** 条（65 篇导入文章 + 那两条），
> 条目总数 = 830 + 67 = **897**，而**不是** 895。
> 这个差异是**真实测量**的结果，不是笔误：本文件把实测值钉死，
> 并把"哪两条是多的、为什么"写清楚，避免沿用陈旧结论。

真实前端的一次完整闭环（`GET /feed` → `POST /label` → 独立 sqlite 复核）在
`tools/t109_entries_e2e.py` 里（会**真的写一条标签**，因此不入 pytest：
pytest 不得往真实库里写人工标签）。本文件只做**只读**测量。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from atlas.archive import open_archive
from atlas.feed import ENTRY_GRANULARITY, FeedQuery, run_query
from atlas.webapp import ArchiveEntriesAccess, build_webapp

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = REPO_ROOT / "data" / "store"

#: SPEC §6.3 逐渠道列出的 `<item>`/`<entry>` 数（用 endpoint 作键：raw_id 是内容寻址的，
#: 内容一变它就变；而"哪个端点是一份 feed"是稳定事实）。
EXPECTED_CONTAINER_FEEDS: Dict[str, int] = {
    "https://arxiv.org/rss/cs.LG": 331,
    "https://arxiv.org/rss/cs.CV": 199,
    "https://arxiv.org/rss/cs.CL": 191,
    "https://arxiv.org/rss/stat.ML": 54,
    "https://googleaiblog.blogspot.com/atom.xml": 25,
    "https://syncedreview.com/feed": 10,
    "https://www.marktechpost.com/feed": 10,
    "https://www.kdnuggets.com/feed": 10,
}

#: §6.3 的合计预期。
EXPECTED_DERIVED_ENTRIES = 830

#: 两份**不是 feed** 的真实输入（既不是容器、也没有可解析内容）。
#: 它们在条目列表里作为"整篇条目"出现 —— 必须被算进去，否则总数会少 2。
EXPECTED_NON_FEED_ENDPOINTS = (
    "https://hn.algolia.com/api/v1/search_by_date?tags=story",
    "https://artificialintelligence-news.com/feed/",
)

MEASURED_TOTAL_RECORDS = 75
MEASURED_CONTAINERS = 8
MEASURED_DIRECT_ENTRIES = 67  # 65 篇导入文章 + 上面那两条非 feed
MEASURED_TOTAL_ENTRIES = 897  # 830 + 67（**不是** §6.3 写的 895，见模块 docstring）


def _has_real_store() -> bool:
    return (STORE_ROOT / "atlas.db").is_file() and (STORE_ROOT / "raw").is_dir()


pytestmark = pytest.mark.skipif(
    not _has_real_store(),
    reason="本地真实归档存储缺失（data/ 不进 git，见 SPEC §8.1）",
)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


@pytest.fixture()
def archive():
    instance = open_archive(STORE_ROOT)
    try:
        yield instance
    finally:
        instance.close()


def _measure() -> Tuple[Dict[str, int], List[str], int, int]:
    """只读测量：`endpoint -> 派生条目数`、非 feed endpoint 列表、容器数、总数。"""
    archive = open_archive(STORE_ROOT)
    try:
        access = ArchiveEntriesAccess(archive)
        per_endpoint: Dict[str, int] = {}
        non_feed: List[str] = []
        derived = 0
        for raw_id in archive.all_raw_ids():
            record = archive.get(raw_id)
            view = access.entries_of(record)
            if view.entries:
                per_endpoint[record.endpoint] = len(view.entries)
                derived += len(view.entries)
            else:
                non_feed.append(record.endpoint)
        records = len(archive.all_raw_ids())
        return per_endpoint, non_feed, derived, records
    finally:
        archive.close()


# ===================================================================== #
# 判据 10：容器 / 条目口径的真实数字
# ===================================================================== #
def test_real_containers_and_entry_counts() -> None:
    raw_root = STORE_ROOT / "raw"
    before = _tree_digest(raw_root)

    per_endpoint, non_feed, derived, records = _measure()

    print(f"\n[T-109 真实] 归档记录 {records} 条")
    print(f"[T-109 真实] 容器（能拆出条目的 feed）{len(per_endpoint)} 份")
    for endpoint, count in sorted(per_endpoint.items(), key=lambda kv: -kv[1]):
        print(f"[T-109 真实]   {count:>4} 条  {endpoint}")
    print(f"[T-109 真实] 派生条目合计 {derived}")
    print(f"[T-109 真实] 本身即条目（整篇）{records - len(per_endpoint)} 条")
    print(f"[T-109 真实] 可浏览条目总数 {derived + (records - len(per_endpoint))}")

    assert records == MEASURED_TOTAL_RECORDS, (
        f"归档记录数变了：实测 {records}，记录值 {MEASURED_TOTAL_RECORDS}；"
        "请重新测量并更新本文件与交付报告"
    )
    assert per_endpoint == EXPECTED_CONTAINER_FEEDS, (
        f"容器的派生条目数变了：实测 {per_endpoint}"
    )
    assert derived == EXPECTED_DERIVED_ENTRIES == 830
    assert len(per_endpoint) == MEASURED_CONTAINERS == 8
    assert records - len(per_endpoint) == MEASURED_DIRECT_ENTRIES == 67
    assert derived + (records - len(per_endpoint)) == MEASURED_TOTAL_ENTRIES == 897

    # 两份"不是 feed"的真实输入必须被算进"整篇条目"，而不是消失
    for endpoint in EXPECTED_NON_FEED_ENDPOINTS:
        assert endpoint in non_feed, f"{endpoint} 不在整篇条目里（条目消失=静默丢失）"
    assert len(non_feed) == MEASURED_DIRECT_ENTRIES

    # 只读纪律：一个字节都没动
    assert _tree_digest(raw_root) == before


def test_real_entry_query_over_the_whole_store(archive) -> None:
    """用**真正的条目粒度查询**跑一遍真实 store，并把两边数字对上。"""
    access = ArchiveEntriesAccess(archive)
    kinds = {raw_id: True for raw_id in access.container_ids()}
    assert len(kinds) == MEASURED_CONTAINERS

    source = _source(archive, kinds)
    result = run_query(
        source,
        FeedQuery(granularity=ENTRY_GRANULARITY, limit=200),
        entries_of=access.entries_of,
    )
    assert result.total == MEASURED_TOTAL_ENTRIES, (
        f"条目粒度查询总数 {result.total} != 实测 {MEASURED_TOTAL_ENTRIES}"
    )
    # 分页不重不漏：逐页拉完，与 total 对得上
    seen: List[Tuple[str, int]] = []
    units: List[Any] = []
    offset = 0
    while True:
        page = run_query(
            source,
            FeedQuery(granularity=ENTRY_GRANULARITY, limit=200, offset=offset),
            entries_of=access.entries_of,
        )
        seen.extend((item.raw_id, item.char_start) for item in page.items)
        units.extend(page.items)
        if not page.has_more or page.next_offset is None:
            break
        offset = page.next_offset
    assert len(seen) == result.total == len(set(seen))

    # 容器**本身**不得作为条目出现。
    # ⚠️ 注意区分：容器的派生条目**本来就**带着容器的 raw_id（锚点必须锚在那份原文上，
    # 见 SPEC §2.2），所以判据是"没有 `from_feed=False` 的单元挂在容器 raw_id 下"，
    # 而不是"列表里不出现容器的 raw_id"。
    container_ids = set(kinds)
    whole_document_containers = [
        (unit.raw_id, unit.char_start)
        for unit in units
        if not unit.from_feed and unit.raw_id in container_ids
    ]
    assert whole_document_containers == [], "容器本身不得作为整篇条目出现"
    # 而它的派生条目必须出现（且带着容器的 raw_id）
    assert sum(1 for raw_id, _ in seen if raw_id in container_ids) == EXPECTED_DERIVED_ENTRIES
    assert sum(1 for unit in units if unit.from_feed) == EXPECTED_DERIVED_ENTRIES
    assert sum(1 for unit in units if not unit.from_feed) == MEASURED_DIRECT_ENTRIES


def _source(archive, kinds: Dict[str, bool]):
    from atlas.feed import ArchiveFeedSource

    return ArchiveFeedSource(
        archive,
        industry_of={},
        entry_kind_of=lambda raw_id: "feed" if raw_id in kinds else None,
    )


def test_real_first_entries_have_readable_titles_and_anchors() -> None:
    """至少展示几条**真实条目**的标题/链接/区间，并断言锚点四元组自洽。

    这是"前端能看到什么"的真实数据版：标题来自条目（不是 raw_id），
    链接是真实 URL，区间落在原文里。
    """
    archive = open_archive(STORE_ROOT)
    try:
        access = ArchiveEntriesAccess(archive)
        kinds = {raw_id: True for raw_id in access.container_ids()}
        result = run_query(
            _source(archive, kinds),
            FeedQuery(granularity=ENTRY_GRANULARITY, limit=5),
            entries_of=access.entries_of,
        )
        assert len(result.items) == 5
        for item in result.items:
            assert item.title.strip() and item.title != item.raw_id
            assert item.raw_sha256 and len(item.raw_sha256) == 64
            assert 0 <= item.char_start < item.char_end
            if item.from_feed:
                assert item.link.startswith("http")
                assert item.published_at, "容器派生条目必须有发布时间"
            print(
                f"[T-109 真实] {item.timestamp.date()} | {item.title[:80]}\n"
                f"             {item.link}\n"
                f"             raw={item.raw_id} span=[{item.char_start},{item.char_end}) "
                f"from_feed={item.from_feed}"
            )
    finally:
        archive.close()


def test_real_frontend_serves_entry_html_from_the_real_store(tmp_path: Path) -> None:
    """**真实前端**（`build_webapp`，端口 0）的 `GET /feed` 必须是 200 且含真实标题。

    这是硬规则 1 的判据：不是"有文件/有字段"，而是真的从 `data/store` 渲染出页面。
    标签库写到 `tmp_path`（**绝不碰真实库**），因此本用例可以在 pytest 里跑。
    """
    import re
    import urllib.request

    app = build_webapp(
        STORE_ROOT,
        db_path=tmp_path / "labels.db",
        industry_provider=lambda: (),
        industry_of={},
        actor="pytest-readonly",
    )
    try:
        with urllib.request.urlopen(app.base_url + "/feed?limit=5", timeout=120) as response:
            status = response.status
            page = response.read().decode("utf-8")
        assert status == 200
        assert "Atlas Feed" in page

        titles = re.findall(
            r'<a class="title" href="([^"]*)"[^>]*>(.*?)</a>', page, re.DOTALL
        )
        assert titles, "条目必须有标题链接（不是只有 raw_id）"
        links = [link for link, _ in titles]
        assert all(link.startswith("http") or link == "#" for link in links)
        # 标题是**真实标题**，不是 raw_id
        for _link, title in titles:
            assert title.strip()
            assert not title.startswith("raw_")
        assert any(" " in title for _link, title in titles), (
            f"真实条目的标题应当是可读文字：{titles[:3]}"
        )
        print(f"\n[T-109 真实] GET /feed 渲染出 {len(titles)} 条条目，标题示例：")
        for link, title in titles[:3]:
            print(f"[T-109 真实]   {title[:90]}\n[T-109 真实]     {link}")

        # 锚点四元组的可见痕迹（前端要靠它构造锚点）
        assert "char_start=" in page and "char_end=" in page
        assert "raw_sha256=" in page
    finally:
        app.close()
