import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from atlas.admission_cli import main
from atlas.archive import open_archive
from atlas.briefs_cli import main as brief_main
from atlas.cognition import BriefClient
from atlas.cognition.admission import (
    AdmissionClient,
    AdmissionStore,
    Review,
    read_admissions,
    review_input,
)
from atlas.webapp import ArchiveEntriesAccess, build_webapp
from tests.test_cognition_support import fake_config
from tests.test_feed_refresh import put
from tests.test_webui_entries_render import _get


def source_of(root, record):
    with open_archive(root) as archive:
        return next(
            iter(ArchiveEntriesAccess(archive).brief_sources(record.raw_id).values())
        )


def fake_reviews(monkeypatch, calls, *, failure=False):
    monkeypatch.setattr(
        "atlas.admission_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )

    def review(self, sources, scope):
        calls.append([s.title for s in sources])
        if failure:
            raise RuntimeError("入站审核模型请求失败：HTTP 401")
        return {
            s.key: Review(
                "rejected" if s.title == "Buy now" else "accepted",
                "advertisement" if s.title == "Buy now" else "substantive",
                "纯推销" if s.title == "Buy now" else "有实质信息",
                review_input(s)["body"][:40],
            )
            for s in sources
        }, {}

    monkeypatch.setattr(AdmissionClient, "review", review)


def test_gate_filters_before_pagination_and_never_rewrites_raw_or_labels(tmp_path):
    root = tmp_path / "store"
    good = source_of(root, put(root, "Technical article"))
    bad = source_of(root, put(root, "Buy now", "https://example.test/buy"))
    put(root, "Pending article", "https://example.test/pending")
    db_path = root / "atlas.db"
    store = AdmissionStore(db_path)
    state = read_admissions(db_path)
    store.record(
        good, Review("accepted", "substantive", "技术文章"), policy=state.policy
    )
    store.record(
        bad,
        Review("rejected", "advertisement", "纯推销", bad.body[:40]),
        policy=state.policy,
    )
    store.enable()
    store.close()
    app = build_webapp(
        root,
        require_login=False,
        db_path=db_path,
        industry_provider=lambda: ("ai",),
        industry_of={"feed": "ai"},
    )
    try:
        db = sqlite3.connect(db_path)
        before = db.iterdump()
        snapshot = list(before)
        status, page, _ = _get(app, "/feed?limit=1")
        soup = BeautifulSoup(page, "html.parser")
        assert status == 200
        assert len(soup.select("article.entry")) == 1
        assert (
            "Technical article" in page
            and "Buy now" not in page
            and "Pending article" not in page
        )
        assert not soup.select(".feed-notices")
        assert not soup.select('a[aria-label="下一页"]')
        assert list(db.iterdump()) == snapshot
        db.close()
    finally:
        app.close()


def test_cli_quarantines_ads_and_briefs_only_accepted(tmp_path, monkeypatch):
    root = tmp_path / "store"
    good = source_of(root, put(root, "Technical article"))
    bad = source_of(root, put(root, "Buy now", "https://example.test/buy"))
    calls = []
    fake_reviews(monkeypatch, calls)
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "review"),
    ]
    assert main(args) == 0
    state = read_admissions(root / "atlas.db")
    assert (
        state.allows(good.key) and not state.allows(bad.key) and not state.allows("new")
    )
    assert main(args) == 0 and len(calls) == 1
    generated = []

    def fail(self, sources):
        generated.extend(s.key for s in sources)
        raise RuntimeError("stop after observing accepted inputs")

    monkeypatch.setattr(BriefClient, "generate", fail)
    assert (
        brief_main(["--store-root", str(root), "--report-dir", str(tmp_path / "brief")])
        == 1
    )
    assert generated == [good.key]


def test_review_failure_is_closed_and_cooldown_avoids_repeated_calls(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    source = source_of(root, put(root))
    calls = []
    fake_reviews(monkeypatch, calls, failure=True)
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "review"),
    ]
    assert main(args) == 1
    state = read_admissions(root / "atlas.db")
    assert not state.allows(source.key)
    assert state.reviews[source.key].reason_code == "review_error"
    assert main(args) == 1 and len(calls) == 1
    (tmp_path / "review/retry-after.json").unlink()
    fake_reviews(monkeypatch, calls)
    assert main([*args, "--refresh"]) == 0
    assert read_admissions(root / "atlas.db").allows(source.key)


