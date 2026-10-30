"""来源名称只影响展示；筛选、埋点和历史身份继续使用渠道 ID。"""

from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

from atlas.aluminium.sources import import_sources, load_manifest
from atlas.aluminium.store import source_id
from atlas.aluminium.ui import SourceManagement
from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.feed import FeedQuery
from atlas.webapp import build_webapp
from atlas.webui.pages import _render_source_menu, _render_source_tabs

MANIFEST = (
    Path(__file__).resolve().parents[1] / "config/overseas_aluminium_sources.json"
)


def test_named_sources_render_filter_and_detail_without_changing_identity(tmp_path):
    db = tmp_path / "atlas.db"
    import_sources(MANIFEST, db)
    source = next(
        s
        for s in load_manifest(MANIFEST).sources
        if s.name == "INALUM Official English"
    )
    channel = source_id(source.url)
    content = b"<html><title>Smelter commissioning</title><body><h1>Smelter commissioning</h1><p>Primary aluminium smelter capacity update.</p></body></html>"
    raw = RawRecord.create(
        channel_id=channel,
        endpoint=source.url,
        content=content,
        fetched_at=datetime.now(UTC),
        http_status=200,
    )
    with open_archive(tmp_path) as archive:
        archive.put(raw, content)
    with build_webapp(
        tmp_path, db_path=db, industry_provider=lambda: (), require_login=False
    ) as app:
        before = db.read_bytes()
        client = requests.Session()
        client.trust_env = False
        response = client.get(app.base_url + "/feed", params={"channel": channel})
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        assert soup.select_one(".source-menu summary span").get_text() == source.name
        assert soup.select_one(".source-menu summary span")["title"] == source.name
        current = soup.select_one(".source-menu nav a[aria-current=page]")
        assert current.get_text() == source.name
        assert parse_qs(urlsplit(current["href"]).query)["channel"] == [channel]
        assert not any(
            a.get_text().startswith("opa-") for a in soup.select(".source-menu nav a")
        )
        card = soup.select_one("article.entry")
        assert card["data-channel-id"] == channel
        assert card.select_one(".feed-item-meta span").get_text() == source.name
        assert card["data-raw-id"] == raw.raw_id
        detail = client.get(app.base_url + card.select_one("a.title")["href"])
        assert detail.status_code == 200
        article = BeautifulSoup(detail.text, "html.parser")
        assert (
            article.select_one(".reader-heading .meta span").get_text() == source.name
        )
        assert (
            article.select_one(".reader-footer span").get_text()
            == "来源 " + source.name
        )
        assert (
            article.select_one("[data-tracking-detail]")["data-channel-id"] == channel
        )
        assert db.read_bytes() == before


def test_readable_names_are_escaped_and_unknown_sources_still_work():
    name = 'EGA <script>alert("x")</script> & News'
    names = {"opa-known": name}
    query = FeedQuery(channels=("opa-known", "legacy-source"))
    for render in (_render_source_menu, _render_source_tabs):
        soup = BeautifulSoup(
            render(query, ["opa-known", "legacy-source"], names), "html.parser"
        )
        assert soup.find("script") is None
        links = {a.get_text(): a for a in soup.select("nav a")}
        assert name in links and "legacy-source" in links
        assert parse_qs(urlsplit(links[name]["href"]).query)["channel"] == ["opa-known"]
        assert parse_qs(urlsplit(links["legacy-source"]["href"]).query)["channel"] == [
            "legacy-source"
        ]


def test_loading_names_is_read_only_and_legacy_store_does_not_get_created(tmp_path):
    absent = tmp_path / "absent.db"
    assert SourceManagement(absent).channel_names() == {}
    assert not absent.exists()
    db = tmp_path / "atlas.db"
    import_sources(MANIFEST, db)
    before = db.read_bytes()
    names = SourceManagement(db).channel_names()
    assert len(names) == 29
    assert "Alba News" in names.values() and "Hydro News" in names.values()
    assert db.read_bytes() == before


def test_menu_groups_sources_and_keeps_selection_and_links():
    names = {"legacy-ai": "AI News", "opa-known": "Alba News"}
    groups = {"legacy-ai": "AI 信源", "opa-known": "海外电解铝"}
    query = FeedQuery(channels=("opa-known",))
    page = BeautifulSoup(
        _render_source_menu(
            query, ["legacy-ai", "opa-known", "archived-unknown"], names, groups
        ),
        "html.parser",
    )
    sections = page.select(".source-menu-group")
    assert [s.select_one(".source-menu-group-title").get_text() for s in sections] == [
        "AI 信源（1）",
        "海外电解铝（1）",
        "其他来源（1）",
    ]
    assert [a.get_text() for a in sections[0].select("a")] == ["AI News"]
    assert [a.get_text() for a in page.select("nav > a")] == [
        "全部来源", "AI全部", "海外电解铝全部", "其他来源全部",
    ]
    selected = sections[1].select_one("a[aria-current=page]")
    assert selected.get_text() == "Alba News"
    assert parse_qs(urlsplit(selected["href"]).query)["channel"] == ["opa-known"]
    assert sections[2].select("a")[-1].get_text() == "archived-unknown"
    assert page.select_one("nav > a").get_text() == "全部来源"
    assert "channel" not in parse_qs(urlsplit(page.select_one("nav > a")["href"]).query)
    assert all(page.find(id=s["aria-labelledby"]) for s in sections)
    # 分组名称同样来自数据，必须作为文本展示。
    hostile = BeautifulSoup(
        _render_source_menu(
            query, ["opa-known"], names, {"opa-known": "<script>alert(1)</script>"}
        ),
        "html.parser",
    )
    assert hostile.find("script") is None


