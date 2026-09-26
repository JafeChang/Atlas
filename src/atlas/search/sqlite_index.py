"""T-205 检索索引的 SQLite/FTS5 持久化（SPEC §2.10 / §4.3 T-205）。

**索引是只读投影**（判据 A3）：它不产生任何事实，随时可删、可从 raw 全量重建。
本模块因此没有任何指向 `raw_records` / `confirmed_labels` / `proposed_claims` /
`evidence_spans` 的写路径——表名一律以 `search_` 开头，`drop()` 也只删自己的表。

表归属（SPEC §2.10：新增表必须先登记）
--------------------------------------

| 表 | 说明 |
|---|---|
| `search_meta` | 索引身份与配置（`schema_version` / `index_version` / `tokenizer` / `segmentation` / `text_source` / `document_count` / `corpus_sha256` / `industry_source` / `built_at` / `index_built`） |
| `search_documents` | 文档级投影：`RawRecord` 元数据 + T-104 归一化文本（**一文档一行**，不分块）+ **切分后的索引列** `text_index` |
| `search_documents_fts` | FTS5 外部内容表（`content='search_documents'`，列名 `text_index`）：只存倒排索引，不重复存文本 |

三张表都是本域私有，全部以 `search_` 前缀命名，避免 `CREATE TABLE IF NOT EXISTS`
把别的域的表静默复用成结构不同的表（SPEC §2.10 的登记规则）。

为什么索引列与展示列分开（T-205 修订 / 判据 C2）
-----------------------------------------------

`unicode61` 不切分连续汉字，因此索引列存 `segment_cjk(text)`（`中 文 分 词`），
而 `text` 保持**原始归一化文本**（对外展示、摘要的来源）。两列分工：

- `text_index`：**内部列**，只进 FTS5 倒排索引，任何对外返回路径都不含它；
- `text`：对外唯一的正文来源（`IndexedDocument.text`、摘要的原文片段）。

FTS5 外部内容表**要求 FTS 列名与内容表列名一致**（不一致时 SQLite 直接报
`no such column: T.<name>`，实测），所以 FTS 只声明一列且命名 `text_index`。
外部内容表**不会**自动跟随内容表变化，必须显式 `'rebuild'`——`rebuild()` 里就有这一步。

摘要：`snippet()` 作用在 `text_index` 上，返回前必须还原（见 `atlas.search.snippet`），
否则摘要会露出 `中 文` 这种**实现细节外泄**。

**为什么这里没有 append-only 触发器**：`search_*` 是**派生物**（索引），不是事实层。
SPEC §2.10 要求用触发器强制"只增不改"的对象是 Raw / Confirmed / 配置版本链这些
**不可重建的事实**；派生物必须可以整体删除并重建，加 append-only 触发器会直接
自相矛盾。这与 T-101 对派生投影表（`industries` / `channels` / `store_meta`）的处理
一致——那里同样不设触发器。索引的"不可信"由 `index_version` / `corpus_sha256`
与"随时可删"共同保证，而不是由禁止 UPDATE 保证。

FTS5 可用性必须**显式探测**（判据 A1）
--------------------------------------

构造时用一条独立语句探测 `CREATE VIRTUAL TABLE temp.… USING fts5(…, tokenize=…)`：
既验证 FTS5 模块被编译进来，也验证本项目使用的分词器配置被接受。
失败抛 `Fts5UnavailableError`，**绝不**降级成 `LIKE`（本包 SQL 字面量里没有
`LIKE` / `GLOB`，由 `tests/test_search_invariants.py` 静态钉死）。
只把"no such module"翻译成本异常；其它 `OperationalError`（例如 `database is locked`）
**原样上抛**，不掩盖接线错误。

可全量重建（判据 A2）
--------------------

`drop()` → `rebuild()` 之后，同一批查询的命中顺序、分数、摘要必须完全相同：

1. 文档按 `raw_id` 升序写入，`doc_rowid` 显式赋值为 `1..N` ⇒ 行号可复现；
2. FTS 索引用 FTS5 的 `'rebuild'` 指令从内容表整体重建，再 `'optimize'`；
3. `corpus_sha256` = 排序后 `(raw_id, text_sha256)` 序列的摘要 ⇒ 语料身份可比对；
4. `index_version` / `tokenizer` / `text_source` 落 `search_meta` ⇒ 重建可复现，
   版本不符时读路径**响亮失败**并指向"删索引后重建"。

时间键
------

`fetched_at` 存**定宽 UTC ISO 串**（`atlas.search.query.canonical_utc_iso`），
因此范围筛选与排序可以直接在 SQL 里用字符串比较，不需要 Python 侧解析，
也不会因混合时区偏移而排错序。

线程安全（沿用 T-103 已定的模式，不另发明）
------------------------------------------

`sqlite3` 的连接**线程亲和**在本项目已踩过坑（真实路径 500，见 T-103 的
`tests/test_archive_threading.py`）。本项目真实消费方天生多线程
（`ThreadingHTTPServer` 每请求一线程，SPEC §2.11），因此本模块照抄
`atlas.archive.sqlite_store.SqliteRawStore` 的三件套：
`check_same_thread=False` + 一把 `threading.RLock` 串行化**所有**连接使用 +
`timeout=BUSY_TIMEOUT_SECONDS`。单个实例可直接接进多线程服务，
不需要调用方"每请求新建"。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Union

from .cjk import SEGMENTATION_VERSION, segment_cjk
from .documents import DocumentText
from .errors import (
    Fts5UnavailableError,
    IndexVersionError,
    SearchIndexError,
    SearchQueryError,
)
from .query import (
    ORDER_RELEVANCE,
    Boost,
    SearchHit,
    SearchQuery,
    SearchResult,
    apply_boost,
    canonical_utc_iso,
    parse_canonical_utc,
    rank_hits,
)
from .snippet import (
    SNIPPET_SENTINEL_CLOSE,
    SNIPPET_SENTINEL_ELLIPSIS,
    SNIPPET_SENTINEL_OPEN,
    restore_snippet,
)

__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "DEFAULT_DB_PATH",
    "DOCS_TABLE",
    "FTS_TABLE",
    "INDEX_VERSION",
    "INDUSTRY_SOURCE_INJECTED",
    "INDUSTRY_SOURCE_NONE",
    "MAX_CANDIDATES",
    "MAX_DOCUMENTS",
    "META_TABLE",
    "SCHEMA_VERSION",
    "SEGMENTATION",
    "TOKENIZER",
    "TEXT_SOURCE",
    "IndexReport",
    "IndexedDocument",
    "SqliteSearchIndex",
    "drop_search_index",
    "open_index",
    "probe_fts5",
]

#: SPEC §2.10 的目录布局：元数据与配置共用同一个库文件。
DEFAULT_DB_PATH = Path("data/store/atlas.db")

#: 本域物理 schema 版本。**2** = 增加 `search_documents.text_index`（T-205 修订）。
#: 将来加列/加表必须显式迁移，不静默兼容。
SCHEMA_VERSION = 2

#: 索引语义版本：**分词器、切分规则、文本来源、打分函数、内容表形状**任一变化都必须升版本。
#: 读者据此判定"库里的索引是不是本代码的索引"（判据 A2 / C3）。
#: **2** = 汉字逐字切分（`text_index` 列 + 汉字短语查询），v1 索引必须弃用重建。
INDEX_VERSION = "atlas.search.index/2"

#: FTS5 分词器配置。`remove_diacritics 2` = 连非 ASCII 的变音符号也折叠。
TOKENIZER = "unicode61 remove_diacritics 2"

#: 汉字切分规则的身份（写进 `search_meta`，让重建可复现）。
SEGMENTATION = (
    SEGMENTATION_VERSION
    + "：segment_cjk（汉字逐字切分，U+3400-U+4DBF/U+4E00-U+9FFF/U+F900-U+FAFF/"
    "U+20000-U+2EBEF/U+2F800-U+2FA1F/U+30000-U+323AF）"
)

#: 被索引文本的来源（写进 `search_meta`，让重建可复现）。
TEXT_SOURCE = (
    "atlas.normalize.normalize -> NormalizedText.text（T-104 文档级归一化文本，不分块）"
    " -> atlas.search.cjk.segment_cjk（T-205 修订：汉字逐字切分后进倒排索引）"
)

#: 跨实例争用同一库文件时，SQLite 的等待上限（秒）。与 T-103 同值。
BUSY_TIMEOUT_SECONDS = 30.0

#: 一次重建允许的文档数上限：超过即报错，绝不静默只索引一部分。
MAX_DOCUMENTS = 200_000

#: boost（第二个打分源）需要全量候选，允许的候选上限：超限报错不截断。
MAX_CANDIDATES = 200_000

#: `search_meta.industry_source` 的取值：建索引时是否注入了 渠道→行业 映射。
INDUSTRY_SOURCE_INJECTED = "injected"
INDUSTRY_SOURCE_NONE = "none"

META_TABLE = "search_meta"
DOCS_TABLE = "search_documents"
FTS_TABLE = "search_documents_fts"

KEY_SCHEMA_VERSION = "schema_version"
KEY_INDEX_VERSION = "index_version"
KEY_TOKENIZER = "tokenizer"
KEY_SEGMENTATION = "segmentation"
KEY_TEXT_SOURCE = "text_source"
KEY_DOCUMENT_COUNT = "document_count"
KEY_EMPTY_TEXT_COUNT = "empty_text_count"
KEY_CORPUS_SHA256 = "corpus_sha256"
KEY_INDUSTRY_SOURCE = "industry_source"
KEY_BUILT_AT = "built_at"
KEY_INDEX_BUILT = "index_built"

_META_KEYS = (
    KEY_INDEX_VERSION,
    KEY_TOKENIZER,
    KEY_SEGMENTATION,
    KEY_TEXT_SOURCE,
    KEY_DOCUMENT_COUNT,
    KEY_EMPTY_TEXT_COUNT,
    KEY_CORPUS_SHA256,
    KEY_INDUSTRY_SOURCE,
    KEY_BUILT_AT,
    KEY_INDEX_BUILT,
)

#: 只删**本域**的表，绝不 DROP 别的域的表（§2.10 表归属）。
_DROP_DDL = f"""
DROP TABLE IF EXISTS {FTS_TABLE};
DROP TABLE IF EXISTS {DOCS_TABLE};
DROP TABLE IF EXISTS {META_TABLE};
"""

_DDL = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- 文档级投影：RawRecord 元数据 + T-104 归一化文本。**一文档一行**，不分块（T-206 负责分块）。
-- `text` 是**原始**归一化文本（对外展示 + 摘要的原文来源）；
-- `text_index` 是**内部**索引列 = segment_cjk(text)（汉字逐字切分），只进 FTS5 倒排索引。
CREATE TABLE IF NOT EXISTS {DOCS_TABLE} (
    doc_rowid      INTEGER PRIMARY KEY,
    raw_id         TEXT NOT NULL UNIQUE,
    channel_id     TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    industry       TEXT,
    content_sha256 TEXT NOT NULL,
    byte_length    INTEGER NOT NULL,
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER,
    text           TEXT NOT NULL,
    text_index     TEXT NOT NULL,
    text_sha256    TEXT NOT NULL,
    text_length    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_documents_channel  ON {DOCS_TABLE}(channel_id);
CREATE INDEX IF NOT EXISTS idx_search_documents_industry ON {DOCS_TABLE}(industry);
CREATE INDEX IF NOT EXISTS idx_search_documents_fetched  ON {DOCS_TABLE}(fetched_at);

-- 外部内容表：倒排索引只此一份，正文仍由 search_documents 提供（不重复存文本）。
-- 列名必须与内容表列名一致（FTS5 外部内容表的硬要求），因此这里只有 `text_index` 一列。
-- 注意：外部内容表不会自动跟随内容表变化，写入后必须显式 `'rebuild'`（rebuild() 里已做）。
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5(
    text_index,
    content='{DOCS_TABLE}',
    content_rowid='doc_rowid',
    tokenize="{TOKENIZER}"
);
"""

