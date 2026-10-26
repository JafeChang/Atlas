"""组合根读取已登记的订阅端点；不创建数据库、表或配置版本。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def read_subscription_sources(db_path: str | Path) -> frozenset[tuple[str, str]]:
    path = Path(db_path)
    if not path.is_file():
        return frozenset()
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='channels'"
        ).fetchone():
            return frozenset()
        return frozenset(
            db.execute(
                "SELECT channel_id,endpoint FROM channels WHERE type IN ('rss','atom','json_api')"
            )
        )