def test_registry_grouping_is_read_only_and_used_by_http_page(tmp_path):
    from atlas.aluminium.sources import channel_for
    from atlas.registry import Industry, open_store
    from atlas.registry.schema import Channel, FetchSpec

    db = tmp_path / "atlas.db"
    sources = load_manifest(MANIFEST).sources[:2]
    source = sources[0]
    with open_store(
        db,
        author="test",
        industries=(
            Industry(id="machine-learning", name="机器学习", enabled=True),
            Industry(id="computer-vision", name="计算机视觉", enabled=True),
        ),
        channels=tuple(
            Channel(
                id=channel_id,
                industry_id=industry_id,
                type="rss",
                endpoint=f"https://example.test/{channel_id}/rss",
                fetch_spec=FetchSpec(type="rss"),
                interval_seconds=3600,
                enabled=True,
            )
            for channel_id, industry_id in (
                ("legacy-ai", "machine-learning"),
                ("legacy-cv", "computer-vision"),
            )
        ),
    ):
        pass
    import_sources(MANIFEST, db)
    manager = SourceManagement(db)
    before = db.read_bytes()
    groups = manager.channel_groups()
    assert groups["legacy-ai"] == "AI 信源"
    assert groups[source_id(source.url)] == "海外电解铝"
    assert set(groups.values()) == {"AI 信源", "海外电解铝"}
    assert db.read_bytes() == before
    content = b'<rss version="2.0"><channel><title>AI News</title><item><title>AI update</title><link>https://example.test/article</link></item></channel></rss>'
    with open_archive(tmp_path) as archive:
        for channel_id, endpoint, body in [
            ("legacy-ai", "https://example.test/legacy-ai/rss", content),
            (
                "legacy-cv",
                "https://example.test/legacy-cv/rss",
                content.replace(b"AI update", b"Vision update").replace(b"/article", b"/vision"),
            ),
            *(
                (
                    channel_for(item).id,
                    item.url,
                    f"<html><title>{item.name}</title><body>Primary aluminium smelter news</body></html>".encode(),
                )
                for item in sources
            ),
        ]:
            raw = RawRecord.create(
                channel_id=channel_id,
                endpoint=endpoint,
                content=body,
                fetched_at=datetime.now(UTC),
                http_status=200,
            )
            archive.put(raw, body)
    with build_webapp(
        tmp_path, db_path=db, industry_provider=lambda: (), require_login=False
    ) as app:
        client = requests.Session()
        client.trust_env = False
        response = client.get(app.base_url + "/feed?order=asc&limit=1&offset=3&labeled=false")
        assert response.status_code == 200
        menu = BeautifulSoup(response.text, "html.parser").select_one(".source-menu")
        assert [s.get_text() for s in menu.select(".source-menu-group-title")] == [
            "AI 信源（2）",
            "海外电解铝（2）",
        ]
        before = db.read_bytes()
        links = {a.get_text(): a["href"] for a in menu.select("nav a")}
        for label, expected in (
            ("AI全部", {"legacy-ai", "legacy-cv"}),
            ("海外电解铝全部", {channel_for(item).id for item in sources}),
        ):
            href = links[label]
            params = parse_qs(urlsplit(href).query)
            assert set(params["channel"]) == expected
            assert params["offset"] == ["0"]
            assert params["order"] == ["asc"] and params["limit"] == ["1"]
            assert params["labeled"] == ["false"]
            seen = set()
            while href:
                response = client.get(app.base_url + href)
                assert response.status_code == 200
                page = BeautifulSoup(response.text, "html.parser")
                assert page.select_one(".source-menu summary span").get_text() == label
                assert [a.get_text() for a in page.select(".source-menu a[aria-current=page]")] == [label]
                seen.update(a["data-channel-id"] for a in page.select("article.entry"))
                next_page = page.select_one('.compact-pager a[aria-label="下一页"]')
                href = next_page["href"] if next_page else None
            assert seen == expected
        # 多来源 URL 的参数顺序不影响当前分组显示。
        response = client.get(app.base_url + "/feed", params=[("channel", "legacy-cv"), ("channel", "legacy-ai")])
        page = BeautifulSoup(response.text, "html.parser")
        assert page.select_one(".source-menu summary span").get_text() == "AI全部"
        assert db.read_bytes() == before
    absent = tmp_path / "not-created.db"
    assert SourceManagement(absent).channel_groups() == {}
    assert not absent.exists()
