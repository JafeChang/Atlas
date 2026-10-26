"""人工打标存储的组合入口（T-108）：打开 / 查询 / 导出。

`SqliteConfirmedStore` 只做一件事——把 `atlas.contracts.ConfirmedStore` 的语义
落到 `confirmed_labels` 表。本模块把它与 `export` 组合成**应用层唯一入口**：
调用方（打标前端 T-109、CLI、将来的服务层）只依赖 `LabelStore`，不直接摸
`sqlite3`，也不直接拼导出文件。

四条契约方法（`add` / `all_for` / `latest_value` / `count`）**逐字转发**给 SQLite 实现，
签名与语义与 `atlas.contracts.ConfirmedStore` 完全一致（可多，不可少、不可改名）。

```python
with open_store(tmp_db) as store:
    store.add(ConfirmedLabel.human(raw_id="raw_x", label_key="industry",
                                   label_value="cs.LG", actor="me"))
    store.latest_value("raw_x", "industry")     # "cs.LG"
    store.to_json(backup_dir / "labels.json")   # B7：人工产物必须可流转
```
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from atlas.contracts import ConfirmedLabel

from .export import (
    backup as _backup,
    dumps_json as _dumps_json,
    export_csv as _export_csv,
    export_json as _export_json,
    import_csv as _import_csv,
    import_json as _import_json,
    restore as _restore,
)
from .sqlite_store import DEFAULT_DB_PATH, SqliteConfirmedStore

__all__ = ["DEFAULT_DB_PATH", "LabelStore", "open_store"]


class LabelStore:
    """人工标签库的应用层入口：SQLite 持久化 + 导出/回流。

    Args:
        db_path: 库文件路径；`None` → SPEC §2.10 默认位置 `data/store/atlas.db`。
            **测试必须传 `tmp_path`**，不得写仓库 `data/`。
    """

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        self._labels = SqliteConfirmedStore(db_path=db_path)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._labels.db_path

    @property
    def labels(self) -> SqliteConfirmedStore:
        """底层持久化层（导出与审计需要全表读取时用）。"""
        return self._labels

    def close(self) -> None:
        self._labels.close()

    def __enter__(self) -> "LabelStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"LabelStore(db_path={str(self.db_path)!r})"

    # ------------------------------------------------------------------
    # 契约方法（= atlas.contracts.ConfirmedStore）
    # ------------------------------------------------------------------
    def add(self, label: ConfirmedLabel) -> ConfirmedLabel:
        """写入一条人工标签。幂等：同 `label_id` 重复 `add` 返回库里那条。"""
        return self._labels.add(label)

    def all_for(self, raw_id: str) -> List[ConfirmedLabel]:
        """该文档上的全部标签（写入顺序）。"""
        return self._labels.all_for(raw_id)

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        """该文档该维度的最新取值；无记录返回 `None`。"""
        return self._labels.latest_value(raw_id, label_key)

    def count(self) -> int:
        """标签总条数（不是文档数）。"""
        return self._labels.count()

    # ------------------------------------------------------------------
    # 查询扩展
    # ------------------------------------------------------------------
    def all_labels(self) -> List[ConfirmedLabel]:
        return self._labels.all_labels()

    def get(self, label_id: str) -> Optional[ConfirmedLabel]:
        return self._labels.get(label_id)

    def has(self, label_id: str) -> bool:
        return self._labels.has(label_id)

    def raw_ids(self) -> List[str]:
        return self._labels.raw_ids()

    def keys_for(self, raw_id: str) -> List[str]:
        return self._labels.keys_for(raw_id)

    # ------------------------------------------------------------------
    # 导出 / 备份 / 回流（B7：人工产物不可重建，必须可流转）
    # ------------------------------------------------------------------
    def to_json(self, path: str | Path, *, exported_at: Optional[datetime] = None) -> Path:
        """把**库里的全部标签**导出为 JSON。"""
        return _export_json(self._labels, path, exported_at=exported_at)

    def to_csv(self, path: str | Path) -> Path:
        """把**库里的全部标签**导出为 CSV。"""
        return _export_csv(self._labels, path)

    def dumps_json(self, *, exported_at: Optional[datetime] = None) -> str:
        """导出为 JSON 文本（不落盘）。"""
        return _dumps_json(self._labels, exported_at=exported_at)

    def backup(
        self,
        directory: str | Path,
        *,
        stem: str = "confirmed_labels",
        now: Optional[datetime] = None,
    ) -> Path:
        """按 UTC 时间戳备份到 `directory`，返回备份文件路径。"""
        return _backup(self._labels, directory, stem=stem, now=now)

    def import_from_json(self, path: str | Path) -> int:
        """从 JSON 导出文件回流（幂等）；返回处理的记录条数。"""
        return _import_json(self._labels, path)

    def import_from_csv(self, path: str | Path) -> int:
        """从 CSV 导出文件回流（幂等）；返回处理的记录条数。"""
        return _import_csv(self._labels, path)

    def restore(self, path: str | Path) -> int:
        """从备份文件回流（按扩展名分派）；返回处理的记录条数。"""
        return _restore(self._labels, path)


def open_store(db_path: str | Path | None = None) -> LabelStore:
    """便捷入口：打开（必要时创建）人工标签库。"""
    return LabelStore(db_path=db_path)
