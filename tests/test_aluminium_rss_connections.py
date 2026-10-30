"""公开 RSS 目录的实际端点配置：不改原清单，不绕过站点拒绝。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from atlas.aluminium.__main__ import main
from atlas.aluminium.models import Source, collection_source
from atlas.aluminium.processing import process_archive
from atlas.aluminium.sources import (
    channel_for,
    configure_rss_source,
    import_sources,
    observe_result,
    probe_sources,
)
from atlas.aluminium.store import AluminiumStore, source_id
from atlas.aluminium.ui import SourceManagement
from atlas.archive import open_archive
from atlas.collect import RobotsCache
from atlas.collect.robots import RobotsFetchResult
from atlas.compose.pipeline import Pipeline, PipelineConfig
from atlas.compose.tasks import ComposeDependencies
from atlas.registry import open_store
from atlas.registry.versioning import RegistryMutation
from tests.test_aluminium import HTML, MANIFEST, RSS, dependencies, event, fixtures

FEED = (
    "https://www.globenewswire.com/RssFeed/industry/1753-Aluminum/feedTitle/"
    "GlobeNewswire%20-%20Industry%20News%20on%20Aluminum"
)


def setup_directory(tmp_path):
    db, sources = fixtures(tmp_path)
    source = next(s for s in sources if s.mode == "rss_directory")
    return db, source, source_id(source.url)


def version(db):
    with open_store(db, author="check") as registry:
        return str(registry.current_version)


def test_configure_idempotent_preserves_originals_and_other_channels(tmp_path):
    db, source, sid = setup_directory(tmp_path)
    with AluminiumStore(db) as store:
        originals = {r["source_id"]: r["original_json"] for r in store.sources()}
    with open_store(db, author="check") as registry:
        channels = {c.id: c.payload() for c in registry.current.channels}
    first = configure_rss_source(db, sid, FEED)
    second = configure_rss_source(db, sid, FEED)
    assert first["created_channels"] == 1 and second["created_channels"] == 0
    assert first["config_version"] == second["config_version"]
    assert not second["enabled"]
    assert import_sources(MANIFEST, db)["inserted_sources"] == 0
    with AluminiumStore(db, read_only=True) as store:
        rows = store.sources()
        assert len(rows) == 37
        assert originals == {r["source_id"]: r["original_json"] for r in rows}
        row = store.source(sid)
        assert row["channel_id"] == sid
        assert row["connection"]["directory_url"] == source.url
        assert collection_source(source, row["connection"]).mode == "rss"
        assert source.mode == "rss_directory" and source.pending_reason
        assert all(
            not r["channel_id"]
            for r in rows
            if Source.model_validate_json(r["original_json"]).mode
            in ("paid", "email", "api_pending")
        )
    with open_store(db, author="check") as registry:
        current = {c.id: c.payload() for c in registry.current.channels}
        assert {cid: current[cid] for cid in channels} == channels
        assert current[sid]["endpoint"] == FEED
        assert current[sid]["type"] == "rss"
        assert current[sid]["interval_seconds"] == 21600


@pytest.mark.parametrize("mode", ["paid", "email", "api_pending"])
def test_rss_configuration_cannot_enable_other_pending_modes(tmp_path, mode):
    db, sources = fixtures(tmp_path)
    source = next(s for s in sources if s.mode == mode)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="仅 RSS 目录"):
        configure_rss_source(db, source_id(source.url), FEED)
    assert db.read_bytes() == before


def test_directory_url_and_retarget_are_rejected_without_overwriting(tmp_path):
    db, source, sid = setup_directory(tmp_path)
    with pytest.raises(ValueError, match="目录入口"):
        configure_rss_source(db, sid, source.url)
    configure_rss_source(db, sid, FEED)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="不能覆盖"):
        configure_rss_source(db, sid, "https://example.test/other.xml")
    assert db.read_bytes() == before


def test_selected_feed_probe_enables_only_verified_channel_and_toggle_works(tmp_path):
    db, source, sid = setup_directory(tmp_path)
    configure_rss_source(db, sid, FEED)
    service = SourceManagement(db, lock_path=tmp_path / "lock")
    # 旧目录的成功记录不能给新 RSS 地址提供验证证据。
    with AluminiumStore(db) as store:
        store.probe(sid, {"collectible": True, "reason": None, "endpoint": source.url})
    with pytest.raises(ValueError, match="验证"):
        service.toggle(sid, "1", version(db), "tester")
    deps, calls = dependencies(RSS)
    report = probe_sources(db, deps, source_ids=[sid], enable_verified=True)
    assert len(report) == 1 and calls == [FEED]
    assert report[0]["collectible"] and report[0]["structure"] == "rss"
    assert report[0]["url"] == source.url and report[0]["endpoint"] == FEED
    with open_store(db, author="check") as registry:
        assert [c.id for c in registry.current.channels if c.enabled] == [sid]
    # 重复配置不能把用户已启用的渠道关掉。
    assert configure_rss_source(db, sid, FEED)["enabled"]
    service.toggle(sid, "0", version(db), "tester")
    service.toggle(sid, "1", version(db), "tester")
    page = BeautifulSoup(service.render(), "html.parser")
    row = page.select_one(f'tr[data-id="{sid}"]')
    if row is None:
        row = page.select_one(f"#source-detail-{sid}").find_previous_sibling("tr")
    assert row["data-mode"] == "rss" and row["data-state"] == "已验证"
    assert len(page.select(f'a[href="{FEED}"]')) == 1
    assert "原始 URL" in page.select_one(f"#source-detail-{sid}").get_text()


@pytest.mark.parametrize(
    "response,status", [(HTML, 200), (b"broken", 200), (b"unavailable", 503)]
)
def test_bad_feed_never_gets_enabled_or_records_success(tmp_path, response, status):
    db, _, sid = setup_directory(tmp_path)
    configure_rss_source(db, sid, FEED)
    deps, calls = dependencies(response, status)
    report = probe_sources(db, deps, source_ids=[sid], enable_verified=True)
    assert calls == [FEED] and not report[0]["collectible"]
    assert report[0]["reason"]
    with open_store(db, author="check") as registry:
        assert not next(c for c in registry.current.channels if c.id == sid).enabled
    with AluminiumStore(db, read_only=True) as store:
        row = next(r for r in store.status() if r["source_id"] == sid)
        assert row["last_success_at"] is None


def test_robots_403_prevents_feed_request_and_exposes_error(tmp_path):
    db, _, sid = setup_directory(tmp_path)
    configure_rss_source(db, sid, FEED)
    deps, calls = dependencies(RSS)
    deps = replace(
        deps,
        robots=RobotsCache(
            lambda url, **kw: RobotsFetchResult(url, status_code=403, body=b"")
        ),
    )
    report = probe_sources(db, deps, source_ids=[sid], enable_verified=True)
    assert not calls and not report[0]["collectible"]
    assert report[0]["connection_error"]["kind"] == "robots_denied"
    assert "403" in report[0]["reason"]
    page = BeautifulSoup(SourceManagement(db).render(), "html.parser")
    detail = page.select_one(f"#source-detail-{sid}")
    assert "403" in detail.get_text()
    row = detail.find_previous_sibling("tr")
    assert row["data-state"] == "不可用"
    assert row.select_one('button[role="switch"]').has_attr("disabled")


def test_configured_feed_uses_existing_archive_and_extraction_pipeline(tmp_path):
    db, _, sid = setup_directory(tmp_path)
    configure_rss_source(db, sid, FEED)
    feed = RSS.replace(b"https://example.org", b"https://www.globenewswire.com")
    deps, calls = dependencies(feed)
    probe_sources(db, deps, source_ids=[sid], enable_verified=True)
    deps = replace(deps, collection_observer=lambda r: observe_result(db, r))
    with Pipeline(
        PipelineConfig(store_root=tmp_path, actor="tester"), dependencies=deps
    ) as pipeline:
        pipeline.run(targets=("archive",))
    assert calls == [FEED, FEED]
    with open_archive(tmp_path) as archive:
        assert len(archive.all_raw_ids()) == 1
        raw = archive.get(archive.all_raw_ids()[0])
        assert raw.channel_id == sid and raw.endpoint == FEED

    class Model:
        config = SimpleNamespace(model="mock-existing-route")

        def extract(self, title, body):
            assert "first metal" in title and "500 ktpa" in body
            return [event()]

    result = process_archive(tmp_path, extract=True, client=Model())
    assert result["extracted"] == 1 and result["failed"] == 0
    assert process_archive(tmp_path, extract=True, client=Model())["extracted"] == 0
    with AluminiumStore(db, read_only=True) as store:
        row = next(r for r in store.status() if r["source_id"] == sid)
        assert row["last_success_at"] and row["last_attempt"]["successful"]
        intelligence = store.intelligence()
        assert len(intelligence) == 1 and intelligence[0]["source_id"] == sid
        assert intelligence[0]["source_url"].startswith(
            "https://www.globenewswire.com/"
        )
        assert intelligence[0]["data"]["event_type"] == "FIRST_METAL"


def test_unknown_probe_id_fails_before_any_request(tmp_path):
    db, _, _ = setup_directory(tmp_path)
    deps, calls = dependencies(RSS)
    with pytest.raises(ValueError, match="未知信源"):
        probe_sources(db, deps, source_ids=["not-a-source"])
    assert calls == []


def test_duplicate_registered_feed_is_not_imported_again(tmp_path):
    db, source, sid = setup_directory(tmp_path)
    # 同一公开端点即使由其他渠道管理，也不能悄悄再创建一次。
    channel = channel_for(collection_source(source, {"endpoint": FEED}))
    with open_store(db, author="test") as registry:
        payload = {**channel.payload(), "id": "existing-feed"}
        registry.commit(
            [RegistryMutation(kind="create", object_kind="channel", payload=payload)]
        )
    before = db.read_bytes()
    with pytest.raises(ValueError, match="未重复创建"):
        configure_rss_source(db, sid, FEED)
    assert db.read_bytes() == before
    with AluminiumStore(db, read_only=True) as store:
        assert store.source(sid)["channel_id"] is None


def test_cli_configure_and_targeted_probe_use_same_collection_path(
    tmp_path, monkeypatch, capsys
):
    _, _, sid = setup_directory(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATLAS_LIVE", "1")
    deps, calls = dependencies(RSS)
    monkeypatch.setattr(ComposeDependencies, "real", lambda root: deps)
    assert main(["--store-root", str(tmp_path), "configure-rss", sid, FEED]) == 0
    report = tmp_path / "probe.json"
    assert (
        main(
            [
                "--store-root",
                str(tmp_path),
                "probe",
                "--source-id",
                sid,
                "--enable-verified",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    import json

    result = json.loads(report.read_text())
    assert result["tested"] == result["collectible"] == 1
    assert calls == [FEED]
    assert "GlobeNewswire" in capsys.readouterr().out
