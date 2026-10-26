"""SQLite 持久化的配置仓储（T-101 / SPEC §2.10）。

本模块把 T-004 的 `ConfigStore` 语义**原样**落到 `data/store/atlas.db`：

| T-004 概念 | 本模块的物理实现 |
|---|---|
| 当前配置 | `industries` / `channels` 两张投影表（只是缓存，可重建） |
| 版本链（只增不改） | `config_versions` 表 + **SQL 触发器**禁止 UPDATE / DELETE |
| 每次提交的 diff | `config_versions.diff`（JSON） |
| 提交时的完整快照 | `config_versions.snapshot`（JSON，原子可读） |
| 标签引用（跨对象规则 4） | `registry_label_refs`（当前） + `registry_label_ref_snapshots`（每版本） |
| 标签空间（启用行业） | `label_space`（每版本只增不改） |

**表归属（SPEC §2.10）**：本模块只负责
`store_meta` / `industries` / `channels` / `config_versions` / `label_space` /
`registry_label_refs` / `registry_label_ref_snapshots`。
`registry_label_refs*` **只记录"哪些行业 id 被标签引用"**（§2.9 校验规则 1 的输入，
本质是引用索引）；**标签本体不在本模块** —— 人工打标的事实层是 T-108 的
`confirmed_labels`（T-103 拥有 `raw_records`，T-105 拥有 `proposed_claims`，
T-107 拥有 `evidence_spans`）。表名带 `registry_` 前缀就是为了避免与 T-108 撞名后
被 `CREATE TABLE IF NOT EXISTS` 静默复用成结构完全不同的表。

三条设计要点：

1. **校验不在这里重复实现。** 本类内部持有一个 T-004 的
   `ConfigStore` 实例作为"内存工作副本"：所有 `commit()` / `rollback()`
   都先交给它跑候选快照校验（`validate_registry()`），通过后才写 SQL。
   因此"非法配置在保存时拒绝"在 SQLite 路径上同样是结构性的，没有旁路。
2. **重开连接后语义一致。** 打开已有库时按版本链**重放 diff** 重建内存副本，
   并逐版本比对 `snapshot_digest`；对不上就响亮失败（不静默接受损坏的历史）。
3. **只增不改由存储层强制**（SPEC §2.10「用 SQL 触发器强制，而非靠约定」）。
   即使有人直接拿 `sqlite3` 去 `UPDATE config_versions`，也会被 `RAISE(ABORT)` 拒绝。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from atlas.contracts import InvalidContractStateError, VersionError

from .schema import Channel, Industry, validate_registry
from .versioning import (
    ChangeKind,
    ConfigSnapshot,
    ConfigStore,
    ConfigVersion,
    DiffEntry,
    RegistryMutation,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "SqliteConfigStore",
    "open_store",
]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本模块的物理 schema 版本。将来加表时必须显式迁移，不静默兼容。
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS store_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- 行业/渠道：当前配置的物化投影（可从 config_versions 单独重建）
CREATE TABLE IF NOT EXISTS industries (
    industry_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    parent_id       TEXT,
    enabled         INTEGER NOT NULL,
    payload         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id      TEXT PRIMARY KEY,
    industry_id     TEXT NOT NULL,
    type            TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    enabled         INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_industry ON channels(industry_id);
CREATE INDEX IF NOT EXISTS idx_channels_enabled  ON channels(enabled);

-- 版本链：只增不改
CREATE TABLE IF NOT EXISTS config_versions (
    version          INTEGER PRIMARY KEY,
    parent_version   INTEGER,
    restored_from    INTEGER,
    author           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    kind             TEXT NOT NULL,
    note             TEXT,
    snapshot_digest  TEXT NOT NULL,
    diff             TEXT NOT NULL,
    snapshot         TEXT NOT NULL,
    FOREIGN KEY (parent_version) REFERENCES config_versions(version)
);

-- 引用索引：**只**记录"哪些行业 id 已被标签引用"（SPEC §2.9 校验规则 1 的输入）。
-- 标签本体**不在**这里 —— 人工打标的事实层归 T-108 的 `confirmed_labels`
-- （SPEC §2.10 表归属）。本表是可重建的投影，随每次提交整体替换。
CREATE TABLE IF NOT EXISTS registry_label_refs (
    industry_id TEXT PRIMARY KEY
);

-- 每个版本提交时的两条只增不改快照：标签引用集合 + 启用行业集合（= 标签空间）
CREATE TABLE IF NOT EXISTS registry_label_ref_snapshots (
    version     INTEGER NOT NULL,
    industry_id TEXT NOT NULL,
    PRIMARY KEY (version, industry_id),
    FOREIGN KEY (version) REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS label_space (
    version     INTEGER NOT NULL,
    industry_id TEXT NOT NULL,
    PRIMARY KEY (version, industry_id),
    FOREIGN KEY (version) REFERENCES config_versions(version)
);

-- SPEC §2.10：用触发器在存储层强制"版本历史只增不改"
CREATE TRIGGER IF NOT EXISTS trg_config_versions_no_update
BEFORE UPDATE ON config_versions
BEGIN
    SELECT RAISE(ABORT, 'config_versions is append-only: UPDATE is forbidden (SPEC 2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_config_versions_no_delete
BEFORE DELETE ON config_versions
BEGIN
    SELECT RAISE(ABORT, 'config_versions is append-only: DELETE is forbidden (SPEC 2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_label_ref_snapshots_no_update
BEFORE UPDATE ON registry_label_ref_snapshots
BEGIN
    SELECT RAISE(ABORT, 'registry_label_ref_snapshots is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_registry_label_ref_snapshots_no_delete
BEFORE DELETE ON registry_label_ref_snapshots
BEGIN
    SELECT RAISE(ABORT, 'registry_label_ref_snapshots is append-only: DELETE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_label_space_no_update
BEFORE UPDATE ON label_space
BEGIN
    SELECT RAISE(ABORT, 'label_space is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_label_space_no_delete
BEFORE DELETE ON label_space
BEGIN
    SELECT RAISE(ABORT, 'label_space is append-only: DELETE is forbidden');
END;
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        return DEFAULT_DB_PATH
    return db_path if isinstance(db_path, Path) else Path(db_path)


class SqliteConfigStore:
    """`ConfigStore` 的 SQLite 实现（同一份提交语义，落盘且版本历史不可改）。

    构造参数与 T-004 的内存 `ConfigStore` 同名同义；额外的 `db_path` 决定库文件位置。
    """

    def __init__(
        self,
        *,
        author: str,
        industries: Iterable[Industry] = (),
        channels: Iterable[Channel] = (),
        label_references: Optional[Callable[[], Iterable[str]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        db_path: str | Path | None = None,
    ) -> None:
        self._path = _as_path(db_path)
        self._clock = clock or _utcnow
        self._label_references = label_references

        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_DDL)

        stored = self._meta("schema_version")
        if stored is None:
            self._set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(stored) != SCHEMA_VERSION:
            raise VersionError(
                f"库文件 {self._path} 的 schema 版本为 {stored}，"
                f"本代码只认 {SCHEMA_VERSION}；需要显式迁移，不做静默兼容"
            )

        self._store = self._load(author=author)
        if self._store is None:
            self._store = self._create_genesis(
                author=author, industries=tuple(industries), channels=tuple(channels)
            )

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只读查询用；写入请走 `commit()` 以保证校验与版本链）。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteConfigStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def today(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------
    # meta
    # ------------------------------------------------------------------
    def _meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _referenced_ids(self) -> Tuple[str, ...]:
        """当前应该记录的引用集合：优先用运行时回调，否则沿用库里已有的。"""
        if self._label_references is not None:
            return tuple(sorted(set(self._label_references())))
        return self.referenced_ids()

    def _effective_references(self) -> Tuple[str, ...]:
        """真正用于校验的引用集合（与提交时的口径完全一致）。

        回调和持久化引用可能同时存在；`_referenced_ids()` 里回调优先，
        因此校验口径也必须是回调优先，否则"校验用的集合"和"落盘的集合"会分叉。
        """
        return self._referenced_ids()

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO store_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------------
    # 装载 / 重放
    # ------------------------------------------------------------------
    def _version_rows(self) -> List[sqlite3.Row]:
        return list(
            self._conn.execute("SELECT * FROM config_versions ORDER BY version").fetchall()
        )

    @staticmethod
    def _row_to_version_record(row: sqlite3.Row) -> ConfigVersion:
        diff = tuple(DiffEntry.model_validate(item) for item in json.loads(row["diff"]))
        note = row["note"]
        restored = row["restored_from"]
        payload: Dict[str, Any] = {
            "version": int(row["version"]),
            "author": row["author"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "kind": ChangeKind(row["kind"]),
            "parent_version": (
                int(row["parent_version"]) if row["parent_version"] is not None else None
            ),
            "diff": diff,
            "snapshot_digest": row["snapshot_digest"],
        }
        if note is not None:
            payload["note"] = note
        if restored is not None:
            payload["restored_from"] = int(restored)
        return ConfigVersion(**payload)

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> ConfigSnapshot:
        return ConfigSnapshot.model_validate_json(row["snapshot"])

    def _load(self, *, author: str) -> Optional[ConfigStore]:
        """按版本链重放，重建内存工作副本；逐版本校验摘要，损坏即响亮失败。

        重放**只重建状态**：重放出的 `ConfigVersion` 记录原样取自库里的
        author / created_at / kind / note / restored_from，模型层只做记录级校验
        （例如 rollback 必须带 `restored_from`）。**跨对象校验不跳过**：每个版本
        重放出来的快照都要过一遍 `validate_registry()`（用持久化的标签引用），
        所以 opml 之外的手工/损坏写入也一样会被挡下。
        空库返回 `None`，由构造方走 `_create_genesis()` —— 这样"内存副本的构造"
        与"库里有什么"不会互相污染，`clock` 也只在真正需要时被消费一次。
        """
        rows = self._version_rows()
        if not rows:
            return None

        genesis_row = rows[0]
        genesis_snapshot = self._snapshot_from_row(genesis_row)
        self._assert_digest(genesis_snapshot, genesis_row, genesis_snapshot)
        self._validate_replayed(genesis_row, genesis_snapshot)

        industries: Dict[str, Industry] = {i.id: i for i in genesis_snapshot.industries}
        channels: Dict[str, Channel] = {c.id: c for c in genesis_snapshot.channels}
        snapshots: Dict[int, ConfigSnapshot] = {1: genesis_snapshot}

        for row in rows[1:]:
            record = self._row_to_version_record(row)
            for entry in record.diff:
                _apply_diff_entry(entry, industries, channels, version=record.version)
            candidate = ConfigSnapshot(
                version=record.version,
                industries=tuple(sorted(industries.values(), key=lambda i: i.id)),
                channels=tuple(sorted(channels.values(), key=lambda c: c.id)),
            )
            stored = self._snapshot_from_row(row)
            self._assert_digest(candidate, row, stored)
            self._validate_replayed(row, candidate)
            snapshots[record.version] = candidate

        return _RehydratedConfigStore(
            records=tuple(self._row_to_version_record(row) for row in rows),
            snapshots=snapshots,
            author=author,
            label_references=(
                self._label_references
                if self._label_references is not None
                else self._persisted_reference_source()
            ),
            clock=self._clock,
        )

    def _persisted_reference_source(self) -> Callable[[], Tuple[str, ...]]:
        """把库里的引用集合做成 `label_references` 回调。

        没有这一步，重启后 `ConfigStore` 拿到的引用集合就是空的 —— 于是
        "被标签引用的行业不得删除 / 回滚不得把它丢掉"（§2.9 规则 1）会**静默失效**。
        这里在装载时把引用集合**固化**下来：固化值不随后续提交变化，
        因此 `_write_labels()` 替换投影时不会把当前集合清空（不会自我蚕食）。
        """
        frozen = self.referenced_ids()
        return lambda: frozen

    def _validate_replayed(self, row: sqlite3.Row, candidate: ConfigSnapshot) -> None:
        """重放校验：用**该版本自己记录**的引用集合快照。

        不能用"当前"引用集合：标签是事后写的，用今天的标签去否定历史版本会让一个
        合法的库打不开。§2.9 规则 1 约束的是"提交时不得删掉已被引用的 id"，
        所以校验必须站在当时的位置上。
        v1（genesis）早于任何标签，引用集合只能取当前值（正常为空）。
        """
        version = int(row["version"])
        recorded = self.referenced_snapshots(version)
        if recorded:
            referenced = recorded
        else:
            # 该版本确实没有引用（空集合）与"快照整块缺失"必须区分开：
            # 空引用是合法状态，快照缺失是库损坏。
            if version != 1 and not self.label_space_snapshots(version):
                raise VersionError(
                    f"v{version} 既没有引用集合快照、也没有标签空间快照，"
                    "版本链不完整 —— 拒绝静默接受"
                )
            referenced = ()
        validate_registry(
            candidate.industries,
            candidate.channels,
            referenced_ids=referenced,
        )

    @staticmethod
    def _assert_digest(
        rebuilt: ConfigSnapshot, row: sqlite3.Row, stored: ConfigSnapshot
    ) -> None:
        if rebuilt.digest() != stored.digest():
            raise VersionError(
                f"库文件中的 v{row['version']} 快照与按 diff 重放出的结果不一致："
                "版本链已损坏（拒绝静默接受）"
            )
        if row["snapshot_digest"] != stored.digest():
            raise VersionError(
                f"v{row['version']} 记录的 snapshot_digest 与其 snapshot 不匹配："
                "库文件被外部改写（拒绝静默接受）"
            )
        if rebuilt.version != stored.version:
            raise VersionError(
                f"v{row['version']} 重放后的版本号变成了 v{rebuilt.version}"
            )

    def _create_genesis(
        self,
        *,
        author: str,
        industries: Tuple[Industry, ...],
        channels: Tuple[Channel, ...],
    ) -> ConfigStore:
        """空库的第一次落盘：构造内存 genesis（v1）并原样写进版本链。

        `ConfigStore` 的构造本身就会跑 `validate_registry()`，因此非法初始配置
        在**任何写入之前**被拒绝 —— 库里不会留下半成品版本。
        """
        seed = ConfigStore(
            author=author,
            industries=industries,
            channels=channels,
            label_references=self._label_references,
            clock=self._clock,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._write_version(seed.version(1), seed.snapshot(1))
            self._write_projection(seed.current)
            self._write_labels(seed.current_version, seed.current)
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return seed

    # ------------------------------------------------------------------
    # 读（与 T-004 语义一致）
    # ------------------------------------------------------------------
    @property
    def store(self) -> ConfigStore:
        """内部内存工作副本（校验权威）；只读用途。"""
        return self._store

    @property
    def current(self) -> ConfigSnapshot:
        return self._store.current

    @property
    def current_version(self) -> int:
        return self._store.current_version

    @property
    def versions(self) -> Tuple[ConfigVersion, ...]:
        return self._store.versions

    def version(self, number: int) -> ConfigVersion:
        return self._store.version(number)

    def snapshot(self, number: int) -> ConfigSnapshot:
        return self._store.snapshot(number)

    def history(self) -> Tuple[ConfigVersion, ...]:
        return self._store.history()

    def label_space(self) -> Tuple[str, ...]:
        return self._store.label_space()

    def referenced_ids(self, version: Optional[int] = None) -> Tuple[str, ...]:
        """当前持久化的**标签引用**集合（SPEC §2.9 校验规则 1 的输入）。

        持久化在这里是**必要的**：T-004 的 `label_references` 是运行时回调，
        重开进程后引用集合必须还能从库里读出来，否则"被引用的行业不得删除 / 回滚
        不得把它丢掉"在重启后就失效了。`version` 参数只为兼容调用习惯，
        引用集合是"当前"状态（随每次提交整体替换）。
        """
        del version
        rows = self._conn.execute(
            "SELECT industry_id FROM registry_label_refs ORDER BY industry_id"
        ).fetchall()
        return tuple(str(row["industry_id"]) for row in rows)

    def referenced_snapshots(self, version: int) -> Tuple[str, ...]:
        """某个历史版本提交时记录的引用集合（审计用，只增不改）。"""
        rows = self._conn.execute(
            "SELECT industry_id FROM registry_label_ref_snapshots WHERE version = ? "
            "ORDER BY industry_id",
            (version,),
        ).fetchall()
        return tuple(str(row["industry_id"]) for row in rows)

    def label_space_snapshots(self, version: int) -> Tuple[str, ...]:
        """某个历史版本提交时记录的启用行业 id（= 那一刻的标签空间）。"""
        rows = self._conn.execute(
            "SELECT industry_id FROM label_space WHERE version = ? "
            "ORDER BY industry_id",
            (version,),
        ).fetchall()
        return tuple(str(row["industry_id"]) for row in rows)

    def stale_with_references(self, version: Optional[int] = None) -> Tuple[str, ...]:
        """已被打标引用、但配置里已不存在的行业 id（一致性自检用）。

        与提交校验同口径（回调优先），因此它报出的漂移与"提交会不会被拒"一致。
        """
        target = self.current_version if version is None else version
        known = {i.id for i in self.snapshot(target).industries}
        return tuple(sorted(set(self._effective_references()) - known))

    # ------------------------------------------------------------------
    # 写（唯一入口，与 T-004 同名同义）
    # ------------------------------------------------------------------
    def commit(
        self,
        mutations: Sequence[RegistryMutation],
        *,
        author: Optional[str] = None,
        kind: Optional[ChangeKind] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        record = self._store.commit(mutations, author=author, kind=kind, note=note)
        candidate = self._store.snapshot(record.version)
        self._persist(record, candidate)
        return record

    def rollback(
        self,
        target_version: int,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        record = self._store.rollback(target_version, author=author, note=note)
        candidate = self._store.snapshot(record.version)
        self._persist(record, candidate)
        return record

    # --- 便利方法（与 T-004 完全一致的签名）-----------------------------

    def create_industry(self, industry: Industry, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="create", object_kind="industry", payload=industry.payload())],
            **kwargs,
        )

    def update_industry(self, industry: Industry, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="industry",
                    object_id=industry.id,
                    payload=industry.payload(),
                )
            ],
            **kwargs,
        )

    def delete_industry(self, industry_id: str, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="delete", object_kind="industry", object_id=industry_id)],
            **kwargs,
        )

    def create_channel(self, channel: Channel, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="create", object_kind="channel", payload=channel.payload())],
            **kwargs,
        )

    def update_channel(self, channel: Channel, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="channel",
                    object_id=channel.id,
                    payload=channel.payload(),
                )
            ],
            **kwargs,
        )

    def delete_channel(self, channel_id: str, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="delete", object_kind="channel", object_id=channel_id)],
            **kwargs,
        )

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------
    def _persist(self, record: ConfigVersion, candidate: ConfigSnapshot) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._write_version(record, candidate)
            self._write_projection(candidate)
            self._write_labels(record.version, candidate)
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _write_version(self, record: ConfigVersion, candidate: ConfigSnapshot) -> None:
        if candidate.version != record.version:
            raise InvalidContractStateError(
                f"版本记录 v{record.version} 与候选快照 v{candidate.version} 不一致"
            )
        if candidate.digest() != record.snapshot_digest:
            raise VersionError(
                f"v{record.version} 的 snapshot_digest 与候选快照内容不符，拒绝落盘"
            )
        self._conn.execute(
            """
            INSERT INTO config_versions(
                version, parent_version, restored_from, author, created_at,
                kind, note, snapshot_digest, diff, snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.version,
                record.parent_version,
                record.restored_from,
                record.author,
                record.created_at.astimezone(timezone.utc).isoformat(),
                record.kind.value,
                record.note,
                record.snapshot_digest,
                json.dumps(
                    [entry.model_dump(mode="json") for entry in record.diff],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                candidate.model_dump_json(),
            ),
        )

    def _write_projection(self, candidate: ConfigSnapshot) -> None:
        self._conn.execute("DELETE FROM channels")
        self._conn.execute("DELETE FROM industries")
        for industry in candidate.industries:
            self._conn.execute(
                "INSERT INTO industries(industry_id, name, parent_id, enabled, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    industry.id,
                    industry.name,
                    industry.parent_id,
                    1 if industry.enabled else 0,
                    industry.model_dump_json(),
                ),
            )
        for channel in candidate.channels:
            self._conn.execute(
                "INSERT INTO channels(channel_id, industry_id, type, endpoint, enabled, "
                "interval_seconds, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    channel.id,
                    channel.industry_id,
                    channel.type.value,
                    channel.endpoint,
                    1 if channel.enabled else 0,
                    channel.interval_seconds,
                    channel.model_dump_json(),
                ),
            )

    def _write_labels(self, version: int, candidate: ConfigSnapshot) -> None:
        """替换"当前引用集合"投影，并把两条快照追加到只增不改的表里。

        引用集合（`registry_label_refs`）与标签空间（`label_space`）是**两码事**：
        前者是"哪些行业已经被人工标签引用过"，后者是"当前启用行业 = AI 分类的
        候选标签集合"。两者都只记 id，标签本体归 T-108 的 `confirmed_labels`。
        """
        referenced = self._referenced_ids()
        self._conn.execute("DELETE FROM registry_label_refs")
        for industry_id in referenced:
            self._conn.execute(
                "INSERT INTO registry_label_refs(industry_id) VALUES (?)", (industry_id,)
            )
            self._conn.execute(
                "INSERT INTO registry_label_ref_snapshots(version, industry_id) "
                "VALUES (?, ?)",
                (version, industry_id),
            )
        for industry_id in sorted(set(candidate.label_space())):
            self._conn.execute(
                "INSERT INTO label_space(version, industry_id) VALUES (?, ?)",
                (version, industry_id),
            )


