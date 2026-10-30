import fcntl
import hashlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from bs4 import BeautifulSoup

from atlas.archive import ArticleContentStore, open_archive
from atlas.briefs_cli import main
from atlas.cognition import ArticleBrief, BriefClient, BriefStore, read_articles
from atlas.contracts import RawRecord
from atlas.feed import ArchiveFeedSource, FeedQuery, run_query
from atlas.registry import (
    Channel,
    FetchSpec,
    Industry,
    SqliteConfigStore,
    read_subscription_sources,
)
from atlas.webapp import ArchiveEntriesAccess, build_webapp
from tests.test_cognition_support import fake_config
from tests.test_webui_entries_render import _get

BODY = (
    "A real article explaining the design, methods and evaluation of a software system. "
    * 5
)


def put(root, title="Article One", url="https://example.test/one"):
    payload = f"<rss><channel><item><title>{title}</title><link>{url}</link><description>{BODY}</description></item></channel></rss>".encode()
    r = RawRecord.create(
        channel_id="feed",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(r, payload)
    return r


def test_analysis_cache_reuses_recent_records_and_evicts_only_oldest(tmp_path, monkeypatch):
    root = tmp_path / "store"
    records = [put(root, f"Article {i}", f"https://example.test/{i}") for i in range(3)]
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive, cache_size=2)
        calls = Counter()
        original = access.content

        def counted(raw_id):
            calls[raw_id] += 1
            return original(raw_id)

        monkeypatch.setattr(access, "content", counted)
        first = access.brief_sources(records[0].raw_id)
        access.brief_sources(records[1].raw_id)
        assert access.brief_sources(records[0].raw_id) == first
        access.brief_sources(records[2].raw_id)
        assert access.brief_sources(records[0].raw_id) == first
        assert calls == {records[0].raw_id: 1, records[1].raw_id: 1, records[2].raw_id: 1}
        access.brief_sources(records[1].raw_id)
        assert calls[records[1].raw_id] == 2


def test_analysis_cache_is_safe_for_concurrent_requests(tmp_path):
    root = tmp_path / "store"
    records = [put(root, f"Article {i}", f"https://example.test/{i}") for i in range(6)]
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive, cache_size=2)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda record: access.brief_sources(record.raw_id), records * 8))
        assert all(len(result) == 1 for result in results)
        assert len(access._cache) <= 2
        assert set(access._brief_sources) <= set(access._cache)


def test_source_filter_precedes_expensive_archive_record_classification(tmp_path):
    root = tmp_path / "store"
    first = put(root)
    payload = b"Another article"
    second = RawRecord.create(channel_id="other", endpoint="https://example.test/other", content=payload, http_status=200)
    with open_archive(root) as archive:
        archive.put(second, payload)
        access = ArchiveEntriesAccess(archive)
        classified = []

        def kind(raw_id):
            classified.append(raw_id)
            return "feed" if access.is_container(raw_id) else None

        source = ArchiveFeedSource(archive, entry_kind_of=kind)
        result = run_query(source, FeedQuery(channels=("feed",), granularity="entry"), entries_of=access.entries_of)
        assert [entry.title for entry in result.items] == ["Article One"]
        assert result.channels == ("feed", "other")
        assert classified == [first.raw_id]
        all_results = run_query(source, FeedQuery(granularity="entry"), entries_of=access.entries_of)
        assert len(all_results.items) == 2
        assert second.raw_id in classified


