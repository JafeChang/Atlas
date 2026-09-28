"""T-107 真实数据流证据（SPEC 硬规则 1 / §2.2 / §2.17）。

真实数据长什么样（2026-09-26 快照，主代理独立复核过）
----------------------------------------------------

`data/store/atlas.db` 里 `proposed_claims` 有 24 行，其中 **4 行 `classified`**
（`kdnuggets` feed 上的 4 个不同**单元**，覆盖 3 个 `ent_*` 条目区间），其余 20 行是
`unclassified` 降级行（`timeout` / `no_claim_extracted`）。这 4 行 quote 就是本任务
能拿到的最真实的证据输入 —— 它们**不是**为本测试造的。

本文件回答的是**真实数据上的接线问题**：

1. 4 行真实 `classified` 行，逐条按真实原文**字节**校验，断言
   `unit_char_start <= char_start < char_end <= unit_char_end`（锚点落在它自己的单元区间内）、
   `anchor.raw_id == row.raw_id`、`anchor.raw_sha256 == row.raw_sha256`
   —— 后两条正是 SPEC §2.2 的真值形状。
2. 落进**临时** `evidence_spans`，行数 == 通过校验的行数（**不落进真实库**：
   `data/store/atlas.db` 在这个测试前后**逐字节不变**）。
3. 节点（离线复算）与流水线（`NodeInputs` 路径）对**同一份真实 claim** 给出**同一个锚点** ——
   两条入口不得各说一套。
4. 负路径（用真实 raw_id + 原文里不存在的 quote）在真实数据上 `FAILED` 且**不落库**，
   同一存储里合法 quote 的活对照**真的**写进去了（跨连接可见）。
5. **真实数字如实打印**（不经 `-s` 也能在失败时看到）。

`data/` 不在 git 里 ⇒ 没有它时**自跳过**（照 `tests/test_search_realdata.py` 的先例），
干净 worktree 仍然退出码 0；真实数字由主工作区的那次运行给出。
"""

from __future__ import annotations