def test_history_append_only_and_invalid_evidence_not_saved(tmp_path):
    root = tmp_path / "store"
    source = source_of(root, put(root))
    store = AdmissionStore(root / "atlas.db")
    state = read_admissions(root / "atlas.db")
    try:
        with pytest.raises(ValueError, match="证据"):
            store.record(
                source,
                Review("rejected", "advertisement", "推销", "invented quote"),
                policy=state.policy,
            )
        store.record(
            source, Review("accepted", "substantive", "有技术内容"), policy=state.policy
        )
        for sql in (
            "DELETE FROM admission_reviews",
            "UPDATE admission_reviews SET decision='pending'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                store.db.execute(sql)
        assert len(read_admissions(root / "atlas.db").reviews) == 1
    finally:
        store.close()


def test_read_only_missing_database_and_legacy_default(tmp_path):
    path = tmp_path / "missing.db"
    assert not read_admissions(path).enabled
    assert read_admissions(path).allows("legacy")
    assert not path.exists()


def test_model_identity_and_evidence_validation_with_live_control(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    source = source_of(root, put(root))
    posted = []
    item = {
        "id": "1",
        "decision": "rejected",
        "reason_code": "advertisement",
        "reason": "纯推销",
        "evidence": source.body[:30],
    }

    def post(url, **kwargs):
        posted.append(kwargs["json"])
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"reviews": [item]})},
                    }
                ]
            },
        )

    monkeypatch.setattr("atlas.cognition.admission.requests.post", post)
    client = AdmissionClient(fake_config())
    results, _ = client.review([source], [])
    assert results[source.key].decision == "rejected"
    assert "不可信" in posted[0]["messages"][0]["content"]
    for bad in (replace(source, body="x"),):
        with pytest.raises(ValueError, match="正文"):
            client.review([bad], [])
    item["evidence"] = "not in article"
    with pytest.raises(ValueError, match="证据"):
        client.review([source], [])
    item["evidence"] = source.body[:30]
    item["id"] = "unknown"
    with pytest.raises(ValueError, match="身份"):
        client.review([source], [])
    item["id"] = "1"
    item["decision"] = "accepted"
    with pytest.raises(ValueError, match="状态"):
        client.review([source], [])


def test_scope_change_requires_new_review(tmp_path):
    from atlas.registry import Industry, SqliteConfigStore

    root = tmp_path / "store"
    source = source_of(root, put(root))
    store = AdmissionStore(root / "atlas.db")
    state = read_admissions(root / "atlas.db")
    store.record(
        source, Review("accepted", "substantive", "有技术内容"), policy=state.policy
    )
    store.enable()
    store.close()
    assert read_admissions(root / "atlas.db").allows(source.key)
    registry = SqliteConfigStore(
        db_path=root / "atlas.db",
        author="test",
        industries=(Industry(id="ai", name="AI", enabled=True),),
    )
    registry.close()
    assert not read_admissions(root / "atlas.db").allows(source.key)


