"""T-004 OPML 导入导出测试（SPEC §2.9「目录交换」）。

关键判据：**导入走同一套校验，不得有旁路**——非法 OPML 必须在导入时被拒绝，
且解析出的记录必须与手工构造走完全相同的模型校验路径。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.contracts import ContractError, InvalidContractStateError
from atlas.registry import (
    Channel,
    ConfigStore,
    FetchSpec,
    Industry,
    channels_to_opml,
    import_opml,
    parse_opml,
    registry_to_opml,
)
from atlas.registry.opml import slug_from_endpoint

FIXED_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    """便捷构造：`enabled` 是 SPEC §2.9 的必填项，测试里默认显式给 True。"""
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def rich_registry() -> tuple[tuple[Industry, ...], tuple[Channel, ...]]:
    industries = (
        industry("ai", name="人工智能", keywords=("机器学习", "llm")),
        industry("ai-vision", name="计算机视觉", parent_id="ai", keywords=("cv",)),
        industry("biotech", name="生物技术", enabled=False),
        industry("arxiv-cs-lg", name="arXiv cs.LG"),
    )
    channels = (
        Channel(
            id="openai-blog",
            industry_id="ai",
            type="rss",
            endpoint="https://openai.com/blog/rss.xml",
            fetch_spec=FetchSpec(type="rss"),
            interval_seconds=3600,
            rate_limit_seconds=5,
            user_agent="atlas-bot",
            tags=("research", "vendor"),
            enabled=True,
        ),
        Channel(
            id="arxiv-new",
            industry_id="arxiv-cs-lg",
            type="json_api",
            endpoint="http://export.arxiv.org/api/query?search_query=cat:cs.LG",
            fetch_spec=FetchSpec(
                type="json_api",
                list_path="feed.entry",
                title_path="title",
                content_path="summary",
                time_path="published",
                url_path="id",
            ),
            interval_seconds=21600,
            enabled=False,
        ),
        Channel(
            id="lab-blog",
            industry_id="ai-vision",
            type="html_xpath",
            endpoint="https://lab.example.com/news",
            fetch_spec=FetchSpec(
                type="html_xpath",
                list_selector="article.post",
                title_selector="h2 > a",
                content_selector="div.body",
                time_selector="time[datetime]",
            ),
            interval_seconds=86400,
            rate_limit_seconds=30,
            enabled=True,
        ),
    )
    return industries, channels


def make_store(industries=(), channels=(), label_references=None) -> ConfigStore:
    return ConfigStore(
        author="alice",
        industries=industries,
        channels=channels,
        label_references=label_references,
    )


# --- 往返 ----------------------------------------------------------------------


def test_registry_round_trip_is_lossless() -> None:
    industries, channels = rich_registry()
    source = make_store(industries, channels)

    xml = registry_to_opml(
        source.current.industries, source.current.channels, owner="alice", created_at=FIXED_TIME
    )
    assert xml.startswith("<?xml version='1.0' encoding='utf-8'?>")
    assert "<opml version=\"2.0\">" in xml

    target = make_store()
    version = import_opml(target, xml, author="bob")

    assert version is not None
    assert target.current.industries == source.current.industries
    assert target.current.channels == source.current.channels
    assert target.current.digest() == source.current.digest()
    assert version.author == "bob"
    assert version.kind.value == "import"


def test_channels_only_export_round_trips() -> None:
    _, channels = rich_registry()
    xml = channels_to_opml(channels, created_at=FIXED_TIME)
    document = parse_opml(xml)

    assert document.industries == ()
    assert document.legacy == ()
    assert document.skipped == ()
    assert {c.id for c in document.channels} == {c.id for c in channels}
    by_id = {c.id: c for c in document.channels}
    assert by_id["arxiv-new"].fetch_spec.list_path == "feed.entry"
    assert by_id["lab-blog"].fetch_spec.title_selector == "h2 > a"


def test_exported_opml_is_valid_xml_and_keeps_standard_feed_fields() -> None:
    import xml.etree.ElementTree as ET

    _, channels = rich_registry()
    root = ET.fromstring(channels_to_opml(channels, created_at=FIXED_TIME).encode("utf-8"))
    outlines = root.findall("./body/outline")
    assert outlines, "body 下应有渠道 outline"
    rss_outline = next(o for o in outlines if o.get("atlas-id") == "openai-blog")
    assert rss_outline.get("type") == "rss"
    assert rss_outline.get("xmlUrl") == "https://openai.com/blog/rss.xml"


def test_reimport_of_identical_opml_is_idempotent_no_change() -> None:
    industries, channels = rich_registry()
    xml = registry_to_opml(industries, channels, created_at=FIXED_TIME)
    store = make_store()
    assert import_opml(store, xml, author="alice") is not None
    before = store.current_version

    assert import_opml(store, xml, author="alice") is None  # 明确的"无变化"
    assert store.current_version == before


def test_import_does_not_overwrite_existing_configuration() -> None:
    industries, channels = rich_registry()
    xml = registry_to_opml(industries, channels, created_at=FIXED_TIME)
    store = make_store()
    import_opml(store, xml, author="alice")
    store.update_channel(
        Channel(
            id="openai-blog",
            industry_id="ai",
            type="rss",
            endpoint="https://openai.com/blog/rss.xml",
            fetch_spec=FetchSpec(type="rss"),
            interval_seconds=7200,
            enabled=True,
        )
    )
    with pytest.raises(InvalidContractStateError, match="导入不覆盖既有配置"):
        import_opml(store, xml, author="alice")


# --- 导入必须走同一套校验（无旁路）--------------------------------------------


def atlas_channel_xml(endpoint: str = "https://x.example/feed") -> str:
    return (
        "<?xml version='1.0' encoding='utf-8'?><opml version=\"2.0\"><head><title>t</title></head><body>"
        "<outline text=\"ai\" atlas-object=\"industry\" atlas-id=\"ai\" atlas-name=\"AI\" atlas-enabled=\"true\"/>"
        f"<outline text=\"c\" atlas-object=\"channel\" atlas-id=\"c\" atlas-industry-id=\"ai\" "
        f"atlas-type=\"rss\" atlas-endpoint=\"{endpoint}\" atlas-fetch-spec=\"{{&quot;type&quot;: &quot;rss&quot;}}\" "
        "atlas-interval-seconds=\"3600\" atlas-enabled=\"true\"/>"
        "</body></opml>"
    )


def test_import_rejects_bad_endpoint() -> None:
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="http"):
        import_opml(store, atlas_channel_xml("ftp://x.example/feed"), author="alice")
    assert store.current_version == 1


def test_import_rejects_interval_below_floor() -> None:
    xml = atlas_channel_xml().replace('atlas-interval-seconds="3600"', 'atlas-interval-seconds="5"')
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        import_opml(store, xml, author="alice")


def test_import_rejects_missing_fetch_spec_fields() -> None:
    xml = atlas_channel_xml().replace(
        'atlas-type="rss" atlas-endpoint',
        'atlas-type="json_api" atlas-endpoint',
    ).replace('atlas-fetch-spec="{&quot;type&quot;: &quot;rss&quot;}"', 'atlas-fetch-spec="{&quot;type&quot;: &quot;json_api&quot;}"')
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="缺少必填项"):
        import_opml(store, xml, author="alice")


def test_import_rejects_unknown_protocol_type() -> None:
    xml = atlas_channel_xml().replace('atlas-type="rss"', 'atlas-type="soap"').replace(
        'type="rss"', 'type="soap"'
    )
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="不在已知枚举"):
        import_opml(store, xml, author="alice")


def test_import_rejects_dangling_industry_id_no_bypass() -> None:
    """渠道引用了不存在的行业 —— 导入路径必须与手工构造同样拒绝。"""
    xml = atlas_channel_xml().replace('atlas-industry-id="ai"', 'atlas-industry-id="ghost"')
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="不存在"):
        import_opml(store, xml, author="alice")
    assert store.current_version == 1


def test_import_rejects_browser_masquerade_ua_without_reason() -> None:
    xml = atlas_channel_xml().replace(
        'atlas-enabled="true"', 'atlas-user-agent="chrome-desktop" atlas-enabled="true"'
    )
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="浏览器伪装"):
        import_opml(store, xml, author="alice")


def test_import_rejects_malformed_fetch_spec_json() -> None:
    xml = atlas_channel_xml().replace(
        'atlas-fetch-spec="{&quot;type&quot;: &quot;rss&quot;}"', 'atlas-fetch-spec="not json"'
    )
    store = make_store()
    with pytest.raises(InvalidContractStateError, match="不是合法 JSON"):
        import_opml(store, xml, author="alice")


def test_import_failure_leaves_no_partial_version() -> None:
    industries, channels = rich_registry()
    xml = registry_to_opml(industries, channels, created_at=FIXED_TIME)
    broken = xml.replace('atlas-endpoint="https://lab.example.com/news"', 'atlas-endpoint="ftp://lab.example.com/news"')
    store = make_store()
    with pytest.raises(InvalidContractStateError):
        import_opml(store, broken, author="alice")
    assert store.current_version == 1
    assert store.current.channels == ()


# --- 外部 OPML（无 atlas-* 字段）----------------------------------------------


EXTERNAL_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>awesome-rss-feeds</title></head>
  <body>
    <outline text="Blogs">
      <outline type="rss" text="OpenAI" title="OpenAI" xmlUrl="https://openai.com/blog/rss.xml"/>
      <outline type="rss" text="DeepMind" title="DeepMind" xmlUrl="https://deepmind.google/blog/rss.xml"/>
    </outline>
    <outline text="只是链接" htmlUrl="https://example.com/"/>
  </body>
</opml>
"""


