from datetime import UTC, datetime, timedelta

import pytest
import requests
from bs4 import BeautifulSoup

from atlas.aluminium import INDUSTRY_ID, INDUSTRY_NAME
from atlas.aluminium.sources import channel_for, import_sources, load_manifest
from atlas.aluminium.store import AluminiumStore, has_aluminium, source_id
from atlas.aluminium.ui import SourceManagement
from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.registry import Channel, FetchSpec, Industry, open_store
from atlas.webapp import build_webapp
from tests.test_aluminium import MANIFEST


def seed_registry(tmp_path, *, catalogue=False, alias=False):
    db = tmp_path / "atlas.db"
    industries = [
        Industry(id="machine-learning", name="机器学习", enabled=True),
        Industry(id="science", name="科学研究", enabled=True),
    ]
    channels = [
        Channel(
            id="openai-blog",
            industry_id="machine-learning",
            type="rss",
            endpoint="https://example.test/ai.xml?a=1&b=2",
            fetch_spec=FetchSpec(type="rss"),
            interval_seconds=3600,
            enabled=True,
            tags=("ai",),
        ),
        Channel(
            id="hacker-news-frontpage",
            industry_id="machine-learning",
            type="json_api",
            endpoint="https://example.test/news.json",
            fetch_spec=FetchSpec(type="json_api", list_path="hits", title_path="title"),
            interval_seconds=1800,
            enabled=False,
        ),
        Channel(
            id="science-feed",
            industry_id="science",
            type="atom",
            endpoint="https://example.test/science.atom",
            fetch_spec=FetchSpec(type="atom"),
            interval_seconds=7200,
            enabled=True,
        ),
    ]
    if alias:
        industries.append(Industry(id=INDUSTRY_ID, name=INDUSTRY_NAME, enabled=True))
        channels.append(
            Channel(
                **(
                    channel_for(load_manifest(MANIFEST).sources[0]).payload()
                    | {"id": "existing-company"}
                )
            )
        )
    with open_store(db, author="test", industries=industries, channels=channels):
        pass
    if catalogue:
        import_sources(MANIFEST, db)
    return db


def test_registry_only_sources_render_without_creating_aluminium_tables(tmp_path):
    db = seed_registry(tmp_path)
    before = db.read_bytes()
    assert not has_aluminium(db)
    soup = BeautifulSoup(SourceManagement(db).render(), "html.parser")
    groups = soup.select(".source-industry-group")
    assert [
        (g["data-industry-group"], len(g.select(".source-row"))) for g in groups
    ] == [("AI 信源", 2), ("科学研究", 1)]
    row = soup.select_one('[data-source-id="openai-blog"]')
    assert row.select_one(".source-name").get_text() == "OpenAI Blog"
    assert (
        row.select_one(".source-name")["href"] == "https://example.test/ai.xml?a=1&b=2"
    )
    assert row["data-priority"] == "unassigned"
    assert row.select_one(".source-priority").get_text() == "—"
    assert row["data-state"] == "已配置"
    assert row.select_one(".source-switch").get("disabled") is None
    assert not soup.select("#intelligence, .source-events-link")
    assert not has_aluminium(db) and db.read_bytes() == before


def test_all_industries_render_without_duplicates_or_discarding_pending_sources(
    tmp_path,
):
    db = seed_registry(tmp_path, catalogue=True, alias=True)
    service = SourceManagement(db, lock_path=tmp_path / "lock")
    before = db.read_bytes()
    for _ in range(2):
        soup = BeautifulSoup(service.render(), "html.parser")
        assert len(soup.select(".source-row")) == 40
        groups = soup.select(".source-industry-group")
        assert [
            (g["data-industry-group"], len(g.select(".source-row"))) for g in groups
        ] == [("AI 信源", 2), (INDUSTRY_NAME, 37), ("科学研究", 1)]
        assert len({r["data-source-id"] for r in soup.select(".source-row")}) == 40
        assert len(soup.select('[data-mode="paid"] .source-switch[disabled]')) == 5
        assert not soup.select('[data-source-id="existing-company"]')
    assert db.read_bytes() == before
    with open_store(db, author="check") as registry:
        version = str(registry.current_version)
    # A registered channel ID cannot bypass catalogue verification.
    with pytest.raises(ValueError, match="验证"):
        service.toggle("existing-company", "1", version, "test")
    sid = source_id(load_manifest(MANIFEST).sources[0].url)
    with AluminiumStore(db) as store:
        store.probe(sid, {"collectible": True, "reason": None})
    service.toggle("existing-company", "1", version, "test")
    with open_store(db, author="check") as registry:
        assert next(
            c for c in registry.current.channels if c.id == "existing-company"
        ).enabled


