import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from atlas.archive import open_archive, read_article_contents
from atlas.cognition.briefs import read_articles
from atlas.collect import FetchResult
from atlas.collect.article import clean_document
from atlas.contracts import RawRecord
from atlas.ingest import collect_due
from atlas.source_index import FILENAME, read_sources, refresh_sources
from atlas.webapp import ArchiveEntriesAccess, store_summary
from tests.test_compose_pipeline import FakeFetcher, _dependencies, _register_channel

URL = "https://example.test/feed"
BODY = b"<rss><channel><item><title>Research result</title><link>https://example.test/post</link><description>A useful research result with detailed methods and evidence.</description></item></channel></rss>"
NOW = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)


def put(root, body=BODY, channel="test", endpoint=URL):
    record = RawRecord.create(
        channel_id=channel,
        endpoint=endpoint,
        content=body,
        fetched_at=NOW,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, body)
    return record


def setup_channel(root):
    _register_channel(root, industry_id="science", channel_id="test", endpoint=URL)
    return _dependencies(FakeFetcher({URL: BODY}))


def test_daily_collection_ignores_legacy_history_and_checks_unchanged_content(
    tmp_path, monkeypatch
):
    deps = setup_channel(tmp_path)
    (tmp_path / "runs").mkdir()
    legacy = tmp_path / "runs/executions.json"
    legacy.write_text("not JSON; daily collection must never open this file")
    first = collect_due(tmp_path, dependencies=deps, now=NOW)
    assert first["new"] == 1 and first["failed"] == 0
    assert len(deps.fetcher.calls) == 1
    assert len(read_sources(tmp_path)) == 1

    def no_parse(*args, **kwargs):
        pytest.fail("unchanged responses must not be parsed/normalized again")

    monkeypatch.setattr(ArchiveEntriesAccess, "_analyse", no_parse)
    monkeypatch.setattr("atlas.ingest.NormalizeStage.run", no_parse)
    assert (
        collect_due(tmp_path, dependencies=deps, now=NOW + timedelta(minutes=5))[
            "checked"
        ]
        == 0
    )
    assert (
        collect_due(tmp_path, dependencies=deps, now=NOW + timedelta(hours=1))[
            "unchanged"
        ]
        == 1
    )
    assert (
        collect_due(
            tmp_path, dependencies=deps, now=NOW + timedelta(hours=1, minutes=5)
        )["checked"]
        == 0
    )
    assert len(deps.fetcher.calls) == 2
    with open_archive(tmp_path) as archive:
        assert len(archive.all_raw_ids()) == 1
        assert archive.get(archive.all_raw_ids()[0]).fetched_at == NOW
    with sqlite3.connect(tmp_path / "ingest.sqlite") as db:
        assert db.execute("SELECT count(*) FROM polls").fetchone()[0] == 1
        assert (
            db.execute("SELECT last_success_at FROM polls").fetchone()[0]
            == (NOW + timedelta(hours=1)).timestamp()
        )
    assert legacy.read_text().startswith("not JSON")


def test_changed_content_is_new_and_processing_failure_does_not_advance_checkpoint(
    tmp_path, monkeypatch
):
    deps = setup_channel(tmp_path)
    collect_due(tmp_path, dependencies=deps, now=NOW)
    deps.fetcher._responses[URL] = FetchResult(
        url=URL, status_code=200, content=BODY.replace(b"result", b"update")
    )
    original = ArchiveEntriesAccess.entries_of
    monkeypatch.setattr(
        ArchiveEntriesAccess,
        "entries_of",
        lambda *a: (_ for _ in ()).throw(RuntimeError("index interrupted")),
    )
    with pytest.raises(RuntimeError, match="index interrupted"):
        collect_due(tmp_path, dependencies=deps, now=NOW + timedelta(hours=1))
    with sqlite3.connect(tmp_path / "ingest.sqlite") as db:
        assert (
            db.execute("SELECT last_success_at FROM polls").fetchone()[0]
            == NOW.timestamp()
        )
    monkeypatch.setattr(ArchiveEntriesAccess, "entries_of", original)
    assert (
        collect_due(tmp_path, dependencies=deps, now=NOW + timedelta(hours=1))[
            "unchanged"
        ]
        == 1
    )
    assert len(read_sources(tmp_path)) == 2