import hashlib
import html as _html
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from atlas.archive import open_archive
from atlas.cognition import (
    CLAIM_STATUS_CLASSIFIED,
    ProposedClaimRow,
    open_proposed_store,
)
from atlas.compose import NODE_EVIDENCE
from atlas.compose.tasks import proposed_claim_from_snapshot
from atlas.contracts import VerificationStatus, content_sha256
from atlas.evidence import (
    SqliteEvidenceStore,
    VerificationOutcome,
    verify_claim,
    verify_quote,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_DB = REPO_ROOT / "data" / "store" / "atlas.db"

#: 真实 raw 上**不可能**匹配到的一段文字（负路径；不是从模型输出里抄的）。
IMPOSSIBLE_QUOTE = "ATLAS_T107_PROBE_这段文字在真实原文里不存在_9f3c1a"

pytestmark = pytest.mark.skipif(
    not STORE_DB.is_file(),
    reason=(
        f"真实存储不存在（{STORE_DB}）：data/ 不进 git，干净检出里没有它。"
        "请在有真实数据的检出（主工作区）里跑本条"
    ),
)

print_prefix = "[T-107 真实]"


class RealClaim:
    """一行真实 `classified`（只读快照，测试用它重建契约对象）。"""

    def __init__(self, payload: Dict[str, object]) -> None:
        self.claim_key = str(payload["claim_key"])
        self.version = int(payload["version"])
        self.raw_id = str(payload["raw_id"])
        self.unit_id = str(payload["unit_id"])
        self.unit_kind = str(payload["unit_kind"])
        self.unit_char_start = int(payload["unit_char_start"])
        self.unit_char_end = int(payload["unit_char_end"])
        self.kind = str(payload["kind"])
        self.value = str(payload["value"])
        self.quote = str(payload["quote"])
        self.confidence = float(payload["confidence"])
        self.code_version = str(payload["code_version"])
        self.config_version = str(payload["config_version"])
        self.model_version = str(payload["model_version"])
        self.label_space_version = str(payload["label_space_version"])

    def row(self) -> ProposedClaimRow:
        return ProposedClaimRow(
            raw_id=self.raw_id,
            unit_id=self.unit_id,
            unit_kind=self.unit_kind,
            unit_char_start=self.unit_char_start,
            unit_char_end=self.unit_char_end,
            kind=self.kind,
            value=self.value,
            quote=self.quote,
            confidence=self.confidence,
            status=CLAIM_STATUS_CLASSIFIED,
            plan_digest="real",
            output_digest="real",
            code_version=self.code_version,
            config_version=self.config_version,
            model_version=self.model_version,
            label_space_version=self.label_space_version,
            input_digest="real",
            batch_id="bat_real",
            batch_position=0,
            batch_size=1,
            version=self.version,
            claim_key=self.claim_key,
        )

    def contract(self):
        """与 T-107 阶段还原契约对象的**同一实现**（`proposed_claim_from_snapshot`）。"""
        return proposed_claim_from_snapshot(
            {
                "claim_id": self.claim_key,
                "claim_version": self.version,
                "raw_id": self.raw_id,
                "kind": self.kind,
                "value": self.value,
                "quote": self.quote,
                "confidence": self.confidence,
                "code_version": self.code_version,
                "config_version": self.config_version,
                "model_version": self.model_version,
            }
        )


def real_claims() -> List[RealClaim]:
    """从真实 store **只读**读出全部 `classified` 行（按身份确定性排序）。

    每次调用都新开一个只读连接并关闭：测试不持有真实库的句柄，
    也就不会因为"忘了关"而在真实库上留下写事务。
    """
    connection = sqlite3.connect(str(STORE_DB), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT claim_key, version, raw_id, unit_id, unit_kind, unit_char_start, "
            "unit_char_end, kind, value, quote, confidence, code_version, config_version, "
            "model_version, label_space_version FROM proposed_claims "
            "WHERE status = ? ORDER BY raw_id, unit_char_start, unit_id, claim_key",
            (CLAIM_STATUS_CLASSIFIED,),
        ).fetchall()
    finally:
        connection.close()
    return [RealClaim(dict(row)) for row in rows]


def real_raw(raw_id: str) -> Tuple[bytes, str]:
    """从真实归档读回原文**字节**（只读，不信任何自述）。"""
    archive = open_archive(REPO_ROOT / "data" / "store")
    try:
        record = archive.get(raw_id)
        content = archive.get_content(raw_id)
    finally:
        archive.close()
    return content, record.content_sha256


def _db_digest() -> str:
    return hashlib.sha256(STORE_DB.read_bytes()).hexdigest()


def test_real_classified_claims_get_anchors_inside_their_unit_ranges(tmp_path: Path) -> None:
    """判据 1 + 2：真实 4 行 `classified` → 锚点落在单元区间内 → 落到**临时**证据库。"""
    claims = list(real_claims())
    assert claims, "真实 store 里应当至少有 1 行 classified（T-105 已交付）"
    # 真实快照（2026-09-26 实测）：4 行 classified，跨 3 个单元、1 个 raw。
    # 数字变了不失败（数据会长），但**单元区间必须真的包含条目文本**这一条照旧成立。
    print(
        f"{print_prefix} classified={len(claims)} "
        f"units={sorted({c.unit_id for c in claims})} "
        f"raws={sorted({c.raw_id for c in claims})} "
        f"ranges={sorted((c.unit_char_start, c.unit_char_end) for c in claims)}"
    )

    store = SqliteEvidenceStore(tmp_path / "evidence.db")
    verified = 0
    failed: List[str] = []
    try:
        by_raw: Dict[str, List[RealClaim]] = {}
        for claim in claims:
            by_raw.setdefault(claim.raw_id, []).append(claim)

        for raw_id, group in sorted(by_raw.items()):
            raw_bytes, raw_sha256 = real_raw(raw_id)
            assert content_sha256(raw_bytes) == raw_sha256, f"{raw_id} 归档字节与元数据不符"
            for claim in group:
                outcome = verify_claim(claim.contract(), raw_bytes)
                if outcome.status is not VerificationStatus.VERIFIED:
                    failed.append(f"{claim.claim_key}@{claim.version}")
                    print(
                        f"{print_prefix} [FAILED] {claim.claim_key}@{claim.version} "
                        f"raw={raw_id} quote={claim.quote[:60]!r}"
                    )
                    continue
                anchor = outcome.anchor
                assert anchor is not None
                # SPEC §2.2 的真值形状
                assert anchor.raw_id == claim.raw_id
                assert anchor.raw_sha256 == raw_sha256
                # 锚点必须落在**它自己的**单元区间内（本任务明确要求的那条断言）
                assert claim.unit_char_start <= anchor.char_start < anchor.char_end <= claim.unit_char_end, (
                    f"{claim.claim_key}: 锚点 [{anchor.char_start}, {anchor.char_end}) "
                    f"越出单元 [{claim.unit_char_start}, {claim.unit_char_end})"
                )
                # 独立重算：同一份 quote + 同一份字节 ⇒ 同一个锚点
                independent = verify_quote(
                    raw_id=claim.raw_id, raw_bytes=raw_bytes, quote=claim.quote
                )
                assert independent.anchor == anchor
                store.record(outcome)
                verified += 1
                raw_text = raw_bytes.decode("utf-8")
                print(
                    f"{print_prefix} [VERIFIED] {claim.claim_key}@{claim.version} raw={raw_id} "
                    f"unit=[{claim.unit_char_start},{claim.unit_char_end}) "
                    f"anchor=[{anchor.char_start},{anchor.char_end}) "
                    f"len={anchor.char_end - anchor.char_start} "
                    f"quote={claim.quote[:40]!r}"
                )
    finally:
        count = store.count()
        store.close()

    print(f"{print_prefix} 真实 classified 行 {len(claims)} 条；verified={verified} failed={len(failed)}")
    assert not failed, f"真实 quote 应当全部可校验，失败：{failed}"
    assert count == verified == len(claims), (count, verified, len(claims))
    assert verified >= 1


def test_real_store_is_untouched_and_temp_db_gets_the_rows(tmp_path: Path) -> None:
    """判据 2（后半）：真实库**逐字节不变**；证据只落在临时库。

    活对照：临时库里**真的**读得到行（跨连接），所以"真实库没变"不是因为没跑通。
    """
    claims = list(real_claims())
    before = _db_digest()
    before_mtime = STORE_DB.stat().st_mtime_ns

    temp_db = tmp_path / "evidence.db"
    store = SqliteEvidenceStore(temp_db)
    try:
        for claim in claims:
            raw_bytes, _ = real_raw(claim.raw_id)
            outcome = verify_claim(claim.contract(), raw_bytes)
            if outcome.status is VerificationStatus.VERIFIED:
                store.record(outcome)
    finally:
        store.close()

    # 跨连接读临时库（不是 store 自己的 count()）
    connection = sqlite3.connect(str(temp_db), isolation_level=None)
    try:
        n = connection.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
    finally:
        connection.close()

    after = _db_digest()
    print(f"{print_prefix} 真实库 sha256 前后：{before[:12]}… → {after[:12]}…；临时库 evidence_spans={n}")
    assert n >= 1, "活对照：临时证据库里必须有行，否则这条测试什么也没证明"
    assert after == before, "真实 store 被改动了（SHA256 变了）"
    assert STORE_DB.stat().st_mtime_ns == before_mtime, "真实 store 的 mtime 变了"


def test_node_and_pipeline_paths_agree_on_real_claims(tmp_path: Path) -> None:
    """判据 3：证据节点的**离线复核**入口在真实 claim 上给出与库中真值一致的锚点。

    用真实的存储根（`data/store`，归档里有 75 条真实 raw），但
    **执行记录库与证据库都指向 tmp_path**：这条测试**不往真实 store 写任何东西**
    （证据表在真实库里根本还不存在，这里也绝不创建它）。

    任何字段漂移（尤其 `content_type` 与单元区间）都会让锚点不同 ——
    那就成了"两条入口两个答案"。
    """
    from atlas.compose import build_pipeline
    from atlas.compose.tasks import proposed_claim_from_snapshot as _restore
    from atlas.evidence import SqliteEvidenceStore
    from atlas.runner import InMemoryExecutionRecordStore

    claims = list(real_claims())
    raw_id = claims[0].raw_id
    raw_bytes, _ = real_raw(raw_id)

    pipeline = build_pipeline(
        store_root=REPO_ROOT / "data" / "store",
        actor="t107-realdata",
        execution_store=InMemoryExecutionRecordStore(),
        evidence=SqliteEvidenceStore(tmp_path / "evidence.db"),
    )
    try:
        stage, inputs, config = pipeline.collect_evidence_input()
        assert stage.name == NODE_EVIDENCE
        assert len(pipeline.archive.all_raw_ids()) == 75, "真实归档应有 75 条 raw"
        projected = [item for item in inputs.payload["claims"] if item["raw_id"] == raw_id]
        assert projected, f"真实 claim 的 raw_id={raw_id} 应当出现在归档投影里"
        anchors = {}
        for item in projected:
            outcome = verify_claim(_restore(item), raw_bytes)
            anchors[item["claim_id"]] = (
                None
                if outcome.anchor is None
                else (outcome.anchor.char_start, outcome.anchor.char_end)
            )
    finally:
        pipeline.close()

    expected = {}
    for claim in claims:
        if claim.raw_id != raw_id:
            continue
        outcome = verify_claim(claim.contract(), raw_bytes)
        expected[claim.claim_key] = (
            None
            if outcome.anchor is None
            else (outcome.anchor.char_start, outcome.anchor.char_end)
        )

    print(
        f"{print_prefix} raw={raw_id} 离线投影 claim {len(anchors)} 条；"
        f"库中真实 claim {len(expected)} 条"
    )
    assert anchors == expected, (anchors, expected)


def test_realdata_negative_quote_is_failed_and_not_persisted_with_live_control(
    tmp_path: Path,
) -> None:
    """判据 4：真实 raw + **不存在**的 quote ⇒ `FAILED`、不落库；活对照真的写进去了。"""
    claims = list(real_claims())
    raw_id = claims[0].raw_id
    raw_bytes, raw_sha256 = real_raw(raw_id)

    db_path = tmp_path / "evidence.db"
    store = SqliteEvidenceStore(db_path)
    control = claims[0]
    try:
        bad = verify_quote(raw_id=raw_id, raw_bytes=raw_bytes, quote=IMPOSSIBLE_QUOTE)
        assert bad.status is VerificationStatus.FAILED
        assert bad.anchor is None and bad.derived is None
        # 负路径：`FAILED` 的 outcome 落库时**什么都不写**（返回 None）
        assert (
            store.record(
                VerificationOutcome(
                    claim_id=control.claim_key, claim_version=control.version, verification=bad
                )
            )
            is None
        )
        # 活对照：同一份字节、同一调用路径，合法 quote 必须成功并写入
        good = verify_quote(raw_id=raw_id, raw_bytes=raw_bytes, quote=control.quote)
        assert good.status is VerificationStatus.VERIFIED
        stored = store.record(
            VerificationOutcome(
                claim_id=control.claim_key, claim_version=control.version, verification=good
            )
        )
        assert stored is not None
    finally:
        store.close()

    # 跨连接（另开一个连接）读：坏的那条不在，好的那条在
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        total = connection.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0]
        bad_rows = connection.execute(
            "SELECT COUNT(*) FROM evidence_spans WHERE quote = ?", (IMPOSSIBLE_QUOTE,)
        ).fetchone()[0]
        good_rows = connection.execute(
            "SELECT COUNT(*) FROM evidence_spans WHERE quote = ?", (control.quote,)
        ).fetchone()[0]
    finally:
        connection.close()

    print(
        f"{print_prefix} 负路径：total={total} bad_rows={bad_rows} good_rows={good_rows} "
        f"raw_sha256={raw_sha256[:12]}…"
    )
    assert bad_rows == 0, "匹配不到的 quote 不得落库"
    assert good_rows == 1, "活对照：合法 quote 必须真的写进去了"
    assert total == 1


