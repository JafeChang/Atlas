"""Hybrid ranking, current filters and browser round-trips over real local HTTP."""

import hashlib
import sqlite3
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from bs4 import BeautifulSoup

from atlas import brief_search_cli as index
from atlas.archive import open_archive
from atlas.brief_search import BriefSearch
from atlas.cognition.briefs import ArticleBrief, BriefStore
from atlas.contracts import RawRecord
from atlas.contracts.anchors import EvidenceAnchor
from atlas.feed import FeedQuery, run_query
from atlas.feed.query import SourceContractError
from atlas.webapp import ArchiveEntriesAccess
from tests.test_brief_search import Encoder, put, seeded
from tests.test_webui_entries_render import _get, running_frontend


def test_keyword_search_needs_no_model_and_reloads_atomic_rebuild(tmp_path):
    source, target = seeded(tmp_path)
    index.build_index(source, target, encoder=Encoder())
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    search = BriefSearch(target, encoder_factory=lambda: pytest.fail("keyword loaded a model"))
    try:
        assert search.search("氧化铝", "keyword", {"alumina", "model"}) == ["alumina"]
        assert search.search("氧化铝", "keyword", {"model"}) == []
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        put(source, "model", "氧化铝模型", "氧化铝产量预测。")
        index.build_index(source, target, encoder=Encoder())
        assert search.search("氧化铝", "keyword", {"model"}) == ["model"]
    finally:
        search.close()


def test_hybrid_boosts_literal_hits_reuses_encoder_and_isolates_filters(tmp_path):
    pytest.importorskip("numpy")
    source, target = seeded(tmp_path)
    # Both documents have the same semantic vector; exact lexical evidence wins.
    put(source, "model", "铝业研究", "氧化铝产业研究。")
    index.build_index(source, target, encoder=Encoder())
    encoder = Encoder()
    search = BriefSearch(target, encoder_factory=lambda: encoder)
    try:
        assert search.search("氧化铝产量", "hybrid", {"model", "alumina"})[0] == "alumina"
        assert search.search("氧化铝产量", "hybrid", {"model"}) == ["model"]
        assert search.search("氧化铝产量", "hybrid", set()) == []
        assert encoder.calls == ["query: 氧化铝产量"]
        assert search.search('" OR *', "keyword", {"model", "alumina"}) == []
    finally:
        search.close()


def test_semantic_window_is_applied_after_current_eligibility(tmp_path):
    pytest.importorskip("numpy")
    source = tmp_path / "source.sqlite"
    for n in range(110):
        put(source, f"doc-{n:03}", "氧化铝", "氧化铝生产。")
    target = tmp_path / "index.sqlite"
    index.build_index(source, target, encoder=Encoder())
    search = BriefSearch(target, encoder_factory=Encoder)
    try:
        assert search.search("铝", "semantic", {"doc-109"}) == ["doc-109"]
    finally:
        search.close()


def test_web_search_preserves_context_and_filters_before_ranking(tmp_path):
    with running_frontend(tmp_path) as world:
        seen = []
        def rank(entries, text, mode):
            seen.append((entries, text, mode))
            return sorted(entries, key=lambda entry: entry.char_start)
        world.app.server.entry_search = rank
        q = 'memory <script>alert(1)</script>'
        path = "/feed?channel=chan-feed&limit=1&q=" + quote(q) + "&search_mode=hybrid"
        status, html, _ = _get(world.app, path)
        assert status == 200
        soup = BeautifulSoup(html, "html.parser")
        assert soup.select_one('[name="q"]')["value"] == q
        assert q not in html
        assert "按相关度排列" in soup.get_text()
        assert seen[-1][1:] == (q, "hybrid")
        assert {entry.channel_id for entry in seen[-1][0]} == {"chan-feed"}
        assert len(soup.select("article.item")) == 1
        next_link = soup.select_one('a[aria-label="下一页"]')["href"]
        assert parse_qs(urlsplit(next_link).query)["q"] == [q]
        status, next_html, _ = _get(world.app, next_link)
        assert status == 200
        second = BeautifulSoup(next_html, "html.parser").select_one("article.item")
        assert second["data-char-start"] != soup.select_one("article.item")["data-char-start"]
        for link in soup.select('.source-menu a, .quick a, .compact-pager a'):
            assert parse_qs(urlsplit(link["href"]).query)["q"] == [q]
        back = soup.select_one('form.act-valid [name="return_to"]')["value"]
        from atlas.webui.app import _validate_return_to
        _validate_return_to(back)
        assert parse_qs(urlsplit(back).query)["search_mode"] == ["hybrid"]
        # Labels are personal and are applied before the ranker is called.
        status, _, _ = _get(world.app, path + "&labeled=true")
        assert status == 200 and seen[-1][0] == ()