def test_bad_item_isolated_without_stopping_valid_batch(tmp_path, monkeypatch):
    root = tmp_path / "store"
    good = source_of(root, put(root, "Technical article"))
    bad = source_of(root, put(root, "Malformed", "https://example.test/bad"))
    monkeypatch.setattr(
        "atlas.admission_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )

    def review(self, sources, scope):
        if any(s.key == bad.key for s in sources):
            raise ValueError("malformed identity")
        return {
            s.key: Review("accepted", "substantive", "有实质信息") for s in sources
        }, {}

    monkeypatch.setattr(AdmissionClient, "review", review)
    assert (
        main(
            [
                "--store-root",
                str(root),
                "--enable",
                "--report-dir",
                str(tmp_path / "review"),
            ]
        )
        == 0
    )
    state = read_admissions(root / "atlas.db")
    assert state.allows(good.key) and not state.allows(bad.key)
    assert state.reviews[bad.key].reason_code == "review_error"
    assert "malformed identity" in (tmp_path / "review/errors.jsonl").read_text()


def test_transient_request_retried_and_manual_error_retry_does_not_regenerate_success(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    source = source_of(root, put(root))
    calls = []
    monkeypatch.setattr(
        "atlas.admission_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )
    monkeypatch.setattr("atlas.admission_cli.time.sleep", lambda seconds: None)

    def review(self, sources, scope):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("入站审核模型请求失败：HTTP 503")
        return {
            s.key: Review("accepted", "substantive", "有实质信息") for s in sources
        }, {}

    monkeypatch.setattr(AdmissionClient, "review", review)
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "review"),
    ]
    assert main(args) == 0 and len(calls) == 2
    assert read_admissions(root / "atlas.db").allows(source.key)
    assert main([*args, "--retry-errors"]) == 0 and len(calls) == 2


def test_admission_fetch_retains_cleaned_body_without_queuing_unreviewed_summary(
    tmp_path, monkeypatch
):
    from atlas.archive import read_article_contents
    from atlas.collect.article import ArticleContent
    from atlas.contracts import RawRecord
    from tests.test_feed_refresh import BODY

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>New technical article</title><link>https://example.test/new-body</link></item></channel></rss>"
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
        "atlas.admission_cli.ArticleReader.read",
        lambda self, url: ArticleContent(BODY, url, raw, "text/html"),
    )
    calls = []
    fake_reviews(monkeypatch, calls)
    assert (
        main(
            [
                "--store-root",
                str(root),
                "--enable",
                "--report-dir",
                str(tmp_path / "review"),
            ]
        )
        == 0
    )
    body = read_article_contents(root / "atlas.db", include_text=True)[
        "https://example.test/new-body"
    ]
    assert body.cleaned_text == BODY and body.status == "ready"
    state = read_admissions(root / "atlas.db")
    assert list(state.reviews.values())[0].decision == "accepted"
    db = sqlite3.connect(root / "atlas.db")
    assert not db.execute(
        "select 1 from sqlite_master where name='feed_articles'"
    ).fetchone()
    db.close()


def test_fetch_budget_preserves_review_progress_and_resumes_deferred_sources(tmp_path, monkeypatch):
    from atlas.collect.article import ArticleContent
    from atlas.contracts import RawRecord
    from tests.test_feed_refresh import BODY

    root = tmp_path / "store"
    payload = b"<rss><channel>" + b"".join(
        f"<item><title>Article {i}</title><link>https://example.test/{i}</link></item>".encode()
        for i in range(3)
    ) + b"</channel></rss>"
    record = RawRecord.create(channel_id="feed", endpoint="https://example.test/feed", content=payload, http_status=200)
    with open_archive(root) as archive:
        archive.put(record, payload)
    fetched = []

    def read(self, url):
        fetched.append(url)
        return ArticleContent(BODY, url, b"<article>body</article>", "text/html")

    monkeypatch.setattr("atlas.admission_cli.ArticleReader.read", read)
    calls = []
    fake_reviews(monkeypatch, calls)
    args = ["--store-root", str(root), "--enable", "--max-fetches", "1", "--report-dir", str(tmp_path / "review")]
    assert main(args) == 0
    state = read_admissions(root / "atlas.db")
    assert len(state.reviews) == 1 and len(fetched) == 1 and len(calls) == 1
    report = json.loads((tmp_path / "review/progress.json").read_text())
    assert report["fetch_deferred"] == 2 and report["accepted"] == 1
    assert main(args) == 0 and main(args) == 0
    assert len(fetched) == 3 and len(read_admissions(root / "atlas.db").reviews) == 3


def test_classification_gate_and_approval_change_invalidates_node_cache(tmp_path):
    from atlas.compose import NODE_CLASSIFY
    from atlas.runner import InMemoryExecutionRecordStore
    from tests._compose_classify import (
        INDUSTRY_WEB,
        QUOTE_FEED_A,
        QuotingPort,
        classify_pipeline,
        make_store_root,
        proposed_rows,
    )

    root = make_store_root(tmp_path, article=False)
    records = InMemoryExecutionRecordStore()
    with classify_pipeline(root, classify=False, execution_store=records) as pipeline:
        assert pipeline.run().all_succeeded()
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive)
        sources = {
            s.title: s
            for rid in archive.all_raw_ids()
            for s in access.brief_sources(rid).values()
        }
    store = AdmissionStore(root / "atlas.db")
    store.enable()
    state = read_admissions(root / "atlas.db")
    port = QuotingPort(((INDUSTRY_WEB, QUOTE_FEED_A),))
    with classify_pipeline(root, port=port, execution_store=records) as pipeline:
        report = pipeline.run()
        assert all(r.status in ("succeeded", "skipped") for r in report.results), [
            (r.task_name, r.status, r.reason) for r in report.results
        ]
        assert (
            report.result(NODE_CLASSIFY).output.artifacts["observed"][
                "admission_blocked_units"
            ]
            == 2
        )
    assert port.call_count == 0 and not proposed_rows(root / "atlas.db")
    store.record(
        sources["Scaling laws"],
        Review("accepted", "substantive", "有实质内容"),
        policy=state.policy,
    )
    store.close()
    with classify_pipeline(root, port=port, execution_store=records) as pipeline:
        report = pipeline.run()
        assert all(r.status in ("succeeded", "skipped") for r in report.results), [
            (r.task_name, r.status, r.reason) for r in report.results
        ]
        observed = report.result(NODE_CLASSIFY).output.artifacts["observed"]
        assert observed["admission_blocked_units"] == 1 and observed["units_seen"] == 1
    assert port.call_count == 1 and len(proposed_rows(root / "atlas.db")) == 1