def test_real_unit_ranges_are_grounded_in_the_real_entry_text() -> None:
    """单元区间不是自述：它必须落在**真实原文**里、且每个单元都非空。

    这条把"锚点落在单元区间内"从"两个数字的大小比较"升级成
    "区间在真实原文里有真实文本"。**跨 raw 只查一个 raw 的费用**（读盘 + 切片）。
    """
    claims = list(real_claims())
    by_raw: Dict[str, List[RealClaim]] = {}
    for claim in claims:
        by_raw.setdefault(claim.raw_id, []).append(claim)

    checked = 0
    for raw_id, group in sorted(by_raw.items()):
        raw_bytes, _ = real_raw(raw_id)
        raw_text = raw_bytes.decode("utf-8")
        decoded = _html.unescape(raw_text)
        for claim in group:
            snippet = raw_text[claim.unit_char_start : claim.unit_char_end]
            assert snippet.strip(), (
                f"{claim.claim_key}: 单元区间 "
                f"[{claim.unit_char_start},{claim.unit_char_end}) 在真实原文里是空白"
            )
            stripped = claim.quote.strip()
            first, last = stripped[0], stripped[-1]
            # SPEC §2.2 的判据用**解码后**的窗口（实体场景下原文切片是实体字面量）
            window = snippet if first in snippet else _html.unescape(snippet)
            assert first in window and last in window, (
                f"{claim.claim_key}: 单元区间 "
                f"[{claim.unit_char_start},{claim.unit_char_end}) 里找不到这条 quote 的"
                f"首尾字符 {first!r}/{last!r}；窗口={window[:80]!r}"
            )
            checked += 1
        del raw_text, decoded
    print(f"{print_prefix} 单元区间落在真实原文上：checked={checked} raw组={len(by_raw)}")
    assert checked == len(claims) >= 1


