"""海外电解铝验收：全部使用本地 fixtures/Mock；实时探测结果单独记录。"""

from __future__ import annotations

import itertools
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pydantic import ValidationError

from atlas.aluminium import INDUSTRY_ID
from atlas.aluminium.models import Intelligence, capacity_tpy
from atlas.aluminium.processing import (
    IntelligenceClient,
    documents_for,
    process_archive,
)
from atlas.aluminium.rules import BusinessType, business_hint, keyword_matches
from atlas.aluminium.sources import (
    channel_for,
    import_sources,
    inspect_content,
    load_manifest,
    observe_result,
    probe_sources,
)
from atlas.aluminium.store import AluminiumStore, canonical_url, digest, source_id
from atlas.aluminium.ui import SourceManagement
from atlas.archive import open_archive
from atlas.collect import Collector, DomainThrottle, RetryPolicy, RobotsCache
from atlas.collect.fetch import FetchResult
from atlas.collect.robots import RobotsFetchResult
from atlas.compose.tasks import ComposeDependencies
from atlas.contracts import RawRecord
from atlas.entries import EntryParseError
from atlas.registry import Industry, open_store
from atlas.schedule import SqliteLastCollectionSource, due_channels

MANIFEST = (
    Path(__file__).resolve().parents[1] / "config/overseas_aluminium_sources.json"
)
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>News</title><link>https://example.org</link><description>News</description><item><title>Primary aluminium smelter first metal</title><link>https://example.org/news/first-metal</link><description>The primary aluminium smelter produced first metal on 2026-10-01. Nameplate capacity is 500 ktpa.</description></item></channel></rss>"""
TEXT = "The primary aluminium smelter produced first metal on 2026-10-01. Nameplate capacity is 500 ktpa. "
HTML = (
    "<html><head><title>Primary aluminium smelter</title></head><body><main>"
    + TEXT * 3
    + "</main></body></html>"
).encode()


def fixtures(tmp_path):
    db = tmp_path / "atlas.db"
    import_sources(MANIFEST, db)
    sources = load_manifest(MANIFEST).sources
    return db, sources


def dependencies(content=HTML, status=200):
    calls = []

    def fetch(req):
        calls.append(req.url)
        return FetchResult(url=req.url, status_code=status, content=content)

    deps = ComposeDependencies(
        fetcher=fetch,
        robots=RobotsCache(
            lambda url, **kw: RobotsFetchResult(
                url, status_code=200, body=b"User-agent: *\nAllow: /"
            )
        ),
        throttle=DomainThrottle(
            global_min_interval=0,
            clock=lambda ticks=itertools.count(step=10): next(ticks),
            sleeper=lambda _: None,
        ),
        sleeper=lambda _: None,
        retry=RetryPolicy(max_attempts=1),
    )
    return deps, calls


def result_for(source, content=HTML, status=200):
    deps, _ = dependencies(content, status)
    return Collector(
        fetcher=deps.fetcher,
        robots=deps.robots,
        throttle=deps.throttle,
        retry=deps.retry,
    ).collect(channel_for(source, enabled=True))


def event(**values):
    data = {
        "country": "Bahrain",
        "company": "Example",
        "project_name": "Line 7",
        "smelter_name": "Example Smelter",
        "event_type": "FIRST_METAL",
        "event_status": "actual",
        "event_date": "2026-10-01",
        "announced_capacity_tpy": 500000,
        "evidence": TEXT.strip(),
        "business_type": BusinessType.PRIMARY,
        "country_is_overseas": True,
        "primary_capacity_explicit": True,
        "numeric_evidence": {
            "announced_capacity_tpy": {
                "raw_value": "500",
                "unit": "ktpa",
                "quote": "Nameplate capacity is 500 ktpa.",
            }
        },
        "title_zh": "某铝厂首次出铝",
        "summary_zh": "铝厂首次出铝，公告年产能五十万吨，尚未全面达产。",
    }
    data.update(values)
    return Intelligence.model_validate(data)


def candidate(
    store, source, *, title="Primary aluminium smelter first metal", body=TEXT, url=None
):
    key = store.candidate(
        source_id(source.url),
        "raw-test-" + digest(body)[:16],
        url or source.url + "/news/first",
        title,
        body,
    )
    return dict(
        store.db.execute(
            "SELECT * FROM aluminium_candidates WHERE candidate_id=?", (key,)
        ).fetchone()
    )


