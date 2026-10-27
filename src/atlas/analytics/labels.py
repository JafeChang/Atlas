"""Read-only projection of confirmed labels onto stable user IDs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def read_user_labels(labels_db: str | Path, users_db: str | Path) -> list[dict[str, Any]]:
    """Expose existing labels with user_id; unmatched historical actors stay unassigned.

    Reads both databases without creating or migrating either one. Confirmed labels retain
    their original append-only ordering and anchors; user names do not enter the output.
    """
    labels_path, users_path = Path(labels_db), Path(users_db)
    if not labels_path.exists():
        return []
    user_ids = {}
    if users_path.exists():
        with closing(_read_only(users_path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "user_id" in columns:
                user_ids = dict(db.execute("SELECT name, user_id FROM users"))
    with closing(_read_only(labels_path)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='confirmed_labels'").fetchone()
        if not exists:
            return []
        rows = db.execute("""SELECT label_id, raw_id, label_key, label_value, actor,
            anchor_raw_sha256, anchor_char_start, anchor_char_end, created_at
            FROM confirmed_labels ORDER BY rowid""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["user_id"] = user_ids.get(item.pop("actor"))
        result.append(item)
    return result