def test_offline_entry_persists_real_evidence_without_touching_real_store(
    tmp_path: Path,
) -> None:
    """**一条命令那条路径**在真实 claim 上真的落库，且真实库逐字节不变。

    这里走的是 `evidence` 子命令用的同一条入口
    （`Pipeline.collect_evidence_input()` → `EvidenceStage.execute()`），
    但把**执行记录库 / 证据库 / 注册表与标签库**都指向 `tmp_path`
    （`data/store/runs/` 与真实 `atlas.db` 一个字都不动）。
    真实库的 SHA256 前后对比就是这条测试要的硬证据。
    """
    from atlas.compose import build_pipeline
    from atlas.evidence import SqliteEvidenceStore
    from atlas.labels import open_store as open_labels
    from atlas.registry import RegistryService, open_store as open_registry
    from atlas.runner import InMemoryExecutionRecordStore

    claims = list(real_claims())
    raw_id = claims[0].raw_id
    before = _db_digest()

    temp_db = tmp_path / "evidence.db"
    labels = open_labels(tmp_path / "labels.db")
    registry = RegistryService(open_registry(tmp_path / "registry.db", author="t107-realdata"))
    pipeline = build_pipeline(
        store_root=REPO_ROOT / "data" / "store",
        actor="t107-realdata",
        raw_ids=(raw_id,),
        execution_store=InMemoryExecutionRecordStore(),
        evidence=SqliteEvidenceStore(temp_db),
        labels=labels,
        registry=registry,
    )
    try:
        stage, inputs, config = pipeline.collect_evidence_input()
        output = stage.execute(inputs, config)
    finally:
        pipeline.close()
        labels.close()
        registry.store.close()

    observed = output.artifacts["observed"]
    print(
        f"{print_prefix} 离线入口：raws_in_scope={observed['raws_in_scope']} "
        f"classified_claims={observed['classified_claims']} verified={observed['verified']} "
        f"spans_written={observed['spans_written']} spans_in_store={observed['spans_in_store']}"
    )
    assert observed["classified_claims"] == len(claims)
    assert observed["verified"] == len(claims)
    assert observed["verification_failed"] == 0
    assert observed["spans_written"] == len(claims)

    # 跨连接读临时证据库：每一条真实 claim 都有一行，且坐标自洽
    connection = sqlite3.connect(str(temp_db), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT claim_id, claim_version, raw_id, raw_sha256, char_start, char_end "
            "FROM evidence_spans ORDER BY claim_id"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == len(claims)
    for row in rows:
        assert row["raw_id"] == raw_id
        assert row["char_start"] < row["char_end"]

    after = _db_digest()
    print(f"{print_prefix} 真实库 sha256：{before[:12]}… → {after[:12]}…")
    assert after == before, "真实 store 被改动了（SHA256 变了）"


def test_real_numbers_snapshot_is_reported() -> None:
    """判据 5：把真实数字如实打印出来（人读的报告，不写进任何文档）。"""
    claims = list(real_claims())
    statuses: Dict[str, int] = {}
    for claim in claims:
        raw_bytes, _ = real_raw(claim.raw_id)
        outcome = verify_claim(claim.contract(), raw_bytes)
        statuses[outcome.status.value] = statuses.get(outcome.status.value, 0) + 1
    print(
        f"{print_prefix} 真实 classified 行 {len(claims)} 条，校验状态分布 "
        f"{statuses}，raw 去重 {len({c.raw_id for c in claims})} 个"
    )
    assert sum(statuses.values()) == len(claims)