_DOC_COLUMNS = (
    "doc_rowid, raw_id, channel_id, endpoint, industry, content_sha256, "
    "byte_length, fetched_at, http_status, text, text_index, text_sha256, text_length"
)
_INSERT_SQL = (
    f"INSERT INTO {DOCS_TABLE} ({_DOC_COLUMNS}) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_COLUMNS = (
    f"{DOCS_TABLE}.raw_id, {DOCS_TABLE}.channel_id, {DOCS_TABLE}.endpoint, "
    f"{DOCS_TABLE}.industry, {DOCS_TABLE}.content_sha256, {DOCS_TABLE}.byte_length, "
    f"{DOCS_TABLE}.fetched_at, {DOCS_TABLE}.http_status"
)

#: 摘要还原需要的两列：**原始文本**（对外片段来源）与**内部索引列**（定位插入的分隔符）。
#: 两者都不进 `SearchHit`，`text_index` 因此没有任何对外返回路径。
_SNIPPET_COLUMNS = f"{DOCS_TABLE}.text, {DOCS_TABLE}.text_index"

_PathLike = Union[str, Path]


# --------------------------------------------------------------------------- #
# FTS5 探测
# --------------------------------------------------------------------------- #
def probe_fts5(connection: Optional[sqlite3.Connection] = None) -> None:
    """探测当前 SQLite 是否可用 FTS5，且本项目的分词器配置是否被接受。

    - `connection is None`：自建一条 `:memory:` 连接探测后关闭；
    - 传入连接：在 `temp` schema 里建/删探针表，**不污染**调用方的库文件。

    FTS5 缺失（`no such module`）→ `Fts5UnavailableError`（判据 A1）。
    其它 `OperationalError` **原样上抛**——不把接线错误伪装成"FTS5 不可用"。
    """
    own = connection is None
    conn = sqlite3.connect(":memory:") if own else connection
    assert conn is not None
    try:
        conn.execute(
            f'CREATE VIRTUAL TABLE temp.__atlas_fts5_probe USING fts5(x, tokenize="{TOKENIZER}")'
        )
        conn.execute("DROP TABLE temp.__atlas_fts5_probe")
    except sqlite3.OperationalError as exc:
        if "no such module" not in str(exc).lower():
            raise
        raise Fts5UnavailableError(
            "当前 SQLite 未编译 FTS5，无法提供全文检索："
            f"{exc}。FTS5 是 SPEC §2.10 选定的零依赖方案，"
            "本层拒绝降级成 LIKE 假装是全文检索——请换用带 FTS5 的 SQLite 构建。"
        ) from exc
    finally:
        if own:
            conn.close()


