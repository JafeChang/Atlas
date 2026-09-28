"""`evidence_spans` 持久化：证据锚点索引 + 只增不改触发器（SPEC §2.10，表归属 T-107）。

共用 DB 的边界（硬性要求）
--------------------------

`data/store/atlas.db` 由多个域共用。本模块**只**执行：

- `CREATE TABLE IF NOT EXISTS evidence_spans`
- `CREATE INDEX IF NOT EXISTS idx_evidence_spans_raw` / `..._sha`
- `CREATE TRIGGER IF NOT EXISTS trg_evidence_spans_no_update` / `..._no_delete`

**不做**任何破坏性 DDL（不 `DROP` / 不 `ALTER`），不碰任何其它域的表，也不 import
其它域的持久化模块。也**不**新建元数据表：SPEC §2.10 只登记了 `store_meta`（T-101 所有，
其它域只读）与 `evidence_spans`（本模块），多建一张 `evidence_store_meta` 就是往共享
库里塞一张未登记的表。

只增不改（SPEC §2.3 / §2.10）
------------------------------

`evidence_spans` 是**证据索引**，不是可覆写的草稿：一行记录一旦写入就不再改动。
"改"的表达方式是**新记录**，而记录身份是 `(claim_id, claim_version)`：

| 情形 | 结果 |
|---|---|
| 同一 claim 的**同一版本**重复校验 | **幂等**：返回已有行，不产生第二条 |
| 同一 claim 的**新版本** | **新记录**（版本推进就是 Proposed 层的"覆写"语义） |
| 同一 `(claim_id, claim_version)` 但内容不同 | `ImmutabilityError`（篡改 / 原文被换） |

三层强制，而不是靠调用方自觉：

1. `PRIMARY KEY (claim_id, claim_version)` —— 存储层唯一约束；
2. `record()` 的幂等比对（内容相同返回旧行，内容不同抛错）；
3. `BEFORE UPDATE` / `BEFORE DELETE` 触发器 —— 直接用 `sqlite3` 也改不动。

**不留半成品**：`record()` 先构造并校验完整的 `EvidenceSpan` 记录（`VERIFIED` 之外
的状态根本构造不出来），再在显式事务里插入；任何失败都 `ROLLBACK`，不会留下半行。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Union

from atlas.contracts import ImmutabilityError, ProposedClaim, VerificationStatus

from .verify import EvidenceSpan, VerificationOutcome, verify_claim

__all__ = [
    "DEFAULT_DB_PATH",
    "SqliteEvidenceStore",
    "open_evidence_store",
    "verify_and_record",
]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

_COLUMNS = (
    "claim_id, claim_version, raw_id, raw_sha256, quote, "
    "char_start, char_end, normalized_start, normalized_end, verified_at"
)

_DDL = """
-- SPEC §2.10 表归属：evidence_spans 属于 T-107（证据锚点索引；真值仍在 raw 偏移上）
CREATE TABLE IF NOT EXISTS evidence_spans (
    claim_id         TEXT    NOT NULL,
    claim_version    INTEGER NOT NULL,
    raw_id           TEXT    NOT NULL,
    raw_sha256       TEXT    NOT NULL,
    quote            TEXT    NOT NULL,
    char_start       INTEGER NOT NULL,
    char_end         INTEGER NOT NULL,
    normalized_start INTEGER NOT NULL,
    normalized_end   INTEGER NOT NULL,
    verified_at      TEXT    NOT NULL,
    PRIMARY KEY (claim_id, claim_version)
);

CREATE INDEX IF NOT EXISTS idx_evidence_spans_raw ON evidence_spans(raw_id);
CREATE INDEX IF NOT EXISTS idx_evidence_spans_sha ON evidence_spans(raw_sha256);

-- SPEC §2.3 / §2.10：证据记录只增不改，用触发器强制（不是靠调用方约定）
CREATE TRIGGER IF NOT EXISTS trg_evidence_spans_no_update
BEFORE UPDATE ON evidence_spans
BEGIN
    SELECT RAISE(ABORT, 'evidence_spans is append-only: UPDATE is forbidden (SPEC 2.10)');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_spans_no_delete