def test_source_index_reuses_parses_keeps_raw_locators_and_reads_bounded_bodies(
    tmp_path, monkeypatch
):
    first = put(tmp_path)
    second = put(tmp_path, BODY.replace(b"<channel>", b"<channel>\n"))
    assert refresh_sources(tmp_path) == 2
    light = read_sources(tmp_path)
    assert len(light) == 1 and next(iter(light.values())).body == ""
    assert next(
        iter(read_sources(tmp_path, keys=light, include_body=True).values())
    ).body
    assert read_sources(tmp_path, keys=[]) == {}
    monkeypatch.setattr(
        ArchiveEntriesAccess, "_analyse", lambda *a: pytest.fail("history reparsed")
    )
    assert refresh_sources(tmp_path) == 0
    with open_archive(tmp_path) as archive:
        access = ArchiveEntriesAccess(archive)
        a = access.browse_record(first)
        b = access.browse_record(second)
        assert a.view.entries[0].entry_id != b.view.entries[0].entry_id
        assert next(iter(a.sources.values())).key == next(iter(b.sources.values())).key


def test_source_index_transaction_recovers_failed_parse(tmp_path, monkeypatch):
    put(tmp_path)
    original = ArchiveEntriesAccess.brief_sources
    monkeypatch.setattr(
        ArchiveEntriesAccess,
        "brief_sources",
        lambda *a: (_ for _ in ()).throw(ValueError("parse failed")),
    )
    with pytest.raises(ValueError, match="parse failed"):
        refresh_sources(tmp_path)
    with sqlite3.connect(tmp_path / FILENAME) as db:
        assert db.execute("SELECT count(*) FROM indexed_raws").fetchone()[0] == 0
    monkeypatch.setattr(ArchiveEntriesAccess, "brief_sources", original)
    assert refresh_sources(tmp_path) == 1


def test_pdf_limit_is_saved_as_nonretryable_and_not_refetched(tmp_path, monkeypatch):
    from pypdf.errors import LimitReachedError

    from atlas.article_content_cli import main

    put(
        tmp_path,
        BODY.replace(b"https://example.test/post", b"https://example.test/problem.pdf"),
    )
    calls = []

    def parse(*args):
        raise LimitReachedError("Reached maximum number of tokens per stream.")

    monkeypatch.setattr("pypdf.PdfReader", parse)

    def read(self, url):
        calls.append(url)
        return clean_document(b"%PDF-bad", "application/pdf")

    monkeypatch.setattr("atlas.article_content_cli.ArticleReader.read", read)
    args = [
        "--store-root",
        str(tmp_path),
        "--report-dir",
        str(tmp_path / "report"),
        "--workers",
        "1",
        "--retry-transient",
    ]
    assert main(args) == 0 and len(calls) == 1
    row = read_article_contents(tmp_path / "atlas.db")[calls[0]]
    assert row.failure_code == "pdf_extraction_failed" and not row.retryable
    assert main(args) == 0 and len(calls) == 1
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda *a: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda: "Valid document text")]
        ),
    )
    assert clean_document(b"%PDF-valid").text == "Valid document text"