def drop_search_index(db_path: Optional[_PathLike] = None) -> None:
    """删除**只属于检索索引**的表（`search_meta` / `search_documents` / `*_fts`）。

    存在的意义是给"索引版本不符"提供补救路径：`SqliteSearchIndex` 在 schema
    版本不符时会拒绝打开，而重建又必须先能打开——于是先删、再开、再重建。
    本函数**不触碰**其它域的任何表。
    """
    path = DEFAULT_DB_PATH if db_path is None else Path(db_path)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.executescript(_DROP_DDL)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndexedDocument:
    """索引里一条文档级记录（`get(raw_id)` 的返回；**没有分数**，分数只在查询里存在）。"""

    raw_id: str
    channel_id: str
    endpoint: str
    industry: str | None
    content_sha256: str
    byte_length: int
    fetched_at: datetime
    http_status: int | None
    text: str
    text_sha256: str
    text_length: int


@dataclass(frozen=True)
class IndexReport:
    """一次 `rebuild()` 的结果（供证据与诊断；`built_at` 是 observed，不参与身份）。"""

    document_count: int
    empty_text_count: int
    corpus_sha256: str
    index_version: str
    tokenizer: str
    segmentation: str
    text_source: str
    industry_source: str
    built_at: datetime

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document_count": self.document_count,
            "empty_text_count": self.empty_text_count,
            "corpus_sha256": self.corpus_sha256,
            "index_version": self.index_version,
            "tokenizer": self.tokenizer,
            "segmentation": self.segmentation,
            "text_source": self.text_source,
            "industry_source": self.industry_source,
            "built_at": self.built_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# 索引