def test_manifest_exact_and_modes():
    m = load_manifest(MANIFEST)
    assert len(m.sources) == 37
    assert Counter(s.mode for s in m.sources) == {
        "web": 26,
        "rss": 3,
        "rss_directory": 1,
        "email": 1,
        "api_pending": 1,
        "paid": 5,
    }
    assert all(
        s.pending_reason
        for s in m.sources
        if s.mode in ("email", "paid", "api_pending", "rss_directory")
    )


@pytest.mark.parametrize(
    "mutation", ["missing", "count", "duplicate", "priority", "ingestion", "url"]
)
def test_invalid_input_never_creates_database(tmp_path, mutation):
    data = json.loads(MANIFEST.read_text())
    if mutation == "missing":
        del data["sources"][0]["region"]
    elif mutation == "count":
        data["sources"].pop()
    elif mutation == "duplicate":
        data["sources"][1] = data["sources"][0]
    else:
        data["sources"][0][mutation] = {
            "priority": "P9",
            "ingestion": "invented",
            "url": "file:///etc/passwd",
        }[mutation]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        import_sources(path, tmp_path / "bad.db")
    assert not (tmp_path / "bad.db").exists()
    assert len(load_manifest(MANIFEST).sources) == 37


def test_import_idempotent_preserves_existing_and_history(tmp_path):
    db = tmp_path / "atlas.db"
    with open_store(
        db,
        author="existing",
        industries=(Industry(id="old", name="旧行业", enabled=True),),
    ) as registry:
        original_digest = registry.current.digest()
    first = import_sources(MANIFEST, db)
    assert first["inserted_sources"] == 37 and first["created_channels"] == 29
    second = import_sources(MANIFEST, db)
    assert second["inserted_sources"] == second["created_channels"] == 0
    assert first["config_version"] == second["config_version"]
    with open_store(db, author="check") as registry:
        assert registry.snapshot(1).digest() == original_digest
        assert "old" in registry.label_space() and INDUSTRY_ID in registry.label_space()
        assert len(registry.current.channels) == 29
        assert all(not c.enabled for c in registry.current.channels)
        assert {c.interval_seconds for c in registry.current.channels} == {
            21600,
            43200,
            86400,
        }
    with AluminiumStore(db, read_only=True) as store:
        assert {json.loads(r["original_json"])["url"] for r in store.sources()} == {
            s.url for s in load_manifest(MANIFEST).sources
        }


def test_existing_same_endpoint_not_duplicated_or_reconfigured(tmp_path):
    src = load_manifest(MANIFEST).sources[0]
    ch = channel_for(src, enabled=True)
    db = tmp_path / "atlas.db"
    with open_store(
        db,
        author="old",
        industries=(Industry(id=INDUSTRY_ID, name="existing", enabled=True),),
        channels=(ch,),
    ) as registry:
        before = registry.current.channels[0].payload()
    assert import_sources(MANIFEST, db)["created_channels"] == 28
    with open_store(db, author="check") as registry:
        assert (
            next(
                c for c in registry.current.channels if c.endpoint == src.url
            ).payload()
            == before
        )


def test_rss_valid_and_abnormal_responses(tmp_path):
    _, sources = fixtures(tmp_path)
    src = next(s for s in sources if s.mode == "rss")
    assert inspect_content(src, RSS)["entry_count"] == 1
    for bad in (HTML, b"<rss><channel><item>", b"", b'{"error":403}'):
        with pytest.raises((ValueError, EntryParseError)):
            inspect_content(src, bad)
    assert inspect_content(src, RSS)["structure"] == "rss"
    docs = documents_for(src, RSS)
    assert len(docs) == 1 and "500 ktpa" in docs[0]["body"]


