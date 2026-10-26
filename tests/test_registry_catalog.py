"""T-101 选配测试：从预置目录勾选 + 覆盖默认值（SPEC §2.9）。

三件必须证明的事：

- 预置目录是**结构化数据**（可整体替换），代码里**没有行业枚举**
- 勾选后可覆盖 `interval_seconds` / `tags` / `enabled` / `user_agent` 等，
  未覆盖的走模板默认值
- **覆盖结果仍走 schema.py 的校验，没有旁路**：非法覆盖一律在保存时被拒
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.contracts import (
    ContractError,
    IdError,
    InvalidContractStateError,
    NotFoundError,
)
from atlas.registry import (
    CatalogSelection,
    ChangeKind,
    ChannelTemplate,
    FetchSpec,
    FetchType,
    IndustryTemplate,
    PrebuiltCatalog,
    RegistryService,
    SqliteConfigStore,
    default_catalog,
    instantiate_channel,
    instantiate_industry,
)

BASE = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def make_clock():
    state = {"n": -1}

    def clock() -> datetime:
        state["n"] += 1
        return BASE + timedelta(minutes=state["n"])

    return clock


@pytest.fixture()
def catalog() -> PrebuiltCatalog:
    return default_catalog()


@pytest.fixture()
def service(tmp_path: Path) -> RegistryService:
    store = SqliteConfigStore(
        author="alice", clock=make_clock(), db_path=tmp_path / "atlas.db"
    )
    return RegistryService(store)


def simple_template(**overrides: object) -> ChannelTemplate:
    payload: dict = {
        "id": "example-feed",
        "industry_id": "example",
        "type": FetchType.RSS,
        "endpoint": "https://example.com/feed.xml",
        "interval_seconds": 3600,
        "tags": ("default-tag",),
        "enabled": True,
    }
    payload.update(overrides)
    return ChannelTemplate(**payload)


# --- 目录是数据，不是行业枚举 --------------------------------------------------


def test_default_catalog_is_structured_data(catalog: PrebuiltCatalog) -> None:
    assert catalog.name
    assert {i.id for i in catalog.industries} == {
        "machine-learning",
        "computer-vision",
        "natural-language",
        "statistical-learning",
    }
    assert all(isinstance(i, IndustryTemplate) for i in catalog.industries)
    assert all(isinstance(c, ChannelTemplate) for c in catalog.channels)


def test_catalog_industries_are_config_data_not_code_enum() -> None:
    """同一份代码可以整体换掉目录 —— 行业清单来自数据，不是代码里的枚举。"""
    custom = PrebuiltCatalog(
        name="自定义目录",
        industries=(IndustryTemplate(id="only-one", name="唯一行业"),),
        channels=(simple_template(industry_id="only-one"),),
    )
    assert custom.instantiate(CatalogSelection(template_id="example-feed")).industry_id == (
        "only-one"
    )
    # default_catalog() 里没有这个行业 —— 代码没有硬编码"只能有哪些行业"
    assert "only-one" not in {i.id for i in default_catalog().industries}


def test_catalog_channels_reference_known_industries(catalog: PrebuiltCatalog) -> None:
    known = {i.id for i in catalog.industries}
    unknown = sorted({c.industry_id for c in catalog.channels} - known)
    assert unknown == [], f"目录里有指向未知行业的渠道：{unknown}"


def test_catalog_seed_uses_existing_project_sources(catalog: PrebuiltCatalog) -> None:
    """种子取自 config/sources.yaml 已在用的 arXiv 分类与源（SPEC §2.5）。"""
    endpoints = {c.endpoint for c in catalog.channels}
    assert "https://arxiv.org/rss/cs.LG" in endpoints
    assert "https://arxiv.org/rss/cs.CV" in endpoints
    assert "https://arxiv.org/rss/cs.CL" in endpoints
    assert "https://arxiv.org/rss/stat.ML" in endpoints


# --- 浏览目录 -----------------------------------------------------------------


def test_channel_templates_filter_by_industry_tag_type(catalog: PrebuiltCatalog) -> None:
    assert {t.id for t in catalog.channel_templates(industry_id="machine-learning")} == {
        "arxiv-machine-learning",
        "openai-blog",
        "google-ai-blog",
        "synced-review",
        "hacker-news-frontpage",
    }
    assert {t.id for t in catalog.channel_templates(tag="papers")} == {
        "arxiv-machine-learning",
        "arxiv-computer-vision",
        "arxiv-natural-language",
        "arxiv-statistical-learning",
    }
    assert {t.id for t in catalog.channel_templates(fetch_type=FetchType.JSON_API)} == {
        "hacker-news-frontpage"
    }


def test_entries_expose_defaults_for_the_frontend(catalog: PrebuiltCatalog) -> None:
    entry = next(e for e in catalog.entries() if e.id == "openai-blog")
    assert entry.defaults["interval_seconds"] == 3600
    assert entry.defaults["enabled"] is True
    assert entry.defaults["endpoint"] == "https://openai.com/blog/rss/"
    assert entry.template.name == "OpenAI 官方博客"


def test_templates_by_industry_groups_for_checklist(catalog: PrebuiltCatalog) -> None:
    grouped = dict(catalog.templates_by_industry())
    assert "arxiv-machine-learning" in grouped["machine-learning"]
    assert grouped["statistical-learning"] == ("arxiv-statistical-learning",)


def test_unknown_template_raises_not_found(catalog: PrebuiltCatalog) -> None:
    with pytest.raises(NotFoundError, match="没有渠道模板"):
        catalog.channel_template("nope")
    with pytest.raises(NotFoundError, match="没有行业模板"):
        catalog.industry_template("nope")


# --- 勾选 + 覆盖默认值 --------------------------------------------------------


def test_selection_without_overrides_uses_template_defaults() -> None:
    template = simple_template()
    channel = instantiate_channel(template, CatalogSelection(template_id=template.id))
    assert channel.interval_seconds == 3600
    assert channel.tags == ("default-tag",)
    assert channel.enabled is True
    assert channel.user_agent is None


def test_overrides_replace_defaults_field_by_field() -> None:
    template = simple_template()
    channel = instantiate_channel(
        template,
        CatalogSelection(
            template_id=template.id,
            overrides={"interval_seconds": 900, "tags": ("fast", "custom")},
        ),
    )
    assert channel.interval_seconds == 900
    assert channel.tags == ("fast", "custom")
    # 未覆盖的字段仍然走模板默认值
    assert channel.enabled is True
    assert channel.endpoint == template.endpoint


def test_override_enabled_false() -> None:
    template = simple_template(enabled=True)
    channel = instantiate_channel(
        template, CatalogSelection(template_id=template.id, overrides={"enabled": False})
    )
    assert channel.enabled is False


def test_override_user_agent_requires_justification() -> None:
    template = simple_template()
    with pytest.raises(InvalidContractStateError, match="浏览器伪装"):
        instantiate_channel(
            template,
            CatalogSelection(template_id=template.id, overrides={"user_agent": "chrome"}),
        )
    channel = instantiate_channel(
        template,
        CatalogSelection(
            template_id=template.id,
            overrides={
                "user_agent": "chrome",
                "user_agent_justification": "站点对默认 UA 返回 403，已确认 robots.txt 允许",
            },
        ),
    )
    assert channel.user_agent == "chrome"


def test_override_rate_limit() -> None:
    template = simple_template(rate_limit_seconds=None)
    channel = instantiate_channel(
        template,
        CatalogSelection(template_id=template.id, overrides={"rate_limit_seconds": 30}),
    )
    assert channel.rate_limit_seconds == 30


def test_selection_can_remount_to_another_industry() -> None:
    template = simple_template(industry_id="example")
    channel = instantiate_channel(
        template,
        CatalogSelection(template_id=template.id, industry_id="other-industry"),
    )
    assert channel.industry_id == "other-industry"


def test_industry_override_must_be_valid_id() -> None:
    template = simple_template()
    with pytest.raises(IdError):
        instantiate_channel(
            template,
            CatalogSelection(template_id=template.id, industry_id="Not Valid"),
        )


# --- 覆盖不得绕过校验 ---------------------------------------------------------


def test_override_below_interval_floor_is_rejected() -> None:
    """覆盖 `interval_seconds` 仍必须满足 §2.9 规则 5 的下限。"""
    template = simple_template()
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        instantiate_channel(
            template,
            CatalogSelection(template_id=template.id, overrides={"interval_seconds": 5}),
        )


def test_override_with_disallowed_field_is_rejected() -> None:
    """不许用覆盖去改身份字段（endpoint / type / id）—— 那是换了一个渠道。"""
    for field_name, value in (
        ("endpoint", "https://evil.example.com/feed.xml"),
        ("type", "html_xpath"),
        ("id", "renamed"),
    ):
        with pytest.raises(InvalidContractStateError, match="不被允许"):
            CatalogSelection(template_id="example-feed", overrides={field_name: value})


def test_template_with_invalid_fetch_spec_is_rejected_at_instantiation() -> None:
    template = ChannelTemplate(
        id="json-feed",
        industry_id="example",
        type=FetchType.JSON_API,
        endpoint="https://example.com/api",
        fetch_spec={"list_path": "data.items"},
    )
    with pytest.raises(ContractError, match="缺少必填项"):
        instantiate_channel(template, CatalogSelection(template_id=template.id))


def test_template_with_bad_endpoint_is_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="http"):
        ChannelTemplate(
            id="bad-endpoint",
            industry_id="example",
            type=FetchType.RSS,
            endpoint="ftp://example.com/feed.xml",
        )


def test_template_with_bad_id_or_industry_id_is_rejected() -> None:
    with pytest.raises(IdError):
        ChannelTemplate(
            id="Bad_ID",
            industry_id="example",
            type=FetchType.RSS,
            endpoint="https://example.com/feed.xml",
        )
    with pytest.raises(IdError):
        ChannelTemplate(
            id="good-id",
            industry_id="Bad Industry",
            type=FetchType.RSS,
            endpoint="https://example.com/feed.xml",
        )


def test_industry_template_defaults_and_overrides() -> None:
    template = IndustryTemplate(id="example", name="示例", keywords=("a",))
    plain = instantiate_industry(template, {})
    assert plain.id == "example" and plain.enabled is True
    overridden = instantiate_industry(template, {"name": "示例（改名）", "enabled": False})
    assert overridden.name == "示例（改名）"
    assert overridden.enabled is False


def test_industry_override_disallowed_field_is_rejected() -> None:
    template = IndustryTemplate(id="example", name="示例")
    with pytest.raises(InvalidContractStateError, match="不支持覆盖"):
        instantiate_industry(template, {"id": "other"})


def test_select_batch_fails_entirely_on_one_bad_selection() -> None:
    template = simple_template()
    catalog = PrebuiltCatalog(
        name="测试目录",
        industries=(IndustryTemplate(id="example", name="示例"),),
        channels=(template,),
    )
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        catalog.select(
            [
                CatalogSelection(template_id=template.id),
                CatalogSelection(
                    template_id=template.id, overrides={"interval_seconds": 1}
                ),
            ]
        )


# --- 勾选落到配置（服务层） ---------------------------------------------------


def test_apply_catalog_creates_industries_and_channels(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    record = service.apply_catalog(
        catalog,
        [
            CatalogSelection(template_id="arxiv-machine-learning"),
            CatalogSelection(template_id="arxiv-computer-vision"),
        ],
        author="bob",
    )
    assert record is not None
    assert record.kind is ChangeKind.BATCH
    assert record.author == "bob"
    assert service.require_channel("arxiv-machine-learning").interval_seconds == 7200
    assert service.require_industry("machine-learning").name == "机器学习"
    assert service.require_industry("computer-vision").name == "计算机视觉"
    # 未勾选的行业不会被顺手创建
    with pytest.raises(NotFoundError):
        service.require_industry("natural-language")


def test_apply_catalog_with_overrides_writes_overridden_values(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    service.apply_catalog(
        catalog,
        [
            CatalogSelection(
                template_id="arxiv-machine-learning",
                overrides={"interval_seconds": 1800, "tags": ("fast",), "enabled": False},
            )
        ],
    )
    channel = service.require_channel("arxiv-machine-learning")
    assert channel.interval_seconds == 1800
    assert channel.tags == ("fast",)
    assert channel.enabled is False
    assert channel.endpoint == "https://arxiv.org/rss/cs.LG"


def test_apply_catalog_is_idempotent_when_nothing_changes(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    selections = [CatalogSelection(template_id="arxiv-machine-learning")]
    first = service.apply_catalog(catalog, selections)
    assert first is not None
    version_after_first = service.current_version
    assert service.apply_catalog(catalog, selections) is None
    assert service.current_version == version_after_first


def test_apply_catalog_refuses_to_silently_overwrite_user_changes(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    selections = [CatalogSelection(template_id="arxiv-machine-learning")]
    service.apply_catalog(catalog, selections)
    service.update_channel("arxiv-machine-learning", interval_seconds=999)

    with pytest.raises(InvalidContractStateError, match="不会覆盖你已有的配置"):
        service.apply_catalog(catalog, selections)
    # 用户改动没有被静默重置
    assert service.require_channel("arxiv-machine-learning").interval_seconds == 999


def test_apply_catalog_rejects_empty_selection(service: RegistryService, catalog: PrebuiltCatalog) -> None:
    with pytest.raises(InvalidContractStateError, match="选配为空"):
        service.apply_catalog(catalog, [])
    assert service.current_version == 1


def test_apply_catalog_json_api_template_works_end_to_end(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    service.apply_catalog(catalog, [CatalogSelection(template_id="hacker-news-frontpage")])
    channel = service.require_channel("hacker-news-frontpage")
    assert channel.type is FetchType.JSON_API
    assert channel.fetch_spec.list_path == "hits"
    assert channel.fetch_spec.title_path == "title"
    assert service.require_industry("machine-learning").name == "机器学习"


def test_apply_catalog_selection_that_violates_schema_writes_nothing(
    service: RegistryService, catalog: PrebuiltCatalog
) -> None:
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        service.apply_catalog(
            catalog,
            [
                CatalogSelection(
                    template_id="arxiv-machine-learning",
                    overrides={"interval_seconds": 1},
                )
            ],
        )
    assert service.current_version == 1
    with pytest.raises(NotFoundError):
        service.require_channel("arxiv-machine-learning")


def test_apply_catalog_is_audited(service: RegistryService, catalog: PrebuiltCatalog) -> None:
    record = service.apply_catalog(
        catalog, [CatalogSelection(template_id="arxiv-machine-learning")], author="bob"
    )
    assert record is not None
    entries = service.audit_by("bob")
    assert {e.object_id for e in entries} == {
        "machine-learning",
        "arxiv-machine-learning",
    }
    assert all(e.note is not None for e in entries)


def test_apply_catalog_then_reopen_persists_everything(
    tmp_path: Path, catalog: PrebuiltCatalog
) -> None:
    db = tmp_path / "atlas.db"
    store = SqliteConfigStore(author="alice", clock=make_clock(), db_path=db)
    service = RegistryService(store)
    service.apply_catalog(
        catalog,
        [
            CatalogSelection(
                template_id="arxiv-natural-language",
                overrides={"interval_seconds": 1200},
            )
        ],
        author="bob",
    )
    store.close()

    reopened = RegistryService(SqliteConfigStore(author="alice", db_path=db))
    channel = reopened.require_channel("arxiv-natural-language")
    assert channel.interval_seconds == 1200
    assert reopened.require_industry("natural-language").name == "自然语言处理"
    assert {e.object_id for e in reopened.audit_by("bob")} == {
        "natural-language",
        "arxiv-natural-language",
    }
    reopened.store.close()


def test_required_industries_pulls_in_ancestors_parents_first() -> None:
    """勾选一个"有父行业"的渠道，必须连祖先行业一起建，且父在前。"""
    parent = IndustryTemplate(id="parent-ind", name="父行业")
    child = IndustryTemplate(id="child-ind", name="子行业", parent_id="parent-ind")
    catalog = PrebuiltCatalog(
        name="层级目录",
        industries=(child, parent),  # 故意把子行业放前面
        channels=(
            ChannelTemplate(
                id="child-feed",
                industry_id="child-ind",
                type=FetchType.RSS,
                endpoint="https://example.com/child.xml",
            ),
        ),
    )
    ordered = catalog.required_industries([CatalogSelection(template_id="child-feed")])
    assert [i.id for i in ordered] == ["parent-ind", "child-ind"]


def test_apply_catalog_with_parent_industry_commits_in_one_version(
    service: RegistryService,
) -> None:
    """层级目录的勾选一次提交成功（父行业与子行业在同一版本里建好）。"""
    parent = IndustryTemplate(id="parent-ind", name="父行业")
    child = IndustryTemplate(id="child-ind", name="子行业", parent_id="parent-ind")
    catalog = PrebuiltCatalog(
        name="层级目录",
        industries=(child, parent),
        channels=(
            ChannelTemplate(
                id="child-feed",
                industry_id="child-ind",
                type=FetchType.RSS,
                endpoint="https://example.com/child.xml",
            ),
        ),
    )
    record = service.apply_catalog(catalog, [CatalogSelection(template_id="child-feed")])
    assert record is not None
    assert record.version == 2
    assert service.require_industry("child-ind").parent_id == "parent-ind"
    # 一次提交里三条 CREATE，只产生一个版本
    assert {e.object_id for e in service.audit_log() if e.version == 2} == {
        "parent-ind",
        "child-ind",
        "child-feed",
    }