def test_real_composition_maps_index_keys_to_archived_entries_without_writes(tmp_path, monkeypatch):
    with running_frontend(tmp_path) as world:
        archive = open_archive(world.root)
        access = ArchiveEntriesAccess(archive)
        sources = [replace(source, body=source.body + " Full archived article body." * 30)
                   for source in access.brief_sources("raw-feed-render").values()]
        original = archive.get("raw-feed-render")
        original_bytes = archive.get_content(original.raw_id)
        copy = RawRecord(**{**original.model_dump(), "raw_id": "raw-feed-copy", "fetched_at": original.fetched_at + timedelta(hours=1)})
        archive.put(copy, original_bytes)
        span = next(iter(access.brief_sources(original.raw_id)))
        store = BriefStore(world.db_path)
        store.record(sources, {s.key: ArticleBrief("氧化铝报道", "氧化铝工厂产量增加。") for s in sources}, model="fixture")
        store.close()
        archive.close()
        index.build_index(world.db_path, world.root / "brief-search.sqlite", encoder=Encoder())
        with sqlite3.connect(world.db_path) as db:
            before = db.execute("SELECT * FROM feed_articles ORDER BY source_key").fetchall()
        assert _get(world.app, "/feed")[0] == 200  # Warm the immutable browse metadata.
        monkeypatch.setattr(ArchiveEntriesAccess, "brief_sources", lambda *args: pytest.fail("search reparsed full archive text"))
        status, html, _ = _get(world.app, "/feed?q=" + quote("氧化铝") + "&search_mode=keyword")
        assert status == 200
        soup = BeautifulSoup(html, "html.parser")
        assert len(soup.select("article.item")) == 2
        assert {item["data-raw-id"] for item in soup.select("article.item")} == {"raw-feed-copy"}
        assert "氧化铝报道" in soup.get_text()
        world.app.labels.record(raw_id=original.raw_id, label_key="valid", label_value="valid", actor="tester",
                                anchor=EvidenceAnchor.create(raw_id=original.raw_id, raw_sha256=original.content_sha256,
                                                             char_start=span[0], char_end=span[1]))
        status, labeled_html, _ = _get(world.app, "/feed?q=" + quote("氧化铝") + "&search_mode=keyword&labeled=true")
        labeled = BeautifulSoup(labeled_html, "html.parser").select("article.item")
        assert status == 200 and len(labeled) == 1 and labeled[0]["data-raw-id"] == original.raw_id
        with sqlite3.connect(world.db_path) as db:
            assert db.execute("SELECT * FROM feed_articles ORDER BY source_key").fetchall() == before


def test_feed_selection_callbacks_cannot_modify_or_duplicate_articles(tmp_path):
    with running_frontend(tmp_path) as world:
        kwargs = {"source": world.app.server.resolve_source(), "query": FeedQuery(granularity="entry"),
                  "entries_of": world.app.server.entries_lookup}
        assert run_query(**kwargs, entry_ranker=lambda entries: entries[:1]).total == 1
        for name in ("entry_selector", "entry_ranker"):
            with pytest.raises(SourceContractError):
                run_query(**kwargs, **{name: lambda entries: [replace(entries[0], title="forged")]})
            with pytest.raises(SourceContractError):
                run_query(**kwargs, **{name: lambda entries: [entries[0], entries[0]]})