# --------------------------------------------------------------------------- #
class SqliteSearchIndex:
    """`atlas.search` 的 SQLite/FTS5 实现。

    ``SqliteSearchIndex(db_path)``：默认 `data/store/atlas.db`（与归档/注册表同库）。

    **可跨线程使用**：连接以 `check_same_thread=False` 打开，所有连接操作在
    `self._lock`（`threading.RLock`）内串行化——与 T-103 的 `SqliteRawStore` 同一模式。
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
        # 先探测（判据 A1）：FTS5 不可用时在建立任何东西之前就响亮失败。
        probe_fts5()
        if str(self._path) != ":memory:":
            parent = self._path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._conn = sqlite3.connect(
                str(self._path),
                isolation_level=None,
                check_same_thread=False,
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
        """保护底层连接的锁。多语句操作请用 `transaction()`。"""
        return self._lock

    @property
    def connection(self) -> sqlite3.Connection:
        """底层连接（只读诊断用；**未经加锁**，写入请走本类方法）。"""
        return self._conn

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "SqliteSearchIndex":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """在**一把锁 + 一个事务**里执行多条语句（提交/回滚都在锁内）。"""
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
    # 元信息
    # ------------------------------------------------------------------
    def meta(self) -> Dict[str, str]:
        """索引身份与配置的只读快照（`search_meta` 全表）。

        表不存在（索引刚被 `drop()`）时返回空字典——"索引不在"是合法状态，
        由 `is_built()` / `_assert_usable()` 负责把它与"有索引"区分开。
        """
        with self._lock:
            if not self._has_table(META_TABLE):
                return {}
            rows = self._conn.execute(
                f"SELECT key, value FROM {META_TABLE} ORDER BY key"
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def is_built(self) -> bool:
        """索引是否处于"已构建且版本可用"的状态。"""
        meta = self.meta()
        return (
            meta.get(KEY_INDEX_BUILT) == "1"
            and meta.get(KEY_INDEX_VERSION) == INDEX_VERSION
        )

    def count(self) -> int:
        """索引内的文档条数（诊断用；索引不存在时为 0）。"""
        with self._lock:
            if not self._has_table(DOCS_TABLE):
                return 0
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {DOCS_TABLE}"
            ).fetchone()
        return int(row["n"])

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return int(row["n"]) > 0

    # ------------------------------------------------------------------
    # 写：只有"重建"与"删除"，没有任何指向上游事实的写路径
    # ------------------------------------------------------------------
    def drop(self) -> None:
        """删除索引（只删本域的表）。上游 raw / Confirmed / Proposed 不受影响。

        删除是**真删**（`search_meta` / `search_documents` / `search_documents_fts`
        连影子表一起消失），使"索引可随时删除"成立；`rebuild()` 会按需重新建表。
        """
        with self._lock:
            self._conn.executescript(_DROP_DDL)

    def rebuild(
        self,
        documents: Iterable[DocumentText],
        *,
        industry_of: Callable[[str], str | None] | Mapping[str, str] | None = None,
        built_at: Optional[datetime] = None,
    ) -> IndexReport:
        """**全量重建**索引：建表（如缺）→ 清空 → 按 `raw_id` 升序写入 → 重建 FTS → 记录身份。

        - `documents`：文档级输入（`DocumentText`：`RawRecord` + T-104 归一化文本）；
        - `industry_of`：注入的 渠道→行业 映射（SPEC §2.5 的 C8 闭环接口）。
          为 `None` 时索引里 `industry` 全为 `NULL`，并记录
          `industry_source=none`——此时**按行业筛选会响亮失败**，
          因为那正是"能跑但闭环断开"（§2.5 的警告）的形态；
        - 同一 `raw_id` 重复出现且文本一致 → 幂等跳过（与 `RawStore.put` 同语义）；
          文本不一致 → 响亮失败（同一 raw_id 不可能有两份归一化文本）。

        整个写入在**一个事务**里完成：失败即回滚，不留半成品索引。
        建表语句是幂等的 `CREATE ... IF NOT EXISTS`，因此 `drop()` 之后无需重新构造对象。
        """
        lookup = _as_industry_lookup(industry_of)
        industry_source = (
            INDUSTRY_SOURCE_NONE if lookup is None else INDUSTRY_SOURCE_INJECTED
        )
        rows, empty_text_count, corpus_sha256 = _prepare_documents(documents, lookup)

        moment = built_at if built_at is not None else datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        with self._lock:
            # `executescript` 会隐式提交，因此必须在 BEGIN 之前。
            self._conn.executescript(_DDL)
            self._init_meta()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(f"DELETE FROM {DOCS_TABLE}")
                for index, (document, industry) in enumerate(rows, start=1):
                    self._conn.execute(_INSERT_SQL, _document_row(index, document, industry))
                # 外部内容表整体重建：FTS5 从 search_documents 读回正文并重排倒排索引。
                self._conn.execute(
                    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')"
                )
                self._conn.execute(
                    f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('optimize')"
                )
                for key in _META_KEYS:
                    self._conn.execute(
                        f"DELETE FROM {META_TABLE} WHERE key = ?", (key,)
                    )
                self._write_meta(
                    {
                        KEY_INDEX_VERSION: INDEX_VERSION,
                        KEY_TOKENIZER: TOKENIZER,
                        KEY_SEGMENTATION: SEGMENTATION,
                        KEY_TEXT_SOURCE: TEXT_SOURCE,
                        KEY_DOCUMENT_COUNT: str(len(rows)),
                        KEY_EMPTY_TEXT_COUNT: str(empty_text_count),
                        KEY_CORPUS_SHA256: corpus_sha256,
                        KEY_INDUSTRY_SOURCE: industry_source,
                        KEY_BUILT_AT: moment.astimezone(timezone.utc).isoformat(),
                        KEY_INDEX_BUILT: "1",
                    }
                )
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

        return IndexReport(
            document_count=len(rows),
            empty_text_count=empty_text_count,
            corpus_sha256=corpus_sha256,
            index_version=INDEX_VERSION,
            tokenizer=TOKENIZER,
            segmentation=SEGMENTATION,
            text_source=TEXT_SOURCE,
            industry_source=industry_source,
            built_at=moment.astimezone(timezone.utc),
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, raw_id: str) -> Optional[IndexedDocument]:
        """按 `raw_id` 取单条索引记录；不存在返回 `None`。

        索引未构建时**响亮失败**（`SearchIndexError`），不把"索引不在"
        伪装成"这条文档不在"。
        """
        if not isinstance(raw_id, str) or not raw_id:
            raise SearchQueryError("raw_id", "必须是非空字符串")
        self._assert_usable()
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_DOC_COLUMNS} FROM {DOCS_TABLE} WHERE raw_id = ?", (raw_id,)
            ).fetchone()
        return None if row is None else _row_to_document(row)

    def search(self, query: SearchQuery, *, boost: Optional[Boost] = None) -> SearchResult:
        """执行一次只读检索。

        - `boost is None`：排序与切片完全在 SQL 里完成（快路径）；
        - `boost` 提供时：先取**全部**命中作为候选（上限 `MAX_CANDIDATES`，超限报错
          不截断），把 `boost` 返回的附加分加到 `score` 上，再按同一套排序键重排。
          这就是"打分可组合"的落点，T-201 的第二打分源（向量）从同一个位置接入。
        """
        if not isinstance(query, SearchQuery):
            raise SearchQueryError(
                "query", f"必须是 SearchQuery（收到 {type(query).__name__}）"
            )
        self._assert_usable()
        if query.industries and self.meta().get(KEY_INDUSTRY_SOURCE) == INDUSTRY_SOURCE_NONE:
            raise SearchIndexError(
                "该索引在建时未注入 industry_of（industry_source=none），"
                "按行业筛选会静默返回全空（SPEC §2.5 警告的'闭环断开'形态）；"
                "请注入 渠道→行业 映射后重建索引，而不是接受一个看起来正常的空结果"
            )

        expression = query.match_expression()
        where, where_params = _filter_clause(query)
        total = self._count(expression, where, where_params)

        if boost is None:
            hits = self._fetch(
                query,
                expression,
                where,
                where_params,
                limit=query.offset + query.limit,
            )
            page = hits[query.offset : query.offset + query.limit]
            candidates = len(page)
            boosted = False
        else:
            if total > MAX_CANDIDATES:
                raise SearchIndexError(
                    f"命中 {total} 条，超过 boost 重排的候选上限 {MAX_CANDIDATES}；"
                    "拒绝静默截断候选（那会让排序结果不完整）"
                )
            hits = self._fetch(query, expression, where, where_params, limit=MAX_CANDIDATES)
            ranked = rank_hits(apply_boost(hits, boost), query)
            page = ranked[query.offset : query.offset + query.limit]
            candidates = len(ranked)
            boosted = True

        returned = len(page)
        has_more = query.offset + returned < total
        return SearchResult(
            query=query,
            items=tuple(page),
            total=total,
            limit=query.limit,
            offset=query.offset,
            has_more=has_more,
            next_offset=(query.offset + returned) if has_more else None,
            expression=expression,
            boosted=boosted,
            candidates=candidates,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _init_meta(self) -> None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT value FROM {META_TABLE} WHERE key = ?", (KEY_SCHEMA_VERSION,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?)",
                    (KEY_SCHEMA_VERSION, str(SCHEMA_VERSION)),
                )
                return
        if int(row["value"]) != SCHEMA_VERSION:
            raise IndexVersionError(
                f"库文件 {self._path} 的检索索引 schema 版本为 {row['value']}，"
                f"本代码只认 {SCHEMA_VERSION}（T-205 修订：schema 2 增加了"
                " search_documents.text_index，汉字逐字切分后才能被检索）；"
                "索引是可重建派生物，补救路径是 drop_search_index(db_path) 删掉后"
                " rebuild() 重建，不做静默兼容"
            )

    def _assert_usable(self) -> None:
        meta = self.meta()
        if meta.get(KEY_INDEX_BUILT) != "1":
            raise SearchIndexError(
                f"检索索引不存在或未构建（{self._path}）：索引只是 raw 的只读投影，"
                "请先用 rebuild() 从归档重建，而不是把'索引不在'当成'没有命中'"
            )
        stored = meta.get(KEY_INDEX_VERSION)
        if stored != INDEX_VERSION:
            raise IndexVersionError(
                f"索引版本不符：库内 {stored!r}，本代码 {INDEX_VERSION!r}"
                f"（tokenizer={meta.get(KEY_TOKENIZER)!r}, "
                f"segmentation={meta.get(KEY_SEGMENTATION)!r}）；"
                "索引可全量重建，请 drop() 后 rebuild()"
                "（跨进程请用 drop_search_index(db_path)），不做静默兼容"
            )

    def _write_meta(self, values: Mapping[str, str]) -> None:
        for key, value in values.items():
            self._conn.execute(
                f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?)", (key, value)
            )

    def _count(self, expression: str, where: str, params: List[Any]) -> int:
        sql = (
            f"SELECT COUNT(*) AS n FROM {FTS_TABLE} "
            f"JOIN {DOCS_TABLE} ON {DOCS_TABLE}.doc_rowid = {FTS_TABLE}.rowid "
            f"WHERE {FTS_TABLE} MATCH ?{where}"
        )
        with self._lock:
            row = self._conn.execute(sql, [expression, *params]).fetchone()
        return int(row["n"])

    def _fetch(
        self,
        query: SearchQuery,
        expression: str,
        where: str,
        params: List[Any],
        *,
        limit: int,
    ) -> List[SearchHit]:
        sql = (
            f"SELECT {_SELECT_COLUMNS}, {_SNIPPET_COLUMNS}, "
            f"-bm25({FTS_TABLE}) AS fts_score, "
            f"snippet({FTS_TABLE}, 0, ?, ?, ?, ?) AS snippet "
            f"FROM {FTS_TABLE} "
            f"JOIN {DOCS_TABLE} ON {DOCS_TABLE}.doc_rowid = {FTS_TABLE}.rowid "
            f"WHERE {FTS_TABLE} MATCH ?{where} "
            f"ORDER BY {_order_by(query)} "
            "LIMIT ?"
        )
        # 摘要用**私用区哨兵**做标记：`snippet()` 作用在切分后的列（text_index）上，
        # 返回的是带插入分隔符的文本，必须还原成原始文本上的摘要（见 snippet.restore_snippet）。
        args: List[Any] = [
            SNIPPET_SENTINEL_OPEN,
            SNIPPET_SENTINEL_CLOSE,
            SNIPPET_SENTINEL_ELLIPSIS,
            query.snippet_tokens,
            expression,
            *params,
            limit,
        ]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_hit(row) for row in rows]


# --------------------------------------------------------------------------- #
# 辅助：文档准备
# --------------------------------------------------------------------------- #
def _prepare_documents(
    documents: Iterable[DocumentText],
    lookup: Optional[Callable[[str], str | None]],
) -> tuple[List[tuple[DocumentText, Optional[str]]], int, str]:
    """物化 + 去重 + 按 `raw_id` 排序 + 计算语料摘要（全部在写入前完成）。"""
    if isinstance(documents, DocumentText):
        raise SearchIndexError("documents 必须是可迭代的 DocumentText，而不是单个实例")
    by_id: Dict[str, DocumentText] = {}
    for document in documents:
        if not isinstance(document, DocumentText):
            raise SearchIndexError(
                "索引输入必须是 atlas.search.documents.DocumentText，"
                f"收到 {type(document).__name__}"
            )
        previous = by_id.get(document.raw_id)
        if previous is not None:
            if previous.text_sha256 == document.text_sha256:
                continue  # 幂等：同 raw_id 同文本
            raise SearchIndexError(
                f"同一 raw_id 给出了两份不同的归一化文本：{document.raw_id}"
            )
        by_id[document.raw_id] = document
        if len(by_id) > MAX_DOCUMENTS:
            raise SearchIndexError(
                f"文档数超过重建上限 {MAX_DOCUMENTS}；拒绝静默只索引一部分"
            )

    ordered = [by_id[raw_id] for raw_id in sorted(by_id)]
    digest = hashlib.sha256()
    empty = 0
    rows: List[tuple[DocumentText, Optional[str]]] = []
    for document in ordered:
        if not document.text.strip():
            empty += 1
        digest.update(document.raw_id.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(document.text_sha256.encode("ascii"))
        digest.update(b"\x1e")
        industry = None
        if lookup is not None:
            industry = lookup(document.record.channel_id)
            if industry is not None and not isinstance(industry, str):
                raise SearchIndexError(
                    "industry_of 必须返回 str 或 None，"
                    f"收到 {type(industry).__name__}（channel_id={document.record.channel_id!r}）"
                )
        rows.append((document, industry))
    return rows, empty, digest.hexdigest()


def _document_row(
    doc_rowid: int, document: DocumentText, industry: Optional[str]
) -> tuple:
    record = document.record
    return (
        doc_rowid,
        record.raw_id,
        record.channel_id,
        record.endpoint,
        industry,
        record.content_sha256,
        int(record.byte_length),
        canonical_utc_iso(record.fetched_at),
        None if record.http_status is None else int(record.http_status),
        document.text,
        segment_cjk(document.text),
        document.text_sha256,
        document.text_length,
    )


def _as_industry_lookup(
    source: Callable[[str], str | None] | Mapping[str, str] | None,
) -> Optional[Callable[[str], str | None]]:
    """把 `None` / 映射 / 可调用统一成"可调用或 None"。

    `None` 保留为 `None`（而不是恒返回 `None` 的函数）——"没注入"与
    "注入了但查不到"必须可区分（§2.5 的闭环断开警告）。
    """
    if source is None:
        return None
    if callable(source):
        return source
    mapping: Dict[str, str] = dict(source)
    return lambda channel_id: mapping.get(channel_id)


def _filter_clause(query: SearchQuery) -> tuple[str, List[Any]]:
    clause = ""
    params: List[Any] = []
    for column, values in (
        (f"{DOCS_TABLE}.channel_id", query.channels),
        (f"{DOCS_TABLE}.industry", query.industries),
        (f"{DOCS_TABLE}.raw_id", query.raw_ids),
    ):
        if not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        clause += f" AND {column} IN ({placeholders})"
        params.extend(values)
    if query.since is not None:
        clause += f" AND {DOCS_TABLE}.fetched_at >= ?"
        params.append(canonical_utc_iso(query.since))
    if query.until is not None:
        clause += f" AND {DOCS_TABLE}.fetched_at <= ?"
        params.append(canonical_utc_iso(query.until))
    return clause, params


def _order_by(query: SearchQuery) -> str:
    if query.order == ORDER_RELEVANCE:
        return (
            f"-bm25({FTS_TABLE}) DESC, {DOCS_TABLE}.fetched_at DESC, "
            f"{DOCS_TABLE}.raw_id ASC"
        )
    return f"{DOCS_TABLE}.fetched_at DESC, {DOCS_TABLE}.raw_id ASC"


def _row_to_hit(row: sqlite3.Row) -> SearchHit:
    score = float(row["fts_score"])
    return SearchHit(
        raw_id=row["raw_id"],
        channel_id=row["channel_id"],
        endpoint=row["endpoint"],
        industry=row["industry"],
        content_sha256=row["content_sha256"],
        byte_length=int(row["byte_length"]),
        fetched_at=parse_canonical_utc(row["fetched_at"]),
        http_status=None if row["http_status"] is None else int(row["http_status"]),
        score=score,
        fts_score=score,
        # 原始文本上的摘要：全文不出现 segment_cjk 插入的分隔符（判据 C5）。
        snippet=restore_snippet(
            row["snippet"], indexed_text=row["text_index"], original_text=row["text"]
        ),
    )


def _row_to_document(row: sqlite3.Row) -> IndexedDocument:
    return IndexedDocument(
        raw_id=row["raw_id"],
        channel_id=row["channel_id"],
        endpoint=row["endpoint"],
        industry=row["industry"],
        content_sha256=row["content_sha256"],
        byte_length=int(row["byte_length"]),
        fetched_at=parse_canonical_utc(row["fetched_at"]),
        http_status=None if row["http_status"] is None else int(row["http_status"]),
        text=row["text"],
        text_sha256=row["text_sha256"],
        text_length=int(row["text_length"]),
    )


def open_index(
    root: Optional[_PathLike] = None, *, db_path: Optional[_PathLike] = None
) -> SqliteSearchIndex:
    """便捷构造：`open_index(root)` → `<root>/atlas.db`（SPEC §2.10 的目录布局）。"""
    if root is not None and db_path is not None:
        raise ValueError("root 与 db_path 不能同时指定")
    if root is not None:
        db_path = Path(root) / "atlas.db"
    return SqliteSearchIndex(db_path)
