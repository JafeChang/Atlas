"""T-205 检索排序（FTS）：SQLite FTS5 / BM25 + 排序 + 高亮（SPEC §2.10 / §4.3）。

交付范围
--------

对**已归档并归一化**的文档提供全文检索与排序，索引**可全量重建**：

| 模块 | 职责 |
|---|---|
| `query` | 关键词 → 安全 FTS5 表达式、筛选、排序、分页（**纯逻辑，零 I/O**） |
| `cjk` | 汉字逐字切分与逆切分（纯函数、**可逆**；T-205 修订） |
| `snippet` | 把 FTS5 摘要从切分后的文本还原到**原始文本**（禁止泄漏插入的分隔符） |
| `documents` | 索引输入单元：`DocumentText`（`RawRecord` + T-104 归一化文本） |
| `sqlite_index` | FTS5 索引的持久化：探测、重建、删除、查询（只写自己的三张表） |
| `source` | 只读来源适配：`raw`（T-103）+ `normalize`（T-104）→ `DocumentText` |
| `errors` | 错误层级（FTS5 不可用 / 查询非法 / 索引不可用 / 版本不符） |

三条硬性质
----------

1. **只读投影，不产生事实**：索引可以被随时删除并从 raw 全量重建；
   本包没有任何指向 `raw_records` / `confirmed_labels` / `proposed_claims` /
   `evidence_spans` 的写路径（`tests/test_search_invariants.py` 静态钉死）。
2. **零新依赖**：只用标准库（`sqlite3` / `hashlib` / `re` / `dataclasses` /
   `datetime` / `threading`）与已提交的 `atlas.contracts` / `atlas.archive` /
   `atlas.normalize`（后两者只读借用）。**不引入分块**（T-206），
   **不引入向量检索**（T-201）。
3. **响亮失败**：FTS5 不可用、查询切词后为空、索引未构建或版本不符、
   参数越界——一律抛显式异常，**不**降级成 `LIKE`、**不**静默返回全空、
   **不**静默截断。

汉字检索（T-205 修订）
----------------------

`unicode61` 不切分连续汉字，也不切"汉字紧贴拉丁"（`Transformer架构` 是一个 token），
因此索引列存 `segment_cjk(text)`（`中 文 分 词`、`Transformer 架 构`、
`数 据 库 abc`），对外正文仍是原始 `text`。查询侧把未切开的连续片段拼成 FTS5 **短语**
（`中文分词` → `"中 文 分 词"`，不是逐字 `AND`——后者会命中"人民工作智慧能力"），
摘要侧把插入的分隔符还原掉。索引版本因此升到 `atlas.search.index/3`、
schema 升到 `2`；**旧索引必须删除后重建**（读路径响亮失败并给出补救路径）。

最小用法::

    from atlas.archive import open_archive
    from atlas.search import ArchiveDocumentSource, SearchQuery, open_index

    archive = open_archive("data/store")
    index = open_index("data/store")            # 同一个 atlas.db（SPEC §2.10）
    index.rebuild(
        ArchiveDocumentSource(archive).iter_documents(),
        industry_of={c.id: c.industry_id for c in registry.list_channels()},   # §2.5 C8 闭环
    )
    result = index.search(SearchQuery(text="vector database", industries=("ai",), limit=20))
    for hit in result.items:
        print(hit.score, hit.raw_id, hit.snippet)

打分可组合（为 T-201 留的位置，**不是占位类**）::

    # boost 是第二个打分源：返回附加分，与 FTS 分数相加后再按同一套排序键重排。
    result = index.search(query, boost=lambda hit: 0.5 if hit.industry == "ai" else 0.0)
"""

from __future__ import annotations

from .cjk import (
    CJK_RANGES,
    SEGMENTATION_VERSION,
    desegment,
    is_cjk,
    is_inserted_space,
    segment_cjk,
)
from .documents import DocumentText, text_sha256
from .errors import (
    EmptyQueryError,
    Fts5UnavailableError,
    IndexVersionError,
    SearchError,
    SearchIndexError,
    SearchQueryError,
    SearchSourceError,
)
from .query import (
    DEFAULT_LIMIT,
    DEFAULT_SNIPPET_TOKENS,
    HIGHLIGHT_CLOSE,
    HIGHLIGHT_OPEN,
    MAX_FILTER_VALUES,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_QUERY_TERMS,
    MAX_SNIPPET_TOKENS,
    ORDER_RECENCY,
    ORDER_RELEVANCE,
    ORDERS,
    SNIPPET_ELLIPSIS,
    Boost,
    Group,
    SearchHit,
    SearchQuery,
    SearchResult,
    apply_boost,
    canonical_utc_iso,
    match_expression,
    parse_canonical_utc,
    phrase_groups,
    rank_hits,
    tokenize,
)
from .snippet import (
    SNIPPET_SENTINEL_CLOSE,
    SNIPPET_SENTINEL_ELLIPSIS,
    SNIPPET_SENTINEL_OPEN,
    restore_snippet,
)
from .source import ArchiveDocumentSource, DocumentSource, Normalizer
from .sqlite_index import (
    BUSY_TIMEOUT_SECONDS,
    DEFAULT_DB_PATH,
    DOCS_TABLE,
    FTS_TABLE,
    INDEX_VERSION,
    INDUSTRY_SOURCE_INJECTED,
    INDUSTRY_SOURCE_NONE,
    MAX_CANDIDATES,
    MAX_DOCUMENTS,
    META_TABLE,
    SCHEMA_VERSION,
    SEGMENTATION,
    TEXT_SOURCE,
    TOKENIZER,
    IndexReport,
    IndexedDocument,
    SqliteSearchIndex,
    drop_search_index,
    open_index,
    probe_fts5,
)

__all__ = [
    # cjk（T-205 修订）
    "CJK_RANGES",
    "SEGMENTATION_VERSION",
    "desegment",
    "is_cjk",
    "is_inserted_space",
    "segment_cjk",
    # documents
    "DocumentText",
    "text_sha256",
    # errors
    "EmptyQueryError",
    "Fts5UnavailableError",
    "IndexVersionError",
    "SearchError",
    "SearchIndexError",
    "SearchQueryError",
    "SearchSourceError",
    # snippet（T-205 修订）
    "SNIPPET_SENTINEL_CLOSE",
    "SNIPPET_SENTINEL_ELLIPSIS",
    "SNIPPET_SENTINEL_OPEN",
    "restore_snippet",
    # query
    "DEFAULT_LIMIT",
    "DEFAULT_SNIPPET_TOKENS",
    "HIGHLIGHT_CLOSE",
    "HIGHLIGHT_OPEN",
    "MAX_FILTER_VALUES",
    "MAX_LIMIT",
    "MAX_QUERY_LENGTH",
    "MAX_QUERY_TERMS",
    "MAX_SNIPPET_TOKENS",
    "ORDER_RECENCY",
    "ORDER_RELEVANCE",
    "ORDERS",
    "SNIPPET_ELLIPSIS",
    "Boost",
    "Group",
    "SearchHit",
    "SearchQuery",
    "SearchResult",
    "apply_boost",
    "canonical_utc_iso",
    "match_expression",
    "parse_canonical_utc",
    "phrase_groups",
    "rank_hits",
    "tokenize",
    # source
    "ArchiveDocumentSource",
    "DocumentSource",
    "Normalizer",
    # sqlite_index
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
    "TEXT_SOURCE",
    "TOKENIZER",
    "IndexReport",
    "IndexedDocument",
    "SqliteSearchIndex",
    "drop_search_index",
    "open_index",
    "probe_fts5",
]
