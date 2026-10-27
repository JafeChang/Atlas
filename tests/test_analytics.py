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
