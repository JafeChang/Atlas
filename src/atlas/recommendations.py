"""Per-user, online content recommendations over the existing brief vectors.

Profiles are rebuilt from that user's acknowledged events on each request. Reading
snapshots use their maximum, not their sum; no anonymous or other-user history is
borrowed. Neither profiles nor rankings write to the archive or label databases.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from atlas.brief_search_cli import INDEX_VERSION, readonly, validate_vector

STRATEGY = "personalized-content-v2"
PROFILE_DAYS = 30
IMPRESSION_DAYS = 7
MAX_INTERESTS = 40
MAX_RESULTS = 200


@dataclass(frozen=True)
class Interest:
    key: str
    url: str
    weight: float
    read_ms: int


@dataclass(frozen=True)
class Profile:
    interests: tuple[Interest, ...]
    seen_urls: frozenset[str]
    cursor: int
    impression_weights: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Recommendations:
    entries: tuple[Any, ...]
    decision_id: str
    profile_size: int


def read_profile(path, user_id, resolve, *, now=None):
    """A read-only, user-scoped projection; resolve verifies immutable anchors."""
    if not user_id:
        raise ValueError("推荐需要已登录用户")
    now = now or datetime.now(UTC)
    if not Path(path).exists():
        return Profile((), frozenset(), 0)
    with closing(readonly(path)) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='interaction_events'"
        ).fetchone():
            return Profile((), frozenset(), 0)
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(interaction_events)")
        }
        if not {"user_id", "collection_version", "read_ms"} <= columns:
            return Profile((), frozenset(), 0)
        rows = db.execute(
            "SELECT rowid AS sequence,view_id,raw_id,raw_sha256,char_start,char_end,"
            "event_type,read_ms,occurred_at FROM interaction_events "
            "WHERE user_id=? AND collection_version='v2' "
            "AND event_type IN ('detail_view','read_progress') AND received_at>=? "
            "ORDER BY rowid DESC LIMIT 6000",
            (user_id, (now - timedelta(days=PROFILE_DAYS)).isoformat()),
        ).fetchall()
        # A served item may never enter the viewport. Only acknowledged, qualified
        # impressions affect repetition, separately from positive reading signals.
        impressions = (
            db.execute(
                "SELECT rowid AS sequence,view_id,raw_id,raw_sha256,char_start,"
                "char_end,occurred_at FROM interaction_events "
                "WHERE user_id=? AND collection_version='v2' "
                "AND event_type='list_impression' AND visible_ms>=1000 "
                "AND received_at>=? ORDER BY rowid DESC LIMIT 6000",
                (user_id, (now - timedelta(days=IMPRESSION_DAYS)).isoformat()),
            ).fetchall()
            if "visible_ms" in columns
            else []
        )
    views = {}
    cursor = max((row["sequence"] for row in (*rows, *impressions)), default=0)
    for row in rows:
        if (
            row["char_start"] is None
            or row["char_end"] is None
            or not row["raw_sha256"]
        ):
            continue
        identity = (
            row["raw_id"],
            row["raw_sha256"],
            row["char_start"],
            row["char_end"],
        )
        key = (row["view_id"], identity)
        at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        if not now - timedelta(days=PROFILE_DAYS) <= at <= now + timedelta(minutes=5):
            continue
        previous = views.get(key, (0, at))
        views[key] = (
            max(previous[0], min(row["read_ms"] or 0, 300000)),
            max(previous[1], at),
        )
    sources, interests, seen = {}, {}, set()
    for (_, identity), (read_ms, at) in views.items():
        if identity not in sources:
            sources[identity] = resolve(*identity)
        source = sources[identity]
        if source is None:
            continue
        url = source.url or source.key
        seen.add(url)
        # Opening is weak evidence; sustained foreground reading is stronger.
        strength = (
            0.08
            if read_ms < 10000
            else min(1.0, math.log1p(read_ms / 1000) / math.log(121))
        )
        weight = strength * 2 ** (-max(0, (now - at).total_seconds()) / (14 * 86400))
        old = interests.get(url)
        if old is None or weight > old.weight:
            interests[url] = Interest(source.key, url, weight, read_ms)
    exposed_views = {}
    for row in impressions:
        if (
            row["char_start"] is None
            or row["char_end"] is None
            or not row["raw_sha256"]
        ):
            continue
        at = datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00"))
        if (
            not now - timedelta(days=IMPRESSION_DAYS)
            <= at
            <= now + timedelta(minutes=5)
        ):
            continue
        identity = (
            row["raw_id"],
            row["raw_sha256"],
            row["char_start"],
            row["char_end"],
        )
        if identity not in sources:
            sources[identity] = resolve(*identity)
        source = sources[identity]
        if source is None:
            continue
        # Collapse duplicate events and archive occurrences of one URL per view.
        key = (row["view_id"], source.url or source.key)
        exposed_views[key] = max(exposed_views.get(key, at), at)
    impression_weights = {}
    for (_, url), at in exposed_views.items():
        weight = 2 ** (-max(0, (now - at).total_seconds()) / 86400)
        impression_weights[url] = impression_weights.get(url, 0) + weight
    ordered = sorted(interests.values(), key=lambda item: (-item.weight, item.key))
    return Profile(
        tuple(ordered[:MAX_INTERESTS]),
        frozenset(seen),
        cursor,
        tuple(sorted(impression_weights.items())),
    )


class RecommendationEngine:
    def __init__(self, index_path, events_path, decisions_path):
        self.index_path = Path(index_path)
        self.events_path = Path(events_path)
        self.decisions_path = Path(decisions_path)
        self._lock = threading.RLock()
        self._signature = None

    def _load(self):
        import numpy as np

        stat = self.index_path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        with self._lock:
            if signature == self._signature:
                return self._rows, self._vectors, self._metadata
            with closing(readonly(self.index_path)) as db:
                metadata = {
                    r[0]: json.loads(r[1])
                    for r in db.execute("SELECT key,value FROM brief_search_meta")
                }
                if metadata.get("index_version") != INDEX_VERSION:
                    raise ValueError("推荐索引版本不符")
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT source_key,source_url,embedding FROM brief_search_documents ORDER BY source_key"
                    )
                ]
            if len(rows) != metadata["document_count"]:
                raise ValueError("推荐索引计数不符")
            for row in rows:
                validate_vector(row["embedding"])
            vectors = (
                np.vstack(
                    [np.frombuffer(r.pop("embedding"), dtype="<f4") for r in rows]
                )
                if rows
                else np.empty((0, 384), dtype="float32")
            )
            if len(vectors):
                # Remove the corpus-wide common direction before item similarity.
                # This is a derived recommendation score, not a changed E5 protocol.
                vectors = vectors - vectors.mean(axis=0)
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                vectors = np.divide(
                    vectors, norms, out=np.zeros_like(vectors), where=norms > 1e-8
                )
            vectors.setflags(write=False)
            self._rows, self._vectors, self._metadata = rows, vectors, metadata
            self._signature = signature
            return rows, vectors, metadata

    def rank(self, candidates, user_id, resolve, *, allows=lambda key: True, now=None):
        import numpy as np

        if not user_id:
            raise ValueError("推荐需要已登录用户")
        now = now or datetime.now(UTC)
        rows, vectors, metadata = self._load()
        positions = {r["source_key"]: i for i, r in enumerate(rows)}
        profile = read_profile(self.events_path, user_id, resolve, now=now)
        interests = [
            s for s in profile.interests if s.key in positions and allows(s.key)
        ]
        # Filters have already run. Deduplicate only their surviving occurrences.
        by_url = {}
        for entry, key in sorted(
            candidates,
            key=lambda pair: (pair[0].fetched_at, pair[0].raw_id),
            reverse=True,
        ):
            if (
                key not in positions
                or not allows(key)
                or dict(entry.confirmed_values or ()).get("valid") == "invalid"
            ):
                continue
            url = entry.link or entry.endpoint
            by_url.setdefault(url, (entry, key))
        eligible = list(by_url.values())
        indexes = [positions[key] for _, key in eligible]
        affinity = np.zeros(len(eligible))
        if interests and eligible:
            weights = np.asarray([s.weight for s in interests])
            weights /= max(1.0, float(weights.max()))
            similarity = (
                vectors[indexes] @ vectors[[positions[s.key] for s in interests]].T
            )
            affinity = np.maximum(0, similarity * weights).max(axis=1)
        scores, sources, impression_penalties = [], [], []
        impression_weights = dict(profile.impression_weights)
        for i, (entry, _key) in enumerate(eligible):
            age = max(0, (now - entry.timestamp).total_seconds()) / 86400
            fresh = 2 ** (-age / 7)
            url = entry.link or entry.endpoint
            score = (0.78 * float(affinity[i]) + 0.22 * fresh) if interests else fresh
            if url in profile.seen_urls:
                score -= 0.5
            # Temporary fatigue of this article is not dislike of its topic.
            penalty = min(0.6, 0.3 * impression_weights.get(url, 0))
            score -= penalty
            impression_penalties.append(penalty)
            scores.append(score)
            sources.append(urlsplit(url).hostname or entry.channel_id)
        order = sorted(range(len(eligible)), key=lambda i: (-scores[i], eligible[i][1]))
        # Bounded multi-interest shortlist with recent unseen discovery candidates.
        recent = sorted(
            range(len(eligible)),
            key=lambda i: (-eligible[i][0].timestamp.timestamp(), eligible[i][1]),
        )
        discovery = [
            i
            for i in recent
            if (eligible[i][0].link or eligible[i][0].endpoint) not in profile.seen_urls
        ][:100]
        remaining = set(order[:300] + discovery)
        chosen = []
        while remaining and len(chosen) < MAX_RESULTS:
            recent_sources = [sources[i] for i in chosen[-5:]]
            # Every tenth slot explores recent content, independently of interest.
            options = (
                [i for i in discovery if i in remaining]
                if interests and len(chosen) % 10 == 9
                else []
            )
            options = options or sorted(remaining)
            best = min(
                options,
                key=lambda i: (
                    -(scores[i] - 0.08 * recent_sources.count(sources[i])),
                    eligible[i][1],
                ),
            )
            chosen.append(best)
            remaining.remove(best)
        decision_id = uuid.uuid4().hex
        self._record(
            decision_id,
            user_id,
            now,
            profile,
            metadata,
            eligible,
            scores,
            chosen,
            impression_penalties,
        )
        return Recommendations(
            tuple(eligible[i][0] for i in chosen), decision_id, len(interests)
        )

    def _record(
        self,
        decision_id,
        user_id,
        now,
        profile,
        metadata,
        eligible,
        scores,
        chosen,
        impression_penalties,
    ):
        """Independent telemetry records the full eligible pool and policy version."""
        selected = {i: rank for rank, i in enumerate(chosen)}
        payload = {
            "strategy": STRATEGY,
            "experiment": None,
            "profile_cursor": profile.cursor,
            "profile": [
                {"source_key": s.key, "weight": s.weight} for s in profile.interests
            ],
            "index_snapshot": metadata.get("source_snapshot_sha256"),
            "model_revision": metadata.get("model_revision"),
            "candidates": [
                {
                    "source_key": key,
                    "raw_id": entry.raw_id,
                    "raw_sha256": entry.raw_sha256,
                    "char_start": entry.char_start,
                    "char_end": entry.char_end,
                    "score": scores[i],
                    "impression_penalty": impression_penalties[i],
                    "rank": selected.get(i),
                }
                for i, (entry, key) in enumerate(eligible)
            ],
        }
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.decisions_path, timeout=10) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS recommendation_decisions (decision_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS recommendation_decisions_user ON recommendation_decisions(user_id,created_at)"
            )
            db.execute(
                "INSERT INTO recommendation_decisions VALUES(?,?,?,?)",
                (
                    decision_id,
                    user_id,
                    now.isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            db.execute(
                "DELETE FROM recommendation_decisions WHERE created_at<?",
                ((now - timedelta(days=14)).isoformat(),),
            )
            db.execute(
                "DELETE FROM recommendation_decisions WHERE rowid IN (SELECT rowid FROM recommendation_decisions WHERE user_id=? ORDER BY rowid DESC LIMIT -1 OFFSET 200)",
                (user_id,),
            )
