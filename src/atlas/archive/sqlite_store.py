"""元数据持久化：`raw_records` 表 + 只增不改触发器（SPEC §2.10）。

与 T-101 在 `config_versions` 上的做法一致：**用 SQL 触发器在存储层强制不变量**，
而不是靠调用方自觉。

共用 DB 的边界（硬性要求 3）
---------------------------

`data/store/atlas.db` 由多个域共用。本模块**只**执行：

- `CREATE TABLE IF NOT EXISTS raw_records`
- `CREATE TABLE IF NOT EXISTS raw_store_meta`（本域的 schema 版本登记）
- `CREATE TRIGGER IF NOT EXISTS trg_raw_records_no_update` / `..._no_delete`
- 自己的索引（`idx_raw_records_channel` / `idx_raw_records_sha`）

**不做**任何破坏性 DDL，不 `DROP`，不删除任何非本域的表，也不 import
其它域的持久化模块。`raw_records` 是本域唯一的事实表（SPEC §2.10 表归属）。

表名与列
--------

| 列 | 说明 |
|---|---|
| `raw_id` | 主键（`atlas.contracts.ids.raw_id_for`，内容寻址） |
| `channel_id` / `endpoint` | 抓取来源 |
| `content_sha256` | 原文指纹（与 `content.bin` 的字节一致） |
| `byte_length` | 字节数 |
| `fetched_at` | 抓取时刻（UTC，ISO-8601 微秒精度） |
| `http_status` | HTTP 状态码；无则为 `NULL` |

**事务边界**：连接为 `isolation_level=None`（自动提交），单条 `INSERT` 落盘；
`raw_records` 只有"插入"一种写操作，没有 `UPDATE` / `DELETE` 的代码路径。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from atlas.contracts import ImmutabilityError, NotFoundError, RawRecord, VersionError

__all__ = ["DEFAULT_DB_PATH", "SCHEMA_VERSION", "SqliteRawStore"]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本域物理 schema 版本。将来加列必须显式迁移，不静默兼容。
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS raw_store_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- SPEC §2.10 表归属：raw_records 属于 T-103（原文元数据；字节本体在文件系统）
CREATE TABLE IF NOT EXISTS raw_records (
    raw_id          TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    byte_length     INTEGER NOT NULL,
    fetched_at      TEXT NOT NULL,
    http_status     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_raw_records_channel ON raw_records(channel_id);
CREATE INDEX IF NOT EXISTS idx_raw_records_sha     ON raw_records(content_sha256);

-- SPEC §2.3 / §2.10：Raw 只增不改，用触发器强制（不是靠调用方约定）
CREATE TRIGGER IF NOT EXISTS trg_raw_records_no_update
BEFORE UPDATE ON raw_records
BEGIN
    SELECT RAISE(ABORT, 'raw_records is append-only: UPDATE is forbidden (SPEC 2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_raw_records_no_delete
BEFORE DELETE ON raw_records
BEGIN
    SELECT RAISE(ABORT, 'raw_records is append-only: DELETE is forbidden (SPEC 2.10)');
END;
"""

_COLUMNS = "raw_id, channel_id, endpoint, content_sha256, byte_length, fetched_at, http_status"

_PathLike = Union[str, Path]


class SqliteRawStore:
    """`RawRecord` 元数据的 SQLite 持久化。

    只实现"插入 + 查询"：`insert` / `get` / `all_raw_ids` / `exists`。
    覆盖与删除在 SQL 层就被触发器拒绝，本类**没有**对应方法。
    """

    def __init__(self, db_path: Optional[_PathLike] = None) -> None:
        self._path = DEFAULT_DB_PATH if db_path is None else Path(db_path)
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._init_meta()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（供只读诊断与触发器测试用）。写入请走 `insert`。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteRawStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 写（只有插入）
    # ------------------------------------------------------------------
    def insert(self, record: RawRecord) -> RawRecord:
        """插入一条元数据。

        已存在同一 `raw_id` 时**不覆盖**：
        - 指纹相同 → 幂等，返回已存记录；
        - 指纹不同 → `ImmutabilityError`。
        """
        existing = self.get_optional(record.raw_id)
        if existing is not None:
            if existing.content_sha256 == record.content_sha256:
                return existing
            raise ImmutabilityError(f"raw_id 已存在且内容不同：{record.raw_id}")
        try:
            self._conn.execute(
                f"INSERT INTO raw_records ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.raw_id,
                    record.channel_id,
                    record.endpoint,
                    record.content_sha256,
                    record.byte_length,
                    _to_iso(record.fetched_at),
                    record.http_status,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 并发插入同一 raw_id：不静默吞掉，按契约语义翻译。
            concurrent = self.get_optional(record.raw_id)
            if concurrent is None:
                raise
            if concurrent.content_sha256 == record.content_sha256:
                return concurrent
            raise ImmutabilityError(
                f"raw_id 已被并发写入且内容不同：{record.raw_id}"
            ) from exc
        return record

    def delete(self, raw_id: str) -> None:  # pragma: no cover - 永不提供
        """`raw_records` 只增不改：删除是契约违例，在 API 层就不存在。"""
        raise NotImplementedError(
            "raw_records 只增不改（SPEC §2.3）：归档存储不提供 delete；"
            "触发器也会在 SQL 层拒绝 DELETE"
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get_optional(self, raw_id: str) -> Optional[RawRecord]:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM raw_records WHERE raw_id = ?", (raw_id,)
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def get(self, raw_id: str) -> RawRecord:
        record = self.get_optional(raw_id)
        if record is None:
            raise NotFoundError(f"raw_id 不存在：{raw_id}")
        return record

    def all_raw_ids(self) -> List[str]:
        rows = self._conn.execute("SELECT raw_id FROM raw_records ORDER BY raw_id").fetchall()
        return [row["raw_id"] for row in rows]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM raw_records").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _init_meta(self) -> None:
        row = self._conn.execute(
            "SELECT value FROM raw_store_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO raw_store_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        if int(row["value"]) != SCHEMA_VERSION:
            raise VersionError(
                f"库文件 {self._path} 的 raw schema 版本为 {row['value']}，"
                f"本代码只认 {SCHEMA_VERSION}；需要显式迁移，不做静默兼容"
            )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SqliteRawStore(db_path={str(self._path)!r})"


# ---------------------------------------------------------------------- #
# 行 ↔ 记录
# ---------------------------------------------------------------------- #


def _to_iso(moment: datetime) -> str:
    return moment.isoformat()


def _from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _row_to_record(row: sqlite3.Row) -> RawRecord:
    status = row["http_status"]
    return RawRecord(
        raw_id=row["raw_id"],
        channel_id=row["channel_id"],
        endpoint=row["endpoint"],
        content_sha256=row["content_sha256"],
        byte_length=int(row["byte_length"]),
        fetched_at=_from_iso(row["fetched_at"]),
        http_status=None if status is None else int(status),
    )
