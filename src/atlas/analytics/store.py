"""Append frontend interactions to a separate SQLite database."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TYPES = {"page_view", "list_served", "list_impression", "detail_view", "read_progress", "click"}
_PAGES = {"/", "/feed", "/article", "/entry"}
_TEXT_LIMITS = {
    "event_id": 80, "visitor_id": 80, "session_id": 80, "view_id": 80,
    "event_type": 32, "page": 16, "occurred_at": 40, "raw_id": 256,
    "entry_id": 256, "raw_sha256": 64, "channel_id": 256, "action": 80,
    "order": 16, "strategy": 80, "collection_version": 32, "parent_view_id": 80,
}
_INTS = {"char_start", "char_end", "position", "offset", "limit", "total", "visible_ms", "read_ms", "scroll_pct", "filter_mask"}
_REQUIRED = {"event_id", "visitor_id", "session_id", "view_id", "event_type", "page", "occurred_at"}


def validate_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) - (_TEXT_LIMITS.keys() | _INTS):
        raise ValueError("invalid event fields")
    if not _REQUIRED <= event.keys():
        raise ValueError("missing event fields")
    for key, value in event.items():
        if key in _TEXT_LIMITS:
            if not isinstance(value, str) or not value or len(value) > _TEXT_LIMITS[key]:
                raise ValueError("invalid text field: " + key)
        elif type(value) is not int or not 0 <= value <= 10**9:
            raise ValueError("invalid integer field: " + key)
    if event["event_type"] not in _TYPES or event["page"] not in _PAGES:
        raise ValueError("invalid event type or page")
    if event["event_type"] in {"list_served", "list_impression", "detail_view", "read_progress"} and "raw_id" not in event:
        raise ValueError("missing item identity")
    if event.get("scroll_pct", 0) > 100:
        raise ValueError("invalid scroll percentage")
    if event["event_type"] == "read_progress" and not {"read_ms", "scroll_pct"} <= event.keys():
        raise ValueError("missing reading progress")
    if event.get("collection_version") == "v2":
        if event["event_type"] == "list_impression" and event.get("visible_ms", 0) < 1000:
            raise ValueError("exposure is too short")
        if event["event_type"] == "read_progress" and event.get("read_ms", 0) < 1000:
            raise ValueError("reading is too short")
        if event["event_type"] == "list_served" and not {"strategy", "position"} <= event.keys():
            raise ValueError("missing feed context")
    if event["event_type"] == "click" and "action" not in event:
        raise ValueError("missing action")
    try:
        timestamp = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("timestamp lacks timezone")
    except ValueError as exc:
        raise ValueError("invalid occurred_at") from exc
    return event


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append_event(self, event: dict[str, Any], *, user_id: str | None = None) -> None:
        event = validate_event(event)
        if user_id is not None and (not isinstance(user_id, str) or not user_id or len(user_id) > 80):
            raise ValueError("invalid user id")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS interaction_events (
                event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                occurred_at TEXT NOT NULL, visitor_id TEXT NOT NULL,
                session_id TEXT NOT NULL, view_id TEXT NOT NULL,
                event_type TEXT NOT NULL, page TEXT NOT NULL,
                raw_id TEXT, entry_id TEXT, raw_sha256 TEXT, char_start INTEGER,
                char_end INTEGER, channel_id TEXT, position INTEGER,
                action TEXT, offset INTEGER, limit_count INTEGER, sort_order TEXT,
                payload TEXT NOT NULL, user_id TEXT, strategy TEXT, collection_version TEXT,
                total INTEGER, visible_ms INTEGER, read_ms INTEGER, scroll_pct INTEGER, parent_view_id TEXT, filter_mask INTEGER
            )""")
            existing = {column[1] for column in db.execute("PRAGMA table_info(interaction_events)")}
            for column, column_type in (("user_id", "TEXT"), ("strategy", "TEXT"),
                                        ("collection_version", "TEXT"), ("total", "INTEGER"),
                                        ("visible_ms", "INTEGER"), ("read_ms", "INTEGER"),
                                        ("scroll_pct", "INTEGER"), ("parent_view_id", "TEXT"), ("filter_mask", "INTEGER")):
                if column not in existing:
                    db.execute(f"ALTER TABLE interaction_events ADD COLUMN {column} {column_type}")
            db.execute("CREATE INDEX IF NOT EXISTS interaction_events_user ON interaction_events(user_id, occurred_at)")
            db.execute("CREATE INDEX IF NOT EXISTS interaction_events_item ON interaction_events(raw_id, char_start, char_end)")
            db.execute("CREATE INDEX IF NOT EXISTS interaction_events_session ON interaction_events(session_id, occurred_at)")
            db.execute("""INSERT INTO interaction_events (
                event_id, received_at, occurred_at, visitor_id, session_id, view_id,
                event_type, page, raw_id, entry_id, raw_sha256, char_start, char_end,
                channel_id, position, action, offset, limit_count, sort_order, payload, user_id,
                strategy, collection_version, total, visible_ms, read_ms, scroll_pct, parent_view_id, filter_mask
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING""", (
                event["event_id"], datetime.now(UTC).isoformat(), event["occurred_at"],
                event["visitor_id"], event["session_id"], event["view_id"], event["event_type"],
                event["page"], event.get("raw_id"), event.get("entry_id"), event.get("raw_sha256"),
                event.get("char_start"), event.get("char_end"), event.get("channel_id"),
                event.get("position"), event.get("action"), event.get("offset"),
                event.get("limit"), event.get("order"), json.dumps(event, ensure_ascii=False), user_id,
                event.get("strategy"), event.get("collection_version"), event.get("total"),
                event.get("visible_ms"), event.get("read_ms"), event.get("scroll_pct"), event.get("parent_view_id"), event.get("filter_mask"),
            ))
