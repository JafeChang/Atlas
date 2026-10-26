"""运行入口显式启用 SQLite 并发读写；只读页面不修改数据库配置。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path


def enable_wal(db_path: str | Path) -> None:
    """在已有本地库开启持久 WAL，防止大正文入库阻塞网页读取。"""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError(f"无法启用数据库并发读写模式：{mode}")
