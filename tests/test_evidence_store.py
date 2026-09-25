"""`evidence_spans` 持久化的判据（SPEC §2.3 / §2.10 / §4.2 T-107）。

判据逐条对应本文件里的测试：

1. 只增不改：**另一个** sqlite 连接直接 `UPDATE` / `DELETE` 必须被触发器拒绝
2. 幂等：同一 claim 同一版本重复校验，表里只有一条
3. 新版本 = 新记录：同一 claim 的新版本追加，不覆盖旧版本
4. 不留半成品：校验失败不写、写库失败回滚后表里仍是原样
5. 共用 DB 边界：只建 `evidence_spans`，不碰其它域的表
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.contracts import (
    EvidenceAnchor,
    ImmutabilityError,
    ProposedClaim,
    TaskVersions,
    VerificationStatus,
    content_sha256,
    raw_id_for,
)
from atlas.evidence import (
    QuoteVerification,
    SqliteEvidenceStore,
    VerificationOutcome,
    build_highlight,
    highlight_for_span,
    verify_and_record,
    verify_claim,
)

VERSIONS = TaskVersions(code_version="t107", config_version="v1", model_version="m1")
FIXED_TIME = datetime(2026, 9, 25, 12, 0, 0, 123456, tzinfo=timezone.utc)

RAW_TEXT = "Atlas 把证据锚定在原文上，坐标由确定性匹配算出，而不是让模型猜。"
RAW_BYTES = RAW_TEXT.encode("utf-8")
RAW_ID = raw_id_for("ch_test", "https://example.invalid/a", content_sha256(RAW_BYTES))
QUOTE = "证据锚定在原文上"

REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """测试一律用 `tmp_path`，绝不往仓库 `data/` 写（SPEC §2.10 共享存储根）。"""
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


def make_claim(quote: str = QUOTE, *, version: int = 1) -> ProposedClaim:
    claim = ProposedClaim.propose(
        raw_id=RAW_ID,
        kind="industry",
        value="ai",
        quote=quote,
        confidence=0.9,
        versions=VERSIONS,
    )
    return claim.with_version(version)


def make_store(tmp_path: Path) -> SqliteEvidenceStore:
    return SqliteEvidenceStore(tmp_path / "atlas.db", clock=lambda: FIXED_TIME)


def verified_span(tmp_path: Path):
    store = make_store(tmp_path)
    claim = make_claim()
    outcome = verify_and_record(store, claim, RAW_BYTES, content_type="text/plain")
    assert outcome.status is VerificationStatus.VERIFIED
    return store, claim, outcome


# --------------------------------------------------------------------------- #
# 基本落盘 / 幂等 / 版本
# --------------------------------------------------------------------------- #


def test_record_persists_verified_span(tmp_path):
    store, claim, outcome = verified_span(tmp_path)
    assert store.count() == 1

    span = store.span_for(claim.claim_id, claim.version)
    assert span is not None
    assert span.claim_id == claim.claim_id
    assert span.claim_version == 1
    assert span.raw_id == RAW_ID
    assert span.raw_sha256 == content_sha256(RAW_BYTES)
    assert span.quote == QUOTE
    assert (span.char_start, span.char_end) == (
        outcome.anchor.char_start,
        outcome.anchor.char_end,
    )
    assert span.normalized_end > span.normalized_start
    assert span.verified_at == FIXED_TIME
    assert store.spans_for_claim(claim.claim_id) == [span]
    assert store.spans_for_raw(RAW_ID) == [span]
    assert store.span_for("clm_missing", 1) is None

    # 重开连接后仍在（落盘，不是内存态）
    store.close()
    reopened = SqliteEvidenceStore(tmp_path / "atlas.db")
    assert reopened.count() == 1
    assert reopened.span_for(claim.claim_id, 1) == span
    reopened.close()


def test_same_claim_same_version_is_idempotent(tmp_path):
    store, claim, outcome = verified_span(tmp_path)
    first = store.span_for(claim.claim_id, 1)
    assert first is not None

    # 用**另一个** store 实例（不同连接、不同时刻）重复校验同一 claim 同一版本
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    other = SqliteEvidenceStore(tmp_path / "atlas.db", clock=lambda: later)
    again = verify_and_record(other, claim, RAW_BYTES, content_type="text/plain")
    assert again.status is VerificationStatus.VERIFIED

    assert other.count() == 1
    assert other.span_for(claim.claim_id, 1) == first  # 返回的是**已有行**，不是新行
    assert other.span_for(claim.claim_id, 1).verified_at == FIXED_TIME
    other.close()
    store.close()


def test_new_version_is_a_new_record(tmp_path):
    store, claim, _outcome = verified_span(tmp_path)

    bumped = claim.with_version(2)
    assert bumped.claim_id == claim.claim_id and bumped.version == 2
    outcome_v2 = verify_and_record(store, bumped, RAW_BYTES, content_type="text/plain")
    assert outcome_v2.status is VerificationStatus.VERIFIED

    assert store.count() == 2
    spans = store.spans_for_claim(claim.claim_id)
    assert [s.claim_version for s in spans] == [1, 2]
    # 旧版本没有被覆盖
    assert spans[0].char_start == spans[1].char_start
    assert spans[0].verified_at == spans[1].verified_at == FIXED_TIME
    store.close()


def test_failed_verification_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    claim = make_claim("这句话在原文里根本不存在")

    outcome = verify_and_record(store, claim, RAW_BYTES, content_type="text/plain")

    assert outcome.status is VerificationStatus.FAILED
    assert outcome.anchor is None
    assert store.count() == 0
    assert store.span_for(claim.claim_id, 1) is None
    assert store.spans_for_raw(RAW_ID) == []
    # 失败路径不留下任何未完成的事务
    assert store.connection.in_transaction is False
    store.close()


def test_record_returns_none_for_non_verified_outcome(tmp_path):
    store = make_store(tmp_path)
    outcome = verify_claim(make_claim("不存在"), RAW_BYTES, content_type="text/plain")
    assert store.record(outcome) is None
    assert store.count() == 0
    store.close()


def test_conflicting_anchor_for_same_version_is_rejected(tmp_path):
    store, claim, outcome = verified_span(tmp_path)
    before = store.span_for(claim.claim_id, 1)
    assert before is not None

    # 同一版本、不同内容（锚点被改）→ 只增不改，响亮失败
    shifted = outcome.verification.anchor
    assert shifted is not None
    forged_anchor = EvidenceAnchor.create(
        raw_id=shifted.raw_id,
        raw_sha256=shifted.raw_sha256,
        char_start=shifted.char_start + 1,
        char_end=shifted.char_end + 1,
    )
    forged_verification = QuoteVerification(
        raw_id=outcome.verification.raw_id,
        raw_sha256=outcome.verification.raw_sha256,
        quote=outcome.verification.quote,
        status=VerificationStatus.VERIFIED,
        anchor=forged_anchor,
        derived=outcome.verification.derived,
        normalized_text=outcome.verification.normalized_text,
        raw_text=outcome.verification.raw_text,
    )
    forged = VerificationOutcome(
        claim_id=outcome.claim_id,
        claim_version=outcome.claim_version,
        verification=forged_verification,
    )
    assert forged.anchor != before.anchor

    with pytest.raises(ImmutabilityError):
        store.record(forged)

    assert store.count() == 1
    assert store.span_for(claim.claim_id, 1) == before  # 原记录原样保留
    assert store.connection.in_transaction is False
    store.close()


# --------------------------------------------------------------------------- #
# 只增不改：另一个连接直接改也改不动
# --------------------------------------------------------------------------- #


def test_trigger_rejects_update_and_delete_from_another_connection(tmp_path):
    store, claim, _outcome = verified_span(tmp_path)
    db_path = tmp_path / "atlas.db"
    before = store.span_for(claim.claim_id, 1)
    assert before is not None

    other = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError) as update_error:
            other.execute("UPDATE evidence_spans SET char_start = 0, char_end = 1")
        assert "append-only" in str(update_error.value)

        with pytest.raises(sqlite3.IntegrityError) as delete_error:
            other.execute("DELETE FROM evidence_spans")
        assert "append-only" in str(delete_error.value)

        with pytest.raises(sqlite3.IntegrityError):
            other.execute("UPDATE evidence_spans SET quote = 'forged'")
    finally:
        other.close()

    # 表里还是原样
    assert store.count() == 1
    assert store.span_for(claim.claim_id, 1) == before
    store.close()

    fresh = sqlite3.connect(str(db_path))
    try:
        assert fresh.execute("SELECT COUNT(*) FROM evidence_spans").fetchone()[0] == 1
    finally:
        fresh.close()


def test_store_exposes_no_delete_api(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(NotImplementedError):
        store.delete("clm_x", 1)
    store.close()


def test_write_failure_rolls_back_without_partial_row(tmp_path, monkeypatch):
    """写库失败时不留半成品：插入撞上唯一约束 → 回滚 → 表里仍是原样。

    故障注入：让**代码层的幂等检查**看不见已有行（模拟并发/竞态），
    于是插入真的撞上 `PRIMARY KEY`，从而走到回滚分支。
    """
    store, claim, _outcome = verified_span(tmp_path)
    before = store.span_for(claim.claim_id, 1)
    assert before is not None

    monkeypatch.setattr(store, "span_for", lambda *args, **kwargs: None)
    conflicting = verify_claim(claim, RAW_BYTES, content_type="text/plain")
    assert conflicting.status is VerificationStatus.VERIFIED

    with pytest.raises(ImmutabilityError):
        store.record(conflicting)

    monkeypatch.undo()
    assert store.count() == 1
    assert store.span_for(claim.claim_id, 1) == before
    assert store.connection.in_transaction is False  # 回滚完成，没有悬挂事务
    store.close()


# --------------------------------------------------------------------------- #
# 共用 DB 的边界（SPEC §2.10 表归属）
# --------------------------------------------------------------------------- #


def test_store_only_creates_its_own_table(tmp_path):
    db_path = tmp_path / "atlas.db"
    store = SqliteEvidenceStore(db_path)
    try:
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert tables == {"evidence_spans"}, (
            "证据存储只允许创建自己登记过的表（SPEC §2.10 表归属）"
        )
        triggers = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert triggers == {
            "trg_evidence_spans_no_update",
            "trg_evidence_spans_no_delete",
        }
    finally:
        store.close()


def test_store_does_not_touch_other_domains_tables(tmp_path):
    db_path = tmp_path / "atlas.db"
    seed = sqlite3.connect(str(db_path))
    seed.execute(
        "CREATE TABLE raw_records (raw_id TEXT PRIMARY KEY, content_sha256 TEXT NOT NULL)"
    )
    seed.execute("INSERT INTO raw_records VALUES ('raw_1', 'deadbeef')")
    seed.execute("CREATE TABLE confirmed_labels (label_id TEXT PRIMARY KEY)")
    seed.execute("INSERT INTO confirmed_labels VALUES ('lbl_1')")
    seed.commit()
    seed.close()

    store, claim, _outcome = verified_span(tmp_path)
    assert store.count() == 1
    store.close()

    check = sqlite3.connect(str(db_path))
    try:
        assert check.execute("SELECT raw_id FROM raw_records").fetchall() == [("raw_1",)]
        assert check.execute("SELECT label_id FROM confirmed_labels").fetchall() == [("lbl_1",)]
        names = {
            row[0]
            for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"raw_records", "confirmed_labels", "evidence_spans"} <= names
    finally:
        check.close()


def test_default_path_is_not_written_by_tests(tmp_path):
    """显式传入 tmp_path 时，默认路径（仓库 data/store）绝不能被创建。"""
    store = SqliteEvidenceStore(tmp_path / "nested" / "atlas.db")
    assert store.db_path == tmp_path / "nested" / "atlas.db"
    assert store.db_path.exists()
    store.close()


# --------------------------------------------------------------------------- #
# 与展示层的衔接（T-109 读库 → 渲染高亮）
# --------------------------------------------------------------------------- #


def test_saved_span_renders_highlight_from_raw(tmp_path):
    store, claim, outcome = verified_span(tmp_path)
    span = store.span_for(claim.claim_id, claim.version)
    assert span is not None

    payload = highlight_for_span(span, RAW_BYTES, content_type="text/plain")
    assert payload.regions[0].raw_text == QUOTE
    assert payload.normalized_text == outcome.verification.normalized_text
    assert payload.status is VerificationStatus.VERIFIED

    # 与直接由校验结果构造的载荷一致（库里存的坐标没有引入偏差）
    assert payload == build_highlight(outcome.verification)
    store.close()