def test_web_semantic_update_ignores_navigation_and_detects_data(tmp_path):
    db, sources = fixtures(tmp_path)
    src = sources[0]
    first = inspect_content(src, HTML)
    nav = HTML.replace(b"<body>", b"<body><nav>Updated navigation 124</nav>")
    assert inspect_content(src, nav)["content_hash"] == first["content_hash"]
    updated = HTML.replace(b"500 ktpa", b"600 ktpa")
    assert inspect_content(src, updated)["content_hash"] != first["content_hash"]
    observe_result(db, result_for(src))
    observe_result(db, result_for(src))
    observe_result(db, result_for(src, updated))
    with AluminiumStore(db, read_only=True) as store:
        assert [
            r[0]
            for r in store.db.execute(
                "SELECT changed FROM aluminium_attempts ORDER BY id"
            )
        ] == [1, 0, 1]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Primary aluminium smelter announces first metal", True),
        ("Potline restart in Europe", True),
        ("smelter aluminium mulai beroperasi", True),
        ("Aluminum smelters report capacity expansion", True),
        ("Electrolysis commissioning", True),
        ("aluminium smelter has a new logo", False),
        ("Oil refinery shutdown", False),
        ("Alumina refinery capacity expansion", False),
        ("Secondary aluminum recycling capacity expansion", False),
    ],
)
def test_keyword_groups(text, expected):
    assert keyword_matches(text)["candidate"] is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("primary aluminium smelter", BusinessType.PRIMARY),
        ("alumina refinery", BusinessType.ALUMINA),
        ("secondary aluminum recycling", BusinessType.SECONDARY),
        ("primary aluminium smelter and alumina refinery", BusinessType.MIXED),
        ("factory output", BusinessType.UNKNOWN),
    ],
)
def test_business_types(text, expected):
    assert business_hint(text) == expected


def test_primary_only_actual_overseas_statistics():
    assert event().eligible_for_capacity
    for kind in (
        BusinessType.ALUMINA,
        BusinessType.SECONDARY,
        BusinessType.MIXED,
        BusinessType.UNKNOWN,
    ):
        assert not event(
            business_type=kind, primary_capacity_explicit=False
        ).eligible_for_capacity
    assert not event(country_is_overseas=None).eligible_for_capacity
    assert not event(country="China", country_is_overseas=False).eligible_for_capacity
    assert not event(
        event_status="planned",
        event_date=None,
        planned_event_date="2027-01-01",
        evidence=TEXT + "Planned on 2027-01-01.",
    ).eligible_for_capacity


def test_first_metal_not_full_capacity_dates_and_nulls():
    first = event(
        expected_full_capacity_date="2027-06-01",
        evidence=TEXT + "Expected full capacity on 2027-06-01.",
    )
    assert first.event_type == "FIRST_METAL" and first.operating_capacity_tpy is None
    assert first.actual_production_tonnes is None
    with pytest.raises(ValidationError):
        event(event_type="FULL_CAPACITY")
    with pytest.raises(ValidationError):
        event(event_status="planned")
    assert (
        event(
            event_type="FULL_CAPACITY",
            event_date=None,
            evidence="The smelter reached full capacity. Nameplate capacity is 500 ktpa.",
        ).event_type
        == "FULL_CAPACITY"
    )


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (500, "ktpa", 500000),
        (0.5, "mtpa", 500000),
        (50, "万吨/年", 500000),
        ("500,000", "tpy", 500000),
        (None, "tpy", None),
        (10, "short tons/year", 9.0718474),
    ],
)
def test_capacity_unit_conversion(value, unit, expected):
    assert capacity_tpy(value, unit) == expected


@pytest.mark.parametrize(
    "value,unit",
    [
        (10, "tonnes/month"),
        (10, "tonnes"),
        (-1, "tpy"),
        (float("nan"), "tpy"),
        (True, "tpy"),
    ],
)
def test_bad_capacity_unit_not_annualized(value, unit):
    with pytest.raises(ValueError):
        capacity_tpy(value, unit)
    assert capacity_tpy(1, "tpy") == 1