def test_registered_status_uses_archive_facts_without_claiming_verification(tmp_path):
    db = seed_registry(tmp_path)
    base = datetime(2026, 10, 29, 0, tzinfo=UTC)
    with open_archive(tmp_path) as archive:
        for status, moment in [(200, base), (503, base + timedelta(hours=1))]:
            content = f"Saved HTTP {status} response".encode()
            raw = RawRecord.create(
                channel_id="openai-blog",
                endpoint="https://example.test/ai.xml",
                content=content,
                fetched_at=moment,
                http_status=status,
            )
            archive.put(raw, content)
    page = BeautifulSoup(SourceManagement(db).render(), "html.parser")
    row = page.select_one('[data-source-id="openai-blog"]')
    assert (
        row["data-state"] == "不可用"
        and "10-29 08:00" in row.select_one(".source-time").get_text()
    )
    details = page.select_one("#source-detail-openai-blog")
    assert "HTTP 503" in details.get_text() and "未单独验证" in details.get_text()
    assert "非完整尝试记录" in details.get_text()
    # A historical successful response supports only archived status, not verification.
    with open_archive(tmp_path) as archive:
        content = b"Another saved response"
        raw = RawRecord.create(
            channel_id="hacker-news-frontpage",
            endpoint="https://example.test/news.json",
            content=content,
            fetched_at=base,
            http_status=200,
        )
        archive.put(raw, content)
    page = BeautifulSoup(SourceManagement(db).render(), "html.parser")
    assert (
        page.select_one('[data-source-id="hacker-news-frontpage"]')["data-state"]
        == "已归档"
    )


def test_registered_source_toggle_http_and_version_checks_preserve_history(tmp_path):
    db = seed_registry(tmp_path, catalogue=True)
    with build_webapp(
        tmp_path, db_path=db, industry_provider=lambda: (), require_login=False
    ) as app:
        app.server.source_management = SourceManagement(db, lock_path=tmp_path / "lock")
        with requests.Session() as session:
            session.trust_env = False
            soup = BeautifulSoup(
                session.get(app.base_url + "/sources").text, "html.parser"
            )
            row = soup.select_one('[data-source-id="hacker-news-frontpage"]')
            fields = {i["name"]: i["value"] for i in row.select("form input")}
            assert fields["enabled"] == "1"
            result = session.post(
                app.base_url + "/sources/toggle", data=fields, allow_redirects=False
            )
            assert result.status_code == 303
            stale = session.post(app.base_url + "/sources/toggle", data=fields)
            assert stale.status_code == 409 and "配置已更新" in stale.text
            with open_store(db, author="check") as registry:
                assert next(
                    c
                    for c in registry.current.channels
                    if c.id == "hacker-news-frontpage"
                ).enabled
                assert next(
                    c for c in registry.current.channels if c.id == "openai-blog"
                ).enabled
                current = str(registry.current_version)
            with AluminiumStore(db, read_only=True) as store:
                assert len(store.sources()) == 37
            with pytest.raises(ValueError, match="未知信源"):
                app.server.source_management.toggle(
                    "missing-source", "1", current, "test"
                )


def test_industry_filters_and_registered_toggle_in_browser(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    db = seed_registry(tmp_path, catalogue=True)
    with (
        build_webapp(
            tmp_path, db_path=db, industry_provider=lambda: (), require_login=False
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
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(app.base_url + "/sources")
            assert page.locator(".source-row:visible").count() == 40
            page.locator("#source-industry").select_option("AI 信源")
            assert page.locator(".source-row:visible").count() == 2
            assert page.locator(".source-industry-group:visible").count() == 1
            assert (
                page.locator("[data-source-filter=enabled] .source-count").inner_text()
                == "1"
            )
            page.locator("[data-source-filter=pending]").click()
            assert page.locator(".source-industry-group:visible").count() == 0
            assert page.locator(".source-filter-empty").is_visible()
            page.get_by_role("button", name="清除筛选").click()
            page.locator("#source-industry").select_option("AI 信源")
            page.locator("#source-mode").select_option("api")
            assert page.locator(".source-row:visible").count() == 1
            page.locator(".source-switch:visible").click()
            page.wait_for_load_state()
            assert page.locator("#source-industry").input_value() == "AI 信源"
            assert (
                page.locator(".source-switch:visible").get_attribute("aria-checked")
                == "true"
            )
            page.locator("#source-mode").select_option("")
            page.locator("#source-industry").select_option(INDUSTRY_NAME)
            assert page.locator(".source-row:visible").count() == 37
            page.locator("#source-mode").select_option("web")
            assert page.locator(".source-row:visible").count() == 26
            page.locator("#source-priority").select_option("unassigned")
            assert page.locator(".source-row:visible").count() == 0
            assert page.locator(".source-filter-empty").is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
        finally:
            browser.close()
