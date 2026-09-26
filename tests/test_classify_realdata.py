"""T-105 判据 9 / 11 / 12：**真实 `data/store` 上的分流与提议**（硬规则 1）。

判据全文（**先定义后实现**的唯一出处）见 `tests/_t105_criteria.py`。本文件覆盖：

- **判据 12**：真实数据流证据。默认解析 **65 篇文章** 与 **8 份 feed**（→ **830 条目**），
  合计 **895 个分类单元**（与 SPEC §6.3 / §6.3 与 T-130 的实测数字一致），
  并在真实 store 上跑通"单元 → 端口 → 归属 → `proposed_claims` → 独立读回"。
- **判据 9**：分流三选一在这 75 条真实 raw 上逐条成立
  （8 feed / 65 文章 / 2 跳过），跳过理由码落在闭集里。
- **判据 11**：`data/store/raw` 树在整文件运行前后 **sha256 相同**（只读纪律）。
- **判据 13**：降级路径的真实触发（见 `tests/test_classify_realdata.py` 的
  `test_degradation_*`；它指向**不可达端点**，不需要真实模型）。

**为什么默认 skip**
=================

三个前置条件都不是本提交的属性：`data/` 不进 git（SPEC §8.1）、边车依赖需要 `npm install`
（外网）、凭据在 `.env.local`（gitignored）。照 `tests/test_search_realdata.py` 的先例：

- 没有 `data/store/raw` ⇒ **skip**；
- 没有边车依赖 / 凭据 ⇒ **skip**；
- 真实模型默认不调用（消耗额度、受远端状态影响）⇒ 由
  `ATLAS_COGNITION_LIVE_TESTS=1` 显式开启。

在**干净 worktree** 里本文件因此整体 skip，退出码仍为 **0**；真实数字由主工作区那次
运行给出（本任务的交付报告里逐项列出）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

import pytest

from atlas.cognition import (
    CallStatus,
    CognitionConfig,
    DocumentKind,
    LabelSpace,
    PiSidecarCognitionPort,
    ProposalPolicy,
    SkipReason,
    SqliteProposedStore,
    classify_document,
    propose_documents,
)
from tests._t105_criteria import CRITERIA

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_RAW = REPO_ROOT / "data" / "store" / "raw"
SIDECAR_DIR = REPO_ROOT / "src" / "atlas" / "cognition" / "sidecar"

#: SPEC §6.3 / T-130 实测的十份 feed（`endpoint` 作键：内容寻址的 raw_id 会变）。
EXPECTED_FEEDS: Dict[str, int] = {
    "https://arxiv.org/rss/cs.LG": 331,
    "https://arxiv.org/rss/cs.CV": 199,
    "https://arxiv.org/rss/cs.CL": 191,
    "https://arxiv.org/rss/stat.ML": 54,
    "https://googleaiblog.blogspot.com/atom.xml": 25,
    "https://syncedreview.com/feed": 10,
    "https://www.marktechpost.com/feed": 10,
    "https://www.kdnuggets.com/feed": 10,
}

#: **不是** feed 的两条真实输入（跳过的两条）与它们该得到的理由码。
EXPECTED_SKIPS: Dict[str, str] = {
    "https://hn.algolia.com/api/v1/search_by_date?tags=story": SkipReason.UNSUPPORTED_CONTENT_JSON.value,
    "https://artificialintelligence-news.com/feed/": SkipReason.UNSUPPORTED_CONTENT_HTML.value,
}

EXPECTED_FEED_RECORDS = 8
EXPECTED_SKIPPED_RECORDS = 2
EXPECTED_TOTAL_RECORDS = 75
EXPECTED_ENTRIES = 830
EXPECTED_ARTICLE_UNITS = 65
EXPECTED_UNITS = EXPECTED_ENTRIES + EXPECTED_ARTICLE_UNITS

LIVE_ENV_VAR = "ATLAS_COGNITION_LIVE_TESTS"


class Record(NamedTuple):
    raw_id: str
    channel_id: str
    endpoint: str
    content: bytes


def _store_records() -> List[Record]:
    """真实归档记录（**只读**），按 raw_id 排序。"""
    records: List[Record] = []
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
FEEDS = [record for record in RECORDS if record.endpoint in EXPECTED_FEEDS]

requires_real_data = pytest.mark.skipif(
    len(RECORDS) < EXPECTED_TOTAL_RECORDS,
    reason="本地真实归档存储缺失或不足（data/ 不进 git，见 SPEC §8.1）",
)


def _require_live() -> None:
    if os.environ.get(LIVE_ENV_VAR) != "1":
        pytest.skip(
            f"真实模型调用默认不跑（消耗额度且受远端状态影响）；设 {LIVE_ENV_VAR}=1 显式开启"
        )
    if not (SIDECAR_DIR / "node_modules").is_dir():
        pytest.skip("边车依赖未安装（需要 npm install）")
    from atlas.cognition import available_routes

    if not available_routes().get("deepseek", {}).get("credential_available"):
        pytest.skip("DEEPSEEK_API_KEY 不可用")


# =========================================================================== #
# 判据 9 / 12：真实数据上的分流（不调用模型，因此门禁里也跑）
# =========================================================================== #


@requires_real_data
def test_real_dispatch_splits_all_75_records_into_exactly_three_kinds() -> None:
    """**真实分流**：75 条 raw → 8 feed / 65 文章 / 2 跳过，逐条给理由。"""
    before = _tree_digest(STORE_RAW)
    kinds: Dict[str, int] = {}
    per_feed: Dict[str, int] = {}
    skipped: Dict[str, str] = {}
    entries_total = 0
    article_units = 0
    lines: List[str] = []

    for record in RECORDS:
        plan = classify_document(
            record.content, raw_id=record.raw_id, endpoint=record.endpoint
        )
        kinds[plan.kind.value] = kinds.get(plan.kind.value, 0) + 1
        if plan.kind is DocumentKind.FEED:
            per_feed[record.endpoint] = plan.unit_count
            entries_total += plan.unit_count
            lines.append(
                f"[T-105 真实] FEED   {record.channel_id:26s} {record.endpoint[:52]:52s} "
                f"→ {plan.unit_count:4d} 条目"
            )
        elif plan.kind is DocumentKind.ARTICLE:
            article_units += plan.unit_count
        else:
            skipped[record.endpoint] = plan.skip_reason.value
            lines.append(
                f"[T-105 真实] SKIP   {record.channel_id:26s} {record.endpoint[:52]:52s} "
                f"→ {plan.skip_reason.value}"
            )

    print("\n" + "\n".join(sorted(lines)))
    print(
        f"[T-105 真实] 分流合计：feed {kinds.get('feed', 0)} 条（{entries_total} 条目）"
        f" + article {kinds.get('article', 0)} 条（{article_units} 单元）"
        f" + skipped {kinds.get('skipped', 0)} 条"
        f" ⇒ **{entries_total + article_units} 个分类单元**"
    )

    assert kinds.get("feed", 0) == EXPECTED_FEED_RECORDS
    assert kinds.get("article", 0) == EXPECTED_ARTICLE_UNITS
    assert kinds.get("skipped", 0) == EXPECTED_SKIPPED_RECORDS
    assert per_feed == EXPECTED_FEEDS, (
        f"feed 条目数变了：实测 {per_feed}，记录值 {EXPECTED_FEEDS}。"
        "请重新测量并更新本文件与交付报告，不要沿用陈旧数字"
    )
    assert entries_total == EXPECTED_ENTRIES
    assert article_units == EXPECTED_ARTICLE_UNITS
    assert entries_total + article_units == EXPECTED_UNITS
    assert skipped == EXPECTED_SKIPS, f"跳过理由码变了：{skipped}"
    # 每条 raw 都落在这三类的**闭集**里，没有第四条路径
    assert sum(kinds.values()) == len(RECORDS)
    assert set(kinds) == {"feed", "article", "skipped"}
    # 判据 11：只读
    assert _tree_digest(STORE_RAW) == before


@requires_real_data
def test_real_article_units_are_whole_documents_and_ids_are_unique() -> None:
    """65 篇文章各自成为**一个整篇单元**；单元 ID 跨 raw 全局唯一。"""
    seen: Dict[str, str] = {}
    articles = 0
    for record in RECORDS:
        if record.endpoint in EXPECTED_FEEDS or record.endpoint in EXPECTED_SKIPS:
            continue
        plan = classify_document(record.content, raw_id=record.raw_id)
        assert plan.kind is DocumentKind.ARTICLE, record.endpoint
        assert plan.unit_count == 1
        unit = plan.units[0]
        assert unit.kind == "article"
        assert unit.unit_id.startswith("art_")
        assert unit.char_start == 0 and unit.char_end > 0
        assert unit.unit_id not in seen, f"单元 ID 撞了：{unit.unit_id}"
        seen[unit.unit_id] = record.raw_id
        articles += 1
    assert articles == EXPECTED_ARTICLE_UNITS
    print(
        f"\n[T-105 真实] {articles} 篇导入文章 = {articles} 个整篇单元，ID 全局唯一；"
        f"样例：{list(seen)[0]}"
    )


@requires_real_data
def test_real_dispatch_is_reproducible() -> None:
    """真实数据上的分流可重算：同输入两次 → 逐字段相同的计划。"""
    for record in RECORDS[:12]:
        first = classify_document(record.content, raw_id=record.raw_id)
        second = classify_document(record.content, raw_id=record.raw_id)
        assert first == second


# =========================================================================== #
# 判据 12：真实模型上的完整链路（默认 skip）
# =========================================================================== #

#: 有界子集的规模（**不跑全部 895 个单元**：成本与时间都不可接受）。
SAMPLE_FEED_UNITS = 8
SAMPLE_ARTICLES = 3


def _sample_jobs() -> Tuple[List[Tuple[Record, bytes]], List[str]]:
    """有界子集：若干**条目** + 若干**导入文章**（两类输入都被覆盖）。"""
    jobs: List[Tuple[Record, bytes]] = []
    notes: List[str] = []
    feed = FEEDS[0]
    plan = classify_document(feed.content, raw_id=feed.raw_id, endpoint=feed.endpoint)
    jobs.append((feed, feed.content))
    notes.append(f"{feed.channel_id}: {plan.unit_count} 单元")
    articles = [
        record
        for record in RECORDS
        if record.endpoint not in EXPECTED_FEEDS and record.endpoint not in EXPECTED_SKIPS
    ][:SAMPLE_ARTICLES]
    for record in articles:
        jobs.append((record, record.content))
        notes.append(f"{record.channel_id}: 1 整篇单元")
    return jobs, notes


@requires_real_data
def test_real_pipeline_writes_and_reads_back(tmp_path) -> None:
    """**完整链路**：真实单元 → 真实模型 → 归属 → `proposed_claims` → 独立读回。

    数据库写到 `tmp_path`（**不**写 `data/store`，因为 pytest 可能跑在只读副本上）；
    往真实 `data/store/atlas.db` 写入 Proposed 由 `tools/t105_real_evidence.py` 完成
    （那是本任务的交付动作，不是门禁动作）。
    """
    _require_live()
    space = _real_label_space()

    jobs, notes = _sample_jobs()
    print("\n[T-105 真实] 样本：" + "；".join(notes))
    assert len(jobs) >= 2, "样本至少要有 1 份 feed + 1 篇文章"

    store = SqliteProposedStore(db_path=tmp_path / "t105.db")
    port = PiSidecarCognitionPort(CognitionConfig.from_env(route="deepseek"))
    outcome = propose_documents(
        jobs,
        label_space=space,
        port=port,
        store=store,
        policy=ProposalPolicy(max_units_per_call=8),
    )
    counters = outcome.counters
    print(
        f"[T-105 真实] 对账：{outcome.summary()}\n"
        f"[T-105 真实] 调用 {len(outcome.batches)} 次；"
        f"label_space={space.as_dict()}"
    )

    # 真实数字必须来自真实响应（不是零值）
    assert counters.units_run > 0
    assert counters.batches >= 1
    assert counters.input_tokens > 0 and counters.output_tokens > 0
    assert counters.elapsed_ms > 0
    for batch in outcome.batches:
        assert batch.tools_declared == 0 and batch.tool_calls == 0
        if batch.status == CallStatus.OK.value:
            assert batch.extracted_claims, "真实模型没有产出任何 claim"
            for claim in batch.attributed:
                assert claim.quote.strip()
                assert claim.value in space.labels
                # quote 必须逐字出现在**它被归属到**的那段单元文本里
                assert claim.quote in claim.unit.text

    # 独立读回（不经过本层的内存对象）
    stored = store.claim_count()
    assert stored == len(outcome.claims) >= 1
    checked = 0
    for row in store.current_for_raw(jobs[0][0].raw_id):
        assert row.code_version and row.config_version and row.model_version
        if row.is_classified:
            assert row.quote and row.value in space.labels
            checked += 1
    assert checked >= 1, "独立读回没有看到任何分类行"
    print(
        f"[T-105 真实] 落库 {stored} 行（独立读回校验 {checked} 条分类行）；"
        f"状态分布 {store.status_counts()}；理由分布 {store.reason_counts()}"
    )
    store.close()


@requires_real_data
def test_live_gate_skips_by_default() -> None:
    """**活对照**：不加环境变量时，真实模型调用类测试必须被**跳过**。"""
    if os.environ.get(LIVE_ENV_VAR) == "1":
        pytest.skip("本次运行显式开启了真实调用")
    with pytest.raises(BaseException) as excinfo:
        _require_live()
    assert LIVE_ENV_VAR in str(excinfo.value)


# =========================================================================== #
# 判据 13：降级路径的**真实触发**（不需要凭据，只需要边车；不可达端点）
# =========================================================================== #


@requires_real_data
def test_degradation_on_unreachable_endpoint_is_unclassified_with_reason(tmp_path) -> None:
    """**真实降级**：把 base_url 指向不可达端点 ⇒ `unclassified` + 非空原因码。

    这不是 mock：请求真的发出去、真的失败。断言三件事（§2.14 决策四）：
    `status == unclassified`、`reason` 非空、`claims` 为空；并且落库行为是
    **每个单元一行未分类**。
    """
    if not (SIDECAR_DIR / "node_modules").is_dir():
        pytest.skip("边车依赖未安装（需要 npm install）")

    from atlas.cognition import CognitionConfig, propose_units

    # 只取 **2 个短单元**（synced-review feed 的前两个条目）：降级测试不需要长文本，
    # 用它控制成本（每次调用仍要付一次边车启动）。
    feed = next(
        record
        for record in FEEDS
        if record.endpoint == "https://syncedreview.com/feed"
    )
    space = _real_label_space()
    plan = classify_document(feed.content, raw_id=feed.raw_id, endpoint=feed.endpoint)
    units = plan.units[:2]
    assert len(units) == 2

    store = SqliteProposedStore(db_path=tmp_path / "degrade.db")
    # 端口配置：真的会去连（但连不上）的端点。密钥随便给一个假的（不会真的用到）。
    config = CognitionConfig(
        provider="test-provider",
        model="deepseek-flash",
        base_url="http://127.0.0.1:1/v1",
        route_name="test",
        api_key="not-a-secret",
        timeout_seconds=15.0,
        model_version="deepseek-flash",
    )
    port = PiSidecarCognitionPort(config)
    outcome = propose_units(
        feed.raw_id,
        units,
        label_space=space,
        port=port,
        store=store,
        policy=ProposalPolicy(max_units_per_call=2, max_retries=0),
    )

    assert outcome.counters.calls_degraded >= 1
    unclassified = [row for row in outcome.claims if row.is_unclassified]
    assert len(unclassified) == len(units), "降级时每个单元都要有一行未分类"
    for row in unclassified:
        assert row.reason, "降级必须带原因码"
        assert row.value is None and row.quote is None, "降级不得携带编造的结果"
    for batch in outcome.batches:
        assert batch.degraded
        assert batch.reason
        assert not batch.extracted_claims and not batch.attributed
    print(
        f"\n[T-105 真实降级] 不可达端点 → {len(unclassified)} 行未分类；"
        f"原因码={sorted({row.reason for row in unclassified})}"
    )
    store.close()


def _real_label_space() -> LabelSpace:
    """从**注册表**读标签空间（组合根注入；本包不 import registry）。

    `author` 是 `SqliteConfigStore` 的必填参数（T-101 的构造契约）。库文件里已经有
    7 个配置版本，因此这次打开**只读**：构造函数只在"库里没有版本"时才会写 genesis。
    用测试专用的 actor 名字，万一将来真的写了，一眼能看出来是谁写的。
    """
    from atlas.registry import RegistryService, SqliteConfigStore

    config_store = SqliteConfigStore(
        author="t105-tests", db_path=REPO_ROOT / "data" / "store" / "atlas.db"
    )
    service = RegistryService(config_store)
    space = LabelSpace.of(
        service.label_space(), config_version=service.config_version, source="registry"
    )
    config_store.close()
    return space


def test_criteria_reference_is_stable() -> None:
    assert set(CRITERIA) == set(range(1, 16))
