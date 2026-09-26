"""T-105 判据 3 / 4 / 5 / 6 / 7 / 11：**`proposed_claims` 存储层的不变量**。

判据全文（**先定义后实现**的唯一出处）见 `tests/_t105_criteria.py`，本文件覆盖：

- **判据 3**：Proposed 可覆写 = 追加新版本 + 保留版本链；`UPDATE` / `DELETE`
  被 SQL 触发器拒绝（**不是**靠调用方自觉）。每条否定断言都带**活对照**。
- **判据 4**：本层**一行都不写** `confirmed_labels`（同库同会话里，该表计数与内容不变）。
- **判据 5 / 6 / 11**：版本三元组非空；未分类行不得携带取值/引用且必须有理由
  （`CHECK` 在存储层强制）；`raw_records` 只增不改。
- **判据 7**：同内容重复写入是明确的"无变化"（版本不推进）。
- **表归属**：本模块只建自己的三张表，**不碰**别人的表（SPEC §2.10）。

这些测试全部指向 `tmp_path`，**绝不写仓库 `data/`**。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.cognition import (
    CLAIM_STATUS_CLASSIFIED,
    CLAIM_STATUS_UNATTRIBUTED,
    CLAIM_STATUS_UNCLASSIFIED,
    ProposalRunRow,
    ProposedClaimRow,
    ProposedStoreError,
    SqliteProposedStore,
    claim_key_for,
    output_digest_for,
)
from atlas.cognition.store import (
    CLAIMS_TABLE,
    META_TABLE,
    RUNS_TABLE,
    SCHEMA_VERSION,
)
from tests._t105_criteria import CRITERIA

RAW_ID = "raw_" + "a" * 32
UNIT_ID = "ent_" + "b" * 32
PLAN_DIGEST = "p" * 64
INPUT_DIGEST = "i" * 64


def _classified_row(
    *,
    unit_id: str = UNIT_ID,
    value: str = "machine-learning",
    quote: str = "a quote from the document",
    confidence: float = 0.8,
    batch_id: str = "bat_" + "c" * 32,
) -> ProposedClaimRow:
    return ProposedClaimRow(
        raw_id=RAW_ID,
        unit_id=unit_id,
        unit_kind="entry",
        unit_char_start=10,
        unit_char_end=60,
        entry_index=3,
        title="A title",
        kind="industry",
        value=value,
        quote=quote,
        confidence=confidence,
        status=CLAIM_STATUS_CLASSIFIED,
        output_digest=output_digest_for(
            value=value,
            quote=quote,
            confidence=confidence,
            status=CLAIM_STATUS_CLASSIFIED,
            reason=None,
        ),
        plan_digest=PLAN_DIGEST,
        code_version="cognition-extract-prompt/1",
        config_version="cognition-config/1",
        model_version="deepseek-flash",
        label_space_version="cfg/7#deadbeef",
        input_digest=INPUT_DIGEST,
        batch_id=batch_id,
        batch_position=0,
        batch_size=4,
    )


def _unclassified_row(reason: str = "timeout") -> ProposedClaimRow:
    return ProposedClaimRow(
        raw_id=RAW_ID,
        unit_id=UNIT_ID,
        unit_kind="entry",
        unit_char_start=10,
        unit_char_end=60,
        entry_index=3,
        title="A title",
        kind="industry",
        value=None,
        quote=None,
        confidence=None,
        status=CLAIM_STATUS_UNCLASSIFIED,
        reason=reason,
        output_digest=output_digest_for(
            value=None,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason=reason,
        ),
        plan_digest=PLAN_DIGEST,
        code_version="cognition-extract-prompt/1",
        config_version="cognition-config/1",
        model_version="deepseek-flash",
        label_space_version="cfg/7#deadbeef",
        input_digest=INPUT_DIGEST,
        batch_id="bat_" + "c" * 32,
        batch_position=0,
        batch_size=4,
    )


@pytest.fixture()
def store(tmp_path) -> SqliteProposedStore:
    return SqliteProposedStore(db_path=tmp_path / "atlas.db")


# =========================================================================== #
# 表归属与建表
# =========================================================================== #


def test_creates_only_its_own_tables_and_registers_schema_version(store) -> None:
    """只建自己的三张表（SPEC §2.10 的表归属），并登记本域 schema 版本。"""
    names = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    }
    assert names == {CLAIMS_TABLE, RUNS_TABLE, META_TABLE}
    row = store.connection.execute(
        f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION


def test_refuses_a_same_named_table_with_a_different_shape(tmp_path) -> None:
    """同库里有**结构不同**的同名表 ⇒ 响亮失败（不静默复用别人的表）。

    活对照：同一个构造路径对**自己建的**库必须成功（见上一个测试）。
    """
    path = tmp_path / "atlas.db"
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE {CLAIMS_TABLE} (something_else TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ProposedStoreError) as excinfo:
        SqliteProposedStore(db_path=path)
    assert "结构与本模块预期不符" in str(excinfo.value)


def test_reopen_is_idempotent(tmp_path) -> None:
    """重开同一个库不报错、不丢数据（活对照：形状校验不是"谁都拒绝"）。"""
    path = tmp_path / "atlas.db"
    first = SqliteProposedStore(db_path=path)
    first.record_claim(_classified_row())
    first.close()
    second = SqliteProposedStore(db_path=path)
    assert second.claim_count() == 1
    second.close()


# =========================================================================== #
# 判据 3 / 7：版本链与幂等
# =========================================================================== #


def test_criterion_7_same_row_twice_is_a_no_op(store) -> None:
    """同内容重复写入 ⇒ 明确的"无变化"：返回既有行，版本**不**推进。"""
    first, written = store.record_claim(_classified_row())
    assert written is True and first.version == 1 and first.supersedes == 0
    again, written2 = store.record_claim(_classified_row())
    assert written2 is False
    assert again.version == 1
    assert len(store.history(first.claim_key)) == 1


def test_criterion_3_changed_content_appends_a_new_version(store) -> None:
    """内容变 ⇒ 新版本，`supersedes` 指向前一版，旧版本保留（可覆写 + 版本链）。"""
    row = _classified_row()
    first, _ = store.record_claim(row)
    changed = _classified_row(confidence=0.42)
    assert changed.claim_key == first.claim_key, "身份只由输入决定，置信度变化不换身份"
    second, written = store.record_claim(changed)
    assert written is True
    assert second.version == 2 and second.supersedes == 1
    history = store.history(first.claim_key)
    assert [item.version for item in history] == [1, 2]
    assert history[0].confidence == pytest.approx(0.8)
    assert history[1].confidence == pytest.approx(0.42)
    assert store.head(first.claim_key).version == 2


def test_criterion_3_different_units_get_different_identities(store) -> None:
    """**单元参与身份**：同一条 quote 出现在两个条目里时，两条证据不得塌成一个 id。"""
    a = _classified_row(unit_id="ent_" + "1" * 32)
    b = _classified_row(unit_id="ent_" + "2" * 32)
    assert a.claim_key != b.claim_key
    first, _ = store.record_claim(a)
    second, _ = store.record_claim(b)
    assert first.claim_key != second.claim_key
    assert store.claim_count() == 2
    assert {row.unit_id for row in store.current_for_raw(RAW_ID)} == {
        "ent_" + "1" * 32,
        "ent_" + "2" * 32,
    }


def test_criterion_3_update_is_refused_by_trigger_with_live_control(store) -> None:
    """**否定性断言 + 活对照**：`UPDATE` 被触发器拒绝；同一连接上的 `SELECT` 必须成功。"""
    row, _ = store.record_claim(_classified_row())
    conn = store.connection
    # 活对照：读是允许的（证明连接与语法都是对的）
    assert conn.execute(
        f"SELECT value FROM {CLAIMS_TABLE} WHERE claim_key = ?", (row.claim_key,)
    ).fetchone()[0] == "machine-learning"
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(
            f"UPDATE {CLAIMS_TABLE} SET value = 'tampered' WHERE claim_key = ?",
            (row.claim_key,),
        )
    assert "append-only" in str(excinfo.value)
    # 再读一次：内容没有被改动
    assert conn.execute(
        f"SELECT value FROM {CLAIMS_TABLE} WHERE claim_key = ?", (row.claim_key,)
    ).fetchone()[0] == "machine-learning"


def test_criterion_3_delete_is_refused_by_trigger_with_live_control(store) -> None:
    """`DELETE` 被触发器拒绝；活对照是"插入别的行"仍然成功。"""
    row, _ = store.record_claim(_classified_row())
    conn = store.connection
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(f"DELETE FROM {CLAIMS_TABLE} WHERE claim_key = ?", (row.claim_key,))
    assert "append-only" in str(excinfo.value)
    other, written = store.record_claim(_classified_row(unit_id="ent_" + "3" * 32))
    assert written is True and other.claim_key != row.claim_key
    assert store.claim_count() == 2


def test_history_preserves_every_version(store) -> None:
    """版本链完整：连续三次不同输出 ⇒ 三个版本，`supersedes` 依次相连。"""
    row = _classified_row()
    store.record_claim(row)
    store.record_claim(_classified_row(confidence=0.5))
    store.record_claim(_classified_row(confidence=0.25))
    history = store.history(row.claim_key)
    assert [item.version for item in history] == [1, 2, 3]
    assert [item.supersedes for item in history] == [0, 1, 2]
    assert {item.confidence for item in history} == {0.8, 0.5, 0.25}


# =========================================================================== #
# 判据 6：降级 = 未分类，在**存储层**也强制
# =========================================================================== #


def test_criterion_6_unclassified_row_needs_a_reason(store) -> None:
    """未分类行没有理由 ⇒ 构造期就拒绝（活对照：带理由的能构造成功）。"""
    assert _unclassified_row().reason == "timeout"
    with pytest.raises(ProposedStoreError):
        ProposedClaimRow(
            raw_id=RAW_ID,
            unit_id=UNIT_ID,
            unit_kind="entry",
            unit_char_start=10,
            unit_char_end=60,
            kind="industry",
            value=None,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason=None,
            output_digest="d" * 64,
            plan_digest=PLAN_DIGEST,
            code_version="c",
            config_version="k",
            model_version="m",
            label_space_version="ls",
            input_digest=INPUT_DIGEST,
            batch_id="bat",
            batch_position=0,
            batch_size=1,
        )


def test_criterion_6_db_check_refuses_a_claim_without_quote(store) -> None:
    """**存储层**的 `CHECK`：分类行必须有逐字引用 —— 绕过本模块直插也不行。"""
    conn = store.connection
    columns = (
        "claim_key, version, supersedes, raw_id, unit_id, unit_kind, unit_char_start, "
        "unit_char_end, entry_index, title, kind, value, quote, confidence, status, "
        "reason, detail, code_version, config_version, model_version, "
        "label_space_version, plan_digest, output_digest, batch_id, batch_position, "
        "batch_size, provider, model, credential_route, input_digest, source, created_at"
    )
    values = (
        "pcl_" + "f" * 32, 1, 0, RAW_ID, UNIT_ID, "entry", 0, 5, None, "", "industry",
        "machine-learning", None, None, CLAIM_STATUS_CLASSIFIED, None, "", "c", "k",
        "m", "ls", PLAN_DIGEST, "d" * 64, "bat", 0, 1, "", "", "", INPUT_DIGEST, "",
        "2026-01-01T00:00:00+00:00",
    )
    placeholders = ", ".join("?" for _ in values)
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(
            f"INSERT INTO {CLAIMS_TABLE} ({columns}) VALUES ({placeholders})", values
        )
    assert "CHECK" in str(excinfo.value).upper()
    # 活对照：同一条 INSERT 补上 value/quote/confidence 后必须成功
    ok_values = list(values)
    ok_values[12] = "a verbatim quote"
    ok_values[13] = 0.5
    conn.execute(
        f"INSERT INTO {CLAIMS_TABLE} ({columns}) VALUES ({placeholders})", ok_values
    )
    assert store.claim_count() == 1


def test_criterion_6_unclassified_row_may_not_carry_a_value(store) -> None:
    """未分类行带取值 = "猜测" ⇒ 构造期拒绝（§2.14 决策四）。"""
    with pytest.raises(ProposedStoreError):
        ProposedClaimRow(
            raw_id=RAW_ID,
            unit_id=UNIT_ID,
            unit_kind="entry",
            unit_char_start=10,
            unit_char_end=60,
            kind="industry",
            value="guessed-label",
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason="timeout",
            output_digest="d" * 64,
            plan_digest=PLAN_DIGEST,
            code_version="c",
            config_version="k",
            model_version="m",
            label_space_version="ls",
            input_digest=INPUT_DIGEST,
            batch_id="bat",
            batch_position=0,
            batch_size=1,
        )


def test_audit_rows_may_not_carry_a_quote(store) -> None:
    """审计行（未归属）可以记 value，但**不得**带 quote（不得当证据）。"""
    with pytest.raises(ProposedStoreError):
        ProposedClaimRow(
            raw_id=RAW_ID,
            unit_id="unattributed:bat:0",
            unit_kind="unattributed",
            unit_char_start=0,
            unit_char_end=1,
            kind="industry",
            value="machine-learning",
            quote="a quote that could not be attributed",
            confidence=0.9,
            status=CLAIM_STATUS_UNATTRIBUTED,
            reason="unattributed_quote",
            output_digest="d" * 64,
            plan_digest=PLAN_DIGEST,
            code_version="c",
            config_version="k",
            model_version="m",
            label_space_version="ls",
            input_digest=INPUT_DIGEST,
            batch_id="bat",
            batch_position=0,
            batch_size=1,
        )


def test_criterion_6_reason_histogram_is_available(store) -> None:
    """未分类必须**可审计**：理由直方图能查（不能只有一个总数）。"""
    store.record_claim(_unclassified_row("timeout"))
    import dataclasses

    store.record_claim(
        dataclasses.replace(
            _unclassified_row("unreachable_model"),
            unit_id="ent_" + "9" * 32,
            claim_key="",  # 改身份字段 ⇒ 让构造期按新内容重算身份
            output_digest=output_digest_for(
                value=None,
                quote=None,
                confidence=None,
                status=CLAIM_STATUS_UNCLASSIFIED,
                reason="unreachable_model",
            ),
        )
    )
    counts = store.reason_counts()
    assert counts == {"timeout": 1, "unreachable_model": 1}
    assert store.status_counts() == {CLAIM_STATUS_UNCLASSIFIED: 2}


# =========================================================================== #
# 判据 5：版本三元组
# =========================================================================== #


def test_criterion_5_versions_are_required(store) -> None:
    """版本三元组为空 ⇒ 构造期拒绝（活对照：非空的三元组能构造成功）。"""
    assert _classified_row().model_version == "deepseek-flash"
    with pytest.raises(ProposedStoreError):
        ProposedClaimRow(
            raw_id=RAW_ID,
            unit_id=UNIT_ID,
            unit_kind="entry",
            unit_char_start=10,
            unit_char_end=60,
            kind="industry",
            value="machine-learning",
            quote="q",
            confidence=0.5,
            status=CLAIM_STATUS_CLASSIFIED,
            output_digest="d" * 64,
            plan_digest=PLAN_DIGEST,
            code_version="c",
            config_version="k",
            model_version="",
            label_space_version="ls",
            input_digest=INPUT_DIGEST,
            batch_id="bat",
            batch_position=0,
            batch_size=1,
        )


# =========================================================================== #
# 运行账（§3 幂等 / §2.14 启动成本）
# =========================================================================== #


def test_run_ledger_is_idempotent_and_records_cost(store) -> None:
    """运行账：同一 `(unit_id, plan_digest)` 重复写入 ⇒ 无变化；token 数如实记录。"""
    run = ProposalRunRow.make(
        unit_id=UNIT_ID,
        raw_id=RAW_ID,
        plan_digest=PLAN_DIGEST,
        status=CLAIM_STATUS_UNCLASSIFIED,
        reason="timeout",
        code_version="c",
        config_version="k",
        model_version="m",
        label_space_version="ls",
        batch_id="bat_" + "c" * 32,
        batch_size=4,
        calls=1,
        input_tokens=1234,
        output_tokens=56,
        reasoning_tokens=40,
        elapsed_ms=7000,
    )
    stored, written = store.record_run(run)
    assert written is True and stored.input_tokens == 1234
    assert store.already_planned(UNIT_ID, PLAN_DIGEST) is True
    again, written2 = store.record_run(run)
    assert written2 is False, "同一计划的重复运行账必须是'无变化'"
    assert store.run_count() == 1
    assert again.reasoning_tokens == 40

    # 换计划摘要（= 换配置/换输入）⇒ 新的一条
    other = ProposalRunRow.make(
        unit_id=UNIT_ID,
        raw_id=RAW_ID,
        plan_digest="q" * 64,
        status=CLAIM_STATUS_CLASSIFIED,
        code_version="c",
        config_version="k",
        model_version="m",
        label_space_version="ls",
    )
    _stored, written3 = store.record_run(other)
    assert written3 is True
    assert store.run_count() == 2
    assert store.already_planned(UNIT_ID, "q" * 64) is True
    assert store.already_planned(UNIT_ID, "z" * 64) is False


def test_run_ledger_update_is_refused(store) -> None:
    """运行账也是只增不改（活对照：`SELECT` 成功）。"""
    run = ProposalRunRow.make(
        unit_id=UNIT_ID,
        raw_id=RAW_ID,
        plan_digest=PLAN_DIGEST,
        status=CLAIM_STATUS_UNCLASSIFIED,
        reason="timeout",
        code_version="c",
        config_version="k",
        model_version="m",
        label_space_version="ls",
    )
    stored, _ = store.record_run(run)
    conn = store.connection
    assert conn.execute(
        f"SELECT status FROM {RUNS_TABLE} WHERE run_id = ?", (stored.run_id,)
    ).fetchone()[0] == CLAIM_STATUS_UNCLASSIFIED
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"UPDATE {RUNS_TABLE} SET status = 'classified' WHERE run_id = ?",
            (stored.run_id,),
        )


# =========================================================================== #
# 判据 4 / 11：不碰 Confirmed，不碰 Raw
# =========================================================================== #


def test_criterion_4_and_11_does_not_touch_confirmed_or_raw(tmp_path) -> None:
    """本任务的存储层**一行都不写** `confirmed_labels` / `raw_records`。

    做法：在一个库里预先建好这两张表并写入一条记录，然后用本模块打开、写入提议行，
    最后逐字段比对它们**完全相同**（不是"数量相同"，是内容相同）。
    """
    path = tmp_path / "atlas.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE raw_records (
            raw_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, endpoint TEXT NOT NULL,
            content_sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
            fetched_at TEXT NOT NULL, http_status INTEGER
        );
        CREATE TABLE confirmed_labels (
            label_id TEXT PRIMARY KEY, raw_id TEXT NOT NULL, label_key TEXT NOT NULL,
            label_value TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TRIGGER trg_raw_records_no_update BEFORE UPDATE ON raw_records
        BEGIN SELECT RAISE(ABORT, 'raw_records is append-only: UPDATE is forbidden (SPEC 2.10)'); END;
        """
    )
    conn.execute(
        "INSERT INTO raw_records VALUES (?,?,?,?,?,?,?)",
        (RAW_ID, "chan", "https://example.com/feed", "a" * 64, 10, "2026-01-01T00:00:00+00:00", 200),
    )
    conn.execute(
        "INSERT INTO confirmed_labels VALUES (?,?,?,?,?,?)",
        ("lbl_" + "1" * 32, RAW_ID, "industry", "machine-learning", "me", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    raw_before = conn.execute("SELECT * FROM raw_records").fetchall()
    labels_before = conn.execute("SELECT * FROM confirmed_labels").fetchall()
    conn.close()

    store = SqliteProposedStore(db_path=path)
    store.record_claim(_classified_row())
    store.record_claim(_unclassified_row("timeout"))
    store.close()

    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT * FROM raw_records").fetchall() == raw_before
    assert conn.execute("SELECT * FROM confirmed_labels").fetchall() == labels_before
    # 活对照：本模块自己的表**确实**被写了（否则上面的"没变"可能只是因为什么都没做）
    assert conn.execute(f"SELECT COUNT(*) FROM {CLAIMS_TABLE}").fetchone()[0] == 2
    # 活对照 2：raw_records 的只增不改触发器仍然在自己的位置上生效
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE raw_records SET byte_length = 99")
    conn.close()


def test_claim_key_and_output_digest_are_deterministic() -> None:
    """身份与输出摘要都是确定性复算（换一次调用顺序不影响）。"""
    a = claim_key_for(
        raw_id=RAW_ID,
        unit_id=UNIT_ID,
        kind="industry",
        value="ai",
        quote="q",
        status=CLAIM_STATUS_CLASSIFIED,
        reason=None,
    )
    b = claim_key_for(
        raw_id=RAW_ID,
        unit_id=UNIT_ID,
        kind="industry",
        value="ai",
        quote="q",
        status=CLAIM_STATUS_CLASSIFIED,
        reason=None,
    )
    assert a == b and a.startswith("pcl_") and len(a) == 36
    d1 = output_digest_for(
        value="ai", quote="q", confidence=0.5, status=CLAIM_STATUS_CLASSIFIED, reason=None
    )
    d2 = output_digest_for(
        value="ai", quote="q", confidence=0.5, status=CLAIM_STATUS_CLASSIFIED, reason=None
    )
    d3 = output_digest_for(
        value="ai", quote="q", confidence=0.6, status=CLAIM_STATUS_CLASSIFIED, reason=None
    )
    assert d1 == d2 != d3


def test_claim_key_rejects_broken_inputs() -> None:
    """非法输入抛错（活对照：合法输入成功 —— 见上一个测试）。"""
    with pytest.raises(ProposedStoreError):
        claim_key_for(
            raw_id="",
            unit_id=UNIT_ID,
            kind="industry",
            value="ai",
            quote="q",
            status=CLAIM_STATUS_CLASSIFIED,
            reason=None,
        )
    with pytest.raises(ProposedStoreError):
        claim_key_for(
            raw_id=RAW_ID,
            unit_id=UNIT_ID,
            kind="industry",
            value="ai",
            quote="q",
            status="made-up-status",
            reason=None,
        )
    with pytest.raises(ProposedStoreError):
        claim_key_for(
            raw_id=RAW_ID,
            unit_id=UNIT_ID,
            kind="industry",
            value="ai",
            quote=None,
            status=CLAIM_STATUS_CLASSIFIED,
            reason=None,
        )


def test_default_db_path_is_the_shared_store_and_no_io(tmp_path) -> None:
    """默认库文件路径 = SPEC §2.10 的 `data/store/atlas.db`（不产生任何 I/O）。"""
    from atlas.cognition import store as store_module

    assert str(store_module.resolve_db_path(None)) == str(Path("data/store/atlas.db"))
    assert str(store_module.resolve_db_path(tmp_path / "x.db")) == str(tmp_path / "x.db")


def test_proposed_claim_contract_bridge_rejects_unclassified_row(store) -> None:
    """契约桥只对**分类行**成立：未分类行没有"内容"可以装进 `ProposedClaim`。

    活对照：分类行必须真的转换成功，且字段与契约一致。
    """
    classified, _ = store.record_claim(_classified_row())
    claim = classified.as_proposed_claim()
    assert claim.claim_id == classified.claim_key
    assert claim.raw_id == RAW_ID
    assert claim.kind == "industry"
    assert claim.value == "machine-learning"
    assert claim.quote == "a quote from the document"
    assert claim.versions.model_version == "deepseek-flash"
    assert claim.version == 1

    unclassified, _ = store.record_claim(_unclassified_row("timeout"))
    with pytest.raises(ProposedStoreError):
        unclassified.as_proposed_claim()


def test_criteria_reference_is_stable() -> None:
    assert set(CRITERIA) == set(range(1, 16))