def test_external_opml_parses_channels_into_legacy_bucket() -> None:
    document = parse_opml(EXTERNAL_OPML)
    assert document.title == "awesome-rss-feeds"
    assert document.industries == ()
    assert document.channels == ()
    assert len(document.legacy) == 2
    assert len(document.skipped) == 1
    assert "既非渠道" in document.skipped[0][1]


def test_external_opml_import_requires_explicit_defaults() -> None:
    store = make_store(industries=(industry("ai", name="AI"),))
    with pytest.raises(InvalidContractStateError, match="default_industry_id"):
        import_opml(store, EXTERNAL_OPML, author="alice", default_interval_seconds=3600)
    with pytest.raises(InvalidContractStateError, match="default_interval_seconds"):
        import_opml(store, EXTERNAL_OPML, author="alice", default_industry_id="ai")


def test_external_opml_import_uses_given_defaults_and_records_skips() -> None:
    store = make_store(industries=(industry("ai", name="AI"),))
    record = import_opml(
        store,
        EXTERNAL_OPML,
        author="alice",
        default_industry_id="ai",
        default_interval_seconds=43200,
    )
    assert record is not None
    assert record.note is not None and "跳过 1 条" in record.note
    ids = sorted(c.id for c in store.current.channels)
    assert ids == [
        slug_from_endpoint("https://deepmind.google/blog/rss.xml"),
        slug_from_endpoint("https://openai.com/blog/rss.xml"),
    ]
    assert all(c.industry_id == "ai" for c in store.current.channels)
    assert all(c.interval_seconds == 43200 for c in store.current.channels)
    assert all(c.type.value == "rss" for c in store.current.channels)


