"""Live user isolation, cumulative reading, ranking, and incremental vectors."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

import pytest
from bs4 import BeautifulSoup

from atlas import brief_search_cli as index
from atlas.analytics import EventStore
from atlas.archive import open_archive
from atlas.cognition.briefs import ArticleBrief, BriefStore
from atlas.contracts import RawRecord
from atlas.recommendations import RecommendationEngine, read_profile
from atlas.webapp import ArchiveEntriesAccess
from tests.test_brief_search import Encoder, put, seeded
from tests.test_webui_entries_render import running_frontend
from tests.test_webui_users import client, request


def test_incremental_reuses_vectors_encodes_changes_and_noops_without_model(
    tmp_path, monkeypatch
):
    source, target = seeded(tmp_path)
    index.build_index(source, target, encoder=Encoder())
    before, modified = target.read_bytes(), target.stat().st_mtime_ns
    monkeypatch.setattr(index, "model_metadata", lambda _: Encoder.metadata)
    monkeypatch.setattr(
        index,
        "LocalEncoder",
        lambda _: pytest.fail("unchanged index started inference"),
    )
    report = index.build_index(source, target, incremental=True)
    assert report["encoded_count"] == 0 and report["updated"] is False
    assert target.read_bytes() == before and target.stat().st_mtime_ns == modified
    put(source, "new", "铝业新闻", "新增铝业产量。")
    encoder = Encoder()
    report = index.build_index(source, target, encoder=encoder, incremental=True)
    assert report["encoded_count"] == 1 and report["reused_count"] == 2
    assert encoder.calls == ["passage: 铝业新闻\n新增铝业产量。"]
    put(source, "model", "新模型研究", "改变模型简介。")
    put(source, "alumina", "氧化铝", "已撤回。", status="pending")
    encoder = Encoder()
    report = index.build_index(source, target, encoder=encoder, incremental=True)
    assert report["document_count"] == 2 and report["encoded_count"] == 1
    assert report["reused_count"] == 1
    assert not index.query_index(target, "氧化铝", mode="keyword")
    assert index.query_index(target, "新模型", mode="keyword")


def test_incremental_protocol_mismatch_and_failure_preserve_old_snapshot(
    tmp_path, monkeypatch
):
    source, target = seeded(tmp_path)
    index.build_index(source, target, encoder=Encoder())
    before = target.read_bytes()
    wrong = Encoder()
    wrong.metadata = {**wrong.metadata, "pooling": "changed"}
    with pytest.raises(ValueError, match="协议与索引不符"):
        index.build_index(source, target, encoder=wrong, incremental=True)
    assert target.read_bytes() == before
    put(source, "new", "新文章", "新文章中文简介。")
    monkeypatch.setattr(
        index,
        "write_index",
        lambda *args: (_ for _ in ()).throw(ValueError("failed publish")),
    )
    with pytest.raises(ValueError, match="failed publish"):
        index.build_index(source, target, encoder=Encoder(), incremental=True)
    assert target.read_bytes() == before
    assert index.query_index(target, "模型", mode="keyword")


def test_incremental_can_remove_the_last_eligible_vectors_without_inference(tmp_path):
    source, target = seeded(tmp_path)
    index.build_index(source, target, encoder=Encoder())
    for key in ("alumina", "model"):
        put(source, key, "已撤回", "不再可用。", status="pending")
    encoder = Encoder()
    report = index.build_index(source, target, encoder=encoder, incremental=True)
    assert report["document_count"] == 0 and not encoder.calls
    assert not index.query_index(target, "撤回", mode="keyword")


def test_recommendations_require_identity_and_fail_visibly_without_index(tmp_path):
    from tests.test_webui_entries_render import _get

    with running_frontend(tmp_path) as world:
        assert _get(world.app, "/feed")[0] == 200
        assert _get(world.app, "/feed?view=recommended")[0] == 401
    with running_frontend(tmp_path / "authenticated", require_login=True) as world:
        opener, _ = client()
        assert request(opener, world.app, "/login", {"name": "alice"})[0] == 303
        status, page, _ = request(opener, world.app, "/feed?view=recommended")
        assert status == 503 and "推荐暂时不可用" in page
        assert request(opener, world.app, "/feed")[0] == 200


def event(key, *, view="view", ms=30000, at=None):
    return {
        "event_id": uuid.uuid4().hex,
        "visitor_id": "browser",
        "session_id": "session",
        "view_id": view,
        "event_type": "read_progress",
        "page": "/article",
        "occurred_at": (at or datetime.now(UTC)).isoformat(),
        "collection_version": "v2",
        "raw_id": key,
        "raw_sha256": "a" * 64,
        "char_start": 0,
        "char_end": 100,
        "read_ms": ms,
        "scroll_pct": 80,
    }


def test_online_profile_uses_only_its_user_and_maximum_reading_snapshots(tmp_path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    now = datetime.now(UTC)

    def resolve(raw, *anchor):
        return SimpleNamespace(key=raw, url="https://test/" + raw)

    for ms in (15000, 30000, 45000):
        store.append_event(event("alumina", ms=ms, at=now), user_id="alice")
    store.append_event(event("model", ms=120000, at=now), user_id="bob")
    store.append_event(event("anonymous", ms=120000, at=now))
    store.append_event(event("old", at=now - timedelta(days=31)), user_id="alice")
    store.append_event(event("future", at=now + timedelta(days=10)), user_id="alice")
    first = read_profile(path, "alice", resolve, now=now)
    assert len(first.interests) == 1
    assert first.interests[0].key == "alumina" and first.interests[0].read_ms == 45000
    assert first.seen_urls == {"https://test/alumina"}
    assert read_profile(path, "bob", resolve, now=now).interests[0].key == "model"
    duplicate = event("alumina", ms=45000, at=now)
    store.append_event(duplicate, user_id="alice")
    store.append_event(duplicate, user_id="alice")
    assert read_profile(path, "alice", resolve, now=now).interests == first.interests
    store.append_event(
        event("new-topic", view="second", ms=120000, at=now), user_id="alice"
    )
    updated = read_profile(path, "alice", resolve, now=now)
    assert updated.cursor > first.cursor and len(updated.interests) == 2
    assert updated.interests[0].key == "new-topic"
    assert not read_profile(path, "empty", resolve, now=now).interests
    with pytest.raises(ValueError, match="登录"):
        read_profile(path, "", resolve, now=now)


def candidate(key, now, *, url=None, valid=None):
    return SimpleNamespace(
        raw_id="raw-" + key,
        raw_sha256="a" * 64,
        char_start=0,
        char_end=100,
        fetched_at=now,
        timestamp=now,
        channel_id=key,
        link=url or "https://test/" + key,
        endpoint="https://test/feed",
        confirmed_values=(("valid", valid),) if valid else (),
    ), key


def test_rank_is_personal_updates_next_request_and_reloads_incremental_index(tmp_path):
    pytest.importorskip("numpy")
    source, target = seeded(tmp_path)
    put(source, "alumina-news", "铝业新闻", "氧化铝工厂投产。")
    put(source, "model-news", "模型新闻", "模型记忆研究。")
    index.build_index(source, target, encoder=Encoder())
    events = tmp_path / "events.db"
    engine = RecommendationEngine(target, events, tmp_path / "decisions.db")
    store = EventStore(events)
    now = datetime.now(UTC)

    def resolve(raw, *anchor):
        return SimpleNamespace(key=raw, url="https://test/" + raw)

    candidates = [candidate(key, now) for key in ("alumina-news", "model-news")]
    store.append_event(event("alumina", ms=15000, at=now), user_id="alice")
    store.append_event(event("model", ms=60000, at=now), user_id="bob")
    assert (
        engine.rank(candidates, "alice", resolve, now=now).entries[0].raw_id
        == "raw-alumina-news"
    )
    assert (
        engine.rank(candidates, "bob", resolve, now=now).entries[0].raw_id
        == "raw-model-news"
    )
    store.append_event(event("model", ms=120000, view="new", at=now), user_id="alice")
    result = engine.rank(candidates, "alice", resolve, now=now)
    assert result.entries[0].raw_id == "raw-model-news"
    assert result.profile_size == 2
    put(source, "new-model", "新增模型", "模型的新研究。")
    report = index.build_index(source, target, encoder=Encoder(), incremental=True)
    assert report["encoded_count"] == 1
    result = engine.rank(
        [*candidates, candidate("new-model", now)], "alice", resolve, now=now
    )
    assert "raw-new-model" in {entry.raw_id for entry in result.entries}
    # Filtering and manual invalidity survive ranking; dedup runs after eligibility.
    invalid, key = candidate("model-news", now, valid="invalid")
    result = engine.rank(
        [candidate("alumina-news", now), (invalid, key)],
        "alice",
        resolve,
        now=now,
        allows=lambda key: key != "alumina-news",
    )
    assert not result.entries
    with sqlite3.connect(engine.decisions_path) as db:
        rows = db.execute(
            "SELECT user_id,payload FROM recommendation_decisions ORDER BY rowid"
        ).fetchall()
    assert {row[0] for row in rows} == {"alice", "bob"}
    payload = json.loads(rows[-2][1])
    assert len(payload["candidates"]) == 3 and payload["experiment"] is None
    assert len({row["source_key"] for row in payload["profile"]}) == 2
    with pytest.raises(ValueError, match="登录"):
        engine.rank(candidates, "", resolve)


@contextmanager
def recommendation_frontend(tmp_path):
    with running_frontend(tmp_path, require_login=True) as world:
        rss = (
            "<rss><channel><title>Recommendations</title>"
            + "".join(
                f"<item><title>{title}</title><link>https://test/{key}</link>"
                f"<description>{title}{' article body content.' * 8}</description></item>"
                for key, title in (
                    ("alumina", "Alumina seed"),
                    ("alumina-news", "Alumina news"),
                    ("model", "Model seed"),
                    ("model-news", "Model news"),
                )
            )
            + "</channel></rss>"
        )
        with open_archive(world.root) as archive:
            record = RawRecord.create(
                channel_id="chan-feed",
                endpoint="https://test/recommendations.rss",
                content=rss.encode(),
            )
            archive.put(record, rss.encode())
            sources = tuple(
                ArchiveEntriesAccess(archive).brief_sources(record.raw_id).values()
            )
        store = BriefStore(world.db_path)
        store.record(
            sources,
            {
                s.key: ArticleBrief(
                    "氧化铝新闻" if "Alumina" in s.title else "模型新闻",
                    "氧化铝工厂产量。" if "Alumina" in s.title else "模型记忆研究。",
                )
                for s in sources
            },
            model="fixture",
        )
        store.close()
        index.build_index(
            world.db_path, world.root / "brief-search.sqlite", encoder=Encoder()
        )
        yield world


def send_read(opener, world, card, *, ms=30000):
    payload = event(card["data-raw-id"], ms=ms, view=uuid.uuid4().hex)
    payload.update(
        raw_sha256=card["data-raw-sha256"],
        char_start=int(card["data-char-start"]),
        char_end=int(card["data-char-end"]),
    )
    req = Request(
        world.app.base_url + "/events",
        json.dumps(payload).encode(),
        {"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=10) as response:
        assert response.status == 200


def test_real_http_online_interests_are_scoped_to_login_and_keep_navigation(tmp_path):
    with recommendation_frontend(tmp_path) as world:
        alice, _ = client()
        bob, _ = client()
        assert request(alice, world.app, "/feed?view=recommended")[0] == 303
        for opener, name in ((alice, "alice"), (bob, "bob")):
            assert request(opener, world.app, "/login", {"name": name})[0] == 303
        status, page, _ = request(alice, world.app, "/feed?view=recommended&limit=1")
        assert status == 200 and "先看看近期内容" in page
        soup = BeautifulSoup(page, "html.parser")
        menu = soup.select(".source-menu nav a")
        assert [a.get_text() for a in menu[:2]] == ["全部来源", "为你推荐"]
        assert menu[1]["aria-current"] == "page" and "aria-current" not in menu[0].attrs
        assert "view=" not in menu[0]["href"]
        assert soup.select_one("[data-feed-context]")["data-recommendation-id"]
        _, original, _ = request(alice, world.app, "/feed")
        cards = BeautifulSoup(original, "html.parser").select("article.item")
        alumina = next(c for c in cards if "Alumina seed" in c.get_text())
        model = next(c for c in cards if "Model seed" in c.get_text())
        send_read(alice, world, alumina, ms=15000)
        send_read(bob, world, model, ms=60000)
        for opener, expected in ((alice, "Alumina news"), (bob, "Model news")):
            status, page, _ = request(
                opener, world.app, "/feed?view=recommended&limit=1"
            )
            assert status == 200
            soup = BeautifulSoup(page, "html.parser")
            assert expected in soup.select_one("article.item").get_text()
            next_page = soup.select_one('a[aria-label="下一页"]')["href"]
            assert parse_qs(urlsplit(next_page).query)["view"] == ["recommended"]
            for link in soup.select(".quick a, .compact-pager a"):
                assert "view=recommended" in link["href"]
            from atlas.webui.app import _validate_return_to

            _validate_return_to(
                soup.select_one('form.act-valid [name="return_to"]')["value"]
            )
            back = parse_qs(urlsplit(soup.select_one("a.title")["href"]).query)[
                "back_to"
            ][0]
            assert "view=recommended" in back
        send_read(alice, world, model, ms=120000)
        _, updated, _ = request(alice, world.app, "/feed?view=recommended&limit=1")
        assert (
            "Model news"
            in BeautifulSoup(updated, "html.parser")
            .select_one("article.item")
            .get_text()
        )
        for suffix in (
            "view=bad",
            "view=all&view=recommended",
            "view=recommended&user_id=bob",
            "view=recommended&granularity=document",
        ):
            assert request(alice, world.app, "/feed?" + suffix)[0] == 400
        # Keyword filtering works inside the recommendation category without inference.
        status, page, _ = request(
            alice,
            world.app,
            "/feed?"
            + urlencode({"view": "recommended", "q": "模型", "search_mode": "keyword"}),
        )
        assert status == 200
        soup = BeautifulSoup(page, "html.parser")
        assert len(soup.select("article.item")) == 2
        assert soup.select_one('[name="view"]')["value"] == "recommended"
        assert "view=recommended" in soup.select_one(".feed-search-note a")["href"]


@pytest.mark.parametrize("width", [320, 1366])
def test_recommendation_menu_browser_selection_and_no_overflow(tmp_path, width):
    api = pytest.importorskip("playwright.sync_api")
    with recommendation_frontend(tmp_path) as world, api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = browser.new_context(viewport={"width": width, "height": 900})
            token = world.app.server.users.login("browser-user")
            context.add_cookies(
                [{"name": "atlas_session", "value": token, "url": world.app.base_url}]
            )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(world.app.base_url + "/feed")
            page.locator(".source-menu summary").click()
            option = page.locator(".source-menu a.recommendation-link")
            assert option.bounding_box()["height"] >= 44
            option.click()
            page.wait_for_url("**view=recommended**")
            assert page.locator(".source-menu summary").inner_text() == "为你推荐"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("button", name="搜索文章", exact=True).click()
            page.get_by_role("searchbox", name="搜索文章", exact=True).fill("模型")
            page.get_by_label("搜索方式", exact=True).select_option("keyword")
            page.get_by_role("button", name="搜索", exact=True).click()
            page.wait_for_url("**q=**")
            assert "view=recommended" in page.url
            page.get_by_role("button", name="搜索文章", exact=True).click()
            page.get_by_role("link", name="清除搜索", exact=True).click()
            page.wait_for_url(lambda url: "q=" not in url)
            assert "view=recommended" in page.url
            page.locator(".source-menu summary").click()
            page.locator(".source-menu nav").get_by_role(
                "link", name="全部来源", exact=True
            ).click()
            page.wait_for_url(lambda url: "view=" not in url)
            assert page.locator(".source-menu summary").inner_text() == "全部来源"
            assert not errors
        finally:
            browser.close()