def test_briefs_cooldown_skips_index_and_selective_article_read(tmp_path, monkeypatch):
    from atlas.briefs_cli import main
    from atlas.cognition.briefs import BriefStore, brief_source

    put(tmp_path)
    source = brief_source("hello", "body", url="https://example.test/post")
    store = BriefStore(tmp_path / "atlas.db")
    store.ensure_sources([source])
    store.close()
    assert read_articles(tmp_path / "atlas.db", source_keys=[]) == {}
    assert (
        read_articles(tmp_path / "atlas.db", source_keys=[source.key])[
            source.key
        ].article_text
        == "body"
    )
    report = tmp_path / "briefs"
    report.mkdir()
    (report / "retry-after.json").write_text(json.dumps({"until": 9999999999}))
    monkeypatch.setattr(
        "atlas.source_index.refresh_sources",
        lambda *a: pytest.fail("cooldown scanned history"),
    )
    assert (
        main(
            [
                "--store-root",
                str(tmp_path),
                "--report-dir",
                str(report),
                "--failure-cooldown",
                "900",
            ]
        )
        == 0
    )


def test_browse_memory_is_bounded_and_eviction_uses_disk_index(tmp_path, monkeypatch):
    record = put(tmp_path)
    refresh_sources(tmp_path)
    with open_archive(tmp_path) as archive:
        access = ArchiveEntriesAccess(archive)
        first = access.browse_record(record)
        for number in range(200):
            access._remember_browse(str(number), first)
        assert len(access._browse_cache) == 128
        assert record.raw_id not in access._browse_cache
        monkeypatch.setattr(
            access, "_analyse", lambda *a: pytest.fail("eviction reparsed raw")
        )
        assert access.browse_record(record) == first
        assert len(access._browse_cache) == 128


def test_idle_vector_update_does_not_load_or_validate_all_vectors(
    tmp_path, monkeypatch
):
    from atlas import brief_search_cli as search
    from tests.test_brief_search import Encoder, seeded
    from tests.test_brief_search import put as put_brief

    source, target = seeded(tmp_path)
    search.build_index(source, target, encoder=Encoder())
    validate = search.validate_vector
    monkeypatch.setattr(
        search,
        "validate_vector",
        lambda *a: pytest.fail("idle update loaded embeddings"),
    )
    assert (
        search.build_index(source, target, encoder=Encoder(), incremental=True)[
            "updated"
        ]
        is False
    )
    monkeypatch.setattr(search, "validate_vector", validate)
    put_brief(source, "new", "新增文章", "新的中文简介。")
    assert (
        search.build_index(source, target, encoder=Encoder(), incremental=True)[
            "encoded_count"
        ]
        == 1
    )


def test_startup_summary_uses_index_without_reparsing_historical_bodies(tmp_path, monkeypatch):
    put(tmp_path)
    put(tmp_path, b"A whole article without a feed container.")
    expected = store_summary(tmp_path)
    assert (expected.records, expected.containers, expected.derived_entries, expected.direct_entries) == (2, 1, 1, 1)
    assert refresh_sources(tmp_path) == 2
    monkeypatch.setattr(
        ArchiveEntriesAccess, "_analyse",
        lambda *a: pytest.fail("startup reparsed an indexed historical response"),
    )
    assert store_summary(tmp_path) == expected


def test_failed_poll_has_backoff_without_changing_last_success(tmp_path):
    deps = setup_channel(tmp_path)
    collect_due(tmp_path, dependencies=deps, now=NOW)
    deps.fetcher._responses[URL] = FetchResult(
        url=URL, status_code=503, content=b"busy"
    )
    after = NOW + timedelta(hours=1)
    assert collect_due(tmp_path, dependencies=deps, now=after)["failed"] == 1
    assert (
        collect_due(tmp_path, dependencies=deps, now=after + timedelta(seconds=30))[
            "checked"
        ]
        == 0
    )
    with sqlite3.connect(tmp_path / "ingest.sqlite") as db:
        assert db.execute("SELECT last_success_at,failures FROM polls").fetchone() == (
            NOW.timestamp(),
            1,
        )
    deps.fetcher._responses[URL] = FetchResult(url=URL, status_code=200, content=BODY)
    assert (
        collect_due(tmp_path, dependencies=deps, now=after + timedelta(minutes=1))[
            "unchanged"
        ]
        == 1
    )
