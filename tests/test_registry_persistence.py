"""T-101 持久化测试（SPEC §2.10）。

要点：
- 库文件路径可配置；默认值是 `data/store/atlas.db`（测试**绝不**写仓库的 data/）
- 重开连接后 `current` / `history` / `rollback` 与 T-004 的内存实现语义一致
- 版本历史**只增不改**：靠 SQL 触发器，而不是靠约定
- 非法变更在 SQLite 路径上同样被拒绝（校验没有旁路）
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.contracts import (
    InvalidContractStateError,
    NotFoundError,
    VersionError,
)
from atlas.registry import ChangeKind, Channel, FetchSpec, Industry, RegistryMutation
from atlas.registry.persistence import (
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
    SqliteConfigStore,
)

BASE = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def make_clock():
    state = {"n": -1}

    def clock() -> datetime:
        state["n"] += 1
        return BASE + timedelta(minutes=state["n"])

    return clock


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def channel(channel_id: str = "openai-blog", **overrides: object) -> Channel:
    payload: dict = {
        "id": channel_id,
        "industry_id": "ai",
        "type": "rss",
        "endpoint": f"https://example.com/{channel_id}.xml",
        "fetch_spec": FetchSpec(type="rss"),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(overrides)
    return Channel(**payload)


def seed_store(db_path: Path, **kwargs: object) -> SqliteConfigStore:
    params: dict = {
        "author": "alice",
        "industries": (industry("ai", name="人工智能"),),
        "channels": (channel(),),
        "clock": make_clock(),
        "db_path": db_path,
    }
    params.update(kwargs)
    return SqliteConfigStore(**params)  # type: ignore[arg-type]


def reopen(db_path: Path, **kwargs: object) -> SqliteConfigStore:
    params: dict = {"author": "alice", "db_path": db_path}
    params.update(kwargs)
    return SqliteConfigStore(**params)  # type: ignore[arg-type]


# --- 路径可配置 / 默认值 -------------------------------------------------------


def test_default_db_path_matches_spec_layout() -> None:
    assert DEFAULT_DB_PATH == Path("data/store/atlas.db")


def test_explicit_path_is_used(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "custom.db"
    store = seed_store(db)
    assert db.exists()
    assert store.db_path == db
    store.close()


def test_configured_path_never_touches_repo_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式给出路径时，默认位置（仓库内 `data/store/atlas.db`）绝不被创建。

    把 cwd 换到 `tmp_path` 再跑，这样断言与**仓库当前是否干净**无关：
    默认路径会被解析到 tmp_path 下，任何"忘了用显式路径"的写入都会立刻现形。
    """
    monkeypatch.chdir(tmp_path)
    store = seed_store(tmp_path / "atlas.db")
    store.create_industry(industry("biotech", name="生物技术"))
    store.close()
    assert not (tmp_path / "data" / "store" / "atlas.db").exists()
    assert (tmp_path / "atlas.db").exists()


