"""Confirmed（人工打标）的 SQLite 持久化实现（T-108 / SPEC §2.1 §2.3 §2.10）。

本模块把 T-002 的 `atlas.contracts.ConfirmedStore` 语义**原样**落到
`data/store/atlas.db` 的 `confirmed_labels` 表（表归属见 SPEC §2.10，由 T-108 持有）。

| 契约概念 | 本模块的物理实现 |
|---|---|
| Confirmed 只增不改 | `confirmed_labels` 表 + **SQL 触发器**禁止 UPDATE / DELETE |
| 打标锚定在 `raw_id`（1A 文档级） | 表上有 `raw_id` 列；`CHECK` 强制 `anchor_raw_id = raw_id` |
| 人工直判不要求证据 | `anchor_*` 列全部可空，四个锚点列**要么全空、要么全有** |
| 改判 = 新记录 | `label_id` 是主键；不同 `label_value` → 不同 `label_id` → 新行 |
| `latest_value` 取最新 | 按 `created_at DESC`，并列时取**先写**的那条（与内存契约的 `max` 一致） |

三条设计要点（与 `registry/persistence.py` 同做法，但**不 import 它**）：

1. **只增不改由存储层强制**，不是靠约定（SPEC §2.10）。即使有人绕过本模块，
   直接拿 `sqlite3` 去 `UPDATE confirmed_labels`，也会被 `RAISE(ABORT)` 拒绝。
2. **绝不动别人的表**。本模块只发 `CREATE TABLE IF NOT EXISTS confirmed_labels`
   以及自己的索引与触发器，不 `DROP` / 不 `ALTER`、不读取也不写其它域的表
   （连 `store_meta` 都不碰 —— 它归 T-101）。重开时用 `PRAGMA table_info`
   校验表结构，形状不符就 `VersionError` **响亮失败**，而不是把别人的表静默当成自己的。
3. **幂等由数据库裁决**。重复 `add` 走 `ON CONFLICT(label_id) DO NOTHING` + 回读，
   因此返回的**永远是库里那条**（与内存契约"返回已存在的那条"逐字段一致），
   而不是调用方刚构造的那条。注意这里**不用** `INSERT OR IGNORE`：那会连
   `CHECK` 违例一起吞掉，静默接受非法锚点（CLAUDE.md 硬规则 2）。

4. **两条写入前的完整性检查，比内存契约更严（单向）**：
   `label_id` 必须与内容一致（`IdError`），证据锚点必须锚在同一份 `raw_id` 上
   （`AnchorError`，另有 SQL `CHECK` 兜底）。内存契约不做这两项检查，因此它们
   **不在**差分测试的场景集里 —— 差异方向只有一种：存储层 ⊇ 契约，绝不更松。

对外方法与 `ConfirmedStore` 一致（可多，不可少、不可改名）：
`add` / `all_for` / `latest_value` / `count`。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from atlas.contracts import AnchorError, ConfirmedLabel, IdError, VersionError, label_id_for

__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "TABLE_NAME",
    "SqliteConfirmedStore",
    "open_store",
    "resolve_db_path",
]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本模块的物理 schema 版本（存在表自身的结构上，不借用 T-101 的 `store_meta`）。
SCHEMA_VERSION = 1

#: SPEC §2.10 登记给 T-108 的表名。
TABLE_NAME = "confirmed_labels"

#: 期望的表结构：用于重开时识别"结构不同的同名表"。
_COLUMNS: Sequence[str] = (
    "label_id",
    "raw_id",
    "label_key",
    "label_value",
    "actor",
    "from_claim_id",
    "anchor_raw_id",
    "anchor_raw_sha256",
    "anchor_char_start",
    "anchor_char_end",
    "created_at",
)

_DDL = """
CREATE TABLE IF NOT EXISTS confirmed_labels (
    label_id          TEXT PRIMARY KEY,
    raw_id            TEXT NOT NULL,
    label_key         TEXT NOT NULL,
    label_value       TEXT NOT NULL,
    actor             TEXT NOT NULL,
    from_claim_id     TEXT,
    anchor_raw_id     TEXT,
    anchor_raw_sha256 TEXT,
    anchor_char_start INTEGER,
    anchor_char_end    INTEGER,
    created_at        TEXT NOT NULL,
    -- SPEC §2.1：人工标签锚定在 raw_id 上。证据锚点（若来自确认 AI 提议）
    -- 必须锚在同一份原文上，否则标签的依附对象就有两个（文档级 vs 句级）。
    CHECK (anchor_raw_id IS NULL OR anchor_raw_id = raw_id),
    -- SPEC §2.3 / §2.1：人工直判不带证据（1A）→ 锚点整体可空；
    -- 但要带就必须带全，且区间非空 —— 不允许"半截锚点"。
    CHECK (
        (anchor_raw_id IS NULL AND anchor_raw_sha256 IS NULL
         AND anchor_char_start IS NULL AND anchor_char_end IS NULL)
        OR
        (anchor_raw_id IS NOT NULL AND anchor_raw_sha256 IS NOT NULL
         AND anchor_char_start IS NOT NULL AND anchor_char_end IS NOT NULL
         AND anchor_char_start >= 0 AND anchor_char_end > anchor_char_start)
    )
);