BEFORE DELETE ON evidence_spans
BEGIN
    SELECT RAISE(ABORT, 'evidence_spans is append-only: DELETE is forbidden (SPEC 2.10)');
END;
"""

_PathLike = Union[str, Path]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqliteEvidenceStore:
    """`evidence_spans` 的 SQLite 持久化（只有"插入 + 查询"，没有 update/delete）。

    `read_only=True`（T-107 的 `--read-only` 复核路径）
    ---------------------------------------------------

    只读打开的**唯一**目的是：让"只校验不落库"在**文件层**也成立。
    默认模式下构造函数会 `executescript(_DDL)`（`CREATE TABLE IF NOT EXISTS`），
    这会**改动库文件**（哪怕一条数据都没写）。对 `data/store/atlas.db` 这种
    多域共用、且是用户真实数据的库，一个声称"只读"的命令**不得**留下任何字节改动。

    因此只读模式：用 URI 的 `mode=ro` 连接（SQLite 层拒绝一切写入，不建文件、
    不建表、不写 journal），且**不执行任何 DDL**。代价是库/表不存在时读不到 ——
    这里把 `no such table` 如实解释成"0 行"（并保持可读性），而**不**去创建它。
    """

    def __init__(
        self,
        db_path: Optional[_PathLike] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        read_only: bool = False,
    ) -> None:
        self._path = DEFAULT_DB_PATH if db_path is None else Path(db_path)
        self._clock = clock or _utcnow
        self._read_only = bool(read_only)
        if self._read_only and str(self._path) == ":memory:":
            raise ValueError("read_only=True 不能配 ':memory:'（内存库没有既存数据可读）")
        if self._read_only:
            if not self._path.is_file():
                raise FileNotFoundError(
                    f"只读打开 {self._path} 失败：文件不存在。"
                    "只读模式**不创建**任何东西（那正是它的意义）；"
                    "先跑一次写入模式（不要传 read_only=True）建立证据表"
                )
            uri = f"file:{self._path}?mode=ro"
            self._conn = sqlite3.connect(uri, isolation_level=None, uri=True)
            self._conn.row_factory = sqlite3.Row
            self._has_table = self._table_exists()
            return
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._has_table = True

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _table_exists(self) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evidence_spans'"
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（供只读诊断与触发器测试用）。写入请走 `record()`。"""
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteEvidenceStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 写（只有插入）
    # ------------------------------------------------------------------
    def record(self, outcome: VerificationOutcome) -> Optional[EvidenceSpan]:
        """把一次校验的结果落成证据行。

        - `VERIFIED`：插入一行；同一 `(claim_id, claim_version)` 已存在且内容相同 → 幂等返回旧行；
          内容不同（锚点被改 / 原文被换）→ `ImmutabilityError`。
        - 其它状态（`FAILED` / `UNVERIFIED`）：**不写任何东西**，返回 `None`
          —— 未取得锚点的证据不进入 `evidence_spans`（SPEC §2.2 / §2.3）。
          这是明确的状态分支，不是被 `except` 吞掉的错误。
        """
        if outcome.status is not VerificationStatus.VERIFIED:
            return None
        if self._read_only:
            raise ImmutabilityError(
                "只读打开的 SqliteEvidenceStore 不得写入（read_only=True）；"
                "需要落库请用默认的写入模式打开"
            )

        span = EvidenceSpan.from_outcome(outcome, verified_at=self._clock())
        existing = self.span_for(span.claim_id, span.claim_version)
        if existing is not None:
            if existing.identity_fields() == span.identity_fields():
                return existing
            raise ImmutabilityError(
                f"claim {span.claim_id}@v{span.claim_version} 已有证据记录 "
                f"[{existing.char_start}, {existing.char_end})，与本次校验结果 "
                f"[{span.char_start}, {span.char_end}) 不一致：evidence_spans 只增不改"
            )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                f"INSERT INTO evidence_spans ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    span.claim_id,
                    span.claim_version,
                    span.raw_id,
                    span.raw_sha256,
                    span.quote,
                    span.char_start,
                    span.char_end,
                    span.normalized_start,
                    span.normalized_end,
                    span.verified_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            concurrent = self.span_for(span.claim_id, span.claim_version)
            if concurrent is not None and (
                concurrent.identity_fields() == span.identity_fields()
            ):
                return concurrent
            raise ImmutabilityError(
                f"claim {span.claim_id}@v{span.claim_version} 已被并发写入且内容不同"
            ) from exc
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return span

    def delete(self, claim_id: str, claim_version: int) -> None:  # pragma: no cover
        """`evidence_spans` 只增不改：删除是契约违例，在 API 层就不存在。"""
        raise NotImplementedError(
            "evidence_spans 只增不改（SPEC §2.3）：不提供 delete；"
            "触发器也会在 SQL 层拒绝 DELETE"
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def span_for(self, claim_id: str, claim_version: int) -> Optional[EvidenceSpan]:
        if not self._has_table:
            return None
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM evidence_spans WHERE claim_id = ? AND claim_version = ?",
            (claim_id, claim_version),
        ).fetchone()
        return None if row is None else _row_to_span(row)

    def spans_for_claim(self, claim_id: str) -> List[EvidenceSpan]:
        if not self._has_table:
            return []
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM evidence_spans WHERE claim_id = ? "
            "ORDER BY claim_version",
            (claim_id,),
        ).fetchall()
        return [_row_to_span(row) for row in rows]

    def spans_for_raw(self, raw_id: str) -> List[EvidenceSpan]:
        if not self._has_table:
            return []
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM evidence_spans WHERE raw_id = ? "
            "ORDER BY claim_id, claim_version",
            (raw_id,),
        ).fetchall()
        return [_row_to_span(row) for row in rows]

    def count(self) -> int:
        if not self._has_table:
            return 0
        row = self._conn.execute("SELECT COUNT(*) AS n FROM evidence_spans").fetchone()
        return int(row["n"])

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SqliteEvidenceStore(db_path={str(self._path)!r})"


