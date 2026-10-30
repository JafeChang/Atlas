"""增量建表；来源目录可更新，验证、采集及产能公告历史只追加。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import INDUSTRY_ID, INDUSTRY_NAME, INDUSTRY_NAME_EN


def now_iso():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    # 保留 feed 查询语义；仅去掉跟踪参数和 fragment。
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.casefold().startswith("utm_")
        and k.casefold() not in ("fbclid", "gclid")
    ]
    return urlunsplit(
        (
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def source_id(url):
    return "opa-" + digest(canonical_url(url))[:24]


DDL = """
CREATE TABLE IF NOT EXISTS aluminium_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aluminium_sources (
    source_id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE,
    original_json TEXT NOT NULL, channel_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aluminium_rss_connections (
    source_id TEXT PRIMARY KEY REFERENCES aluminium_sources(source_id),
    endpoint TEXT NOT NULL, canonical_endpoint TEXT NOT NULL UNIQUE,
    directory_url TEXT NOT NULL, configured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aluminium_probes (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, tested_at TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS aluminium_probes_source ON aluminium_probes(source_id,id);
CREATE TABLE IF NOT EXISTS aluminium_attempts (
    id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, channel_id TEXT NOT NULL,
    attempted_at TEXT NOT NULL, successful INTEGER NOT NULL,
    content_hash TEXT, changed INTEGER, raw_id TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS aluminium_attempts_channel ON aluminium_attempts(channel_id,id);
CREATE TABLE IF NOT EXISTS aluminium_candidates (
    candidate_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, raw_id TEXT NOT NULL,
    source_url TEXT NOT NULL, original_title TEXT NOT NULL, body TEXT NOT NULL,
    body_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aluminium_extractions (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, source_id TEXT NOT NULL,
    project_id TEXT, story_key TEXT NOT NULL, model TEXT NOT NULL,
    data_json TEXT NOT NULL, supersedes TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS aluminium_candidates_origin
ON aluminium_candidates(source_id,raw_id,source_url,original_title);
CREATE INDEX IF NOT EXISTS aluminium_extractions_project ON aluminium_extractions(project_id,created_at);
CREATE TABLE IF NOT EXISTS aluminium_processing_errors (
    id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aluminium_inputs (
    candidate_id TEXT NOT NULL, body_hash TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(candidate_id,body_hash)
);
CREATE TABLE IF NOT EXISTS aluminium_processing_results (
    id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processed','no_event','failed')),
    model TEXT NOT NULL, event_count INTEGER NOT NULL CHECK(event_count>=0),
    reason TEXT NOT NULL, processed_at TEXT NOT NULL, retry_after TEXT
);
CREATE INDEX IF NOT EXISTS aluminium_processing_results_candidate
ON aluminium_processing_results(candidate_id,id);
"""


class AluminiumStore:
    def __init__(self, path: str | Path, *, read_only=False):
        self.path = Path(path)
        self._batch = False
        if read_only:
            self.db = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
            )
            self.db.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        version = self.db.execute(
            "SELECT value FROM aluminium_meta WHERE key='schema_version'"
        ).fetchone()
        if version and version[0] != "1":
            self.db.close()
            raise ValueError("不支持的电解铝 schema 版本；需要显式迁移")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_meta VALUES ('schema_version','1')"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_meta VALUES ('industry',?)",
                (
                    json.dumps(
                        {
                            "id": INDUSTRY_ID,
                            "name": INDUSTRY_NAME,
                            "name_en": INDUSTRY_NAME_EN,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        for table in (
            "aluminium_probes",
            "aluminium_attempts",
            "aluminium_candidates",
            "aluminium_extractions",
            "aluminium_processing_errors",
            "aluminium_inputs",
            "aluminium_processing_results",
        ):
            for action in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append-only'); END"
                )
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def add_sources(self, sources, channels):
        inserted = 0
        with self.db:
            for source in sources:
                url = canonical_url(source.url)
                inserted += self.db.execute(
                    "INSERT OR IGNORE INTO aluminium_sources VALUES (?,?,?,?,?)",
                    (
                        source_id(source.url),
                        url,
                        source.model_dump_json(),
                        channels.get(url),
                        now_iso(),
                    ),
                ).rowcount
                if channels.get(url):
                    self.db.execute(
                        "UPDATE aluminium_sources SET channel_id=? WHERE canonical_url=? AND channel_id IS NULL",
                        (channels[url], url),
                    )
        return inserted

    def sources(self):
        connections = self._rss_connections()
        return [
            {**dict(r), "connection": connections.get(r["source_id"])}
            for r in self.db.execute(
                "SELECT * FROM aluminium_sources ORDER BY source_id"
            )
        ]

    def _rss_connections(self):
        # 旧库可直接只读打开；新表由写入口增量创建。
        if not self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aluminium_rss_connections'"
        ).fetchone():
            return {}
        return {
            row["source_id"]: dict(row)
            for row in self.db.execute("SELECT * FROM aluminium_rss_connections")
        }

    def source(self, sid):
        row = self.db.execute(
            "SELECT * FROM aluminium_sources WHERE source_id=? OR channel_id=?",
            (sid, sid),
        ).fetchone()
        return (
            {**dict(row), "connection": self._rss_connections().get(row["source_id"])}
            if row
            else None
        )

    def connect_rss(self, sid, endpoint, channel_id):
        from .models import Source, collection_source

        row = self.source(sid)
        if not row:
            raise ValueError("未知信源")
        original = Source.model_validate_json(row["original_json"])
        collection_source(original, {"endpoint": endpoint})
        if row["channel_id"] not in (None, channel_id):
            raise ValueError("信源已关联其他渠道")
        existing = row["connection"]
        if existing and canonical_url(existing["endpoint"]) != canonical_url(endpoint):
            raise ValueError("信源已有其他订阅地址；不能覆盖已有配置")
        with self.db:
            self.db.execute(
                "INSERT INTO aluminium_rss_connections VALUES (?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING",
                (sid, endpoint, canonical_url(endpoint), original.url, now_iso()),
            )
            self.db.execute(
                "UPDATE aluminium_sources SET channel_id=? WHERE source_id=? AND channel_id IS NULL",
                (channel_id, sid),
            )

    def probe(self, sid, result, *, tested_at=None):
        with self.db:
            self.db.execute(
                "INSERT INTO aluminium_probes(source_id,tested_at,result_json) VALUES(?,?,?)",
                (sid, tested_at or now_iso(), json.dumps(result, ensure_ascii=False)),
            )

    def latest_probe(self, sid):
        row = self.db.execute(
            "SELECT tested_at,result_json FROM aluminium_probes WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        return (
            {**json.loads(row["result_json"]), "tested_at": row["tested_at"]}
            if row
            else None
        )

    def attempt(
        self,
        sid,
        channel_id,
        *,
        successful,
        content_hash=None,
        raw_id=None,
        error=None,
        attempted_at=None,
    ):
        previous = self.db.execute(
            "SELECT content_hash FROM aluminium_attempts WHERE source_id=? AND successful=1 ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        changed = (
            bool(not previous or previous[0] != content_hash) if successful else None
        )
        with self.db:
            self.db.execute(
                "INSERT INTO aluminium_attempts(source_id,channel_id,attempted_at,successful,content_hash,changed,raw_id,error) VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid,
                    channel_id,
                    attempted_at or now_iso(),
                    int(successful),
                    content_hash,
                    changed,
                    raw_id,
                    error,
                ),
            )
        return changed

    def status(self):
        rows = self.sources()
        for row in rows:
            source = json.loads(row.pop("original_json"))
            row.update(source)
            row["probe"] = self.latest_probe(row["source_id"])
            last = self.db.execute(
                "SELECT * FROM aluminium_attempts WHERE source_id=? ORDER BY id DESC LIMIT 1",
                (row["source_id"],),
            ).fetchone()
            success = self.db.execute(
                "SELECT attempted_at FROM aluminium_attempts WHERE source_id=? AND successful=1 ORDER BY id DESC LIMIT 1",
                (row["source_id"],),
            ).fetchone()
            row.update(
                last_attempt=dict(last) if last else None,
                last_success_at=success[0] if success else None,
            )
        return rows

    def candidate(self, sid, raw_id, url, title, body):
        # 同一来源同一文章内容跨 feed 快照只保留一份候选；不同来源仍独立保留。
        existing = self.db.execute(
            "SELECT candidate_id FROM aluminium_candidates WHERE source_id=? AND raw_id=? AND source_url=? AND original_title=? ORDER BY created_at LIMIT 1",
            (sid, raw_id, url, title),
        ).fetchone()
        if existing:
            return existing[0]
        key = digest([sid, canonical_url(url), title, body])
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_candidates VALUES (?,?,?,?,?,?,?,?)",
                (key, sid, raw_id, url, title, body, digest(body), now_iso()),
            )
        return key

    def pending(self, limit=100, *, retry_errors=False):
        # 负面分类是完成状态，失败按冷却时间自动续跑；旧错误记录仍保留。
        condition = " AND NOT EXISTS (SELECT 1 FROM aluminium_processing_results r WHERE r.candidate_id=c.candidate_id AND r.id=(SELECT MAX(id) FROM aluminium_processing_results WHERE candidate_id=c.candidate_id) AND r.status IN ('processed','no_event'))"
        parameters = []
        if not retry_errors:
            condition += " AND (NOT EXISTS(SELECT 1 FROM aluminium_processing_errors e WHERE e.candidate_id=c.candidate_id) OR EXISTS(SELECT 1 FROM aluminium_processing_results r WHERE r.candidate_id=c.candidate_id))"
            condition += " AND NOT EXISTS(SELECT 1 FROM aluminium_processing_results r WHERE r.candidate_id=c.candidate_id AND r.id=(SELECT MAX(id) FROM aluminium_processing_results WHERE candidate_id=c.candidate_id) AND r.status='failed' AND r.retry_after>?)"
            parameters.append(now_iso())
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT c.* FROM aluminium_candidates c WHERE NOT EXISTS (SELECT 1 FROM aluminium_extractions e WHERE e.candidate_id=c.candidate_id)"
                + condition
                + " ORDER BY c.created_at LIMIT ?",
                (*parameters, limit),
            )
        ]

    def processing_result(
        self, candidate, *, status, model, event_count=0, reason="", retry_seconds=3600
    ):
        moment = datetime.now(UTC)
        retry_after = (
            (moment + timedelta(seconds=retry_seconds)).isoformat(
                timespec="microseconds"
            )
            if status == "failed"
            else None
        )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_inputs VALUES (?,?,?,?)",
                (
                    candidate["candidate_id"],
                    digest(candidate["body"]),
                    candidate["body"],
                    now_iso(),
                ),
            )
            self.db.execute(
                "INSERT INTO aluminium_processing_results(candidate_id,status,model,event_count,reason,processed_at,retry_after) VALUES(?,?,?,?,?,?,?)",
                (
                    candidate["candidate_id"],
                    status,
                    model,
                    event_count,
                    reason,
                    moment.isoformat(timespec="microseconds"),
                    retry_after,
                ),
            )

    def error(self, candidate_id, error):
        with self.db:
            self.db.execute(
                "INSERT INTO aluminium_processing_errors(candidate_id,occurred_at,error) VALUES(?,?,?)",
                (candidate_id, now_iso(), error),
            )

    def save(self, candidate, intelligence, *, model):
        from .models import CAPACITY_FIELDS, capacity_tpy

        data = intelligence.model_dump(mode="json")
        # 来源身份由程序提供，不能听从模型填入任意 URL。
        source = self.db.execute(
            "SELECT original_json FROM aluminium_sources WHERE source_id=?",
            (candidate["source_id"],),
        ).fetchone()
        if not source:
            raise ValueError("候选没有登记来源")
        data.update(
            source_url=candidate["source_url"],
            source_type=json.loads(source[0])["type"],
        )
        data["eligible_for_capacity"] = intelligence.eligible_for_capacity
        data["input_body_sha256"] = digest(candidate["body"])
        evidence = data["evidence"]
        if evidence and evidence not in candidate["body"]:
            raise ValueError("原文证据不是输入正文的逐字子串")
        if any(
            data[f] is not None for f in (*CAPACITY_FIELDS, "actual_production_tonnes")
        ) and not re.search(r"\d", evidence or ""):
            raise ValueError("产能证据缺少数字")
        for field in (*CAPACITY_FIELDS, "actual_production_tonnes"):
            if data[field] is None:
                continue
            proof = intelligence.numeric_evidence.get(field)
            if not proof or proof.quote not in candidate["body"]:
                raise ValueError(f"{field} 缺少可核验的数字原文")
            tokens = re.findall(
                r"(?<!\w)\d[\d,]*(?:\.\d+)?[km]?(?!\w)", proof.quote, re.I
            )
            if proof.raw_value.replace(",", "").casefold() not in [
                t.replace(",", "").casefold() for t in tokens
            ]:
                raise ValueError(f"{field} 的数字不在原文证据内")
            if proof.unit.casefold() not in proof.quote.casefold():
                raise ValueError(f"{field} 的单位不在原文证据内")
            if (
                field == "operating_capacity_tpy"
                and "nameplate" in proof.quote.casefold()
                and not re.search(
                    r"operating|in operation|currently|运行", proof.quote, re.I
                )
            ):
                raise ValueError("名义产能证据不能用作运行产能")
            if field == "actual_production_tonnes":
                factor = {
                    "tonnes": 1,
                    "吨": 1,
                    "kt": 1000,
                    "million tonnes": 1000000,
                    "万吨": 10000,
                }.get(proof.unit.casefold())
                if factor is None:
                    raise ValueError("实际产量不能使用年产能单位")
                converted = capacity_tpy(proof.raw_value) * factor
            else:
                unit = proof.unit
                if unit.casefold() in (
                    "tonnes",
                    "million tonnes",
                    "thousand tonnes",
                ) and re.search(
                    r"\bannual\s+(?:production\s+)?capacity\b", proof.quote, re.I
                ):
                    unit += "/year"
                    if unit == "tonnes/year":
                        unit = "tpy"
                converted = capacity_tpy(proof.raw_value, unit)
            if abs(converted - data[field]) > max(0.000001, converted * 1e-9):
                raise ValueError(f"{field} 与原文单位转换不符")
        # 同名设施在同国同公司内关联；缺信息则留空，不按公司猜项目。
        facility = data["project_name"] or data["smelter_name"]
        project = (
            digest(
                [_entity(data["country"]), _entity(data["company"]), _entity(facility)]
            )
            if all((facility, data["country"], data["company"]))
            else None
        )
        # 标题/日期完全匹配才分组，关联不会改变候选、Raw 或人工标签身份。
        story = digest(
            [
                project,
                _entity(candidate["original_title"]),
                data["event_type"],
                data["event_date"],
                data["planned_event_date"],
            ]
        )
        previous = self.db.execute(
            "SELECT e.id FROM aluminium_extractions e JOIN aluminium_candidates c ON c.candidate_id=e.candidate_id WHERE c.source_url=? AND c.source_id=? AND (c.candidate_id!=? OR e.model!=?) AND e.project_id IS ? AND json_extract(e.data_json,'$.event_type') IS ? ORDER BY e.created_at DESC LIMIT 1",
            (
                candidate["source_url"],
                candidate["source_id"],
                candidate["candidate_id"],
                model,
                project,
                data["event_type"],
            ),
        ).fetchone()
        if data["corrects_source_url"]:
            previous = (
                self.db.execute(
                    "SELECT e.id FROM aluminium_extractions e JOIN aluminium_candidates c ON c.candidate_id=e.candidate_id WHERE c.source_url=? AND e.project_id=? ORDER BY e.created_at DESC LIMIT 1",
                    (data["corrects_source_url"], project),
                ).fetchone()
                if project
                else None
            )
        key = digest([candidate["candidate_id"], model, data])
        with nullcontext() if self._batch else self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_inputs VALUES (?,?,?,?)",
                (
                    candidate["candidate_id"],
                    data["input_body_sha256"],
                    candidate["body"],
                    now_iso(),
                ),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO aluminium_extractions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    candidate["candidate_id"],
                    candidate["source_id"],
                    project,
                    story,
                    model,
                    json.dumps(data, ensure_ascii=False),
                    previous[0] if previous and previous[0] != key else None,
                    now_iso(),
                ),
            )
        return key

    def save_events(self, candidate, events, *, model):
        if self._batch:
            raise ValueError("不可嵌套提取事务")
        with self.db:
            self._batch = True
            try:
                return [self.save(candidate, event, model=model) for event in events]
            finally:
                self._batch = False

    def intelligence(self):
        rows = [
            dict(r)
            for r in self.db.execute(
                "SELECT e.*,c.source_url,c.original_title,c.raw_id FROM aluminium_extractions e JOIN aluminium_candidates c ON c.candidate_id=e.candidate_id ORDER BY e.created_at,e.id"
            )
        ]
        for r in rows:
            r["data"] = json.loads(r.pop("data_json"))
        groups = {}
        source_types = {
            r["source_id"]: json.loads(r["original_json"])["type"]
            for r in self.sources()
        }

        def rank(r):
            return (
                0
                if source_types[r["source_id"]].startswith("company")
                else 1
                if source_types[r["source_id"]]
                in ("regulatory filing", "exchange filing", "government project")
                else 2,
                r["created_at"],
                r["id"],
            )

        for r in rows:
            groups.setdefault(r["story_key"], []).append(r)
        superseded = {r["supersedes"] for r in rows if r["supersedes"]}
        for group in groups.values():
            preferred = min(
                (r for r in group if r["id"] not in superseded), key=rank, default=None
            ) or min(group, key=rank)
            for r in group:
                r["is_superseded"] = r["id"] in superseded
                r["preferred_id"] = preferred["id"]
                r["related_sources"] = list(
                    dict.fromkeys(x["source_url"] for x in group)
                )
        return rows


def _entity(value):
    return re.sub(r"[^\w]+", " ", (value or "").casefold()).strip()


def has_aluminium(path):
    path = Path(path)
    if not path.is_file():
        return False
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ) as db:
        return bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='aluminium_sources'"
            ).fetchone()
        )
