"""T-131 旧语料导入（SPEC §4.2 T-131 / §5 登记 #12 / §6.2）。

把旧系统 `data/raw/<频道>/*.json` 里的**逐篇文章**导入新结构的不可变归档
`data/store/raw/`（SPEC §2.10 布局），当作**新 Raw** 写入。

为什么必须**按篇**导入（§6.3 裁决 B 的事实前提）
------------------------------------------------

旧系统是**按篇**存的：`data/raw/<频道>/<uuid>.json` 的一份 JSON 就是**一篇文章**
（`raw_content` 是正文，`source_url` 是文章地址）。新结构此前把**整个 feed** 当成一篇
文档归档（§6.3：10 条归档记录里含 830 篇文章，一篇都不是独立文档）。因此导入时：

- `channel_id` = 映射后的注册表渠道 id（保行业归属）
- `endpoint`    = 旧记录自己的 **`source_url`（文章地址）**
- `content`     = 旧记录的 **`raw_content` 的 UTF-8 字节**

于是 `raw_id = raw_id_for(channel_id, endpoint, content_sha256)` 是**逐篇**的，
而不是逐 feed 的 —— 这正是本任务的价值。

⚠️ **`endpoint` 的语义差异（需登记进 SPEC）**：新采集的记录里 `endpoint` 是
**feed 地址**（`https://syncedreview.com/feed`，见 T-102/T-120 的落盘），
而本包导入的记录里 `endpoint` 是**文章地址**。两者共用同一个列，
`raw_id` 的构造式不变，因此**读 `endpoint` 的代码不能假设它是 feed 地址**。

四条不变量与两条禁令
--------------------

- Raw **只增不改**：全部写入走 T-103 的 `ArchiveStore.put`，同 `raw_id` 同内容幂等、
  不同内容 `ImmutabilityError`；`raw_records` 的 append-only 触发器不绕过、不关闭。
- 归一化 / Proposed / Confirmed 本包**一个字节都不写**。特别是 **§2.4：不得迁移任何
  人工标签**——标签锚在 `raw_id` 上，而导入会**改变条目身份**（旧 uuid → 新内容寻址
  `raw_id`），迁移标签等于把打标数据锚到错误的对象上。本包没有任何写标签的代码路径
  （`tests/test_migrate_no_label_migration.py` 用 sqlite `authorizer` 把这条钉死）。
- 失败的记录**逐一记录原因**进对账表，绝不静默跳过；`strict=True` 时直接响亮失败。

零新增依赖：只用标准库与已装的 `pydantic`（经 `atlas.contracts`）。

包布局
------

| 模块 | 职责 |
|---|---|
| `errors` | 异常与失败原因常量 |
| `source` | 只读扫描 `data/raw/`（`LegacyDocument`） |
| `moment` | 旧时间字段 → aware UTC（**不编造时间**） |
| `mapping` | 旧频道名 → 注册表 `channel_id`（注入式，本包不 import `atlas.registry`） |
| `report` | 对账表数据结构（`MigrateReport` + `ledger_problems()`） |
| `migrate` | 逐篇导入主流程（`Migrate` / `migrate_legacy`） |
"""

from __future__ import annotations

from .errors import (
    REASON_CHANNEL_MAPPING_CONFLICT,
    REASON_CHANNEL_NOT_IN_REGISTRY,
    REASON_CHANNEL_NOT_MAPPED,
    REASON_FINGERPRINT_MISMATCH,
    REASON_MISSING_TIMESTAMP,
    REASON_NESTED_LAYOUT,
    REASON_NOT_AN_OBJECT,
    REASON_NO_CHANNEL_DIR,
    REASON_UNPARSEABLE_JSON,
    REASON_UNPARSEABLE_TIMESTAMP,
    REASON_UNREADABLE_BYTES,
    SKIP_EMPTY_CONTENT,
    SKIP_NON_DOCUMENT,
    ChannelMappingError,
    LegacyScanError,
    MigrateError,
    TimestampError,
)
from .mapping import (
    MAPPER_AMBIGUOUS,
    MAPPER_NOT_FOUND,
    ChannelMapResolver,
    ChannelMapper,
    build_channel_map,
    channel_map_resolver,
    empty_mapper,
    known_channels,
    normalize_channel_map,
    resolve_nothing,
    unmapped_names,
)
from .migrate import (
    DEFAULT_ARCHIVE_ROOT,
    DEFAULT_LEGACY_ROOT,
    LEGACY_HTTP_STATUS,
    REPO_ROOT,
    Migrate,
    MigrateOptions,
    load_channel_map_file,
    migrate_legacy,
)
from .moment import MOMENT_FIELDS, parse_legacy_moment, pick_moment_field
from .report import (
    STATUS_DEDUPLICATED,
    STATUS_FAILED,
    STATUS_IMPORTED,
    STATUS_SKIPPED,
    STATUSES,
    FailureReason,
    MigrateEntry,
    MigrateReport,
    PerChannelSummary,
    SkipReason,
)
from .source import (
    CONTENT_FIELD,
    DOCUMENT_TYPE_FIELD,
    URL_FIELD,
    LegacyDocument,
    LegacyFile,
    LegacyJson,
    classify_legacy,
    iter_legacy_files,
    nested_relative_paths,
    scan_legacy_files,
    to_document,
)

__all__ = [
    "CONTENT_FIELD",
    "DEFAULT_ARCHIVE_ROOT",
    "DEFAULT_LEGACY_ROOT",
    "DOCUMENT_TYPE_FIELD",
    "LEGACY_HTTP_STATUS",
    "MAPPER_AMBIGUOUS",
    "MAPPER_NOT_FOUND",
    "MOMENT_FIELDS",
    "REASON_CHANNEL_MAPPING_CONFLICT",
    "REASON_CHANNEL_NOT_IN_REGISTRY",
    "REASON_CHANNEL_NOT_MAPPED",
    "REASON_FINGERPRINT_MISMATCH",
    "REASON_MISSING_TIMESTAMP",
    "REASON_NESTED_LAYOUT",
    "REASON_NOT_AN_OBJECT",
    "REASON_NO_CHANNEL_DIR",
    "REASON_UNPARSEABLE_JSON",
    "REASON_UNPARSEABLE_TIMESTAMP",
    "REASON_UNREADABLE_BYTES",
    "REPO_ROOT",
    "SKIP_EMPTY_CONTENT",
    "SKIP_NON_DOCUMENT",
    "STATUSES",
    "STATUS_DEDUPLICATED",
    "STATUS_FAILED",
    "STATUS_IMPORTED",
    "STATUS_SKIPPED",
    "URL_FIELD",
    "ChannelMapResolver",
    "ChannelMapper",
    "ChannelMappingError",
    "FailureReason",
    "LegacyDocument",
    "LegacyFile",
    "LegacyJson",
    "LegacyScanError",
    "Migrate",
    "MigrateEntry",
    "MigrateError",
    "MigrateOptions",
    "MigrateReport",
    "PerChannelSummary",
    "SkipReason",
    "TimestampError",
    "build_channel_map",
    "channel_map_resolver",
    "classify_legacy",
    "empty_mapper",
    "iter_legacy_files",
    "known_channels",
    "load_channel_map_file",
    "migrate_legacy",
    "nested_relative_paths",
    "normalize_channel_map",
    "parse_legacy_moment",
    "pick_moment_field",
    "resolve_nothing",
    "scan_legacy_files",
    "to_document",
    "unmapped_names",
]
