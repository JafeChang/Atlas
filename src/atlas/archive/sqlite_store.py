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

线程安全（跨线程可用，是存储层的保证而非调用方纪律）
----------------------------------------------------

`sqlite3` 的默认连接**线程亲和**：在 A 线程创建、B 线程使用会抛
`ProgrammingError: SQLite objects created in a thread can only be used in that same thread`。
而本项目的真实消费方天生多线程（`ThreadingHTTPServer` 每请求一个线程，见 SPEC §2.11），
因此"让调用方每请求新建一个连接"只是调用方纪律 —— 任何人把单个实例接进服务就会
"单测全绿、真实路径 500"。本模块把这条保证收进存储层：

1. `sqlite3.connect(..., check_same_thread=False)`：允许连接跨线程使用；
2. `threading.RLock`：**所有**对 `self._conn` 的使用（`execute` / `commit` /
   `rollback` / `close`，含构造期的 DDL）都在锁内串行化，杜绝并发使用同一连接；
3. `timeout=BUSY_TIMEOUT_SECONDS`：跨**实例**（各自连接、同一库文件）竞争时
   由 SQLite 自己等待而不是立刻 `database is locked`。

锁必须在**整个事务**范围内持有，而不是每条语句各持一次 —— 因此
`insert()` 的"先查后插"两步在同一个 `with self._lock` 里完成；
`transaction()` 为将来的多语句写入提供同一个保证了。

`connection` 属性会返回**未加锁**的裸连接（供只读诊断与触发器测试）；
持锁写入请走 `insert()` / `transaction()`。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Union

from atlas.contracts import ImmutabilityError, NotFoundError, RawRecord, VersionError

__all__ = ["BUSY_TIMEOUT_SECONDS", "DEFAULT_DB_PATH", "SCHEMA_VERSION", "SqliteRawStore"]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本域物理 schema 版本。将来加列必须显式迁移，不静默兼容。
SCHEMA_VERSION = 1

#: 跨实例争用同一库文件时，SQLite 的等待上限（秒）。默认 5s 偏短。
BUSY_TIMEOUT_SECONDS = 30.0

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

    只实现"插入 + 查询"：`insert` / `get` / `all_raw_ids` / `count`。
    覆盖与删除在 SQL 层就被触发器拒绝，本类**没有**对应方法。

    **可跨线程使用**：连接用 `check_same_thread=False` 打开，所有连接操作在
    `self._lock`（`threading.RLock`）内串行化 —— 单个实例可以安全地交给
    `ThreadingHTTPServer` 这类每请求一线程的服务，不需要调用方"每请求新建"。
    """

    def __init__(
        self,
        db_path: Optional[_PathLike] = None,
        *,
        busy_timeout: float = BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self._path = DEFAULT_DB_PATH if db_path is None else Path(db_path)
        self._lock = threading.RLock()
        self._closed = False
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            # check_same_thread=False 解除 sqlite3 的线程亲和；并发访问由 _lock 串行化。
            self._conn = sqlite3.connect(
                str(self._path), isolation_level=None, check_same_thread=False,
                timeout=busy_timeout,
            )
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
    def lock(self) -> threading.RLock:
        """保护底层连接的锁。多语句操作请用 `transaction()`，不要手工拆锁。"""
        return self._lock

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（供只读诊断与触发器测试用）。

        **注意**：本属性返回未经加锁的裸连接；写入请走 `insert()`，多语句请走
        `transaction()`。直接在多线程里 `connection.execute(...)` 会绕过串行化。
        """
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """关闭连接（幂等）。持锁执行，避免与并发请求交叉。"""
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "SqliteRawStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """在**一把锁 + 一个事务**里执行多条语句。

        `isolation_level=None` 表示库不自动开事务，所以这里显式 `BEGIN IMMEDIATE`；
        提交与回滚都在锁内完成，锁外不留任何半成品。

        本域当前没有多语句写入（`insert` 只有单条 `INSERT`），本方法是为
        后续需要原子多语句写入时准备的，并保证那种写法一开始就是线程安全的。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # ------------------------------------------------------------------
    # 写（只有插入）
    # ------------------------------------------------------------------
    def insert(self, record: RawRecord) -> RawRecord:
        """插入一条元数据。

        已存在同一 `raw_id` 时**不覆盖**：
        - 指纹相同 → 幂等，返回已存记录；
        - 指纹不同 → `ImmutabilityError`。

        "先查后插"两步在**同一把锁**内完成：否则两个线程可能同时通过"不存在"检查，
        一个成功、另一个撞主键。锁可重入，因此内层 `get_optional` 直接调用。
        """
        with self._lock:
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
                # 同进程内已被上面的锁排除；这里是**跨实例/跨进程**抢同一主键的兜底：
                # 不静默吞掉，按契约语义翻译。
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
        with self._lock:
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT raw_id FROM raw_records ORDER BY raw_id"
            ).fetchall()
        return [row["raw_id"] for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM raw_records").fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _init_meta(self) -> None:
        """"读 schema 版本，缺失就写一条" —— 两步在同一把锁内（构造期调用）。"""
        with self._lock:
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