def test_insufficient_abstract_fetches_original_on_next_review(tmp_path, monkeypatch):
    from atlas.collect.article import ArticleContent
    from tests.test_feed_refresh import BODY

    root = tmp_path / "store"
    source = source_of(root, put(root))
    monkeypatch.setattr(
        "atlas.admission_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )
    reads = []

    def read(self, url):
        reads.append(url)
        return ArticleContent(BODY, url, b"<article>body</article>", "text/html")

    def review(self, sources, scope):
        return {
            s.key: (
                Review("accepted", "substantive", "正文有实质内容")
                if s.kind == "original"
                else Review("pending", "insufficient_content", "摘要不足以判断")
            )
            for s in sources
        }, {}

    monkeypatch.setattr("atlas.admission_cli.ArticleReader.read", read)
    monkeypatch.setattr(AdmissionClient, "review", review)
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "review"),
    ]
    assert main(args) == 0 and not read_admissions(root / "atlas.db").allows(source.key)
    assert main(args) == 0 and read_admissions(root / "atlas.db").allows(source.key)
    assert reads == [source.url]
    assert main(args) == 0 and reads == [source.url]


def test_unreadable_original_is_quarantined_without_repeated_fetch_or_model(
    tmp_path, monkeypatch
):
    from atlas.archive import read_article_contents
    from atlas.collect.article import ArticleReadError
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>Blocked original</title><link>https://example.test/blocked</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="feed",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    calls = []

    def read(self, url):
        calls.append(url)
        raise ArticleReadError(
            "原文 HTTP 403",
            url=url,
            raw_content=b"Access denied",
            content_type="text/html",
        )

    monkeypatch.setattr("atlas.admission_cli.ArticleReader.read", read)
    monkeypatch.setattr(
        AdmissionClient, "review", lambda *a: pytest.fail("不可读原文不应调用模型")
    )
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "review"),
    ]
    assert main(args) == 0 and main(args) == 0
    state = read_admissions(root / "atlas.db")
    source = source_of(root, record)
    assert (
        not state.allows(source.key)
        and state.reviews[source.key].reason_code == "source_unavailable"
    )
    assert calls == [source.url]
    body = read_article_contents(root / "atlas.db")[source.url]
    assert body.status == "unavailable" and body.http_status == 403


def test_admission_reads_paper_full_text_not_abs_page(tmp_path, monkeypatch):
    from atlas.archive import read_article_contents
    from atlas.collect.article import ArticleContent
    from atlas.contracts import RawRecord
    from tests.test_feed_refresh import BODY

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>New paper</title><link>https://arxiv.org/abs/2601.12345</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="feed",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    calls = []

    def read(self, url):
        calls.append(url)
        assert "/html/" in url
        return ArticleContent(
            BODY,
            url,
            b'<article class="ltx_document">Full paper</article>',
            "text/html",
        )

    monkeypatch.setattr("atlas.admission_cli.ArticleReader.read", read)
    fake_reviews(monkeypatch, [])
    assert (
        main(
            [
                "--store-root",
                str(root),
                "--enable",
                "--report-dir",
                str(tmp_path / "review"),
            ]
        )
        == 0
    )
    body = read_article_contents(root / "atlas.db")["https://arxiv.org/abs/2601.12345"]
    assert (
        body.content_kind == "paper"
        and body.resolved_url == "https://arxiv.org/html/2601.12345"
    )
    assert calls == [body.resolved_url]