def test_evidence_numeric_verification_and_history(tmp_path):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0])
        key = store.save(c, event(), model="mock/1")
        assert store.save(c, event(), model="mock/1") == key
        assert len(store.intelligence()) == 1
        with pytest.raises(ValueError, match="单位转换"):
            store.save(c, event(announced_capacity_tpy=900000), model="mock/1")
        with pytest.raises(ValueError, match="逐字"):
            store.save(
                c, event(evidence="Imaginary 500 ktpa on 2026-10-01"), model="mock/1"
            )
        c2 = candidate(store, sources[0], body=TEXT.replace("500", "600"))
        updated = event(
            announced_capacity_tpy=600000,
            evidence=TEXT.replace("500", "600").strip(),
            numeric_evidence={
                "announced_capacity_tpy": {
                    "raw_value": "600",
                    "unit": "ktpa",
                    "quote": "Nameplate capacity is 600 ktpa.",
                }
            },
        )
        store.save(c2, updated, model="mock/1")
        rows = store.intelligence()
        assert len(rows) == 2 and rows[-1]["supersedes"] == key
        assert rows[0]["project_id"] == rows[1]["project_id"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.db.execute("DELETE FROM aluminium_extractions")


def test_multisource_duplicate_keeps_corporate_preferred_and_all_links(tmp_path):
    db, sources = fixtures(tmp_path)
    corp = next(s for s in sources if s.type == "company")
    agg = next(s for s in sources if s.type == "news aggregation")
    with AluminiumStore(db) as store:
        secondary = candidate(store, agg)
        store.save(secondary, event(), model="mock")
        original = candidate(store, corp)
        preferred = store.save(original, event(), model="mock")
        rows = store.intelligence()
        assert len(rows) == 2
        assert all(r["preferred_id"] == preferred for r in rows)
        assert all(len(r["related_sources"]) == 2 for r in rows)
        assert len({r["candidate_id"] for r in rows}) == 2
    assert canonical_url("https://example.org/story?utm_source=a#top") == canonical_url(
        "https://example.org/story"
    )
    assert canonical_url("https://example.org/rss?q=expansion") != canonical_url(
        "https://example.org/rss?q=shutdown"
    )


def test_failure_does_not_fake_success_and_unchanged_polls_respect_schedule(tmp_path):
    db, sources = fixtures(tmp_path)
    source = sources[0]
    assert observe_result(db, result_for(source)).ok
    assert not observe_result(db, result_for(source, status=503)).ok
    with AluminiumStore(db, read_only=True) as store:
        row = next(r for r in store.status() if r["source_id"] == source_id(source.url))
        assert row["last_success_at"] and row["last_attempt"]["successful"] == 0
        assert "503" in row["last_attempt"]["error"]
    # 真归档表存在但相同内容不新增 Raw：轮询记录仍阻止立即重抓。
    with open_archive(tmp_path):
        pass
    last = SqliteLastCollectionSource(db).last_collected_at()
    ch = channel_for(source, enabled=True)
    assert not due_channels([ch], last, datetime.now(UTC))[0].due
    assert due_channels([ch], last, datetime.now(UTC) + timedelta(hours=7))[0].due


def test_structure_error_is_failure_not_success(tmp_path):
    db, sources = fixtures(tmp_path)
    src = next(s for s in sources if s.mode == "rss")
    result = observe_result(db, result_for(src, HTML))
    assert not result.ok and "结构" in result.failure.reason
    with AluminiumStore(db, read_only=True) as store:
        assert not any(r["last_success_at"] for r in store.status())


def test_probe_does_not_activate_constructed_directory_or_paid_without_evidence(
    tmp_path,
):
    db, _ = fixtures(tmp_path)
    deps, calls = dependencies()
    report = probe_sources(db, deps, enable_verified=True)
    assert len(report) == len(calls) == 37
    assert sum(r["collectible"] for r in report) == 26
    with open_store(db, author="check") as registry:
        assert sum(c.enabled for c in registry.current.channels) == 26
        assert all(not c.enabled for c in registry.current.channels if c.type == "rss")
    assert all(
        not r["collectible"] for r in report if r["input_status"] == "domain-only"
    )


def test_ui_read_only_toggle_validation_and_csrf(tmp_path):
    from atlas.webapp import build_webapp

    db, sources = fixtures(tmp_path)
    service = SourceManagement(db, lock_path=tmp_path / "lock")
    before = db.read_bytes()
    page = service.render()
    assert "海外电解铝" in page and "需要授权" in page and "最近成功" in page
    assert before == db.read_bytes()
    sid = source_id(sources[0].url)
    with pytest.raises(ValueError, match="验证"):
        service.toggle(sid, "1", "2", "tester")
    with AluminiumStore(db) as store:
        store.probe(sid, {"collectible": True, "reason": None})
    service.toggle(sid, "1", "2", "tester")
    with open_store(db, author="check") as registry:
        assert next(c for c in registry.current.channels if c.id == sid).enabled
        version = str(registry.current_version)
    service.toggle(sid, "0", version, "tester")
    raw = RawRecord.create(
        channel_id=sid,
        endpoint=sources[0].url,
        content=HTML,
        fetched_at=datetime.now(UTC),
        http_status=200,
    )
    with open_archive(tmp_path) as archive:
        archive.put(raw, HTML)
    with build_webapp(
        tmp_path,
        db_path=db,
        industry_provider=lambda: (INDUSTRY_ID,),
        require_login=False,
    ) as app:
        session = requests.Session()
        session.trust_env = False
        response = session.get(app.base_url + "/sources")
        assert response.status_code == 200 and "37 个来源" in response.text
        response = session.post(
            app.base_url + "/sources/toggle",
            data={"source_id": sid, "enabled": "1", "version": "4"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
    with build_webapp(
        tmp_path,
        db_path=db,
        industry_provider=lambda: (INDUSTRY_ID,),
        require_login=True,
    ) as app:
        session = requests.Session()
        session.trust_env = False
        response = session.get(app.base_url + "/sources", allow_redirects=False)
        assert (
            response.status_code == 303
            and "return_to=%2Fsources" in response.headers["Location"]
        )
        response = session.post(
            app.base_url + "/login",
            data={"name": "tester", "return_to": "/sources"},
            allow_redirects=False,
        )
        assert (
            response.status_code == 303 and response.headers["Location"] == "/sources"
        )
        assert session.get(app.base_url + "/sources").status_code == 200


def test_llm_response_validated_no_tools_or_truncation():
    cfg = SimpleNamespace(
        model="mock", api_key="test", base_url="https://mock.invalid", timeout_seconds=5
    )
    choice = {
        "finish_reason": "stop",
        "message": {
            "content": json.dumps({"events": [event().model_dump(mode="json")]})
        },
    }
    calls = []

    def post(url, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=200, json=lambda: {"choices": [choice]})

    client = IntelligenceClient(cfg, post=post)
    assert client.extract("Original English title", TEXT)[0].event_type == "FIRST_METAL"
    assert "untrusted_body" in calls[0]["json"]["messages"][1]["content"]
    session = calls[0]["headers"]["x-opencode-session"]
    assert session.startswith("atlas-aluminium-")
    assert client.extract("Original English title", TEXT)[0].event_type == "FIRST_METAL"
    assert calls[-1]["headers"]["x-opencode-session"] == session
    choice["finish_reason"] = "length"
    with pytest.raises(ValueError, match="截断"):
        client.extract("Title", TEXT)
    choice["finish_reason"] = "stop"
    choice["message"]["tool_calls"] = [{}]
    with pytest.raises(ValueError, match="工具"):
        client.extract("Title", TEXT)


def test_archive_to_candidates_to_llm_to_export_end_to_end(tmp_path):
    db, sources = fixtures(tmp_path)
    src = next(s for s in sources if s.mode == "rss")
    raw = RawRecord.create(
        channel_id=source_id(src.url),
        endpoint=src.url,
        content=RSS,
        fetched_at=datetime.now(UTC),
        http_status=200,
    )
    with open_archive(tmp_path) as archive:
        archive.put(raw, RSS)
    first = process_archive(tmp_path)
    assert first["candidates"] == 1

    class MockClient:
        config = SimpleNamespace(model="mock")

        def extract(self, title, body):
            assert "500 ktpa" in body
            return [event()]

    output = process_archive(tmp_path, extract=True, client=MockClient())
    assert output["extracted"] == 1 and output["failed"] == 0
    assert (
        process_archive(tmp_path, extract=True, client=MockClient())["extracted"] == 0
    )
    with AluminiumStore(db, read_only=True) as store:
        rows = store.intelligence()
        assert (
            len(rows) == 1
            and rows[0]["original_title"] == "Primary aluminium smelter first metal"
        )
        assert rows[0]["data"]["title_zh"] == "某铝厂首次出铝"
        assert rows[0]["raw_id"] == raw.raw_id


def test_multi_event_failure_rolls_back_entire_announcement(tmp_path):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0])
        with pytest.raises(ValueError, match="单位转换"):
            store.save_events(
                c, [event(), event(announced_capacity_tpy=900000)], model="mock"
            )
        assert store.intelligence() == []
        assert store.pending()[0]["candidate_id"] == c["candidate_id"]
        store.save_events(c, [event()], model="mock")
        assert len(store.intelligence()) == 1
        assert (
            store.db.execute("SELECT body FROM aluminium_inputs").fetchone()[0] == TEXT
        )


def test_company_short_title_is_candidate_but_unknown_source_is_not(tmp_path):
    from atlas.aluminium.processing import is_candidate

    _, sources = fixtures(tmp_path)
    company = next(s for s in sources if s.type == "company")
    aggregator = next(s for s in sources if s.type == "news aggregation")
    assert is_candidate(company, "Al Taweelah smelter restart")
    assert not is_candidate(aggregator, "Copper smelter restart")


def test_collect_pipeline_records_real_attempt_and_archive(tmp_path):
    from dataclasses import replace

    from atlas.compose.pipeline import Pipeline, PipelineConfig
    from atlas.registry.versioning import RegistryMutation

    db, sources = fixtures(tmp_path)
    src = sources[0]
    ch = channel_for(src, enabled=True)
    with open_store(db, author="test") as registry:
        registry.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="channel",
                    object_id=ch.id,
                    payload=ch.payload(),
                )
            ]
        )
    deps, calls = dependencies()
    deps = replace(deps, collection_observer=lambda r: observe_result(db, r))
    with Pipeline(
        PipelineConfig(store_root=tmp_path, actor="test"), dependencies=deps
    ) as pipeline:
        pipeline.run(targets=("archive",))
    assert calls == [src.url]
    with open_archive(tmp_path) as archive:
        assert len(archive.all_raw_ids()) == 1
    with AluminiumStore(db, read_only=True) as store:
        assert sum(bool(r["last_success_at"]) for r in store.status()) == 1


