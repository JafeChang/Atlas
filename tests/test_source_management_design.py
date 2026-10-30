import hashlib

import pytest
import requests
from bs4 import BeautifulSoup

from atlas.aluminium import INDUSTRY_ID
from atlas.aluminium.source_page import render_sources
from atlas.aluminium.store import AluminiumStore, source_id
from atlas.aluminium.ui import SourceManagement
from atlas.webapp import build_webapp
from tests.test_aluminium import fixtures


def test_compact_directory_is_read_only_and_priorities_stay_ordered(tmp_path):
    db, sources = fixtures(tmp_path)
    before = hashlib.sha256(db.read_bytes()).digest()
    page = SourceManagement(db).render()
    soup = BeautifulSoup(page, "html.parser")
    rows = soup.select("tr.source-row")
    assert len(rows) == 37
    assert [r["data-priority"] for r in rows] == sorted(s.priority for s in sources)
    assert len({r["data-source-id"] for r in rows}) == 37
    assert len(soup.select("tr.source-detail-row[hidden]")) == 37
    for source in sources:
        row = soup.select_one(f'[data-source-id="{source_id(source.url)}"]')
        assert row.select_one(".source-name").get_text() == source.name
        assert row.select_one(".source-name")["href"] == source.url
        if source.pending_reason:
            assert row.select_one(".source-switch").has_attr("disabled")
            assert not row.select("form")
    assert soup.select_one("#source-search")
    assert soup.select_one("#source-mode") and soup.select_one("#source-priority")
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_empty_directory_and_conflict_feedback_do_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    service = SourceManagement(path)
    page = service.render()
    assert "尚未导入行业信源" in page and "共 0 个来源" in page
    assert not path.exists()
    page = service.render('<script>alert("conflict")</script>')
    soup = BeautifulSoup(page, "html.parser")
    assert (
        soup.select_one('[role="alert"]').get_text()
        == '<script>alert("conflict")</script>'
    )
    assert not path.exists()


def test_details_keep_original_metadata_and_escape_external_text(tmp_path):
    db, _ = fixtures(tmp_path)
    with AluminiumStore(db, read_only=True) as store:
        row = store.status()[0]
    row.update(
        name="Example <script>alert(1)</script>",
        url="https://example.test/news?a=1&b=2",
        region='Country <img src=x onerror="bad()">',
        last_success_at="2026-10-29T06:38:00+00:00",
        last_attempt=None,
        probe={
            "collectible": False,
            "reason": '<svg onload="bad()">',
            "tested_at": "2026-10-29T06:37:00+00:00",
        },
    )
    soup = BeautifulSoup(render_sources([row], {}, 1, []), "html.parser")
    assert soup.select_one(".source-name")["href"] == row["url"]
    assert not soup.select(".source-table script, .source-table img, .source-table svg")
    assert "<svg" in soup.select_one(".source-reason").get_text()
    assert "10-29 14:38" in soup.select_one(".source-time").get_text()
    assert row["focus"] in soup.select_one(".source-metadata").get_text()
    assert row["region"] in soup.select_one(".source-metadata").get_text()
    assert row["status"] in soup.select_one(".source-metadata").get_text()


def test_compact_toggle_form_preserves_version_checks_and_status(tmp_path):
    db, sources = fixtures(tmp_path)
    source = next(s for s in sources if s.mode == "web" and not s.pending_reason)
    sid = source_id(source.url)
    with AluminiumStore(db) as store:
        store.probe(sid, {"collectible": True, "reason": None})
    with build_webapp(
        tmp_path,
        db_path=db,
        industry_provider=lambda: (INDUSTRY_ID,),
        require_login=False,
    ) as app:
        app.server.source_management = SourceManagement(db, lock_path=tmp_path / "lock")
        with requests.Session() as session:
            session.trust_env = False
            page = session.get(app.base_url + "/sources")
            assert page.status_code == 200
            soup = BeautifulSoup(page.text, "html.parser")
            row = soup.select_one(f'[data-source-id="{sid}"]')
            form = {i["name"]: i["value"] for i in row.select("form input")}
            assert form["enabled"] == "1"
            assert row.select_one(".source-switch")["aria-checked"] == "false"
            response = session.post(
                app.base_url + "/sources/toggle", data=form, allow_redirects=False
            )
            assert response.status_code == 303
            soup = BeautifulSoup(
                session.get(app.base_url + "/sources").text, "html.parser"
            )
            assert (
                soup.select_one(f'[data-source-id="{sid}"] .source-switch')[
                    "aria-checked"
                ]
                == "true"
            )
            stale = session.post(app.base_url + "/sources/toggle", data=form)
            assert stale.status_code == 409 and "配置已更新" in stale.text
            assert 'class="source-row"' in stale.text


def test_source_controls_and_filters_in_browser(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    db, sources = fixtures(tmp_path)
    source = next(s for s in sources if s.mode == "web" and not s.pending_reason)
    sid = source_id(source.url)
    with AluminiumStore(db) as store:
        store.probe(sid, {"collectible": True, "reason": None})
    with (
        build_webapp(
            tmp_path,
            db_path=db,
            industry_provider=lambda: (INDUSTRY_ID,),
            require_login=False,
        ) as app,
        api.sync_playwright() as p,
    ):
        app.server.source_management = SourceManagement(db, lock_path=tmp_path / "lock")
        try:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        except api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("需要 Chromium")
            raise
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(app.base_url + "/sources")
            page.locator("#source-search").fill(source.name)
            assert page.locator(".source-row:visible").count() == 1
            page.locator(".source-detail-toggle:visible").click()
            assert page.locator(".source-detail-row:visible").count() == 1
            page.locator(".source-switch:not(:disabled):visible").click()
            page.wait_for_load_state()
            assert page.locator(".source-row:visible").count() == 1
            assert (
                page.locator(f'[data-source-id="{sid}"] .source-switch').get_attribute(
                    "aria-checked"
                )
                == "true"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()
