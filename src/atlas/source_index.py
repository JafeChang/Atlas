"""Rebuildable article/source index. Parse each immutable Raw once, outside HTTP.

Bodies are stored once per source key and loaded only for selected work. Raw
locators remain distinct: this cache never merges identities or moves labels.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from atlas.archive import open_archive
from atlas.cognition.briefs import BRIEF_VERSION, BriefSource
from atlas.entries.ids import ENTRY_PARSER_VERSION
from atlas.registry import read_subscription_sources

VERSION = f"sources-v1:{ENTRY_PARSER_VERSION}:{BRIEF_VERSION}"
FILENAME = "entry-index.sqlite"


def readonly(path):
    return sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30
    )


class SourceIndex:
    def __init__(self, root):
        self.root = Path(root)
        self.db = sqlite3.connect(self.root / FILENAME, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        # This derived cache can be rebuilt after power loss. WAL still commits
        # every Raw atomically; do not fsync the disk twice per cached response.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS indexed_raws (
                raw_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                subscription INTEGER NOT NULL, browse TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                source_key TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                kind TEXT NOT NULL, url TEXT NOT NULL, last_seen REAL NOT NULL,
                recency REAL NOT NULL, paper_channel TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raw_sources (
                raw_id TEXT NOT NULL, char_start INTEGER NOT NULL,
                char_end INTEGER NOT NULL, source_key TEXT NOT NULL,
                PRIMARY KEY(raw_id, char_start, char_end)
            );
            CREATE INDEX IF NOT EXISTS raw_sources_key ON raw_sources(source_key);
            CREATE INDEX IF NOT EXISTS sources_recent ON sources(last_seen DESC, source_key);
        """)

    def close(self):
        self.db.close()

    def index_raw(self, archive, access, record):
        subscription = (
            record.channel_id,
            record.endpoint,
        ) in access._subscription_sources
        previous = self.db.execute(
            "SELECT version,subscription FROM indexed_raws WHERE raw_id=?",
            (record.raw_id,),
        ).fetchone()
        if previous == (VERSION, int(subscription)):
            return False
        view = access.entries_of(record)
        sources = access.brief_sources(record.raw_id)
        fields = (
            "entry_id",
            "raw_id",
            "raw_sha256",
            "index",
            "char_start",
            "char_end",
            "title",
            "link",
            "published_at",
        )
        payload = {
            "entries": [
                {name: getattr(entry, name) for name in fields}
                for entry in view.entries
            ],
            "text_length": view.text_length,
            "is_feed": view.is_feed,
            "problems": view.problems,
            "unavailable_reason": view.unavailable_reason,
            "sources": [
                [start, end, source.key, source.title, source.kind, source.url]
                for (start, end), source in sources.items()
            ],
        }
        by_span = {(e.char_start, e.char_end): e for e in view.entries}
        with self.db:
            self.db.execute("DELETE FROM raw_sources WHERE raw_id=?", (record.raw_id,))
            for (start, end), source in sources.items():
                recency = record.fetched_at.timestamp()
                published = getattr(by_span.get((start, end)), "published_at", None)
                if published:
                    try:
                        stamp = datetime.fromisoformat(published)
                        recency = (
                            stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
                        ).timestamp()
                    except ValueError:
                        pass  # Invalid publisher dates retain the recorded fetch time.
                self.db.execute(
                    """
                    INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        last_seen=max(last_seen,excluded.last_seen),
                        recency=max(recency,excluded.recency),
                        paper_channel=CASE WHEN paper_channel='' THEN excluded.paper_channel ELSE paper_channel END
                    WHERE excluded.last_seen>last_seen OR excluded.recency>recency
                        OR (paper_channel='' AND excluded.paper_channel!='')
                    """,
                    (
                        source.key,
                        source.title,
                        source.body,
                        source.kind,
                        source.url,
                        record.fetched_at.timestamp(),
                        recency,
                        record.channel_id
                        if record.channel_id.startswith("arxiv-")
                        else "",
                    ),
                )
                self.db.execute(
                    "INSERT INTO raw_sources VALUES (?,?,?,?)",
                    (record.raw_id, start, end, source.key),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO indexed_raws VALUES (?,?,?,?)",
                (
                    record.raw_id,
                    VERSION,
                    int(subscription),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        return True


def refresh_sources(root):
    """Catch up after collection or a crash; a completed Raw is never reparsed."""
    from atlas.webapp import ArchiveEntriesAccess

    root = Path(root)
    with (root / ".entry-index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open_archive(root) as archive, closing(SourceIndex(root)) as index:
            access = ArchiveEntriesAccess(archive, cache_size=1)
            known = {
                r[0]: (r[1], bool(r[2]))
                for r in index.db.execute(
                    "SELECT raw_id,version,subscription FROM indexed_raws"
                )
            }
            subscriptions = read_subscription_sources(archive.db_path)
            changed = 0
            # Metadata is small; no historical response/body is read on an idle run.
            for raw_id in archive.all_raw_ids():
                record = archive.get(raw_id)
                signature = (
                    VERSION,
                    (record.channel_id, record.endpoint) in subscriptions,
                )
                if known.get(raw_id) != signature:
                    changed += index.index_raw(archive, access, record)
            return changed


def read_sources(root, *, keys=None, include_body=False):
    """Return source metadata, or bodies for a bounded batch of known keys."""
    with closing(readonly(Path(root) / FILENAME)) as db:
        columns = "source_key,title," + ("body" if include_body else "''") + ",kind,url"
        base = f"SELECT {columns} FROM sources WHERE EXISTS (SELECT 1 FROM raw_sources r WHERE r.source_key=sources.source_key)"
        if keys is None:
            return {
                row[0]: BriefSource(*row)
                for row in db.execute(base + " ORDER BY last_seen DESC,source_key")
            }
        result = {}
        keys = list(keys)
        for start in range(0, len(keys), 400):
            batch = keys[start : start + 400]
            query = base + " AND source_key IN (" + ",".join("?" for _ in batch) + ")"
            result.update(
                (row[0], BriefSource(*row)) for row in db.execute(query, batch)
            )
        return result


def source_priorities(root):
    with closing(readonly(Path(root) / FILENAME)) as db:
        rows = db.execute(
            "SELECT url,max(recency),max(paper_channel) FROM sources GROUP BY url"
        )
        recency, channels = {}, {}
        for url, stamp, channel in rows:
            recency[url], channels[url] = stamp, channel
        return recency, channels


def read_browse(root, raw_id, subscription):
    path = Path(root) / FILENAME
    if not path.is_file():
        return None
    with closing(readonly(path)) as db:
        row = db.execute(
            "SELECT browse FROM indexed_raws WHERE raw_id=? AND version=? AND subscription=?",
            (raw_id, VERSION, int(subscription)),
        ).fetchone()
        return json.loads(row[0]) if row else None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="增量建立可重建的文章来源索引，不请求网络"
    )
    parser.add_argument("--store-root", default="data/store")
    args = parser.parse_args(argv)
    print(json.dumps({"indexed_raws": refresh_sources(args.store_root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