CREATE INDEX IF NOT EXISTS idx_confirmed_labels_raw
    ON confirmed_labels(raw_id);

CREATE INDEX IF NOT EXISTS idx_confirmed_labels_raw_key
    ON confirmed_labels(raw_id, label_key);

-- SPEC §2.10：用触发器在存储层强制"Confirmed 只增不改"。
-- 触发器名与表名同时固定，避免与其它域的同名对象相撞。
CREATE TRIGGER IF NOT EXISTS trg_confirmed_labels_no_update
BEFORE UPDATE ON confirmed_labels
BEGIN
    SELECT RAISE(ABORT, 'confirmed_labels is append-only: UPDATE is forbidden (SPEC 2.3/2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_confirmed_labels_no_delete
BEFORE DELETE ON confirmed_labels
BEGIN
    SELECT RAISE(ABORT, 'confirmed_labels is append-only: DELETE is forbidden (SPEC 2.3/2.10)');
END;
"""


def resolve_db_path(db_path: str | Path | None) -> Path:
    """把 `db_path` 归一成 `Path`；`None` → SPEC §2.10 的默认库文件位置。

    单独暴露出来是为了能**不产生任何 I/O** 地断言默认路径策略（测试里尤其重要：
    仓库的 `data/` 不得被测试写入）。
    """
    if db_path is None:
        return DEFAULT_DB_PATH
    return db_path if isinstance(db_path, Path) else Path(db_path)


class SqliteConfirmedStore:
    """`ConfirmedStore` 的 SQLite 实现（同一份只增不改语义，落盘且不可改）。

    Args:
        db_path: 库文件路径；`None` 表示 SPEC §2.10 的默认位置
            `data/store/atlas.db`。测试必须传 `tmp_path`，不得写仓库 `data/`。
    """

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._path = resolve_db_path(db_path)

        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # 建表**之前**先验形状：`CREATE TABLE IF NOT EXISTS` 对结构不同的同名表是
        # 静默无操作，随后的 `CREATE INDEX` 只会抛一个语焉不详的 `OperationalError`。
        if self._table_exists():
            self._assert_own_table_shape()
        self._conn.executescript(_DDL)
        self._assert_own_table_shape()
        self._conn.commit()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def table_name(self) -> str:
        return TABLE_NAME

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只读查询用）。

        写入请走 `add()` —— 直接 `INSERT` 会绕过本模块的锚点校验，
        而 `UPDATE` / `DELETE` 无论如何都会被触发器拒绝。
        """
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteConfirmedStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"SqliteConfirmedStore(db_path={str(self._path)!r})"

    def _table_exists(self) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TABLE_NAME,),
        ).fetchone()
        return row is not None

    def _assert_own_table_shape(self) -> None:
        """重开时确认 `confirmed_labels` 确实是本模块建的那张表。

        `CREATE TABLE IF NOT EXISTS` 对**已存在但结构不同**的同名表是静默无操作，
        于是"别人先占了这张表"会变成难以定位的运行时错误。这里显式阻挡。
        """
        rows = self._conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
        actual = tuple(str(row["name"]) for row in rows)
        if actual != tuple(_COLUMNS):
            raise VersionError(
                f"{self._path} 里的 {TABLE_NAME} 表结构与本模块预期不符"
                f"（实际列：{actual}，预期列：{tuple(_COLUMNS)}）；"
                f"当前 SCHEMA_VERSION={SCHEMA_VERSION}。需要显式迁移，不做静默兼容"
            )

    # ------------------------------------------------------------------
    # 写（唯一入口）
    # ------------------------------------------------------------------
    def add(self, label: ConfirmedLabel) -> ConfirmedLabel:
        """写入一条人工标签；**幂等**：同 `label_id` 重复 `add` 返回库里已存在的那条。

        存储层强制的三条不变量（在写之前就失败，不留半成品）：

        - `label_id` 必须与内容一致（内容寻址，SPEC §4.1 T-002 的 ID 策略），
          否则 `IdError` —— 与 `RawStore.put()` 校验内容指纹同一条道理；
        - 证据锚点必须锚在同一份原文上（SPEC §2.1），否则 `AnchorError`；
        - 写入后该行不可 UPDATE / DELETE（SPEC §2.3，由触发器强制）。
        """
        self._assert_label_id(label)
        self._assert_anchor_on_raw(label)
        with self._conn:  # 事务：要么完整写入，要么什么都不写
            self._conn.execute(
                f"""
                INSERT INTO {TABLE_NAME}(
                    label_id, raw_id, label_key, label_value, actor, from_claim_id,
                    anchor_raw_id, anchor_raw_sha256, anchor_char_start, anchor_char_end,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label_id) DO NOTHING
                """,
                (
                    label.label_id,
                    label.raw_id,
                    label.label_key,
                    label.label_value,
                    label.actor,
                    label.from_claim_id,
                    label.anchor.raw_id if label.anchor else None,
                    label.anchor.raw_sha256 if label.anchor else None,
                    label.anchor.char_start if label.anchor else None,
                    label.anchor.char_end if label.anchor else None,
                    label.created_at.isoformat(),
                ),
            )
            stored = self._select_one(label.label_id)
        if stored is None:
            # 只有"插入被冲突规则吞掉、随后又查不到"才会走到这里，属于不可能状态。
            raise AnchorError(
                f"label_id {label.label_id} 写入后无法读回：库状态与写入语义不一致"
            )
        return stored

    @staticmethod
    def _assert_label_id(label: ConfirmedLabel) -> None:
        """`label_id` 必须由 (raw_id, label_key, label_value, actor) 内容寻址算得。

        与 `RawStore.put()` 校验 `content_sha256` 是同一条道理：标识与内容不符的记录
        一旦入库，去重与改判判定就都失效了（两张不同的判断可能撞成同一个 id）。
        """
        expected = label_id_for(
            label.raw_id, label.label_key, label.label_value, label.actor
        )
        if label.label_id != expected:
            raise IdError(
                f"label_id 与内容不符：声明 {label.label_id}，按 (raw_id, label_key, "
                f"label_value, actor) 重算应为 {expected} —— 拒绝写入"
            )

    @staticmethod
    def _assert_anchor_on_raw(label: ConfirmedLabel) -> None:
        if label.anchor is None:
            return
        if label.anchor.raw_id != label.raw_id:
            raise AnchorError(
                f"证据锚点锚在 {label.anchor.raw_id}，而标签锚在 {label.raw_id}："
                "SPEC §2.1 要求人工标签只锚 raw_id，不得锚到解析产物 / 块 ID / 归一化偏移"
            )

    # ------------------------------------------------------------------
    # 读（与 T-002 契约语义一致）
    # ------------------------------------------------------------------
    def all_for(self, raw_id: str) -> List[ConfirmedLabel]:
        """该文档上的全部标签，按**写入顺序**（与内存契约的插入顺序一致）。"""
        rows = self._conn.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE raw_id = ? ORDER BY rowid", (raw_id,)
        ).fetchall()
        return [self._row_to_label(row) for row in rows]

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        """该文档该维度上的最新取值；无记录返回 `None`（不编造默认值）。

        排序键与内存契约的 `max(..., key=created_at)` 对齐：`created_at` 相同时
        取**先写入**的那条（`rowid ASC`）。
        """
        row = self._conn.execute(
            f"SELECT label_value FROM {TABLE_NAME} WHERE raw_id = ? AND label_key = ? "
            "ORDER BY created_at DESC, rowid ASC LIMIT 1",
            (raw_id, label_key),
        ).fetchone()
        return None if row is None else str(row["label_value"])

    def count(self) -> int:
        row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {TABLE_NAME}").fetchone()
        assert row is not None  # COUNT(*) 恒有一行
        return int(row["n"])

    # --- 额外只读查询（契约之外的便利，不改变上面四条语义）----------------

    def get(self, label_id: str) -> Optional[ConfirmedLabel]:
        return self._select_one(label_id)

    def has(self, label_id: str) -> bool:
        return self._select_one(label_id) is not None

    def all_labels(self) -> List[ConfirmedLabel]:
        """全表按写入顺序读出（导出与审计用）。"""
        rows = self._conn.execute(f"SELECT * FROM {TABLE_NAME} ORDER BY rowid").fetchall()
        return [self._row_to_label(row) for row in rows]

    def raw_ids(self) -> List[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT raw_id FROM {TABLE_NAME} ORDER BY raw_id"
        ).fetchall()
        return [str(row["raw_id"]) for row in rows]

    def keys_for(self, raw_id: str) -> List[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT label_key FROM {TABLE_NAME} WHERE raw_id = ? "
            "ORDER BY label_key",
            (raw_id,),
        ).fetchall()
        return [str(row["label_key"]) for row in rows]

    # ------------------------------------------------------------------
    # 行 ↔ 记录
    # ------------------------------------------------------------------
    def _select_one(self, label_id: str) -> Optional[ConfirmedLabel]:
        row = self._conn.execute(
            f"SELECT * FROM {TABLE_NAME} WHERE label_id = ?", (label_id,)
        ).fetchone()
        return None if row is None else self._row_to_label(row)

    @staticmethod
    def _row_to_label(row: sqlite3.Row) -> ConfirmedLabel:
        """行 → 契约记录。锚点四列由 `CHECK` 保证同进同出。"""
        anchor_raw_id = row["anchor_raw_id"]
        payload: dict[str, object] = {
            "label_id": str(row["label_id"]),
            "raw_id": str(row["raw_id"]),
            "label_key": str(row["label_key"]),
            "label_value": str(row["label_value"]),
            "actor": str(row["actor"]),
            "from_claim_id": row["from_claim_id"],
            "created_at": datetime.fromisoformat(str(row["created_at"])),
        }
        if anchor_raw_id is not None:
            payload["anchor"] = {
                "raw_id": str(anchor_raw_id),
                "raw_sha256": str(row["anchor_raw_sha256"]),
                "char_start": int(row["anchor_char_start"]),
                "char_end": int(row["anchor_char_end"]),
            }
        return ConfirmedLabel.model_validate(payload)


def open_store(db_path: str | Path | None = None) -> SqliteConfirmedStore:
    """便捷入口：打开（必要时创建）一个库文件并返回人工打标仓储。"""
    return SqliteConfirmedStore(db_path=db_path)
