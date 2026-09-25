"""T-004 行业/渠道配置外部契约（SPEC §2.9）。

前端可配置项的**唯一**权威定义。该包提供：

- `schema`      —— `Industry` / `Channel` / `FetchSpec` + 单对象与跨对象校验
- `versioning`  —— 配置版本记录（作者/时间/diff）与回滚
- `opml`        —— 渠道目录的 OPML 导入导出（stdlib，无新依赖）
- `persistence` —— `data/store/atlas.db` 上的 sqlite3 配置仓储（版本历史只增不改）
- `service`     —— 行业/渠道 CRUD、列表查询与变更审计
- `catalog`     —— 预置渠道目录：勾选 + 覆盖默认值（SPEC §2.9 选配体验）

闭环约束（SPEC §2.5）：行业配置即标签空间。本包**不硬编码任何行业枚举**，
行业一律由配置数据提供。
"""

from __future__ import annotations

from .catalog import (
    CatalogEntry,
    CatalogSelection,
    ChannelTemplate,
    IndustryTemplate,
    PrebuiltCatalog,
    default_catalog,
    instantiate_channel,
    instantiate_industry,
)
from .opml import (
    OPML_ATLAS_NAMESPACE_NOTE,
    channels_to_opml,
    import_opml,
    parse_opml,
    registry_to_opml,
)
from .persistence import DEFAULT_DB_PATH, SCHEMA_VERSION, SqliteConfigStore, open_store
from .schema import (
    BROWSER_MASQUERADE_USER_AGENTS,
    ID_PATTERN,
    KNOWN_FETCH_TYPES,
    MIN_INTERVAL_SECONDS,
    MIN_RATE_LIMIT_SECONDS,
    REQUIRED_SPEC_FIELDS,
    Channel,
    FetchSpec,
    FetchType,
    Industry,
    require_adapter_plugin,
    validate_endpoint,
    validate_id,
    validate_registry,
)
from .service import (
    AuditEntry,
    ChannelView,
    IndustryView,
    RegistryService,
    index_by_id,
)
from .versioning import (
    ChangeKind,
    ConfigSnapshot,
    ConfigStore,
    ConfigVersion,
    DiffEntry,
    RegistryMutation,
)

__all__ = [
    "AuditEntry",
    "BROWSER_MASQUERADE_USER_AGENTS",
    "CatalogEntry",
    "CatalogSelection",
    "ChangeKind",
    "Channel",
    "ChannelTemplate",
    "ChannelView",
    "ConfigSnapshot",
    "ConfigStore",
    "ConfigVersion",
    "DEFAULT_DB_PATH",
    "DiffEntry",
    "FetchSpec",
    "FetchType",
    "ID_PATTERN",
    "KNOWN_FETCH_TYPES",
    "MIN_INTERVAL_SECONDS",
    "MIN_RATE_LIMIT_SECONDS",
    "OPML_ATLAS_NAMESPACE_NOTE",
    "PrebuiltCatalog",
    "REQUIRED_SPEC_FIELDS",
    "SCHEMA_VERSION",
    "Industry",
    "IndustryTemplate",
    "IndustryView",
    "RegistryMutation",
    "RegistryService",
    "SqliteConfigStore",
    "channels_to_opml",
    "default_catalog",
    "import_opml",
    "index_by_id",
    "instantiate_channel",
    "instantiate_industry",
    "open_store",
    "parse_opml",
    "registry_to_opml",
    "require_adapter_plugin",
    "validate_endpoint",
    "validate_id",
    "validate_registry",
]