def _apply_diff_entry(
    entry: DiffEntry,
    industries: Dict[str, Industry],
    channels: Dict[str, Channel],
    *,
    version: int,
) -> None:
    """把一条持久化的 diff 施加到状态字典上（重放路径）。

    这里只做**结构性**检查（对象在不在、id 撞不撞），字段级与跨对象校验仍由
    `Industry(**...)` / `Channel(**...)` 与 `validate_registry()` 承担。
    重放出的状态必须与库里存的 snapshot 摘要一致，否则 `_assert_digest()` 会拒绝。
    """
    where = f"v{version}"

    if entry.object_kind == "industry":
        if entry.change is ChangeKind.CREATE:
            assert entry.after is not None, "create 变更必须有 after"
            record = Industry(**entry.after)
            if record.id != entry.object_id:
                raise VersionError(
                    f"{where} 的 diff 中 industry.create 的 id={record.id!r} "
                    f"与 object_id={entry.object_id!r} 不一致"
                )
            if record.id in industries:
                raise VersionError(f"{where} 的 diff 重复创建行业 {record.id!r}")
            industries[record.id] = record
            return
        if entry.change is ChangeKind.DELETE:
            assert entry.before is not None, "delete 变更必须有 before"
            if entry.object_id not in industries:
                raise VersionError(
                    f"{where} 的 diff 要求删除不存在的行业 {entry.object_id!r}"
                )
            del industries[entry.object_id]
            return
        assert entry.after is not None, "update 变更必须有 after"
        if entry.object_id not in industries:
            raise VersionError(
                f"{where} 的 diff 要求更新不存在的行业 {entry.object_id!r}"
            )
        industries[entry.object_id] = Industry(**entry.after)
        return

    if entry.change is ChangeKind.CREATE:
        assert entry.after is not None, "create 变更必须有 after"
        record = Channel(**entry.after)
        if record.id != entry.object_id:
            raise VersionError(
                f"{where} 的 diff 中 channel.create 的 id={record.id!r} "
                f"与 object_id={entry.object_id!r} 不一致"
            )
        if record.id in channels:
            raise VersionError(f"{where} 的 diff 重复创建渠道 {record.id!r}")
        channels[record.id] = record
        return
    if entry.change is ChangeKind.DELETE:
        assert entry.before is not None, "delete 变更必须有 before"
        if entry.object_id not in channels:
            raise VersionError(
                f"{where} 的 diff 要求删除不存在的渠道 {entry.object_id!r}"
            )
        del channels[entry.object_id]
        return
    assert entry.after is not None, "update 变更必须有 after"
    if entry.object_id not in channels:
        raise VersionError(f"{where} 的 diff 要求更新不存在的渠道 {entry.object_id!r}")
    channels[entry.object_id] = Channel(**entry.after)


