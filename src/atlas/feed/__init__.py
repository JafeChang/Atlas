"""T-106 Feed 查询 API（SPEC §1.5 / §2.11 / §4.2）。

交付形态是**可筛选、可排序的 feed 流**（明确不做定期报告）。本包提供：

| 模块 | 职责 |
|---|---|
| `query` | 筛选 / 稳定排序 / 分页（**纯逻辑**，零 I/O、不碰 HTTP） |
| `repository` | 数据来源适配：`FeedSource` Protocol + `atlas.archive` 只读适配 |
| `http` | stdlib `ThreadingHTTPServer` + `json` 的只读 HTTP 服务 + 对外 JSON 契约 |

三条硬性质
----------

1. **只读投影，不产生事实**：没有写 raw / 写标签 / 写 Proposed 的代码路径。
   打标是 T-108 的 Confirmed 契约，与查询通道分离（SPEC §2.11）。
2. **零新依赖**：只用标准库（`http.server` / `json` / `dataclasses` / `datetime`）
   与已提交的 `atlas.contracts` / `atlas.archive`。
3. **解耦**：`industry_of` 与 `label_lookup` 都是**注入**的；本包**不 import**
   `atlas.labels`（T-108 在建）与 `atlas.registry` 的实现（SPEC §4.0）。

最小用法::

    from atlas.archive import open_archive
    from atlas.feed import ArchiveFeedSource, FeedQuery, FeedServer, run_query

    source = ArchiveFeedSource(open_archive("data/store"), industry_of={"chan-1": "ai"})
    with FeedServer(source) as server:          # 127.0.0.1 上随机端口
        print(server.base_url + "/feed?industry=ai&limit=20")

    # 纯逻辑（无 HTTP）
    result = run_query(source, FeedQuery(industries=("ai",)))
"""

from __future__ import annotations

from .http import (
    CONTRACT_VERSION,
    ERROR_CODES,
    FeedRequestHandler,
    FeedServer,
    error_payload,
    feed_payload,
    start_feed_server,
)
from .query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FeedItem,
    FeedQuery,
    FeedQueryError,
    FeedResult,
    InvalidQueryError,
    LabelLookup,
    SourceContractError,
    run_query,
)
from .repository import (
    EMPTY_LABEL_LOOKUP,
    ArchiveFeedSource,
    FeedSource,
    StaticFeedSource,
    archive_source_factory,
    labels_from_mapping,
)

__all__ = [
    # http
    "CONTRACT_VERSION",
    "ERROR_CODES",
    "FeedRequestHandler",
    "FeedServer",
    "error_payload",
    "feed_payload",
    "start_feed_server",
    # query
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "FeedItem",
    "FeedQuery",
    "FeedQueryError",
    "FeedResult",
    "InvalidQueryError",
    "LabelLookup",
    "SourceContractError",
    "run_query",
    # repository
    "EMPTY_LABEL_LOOKUP",
    "ArchiveFeedSource",
    "FeedSource",
    "StaticFeedSource",
    "archive_source_factory",
    "labels_from_mapping",
]