def test_external_opml_import_rejects_missing_industry_in_store() -> None:
    store = make_store()  # 没有 ai 行业
    with pytest.raises(InvalidContractStateError, match="不存在"):
        import_opml(
            store,
            EXTERNAL_OPML,
            author="alice",
            default_industry_id="ai",
            default_interval_seconds=3600,
        )


def test_slug_from_endpoint_is_deterministic_and_valid() -> None:
    first = slug_from_endpoint("https://OpenAI.com/blog/rss.xml")
    second = slug_from_endpoint("https://openai.com/blog/rss.xml")
    assert first == second == "openai-com-blog-rss-xml"


# --- 解析错误 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not xml at all",
        "<rss version='2.0'></rss>",
        "<?xml version='1.0'?><opml version='2.0'></opml>",
    ],
)
def test_parse_opml_rejects_invalid_documents(text: str) -> None:
    with pytest.raises((InvalidContractStateError, ContractError)):
        parse_opml(text)


def test_parse_opml_rejects_unknown_atlas_object() -> None:
    text = (
        "<opml version='2.0'><head/><body>"
        "<outline text='x' atlas-object='tag' atlas-id='t'/></body></opml>"
    )
    with pytest.raises(InvalidContractStateError, match="未知"):
        parse_opml(text)


def test_parse_opml_rejects_inconsistent_type_and_spec() -> None:
    text = (
        "<opml version='2.0'><head/><body>"
        "<outline text='c' atlas-object='channel' atlas-id='c' atlas-industry-id='ai' "
        "atlas-type='json_api' atlas-fetch-spec='{\"type\": \"rss\"}' "
        "atlas-interval-seconds='3600' atlas-endpoint='https://x.example/f'/></body></opml>"
    )
    with pytest.raises(InvalidContractStateError, match="不一致"):
        parse_opml(text)


def test_parse_opml_rejects_non_boolean_enabled() -> None:
    text = (
        "<opml version='2.0'><head/><body>"
        "<outline text='ai' atlas-object='industry' atlas-id='ai' atlas-enabled='maybe'/>"
        "</body></opml>"
    )
    with pytest.raises(InvalidContractStateError, match="不是布尔值"):
        parse_opml(text)


def test_parse_opml_requires_industry_id_for_atlas_channel() -> None:
    text = (
        "<opml version='2.0'><head/><body>"
        "<outline text='c' atlas-object='channel' atlas-id='c' atlas-type='rss' "
        "atlas-interval-seconds='3600' atlas-endpoint='https://x.example/f'/></body></opml>"
    )
    with pytest.raises(InvalidContractStateError, match="没有 industry_id"):
        parse_opml(text)


def test_export_time_requires_timezone() -> None:
    _, channels = rich_registry()
    with pytest.raises(InvalidContractStateError, match="时区"):
        channels_to_opml(channels, created_at=datetime(2026, 9, 25, 12, 0))
