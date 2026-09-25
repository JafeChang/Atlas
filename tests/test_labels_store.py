"""T-108 判据：人工打标存储（Confirmed）的 SQLite 实现。

**这份文件的核心是差分测试**：同一组场景分别驱动
`atlas.contracts.ConfirmedStore`（内存版，契约权威）与
`atlas.labels.SqliteConfirmedStore`（落盘版），逐条断言
「返回值 / 异常类型 / 幂等 / latest_value 语义」完全一致。
这是"实现了同一契约"的最强证据——比列举 SQLite 自己的行为更有说服力。

差分之外的单侧用例（SQLite 独有、故意比内存版**更严**或内存版根本没有的层）：

- **只增不改由 SQL 触发器强制**（SPEC §2.10）：绕过本模块直接 `UPDATE` / `DELETE`，
  乃至用**另一个连接**改，都必须被 `RAISE(ABORT)` 拒绝；
- **锚点必须锚在同一份原文上**（SPEC §2.1）：`anchor.raw_id != label.raw_id` 抛
  `AnchorError`；**`label_id` 必须与内容一致**（SPEC §4.1 T-002 的 ID 策略），
  否则抛 `IdError`。内存契约没有这两条检查，所以它们**不进**差分场景集——
  差异是单向的（SQLite ⊇ 契约），绝不会更松；
- **不动别人的表**：不建 `store_meta`、不碰已存在的 `config_versions`；
  同名但结构不同的表必须响亮失败（`VersionError`）；
- **重开一致**：关闭后重开，`all_for` / `latest_value` / `count` 与关闭前逐字段相同。

测试一律用 `tmp_path`，绝不写仓库 `data/`（最后一条用例专门盯这件事）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.contracts import (
    AnchorError,
    ConfirmedLabel,
    ConfirmedStore,
    EvidenceAnchor,
    IdError,
    ProposedClaim,
    TaskVersions,
    UnverifiedEvidenceError,
    VerificationStatus,
    VersionError,
    content_sha256,
    label_id_for,
    raw_id_for,
)
from atlas.labels import SqliteConfirmedStore, open_store
from atlas.labels.sqlite_store import (
    DEFAULT_DB_PATH,
    TABLE_NAME,
    resolve_db_path,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
RAW_A = raw_id_for("ch_a", "https://example.com/a", content_sha256(b"body-a"))
RAW_B = raw_id_for("ch_b", "https://example.com/b", content_sha256(b"body-b"))
RAW_Z = raw_id_for("ch_z", "https://example.com/z", content_sha256(b"body-z"))
SHA_A = content_sha256(b"body-a")
SHA_Z = content_sha256(b"body-z")
VERSIONS = TaskVersions(code_version="code-1", config_version="cfg-1", model_version="m-1")


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def make_label(
    *,
    raw_id: str = RAW_A,
    key: str = "industry",
    value: str = "cs.LG",
    actor: str = "alice",
    at: datetime = T0,
    from_claim_id: str | None = None,
    anchor: EvidenceAnchor | None = None,
) -> ConfirmedLabel:
    """直接构造记录（`created_at` 可控——差分测试需要确定的时间序）。"""
    return ConfirmedLabel(
        label_id=label_id_for(raw_id, key, value, actor),
        raw_id=raw_id,
        label_key=key,
        label_value=value,
        actor=actor,
        from_claim_id=from_claim_id,
        anchor=anchor,
        created_at=at,
    )


def make_claim(
    *,
    raw_id: str = RAW_Z,
    kind: str = "industry",
    value: str = "cs.CV",
    quote: str = "本文提出了一种新的视觉方法",
    status: VerificationStatus = VerificationStatus.VERIFIED,
    anchor_raw_id: str | None = None,
    sha: str = SHA_Z,
) -> ProposedClaim:
    claim = ProposedClaim.propose(
        raw_id=raw_id,
        kind=kind,
        value=value,
        quote=quote,
        confidence=0.9,
        versions=VERSIONS,
    )
    if status is VerificationStatus.VERIFIED:
        anchor = EvidenceAnchor.create(
            raw_id=anchor_raw_id or raw_id,
            raw_sha256=sha,
            char_start=4,
            char_end=17,
        )
        return claim.with_verification(status, anchor)
    return claim.with_verification(status, None)


def pin_created_at(label: ConfirmedLabel, at: datetime) -> ConfirmedLabel:
    """把 `created_at` 钉成确定值。

    契约的 `ConfirmedLabel.from_proposal()` 不接受 clock 参数（`created_at` 默认
    `_utcnow()`），而差分测试必须在两次运行之间比较逐字段相同的记录，
    所以这里**按字段重建**（不改契约、不改任何已存在的文件）。
    """
    payload = label.model_dump()
    payload["created_at"] = at
    return ConfirmedLabel(**payload)


# --------------------------------------------------------------------------- #
# 差分测试脚手架
# --------------------------------------------------------------------------- #
def _norm(value: object) -> object:
    if isinstance(value, ConfirmedLabel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    return value


class _Recorder:
    """把"调一次契约方法"的结果记成可比较的一行日志。

    这里**必须**捕获异常才能做差分（要比较的就是异常类型本身）。捕获只用于
    **记录**，绝不吞掉：类型进日志、消息留给失败断言看，不会被当成成功。
    """

    def __init__(self) -> None:
        self.log: list[dict[str, object]] = []

    def step(self, name: str, call) -> None:  # noqa: ANN001 - 测试内部小工具
        try:
            outcome = call()
        except Exception as exc:  # noqa: BLE001 - 差分测试需要捕获以比较类型
            self.log.append(
                {
                    "step": name,
                    "outcome": "raise",
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
        else:
            self.log.append({"step": name, "outcome": "ok", "value": _norm(outcome)})


def _scenarios(store) -> list[dict[str, object]]:  # noqa: ANN001 - 契约任一实现
    """同一组场景，逐条覆盖 SPEC §2.1 / §2.3 的语义。"""
    rec = _Recorder()
    a1 = make_label(value="cs.LG", actor="alice", at=T0)
    a1_later = make_label(value="cs.LG", actor="alice", at=T0 + timedelta(hours=5))
    a2 = make_label(value="cs.CV", actor="alice", at=T0 + timedelta(hours=1))
    b1 = make_label(raw_id=RAW_B, value="cs.CL", actor="bob", at=T0 + timedelta(hours=2))
    tie = make_label(value="stat.ML", actor="carol", at=T0)  # 与 a1 同一时刻：考验并列语义
    other_key = make_label(key="topic", value="cs.LG", actor="alice", at=T0 + timedelta(hours=3))

    # --- 空库 ---------------------------------------------------------------
    rec.step("count/empty", store.count)
    rec.step("all_for/unknown-raw", lambda: store.all_for(RAW_A))
    rec.step("latest_value/unknown-raw", lambda: store.latest_value(RAW_A, "industry"))

    # --- 首次写入 + 幂等 -----------------------------------------------------
    rec.step("add/a1", lambda: store.add(a1))
    rec.step("add/a1-again", lambda: store.add(a1))
    # 同 label_id、不同 created_at：契约要求返回**库里那条**，不是刚构造的那条
    rec.step("add/a1-later-object", lambda: store.add(a1_later))
    rec.step("count/one", store.count)
    rec.step("all_for/a", lambda: store.all_for(RAW_A))
    rec.step("latest_value/a-industry", lambda: store.latest_value(RAW_A, "industry"))
    rec.step("latest_value/a-missing-key", lambda: store.latest_value(RAW_A, "nope"))

    # --- 改判 = 新记录 -------------------------------------------------------
    rec.step("add/a2-new-value", lambda: store.add(a2))
    rec.step("count/two", store.count)
    rec.step("all_for/a-two", lambda: store.all_for(RAW_A))
    rec.step("latest_value/a-newest", lambda: store.latest_value(RAW_A, "industry"))
    rec.step("all_for/other-raw-empty", lambda: store.all_for(RAW_B))

    # --- 跨文档隔离 ----------------------------------------------------------
    rec.step("add/b1", lambda: store.add(b1))
    rec.step("count/three", store.count)
    rec.step("all_for/b", lambda: store.all_for(RAW_B))
    rec.step("latest_value/a-unaffected-by-b", lambda: store.latest_value(RAW_A, "industry"))

    # --- 维度 / 人 都是身份的一部分 -------------------------------------------
    rec.step("add/other-key", lambda: store.add(other_key))
    rec.step("latest_value/a-topic", lambda: store.latest_value(RAW_A, "topic"))
    rec.step("keys-distinct", lambda: store.all_for(RAW_A))

    # --- created_at 并列：取先写入的那条（内存 max / SQL rowid ASC）-----------
    rec.step("add/tie", lambda: store.add(tie))
    rec.step("latest_value/a-tie", lambda: store.latest_value(RAW_A, "industry"))

    # --- 确认 AI 提议：证据未校验必须响亮失败（SPEC §2.3）--------------------
    unverified = make_claim(status=VerificationStatus.UNVERIFIED)
    failed = make_claim(status=VerificationStatus.FAILED)
    rec.step(
        "from_proposal/unverified",
        lambda: store.add(ConfirmedLabel.from_proposal(unverified, actor="alice")),
    )
    rec.step(
        "from_proposal/failed",
        lambda: store.add(ConfirmedLabel.from_proposal(failed, actor="alice")),
    )
    rec.step("count/unchanged-after-rejects", store.count)

    # --- 确认 AI 提议：证据已校验 → 带 from_claim_id 与 anchor ----------------
    verified = make_claim()
    confirmed = pin_created_at(
        ConfirmedLabel.from_proposal(verified, actor="alice"), T0 + timedelta(hours=4)
    )
    rec.step("from_proposal/verified", lambda: store.add(confirmed))
    rec.step("from_proposal/verified-again", lambda: store.add(confirmed))
    rec.step("count/after-confirm", store.count)
    rec.step("all_for/z", lambda: store.all_for(RAW_Z))
    rec.step("latest_value/z", lambda: store.latest_value(RAW_Z, "industry"))

    # --- 人工直判不需要证据（1A），也照样能覆写自己 --------------------------
    human_override = make_label(raw_id=RAW_Z, value="cs.CL", actor="alice", at=T0 + timedelta(days=1))
    rec.step("add/human-override", lambda: store.add(human_override))
    rec.step("latest_value/z-overridden", lambda: store.latest_value(RAW_Z, "industry"))
    rec.step("all_for/z-two", lambda: store.all_for(RAW_Z))

    return rec.log


# --------------------------------------------------------------------------- #
# 1. 差分测试：内存契约 vs SQLite 实现
# --------------------------------------------------------------------------- #
def test_differential_memory_vs_sqlite(tmp_path: Path) -> None:
    memory = ConfirmedStore()
    sqlite_store = SqliteConfirmedStore(db_path=tmp_path / "diff.db")
    try:
        memory_log = _scenarios(memory)
        sqlite_log = _scenarios(sqlite_store)
    finally:
        sqlite_store.close()

    assert len(memory_log) == len(sqlite_log)
    assert len(memory_log) >= 30, "场景集太薄，差分测试会变成空跑"

    for mem, sql in zip(memory_log, sqlite_log, strict=True):
        assert mem["step"] == sql["step"], "两个实现的场景集必须完全对应"
        assert mem["outcome"] == sql["outcome"], (
            f"{mem['step']}: 内存版 {mem['outcome']} / SQLite 版 {sql['outcome']}"
            f"（内存：{mem.get('message', mem.get('value'))!r}，"
            f"SQLite：{sql.get('message', sql.get('value'))!r}）"
        )
        if mem["outcome"] == "ok":
            assert mem["value"] == sql["value"], f"{mem['step']}: 返回值不同"
        else:
            assert mem["error"] == sql["error"], f"{mem['step']}: 异常类型不同"


def test_differential_covers_the_four_contract_methods(tmp_path: Path) -> None:
    """四条契约方法都被差分场景真的调用过（防止"场景集漂移后悄悄失去覆盖"）。"""
    log = _scenarios(SqliteConfirmedStore(db_path=tmp_path / "cover.db"))
    steps = {entry["step"] for entry in log}
    for needed in (
        "add/a1",
        "all_for/a",
        "latest_value/a-industry",
        "count/one",
        "from_proposal/unverified",
    ):
        assert needed in steps
    assert any(entry["outcome"] == "raise" for entry in log), "必须覆盖异常路径"


@pytest.mark.parametrize(
    "method",
    ["add", "all_for", "latest_value", "count"],
)
def test_contract_methods_exist_on_both(tmp_path: Path, method: str) -> None:
    sqlite_store = SqliteConfirmedStore(db_path=tmp_path / "surface.db")
    try:
        assert callable(getattr(sqlite_store, method))
    finally:
        sqlite_store.close()
    assert callable(getattr(ConfirmedStore(), method))


@pytest.mark.parametrize(
    "forbidden",
    ["update", "delete", "remove", "clear", "set", "put_updated", "overwrite"],
)
def test_neither_store_exposes_mutation_api(tmp_path: Path, forbidden: str) -> None:
    """SPEC §2.3：Confirmed **没有** update / delete 这条路径，两个实现都不能有。"""
    sqlite_store = SqliteConfirmedStore(db_path=tmp_path / "noapi.db")
    try:
        assert not hasattr(ConfirmedStore(), forbidden)
        assert not hasattr(sqlite_store, forbidden)
        assert not hasattr(open_store(tmp_path / "wrapper.db"), forbidden)
    finally:
        sqlite_store.close()


# --------------------------------------------------------------------------- #
# 2. 只增不改：SQL 触发器强制（SPEC §2.3 / §2.10）
# --------------------------------------------------------------------------- #
def test_app_update_is_rejected_by_trigger(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "trg.db")
    try:
        store.add(make_label())
        with pytest.raises(sqlite3.IntegrityError) as err:
            store.connection.execute(
                f"UPDATE {TABLE_NAME} SET label_value = 'tampered' WHERE raw_id = ?",
                (RAW_A,),
            )
        assert "append-only" in str(err.value)
    finally:
        store.close()


def test_app_delete_is_rejected_by_trigger(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "trg-del.db")
    try:
        store.add(make_label())
        with pytest.raises(sqlite3.IntegrityError) as err:
            store.connection.execute(f"DELETE FROM {TABLE_NAME}")
        assert "append-only" in str(err.value)
    finally:
        store.close()


def test_trigger_survives_raw_connection_and_data_is_intact(tmp_path: Path) -> None:
    """触发器写在库文件里，不是包在 Python 外壳里：**另一个连接**改也必须失败。"""
    db = tmp_path / "trg-raw.db"
    store = SqliteConfirmedStore(db_path=db)
    store.add(make_label())
    store.close()

    with sqlite3.connect(str(db)) as foreign:
        for statement in (
            f"UPDATE {TABLE_NAME} SET actor = 'mallory'",
            f"DELETE FROM {TABLE_NAME} WHERE label_id LIKE 'lbl_%'",
            f"UPDATE {TABLE_NAME} SET label_id = 'lbl_forged'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                foreign.execute(statement)
        remaining = foreign.execute(f"SELECT label_value FROM {TABLE_NAME}").fetchall()
    assert remaining == [("cs.LG",)]

    reopened = SqliteConfirmedStore(db_path=db)
    try:
        assert reopened.count() == 1
        assert reopened.latest_value(RAW_A, "industry") == "cs.LG"
    finally:
        reopened.close()


def test_append_only_triggers_are_installed(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "trg-names.db")
    try:
        rows = store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
    finally:
        store.close()
    assert [str(row["name"]) for row in rows] == [
        "trg_confirmed_labels_no_delete",
        "trg_confirmed_labels_no_update",
    ]


# --------------------------------------------------------------------------- #
# 3. 锚点：文档级（1A）+ 必须锚在同一份原文上（§2.1）
# --------------------------------------------------------------------------- #
def test_human_label_needs_no_evidence(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "human.db")
    try:
        label = ConfirmedLabel.human(raw_id=RAW_A, label_key="industry", label_value="cs.LG", actor="me")
        assert label.anchor is None, "1A：人工直判是文档级判断，不要求 AI 证据"
        stored = store.add(label)
        assert stored.anchor is None
        assert store.all_for(RAW_A)[0].anchor is None
    finally:
        store.close()


def test_label_anchor_is_bound_to_raw_id_not_to_derived_artifacts() -> None:
    """SPEC §2.1 / §2.4：标签只锚 raw_id，不锚块 ID / 页码 / 归一化偏移。"""
    payload = ConfirmedLabel.human(
        raw_id=RAW_A, label_key="industry", label_value="cs.LG", actor="me"
    ).model_dump()
    assert set(payload) == {
        "label_id",
        "raw_id",
        "label_key",
        "label_value",
        "actor",
        "from_claim_id",
        "anchor",
        "created_at",
    }
    for forbidden in ("block_id", "page_number", "normalized_start", "normalized_end"):
        assert forbidden not in payload


def test_confirmed_from_proposal_keeps_claim_evidence(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "proposal.db")
    try:
        claim = make_claim()
        stored = store.add(ConfirmedLabel.from_proposal(claim, actor="alice"))
        assert stored.from_claim_id == claim.claim_id
        assert stored.anchor is not None
        assert stored.anchor.raw_id == claim.raw_id == RAW_Z
        assert store.all_for(RAW_Z)[0].anchor == claim.anchor
    finally:
        store.close()


def test_rejects_anchor_pointing_at_a_different_document(tmp_path: Path) -> None:
    """SQLite 版比内存契约**更严**的一条：锚点不得指向另一份原文。

    差异是单向的（存储层 ⊇ 契约），所以它不进差分场景集。
    """
    store = SqliteConfirmedStore(db_path=tmp_path / "mismatch.db")
    try:
        stray = EvidenceAnchor.create(
            raw_id=RAW_B, raw_sha256=content_sha256(b"body-b"), char_start=0, char_end=5
        )
        forged = make_label(raw_id=RAW_A, anchor=stray)
        with pytest.raises(AnchorError) as err:
            store.add(forged)
        assert "raw_id" in str(err.value)
        assert store.count() == 0, "拒绝必须是彻底拒绝：不留半成品行"
    finally:
        store.close()


def test_rejects_label_id_that_does_not_match_content(tmp_path: Path) -> None:
    """同样比内存契约更严的一条：标识必须由内容寻址算得（与 `RawStore.put` 同道理）。"""
    store = SqliteConfirmedStore(db_path=tmp_path / "forged-id.db")
    try:
        forged = ConfirmedLabel(
            label_id="lbl_manual_forgery",
            raw_id=RAW_A,
            label_key="industry",
            label_value="cs.LG",
            actor="alice",
            created_at=T0,
        )
        with pytest.raises(IdError):
            store.add(forged)
        assert store.count() == 0
    finally:
        store.close()


def test_sqlite_layer_is_strictly_stricter_never_looser(tmp_path: Path) -> None:
    """把"差异方向是单向的"钉进测试：内存契约放行的坏记录，存储层拒绝。

    内存 `ConfirmedStore.add()` 不检查"锚点是否锚在同一份原文上"，SQLite 层检查。
    这是**有意**的加强（SPEC §2.1 要求标签只锚 `raw_id`），因此这类场景不进差分集；
    本用例的存在保证它也不会反向漂移成"存储层更松"。
    """
    stray_anchor = EvidenceAnchor.create(
        raw_id=RAW_B, raw_sha256=content_sha256(b"body-b"), char_start=0, char_end=5
    )
    memory = ConfirmedStore()
    memory.add(make_label(raw_id=RAW_A, anchor=stray_anchor))
    assert memory.count() == 1  # 内存契约当前不检查（已知差异，非本模块的缺陷）

    store = SqliteConfirmedStore(db_path=tmp_path / "stricter.db")
    try:
        with pytest.raises(AnchorError):
            store.add(make_label(raw_id=RAW_A, anchor=stray_anchor))
        assert store.count() == 0
    finally:
        store.close()


def test_unverified_proposal_never_reaches_the_store(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "unverified.db")
    try:
        claim = make_claim(status=VerificationStatus.UNVERIFIED)
        with pytest.raises(UnverifiedEvidenceError):
            store.add(ConfirmedLabel.from_proposal(claim, actor="alice"))
        assert store.count() == 0
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 4. 改判 = 新记录，latest_value 取最新
# --------------------------------------------------------------------------- #
def test_revision_creates_a_new_row_and_latest_follows_it(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "revise.db")
    try:
        first = store.add(make_label(value="cs.LG", at=T0))
        second = store.add(make_label(value="cs.CV", at=T0 + timedelta(minutes=1)))
        assert first.label_id != second.label_id, "改判必须产生新的 label_id（新记录）"
        assert store.count() == 2, "旧记录不得被覆盖或删除（Confirmed 只增不改）"
        assert store.latest_value(RAW_A, "industry") == "cs.CV"
        assert [lbl.label_value for lbl in store.all_for(RAW_A)] == ["cs.LG", "cs.CV"]
    finally:
        store.close()


def test_latest_value_tie_is_earliest_written(tmp_path: Path) -> None:
    """`created_at` 并列时与内存契约的 `max` 同解：取先写入的那条。"""
    memory = ConfirmedStore()
    sqlite_store = SqliteConfirmedStore(db_path=tmp_path / "tie.db")
    try:
        for store in (memory, sqlite_store):
            store.add(make_label(value="cs.LG", actor="alice", at=T0))
            store.add(make_label(value="cs.CV", actor="bob", at=T0))
        assert sqlite_store.latest_value(RAW_A, "industry") == memory.latest_value(RAW_A, "industry")
    finally:
        sqlite_store.close()


# --------------------------------------------------------------------------- #
# 5. 重开一致
# --------------------------------------------------------------------------- #
def test_reopen_preserves_everything(tmp_path: Path) -> None:
    db = tmp_path / "reopen.db"
    store = SqliteConfirmedStore(db_path=db)
    claim = make_claim()
    store.add(make_label(value="cs.LG", at=T0))
    store.add(make_label(value="cs.CV", at=T0 + timedelta(hours=1)))
    store.add(make_label(raw_id=RAW_B, value="cs.CL", actor="bob", at=T0))
    store.add(ConfirmedLabel.from_proposal(claim, actor="alice"))
    before_all = store.all_for(RAW_A)
    before_latest = store.latest_value(RAW_A, "industry")
    before_count = store.count()
    before_z = store.all_for(RAW_Z)
    store.close()

    reopened = SqliteConfirmedStore(db_path=db)
    try:
        assert reopened.count() == before_count
        assert reopened.all_for(RAW_A) == before_all
        assert reopened.latest_value(RAW_A, "industry") == before_latest
        assert reopened.all_for(RAW_Z) == before_z
        assert reopened.latest_value(RAW_Z, "industry") == "cs.CV"
        assert reopened.latest_value(RAW_B, "industry") == "cs.CL"
        # 重开后再 add 同一条：仍然幂等，返回库里那条（含 anchor / created_at）
        again = reopened.add(ConfirmedLabel.from_proposal(claim, actor="alice"))
        assert again == before_z[0]
        assert reopened.count() == before_count
    finally:
        reopened.close()


def test_label_store_wrapper_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "wrapper.db"
    with open_store(db) as store:
        store.add(make_label())
        assert store.count() == 1
        assert store.latest_value(RAW_A, "industry") == "cs.LG"
        assert store.has(make_label().label_id)
    with open_store(db) as store:
        assert store.count() == 1
        assert store.all_for(RAW_A)[0].label_value == "cs.LG"
        assert store.keys_for(RAW_A) == ["industry"]
        assert store.raw_ids() == [RAW_A]


# --------------------------------------------------------------------------- #
# 6. 只碰自己的表（SPEC §2.10 表归属）
# --------------------------------------------------------------------------- #
def test_creates_only_its_own_table(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "own.db")
    try:
        rows = store.connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        store.close()
    created = {(str(row["type"]), str(row["name"])) for row in rows}
    assert ("table", TABLE_NAME) in created
    assert {name for kind, name in created if kind == "table"} == {TABLE_NAME}
    # T-101 的 `store_meta` 不归本模块 —— 连读都不读，更不能建
    assert "store_meta" not in {name for _, name in created}
    assert ("index", "idx_confirmed_labels_raw") in created
    assert ("index", "idx_confirmed_labels_raw_key") in created


def test_leaves_foreign_tables_untouched(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO store_meta(key, value) VALUES ('schema_version', '1')")
        conn.execute("CREATE TABLE config_versions (version INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO config_versions(version, note) VALUES (1, 'genesis')")
        conn.commit()

    store = SqliteConfirmedStore(db_path=db)
    try:
        store.add(make_label())
        meta = store.connection.execute("SELECT key, value FROM store_meta").fetchall()
        versions = store.connection.execute("SELECT version, note FROM config_versions").fetchall()
    finally:
        store.close()
    assert [(str(r["key"]), str(r["value"])) for r in meta] == [("schema_version", "1")]
    assert [(int(r["version"]), str(r["note"])) for r in versions] == [(1, "genesis")]


def test_same_named_table_of_a_different_shape_fails_loudly(tmp_path: Path) -> None:
    db = tmp_path / "collide.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE confirmed_labels (label_id TEXT PRIMARY KEY, note TEXT)")
        conn.commit()
    with pytest.raises(VersionError) as err:
        SqliteConfirmedStore(db_path=db)
    assert TABLE_NAME in str(err.value)


# --------------------------------------------------------------------------- #
# 7. 路径可配置 + 不写仓库 data/
# --------------------------------------------------------------------------- #
def test_db_path_is_configurable(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deep" / "atlas.db"
    store = SqliteConfirmedStore(db_path=db)
    try:
        assert store.db_path == db
        assert db.exists(), "父目录应被自动创建"
    finally:
        store.close()
    assert open_store(tmp_path / "as-string.db").db_path == tmp_path / "as-string.db"
    assert open_store(str(tmp_path / "as-str.db")).db_path == tmp_path / "as-str.db"


def test_default_path_matches_spec_layout() -> None:
    """默认位置就是 SPEC §2.10 的 `data/store/atlas.db`（只断言策略，不产生 I/O）。"""
    assert DEFAULT_DB_PATH == Path("data/store/atlas.db")
    assert resolve_db_path(None) == DEFAULT_DB_PATH
    assert resolve_db_path("x/y.db") == Path("x/y.db")


def _fingerprint(path: Path) -> tuple[bool, int, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns)


def test_repo_data_is_never_written(tmp_path: Path) -> None:
    """本用例是"测试绝不写仓库 `data/`"的守门人。"""
    repo_db = Path(__file__).resolve().parents[1] / "data" / "store" / "atlas.db"
    before = _fingerprint(repo_db)
    store = SqliteConfirmedStore(db_path=tmp_path / "guard.db")
    try:
        store.add(make_label())
        assert store.count() == 1
    finally:
        store.close()
    assert _fingerprint(repo_db) == before