def test_model_failure_retained_and_retry_is_explicit(tmp_path):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        candidate(store, sources[0])

    class BrokenClient:
        config = SimpleNamespace(model="mock")

        def extract(self, *args):
            raise RuntimeError("HTTP 429")

    result = process_archive(tmp_path, extract=True, client=BrokenClient())
    assert result["failed"] == 1 and result["extracted"] == 0
    with AluminiumStore(db, read_only=True) as store:
        assert not store.pending() and len(store.pending(retry_errors=True)) == 1
        assert (
            "429"
            in store.db.execute(
                "SELECT error FROM aluminium_processing_errors"
            ).fetchone()[0]
        )


def test_article_fetch_retains_original_response_and_obeys_channel_rate(
    tmp_path, monkeypatch
):
    from atlas.archive import read_article_body, read_article_response
    from atlas.collect.article import ArticleContent
    from atlas.collect.network_policy import SharedNetworkPolicy

    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0], body="Primary aluminium smelter first metal")
    calls = []

    class MockReader:
        def read(self, url):
            calls.append(url)
            policy = SharedNetworkPolicy(tmp_path)
            assert (
                max(
                    r["seconds"]
                    for r in policy._read(url.split("/")[2])["rates"].values()
                )
                == 5
            )
            return ArticleContent(
                text=TEXT, url=url, raw_content=HTML, content_type="text/html"
            )

    class MockClient:
        config = SimpleNamespace(model="mock")

        def extract(self, title, body):
            assert body == TEXT
            return [event()]

    result = process_archive(
        tmp_path,
        extract=True,
        fetch_articles=True,
        client=MockClient(),
        reader=MockReader(),
    )
    assert (
        result["extracted"] == 1
        and result["failed"] == 0
        and calls == [c["source_url"]]
    )
    body = read_article_body(db, c["source_url"])
    assert body.cleaned_text == TEXT
    assert read_article_response(db, body.id)[0] == HTML
    with AluminiumStore(db) as store:
        candidate(store, sources[0], url=c["source_url"] + "/second")
    from atlas.collect.throttle import ThrottleTimeout

    def busy(*args):
        raise ThrottleTimeout("example.org", min_interval=5, needed=5, max_wait=0)

    monkeypatch.setattr(SharedNetworkPolicy, "set_rate", busy)
    result = process_archive(
        tmp_path,
        extract=True,
        fetch_articles=True,
        client=MockClient(),
        reader=MockReader(),
    )
    assert result["failed"] == 1 and result["extracted"] == 0
    with AluminiumStore(db, read_only=True) as store:
        assert (
            "ThrottleTimeout"
            in store.db.execute(
                "SELECT error FROM aluminium_processing_errors ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )


def test_no_event_is_completed_without_becoming_an_error(tmp_path):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0])

    class NoEventClient:
        config = SimpleNamespace(model="mock")

        def extract(self, title, body):
            return []

    result = process_archive(tmp_path, extract=True, client=NoEventClient())
    assert result["processed"] == result["no_event"] == 1
    assert result["failed"] == result["pending"] == 0
    assert (
        process_archive(
            tmp_path, extract=True, retry_errors=True, client=NoEventClient()
        )["processed"]
        == 0
    )
    with AluminiumStore(db, read_only=True) as store:
        row = store.db.execute(
            "SELECT candidate_id,status,model,event_count FROM aluminium_processing_results"
        ).fetchone()
        assert (
            row["candidate_id"] == c["candidate_id"]
            and row["status"] == "no_event"
            and row["event_count"] == 0
        )
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM aluminium_processing_errors"
            ).fetchone()[0]
            == 0
        )


