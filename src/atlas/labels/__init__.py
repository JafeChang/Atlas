"""`atlas.labels` —— 人工打标存储（Confirmed 层，T-108）。

SPEC §2.10 的表归属：本包持有 `data/store/atlas.db` 的 **`confirmed_labels`** 表，
不碰其它域的表（`raw_records` 归 T-103，`proposed_claims` 归 T-105，
`evidence_spans` 归 T-107，`store_meta` / `config_versions` / … 归 T-101）。
跨包只依赖 `atlas.contracts`（SPEC §4.0 的包布局约定），不 import 任何其它域的实现。

```python
from atlas.contracts import ConfirmedLabel
from atlas.labels import open_store

with open_store("data/store/atlas.db") as store:
    store.add(ConfirmedLabel.human(raw_id="raw_x", label_key="industry",
                                   label_value="cs.LG", actor="me"))
    store.latest_value("raw_x", "industry")   # "cs.LG"
```

对外契约与 `atlas.contracts.ConfirmedStore` 一致（可多，不可少）：
`add` / `all_for` / `latest_value` / `count`；另有导出/备份/回流：
`atlas.labels.export`。
"""

from .export import (
    CSV_COLUMNS,
    EXPORT_FORMAT,
    EXPORT_FORMAT_VERSION,
    backup,
    dumps_json,
    export_csv,
    export_json,
    import_csv,
    import_json,
    label_from_dict,
    label_to_dict,
    load_csv,
    load_json,
    restore,
    verify_label_id,
)
from .sqlite_store import (
    DEFAULT_DB_PATH,
    SCHEMA_VERSION,
    TABLE_NAME,
    SqliteConfirmedStore,
    open_store as open_sqlite_store,
)
from .store import LabelStore, open_store

__all__ = [
    # 存储
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "TABLE_NAME",
    "LabelStore",
    "SqliteConfirmedStore",
    "open_sqlite_store",
    "open_store",
    # 导出 / 备份
    "CSV_COLUMNS",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "backup",
    "dumps_json",
    "export_csv",
    "export_json",
    "import_csv",
    "import_json",
    "label_from_dict",
    "label_to_dict",
    "load_csv",
    "load_json",
    "restore",
    "verify_label_id",
]
