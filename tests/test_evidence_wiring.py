"""T-107 **接线**判据（SPEC §4.2 T-107 / §2.2 / §2.3 / §2.17 / §4.5）。

本文件回答的是**接线**问题，不是 T-107 内部算法（那是
`tests/test_evidence_verify.py` / `tests/test_evidence_store.py` 的范围）：

1. **能力真的被接进流水线**：`evidence` 是 DAG 的一个节点，跑完一遍后
   `evidence_spans` 里**有行**，且该行能由 quote + 原文独立重算出来
   （硬规则 1："完成"必须用跑通的数据流证明，不是"有文件/有字段/有测试"）。
2. **轨迹边界不变**：坐标只来自确定性匹配（本层**没有**接收坐标的参数）；
   失败的 claim **不落库**，但**必须可见**。
3. **未分类行必须被显式跳过并计数**（`skipped_unclassified`）——
   不得静默丢弃。
4. **幂等**：同输入 + 同配置 ⇒ 幂等跳过、库不变；T-105 产出**新** claim ⇒
   幂等键变化 ⇒ 重跑（这一点靠"claims 投影进输入快照"结构性成立）。
5. **失败可见**：`render_evidence()` 必须把"未验证"逐条打印出来；只打印
   `identity` 会让"3 条 quote 找不到"看起来像"一切正常"（SPEC §7.3 失败模式 3）。
6. **锚点在它自己的单元区间内**：越界 ⇒ 响亮失败，且**不落库**。
7. **保守 = 不落库**（`--read-only`）：`wrote_to_store=false` 时表里一行都不多。

**负路径都带活对照**：断言"这条 quote 落不了库"的同时，同一次运行里另一条
真的能从**另一个连接**读到。跨连接可见才是"真的写进去了"的证据
（SPEC §6.6 第 4 条：同一连接的自我可见不算）。

全部指向 `tmp_path`，绝不写仓库 `data/`。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

import pytest

from atlas.cognition import (
    ProposedStoreError,
    open_proposed_store,
)
from atlas.compose import NODE_EVIDENCE, Pipeline
from atlas.compose.pipeline import NodeInputs
from atlas.compose.tasks import (
    EvidenceStageError,
    claim_verification_requests,
    proposed_claim_from_snapshot,
)
from atlas.contracts import (
    ProposedClaim,
    VerificationStatus,
    content_sha256,
)
from atlas.evidence import verify_quote
from atlas.runner import TaskFailedError
from atlas.runner.runner import STATUS_SKIPPED
from tests._compose_evidence import (
    QUOTE_A,
    QUOTE_B,
    QUOTE_MISSING,
    RAW_TEXT,
    classified_row,
    evidence_db_rows,
    evidence_pipeline,
    make_store_root,
    unclassified_row,
)

REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """测试一律用 `tmp_path`，绝不往仓库 `data/` 写（SPEC §2.10 共享存储根）。"""
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


def _collect_identity(report) -> dict:
    records = report.result("collect").output.artifacts["identity"]["records"]
    assert len(records) == 1
    return records[0]["raw"]


def _evidence_observed(report) -> dict:
    return report.result(NODE_EVIDENCE).output.artifacts["observed"]


def _evidence_identity(report) -> dict:
    return report.result(NODE_EVIDENCE).output.artifacts["identity"]


# --------------------------------------------------------------------------- #
# 判据 1：节点真的跑起来，且落库的证据可由 quote + 原文独立重算
# --------------------------------------------------------------------------- #


def test_evidence_node_verifies_classified_claim_into_temp_store(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw = _collect_identity(report)
        raw_id = raw["raw_id"]
        # T-105 的产出在这里由测试注入（组合根从 proposed_claims 读取）
        pipeline.proposed.record_claim(
            classified_row(raw_id=raw_id, quote=QUOTE_A, unit_char_start=0, unit_char_end=len(RAW_TEXT))
        )
        report = pipeline.run()

    assert NODE_EVIDENCE in report.order
    observed = _evidence_observed(report)
    assert observed["raws_in_scope"] == 1
    assert observed["classified_claims"] == 1
    assert observed["verified"] == 1
    assert observed["verification_failed"] == 0
    assert observed["skipped_unclassified"] == 0
    assert observed["spans_written"] == 1
    assert observed["wrote_to_store"] is True

    # 落库的行从**另一个连接**读出来（跨连接可见才叫真的写进去了）
    rows = evidence_db_rows(root / "atlas.db")
    assert len(rows) == 1
    span = rows[0]
    assert span["raw_id"] == raw_id
    assert span["raw_sha256"] == content_sha256(RAW_TEXT.encode("utf-8"))
    assert span["claim_version"] == 1

    # 该行可由 quote + 原文**独立重算**（坐标不是被存下来的、而是算出来的）
    independent = verify_quote(
        raw_id=raw_id, raw_bytes=RAW_TEXT.encode("utf-8"), quote=QUOTE_A, content_type=""
    )
    assert independent.status is VerificationStatus.VERIFIED
    assert independent.anchor is not None
    assert (span["char_start"], span["char_end"]) == (
        independent.anchor.char_start,
        independent.anchor.char_end,
    )
    assert (span["normalized_start"], span["normalized_end"]) == (
        independent.derived.normalized_start,
        independent.derived.normalized_end,
    )
    raw_text = span_raw_text(root, raw_id)
    assert raw_text[span["char_start"] : span["char_end"]] == QUOTE_A


def span_raw_text(root: Path, raw_id: str) -> str:
    """从归档里独立读回原文（不信产物自述）。"""
    from atlas.archive import open_archive

    archive = open_archive(root)
    try:
        return archive.get_content(raw_id).decode("utf-8")
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 2：未分类行必须被**显式**跳过并计数
# --------------------------------------------------------------------------- #


def test_unclassified_rows_are_skipped_visibly(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        pipeline.proposed.record_claim(unclassified_row(raw_id=raw_id, reason="timeout"))
        report = pipeline.run()

    observed = _evidence_observed(report)
    assert observed["classified_claims"] == 0
    assert observed["verified"] == 0
    assert observed["spans_written"] == 0
    # 关键：不是"0 条"了事，而是明确报出"跳过了 1 条未分类行"
    assert observed["skipped_unclassified"] == 1
    assert observed["raws_without_claims"] == [raw_id]
    assert evidence_db_rows(root / "atlas.db") == []


# --------------------------------------------------------------------------- #
# 判据 3：幂等 —— 同输入同配置 ⇒ 跳过；新 claim ⇒ 重跑
# --------------------------------------------------------------------------- #


def test_evidence_node_is_idempotent_and_reruns_on_new_claims(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_A,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "1" * 32,
            )
        )
        first = pipeline.run()
        second = pipeline.run()

        assert _evidence_observed(first)["spans_written"] == 1
        # 同输入 + 同配置 ⇒ 幂等跳过（不是"又写了一遍"）
        assert second.result(NODE_EVIDENCE).status == STATUS_SKIPPED
        assert _evidence_observed(second)["spans_written"] == 1  # 回放第一次的产物
        assert len(evidence_db_rows(root / "atlas.db")) == 1

        # T-105 又产出一条**新** claim ⇒ 幂等键变化 ⇒ 必须重跑（否则新 claim 永远等不到校验）
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_B,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "2" * 32,
            )
        )
        third = pipeline.run()

    assert third.result(NODE_EVIDENCE).status != STATUS_SKIPPED
    observed = _evidence_observed(third)
    assert observed["classified_claims"] == 2
    assert observed["verified"] == 2
    assert observed["spans_written"] == 1  # 只多了那一条新 claim
    assert observed["spans_unchanged"] == 1
    assert len(evidence_db_rows(root / "atlas.db")) == 2


# --------------------------------------------------------------------------- #
# 判据 4：负路径（quote 找不到）**不落库**，且有跨连接可见的活对照
# --------------------------------------------------------------------------- #


def test_quote_not_found_is_failed_and_not_persisted_with_live_control(
    tmp_path: Path,
) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        # 活对照：同一次运行里的另一条 claim，quote 真的在原文里
        control, _ = pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_B,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "1" * 32,
            )
        )
        control_claim_id = control.claim_key
        # 负路径：quote 在原文里不存在
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_MISSING,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "2" * 32,
            )
        )
        report = pipeline.run()

    observed = _evidence_observed(report)
    assert observed["classified_claims"] == 2
    assert observed["verified"] == 1
    assert observed["verification_failed"] == 1
    assert observed["spans_written"] == 1
    failures = observed["failures"]
    assert len(failures) == 1
    assert failures[0]["quote"] == QUOTE_MISSING
    assert failures[0]["status"] == VerificationStatus.FAILED.value
    # FAILED 的 claim **没有**坐标（不存在"大概位置"）
    assert failures[0]["char_start"] is None
    assert failures[0]["char_end"] is None

    # 跨连接读回：只有活对照那一行在库里
    rows = evidence_db_rows(root / "atlas.db")
    assert len(rows) == 1, f"未验证的证据不得落库，实际库里 {rows!r}"
    assert rows[0]["quote"] == QUOTE_B
    assert rows[0]["claim_id"] == control_claim_id

    # 直接核对"失败那条 claim 的行确实不存在"（按 claim_id 指名道姓）
    failed_claim_id = failures[0]["claim_id"]
    connection = sqlite3.connect(str(root / "atlas.db"), isolation_level=None)
    try:
        hit = connection.execute(
            "SELECT COUNT(*) FROM evidence_spans WHERE claim_id = ?", (failed_claim_id,)
        ).fetchone()[0]
        control = connection.execute(
            "SELECT COUNT(*) FROM evidence_spans WHERE quote = ?", (QUOTE_B,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert hit == 0
    assert control == 1  # 活对照：同一张表、同一调用路径，合法输入真的写进去了


# --------------------------------------------------------------------------- #
# 判据 5：失败可见 —— `render_evidence()` 必须把"未验证"打出来
# --------------------------------------------------------------------------- #


def test_render_evidence_surfaces_failures_and_skips(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_MISSING,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "2" * 32,
            )
        )
        pipeline.proposed.record_claim(unclassified_row(raw_id=raw_id))
        report = pipeline.run()

    from atlas.compose.cli import render_evidence, render_report

    observed = _evidence_observed(report)
    lines = render_evidence(observed)
    text = "\n".join(lines)
    assert "verification_failed=1" in text
    assert "skipped_unclassified=1" in text
    assert "[未验证]" in text and QUOTE_MISSING in text

    # 整份报告（用户实际看到的东西）里也必须有这段
    report_text = render_report(report)
    assert "[未验证]" in report_text
    assert QUOTE_MISSING in report_text
    assert f"skipped_unclassified=1" in report_text


# --------------------------------------------------------------------------- #
# 判据 6：锚点必须落在**它自己的单元区间**内；越界 ⇒ 响亮失败且不落库
# --------------------------------------------------------------------------- #


def test_anchor_outside_declared_unit_range_fails_loudly(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        # 单元区间截在 quote 之前 —— 这条 claim 的证据不在它自己的单元里
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_B,
                unit_char_start=0,
                unit_char_end=10,
            )
        )
        with pytest.raises(TaskFailedError) as error:
            pipeline.run()

    assert isinstance(error.value.last_error, EvidenceStageError)
    assert "越出它自己的单元区间" in str(error.value.last_error)
    # 响亮失败 ⇒ 一条都不许落库
    assert evidence_db_rows(root / "atlas.db") == []


def test_proposed_store_error_from_bridge_is_not_swallowed(tmp_path: Path) -> None:
    """桥的响亮失败真的存在，且本层**没有** `except` 会把它变成"跳过"（硬规则 2）。

    活对照：同一调用路径上 `classified` 行必须真的转换成功。
    """
    store = open_proposed_store(tmp_path / "bridge.db")
    try:
        classified, _ = store.record_claim(
            classified_row(
                raw_id="raw_" + "a" * 32,
                quote="a quote from the document",
                unit_char_start=0,
                unit_char_end=100,
            )
        )
        unclassified, _ = store.record_claim(
            unclassified_row(raw_id="raw_" + "a" * 32, reason="timeout")
        )
    finally:
        store.close()

    # 活对照：分类行**必须**转换成功
    assert classified.as_proposed_claim().version == 1
    # 负路径：未分类行在契约里根本不存在对应状态
    with pytest.raises(ProposedStoreError):
        unclassified.as_proposed_claim()
    # 组合根因此只投影分类行；未分类行只作为**计数**进入快照（不静默丢弃）
    assert unclassified.status == "unclassified"
    assert unclassified.quote is None


# --------------------------------------------------------------------------- #
# 判据 7：输入快照的形状校验（接线错误响亮失败，不"尽力继续"）
# --------------------------------------------------------------------------- #


def test_claims_snapshot_shape_is_validated_loudly() -> None:
    with pytest.raises(EvidenceStageError):
        claim_verification_requests({"claim_id": "x"})  # 不是列表

    with pytest.raises(EvidenceStageError):
        claim_verification_requests([{"claim_id": "x"}])  # 缺字段

    with pytest.raises(EvidenceStageError):
        claim_verification_requests(
            [
                {
                    "claim_id": "x",
                    "claim_version": 0,  # 尚未入 store：不得为它编造版本号
                    "raw_id": "raw",
                    "quote": "q",
                    "kind": "industry",
                    "value": "ai",
                    "confidence": 0.5,
                    "unit_char_start": 0,
                    "unit_char_end": 10,
                    "code_version": "c",
                    "config_version": "v",
                    "model_version": "m",
                }
            ]
        )

    # 活对照：合法形状必须真的通过，并原样保留版本号
    ok = claim_verification_requests(
        [
            {
                "claim_id": "x",
                "claim_version": 3,
                "raw_id": "raw",
                "quote": "q",
                "kind": "industry",
                "value": "ai",
                "confidence": 0.5,
                "unit_char_start": 0,
                "unit_char_end": 10,
                "code_version": "c",
                "config_version": "v",
                "model_version": "m",
            }
        ]
    )
    assert len(ok) == 1 and ok[0]["claim_version"] == 3


def test_snapshot_claim_projection_matches_t105_bridge(tmp_path: Path) -> None:
    """契约对象**只有一处口径**：快照投影 == `ProposedClaimRow.as_proposed_claim()`。

    T-107 阶段里没有 `proposed_claims` 的行（只有组合根投影出来的字段），所以它必须
    自己还原契约对象。这条测试拿**真的入过 store 的行**逐字段比对两者，防止两份实现漂移。
    """
    store = open_proposed_store(tmp_path / "bridge.db")
    try:
        row, _ = store.record_claim(
            classified_row(
                raw_id="raw_" + "a" * 32,
                quote="a quote from the document",
                unit_char_start=0,
                unit_char_end=100,
            )
        )
    finally:
        store.close()

    bridge: ProposedClaim = row.as_proposed_claim()
    projected = proposed_claim_from_snapshot(
        {
            "claim_id": row.claim_key,
            "claim_version": row.version,
            "raw_id": row.raw_id,
            "kind": row.kind,
            "value": row.value,
            "quote": row.quote,
            "confidence": row.confidence,
            "code_version": row.code_version,
            "config_version": row.config_version,
            "model_version": row.model_version,
        }
    )
    assert projected.claim_id == bridge.claim_id
    assert projected.raw_id == bridge.raw_id
    assert projected.kind == bridge.kind
    assert projected.value == bridge.value
    assert projected.quote == bridge.quote
    assert projected.confidence == bridge.confidence
    assert projected.version == bridge.version == 1
    assert projected.versions == bridge.versions
    assert projected.verification_status is VerificationStatus.UNVERIFIED
    assert projected.anchor is None


def test_t105_bridge_version_coercion_is_never_used_by_the_evidence_path(
    tmp_path: Path,
) -> None:
    """`ProposedClaimRow.as_proposed_claim()` 的**版本号抬升**与 T-107 的幂等键。

    这是一个真实的接缝风险（本任务被要求专门核查）：

    | 事实 | 证据 |
    |---|---|
    | `as_proposed_claim()` 用 `version=max(self.version, 1)`，把 `version=0`（尚未入 store）**抬成 1** | 本测试第一条断言 |
    | T-107 的证据身份是 `(claim_id, claim_version)` | `EvidenceSpan.key` |
    | `verify_claim()` 对 `version<1` **响亮失败**（`VersionError`） | `atlas/evidence/verify.py` |

    因此"抬升"是**唯一**能让一条未入 store 的行拿到合法证据身份 `(key, 1)` 的路径 ——
    也就是说这条路径**会编造版本号**。T-107 的接线**不使用**它：
    组合根只投影**库里读回来的行**（`current_for_raw()` ⇒ `version >= 1`），
    阶段自己按 `claim_version` 逐字段还原契约对象（`proposed_claim_from_snapshot`），
    并对 `claim_version < 1` 响亮失败。这条测试把两侧都钉住。
    """
    # 1) 记录桥的真实行为：version=0 被抬成 1（本测试不改变它，只钉住现状）
    unstored = classified_row(
        raw_id="raw_" + "a" * 32,
        quote="a quote from the document",
        unit_char_start=0,
        unit_char_end=100,
    )
    assert unstored.version == 0
    assert unstored.as_proposed_claim().version == 1, (
        "若这条断言失败，说明 T-105 改了抬升行为；"
        "这会**改善**本接缝（不再编造版本号），但必须同步更新本测试与最终报告"
    )

    # 2) 组合根只投影**库里读回来的行**：它们的 version 恒 >= 1
    store = open_proposed_store(tmp_path / "bridge.db")
    try:
        assert store.current_for_raw("raw_" + "a" * 32) == []
        stored, _ = store.record_claim(unstored)
        rows = store.current_for_raw(stored.raw_id)
    finally:
        store.close()
    assert [row.version for row in rows] == [1]
    assert all(row.version >= 1 for row in rows)

    # 3) 阶段侧不接受 < 1 的版本号（绝不为未入 store 的行编造证据身份）
    with pytest.raises(EvidenceStageError):
        claim_verification_requests(
            [
                {
                    "claim_id": stored.claim_key,
                    "claim_version": 0,
                    "raw_id": stored.raw_id,
                    "quote": stored.quote,
                    "kind": stored.kind,
                    "value": stored.value,
                    "confidence": stored.confidence,
                    "unit_char_start": stored.unit_char_start,
                    "unit_char_end": stored.unit_char_end,
                    "code_version": stored.code_version,
                    "config_version": stored.config_version,
                    "model_version": stored.model_version,
                }
            ]
        )

    # 4) 活对照 + 口径一致：库里那一行经两条路径得到**同一个**契约对象
    bridge = stored.as_proposed_claim()
    projected = proposed_claim_from_snapshot(
        {
            "claim_id": stored.claim_key,
            "claim_version": stored.version,
            "raw_id": stored.raw_id,
            "kind": stored.kind,
            "value": stored.value,
            "quote": stored.quote,
            "confidence": stored.confidence,
            "code_version": stored.code_version,
            "config_version": stored.config_version,
            "model_version": stored.model_version,
        }
    )
    assert projected.version == bridge.version == stored.version == 1


def test_claim_row_for_another_raw_fails_loudly(tmp_path: Path) -> None:
    """库里出现**本轮没读过**的 raw 的 claim ⇒ 响亮失败（不许越界校验）。

    用组合根的子类来构造这个状态：让 `_evidence_claims()` 额外吐出一条越界 claim。
    走的是**真实**的装配与执行路径（归一化产物、归档字节、阶段全部是真的），
    只有"库里多了一条别人的 claim"这一件事是注入的。
    """
    root = make_store_root(tmp_path)
    stray = classified_row(
        raw_id="raw_" + "f" * 32,
        quote=QUOTE_A,
        unit_char_start=0,
        unit_char_end=len(RAW_TEXT),
    )

    class FabricatingPipeline(Pipeline):
        def _evidence_claims(self, inputs):
            payload = dict(super()._evidence_claims(inputs))
            payload["claims"] = list(payload["claims"]) + [
                {
                    "claim_id": stray.claim_key,
                    "claim_version": 1,
                    "raw_id": stray.raw_id,
                    "unit_id": stray.unit_id,
                    "unit_kind": stray.unit_kind,
                    "unit_char_start": stray.unit_char_start,
                    "unit_char_end": stray.unit_char_end,
                    "kind": stray.kind,
                    "value": stray.value,
                    "quote": stray.quote,
                    "confidence": stray.confidence,
                    "code_version": stray.code_version,
                    "config_version": stray.config_version,
                    "model_version": stray.model_version,
                    "label_space_version": stray.label_space_version,
                }
            ]
            payload["claims"].sort(
                key=lambda item: (item["raw_id"], item["claim_id"], item["claim_version"])
            )
            return payload

    with evidence_pipeline(root, pipeline_class=FabricatingPipeline) as pipeline:
        with pytest.raises(TaskFailedError) as error:
            pipeline.run()
    assert isinstance(error.value.last_error, EvidenceStageError)
    assert "本轮归一化产物之外" in str(error.value.last_error)
    assert evidence_db_rows(root / "atlas.db") == []


# --------------------------------------------------------------------------- #
# 判据 8：`--read-only` 只校验不落库（wrote_to_store=false），且库里一行都不多
# --------------------------------------------------------------------------- #


def test_read_only_mode_verifies_without_writing(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = _collect_identity(report)["raw_id"]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_A,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
            )
        )

    with evidence_pipeline(root, evidence_read_only=True) as pipeline:
        report = pipeline.run()

    observed = _evidence_observed(report)
    assert observed["verified"] == 1
    assert observed["wrote_to_store"] is False
    assert observed["spans_written"] == 0
    assert observed["spans_in_store"] == 0
    # 只读模式把"已记录"计数当作"匹配数"用（什么都没写，所以不谎称写过了）
    assert observed["spans_unchanged"] == 1
    assert evidence_db_rows(root / "atlas.db") == []


# --------------------------------------------------------------------------- #
# 判据 9：组合根不得把 claims 漏出快照（否则新 claim 永远等不到校验）
# --------------------------------------------------------------------------- #


def test_pipeline_path_and_offline_path_project_the_same_claims(tmp_path: Path) -> None:
    """两条入口（流水线内 / 离线复核）必须投影出**同一份** claim 集合。

    流水线内：`evidence` 的快照来自 `normalize` 的**执行记录**。
    离线复核：快照的 `records` 由归档现状重建（`collect_evidence_input`）。
    两条路径读的是同一个 `proposed_claims`，因此投影结果必须一致 ——
    否则"流水线里校验过"与"离线复核过"就会指向不同的证据集合，
    而这正是本项目反复出现的"两条入口两个答案"缺陷形态。
    """
    root = make_store_root(tmp_path)
    offline_claims: List[dict] = []
    pipeline_claims: List[dict] = []
    with evidence_pipeline(root) as pipeline:
        first = pipeline.run()
        raw_id = _collect_identity(first)["raw_id"]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_A,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "1" * 32,
            )
        )
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_B,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
                unit_id="ent_" + "2" * 32,
            )
        )
        pipeline.proposed.record_claim(unclassified_row(raw_id=raw_id))

        # 流水线路径：真的跑一遍（第二轮），从执行记录里读 evidence 的快照
        second = pipeline.run()
        from atlas.compose import NODE_COLLECT

        inputs = pipeline.last_inputs
        assert inputs is not None
        rebuilt = NodeInputs(
            pipeline.build_graph(),
            pipeline._store,
            _config_snapshot(pipeline),
            roots={
                NODE_COLLECT: {
                    "channels": inputs[NODE_COLLECT].payload["channels"],
                    "window": inputs[NODE_COLLECT].payload["window"],
                }
            },
            extra=pipeline._extra_payload,
        )
        pipeline_claims = rebuilt[NODE_EVIDENCE].payload["claims"]

        # 离线路径：同一个 store、同一份库，走 `collect_evidence_input()`
        _stage, offline_inputs, _config = pipeline.collect_evidence_input()
        offline_claims = offline_inputs.payload["claims"]

    assert second.result(NODE_EVIDENCE).status != STATUS_SKIPPED
    keys = ("claim_id", "claim_version", "raw_id", "quote", "unit_char_start", "unit_char_end")
    assert [{k: item[k] for k in keys} for item in pipeline_claims] == [
        {k: item[k] for k in keys} for item in offline_claims
    ]
    assert len(pipeline_claims) == 2


def test_claims_projection_is_part_of_the_idempotency_input(tmp_path: Path) -> None:
    """claims 投影必须在**输入快照**里：否则新 claim 的幂等键不变 ⇒ 永远等不到校验。

    这条断言直接对着幂等键：同一次运行前后，`evidence` 节点的 `idempotency_key`
    必须从"空 claims"变成"含 1 条 claim"，且两者不相等。
    """
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        pipeline.run()
        before = _evidence_snapshot(pipeline)
        assert before.payload["claims"] == []
        assert before.payload["unclassified_rows"] == 0

        raw_id = _collect_identity_after_run(pipeline)
        row, _ = pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_A,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
            )
        )
        after = _evidence_snapshot(pipeline)
        key_before = pipeline.build_graph().task(NODE_EVIDENCE).idempotency_key(
            before, _config_snapshot(pipeline)
        )
        key_after = pipeline.build_graph().task(NODE_EVIDENCE).idempotency_key(
            after, _config_snapshot(pipeline)
        )

    assert after.payload["claims"] != before.payload["claims"]
    assert key_before != key_after, "新 claim 必须改变幂等键，否则它永远等不到校验"
    claim = after.payload["claims"][0]
    assert claim["claim_id"] == row.claim_key
    assert claim["claim_version"] == 1
    assert claim["quote"] == QUOTE_A
    assert claim["unit_char_start"] == 0 and claim["unit_char_end"] == len(RAW_TEXT)
    # 时刻与调用账**不得**进快照（它们变化不代表证据该重算）
    assert "created_at" not in claim and "output_digest" not in claim and "batch_id" not in claim


def _collect_identity_after_run(pipeline) -> str:
    from atlas.compose import NODE_COLLECT

    records = pipeline.last_inputs.artifacts_of(NODE_COLLECT)["identity"]["records"]
    return records[0]["raw"]["raw_id"]


def _evidence_snapshot(pipeline):
    """重建并返回 `evidence` 节点的输入快照（`Snapshot`，带 `.payload` 与 `.digest()`）。

    `Pipeline.last_inputs` 里的 `NodeInputs` 会缓存已算过的节点快照，因此新写入 claim
    之后必须新起一个 `NodeInputs` 才能看到投影的变化（生产路径上每次 `run()` 都是新的）。
    """
    from atlas.compose import NODE_COLLECT

    inputs = pipeline.last_inputs
    assert inputs is not None
    roots = {
        NODE_COLLECT: {
            "channels": inputs[NODE_COLLECT].payload["channels"],
            "window": inputs[NODE_COLLECT].payload["window"],
        }
    }
    rebuilt = NodeInputs(
        pipeline.build_graph(),
        pipeline._store,
        _config_snapshot(pipeline),
        roots=roots,
        extra=pipeline._extra_payload,
    )
    return rebuilt[NODE_EVIDENCE]


def _config_snapshot(pipeline):
    from atlas.compose.tasks import COMPOSE_CODE_VERSION
    from atlas.contracts import Snapshot

    versions = pipeline.versions()
    return Snapshot(
        payload={
            "actor": pipeline.config.actor,
            "on_channel_failure": pipeline.config.on_channel_failure,
            "require_nonempty_text": pipeline.config.require_nonempty_text,
            "code_version": COMPOSE_CODE_VERSION,
            "config_version": versions.config_version,
        }
    )
