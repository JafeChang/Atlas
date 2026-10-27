"""Interaction events travel from the browser through HTTP into a separate database."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

import pytest

from tests.test_webui_entries_render import running_frontend


def _post(app, event, *, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(app.base_url + "/events", json.dumps(event).encode(), headers)
    try:
        result = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        result = exc
    with result:
        return result.status


def _event():
    return {"event_id": "event-1", "visitor_id": "visitor-1", "session_id": "session-1",
            "view_id": "view-1", "event_type": "list_impression", "page": "/feed",
            "occurred_at": "2026-10-27T10:00:00Z", "raw_id": "raw-feed-render",
            "entry_id": "entry-1", "raw_sha256": "a" * 64, "char_start": 2,
            "char_end": 9, "position": 3}


def _rows(path):
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM interaction_events ORDER BY rowid")]


def test_http_validation_and_persistence_are_separate_from_labels(tmp_path):
    with running_frontend(tmp_path) as world:
        path = world.db_path.with_name("analytics.db")
        assert not path.exists()
        assert _post(world.app, {**_event(), "extra": "private"}) == 400
        assert _post(world.app, {**_event(), "position": True}) == 400
        assert _post(world.app, _event(), origin="https://untrusted.test") == 403
        assert not path.exists()
        assert _post(world.app, _event()) == 200
        assert _post(world.app, _event()) == 200  # retry is idempotent
        rows = _rows(path)
        assert len(rows) == 1
        assert rows[0]["raw_id"] == "raw-feed-render"
        assert (rows[0]["char_start"], rows[0]["char_end"], rows[0]["position"]) == (2, 9, 3)
        with sqlite3.connect(world.db_path) as db:
            assert db.execute("SELECT count(*) FROM confirmed_labels").fetchone()[0] == 0
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='interaction_events'").fetchone()


def test_browser_impressions_click_and_detail_are_linked(tmp_path):
    api = pytest.importorskip("playwright.sync_api", reason="browser test requires Playwright")
    with running_frontend(tmp_path) as world, api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        except api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("browser test requires Chromium")
            raise
        try:
            page = browser.new_page(viewport={"width": 900, "height": 500})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(world.app.base_url + "/feed?order=asc&limit=50")
            cards = page.locator("article[data-tracking-list-item]")
            assert cards.count() >= 3
            first = cards.first
            last = cards.last
            last_identity = (last.get_attribute("data-raw-id"), int(last.get_attribute("data-char-start")))
            assert last.bounding_box()["y"] >= 500
            page.wait_for_timeout(500)
            initial = _rows(world.db_path.with_name("analytics.db"))
            assert not any(r["event_type"] == "list_impression" and
                           (r["raw_id"], r["char_start"]) == last_identity for r in initial)
            first.scroll_into_view_if_needed()
            raw_id = first.get_attribute("data-raw-id")
            start = int(first.get_attribute("data-char-start"))
            path = world.db_path.with_name("analytics.db")
            def wait_for(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    rows = _rows(path)
                    if predicate(rows):
                        return rows
                    page.wait_for_timeout(50)
                raise AssertionError(_rows(path))
            wait_for(lambda rows: any(r["event_type"] == "list_impression" and r["raw_id"] == raw_id and r["char_start"] == start for r in rows))
            first.get_by_role("button", name="标记有效", exact=True).click()
            wait_for(lambda rows: any(r["action"] == "label_valid" and r["raw_id"] == raw_id and r["char_start"] == start for r in rows))
            first.locator("a.title").click()
            page.wait_for_url("**/article?**")
            rows = wait_for(lambda rows: any(r["event_type"] == "detail_view" and r["raw_id"] == raw_id and r["char_start"] == start for r in rows))
            impression = next(r for r in rows if r["event_type"] == "list_impression" and r["raw_id"] == raw_id and r["char_start"] == start)
            detail = next(r for r in rows if r["event_type"] == "detail_view" and r["raw_id"] == raw_id and r["char_start"] == start)
            assert impression["session_id"] == detail["session_id"]
            assert impression["visitor_id"] == detail["visitor_id"]
            assert impression["view_id"] != detail["view_id"]
            assert any(r["action"] == "open_detail" and r["view_id"] == impression["view_id"] for r in rows)
            archive_url = page.locator('a[href^="/entry?"]').first.get_attribute("href")
            page.goto(world.app.base_url + archive_url)
            rows = wait_for(lambda rows: any(r["event_type"] == "detail_view" and r["page"] == "/entry" and r["raw_id"] == raw_id for r in rows))
            assert any(r["event_type"] == "detail_view" and r["page"] == "/entry" and r["char_start"] == start for r in rows)
            assert not errors
        finally:
            browser.close()


def test_authenticated_events_use_server_user_ids(tmp_path):
    from tests.test_webui_users import client, request

    def send(opener, app, event):
        req = urllib.request.Request(
            app.base_url + "/events", json.dumps(event).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status

    with running_frontend(tmp_path, require_login=True) as world:
        alice, _ = client()
        alice_other, _ = client()
        bob, _ = client()
        assert send(alice, world.app, _event()) == 401
        assert request(alice, world.app, "/login", {"name": "alice"})[0] == 303
        assert request(alice_other, world.app, "/login", {"name": "alice"})[0] == 303
        assert request(bob, world.app, "/login", {"name": "bob"})[0] == 303
        assert send(alice, world.app, {**_event(), "user_id": "forged"}) == 400
        assert send(alice, world.app, {**_event(), "event_id": "a1"}) == 200
        assert send(alice_other, world.app, {**_event(), "event_id": "a2", "visitor_id": "different-browser"}) == 200
        assert send(bob, world.app, {**_event(), "event_id": "b1"}) == 200
        rows = _rows(world.root / "analytics.db")
        by_event = {row["event_id"]: row for row in rows}
        a_id = by_event["a1"]["user_id"]
        b_id = by_event["b1"]["user_id"]
        assert a_id and b_id and a_id != b_id
        assert by_event["a2"]["user_id"] == a_id
        assert a_id != "alice" and b_id != "bob"
        with sqlite3.connect(world.root / "users.db") as db:
            ids = dict(db.execute("SELECT name, user_id FROM users"))
        assert ids == {"alice": a_id, "bob": b_id}


def test_old_event_database_is_migrated_without_assigning_history(tmp_path):
    from atlas.analytics.store import EventStore

    path = tmp_path / "analytics.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE interaction_events (
            event_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
            occurred_at TEXT NOT NULL, visitor_id TEXT NOT NULL,
            session_id TEXT NOT NULL, view_id TEXT NOT NULL,
            event_type TEXT NOT NULL, page TEXT NOT NULL,
            raw_id TEXT, entry_id TEXT, raw_sha256 TEXT, char_start INTEGER,
            char_end INTEGER, channel_id TEXT, position INTEGER,
            action TEXT, offset INTEGER, limit_count INTEGER, sort_order TEXT,
            payload TEXT NOT NULL)""")
        db.execute("""INSERT INTO interaction_events
            (event_id, received_at, occurred_at, visitor_id, session_id, view_id,
             event_type, page, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("old", "2026-10-27", "2026-10-27", "v", "s", "v", "page_view", "/feed", "{}"))
    store = EventStore(path)
    store.append_event({**_event(), "event_id": "new"}, user_id="user-id")
    rows = {row["event_id"]: row for row in _rows(path)}
    assert rows["old"]["user_id"] is None
    assert rows["new"]["user_id"] == "user-id"
    assert rows["old"]["payload"] == "{}"


def test_recommendation_event_fields_and_account_binding(tmp_path):
    from tests.test_webui_users import client, request

    with running_frontend(tmp_path, require_login=True) as world:
        opener, _ = client()
        assert request(opener, world.app, "/login", {"name": "alice"})[0] == 303
        with sqlite3.connect(world.root / "users.db") as db:
            user_id = db.execute("SELECT user_id FROM users WHERE name='alice'").fetchone()[0]

        def send(event):
            req = urllib.request.Request(world.app.base_url + "/events", json.dumps(event).encode(),
                                         {"Content-Type": "application/json"})
            try:
                response = opener.open(req, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                return response.status

        event = {**_event(), "expected_user_id": "different-user", "event_id": "wrong"}
        assert send(event) == 409
        event.update(expected_user_id=user_id, event_id="served", event_type="list_served",
                     strategy="chronological-v1", collection_version="v2", total=20,
                     order="desc", offset=0, limit=50, filter_mask=3)
        assert send(event) == 200
        assert send({**event, "event_id": "view", "event_type": "list_impression", "visible_ms": 1250}) == 200
        reading = {**event, "event_id": "read", "event_type": "read_progress", "read_ms": 3000,
                   "scroll_pct": 70, "parent_view_id": "feed-view"}
        assert send({**reading, "scroll_pct": 101}) == 400
        assert send(reading) == 200
        rows = {row["event_id"]: row for row in _rows(world.root / "analytics.db")}
        assert set(rows) == {"served", "view", "read"}
        assert rows["served"]["user_id"] == user_id and rows["served"]["strategy"] == "chronological-v1"
        assert rows["served"]["total"] == 20 and rows["served"]["filter_mask"] == 3 and rows["view"]["visible_ms"] == 1250
        assert (rows["read"]["read_ms"], rows["read"]["scroll_pct"], rows["read"]["parent_view_id"]) == (3000, 70, "feed-view")
        assert "expected_user_id" not in json.loads(rows["read"]["payload"])


def test_user_label_projection_preserves_history_and_is_read_only(tmp_path):
    import hashlib

    from atlas.analytics import read_user_labels
    from tests.test_webui_users import client, request

    with running_frontend(tmp_path, require_login=True) as world:
        world.app.labels.record(raw_id="raw-article-render", label_key="valid", label_value="valid", actor="alice")
        world.app.labels.record(raw_id="raw-article-render", label_key="industry", label_value="ai", actor="legacy")
        opener, _ = client()
        assert request(opener, world.app, "/login", {"name": "alice"})[0] == 303
        paths = (world.db_path, world.root / "users.db")
        before = [hashlib.sha256(path.read_bytes()).digest() for path in paths]
        labels = read_user_labels(*paths)
        after = [hashlib.sha256(path.read_bytes()).digest() for path in paths]
        assert before == after
        with sqlite3.connect(paths[1]) as db:
            user_id = db.execute("SELECT user_id FROM users WHERE name='alice'").fetchone()[0]
        assert [(label["label_key"], label["user_id"]) for label in labels] == [("valid", user_id), ("industry", None)]
        assert all("actor" not in label for label in labels)
        assert read_user_labels(tmp_path / "missing.db", paths[1]) == []
        assert not (tmp_path / "missing.db").exists()


def test_browser_qualified_exposure_reading_and_attribution(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    with running_frontend(tmp_path, require_login=True) as world, api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(world.app.base_url + "/login")
            page.locator("#name").fill("alice")
            page.get_by_role("button", name="登录", exact=True).click()
            page.wait_for_url("**/feed")
            card = page.locator('article[data-tracking-list-item]').first
            card.scroll_into_view_if_needed()
            raw_id = card.get_attribute("data-raw-id")
            start = int(card.get_attribute("data-char-start"))
            path = world.root / "analytics.db"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                rows = _rows(path)
                impressions = [r for r in rows if r['event_type'] == 'list_impression' and
                               r['raw_id'] == raw_id and r['char_start'] == start]
                if impressions:
                    break
                page.wait_for_timeout(100)
            assert impressions and impressions[0]['visible_ms'] >= 1000
            served = next(r for r in rows if r['event_type'] == 'list_served' and
                          r['raw_id'] == raw_id and r['char_start'] == start)
            assert served['strategy'] == 'chronological-v1' and served['collection_version'] == 'v2'
            assert served['view_id'] == impressions[0]['view_id'] and served['user_id']
            card.locator('a.title').click()
            page.wait_for_url('**/article?**')
            page.wait_for_timeout(1250)
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                rows = _rows(path)
                reading = [r for r in rows if r['event_type'] == 'read_progress' and r['raw_id'] == raw_id]
                if reading:
                    break
                page.wait_for_timeout(100)
            assert reading and reading[0]['read_ms'] >= 1000
            assert reading[0]['parent_view_id'] == served['view_id']
            assert reading[0]['user_id'] == served['user_id']
        finally:
            browser.close()


def test_browser_retries_without_reassigning_queued_events(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    with running_frontend(tmp_path, require_login=True) as world, api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page()
            page.route('**/events', lambda route: route.abort())
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('alice')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            alice_id = page.locator('meta[name="atlas-user-id"]').get_attribute('content')
            page.wait_for_function("""id => Object.keys(localStorage).some(k => k.startsWith('atlas_events_v2:' + id + ':'))""", arg=alice_id)
            alice_events = page.evaluate("""id => Object.keys(localStorage).filter(k => k.startsWith('atlas_events_v2:' + id + ':')).map(k => JSON.parse(localStorage[k]).event_id)""", alice_id)
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('bob')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            bob_id = page.locator('meta[name="atlas-user-id"]').get_attribute('content')
            assert bob_id != alice_id
            page.unroute('**/events')
            page.evaluate("window.dispatchEvent(new Event('online'))")
            path = world.root / 'analytics.db'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and bob_id not in {row['user_id'] for row in _rows(path)}:
                page.wait_for_timeout(100)
            assert bob_id in {row['user_id'] for row in _rows(path)}
            assert all(row['user_id'] == alice_id for row in _rows(path) if row['event_id'] in alice_events)
            assert page.evaluate("""id => Object.keys(localStorage).some(k => k.startsWith('atlas_events_v2:' + id + ':'))""", arg=alice_id)
            page.goto(world.app.base_url + '/login')
            page.locator('#name').fill('alice')
            page.get_by_role('button', name='登录', exact=True).click()
            page.wait_for_url('**/feed')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and alice_id not in {row['user_id'] for row in _rows(path)}:
                page.wait_for_timeout(100)
            assert alice_id in {row['user_id'] for row in _rows(path)}
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not set(alice_events) <= {row['event_id'] for row in _rows(path)}:
                page.wait_for_timeout(100)
            rows_by_id = {row['event_id']: row for row in _rows(path)}
            assert set(alice_events) <= rows_by_id.keys()
            assert all(rows_by_id[event_id]['user_id'] == alice_id for event_id in alice_events)
        finally:
            browser.close()