def _row_to_span(row: sqlite3.Row) -> EvidenceSpan:
    return EvidenceSpan(
        claim_id=row["claim_id"],
        claim_version=int(row["claim_version"]),
        raw_id=row["raw_id"],
        raw_sha256=row["raw_sha256"],
        quote=row["quote"],
        char_start=int(row["char_start"]),
        char_end=int(row["char_end"]),
        normalized_start=int(row["normalized_start"]),
        normalized_end=int(row["normalized_end"]),
        verified_at=datetime.fromisoformat(row["verified_at"]),
    )


def verify_and_record(
    store: SqliteEvidenceStore,
    claim: ProposedClaim,
    raw_bytes: bytes,
    *,
    content_type: str = "",
) -> VerificationOutcome:
    """校验 + 落库的常用组合（T-120 接线用）。

    校验失败**不是**写库失败：`FAILED` 的 claim 得到一个 `FAILED` 的 outcome，
    库里不多一行（`record()` 明确返回 `None`），调用方据此把它标成"未验证"。
    """
    outcome = verify_claim(claim, raw_bytes, content_type=content_type)
    store.record(outcome)
    return outcome


def open_evidence_store(
    db_path: Optional[_PathLike] = None,
    *,
    clock: Optional[Callable[[], datetime]] = None,
    read_only: bool = False,
) -> SqliteEvidenceStore:
    """便捷入口：打开（必要时创建）库文件并返回证据存储。

    `read_only=True` 时**不创建任何东西**（文件 / 表都不建，连接用 SQLite 的 `mode=ro`）。
    """
    return SqliteEvidenceStore(db_path, clock=clock, read_only=read_only)
