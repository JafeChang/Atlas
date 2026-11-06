"""Daily collection: per-source checkpoints and content-addressed archive writes.

The legacy DAG remains available for explicit replay. This entry point never
opens runs/executions.json and never stores response bodies in execution logs.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from atlas.archive import open_archive
from atlas.collect import ResultStatus, collect_one
from atlas.compose.tasks import (
    COMPOSE_CODE_VERSION,
    ComposeDependencies,
    NormalizeStage,
)
from atlas.contracts import Snapshot, TaskVersions
from atlas.registry import RegistryService
from atlas.registry import open_store as open_registry
from atlas.schedule import read_last_collected
from atlas.source_index import SourceIndex


def collect_due(root, *, dependencies=None, now=None, force=False):
    root = Path(root)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    stamp = moment.timestamp()
    with (root / ".ingest.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "busy", "checked": 0}
        with (
            open_archive(root) as archive,
            closing(
                open_registry(root / "atlas.db", author="incremental-collector")
            ) as registry_store,
        ):
            registry = RegistryService(registry_store)
            channels = list(registry.fetchable_channels())
            if not channels:
                raise ValueError("注册表里没有可采集的渠道")
            versions = TaskVersions(
                code_version=COMPOSE_CODE_VERSION,
                config_version=registry.config_version,
            )
            normalizer = NormalizeStage(
                versions, archive=archive, normalized_dir=root / "normalized"
            )
            previous = read_last_collected(root / "atlas.db")
            deps = dependencies
            report = {
                "state": "complete",
                "checked": 0,
                "new": 0,
                "unchanged": 0,
                "failed": 0,
                "channels": [],
            }
            with closing(sqlite3.connect(root / "ingest.sqlite", timeout=30)) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS polls (
                        channel_id TEXT PRIMARY KEY, config TEXT NOT NULL,
                        checked_at REAL NOT NULL, last_success_at REAL,
                        next_attempt_at REAL NOT NULL, failures INTEGER NOT NULL,
                        raw_id TEXT, error TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS processed_raws (
                        raw_id TEXT PRIMARY KEY, version TEXT NOT NULL
                    );
                """)
                for channel in channels:
                    signature = hashlib.sha256(
                        json.dumps(
                            channel.model_dump(mode="json"), sort_keys=True
                        ).encode()
                    ).hexdigest()
                    old = db.execute(
                        "SELECT config,next_attempt_at,failures,last_success_at FROM polls WHERE channel_id=?",
                        (channel.id,),
                    ).fetchone()
                    if old and old[0] == signature:
                        due = stamp >= old[1]
                    else:
                        last = previous.get(channel.id) if old is None else None
                        due = (
                            last is None
                            or stamp >= last.timestamp() + channel.interval_seconds
                        )
                    if not due and not force:
                        continue
                    if deps is None:
                        deps = ComposeDependencies.real(root)
                    result = collect_one(
                        channel,
                        fetcher=deps.fetcher,
                        robots=deps.robots,
                        throttle=deps.throttle,
                        retry=deps.retry,
                        sleeper=deps.sleeper,
                        clock=deps.clock,
                        wall_clock=lambda: moment,
                        timeout_seconds=deps.timeout_seconds,
                        user_agent_options=deps.user_agent_options,
                    )
                    if deps.collection_observer:
                        result = deps.collection_observer(result)
                    report["checked"] += 1
                    raw_id, error = None, ""
                    if result.status == ResultStatus.COLLECTED:
                        raw, content = result.unwrap()
                        # The archive verifies the bytes and retains the original identity.
                        existed = archive.records.get_optional(raw.raw_id) is not None
                        stored = archive.put(raw, content)
                        raw_id = stored.raw_id
                        done = db.execute(
                            "SELECT version FROM processed_raws WHERE raw_id=?",
                            (raw_id,),
                        ).fetchone()
                        normalized = root / "normalized"
                        if done != (COMPOSE_CODE_VERSION,) or not all(
                            (normalized / (raw_id + suffix)).is_file()
                            for suffix in (".txt", ".map")
                        ):
                            normalizer.run(
                                Snapshot(
                                    payload={
                                        "upstream": {
                                            "archive": {
                                                "identity": {
                                                    "records": [
                                                        stored.model_dump(mode="json")
                                                    ]
                                                }
                                            }
                                        }
                                    }
                                ),
                                Snapshot(),
                            )
                        # Publish source work before advancing the collection checkpoint.
                        from atlas.webapp import ArchiveEntriesAccess

                        with (root / ".entry-index.lock").open("a") as index_lock:
                            fcntl.flock(index_lock, fcntl.LOCK_EX)
                            with closing(SourceIndex(root)) as index:
                                index.index_raw(
                                    archive,
                                    ArchiveEntriesAccess(archive, cache_size=1),
                                    stored,
                                )
                        with db:
                            db.execute(
                                "INSERT OR REPLACE INTO processed_raws VALUES (?,?)",
                                (raw_id, COMPOSE_CODE_VERSION),
                            )
                        report["unchanged" if existed else "new"] += 1
                        del content, raw, stored
                    else:
                        error = (
                            result.failure.reason
                            if result.failure
                            else (result.note or result.status.value)
                        )
                        report["failed"] += 1
                    failures = (
                        (old[2] + 1 if old and old[0] == signature else 1)
                        if error
                        else 0
                    )
                    delay = (
                        max(
                            60,
                            min(
                                channel.interval_seconds, 60 * 2 ** min(failures - 1, 6)
                            ),
                        )
                        if error
                        else channel.interval_seconds
                    )
                    success = old[3] if error and old else (None if error else stamp)
                    with db:
                        db.execute(
                            "INSERT OR REPLACE INTO polls VALUES (?,?,?,?,?,?,?,?)",
                            (
                                channel.id,
                                signature,
                                stamp,
                                success,
                                stamp + delay,
                                failures,
                                raw_id,
                                error[:2000],
                            ),
                        )
                    report["channels"].append(
                        {
                            "channel": channel.id,
                            "raw_id": raw_id,
                            "status": "failed" if error else "ok",
                            "error": error[:500],
                        }
                    )
                    # Release the response before requesting the next source.
                    del result
                return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="日常增量采集；只记录来源检查点，不加载历史执行快照"
    )
    parser.add_argument(
        "--store-root", default=os.environ.get("ATLAS_STORE_ROOT", "data/store")
    )
    parser.add_argument(
        "--force", action="store_true", help="显式补采：忽略轮询间隔，仍按内容去重"
    )
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args(argv)
    if os.environ.get("ATLAS_LIVE") != "1":
        parser.error("真实采集必须显式设置 ATLAS_LIVE=1")
    report = collect_due(args.store_root, force=args.force)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report.get("failed") and not args.allow_partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
