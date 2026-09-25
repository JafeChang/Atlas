"""T-103 不可变原文归档（SPEC §2.10）。

`Raw` 层的持久化实现：**原始字节落文件系统**（`BlobStore`），
**元数据落 `data/store/atlas.db` 的 `raw_records` 表**（`SqliteRawStore`），
两者由 `ArchiveStore` 组合成与 `atlas.contracts.RawStore` **同一契约**的实现。

- 幂等：同 `raw_id` + 同内容 → 返回已有记录，不报错
- 不可变：同 `raw_id` + 不同内容 → `ImmutabilityError`，绝不覆盖
- 只增不改：`raw_records` 由 SQL 触发器禁止 `UPDATE` / `DELETE`

本包只依赖 stdlib 与 `atlas.contracts`，不 import 其它域的持久化模块（SPEC §4.0）。
"""

from __future__ import annotations

from .blobstore import DEFAULT_RAW_DIR, BlobStore
from .sqlite_store import DEFAULT_DB_PATH, SCHEMA_VERSION, SqliteRawStore
from .store import (
    DEFAULT_STORE_ROOT,
    ArchiveStore,
    RawContentCorruptError,
    RawContentMissingError,
    open_archive,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_RAW_DIR",
    "DEFAULT_STORE_ROOT",
    "SCHEMA_VERSION",
    "ArchiveStore",
    "BlobStore",
    "RawContentCorruptError",
    "RawContentMissingError",
    "SqliteRawStore",
    "open_archive",
]