def test_processing_failure_automatically_retries_after_cooldown_and_keeps_history(
    tmp_path,
):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0])
        store.error(c["candidate_id"], "HTTP 429")
        store.processing_result(
            c, status="failed", model="mock", reason="HTTP 429", retry_seconds=3600
        )
        assert not store.pending() and len(store.pending(retry_errors=True)) == 1
        store.processing_result(
            c,
            status="failed",
            model="mock",
            reason="transient failure",
            retry_seconds=0,
        )
        assert len(store.pending()) == 1
        store.processing_result(c, status="no_event", model="mock", reason="irrelevant")
        assert not store.pending(retry_errors=True)
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM aluminium_processing_results"
            ).fetchone()[0]
            == 3
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.db.execute("DELETE FROM aluminium_processing_results")


def test_partial_dates_cannot_be_filled_with_month_or_quarter_end():
    from datetime import date

    from atlas.aluminium.models import explicit_date_in_quote

    assert explicit_date_in_quote(
        date(2026, 10, 21), "On 21 October 2026, cells restarted."
    )
    assert explicit_date_in_quote(
        date(2026, 10, 21), "On October 21, 2026, cells restarted."
    )
    assert not explicit_date_in_quote(
        date(2027, 3, 31), "Full production during Q1 2027."
    )
    with pytest.raises(ValidationError, match="不能补日"):
        event(
            expected_full_capacity_date="2027-02-28",
            evidence=TEXT + " Full capacity by the end of February 2027.",
        )
    assert event(expected_full_capacity_date=None).expected_full_capacity_date is None


