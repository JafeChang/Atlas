"""Refreshing recommendations makes room for articles the user has not seen."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.request import Request

import pytest
from bs4 import BeautifulSoup

from atlas.analytics import EventStore
from atlas.recommendations import STRATEGY, RecommendationEngine, read_profile
from tests.test_brief_search import Encoder, put, seeded
from tests.test_recommendations import candidate, event, recommendation_frontend
from tests.test_webui_users import client, request


def impression(key, *, view=None, at=None, **fields):
    payload = event(key, view=view or uuid.uuid4().hex, at=at)
    payload.pop("read_ms")
    payload.pop("scroll_pct")
    return {
        **payload,
        "event_type": "list_impression",
        "page": "/feed",
        "visible_ms": 1250,
        "strategy": STRATEGY,
        **fields,
    }


def resolve(raw, *_anchor):
    return SimpleNamespace(key=raw, url="https://test/" + raw)


def test_profile_counts_actual_views_once_without_treating_exposure_as_interest(
    tmp_path,
):
    path = tmp_path / "events.db"
    store = EventStore(path)
    now = datetime.now(UTC)
    for _ in range(3):
        store.append_event(impression("alumina", view="same", at=now), user_id="alice")
    store.append_event(
        impression("alumina", at=now - timedelta(days=1)), user_id="alice"
    )
    store.append_event(impression("model", at=now), user_id="bob")
    store.append_event(impression("anonymous", at=now))
    for key, fields in (
        ("served", {"event_type": "list_served", "position": 0}),
        ("legacy", {"collection_version": "v1"}),
        ("unanchored", {"raw_sha256": "b" * 64}),
        ("old", {"occurred_at": (now - timedelta(days=8)).isoformat()}),
        ("future", {"occurred_at": (now + timedelta(days=1)).isoformat()}),
    ):
        store.append_event(impression(key, at=now, **fields), user_id="alice")

    def verified(raw, sha, *anchor):
        return resolve(raw, *anchor) if sha == "a" * 64 else None

    before = path.read_bytes()
    profile = read_profile(path, "alice", verified, now=now)
    assert profile.impression_weights == (("https://test/alumina", 1.5),)
    assert not profile.interests and not profile.seen_urls
    assert profile.cursor > 0
    assert dict(read_profile(path, "bob", verified, now=now).impression_weights) == {
        "https://test/model": 1
    }
    assert not read_profile(path, "empty", verified, now=now).impression_weights
    assert not read_profile(
        path, "alice", verified, now=now + timedelta(days=8)
    ).impression_weights
    assert path.read_bytes() == before


def test_refresh_demotes_exposed_url_for_only_its_user_and_recovers_over_time(tmp_path):
    from atlas.brief_search_cli import build_index

    source, target = seeded(tmp_path)
    build_index(source, target, encoder=Encoder())
    now = datetime.now(UTC)
    events = tmp_path / "events.db"
    engine = RecommendationEngine(target, events, tmp_path / "decisions.db")
    # The exposed article is newer, so it should return to the top after cooldown.
    candidates = [
        candidate("alumina", now),
        candidate("model", now - timedelta(hours=1)),
    ]
    assert (
        engine.rank(candidates, "alice", resolve, now=now).entries[0].raw_id
        == "raw-alumina"
    )
    EventStore(events).append_event(impression("alumina", at=now), user_id="alice")
    updated = engine.rank(candidates, "alice", resolve, now=now)
    assert updated.entries[0].raw_id == "raw-model"
    assert updated.profile_size == 0
    assert engine.rank(candidates, "alice", resolve, now=now).entries == updated.entries
    assert (
        engine.rank(candidates, "bob", resolve, now=now).entries[0].raw_id
        == "raw-alumina"
    )
    reopened = RecommendationEngine(target, events, engine.decisions_path)
    assert (
        reopened.rank(candidates, "alice", resolve, now=now).entries == updated.entries
    )
    assert (
        reopened.rank(candidates, "alice", resolve, now=now + timedelta(days=8))
        .entries[0]
        .raw_id
        == "raw-alumina"
    )


def test_personalized_refresh_keeps_topic_interest_and_handles_rearchived_urls(
    tmp_path,
):
    from atlas.brief_search_cli import build_index

    source, target = seeded(tmp_path)
    for key in ("alumina-seed", "alumina-news", "alumina-rearchived"):
        put(source, key, "氧化铝", "氧化铝行业新闻。")
    build_index(source, target, encoder=Encoder())
    now = datetime.now(UTC)
    events = tmp_path / "events.db"
    store = EventStore(events)
    store.append_event(event("alumina-seed", ms=120000, at=now), user_id="alice")
    engine = RecommendationEngine(target, events, tmp_path / "decisions.db")
    candidates = [
        candidate("alumina", now),
        candidate("alumina-news", now - timedelta(hours=1)),
        candidate("model", now),
    ]
    assert (
        engine.rank(candidates, "alice", resolve, now=now).entries[0].raw_id
        == "raw-alumina"
    )
    for _ in range(5):
        store.append_event(impression("alumina", at=now), user_id="alice")
    # A fresh archive occurrence of the same URL must retain its exposure history.
    candidates.append(candidate("alumina-rearchived", now, url="https://test/alumina"))
    updated = engine.rank(candidates, "alice", resolve, now=now)
    assert updated.entries[0].raw_id == "raw-alumina-news"
    assert updated.profile_size == 1 and len(updated.entries) == 3
    assert [
        s.key for s in read_profile(events, "alice", resolve, now=now).interests
    ] == ["alumina-seed"]
    with sqlite3.connect(engine.decisions_path) as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM recommendation_decisions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert payload["strategy"] == STRATEGY
    penalties = {
        c["source_key"]: c["impression_penalty"] for c in payload["candidates"]
    }
    assert penalties["alumina-rearchived"] == 0.6
    assert penalties["alumina-news"] == penalties["model"] == 0


def test_http_refresh_uses_acknowledged_impressions_and_keeps_unseen_results(tmp_path):
    with recommendation_frontend(tmp_path) as world:
        alice, _ = client()
        bob, _ = client()
        for opener, name in ((alice, "alice"), (bob, "bob")):
            assert request(opener, world.app, "/login", {"name": name})[0] == 303
        path = "/feed?view=recommended&limit=1"

        def top(opener):
            status, page, headers = request(opener, world.app, path)
            assert status == 200 and headers["Cache-Control"] == "no-store"
            soup = BeautifulSoup(page, "html.parser")
            assert soup.select_one("[data-feed-context]")["data-strategy"] == STRATEGY
            return soup.select_one("article.item")

        first = top(alice)
        first_key = (first["data-raw-id"], first["data-char-start"])
        payload = impression(
            first["data-raw-id"],
            raw_sha256=first["data-raw-sha256"],
            char_start=int(first["data-char-start"]),
            char_end=int(first["data-char-end"]),
        )

        def post(payload):
            req = Request(
                world.app.base_url + "/events",
                json.dumps(payload).encode(),
                {"Content-Type": "application/json"},
            )
            with alice.open(req, timeout=10) as response:
                assert response.status == 200

        post(
            {
                **payload,
                "event_id": uuid.uuid4().hex,
                "event_type": "list_served",
                "position": 0,
            }
        )
        same = top(alice)
        assert (same["data-raw-id"], same["data-char-start"]) == first_key
        post(payload)
        refreshed = top(alice)
        assert (refreshed["data-raw-id"], refreshed["data-char-start"]) != first_key
        unchanged = top(alice)
        assert unchanged["data-char-start"] == refreshed["data-char-start"]
        other = top(bob)
        assert (other["data-raw-id"], other["data-char-start"]) == first_key


def test_browser_reload_replaces_actually_visible_recommendation(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    with (
        recommendation_frontend(tmp_path) as world,
        api.sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 375, "height": 800})
            page.goto(world.app.base_url + "/login")
            page.locator("#name").fill("alice")
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url("**/feed")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/events")
                    and response.request.post_data_json.get("event_type")
                    == "list_impression"
                    and response.request.post_data_json.get("recommendation_id")
                    and response.status == 200
                )
            ):
                page.goto(world.app.base_url + "/feed?view=recommended&limit=1")
                first = page.locator("article.item").get_attribute("data-char-start")
            page.reload()
            assert (
                page.locator("article.item").get_attribute("data-char-start") != first
            )
        finally:
            browser.close()


def test_browser_impressions_do_not_wait_behind_the_whole_served_page(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    with (
        recommendation_frontend(tmp_path) as world,
        api.sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 375, "height": 800})
            page.request.post(world.app.base_url + "/login", form={"name": "alice"})
            held, posted = [], []

            def collect(route):
                kind = route.request.post_data_json["event_type"]
                posted.append(kind)
                if kind == "list_served" and not held:
                    held.append(route)
                else:
                    route.continue_()

            page.route("**/events", collect)
            page.goto(world.app.base_url + "/feed?view=recommended")
            page.wait_for_function("""() => Object.keys(localStorage).some(key =>
                key.startsWith('atlas_events_v2:') &&
                JSON.parse(localStorage.getItem(key)).event_type === 'list_impression')""")
            assert held
            first_served = posted.index("list_served")
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/events")
                    and response.request.post_data_json["event_type"]
                    == "list_impression"
                    and response.status == 200
                )
            ):
                held[0].continue_()
            assert posted[first_served + 1] == "list_impression"
        finally:
            browser.close()