class _RehydratedConfigStore(ConfigStore):
    """从持久化版本链重放出来的只读工作副本。

    它不是"新建的 store"：`_versions` / `_snapshots` 由库里的记录直接给出，
    因此 author / created_at / kind / note 与重启前**逐字节一致**；
    `commit()` / `rollback()` 沿用 `ConfigStore` 的实现（唯一写入入口 + 校验），
    新版本接着链尾追加，正如 T-004 的内存实现。
    """

    def __init__(
        self,
        *,
        records: Tuple[ConfigVersion, ...],
        snapshots: Dict[int, ConfigSnapshot],
        author: str,
        label_references: Optional[Callable[[], Iterable[str]]],
        clock: Optional[Callable[[], datetime]],
    ) -> None:
        self._author = author.strip()
        self._label_references = label_references
        self._clock = clock or _utcnow
        self._versions = list(records)
        self._snapshots = dict(snapshots)


def open_store(
    db_path: str | Path | None = None,
    *,
    author: str,
    industries: Iterable[Industry] = (),
    channels: Iterable[Channel] = (),
    label_references: Optional[Callable[[], Iterable[str]]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> SqliteConfigStore:
    """便捷入口：打开（必要时创建）一个库文件并返回仓储。"""
    return SqliteConfigStore(
        author=author,
        industries=industries,
        channels=channels,
        label_references=label_references,
        clock=clock,
        db_path=db_path,
    )