def test_capacity_raw_suffix_and_explicit_annual_context(tmp_path):
    assert capacity_tpy("1.5m", "tonnes per year") == 1500000
    assert capacity_tpy("75k", "tpy") == 75000
    with pytest.raises(ValueError):
        capacity_tpy("1.5m", "mtpa")
    db, sources = fixtures(tmp_path)
    body = "Nameplate capacity: 1.5m tonnes per year."
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0], body=body)
        store.save(
            c,
            event(
                event_date=None,
                announced_capacity_tpy=1500000,
                evidence=body,
                numeric_evidence={
                    "announced_capacity_tpy": {
                        "raw_value": "1.5m",
                        "unit": "tonnes per year",
                        "quote": body,
                    }
                },
            ),
            model="mock",
        )
        annual = "The annual production capacity is 75,000 tonnes."
        c2 = candidate(store, sources[0], url=c["source_url"] + "/annual", body=annual)
        store.save(
            c2,
            event(
                event_date=None,
                announced_capacity_tpy=75000,
                evidence=annual,
                numeric_evidence={
                    "announced_capacity_tpy": {
                        "raw_value": "75,000",
                        "unit": "tonnes",
                        "quote": annual,
                    }
                },
            ),
            model="mock",
        )
        no_year = "The production capacity is 75,000 tonnes."
        c3 = candidate(
            store, sources[0], url=c["source_url"] + "/unknown", body=no_year
        )
        with pytest.raises(ValueError, match="年产能单位"):
            store.save(
                c3,
                event(
                    event_date=None,
                    announced_capacity_tpy=75000,
                    evidence=no_year,
                    numeric_evidence={
                        "announced_capacity_tpy": {
                            "raw_value": "75,000",
                            "unit": "tonnes",
                            "quote": no_year,
                        }
                    },
                ),
                model="mock",
            )