def install_fake_model(monkeypatch, calls):
    monkeypatch.setattr(
        "atlas.briefs_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )

    def generate(self, sources):
        calls.append([s.title for s in sources])
        return {
            s.key: ArticleBrief(
                "中文译题：" + s.title, "文章介绍软件系统的设计方法及评估。"
            )
            for s in sources
        }, {}

    monkeypatch.setattr(BriefClient, "generate", generate)


@pytest.mark.parametrize(
    "bad",
    [
        b"<!doctype html><html><head><title>Site home</title></head><body><p>Latest homepage news.</p></body></html>",
        b"<response><message>Not a feed</message></response>",
    ],
)
def test_registered_subscription_error_is_not_an_article_and_true_article_survives(
    tmp_path, bad
):
    root = tmp_path / "store"
    put(root)
    store = SqliteConfigStore(
        db_path=root / "atlas.db",
        author="test",
        industries=(Industry(id="ai", name="AI", enabled=True),),
        channels=(
            Channel(
                id="feed",
                industry_id="ai",
                type="rss",
                endpoint="https://example.test/feed",
                fetch_spec=FetchSpec(type="rss"),
                interval_seconds=3600,
                enabled=True,
            ),
            Channel(
                id="broken",
                industry_id="ai",
                type="rss",
                endpoint="https://example.test/broken-feed",
                fetch_spec=FetchSpec(type="rss"),
                interval_seconds=3600,
                enabled=True,
            ),
        ),
    )
    store.close()
    with open_archive(root) as archive:
        broken = RawRecord.create(
            channel_id="broken",
            endpoint="https://example.test/broken-feed",
            content=bad,
            http_status=200,
        )
        archive.put(broken, bad)
        article = b"<!doctype html><html><article>Genuine saved article body.</article></html>"
        direct = RawRecord.create(
            channel_id="broken",
            endpoint="https://example.test/real-article",
            content=article,
            http_status=200,
        )
        archive.put(direct, article)
        access = ArchiveEntriesAccess(archive)
        assert access.entries_of(broken).unavailable_reason
        assert access.brief_sources(broken.raw_id) == {}
        assert not access.entries_of(direct).unavailable_reason
    app = build_webapp(
        root,
        require_login=False,
        db_path=root / "atlas.db",
        industry_provider=lambda: ("ai",),
        industry_of={"feed": "ai", "broken": "ai"},
    )
    try:
        digest = hashlib.sha256((root / "atlas.db").read_bytes()).digest()
        status, page, _ = _get(app, "/feed?limit=200")
        soup = BeautifulSoup(page, "html.parser")
        assert status == 200 and len(soup.select("article.entry")) == 2
        assert "Article One" in page
        assert not soup.select(".feed-notices")
        assert not soup.select(f"#item-{broken.raw_id}")
        assert soup.select(f"#item-{direct.raw_id}")
        assert hashlib.sha256((root / "atlas.db").read_bytes()).digest() == digest
        status, document, _ = _get(app, "/feed?granularity=document&limit=200")
        assert status == 200 and broken.raw_id in document
    finally:
        app.close()


def test_subscription_registry_readview_does_not_create_or_change_files(tmp_path):
    path = tmp_path / "missing.db"
    assert read_subscription_sources(path) == frozenset() and not path.exists()


def test_new_collection_gets_generated_without_regenerating_old_articles(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    put(root)
    calls = []
    install_fake_model(monkeypatch, calls)
    args = ["--store-root", str(root), "--report-dir", str(tmp_path / "report")]
    assert main(args) == 0
    previous = read_articles(root / "atlas.db")
    put(root, "Article Two", "https://example.test/two")
    assert main(args) == 0
    current = read_articles(root / "atlas.db")
    assert calls == [["Article One"], ["Article Two"]]
    assert all(
        current[k].title_zh == row.title_zh and current[k].summary_zh == row.summary_zh
        for k, row in previous.items()
    )
    assert main(args) == 0 and len(calls) == 2


def test_model_job_lock_avoids_duplicate_calls_and_releases_cleanly(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    put(root)
    calls = []
    install_fake_model(monkeypatch, calls)
    args = ["--store-root", str(root), "--report-dir", str(tmp_path / "report")]
    with (root / ".briefs.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main(args) == 0 and calls == []
    assert main(args) == 0 and len(calls) == 1


def test_provider_failure_cooldown_prevents_repeated_paid_requests(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    put(root)
    calls = []
    install_fake_model(monkeypatch, calls)

    def reject(self, sources):
        calls.append("rejected")
        raise RuntimeError("简介模型请求失败：HTTP 401")

    monkeypatch.setattr(BriefClient, "generate", reject)
    args = [
        "--store-root",
        str(root),
        "--report-dir",
        str(tmp_path / "report"),
        "--failure-cooldown",
        "900",
    ]
    assert main(args) == 1
    assert main(args) == 0 and calls == ["rejected"]
    (tmp_path / "report/retry-after.json").unlink()
    install_fake_model(monkeypatch, calls)
    assert main(args) == 0 and len(calls) == 2


def test_recovered_body_is_automatically_eligible_for_summary(tmp_path, monkeypatch):
    root = tmp_path / "store"
    record = put(root)
    with open_archive(root) as archive:
        source = next(
            iter(ArchiveEntriesAccess(archive).brief_sources(record.raw_id).values())
        )
    briefs = BriefStore(root / "atlas.db")
    briefs.ensure_sources([source])
    briefs.record_body(source, body="", error="原文 HTTP 403")
    briefs.close()
    bodies = ArticleContentStore(root / "atlas.db")
    bodies.record(
        source_url=source.url,
        status="ready",
        content_kind="article",
        cleaned_text=BODY,
        cleaner_version="test",
    )
    bodies.close()
    calls = []
    install_fake_model(monkeypatch, calls)
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )
    assert (
        read_articles(root / "atlas.db")[source.key].status == "ready"
        and len(calls) == 1
    )


def test_incremental_generation_retains_newly_fetched_clean_body_and_raw_response(
    tmp_path, monkeypatch
):
    from atlas.archive import read_article_contents
    from atlas.collect.article import ArticleContent

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>New original article</title><link>https://example.test/new-body</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="feed",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    raw = b"<article>" + BODY.encode() + b"</article>"
    monkeypatch.setattr(
        "atlas.briefs_cli.ArticleReader.read",
        lambda self, url: ArticleContent(BODY, url, raw, "text/html"),
    )
    calls = []
    install_fake_model(monkeypatch, calls)
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )
    saved = read_article_contents(root / "atlas.db", include_text=True)[
        "https://example.test/new-body"
    ]
    assert saved.cleaned_text == BODY and saved.status == "ready"
    store = ArticleContentStore(root / "atlas.db")
    try:
        assert store.raw(saved.id) == (raw, "text/html")
    finally:
        store.close()