def test_default_path_is_used_when_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 `db_path` 时才用默认值；默认值就是 SPEC §2.10 的 `data/store/atlas.db`。"""
    monkeypatch.chdir(tmp_path)
    store = SqliteConfigStore(author="alice", clock=make_clock())
    assert store.db_path == DEFAULT_DB_PATH
    assert (tmp_path / "data" / "store" / "atlas.db").exists()
    store.close()


# --- genesis ------------------------------------------------------------------


def test_genesis_version_is_one_and_carries_author_and_diff(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    genesis = store.version(1)
    assert genesis.version == 1
    assert genesis.parent_version is None
    assert genesis.author == "alice"
    assert genesis.kind is ChangeKind.CREATE
    assert genesis.created_at == BASE
    assert {d.object_id for d in genesis.diff} == {"ai", "openai-blog"}
    assert store.current_version == 1
    store.close()


def test_invalid_initial_registry_is_rejected_before_any_write(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    with pytest.raises(InvalidContractStateError, match="不存在"):
        seed_store(db, channels=(channel("orphan", industry_id="ghost"),))
    # 拒绝发生在落盘之前：库里不留半成品版本
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT COUNT(*) FROM config_versions").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


# --- 提交与版本链 -------------------------------------------------------------


def test_create_industry_produces_new_version_with_author_and_timestamp(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    record = store.create_industry(industry("biotech", name="生物技术", keywords=("crispr",)))
    assert record.version == 2
    assert record.parent_version == 1
    assert record.kind is ChangeKind.CREATE
    assert record.author == "alice"
    assert record.created_at == BASE + timedelta(minutes=1)
    assert record.diff[0].object_id == "biotech"
    assert store.current_version == 2
    store.close()


def test_commit_with_explicit_author_is_recorded(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    record = store.create_industry(
        industry("biotech", name="生物技术"), author="carol", note="按需求新增"
    )
    assert record.author == "carol"
    assert record.note == "按需求新增"
    store.close()


def test_batch_commit_is_marked_batch(tmp_path: Path) -> None:
    from atlas.registry import RegistryMutation

    store = seed_store(tmp_path / "atlas.db")
    record = store.commit(
        [
            RegistryMutation(
                kind="create",
                object_kind="industry",
                payload=industry("biotech", name="生物").payload(),
            ),
            RegistryMutation(kind="delete", object_kind="channel", object_id="openai-blog"),
        ]
    )
    assert record.kind is ChangeKind.BATCH
    assert {d.object_id for d in record.diff} == {"biotech", "openai-blog"}
    store.close()


def test_invalid_mutation_is_rejected_and_leaves_no_partial_version(tmp_path: Path) -> None:
    from atlas.registry import RegistryMutation

    db = tmp_path / "atlas.db"
    store = seed_store(db)
    with pytest.raises(InvalidContractStateError, match="不存在"):
        store.commit(
            [
                RegistryMutation(
                    kind="create",
                    object_kind="industry",
                    payload=industry("biotech", name="生物").payload(),
                ),
                RegistryMutation(
                    kind="create",
                    object_kind="channel",
                    payload=channel("bad", industry_id="ghost").payload(),
                ),
            ]
        )
    assert store.current_version == 1
    store.close()

    reopened = reopen(db)
    assert reopened.current_version == 1
    assert [i.id for i in reopened.current.industries] == ["ai"]
    reopened.close()


def test_duplicate_id_and_no_op_update_are_rejected(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    with pytest.raises(InvalidContractStateError, match="已存在"):
        store.create_industry(industry("ai", name="重复"))
    with pytest.raises(InvalidContractStateError, match="无字段变化"):
        store.update_industry(industry("ai", name="人工智能"))
    with pytest.raises(InvalidContractStateError, match="没有任何变更"):
        store.commit([])
    assert store.current_version == 1
    store.close()


def test_unknown_version_lookup_raises_not_found(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    with pytest.raises(NotFoundError):
        store.version(42)
    with pytest.raises(NotFoundError):
        store.snapshot(42)
    store.close()


# --- 重开连接后一致 -----------------------------------------------------------


def test_reopen_preserves_current_history_and_digest(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物技术"))
    store.update_industry(industry("biotech", name="生物技术（改名）"))
    before_digest = store.current.digest()
    before_versions = [(v.version, v.author, v.kind, v.created_at) for v in store.history()]
    before_config_version = store.current.config_version
    store.close()

    reopened = reopen(db)
    assert reopened.current_version == 3
    assert reopened.current.digest() == before_digest
    assert reopened.current.config_version == before_config_version
    assert [
        (v.version, v.author, v.kind, v.created_at) for v in reopened.history()
    ] == before_versions
    assert reopened.current.industry("biotech").name == "生物技术（改名）"
    assert reopened.snapshot(2).industry("biotech").name == "生物技术"
    reopened.close()


def test_reopen_diff_content_survives_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.update_industry(industry("ai", name="人工智能（改名）"))
    original = store.version(2)
    store.close()

    reopened = reopen(db)
    restored = reopened.version(2)
    assert restored == original
    entry = restored.diff[0]
    assert entry.before is not None and entry.before["name"] == "人工智能"
    assert entry.after is not None and entry.after["name"] == "人工智能（改名）"
    reopened.close()


def test_reopen_does_not_duplicate_versions_when_no_new_seed_given(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"))
    store.close()

    for _ in range(3):
        again = reopen(db)
        assert again.current_version == 2
        assert [v.version for v in again.history()] == [1, 2]
        again.close()


def test_projection_tables_are_rebuildable_from_version_chain(tmp_path: Path) -> None:
    """投影只是缓存：直接删掉也不会丢事实（重开即从版本链重建）。"""
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"))
    store.close()

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM channels")
        conn.execute("DELETE FROM industries")
        conn.commit()
    finally:
        conn.close()

    reopened = reopen(db)
    assert {i.id for i in reopened.current.industries} == {"ai", "biotech"}
    assert {c.id for c in reopened.current.channels} == {"openai-blog"}
    reopened.close()


def test_damaged_snapshot_digest_is_refused_loudly(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"))
    store.close()

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP TRIGGER trg_config_versions_no_update")
        conn.execute(
            "UPDATE config_versions SET snapshot_digest = 'deadbeef' WHERE version = 2"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(VersionError, match="snapshot_digest"):
        reopen(db)


def test_schema_version_mismatch_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'", ("99",))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(VersionError, match="schema"):
        reopen(db)


def test_schema_version_recorded_in_store_meta(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and int(row[0]) == SCHEMA_VERSION


# --- 版本历史只增不改（SQL 触发器）-------------------------------------------


def test_sql_trigger_blocks_direct_update_of_version_history(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE config_versions SET author = 'mallory' WHERE version = 1")
    finally:
        conn.close()


def test_sql_trigger_blocks_direct_delete_of_version_history(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM config_versions WHERE version = 1")
    finally:
        conn.close()


def test_trigger_definitions_exist_in_sqlite_master(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()
    conn = sqlite3.connect(str(db))
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {
        "trg_config_versions_no_update",
        "trg_config_versions_no_delete",
        "trg_registry_label_ref_snapshots_no_update",
        "trg_registry_label_ref_snapshots_no_delete",
        "trg_label_space_no_update",
        "trg_label_space_no_delete",
    } <= names


def test_history_grows_monotonically_and_never_rewrites(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    first_history = store.history()
    store.create_industry(industry("biotech", name="生物"))
    second_history = store.history()
    assert [v.version for v in first_history] == [1]
    assert [v.version for v in second_history] == [1, 2]
    assert second_history[0] == first_history[0]
    store.close()

    reopened = reopen(db)
    assert reopened.history()[:2] == tuple(second_history)
    reopened.close()


# --- 回滚 ---------------------------------------------------------------------


def test_rollback_appends_new_version_and_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"))
    record = store.rollback(1, author="bob", note="回退到种子配置")
    assert record.version == 3
    assert record.kind is ChangeKind.ROLLBACK
    assert record.restored_from == 1
    assert record.author == "bob"
    assert store.current.digest() == store.snapshot(1).digest()
    store.close()

    reopened = reopen(db)
    assert reopened.current_version == 3
    assert reopened.version(3).kind is ChangeKind.ROLLBACK
    assert reopened.version(3).restored_from == 1
    assert reopened.version(3).note == "回退到种子配置"
    assert reopened.current.digest() == reopened.snapshot(1).digest()
    assert {i.id for i in reopened.current.industries} == {"ai"}
    assert [v.version for v in reopened.history()] == [1, 2, 3]
    reopened.close()


def test_rollback_to_current_and_unknown_versions_are_rejected(tmp_path: Path) -> None:
    store = seed_store(tmp_path / "atlas.db")
    with pytest.raises(VersionError, match="即当前版本"):
        store.rollback(1)
    with pytest.raises(NotFoundError):
        store.rollback(99)
    assert store.current_version == 1
    store.close()


def test_rollback_blocked_by_persisted_label_reference(tmp_path: Path) -> None:
    """重启之后"被标签引用的行业不得删除"依然生效（引用集合是持久化的）。

    引用集合随一次提交抄进库；此后重开时**不给任何回调**，
    所以能拦住回滚的只能是库里记下的那份（`referenced_ids()`）。
    """
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"), author="alice")
    store.close()

    # 打标系统给 biotech 打了标签；下一次提交把引用集合落盘
    tagged = reopen(db, label_references=lambda: {"biotech"})
    tagged.create_industry(industry("other", name="其他"), author="alice")
    assert tagged.referenced_ids() == ("biotech",)
    # 快照记录的是那一刻的启用行业集合（= 标签空间），与引用集合是两码事
    assert tagged.label_space_snapshots(3) == ("ai", "biotech", "other")
    tagged.close()

    reopened = reopen(db)
    assert reopened.current_version == 3
    assert reopened.referenced_ids() == ("biotech",)
    # v1 / v2 是"当时没有引用"，v3 记下了 biotech
    assert reopened.label_space_snapshots(1) == ("ai",)
    assert reopened.referenced_snapshots(1) == ()
    assert reopened.referenced_snapshots(3) == ("biotech",)
    with pytest.raises(InvalidContractStateError, match="已被标签引用"):
        reopened.rollback(1)
    assert reopened.current_version == 3
    reopened.close()


def test_fresh_database_reopen_without_labels_is_clean(tmp_path: Path) -> None:
    """全新库写入后重开：没有任何标签引用，不得被"已被标签引用"误伤。"""
    db = tmp_path / "atlas.db"
    store = seed_store(tmp_path / "atlas.db")
    store.create_industry(industry("biotech", name="生物"))
    store.create_channel(channel("biotech-feed", industry_id="biotech"))
    assert store.referenced_ids() == ()
    assert store.stale_with_references() == ()
    store.close()

    reopened = reopen(db)
    assert reopened.referenced_ids() == ()
    assert reopened.stale_with_references() == ()
    assert reopened.current_version == 3
    # 没有引用时，删除 / 回滚都不该被规则 4 拦住（此处只涉及渠道，删得掉）
    record = reopened.delete_channel("biotech-feed")
    assert record.kind is ChangeKind.DELETE
    assert reopened.referenced_ids() == ()
    reopened.close()


def test_label_space_is_persisted_per_version(tmp_path: Path) -> None:
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.create_industry(industry("biotech", name="生物"))
    store.create_industry(industry("retired", name="停用行业", enabled=False))
    expected_space = store.current.label_space()
    store.close()

    reopened = reopen(db)
    assert reopened.label_space() == expected_space
    assert set(reopened.label_space()) == {"ai", "biotech"}
    assert reopened.label_space_snapshots(1) == ("ai",)
    assert set(reopened.label_space_snapshots(3)) == {"ai", "biotech"}
    assert "retired" not in reopened.label_space()
    reopened.close()


def test_referenced_industry_cannot_be_deleted_after_reopen(tmp_path: Path) -> None:
    """引用集合经一次提交落盘后，重开仍挡住"删除被引用的行业"。"""
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()

    tagged = reopen(db, label_references=lambda: {"ai"})
    tagged.create_industry(industry("biotech", name="生物"))
    assert tagged.referenced_ids() == ("ai",)
    assert tagged.stale_with_references() == ()
    tagged.close()

    # 这次重开**不给回调**：能拦住删除的只能是库里那份引用集合
    reopened = reopen(db)
    assert reopened.referenced_ids() == ("ai",)
    assert reopened.stale_with_references() == ()
    with pytest.raises(InvalidContractStateError, match="已被标签引用"):
        reopened.delete_industry("ai")
    assert reopened.current_version == 2
    reopened.close()


def test_stale_references_reported_when_config_already_drifted(tmp_path: Path) -> None:
    """已经发生漂移（引用的行业不在配置里）时：自检函数报出来，且提交响亮失败。

    这是"响亮失败而非静默降级"的一例 —— 漂移必须由人显式修（重建行业或清理引用），
    系统不会偷偷放过一次提交。
    """
    db = tmp_path / "atlas.db"
    store = seed_store(db)
    store.close()

    drifted = reopen(db, label_references=lambda: {"ghost"})
    assert drifted.stale_with_references() == ("ghost",)
    with pytest.raises(InvalidContractStateError, match="已被标签引用"):
        drifted.create_industry(industry("biotech", name="生物"))
    assert drifted.current_version == 1
    drifted.close()
