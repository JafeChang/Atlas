"""T-205 检索排序（FTS）的错误层级。

判据 A1 / A5 / A6 的"响亮失败"落点：FTS5 不可用、查询非法、索引缺失或版本不符，
一律用这里的显式异常表达，**不得**降级为静默空结果、`LIKE` 替身或 `sqlite3` 原始异常。

继承关系（有意设计）：

- `SearchQueryError` 同时继承 `ValueError`：迁就 `atlas.feed.query.InvalidQueryError`
  的既有约定，HTTP 层"`ValueError` → 400"的写法可以直接复用；
- `IndexVersionError` 同时继承 `atlas.contracts.VersionError`：与 T-103
  `SqliteRawStore` 对物理 schema 版本的处理保持同一个可捕获类型。
"""

from __future__ import annotations

from atlas.contracts import VersionError

__all__ = [
    "EmptyQueryError",
    "Fts5UnavailableError",
    "IndexVersionError",
    "SearchError",
    "SearchIndexError",
    "SearchQueryError",
    "SearchSourceError",
]


class SearchError(Exception):
    """检索层的基类异常。"""


class SearchQueryError(SearchError, ValueError):
    """查询参数非法：**拒绝**而不是截断、忽略或猜一个默认值。"""


class EmptyQueryError(SearchQueryError):
    """查询文本切词后为空（空串 / 纯空白 / 纯操作符）。

    明确报错，而不是"静默返回全空"——后者会让调用方分不清
    "没有命中"与"查询根本没被理解"（判据 A5）。
    """


class Fts5UnavailableError(SearchError):
    """当前 SQLite 未编译 FTS5（或本项目使用的分词器配置不被接受）。

    索引层启动时探测到即抛出，**不得**降级成 `LIKE` 假装是全文检索（判据 A1）。
    """


class SearchIndexError(SearchError):
    """索引不可用：未构建、被外部破坏、或输入不满足索引契约。"""


class IndexVersionError(SearchIndexError, VersionError):
    """索引的 `schema_version` / `index_version` 与本代码不一致。

    索引是**可重建**的派生物，所以补救路径是"删掉再重建"，而不是静默兼容
    （与 T-103 的 `raw_records` schema 版本处理同一原则）。
    """


class SearchSourceError(SearchError):
    """数据来源违约：归档被破坏、或来源给出的文本与声明不符。"""
