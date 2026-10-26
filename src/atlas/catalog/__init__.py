"""T-111 目录聚合与保鲜（SPEC §2.7 / §2.8 / §2.9 / §2.10）。

两个能力，一个包：

- **聚合**（`aggregate`）：把**外部现成清单**（OPML 文本或 URL）解析、归一化成候选
  渠道，按 `id` + 规范化 `endpoint` 双键去重后合并进注册表。目录是
  **"聚合 + 验证 + 分类"**出来的，不是从零想全的（SPEC §2.7）。
- **保鲜**（`health` + `store`）：对已有渠道做定期健康探测，把可区分的结果写进
  只增不改的 `catalog_health` 表，并把"不健康"作为标签标记写回注册表。
  信息源会失效，目录**没有"完成"状态**（SPEC §2.8），所以这是常驻能力。

三条贯穿性的约束：

1. **没有任何旁路**：OPML 解析用 `atlas.registry.opml.parse_opml`，候选渠道经
   `atlas.registry.schema` 的同一套校验构造，写入只经
   `atlas.registry.service.RegistryService` 的提交入口。
2. **只标注不删除**：冲突保留已有记录并报告；失效渠道**只标记不删除**，
   恢复后能重新标记为健康（SPEC §2.4 精神）。
3. **不绕过合规层**：抓清单与做健康探测都复用 `atlas.collect` 的 robots 检查与
   同域限速，且这两个依赖都是**必填参数**。
"""

from __future__ import annotations

from .aggregate import (
    DEFAULT_LISTING_USER_AGENT,
    AggregationError,
    AggregationOutcome,
    AggregationResult,
    CandidateConflict,
    CandidateOrigin,
    CatalogError,
    ChannelCandidate,
    DuplicateKind,
    ListingDocument,
    ListingFetchFailed,
    ListingRobotsDenied,
    MergeConflict,
    MergeConflictKind,
    MergePlan,
    RejectedCandidate,
    aggregate_listing,
    aggregate_registry,
    aggregate_registry_from_url,
    apply_merge,
    fetch_listing,
    normalize_endpoint,
    plan_merge,
)
from .health import (
    HEALTHY_STATUSES,
    UNHEALTHY_STATUSES,
    ChannelHealth,
    HealthProbe,
    HealthReport,
    HealthStatus,
    probe_channel,
    probe_registry,
)
from .store import (
    DEFAULT_DB_PATH,
    DEFAULT_UNHEALTHY_TAG,
    CatalogHealthStore,
    open_health_store,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_LISTING_USER_AGENT",
    "DEFAULT_UNHEALTHY_TAG",
    "HEALTHY_STATUSES",
    "UNHEALTHY_STATUSES",
    "AggregationError",
    "AggregationOutcome",
    "AggregationResult",
    "CandidateConflict",
    "CandidateOrigin",
    "CatalogError",
    "CatalogHealthStore",
    "ChannelCandidate",
    "ChannelHealth",
    "DuplicateKind",
    "HealthProbe",
    "HealthReport",
    "HealthStatus",
    "ListingDocument",
    "ListingFetchFailed",
    "ListingRobotsDenied",
    "MergeConflict",
    "MergeConflictKind",
    "MergePlan",
    "RejectedCandidate",
    "aggregate_listing",
    "aggregate_registry",
    "aggregate_registry_from_url",
    "apply_merge",
    "fetch_listing",
    "normalize_endpoint",
    "open_health_store",
    "plan_merge",
    "probe_channel",
    "probe_registry",
]