def test_same_candidate_model_correction_preserves_old_record(tmp_path):
    db, sources = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        c = candidate(store, sources[0])
        before = store.save(c, event(), model="mock/1")
        after = store.save(c, event(event_date=None), model="mock/2")
        rows = {r["id"]: r for r in store.intelligence()}
        assert rows[before]["is_superseded"] and rows[after]["supersedes"] == before


def test_listing_snippet_never_includes_another_article(tmp_path):
    from atlas.aluminium.sources import page_documents

    source = load_manifest(MANIFEST).sources[0]
    html = (
        '<html><title>News</title><body><main><div><a href="/news/first">Primary aluminium smelter restart and capacity update</a><a href="/news/second">Other unrelated company news about recycling metal</a>'
        + ("filler " * 25)
        + "</div></main></body></html>"
    ).encode()
    _, _, links = page_documents(html, source.url)
    assert len(links) == 2
    assert "recycling" not in links[0]["body"]
    assert "restart" not in links[1]["body"]
    db, _ = fixtures(tmp_path)
    with AluminiumStore(db) as store:
        first = store.candidate(
            source_id(source.url),
            "raw-one",
            source.url + "/news/first",
            links[0]["title"],
            "legacy full listing text",
        )
        again = store.candidate(
            source_id(source.url),
            "raw-one",
            source.url + "/news/first",
            links[0]["title"],
            links[0]["body"],
        )
        assert first == again


def test_monitor_snapshot_uses_new_bytes_instead_of_stale_article_cache(tmp_path):
    from contextlib import closing

    from atlas.archive import ArticleContentStore

    db, sources = fixtures(tmp_path)
    source = sources[0]
    updated = HTML.replace(b"500 ktpa", b"600 ktpa")
    raw = RawRecord.create(
        channel_id=source_id(source.url),
        endpoint=source.url,
        content=updated,
        fetched_at=datetime.now(UTC),
        http_status=200,
    )
    with open_archive(tmp_path) as archive:
        archive.put(raw, updated)
    with closing(ArticleContentStore(db)) as bodies:
        bodies.record(
            source_url=source.url,
            status="ready",
            cleaned_text=TEXT,
            cleaner_version="mock",
            content_kind="article",
            raw_content=HTML,
        )

    class FreshClient:
        config = SimpleNamespace(model="mock")

        def extract(self, title, body):
            assert "600 ktpa" in body and "500 ktpa" not in body
            return []

    assert (
        process_archive(tmp_path, extract=True, client=FreshClient())["no_event"] == 1
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_collection_script_runs_extraction_only_when_enabled(tmp_path, enabled):
    import os
    import shutil
    import subprocess

    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv-new/bin").mkdir(parents=True)
    (root / "bin").mkdir()
    script = root / "scripts/collect-due.sh"
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "scripts/collect-due.sh", script
    )
    fake_python = root / ".venv-new/bin/python"
    fake_python.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ATLAS_TEST_CALLS"\nexit 0\n'
    )
    fake_python.chmod(0o755)
    systemctl = root / "bin/systemctl"
    systemctl.write_text("#!/bin/sh\nexit 1\n")
    systemctl.chmod(0o755)
    calls = tmp_path / "calls.txt"
    env = {
        **os.environ,
        "PATH": str(root / "bin") + ":" + os.defpath,
        "ATLAS_TEST_CALLS": str(calls),
        "ATLAS_ALUMINIUM_EXTRACT": "1" if enabled else "0",
    }
    subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, check=True, timeout=10
    )
    line = next(
        line
        for line in calls.read_text().splitlines()
        if "atlas.aluminium process" in line
    )
    assert ("--extract --fetch-articles" in line) == enabled
