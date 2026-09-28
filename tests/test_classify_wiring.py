"""T-105 **接进组合根**的接线判据（SPEC §4.2 T-105 / §4.5 / §2.5 / §2.14 / §2.17 / §3）。

本文件回答的是**接线与可用性**问题，不是 T-105 内部算法（那是
`tests/test_classify_*` / `test_propose_*` 的范围）。判据（本模块即判据的唯一出处）：

| # | 判据 | 用例 |
|---|---|---|
| C1 | **一条命令就能拿到 claim**：`classify` 是 DAG 节点，跑完一轮后 `proposed_claims` 里**有分类行**，且同一轮里 `evidence` 把它们校验成 `evidence_spans`（跨连接可见） | `test_closed_loop_classify_writes_claims_that_evidence_verifies_in_the_same_run` |
| C2 | **默认关闭**：`classify=False` 时节点照常出现在 DAG 与报告里，但明确记账"没有调用模型、没有产出"，端口**一次都不构造**；`run` 的既有行为不变 | `test_classify_is_off_by_default_and_never_builds_a_port` |
| C3 | **关→开必须重跑**：开关进输入快照 ⇒ 幂等键变 ⇒ 不会被"关闭态"的执行记录跳过 | `test_turning_classify_on_is_not_skipped_by_the_disabled_run` |
| C4 | **两层幂等**：同输入同配置 ⇒ 节点幂等跳过；换掉执行记录再跑 ⇒ T-105 的运行账让**已跑过的单元不再调用模型**（`units_skipped_already_run`），且报告如实说明 | `test_already_run_units_are_not_sent_to_the_model_again` |
| C5 | **标签空间来自注册表**（SPEC §2.5 的 C8 闭环）：候选标签 == 注册表启用行业；空标签空间**响亮失败**且一行都不写（活对照：非空时同一条路真的产出） | `test_label_space_is_injected_from_registry_and_empty_fails_loudly` |
| C6 | **降级可见**：整批降级 ⇒ 逐单元 `unclassified` 行 + `observed.degraded_calls` 逐条原因码，渲染里打得出来（§7.3 失败模式 3） | `test_degraded_calls_are_visible_and_never_invent_claims` |
| C6b | **重试预算耗尽 ≠ 会重试**：`max_output_tokens` 不在 `plan_digest` 里 ⇒ 已耗尽的单元被跳过；节点必须如实报 `units_skipped_already_run` 而不是伪装成空跑 | `test_exhausted_retryable_units_are_skipped_not_retried` |
| C7 | **收窄输入**：`raw_ids` 只跑指定 raw；写错的 raw_id **响亮失败**（不静默取交集），活对照是合法 id 真的产出 | `test_raw_id_narrowing_scopes_the_work_and_rejects_unknown_ids` |
| C8 | **一个库只由一个所有者开关**：`proposed` 由组合根打开 / 关闭；测试注入的实例不被关闭，且分类真的写进**被注入**的那个库 | `test_pipeline_owns_the_proposed_store_and_injection_is_respected` |
| C9 | **进幂等键的输入与真的送进模型的单元是同一份**：开关 / 标签空间 / 批次策略 / 单元投影都进快照；投影被改坏 ⇒ 响亮失败且不落库 | `test_classify_snapshot_covers_switch_labels_policy_and_units` |
| C10 | **不把 T-104 嗅探出的 Content-Type 喂进分流**（真实 store 上那会让 895 → 877 个单元静默消失），并把这条不一致作为 `content_type_conflicts` 显式呈现 | `test_t104_content_type_is_not_fed_into_dispatch_and_conflict_is_visible` |
| C11 | **未归属不得编造**：模型改写引文 ⇒ `unattributed` 审计行 + 逐单元未分类，`evidence` 因此看到 0 条分类行 | `test_quote_absent_from_the_units_is_accounted_not_invented` |

所有用例**离线**：假 fetcher 只回放内存响应，认知层端口是注入的哑端口（`QuotingPort`），
因此**一次模型调用都不发生**，也不需要 node / 凭据 / 边车依赖。全部指向 `tmp_path`，
绝不写仓库 `data/`。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.cognition import (
    DegradeReason,
    ProposalPolicy,
    open_proposed_store,
)
from atlas.compose import (
    NODE_CLASSIFY,
    NODE_COLLECT,
    NODE_EVIDENCE,
    ClassifyStageError,
    Pipeline,
    PipelineConfig,
    PipelineError,
)
from atlas.compose.pipeline import NodeInputs
from atlas.registry import RegistryService, open_store as open_registry
from atlas.runner import InMemoryExecutionRecordStore, TaskFailedError
from atlas.runner.runner import STATUS_SKIPPED
from tests._compose_classify import (
    ACTOR,
    ARTICLE_ENDPOINT,
    CHANNEL_ARTICLE,
    CHANNEL_FEED,
    CHANNEL_HTML_FEED,
    FEED_BYTES,
    FEED_ENDPOINT,
    HTML_FEED_BYTES,
    HTML_FEED_ENDPOINT,
    INDUSTRY_AI,
    INDUSTRY_WEB,
    QUOTE_ARTICLE_A,
    QUOTE_ARTICLE_B,
    QUOTE_FEED_A,
    QUOTE_FEED_B,
    QUOTE_HTML_FEED_A,
    QUOTE_HTML_FEED_B,
    RAW_TEXT,
    WINDOW,
    FakeFetcher,
    QuotingPort,
    classify_pipeline,
    dependencies,
    make_store_root,
    proposal_runs_rows,
    proposed_rows,
    raw_id_of,
    register_channel,
)
from atlas.registry import FetchType
from tests._compose_evidence import evidence_db_rows

REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """测试一律用 `tmp_path`，绝不往仓库 `data/` 写（SPEC §2.10 共享存储根）。"""
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


ARTICLE_RAW = raw_id_of(RAW_TEXT, channel_id=CHANNEL_ARTICLE, endpoint=ARTICLE_ENDPOINT)
FEED_RAW = raw_id_of(FEED_BYTES, channel_id=CHANNEL_FEED, endpoint=FEED_ENDPOINT)

#: 三个单元（1 篇整篇文档 + 2 个 feed 条目）各配一条**逐字** quote。
FULL_SCRIPT = (
    (INDUSTRY_AI, QUOTE_ARTICLE_A),
    (INDUSTRY_WEB, QUOTE_FEED_A),
    (INDUSTRY_WEB, QUOTE_FEED_B),
)


def _classify_observed(report) -> dict:
    return report.result(NODE_CLASSIFY).output.artifacts["observed"]


def _evidence_observed(report) -> dict:
    return report.result(NODE_EVIDENCE).output.artifacts["observed"]


# --------------------------------------------------------------------------- #
# C1：闭环 —— classify 写 claim，evidence 在同一轮里校验它
# --------------------------------------------------------------------------- #


def test_closed_loop_classify_writes_claims_that_evidence_verifies_in_the_same_run(
    tmp_path: Path,
) -> None:
    """**本任务要闭合的缺口**：系统必须能自己产出 claim，并在同一轮里校验它。

    在此之前 `atlas.compose` 对 `atlas.cognition` 零引用：流水线能采集 / 归档 /
    归一化 / 打标 / 校验证据，却永远不会写 `proposed_claims`，于是 `evidence` 在任何
    新 store 上都只能报 `classified_claims=0`（T-105 的能力只存在于测试与 tools 脚本里）。
    """
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)

    with classify_pipeline(root, port=port, classify=True) as pipeline:
        report = pipeline.run()

    assert report.all_succeeded(), [(r.task_name, r.status, r.reason) for r in report.results]
    # 节点真的在图里，且**先于** evidence（同一层里按声明顺序执行）
    assert NODE_CLASSIFY in report.order
    assert report.order.index(NODE_CLASSIFY) < report.order.index(NODE_EVIDENCE)
    assert report.order.index("normalize") < report.order.index(NODE_CLASSIFY)

    observed = _classify_observed(report)
    assert observed["enabled"] is True
    assert observed["raws_in_scope"] == 2
    assert observed["units_seen"] == 3, observed  # 1 整篇文档 + 2 个 feed 条目
    assert observed["units_classified"] == 3
    assert observed["units_unclassified"] == 0
    assert observed["calls_degraded"] == 0
    assert observed["calls_ok"] == port.call_count >= 1
    assert observed["rows_written"] >= 3
    assert observed["units_skipped_already_run"] == 0

    # 候选标签是**注册表**里的启用行业（C8 闭环），本端口的 value 都在集合里
    assert observed["label_space"]["labels"] == [INDUSTRY_AI, INDUSTRY_WEB]
    assert port.candidate_labels_seen
    for labels in port.candidate_labels_seen:
        assert set(labels) == {INDUSTRY_AI, INDUSTRY_WEB}

    # 跨连接读回：库里真的有分类行（同一连接的自我可见不算证据）
    rows = proposed_rows(root / "atlas.db")
    classified = [row for row in rows if row["status"] == "classified"]
    assert len(classified) == 3, rows
    assert {row["raw_id"] for row in classified} == {ARTICLE_RAW, FEED_RAW}
    assert {row["value"] for row in classified} == {INDUSTRY_AI, INDUSTRY_WEB}
    assert {row["quote"] for row in classified} == {
        QUOTE_ARTICLE_A,
        QUOTE_FEED_A,
        QUOTE_FEED_B,
    }
    for row in classified:
        assert row["unit_char_end"] > row["unit_char_start"] >= 0
        assert row["claim_key"].startswith("pcl_")
    # 运行账也落了库（"这个单元在本配置下跑过没有"）
    assert len(proposal_runs_rows(root / "atlas.db")) == 3

    # 同一轮里 evidence 校验的就是这些 claim（闭环的关键：快照在 classify 之后才算）
    evidence = _evidence_observed(report)
    assert evidence["classified_claims"] == 3
    assert evidence["verified"] == 3
    assert evidence["verification_failed"] == 0
    assert evidence["skipped_unclassified"] == 0
    assert evidence["spans_written"] == 3

    spans = evidence_db_rows(root / "atlas.db")
    assert len(spans) == 3
    assert {span["quote"] for span in spans} == {
        QUOTE_ARTICLE_A,
        QUOTE_FEED_A,
        QUOTE_FEED_B,
    }
    # 锚点落在它自己的单元区间里（SPEC §2.17 的不变量），且锚出来的正是那条 quote
    for span in spans:
        assert span["char_start"] < span["char_end"]


# --------------------------------------------------------------------------- #
# C2：默认关闭 —— 既有 `run` 行为不变，且端口一次都不构造
# --------------------------------------------------------------------------- #


def test_classify_is_off_by_default_and_never_builds_a_port(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    calls = {"n": 0}

    def exploding_factory():
        calls["n"] += 1
        raise AssertionError("关闭分类时绝不允许构造认知层端口（那会读凭据、起边车）")

    with classify_pipeline(
        root, port=None, classify=False, cognition_factory=exploding_factory
    ) as pipeline:
        report = pipeline.run()
        rows_after_off = proposed_rows(root / "atlas.db")

    assert report.all_succeeded()
    assert calls["n"] == 0, "关闭态不得构造端口"
    observed = _classify_observed(report)
    assert observed["enabled"] is False
    assert observed["reason"] == "model_calls_disabled"
    assert observed["units_seen"] == 0 and observed["rows_written"] == 0
    assert observed["claims_in_store"] == 0
    assert rows_after_off == []

    from atlas.compose.cli import render_classify, render_report

    rendered = render_report(report)
    assert "观察 classify：enabled=False" in rendered
    assert "没有**调用模型" in rendered
    lines = "\n".join(render_classify(observed))
    assert "enabled=False" in lines and "没有**调用模型" in lines

    # 活对照：同一条路开启后**真的**产出 claim（所以"没产出"不是因为路径不通）
    with classify_pipeline(root, port=QuotingPort(FULL_SCRIPT), classify=True) as pipeline:
        hot = pipeline.run()
    assert _classify_observed(hot)["rows_written"] >= 3
    assert len([r for r in proposed_rows(root / "atlas.db") if r["status"] == "classified"]) == 3


# --------------------------------------------------------------------------- #
# C3：关 → 开必须重跑（开关进快照，因此幂等键变）
# --------------------------------------------------------------------------- #


def test_turning_classify_on_is_not_skipped_by_the_disabled_run(tmp_path: Path) -> None:
    """若开关**不进**快照，"关闭态"的执行记录会把节点幂等跳过 ⇒ 永远分不出类。

    这正是 T-107 那条"claims 必须进快照"的同一形态，因此必须由测试钉死。
    """
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)

    with classify_pipeline(root, port=port, classify=False) as pipeline:
        off = pipeline.run()
    assert _classify_observed(off)["enabled"] is False
    assert proposed_rows(root / "atlas.db") == []

    # 同一个存储根、同一份执行记录（FileExecutionRecordStore 从盘上重新读）
    with classify_pipeline(root, port=port, classify=True) as pipeline:
        on = pipeline.run()

    assert on.result(NODE_CLASSIFY).status != STATUS_SKIPPED, "开启后节点被幂等跳过了"
    observed = _classify_observed(on)
    assert observed["enabled"] is True and observed["units_classified"] == 3
    assert port.call_count >= 1


# --------------------------------------------------------------------------- #
# C4：两层幂等 —— 节点层跳过；T-105 层让已跑过的单元不再烧钱
# --------------------------------------------------------------------------- #


def test_already_run_units_are_not_sent_to_the_model_again(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)

    with classify_pipeline(root, port=port, classify=True) as pipeline:
        first = pipeline.run()
        second = pipeline.run()
        calls_after_first_run = port.call_count

    assert _classify_observed(first)["units_classified"] == 3
    # 第一层：同输入 + 同配置 ⇒ 节点被幂等跳过（不是"又跑了一遍"）
    assert second.result(NODE_CLASSIFY).status == STATUS_SKIPPED
    assert port.call_count == calls_after_first_run

    # 第二层：换掉执行记录（新进程 / 新存储）后节点会真的重跑，
    # 但 T-105 的运行账（proposal_runs.plan_digest）让**每个单元**都不再调用模型。
    with classify_pipeline(
        root, port=port, classify=True, execution_store=InMemoryExecutionRecordStore()
    ) as pipeline:
        third = pipeline.run()

    assert third.result(NODE_CLASSIFY).status != STATUS_SKIPPED
    observed = _classify_observed(third)
    assert observed["units_seen"] == 3
    assert observed["units_skipped_already_run"] == 3
    assert observed["units_run"] == 0
    assert observed["batches"] == 0
    assert observed["rows_written"] == 0
    assert observed["rows_unchanged"] == 0
    assert port.call_count == calls_after_first_run, "已跑过的单元被重复送进模型（烧钱）"

    # 报告必须**如实区分**"没有输入"与"已经跑过"，并说明 max_output_tokens 不在 plan_digest
    from atlas.compose.cli import render_classify

    text = "\n".join(render_classify(observed))
    assert "units_skipped_already_run=3" in text
    assert "全部被幂等跳过" in text
    assert "max_output_tokens" in text
    assert len(proposed_rows(root / "atlas.db")) == 3  # 没有重复写行


# --------------------------------------------------------------------------- #
# C5：标签空间来自注册表；空标签空间响亮失败（附活对照）
# --------------------------------------------------------------------------- #


def test_label_space_is_injected_from_registry_and_empty_fails_loudly(
    tmp_path: Path,
) -> None:
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)
    with classify_pipeline(root, port=port, classify=False) as pipeline:
        pipeline.run()  # 先把归档准备好，离线入口才有输入

    # 负路径：一个**没有任何行业**的注册表（注入）⇒ 标签空间为空 ⇒ 响亮失败且不落库
    empty_registry = RegistryService(
        open_registry(tmp_path / "empty-registry.db", author="t105-tester")
    )
    try:
        assert empty_registry.label_space() == ()
        with pytest.raises(ClassifyStageError) as caught:
            with classify_pipeline(
                root, port=port, classify=True, registry=empty_registry
            ) as pipeline:
                pipeline.collect_classify_input()
    finally:
        empty_registry.store.close()
    message = str(caught.value)
    assert "标签空间" in message and "C8" in message
    assert proposed_rows(root / "atlas.db") == [], "空标签空间下不得写出任何分类行"

    # 活对照：同一条路 + 真注册表（有行业）⇒ 真的产出分类行
    with classify_pipeline(root, port=port, classify=True) as pipeline:
        stage, inputs, config = pipeline.collect_classify_input()
        output = stage.execute(inputs, config)
    observed = output.artifacts["observed"]
    assert observed["label_space"]["labels"] == [INDUSTRY_AI, INDUSTRY_WEB]
    assert observed["units_classified"] == 3
    assert len([r for r in proposed_rows(root / "atlas.db") if r["status"] == "classified"]) == 3


# --------------------------------------------------------------------------- #
# C6：降级必须可见，且绝不编造结果
# --------------------------------------------------------------------------- #


def test_degraded_calls_are_visible_and_never_invent_claims(tmp_path: Path) -> None:
    """整批降级（模型不可用）⇒ 每个单元一行 `unclassified`，并在报告里逐条可见。

    "降级不是异常"（SPEC §2.14 决策四）不等于"可以静默"：只打印计数会让
    "整批超时"看起来像"一切正常"（§7.3 失败模式 3）。因此本用例同时断言
    `observed.degraded_calls` 与**渲染结果**里都有原因码。
    """
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT, degrade_with=DegradeReason.TIMEOUT)

    with classify_pipeline(root, port=port, classify=True) as pipeline:
        report = pipeline.run()

    # 节点本身不算失败（降级是一等状态），但必须如实记账
    assert report.result(NODE_CLASSIFY).status != "failed"
    observed = _classify_observed(report)
    assert observed["units_seen"] == 3
    assert observed["units_unclassified"] == 3
    assert observed["units_classified"] == 0
    assert observed["calls_ok"] == 0
    assert observed["calls_degraded"] >= 1
    assert observed["unclassified_reasons"] == {"timeout": 3}
    assert len(observed["degraded_calls"]) == observed["calls_degraded"]
    for item in observed["degraded_calls"]:
        assert item["reason"] == "timeout"
        assert item["unit_ids"]
        assert item["detail"]

    rows = proposed_rows(root / "atlas.db")
    assert len(rows) == 3
    for row in rows:
        assert row["status"] == "unclassified"
        assert row["value"] is None and row["quote"] is None
        assert row["reason"] == "timeout"

    from atlas.compose.cli import render_classify, render_report

    text = "\n".join(render_classify(observed))
    assert "[降级] timeout" in text
    assert "没有任何一次成功的模型调用" in text
    assert "[降级] timeout" in render_report(report)
    # 闭环的下一环也如实：证据层看到的是"3 条未分类行被跳过"，而不是"校验通过"
    evidence = _evidence_observed(report)
    assert evidence["classified_claims"] == 0
    assert evidence["skipped_unclassified"] == 3
    assert evidence_db_rows(root / "atlas.db") == []

    # 活对照：同一份输入、**另一个干净的存储根** + 会正常回答的端口 ⇒ 真的产出分类行。
    # 必须换根：同一个根上这些单元的运行账已经写进 `proposal_runs`（只增不改），
    # 因此"降级 + 重试预算耗尽"之后换端口也不会重跑它们 —— 那条纪律由下一个用例专门钉。
    healthy_root = make_store_root(tmp_path / "healthy")
    with classify_pipeline(
        healthy_root, port=QuotingPort(FULL_SCRIPT), classify=True
    ) as pipeline:
        healthy = pipeline.run()
    assert _classify_observed(healthy)["units_classified"] == 3


def test_exhausted_retryable_units_are_skipped_not_retried(tmp_path: Path) -> None:
    """**必须如实呈现的那条机制**（SPEC §2.17 已记录，本任务要求显式表面化）：

    `max_output_tokens` **不在** `plan_digest` 里（`config_version` 是静态常量
    `CONFIG_VERSION`，不是 `public_digest()`）。因此一个已经跑过、可重试原因
    （`timeout` / `empty_completion` / …）且 `retry_count` 已到 `max_retries` 的单元
    **会被跳过，而不是重试** —— 换一个更好的模型端口也救不回它，改预算同样不行。

    节点因此必须把 `units_skipped_already_run` 如实报出来，并说明"这不是没有输入"，
    而不是让它看起来像一次成功的空跑（那正是"看起来成功、实际什么都没做"）。
    """
    root = make_store_root(tmp_path)
    unhealthy = QuotingPort(FULL_SCRIPT, degrade_with=DegradeReason.TIMEOUT)

    with classify_pipeline(root, port=unhealthy, classify=True) as pipeline:
        first = pipeline.run()
    observed = _classify_observed(first)
    assert observed["units_unclassified"] == 3
    assert observed["units_retry_exhausted"] == 3
    # 每个 raw 都走了两轮重试（整批 → 单单元），两个 raw ⇒ 4 轮
    assert observed["retries"] == 4
    runs = proposal_runs_rows(root / "atlas.db")
    assert {row["status"] for row in runs} == {"unclassified"}
    assert {row["reason"] for row in runs} == {"timeout"}
    assert {row["retry_count"] for row in runs} == {2}  # == policy.max_retries

    # 换一个会正常回答的端口 + 干净的执行记录：节点会重跑，但**每个单元都被跳过**
    healthy = QuotingPort(FULL_SCRIPT)
    with classify_pipeline(
        root, port=healthy, classify=True, execution_store=InMemoryExecutionRecordStore()
    ) as pipeline:
        second = pipeline.run()

    assert second.result(NODE_CLASSIFY).status != STATUS_SKIPPED
    observed = _classify_observed(second)
    assert healthy.call_count == 0, "重试预算耗尽的单元不该被再次送进模型"
    assert observed["units_seen"] == 3
    assert observed["units_skipped_already_run"] == 3
    assert observed["units_run"] == 0
    assert observed["units_classified"] == 0
    assert observed["rows_written"] == 0 and observed["rows_unchanged"] == 0
    # 库里仍然是那 3 行降级行（没有新行、没有版本链分叉）
    rows = proposed_rows(root / "atlas.db")
    assert len(rows) == 3 and {row["version"] for row in rows} == {1}
    assert evidence_db_rows(root / "atlas.db") == []

    from atlas.compose.cli import render_classify

    text = "\n".join(render_classify(observed))
    assert "units_skipped_already_run=3" in text
    assert "全部被幂等跳过" in text
    assert "max_output_tokens" in text and "标签空间" in text


# --------------------------------------------------------------------------- #
# C7：`raw_ids` 收窄（离线入口）—— 只跑指定的 raw，写错的响亮失败
# --------------------------------------------------------------------------- #


def test_raw_id_narrowing_scopes_the_work_and_rejects_unknown_ids(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)
    with classify_pipeline(root, port=port, classify=False) as pipeline:
        pipeline.run()

    # 负路径：写错一个字符不得被解释成"没有这条"
    bogus = "raw_" + "0" * 32
    assert bogus not in (ARTICLE_RAW, FEED_RAW)
    with pytest.raises(PipelineError) as caught:
        with classify_pipeline(root, port=port, classify=True, raw_ids=(bogus,)) as pipeline:
            pipeline.collect_classify_input()
    assert bogus in str(caught.value)
    assert "拒绝静默取交集" in str(caught.value)
    assert proposed_rows(root / "atlas.db") == []

    # 活对照：指定一个真的存在的 raw ⇒ 只跑它，且真的产出
    with classify_pipeline(
        root, port=port, classify=True, raw_ids=(ARTICLE_RAW,)
    ) as pipeline:
        stage, inputs, config = pipeline.collect_classify_input()
        assert inputs.payload["raw_ids"] == [ARTICLE_RAW]
        output = stage.execute(inputs, config)

    observed = output.artifacts["observed"]
    assert observed["raws_in_scope"] == 1
    assert observed["units_seen"] == 1
    assert observed["units_classified"] == 1
    rows = [row for row in proposed_rows(root / "atlas.db") if row["status"] == "classified"]
    assert len(rows) == 1
    assert rows[0]["raw_id"] == ARTICLE_RAW
    assert rows[0]["quote"] == QUOTE_ARTICLE_A


# --------------------------------------------------------------------------- #
# C8：库的所有权 —— 组合根打开 / 关闭；测试注入的实例不被关闭
# --------------------------------------------------------------------------- #


def test_pipeline_owns_the_proposed_store_and_injection_is_respected(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    pipeline = classify_pipeline(root, port=QuotingPort(FULL_SCRIPT), classify=True)
    owned = pipeline.proposed
    pipeline.close()
    # 组合根打开的句柄由组合根关闭（"一个库一个所有者"）
    with pytest.raises(sqlite3.ProgrammingError):
        owned.claim_count()

    # 活对照：测试注入的 store 由调用方负责，组合根不得关掉它；
    # 并且分类真的写进**被注入**的那个库（而不是悄悄另开一个）。
    injected = open_proposed_store(tmp_path / "injected.db")
    try:
        with classify_pipeline(
            root, port=QuotingPort(FULL_SCRIPT), classify=True, proposed=injected
        ) as pipeline:
            report = pipeline.run()
        assert _classify_observed(report)["units_classified"] == 3
        assert injected.claim_count() == 3
        assert proposed_rows(tmp_path / "injected.db")
        # 注入的库在流水线关闭后仍然可用
        assert injected.claim_count() == 3
    finally:
        injected.close()


# --------------------------------------------------------------------------- #
# C9：进幂等键的输入 == 真的送进模型的单元
# --------------------------------------------------------------------------- #


def _classify_snapshot(pipeline, classify: bool, policy=None, raw_ids=()):
    """重建 `classify` 节点的输入快照（`NodeInputs` 会缓存，因此每次都新起一个）。"""
    inputs = pipeline.last_inputs
    assert inputs is not None
    rebuilt_config = type(pipeline.config)(
        store_root=pipeline.config.store_root,
        actor=pipeline.config.actor,
        window=pipeline.config.window,
        classify=classify,
        classify_policy=policy,
        raw_ids=raw_ids,
    )
    rebuilt = Pipeline(
        rebuilt_config,
        dependencies=pipeline.dependencies,
        execution_store=pipeline._store,
        archive=pipeline.archive,
        labels=pipeline.labels,
        registry=pipeline.registry,
        evidence=pipeline.evidence,
        proposed=pipeline.proposed,
    )
    try:
        node_inputs = NodeInputs(
            rebuilt.build_graph(),
            pipeline._store,
            pipeline._config_snapshot(),
            roots={
                NODE_COLLECT: {
                    "channels": inputs[NODE_COLLECT].payload["channels"],
                    "window": inputs[NODE_COLLECT].payload["window"],
                }
            },
            extra=rebuilt._extra_payload,
        )
        return node_inputs[NODE_CLASSIFY]
    finally:
        rebuilt.close()


def test_classify_snapshot_covers_switch_labels_policy_and_units(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    port = QuotingPort(FULL_SCRIPT)
    with classify_pipeline(root, port=port, classify=False) as pipeline:
        pipeline.run()
        task = pipeline.build_graph().task(NODE_CLASSIFY)
        config = pipeline._config_snapshot()

        off = _classify_snapshot(pipeline, False)
        on = _classify_snapshot(pipeline, True)
        narrow = _classify_snapshot(pipeline, True, policy=ProposalPolicy(max_units_per_call=1))

    keys = {
        "off": task.idempotency_key(off, config),
        "on": task.idempotency_key(on, config),
        "narrow": task.idempotency_key(narrow, config),
    }
    assert len(set(keys.values())) == 3, f"幂等键没有区分开关 / 策略：{keys}"

    # 关闭态只有"关掉了"这件事本身；开启态必须把四样东西都带上
    assert off.payload["enabled"] is False
    assert on.payload["enabled"] is True
    assert on.payload["label_space"]["labels"] == [INDUSTRY_AI, INDUSTRY_WEB]
    assert on.payload["policy"]["max_units_per_call"] == 4  # SPEC §2.17 的登记值
    assert narrow.payload["policy"]["max_units_per_call"] == 1
    assert sorted(on.payload["raw_ids"]) == sorted([ARTICLE_RAW, FEED_RAW])
    plans = {item["raw_id"]: item for item in on.payload["plans"]}
    assert plans[ARTICLE_RAW]["unit_count"] == 1
    assert plans[FEED_RAW]["unit_count"] == 2
    assert plans[ARTICLE_RAW]["kind"] == "article"
    assert plans[FEED_RAW]["kind"] == "feed"
    for item in plans.values():
        assert len(item["units_digest"]) == 64
        assert item["skip_reason"] is None


def test_projection_drift_fails_loudly_and_writes_nothing(tmp_path: Path) -> None:
    """快照里的单元投影被改坏 ⇒ 响亮失败、一行都不写（绝不"按错的输入继续"）。

    活对照：同一条流水线不改投影时真的产出分类行。
    """

    class DriftingPipeline(Pipeline):
        def _classify_input(self, inputs):
            payload = dict(super()._classify_input(inputs))
            if payload.get("enabled"):
                payload["plans"] = [
                    {**item, "units_digest": "0" * 64} for item in payload["plans"]
                ]
            return payload

    root = make_store_root(tmp_path)
    config = PipelineConfig(
        store_root=root, actor=ACTOR, window=WINDOW, classify=True
    )
    pipeline = DriftingPipeline(
        config, dependencies=dependencies(None), cognition=QuotingPort(FULL_SCRIPT)
    )
    try:
        with pytest.raises(TaskFailedError) as error:
            pipeline.run()
    finally:
        pipeline.close()
    assert isinstance(error.value.last_error, ClassifyStageError)
    assert "单元投影与输入快照不一致" in str(error.value.last_error)
    assert proposed_rows(root / "atlas.db") == [], "投影不一致时不得写出任何行"

    # 活对照：同一份输入、不改投影 ⇒ 真的产出
    with classify_pipeline(root, port=QuotingPort(FULL_SCRIPT), classify=True) as healthy:
        report = healthy.run()
    assert _classify_observed(report)["units_classified"] == 3
    assert len(proposed_rows(root / "atlas.db")) == 3


def test_t104_content_type_is_not_fed_into_dispatch_and_conflict_is_visible(
    tmp_path: Path,
) -> None:
    r"""**真实缺陷的回归测试**（在真实 store 上实测到，SPEC §2.17 登记的 895 个单元）。

    `normalize()` 的兜底嗅探（`looks_like_html`）只看前 4096 字符里有没有
    `<\s*(html|body|div|p|…)\b`，因此**带 HTML 标记的良构 RSS** 会被嗅探成 `text/html`；
    而 T-130 的 `_reject_non_xml` 对 `mime == "text/html"` **一律拒绝**条目化。
    若把 T-104 记下来的 Content-Type 喂进 T-105 的分流，整份 feed 就会被判成
    **一篇文章**：真实 store 上这点差异 = **18 个条目（895 → 877）静默消失**。

    因此本节点按**字节**判定（`DISPATCH_CONTENT_TYPE = ""`），并把这条不一致
    作为 `observed.content_type_conflicts` 显式呈现 —— 两个答案都要看得见。
    """
    root = tmp_path / "store"
    register_channel(
        root,
        channel_id=CHANNEL_HTML_FEED,
        industry_id=INDUSTRY_AI,
        endpoint=HTML_FEED_ENDPOINT,
        fetch_type=FetchType.RSS,
    )
    fetcher = FakeFetcher({HTML_FEED_ENDPOINT: HTML_FEED_BYTES})
    port = QuotingPort([(INDUSTRY_AI, QUOTE_HTML_FEED_A), (INDUSTRY_AI, QUOTE_HTML_FEED_B)])

    with classify_pipeline(root, fetcher=fetcher, port=port, classify=True) as pipeline:
        report = pipeline.run()

    observed = _classify_observed(report)
    # 前提：T-104 真的把这份 RSS 嗅探成了 text/html（归一化产物里记的就是它）
    normalized = report.result("normalize").output.artifacts["identity"]["records"]
    assert [item["content_type"] for item in normalized] == ["text/html"]
    # 生产路径按字节判定 ⇒ **两个条目**（而不是一篇"文章"）
    assert observed["units_seen"] == 2
    assert observed["units_classified"] == 2

    conflicts = observed["content_type_conflicts"]
    assert len(conflicts) == 1, conflicts
    conflict = conflicts[0]
    assert conflict["normalize_content_type"] == "text/html"
    assert conflict["dispatched"] == {"kind": "feed", "unit_count": 2, "skip_reason": None}
    assert conflict["if_content_type_used"] == {
        "kind": "article",
        "unit_count": 1,
        "skip_reason": None,
    }
    from atlas.compose.cli import render_classify

    text = "\n".join(render_classify(observed))
    assert "[CT 冲突]" in text and "text/html" in text

    # 活对照：**不带** HTML 标记的 feed（另外两条渠道）不会被报成冲突
    clean_root = make_store_root(tmp_path / "clean")
    with classify_pipeline(
        clean_root, port=QuotingPort(FULL_SCRIPT), classify=True
    ) as pipeline:
        clean = pipeline.run()
    assert _classify_observed(clean)["content_type_conflicts"] == []


def test_quote_absent_from_the_units_is_accounted_not_invented(tmp_path: Path) -> None:
    """模型给出**原文里没有**的 quote（改写 / 翻译）⇒ 未归属审计行，不得硬塞给某个单元。

    这是 T-105 的归属纪律在**接线层**的活证据：节点不会因为"模型说了些什么"就写出
    一条无法锚定的证据；`evidence` 因此在同一轮里看到的是 0 条分类行 + 未分类/未归属记账。
    """
    root = make_store_root(tmp_path)
    port = QuotingPort(
        [(INDUSTRY_AI, QUOTE_ARTICLE_B.replace("确定性", "启发式"))], hallucinate=True
    )

    with classify_pipeline(root, port=port, classify=True) as pipeline:
        report = pipeline.run()

    observed = _classify_observed(report)
    assert observed["units_classified"] == 0
    # 两个 raw 各一次调用；每次调用都回放那条**原文里不存在**的 quote
    assert observed["extracted_claims"] == 2
    assert observed["attributed_claims"] == 0
    assert observed["unattributed_claims"] == 2
    assert observed["units_unclassified"] == 3
    assert observed["unclassified_reasons"] == {"no_claim_extracted": 3}
    rows = proposed_rows(root / "atlas.db")
    statuses = [row["status"] for row in rows]
    assert statuses.count("unattributed") == 2
    assert statuses.count("unclassified") == 3
    assert not [row for row in rows if row["status"] == "classified"]
    for row in rows:
        if row["status"] == "unattributed":
            # 审计行：记下模型说了什么，但**不得**当证据用（quote 恒为空、理由必非空）
            assert row["quote"] is None
            assert row["reason"]
    assert evidence_db_rows(root / "atlas.db") == []
