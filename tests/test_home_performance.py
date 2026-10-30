from collections import Counter
from contextlib import closing
from dataclasses import replace

from bs4 import BeautifulSoup

from atlas.archive import open_archive
from atlas.cognition import ArticleBrief, BriefStore, read_articles
from atlas.cognition.admission import AdmissionStore, Review, read_admissions
from atlas.entries import is_challenge_page
from atlas.webapp import ArchiveEntriesAccess, build_webapp
from tests.test_feed_refresh import put
from tests.test_webui_entries_render import _get


def test_browse_index_survives_full_text_eviction_and_keeps_exact_identity(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    records = [put(root, f"Article {i}", f"https://example.test/{i}") for i in range(5)]
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive, cache_size=2)
        calls = Counter()
        original = access.content

        def counted(raw_id):
            calls[raw_id] += 1
            return original(raw_id)

        monkeypatch.setattr(access, "content", counted)
        for record in records:
            cached = access.browse_record(record)
            full = access.entries_of(record)
            entry, expected = cached.view.entries[0], full.entries[0]
            for field in (
                "entry_id",
                "raw_id",
                "raw_sha256",
                "index",
                "char_start",
                "char_end",
                "title",
                "link",
                "published_at",
            ):
                assert getattr(entry, field) == getattr(expected, field)
            assert not hasattr(entry, "entry_text")
            sources = access.brief_sources(record.raw_id)
            assert cached.sources == {
                span: replace(s, body="") for span, s in sources.items()
            }
        for _ in range(3):
            for record in records:
                assert access.browse_record(record).view.is_feed
                assert access.browse_record(record).sources
        assert calls == {record.raw_id: 1 for record in records}
        assert len(access._cache) == 2
        # Reading detail still returns complete evidence after full-text eviction.
        assert access.brief_sources(records[0].raw_id)
        assert calls[records[0].raw_id] == 2


def test_warm_home_reads_new_raw_briefs_and_review_decisions(tmp_path, monkeypatch):
    root = tmp_path / "store"
    first = put(root)
    db_path = root / "atlas.db"
    app = build_webapp(
        root,
        db_path=db_path,
        require_login=False,
        industry_provider=lambda: ("ai",),
        industry_of={"feed": "ai"},
        prewarm_browse=True,
    )
    try:
        original = ArchiveEntriesAccess.content
        reads = []

        def counted(self, raw_id):
            reads.append(raw_id)
            return original(self, raw_id)

        with monkeypatch.context() as patch:
            patch.setattr(ArchiveEntriesAccess, "content", counted)
            assert _get(app, "/feed")[0] == 200
            assert reads == []  # 服务预热后首个 HTTP 请求不重新读原文。
        second = put(root, "Fresh collection", "https://example.test/new")
        with open_archive(root) as archive:
            access = ArchiveEntriesAccess(archive)
            sources = [
                next(iter(access.brief_sources(r.raw_id).values()))
                for r in (first, second)
            ]
        with closing(BriefStore(db_path)) as store:
            store.record(
                sources,
                {
                    s.key: ArticleBrief("更新译题", "后台新增的中文简介。")
                    for s in sources
                },
                model="test",
            )
        with closing(AdmissionStore(db_path)) as store:
            policy = read_admissions(db_path).policy
            for source in sources:
                store.record(
                    source, Review("accepted", "substantive", "有效内容"), policy=policy
                )
            store.enable()
        status, page, _ = _get(app, "/feed")
        assert status == 200 and "Fresh collection" in page
        assert len(BeautifulSoup(page, "html.parser").select("article.entry")) == 2
        assert "更新译题" in page and "后台新增的中文简介。" in page
        with closing(AdmissionStore(db_path)) as store:
            store.record(
                sources[1],
                Review("rejected", "advertisement", "复核拦截", sources[1].body[:40]),
                policy=policy,
            )
        status, page, _ = _get(app, "/feed")
        assert status == 200 and "Fresh collection" not in page
        assert len(BeautifulSoup(page, "html.parser").select("article.entry")) == 1
    finally:
        app.close()


def test_article_list_can_skip_full_text_without_changing_default_reader(tmp_path):
    path = tmp_path / "atlas.db"
    from atlas.cognition import brief_source

    source = brief_source("English title", "Full body with useful content. " * 10)
    with closing(BriefStore(path)) as store:
        store.record(
            [source], {source.key: ArticleBrief("中文译题", "中文简介。")}, model="test"
        )
    full = read_articles(path)
    small = read_articles(path, include_text=False)
    assert full[source.key].article_text == source.body
    assert small == {key: replace(row, article_text="") for key, row in full.items()}


def test_feed_without_meta_skips_html_challenge_parser(monkeypatch):
    from atlas.entries import adapters

    original = adapters._ChallengeRedirect
    calls = []

    def counted():
        calls.append(True)
        return original()

    monkeypatch.setattr(adapters, "_ChallengeRedirect", counted)
    assert not is_challenge_page(
        "<rss><channel><item>captcha article</item></channel></rss>" * 1000
    )
    assert calls == []
    assert is_challenge_page(
        '<MeTa\nHTTP-EQUIV="refresh" content="0;url=&#47;.well-known/sgcaptcha/">'
    )
    assert calls == [True]
