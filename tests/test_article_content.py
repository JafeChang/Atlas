import hashlib
import sqlite3

import pytest

from atlas.archive import ArticleContentStore, read_article_contents
from atlas.collect.article import ArticleContent, clean_document
from atlas.collect.article_failure import classify_article_failure


def test_clean_body_preserves_paragraphs_code_and_removes_non_article_sections():
    raw = b'<html><nav>Menu</nav><article><h1>Heading</h1><p>First <b>important</b> paragraph.</p><p>Second paragraph.</p><div class="cookie-banner">Cookie prompt</div><div class="social-share">Share now</div><pre>if ready:\n    run()</pre><div class="related-posts">Other story</div></article><footer>Footer</footer></html>'
    body = clean_document(raw, "text/html").text
    assert "First important paragraph." in body and "\n\nSecond paragraph." in body
    assert "if ready:\n    run()" in body
    assert not any(
        t in body
        for t in ("Menu", "Cookie prompt", "Share now", "Other story", "Footer")
    )


def test_store_retains_cleaned_body_raw_bytes_and_append_only_history(tmp_path):
    path = tmp_path / "atlas.db"
    assert read_article_contents(path) == {} and not path.exists()
    store = ArticleContentStore(path)
    fields = {
        "source_url": "https://example.test/article",
        "resolved_url": "https://example.test/article",
        "status": "ready",
        "content_kind": "article",
        "cleaned_text": "正文第一段。\n\n正文第二段。",
        "cleaner_version": "test-v1",
        "raw_content": b"<article>Original</article>",
        "content_type": "text/html",
    }
    first = store.record(**fields)
    assert store.record(**fields) == first
    assert store.raw(first) == (fields["raw_content"], "text/html")
    second = store.record(
        **{**fields, "cleaned_text": "修正后的清洗正文。", "cleaner_version": "test-v2"}
    )
    assert second > first
    assert (
        store.db.execute(
            "SELECT cleaned_text FROM article_contents WHERE id=?", (first,)
        ).fetchone()[0]
        == fields["cleaned_text"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.db.execute("DELETE FROM article_contents WHERE id=?", (first,))
    store.close()
    digest = hashlib.sha256(path.read_bytes()).digest()
    row = read_article_contents(path, include_text=True)[fields["source_url"]]
    assert row.cleaned_text == "修正后的清洗正文。"
    assert read_article_contents(path)[fields["source_url"]].cleaned_text == ""
    assert hashlib.sha256(path.read_bytes()).digest() == digest


@pytest.mark.parametrize(
    "message,url,code,category,retry",
    [
        ("原文 HTTP 403", "https://example.test/", "http_forbidden", "access", False),
        (
            "robots 拒绝或无法确认允许：disallowed_by_rules",
            "",
            "robots_disallowed",
            "access",
            False,
        ),
        ("原文网络请求失败：SSLError", "", "tls_error", "network", True),
        ("原文 HTTP 404", "", "http_not_found", "not_found", False),
        (
            "原文缺少足够的可读正文",
            "https://www.youtube.com/watch?v=123",
            "video_without_text",
            "video",
            False,
        ),
        (
            "原文缺少足够的可读正文",
            "https://example.test/",
            "no_readable_body",
            "content",
            False,
        ),
        ("未预期的明确错误", "", "unknown", "unknown", False),
    ],
)
def test_failure_categories_keep_observed_evidence(message, url, code, category, retry):
    result = classify_article_failure(message, url)
    assert (result.code, result.category, result.retryable) == (code, category, retry)
    assert result.detail == message
    assert "付费墙" not in result.label  # HTTP 403 本身不证明付费墙。


def test_body_cli_stores_fetched_full_body_without_invoking_models(
    tmp_path, monkeypatch
):
    from atlas.archive import open_archive
    from atlas.article_content_cli import main
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>Example article</title><link>https://example.test/post</link><description>Short abstract.</description></item></channel></rss>"
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    calls = []
    body = "Full original body with meaningful article content. " * 15
    raw = b"<article>" + body.encode() + b"</article>"

    def read(self, url):
        calls.append(url)
        return ArticleContent(body, url, raw, "text/html")

    monkeypatch.setattr("atlas.article_content_cli.ArticleReader.read", read)
    monkeypatch.setattr(
        "atlas.cognition.BriefClient.generate",
        lambda *a, **k: pytest.fail("正文留存不得调用模型"),
    )
    args = ["--store-root", str(root), "--report-dir", str(tmp_path / "report")]
    assert main(args) == 0
    row = read_article_contents(root / "atlas.db", include_text=True)[
        "https://example.test/post"
    ]
    assert row.status == "ready" and row.cleaned_text == body
    assert row.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert main(args) == 0 and len(calls) == 1
    with sqlite3.connect(root / "atlas.db") as db:
        assert (
            db.execute("SELECT count(*) FROM article_contents").fetchone()[0] == 2
        )  # 摘要与原文分别留存。
        assert db.execute("SELECT summary_zh FROM feed_articles").fetchone()[0] == ""
    with open_archive(root) as archive:
        assert archive.get_content(record.raw_id) == payload


def test_arxiv_html_then_pdf_and_never_uses_abs_page_as_full_text():
    from atlas.article_content_cli import read_source
    from atlas.collect.article import ArticleReadError

    calls = []

    class Reader:
        def read(self, url):
            calls.append(url)
            if "/html/" in url:
                raise ArticleReadError("原文 HTTP 404", url=url)
            return ArticleContent(
                "Full paper body " * 30, url, b"%PDF fake", "application/pdf", "paper"
            )

    result = read_source(Reader(), "https://arxiv.org/abs/2601.12345v2")
    assert result.content_kind == "paper"
    assert calls == [
        "https://arxiv.org/html/2601.12345v2",
        "https://arxiv.org/pdf/2601.12345v2",
    ]

    class Denied:
        def read(self, url):
            raise ArticleReadError("原文 HTTP 403", url=url)

    with pytest.raises(ArticleReadError, match="403"):
        read_source(Denied(), "https://arxiv.org/abs/2601.12345v2")


def test_failed_refresh_keeps_previous_successful_body_available(tmp_path):
    from atlas.archive import read_article_body

    path = tmp_path / "atlas.db"
    store = ArticleContentStore(path)
    store.record(
        source_url="https://example.test/post",
        status="ready",
        content_kind="article",
        cleaned_text="已经留存的完整正文。",
        cleaner_version="v1",
    )
    store.record(
        source_url="https://example.test/post",
        status="unavailable",
        cleaner_version="v2",
        failure_code="timeout",
        failure_category="network",
        failure_label="网络或证书问题",
        failure_detail="超时",
        retryable=True,
    )
    store.close()
    assert (
        read_article_contents(path)["https://example.test/post"].status == "unavailable"
    )
    assert (
        read_article_body(path, "https://example.test/post").cleaned_text
        == "已经留存的完整正文。"
    )


def test_batch_rolls_back_all_rows_on_invalid_snapshot(tmp_path):
    store = ArticleContentStore(tmp_path / "atlas.db")
    try:
        with pytest.raises(ValueError):
            with store.batch():
                store.record(
                    source_url="https://example.test/good",
                    status="ready",
                    content_kind="article",
                    cleaned_text="合法正文。",
                    cleaner_version="v1",
                )
                store.record(
                    source_url="https://example.test/bad",
                    status="unavailable",
                    cleaner_version="v1",
                )
        assert (
            store.db.execute("SELECT count(*) FROM article_contents").fetchone()[0] == 0
        )
        with store.batch():
            store.record(
                source_url="https://example.test/good",
                status="ready",
                content_kind="article",
                cleaned_text="合法正文。",
                cleaner_version="v1",
            )
        assert (
            store.db.execute("SELECT count(*) FROM article_contents").fetchone()[0] == 1
        )
    finally:
        store.close()


def test_summary_generation_reuses_retained_cleaned_body_instead_of_fetching_again(
    tmp_path, monkeypatch
):
    from atlas.archive import open_archive
    from atlas.briefs_cli import main
    from atlas.cognition import ArticleBrief
    from atlas.contracts import RawRecord
    from tests.test_cognition_support import fake_config

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>New article</title><link>https://example.test/post</link><description>Only an abstract.</description></item></channel></rss>"
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    full = "The full article contains the method, evaluation and conclusions. " * 20
    store = ArticleContentStore(root / "atlas.db")
    store.record(
        source_url="https://example.test/post",
        status="ready",
        content_kind="article",
        cleaned_text=full,
        cleaner_version="test",
    )
    store.close()

    def generate(self, sources):
        assert sources[0].body == full
        return {
            s.key: ArticleBrief("新的文章", "文章介绍方法、评估和结论。")
            for s in sources
        }, {}

    monkeypatch.setattr("atlas.briefs_cli.BriefClient.generate", generate)
    monkeypatch.setattr(
        "atlas.briefs_cli.ArticleReader.read",
        lambda *a: pytest.fail("应复用已留存正文"),
    )
    monkeypatch.setattr(
        "atlas.briefs_cli.CognitionConfig.from_env", lambda **k: fake_config()
    )
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )


def test_pdf_control_characters_are_cleaned_before_sqlite_length_validation(tmp_path):
    from atlas.collect.article import clean_paragraphs

    dirty = (
        "研究\x00正文\x08第一段。\n\n```\nif ready:\n    run()\x00\n```\n\n第二段\x7f。"
    )
    cleaned = clean_paragraphs(dirty)
    assert "\x00" not in cleaned and "\x08" not in cleaned and "\x7f" not in cleaned
    assert "if ready:\n    run()" in cleaned
    store = ArticleContentStore(tmp_path / "atlas.db")
    try:
        store.record(
            source_url="https://example.test/paper",
            status="ready",
            content_kind="paper",
            cleaned_text=cleaned,
            cleaner_version="test",
        )
        assert store.db.execute(
            "SELECT body_chars,length(cleaned_text) FROM article_contents"
        ).fetchone() == (len(cleaned), len(cleaned))
    finally:
        store.close()


def test_reclean_preserves_paper_type_and_fetch_date_without_network(
    tmp_path, monkeypatch
):
    import fcntl

    from atlas.archive import open_archive
    from atlas.article_content_cli import main
    from atlas.collect.article import CLEANER_VERSION
    from atlas.contracts import RawRecord

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>Paper</title><link>https://arxiv.org/abs/2601.12345</link><description>Abstract.</description></item></channel></rss>"
    record = RawRecord.create(
        channel_id="test",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    raw = (
        b'<article class="ltx_document"><p>'
        + b"Actual full paper content. " * 20
        + b"</p></article>"
    )
    store = ArticleContentStore(root / "atlas.db")
    store.record(
        source_url="https://arxiv.org/abs/2601.12345",
        status="ready",
        content_kind="article",
        cleaned_text="Actual full paper content. " * 20,
        cleaner_version=CLEANER_VERSION,
        raw_content=raw,
        content_type="text/html",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    store.close()
    monkeypatch.setattr(
        "atlas.article_content_cli.ArticleReader.read",
        lambda *a: pytest.fail("离线清洗不得联网"),
    )
    # 正文补抓和纯离线清洗不争用同一个网络任务锁。
    with (root / ".article-contents.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (
            main(
                [
                    "--store-root",
                    str(root),
                    "--reclean",
                    "--report-dir",
                    str(tmp_path / "report"),
                ]
            )
            == 0
        )
    row = read_article_contents(root / "atlas.db")["https://arxiv.org/abs/2601.12345"]
    assert row.content_kind == "paper" and row.fetched_at == "2026-01-01T00:00:00+00:00"


def test_arxiv_abs_previously_saved_as_article_still_fetches_full_paper(
    tmp_path, monkeypatch
):
    from atlas.article_content_cli import main
    from atlas.collect.article import CLEANER_VERSION
    from tests.test_feed_refresh import BODY, put

    root = tmp_path / "store"
    url = "https://arxiv.org/abs/2601.12345"
    put(root, "A research paper", url)
    store = ArticleContentStore(root / "atlas.db")
    store.record(
        source_url=url,
        status="ready",
        content_kind="article",
        cleaned_text=BODY,
        cleaner_version=CLEANER_VERSION,
    )
    store.close()
    calls = []

    def read(self, target):
        calls.append(target)
        assert "/html/" in target
        return ArticleContent(
            BODY,
            target,
            b'<article class="ltx_document">Full paper</article>',
            "text/html",
        )

    monkeypatch.setattr("atlas.article_content_cli.ArticleReader.read", read)
    assert (
        main(["--store-root", str(root), "--report-dir", str(tmp_path / "report")]) == 0
    )
    saved = read_article_contents(root / "atlas.db")[url]
    assert saved.content_kind == "paper" and calls == [
        "https://arxiv.org/html/2601.12345"
    ]