@pytest.mark.parametrize("suffix", ["q=a&q=b", "q=a&search_mode=other", "q=" + quote("a" * 201), "q=***", "q=a&granularity=document"])
def test_invalid_search_inputs_have_a_working_control(tmp_path, suffix):
    with running_frontend(tmp_path) as world:
        world.app.server.entry_search = lambda entries, text, mode: entries
        assert _get(world.app, "/feed?q=valid")[0] == 200
        assert _get(world.app, "/feed?" + suffix)[0] == 400


def test_missing_index_is_an_explicit_service_error_and_browsing_still_works(tmp_path):
    with running_frontend(tmp_path) as world:
        assert _get(world.app, "/feed")[0] == 200
        status, html, _ = _get(world.app, "/feed?q=memory")
        assert status == 503 and "搜索暂时不可用" in html


@pytest.mark.parametrize("width", [320, 1366])
def test_search_form_submits_and_fits_mobile_and_desktop(tmp_path, width):
    api = pytest.importorskip("playwright.sync_api")
    with running_frontend(tmp_path) as world, api.sync_playwright() as p:
        world.app.server.entry_search = lambda entries, text, mode: list(reversed(entries))
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(world.app.base_url + "/feed?channel=chan-feed&limit=1")
            trigger = page.get_by_role("button", name="搜索文章", exact=True)
            dialog = page.get_by_role("dialog", name="搜索文章", exact=True)
            field = page.get_by_role("searchbox", name="搜索文章", exact=True)
            api.expect(dialog).not_to_be_visible()
            trigger.click()
            api.expect(dialog).to_be_visible()
            api.expect(field).to_be_focused()
            api.expect(trigger).to_have_attribute("aria-expanded", "true")
            field.fill("draft")
            page.get_by_role("button", name="关闭搜索", exact=True).click()
            api.expect(dialog).not_to_be_visible()
            api.expect(trigger).to_be_focused()
            trigger.click()
            api.expect(field).to_have_value("draft")
            page.keyboard.press("Escape")
            api.expect(dialog).not_to_be_visible()
            api.expect(trigger).to_have_attribute("aria-expanded", "false")
            api.expect(trigger).to_be_focused()
            trigger.click()
            page.mouse.click(1, 1)
            api.expect(dialog).not_to_be_visible()
            api.expect(trigger).to_be_focused()
            trigger.click()
            page.keyboard.press("Shift+Tab")
            api.expect(page.get_by_role("button", name="关闭搜索", exact=True)).to_be_focused()
            field.fill("memory")
            page.get_by_label("搜索方式", exact=True).select_option("semantic")
            if width == 320:
                field.press("Enter")
            else:
                page.get_by_role("button", name="搜索", exact=True).click()
            page.wait_for_url("**/feed?**q=memory**")
            api.expect(dialog).not_to_be_visible()
            assert "has-search" in trigger.get_attribute("class")
            trigger.click()
            api.expect(field).to_have_value("memory")
            assert page.get_by_label("搜索方式", exact=True).input_value() == "semantic"
            assert "包含语义相关结果" in page.locator(".feed-search-note").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            for selector in ("#feed-search-input", "#feed-search-mode", "[data-search-form] button"):
                box = page.locator(selector).bounding_box()
                assert box["height"] >= 44 and box["x"] >= 0 and box["x"] + box["width"] <= width
            page.get_by_role("link", name="清除搜索", exact=True).click()
            page.wait_for_url(lambda url: "q=" not in url)
            assert "channel=chan-feed" in page.url
            assert "has-search" not in trigger.get_attribute("class")
            assert not errors
        finally:
            browser.close()
