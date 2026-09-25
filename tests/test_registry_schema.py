"""T-004 校验规则逐条测试（SPEC §2.9「校验规则（必须实现）」1–6）。

原则：非法配置必须在保存时被**拒绝**（抛领域异常），不得静默降级；
且违例异常必须是 `atlas.contracts` 的领域异常，而不是被 pydantic 包装的
`ValidationError`（后者会丢掉语义）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

import atlas.registry as registry
from atlas.contracts import ContractError, IdError, ImmutabilityError, InvalidContractStateError
from atlas.registry import (
    MIN_INTERVAL_SECONDS,
    Channel,
    FetchSpec,
    FetchType,
    Industry,
    require_adapter_plugin,
    validate_registry,
)
from atlas.registry.schema import SPEC_FIELDS_BY_FAMILY


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    """便捷构造：`enabled` 是 SPEC §2.9 的必填项，测试里默认显式给 True。"""
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def rss_channel(**overrides: object) -> Channel:
    payload: dict = {
        "id": "openai-blog",
        "industry_id": "ai",
        "type": "rss",
        "endpoint": "https://openai.com/blog/rss.xml",
        "fetch_spec": FetchSpec(type="rss"),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(overrides)
    return Channel(**payload)


# --- 规则 1：id 唯一 + 格式合法 -------------------------------------------------


@pytest.mark.parametrize("bad_id", ["AI", "Openai-Blog", "openai_blog", "-openai", "openai-", "a--b", "", "ai.blog"])
def test_industry_id_format_rejected(bad_id: str) -> None:
    with pytest.raises(IdError):
        industry(bad_id, name="X")


@pytest.mark.parametrize("bad_id", ["AI", "channel_1", "-x", "x-"])
def test_channel_id_format_rejected(bad_id: str) -> None:
    with pytest.raises(IdError):
        rss_channel(id=bad_id)


def test_id_error_is_domain_error_not_validation_error() -> None:
    with pytest.raises(IdError) as excinfo:
        industry("Bad Id", name="X")
    assert isinstance(excinfo.value, ContractError)
    assert "ValidationError" not in type(excinfo.value).__name__


def test_duplicate_industry_id_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="行业 id 重复"):
        validate_registry([industry("ai", name="AI"), industry("ai", name="AI 2")], [])


def test_duplicate_channel_id_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="渠道 id 重复"):
        validate_registry([industry("ai", name="AI")], [rss_channel(), rss_channel()])


def test_industry_and_channel_cannot_share_id() -> None:
    with pytest.raises(InvalidContractStateError, match="同时被行业与渠道占用"):
        validate_registry([industry("ai", name="AI")], [rss_channel(id="ai")])


def test_parent_cycle_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="成环"):
        validate_registry(
            [
                industry("a", name="A", parent_id="b"),
                industry("b", name="B", parent_id="a"),
            ],
            [],
        )


def test_self_parent_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="不能以自己为父行业"):
        industry("a", name="A", parent_id="a")


# --- 规则 2：industry_id 必须存在 ----------------------------------------------


def test_channel_industry_must_exist() -> None:
    with pytest.raises(InvalidContractStateError, match="industry_id='ghost' 不存在"):
        validate_registry([industry("ai", name="AI")], [rss_channel(industry_id="ghost")])


def test_channel_industry_exists_passes() -> None:
    validate_registry([industry("ai", name="AI")], [rss_channel()])


def test_parent_industry_must_exist() -> None:
    with pytest.raises(InvalidContractStateError, match="parent_id='ghost' 不存在"):
        validate_registry([industry("ai", name="AI", parent_id="ghost")], [])


# --- 规则 3：type 必须在枚举内 --------------------------------------------------


def test_known_types_are_the_four_zero_code_types() -> None:
    assert {t.value for t in FetchType} == {"rss", "atom", "json_api", "html_xpath"}


@pytest.mark.parametrize("unknown", ["soap", "graphql", "js_render", "sitemap"])
def test_unknown_fetch_type_cannot_be_constructed(unknown: str) -> None:
    """枚举外的协议在类型层就构造不出来 —— 这是 §2.6 的扩展边界。"""
    with pytest.raises(ValidationError):
        FetchSpec(type=unknown)


def test_unknown_protocol_boundary_is_explicit_not_silent() -> None:
    with pytest.raises(NotImplementedError, match="adapter 插件"):
        require_adapter_plugin("soap")


def test_channel_type_must_match_fetch_spec_type() -> None:
    with pytest.raises(InvalidContractStateError, match="不一致"):
        rss_channel(type="atom")


# --- 规则 4：fetch_spec 必须满足该 type 的必填项 --------------------------------


def test_rss_and_atom_need_no_spec_fields() -> None:
    assert FetchSpec(type="rss").payload() == {"type": "rss"}
    assert FetchSpec(type="atom").payload() == {"type": "atom"}


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "json_api"},
        {"type": "json_api", "title_path": "title"},
    ],
)
def test_json_api_requires_list_and_title_path(payload: dict) -> None:
    with pytest.raises(InvalidContractStateError, match="缺少必填项"):
        FetchSpec(**payload)


def test_json_api_accepts_field_paths() -> None:
    spec = FetchSpec(
        type="json_api",
        list_path="data.items",
        title_path="title",
        content_path="summary",
        time_path="published_at",
        url_path="link",
    )
    assert spec.list_path == "data.items"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "html_xpath"},
        {"type": "html_xpath", "list_selector": "div.item"},
    ],
)
def test_html_xpath_requires_list_and_title_selector(payload: dict) -> None:
    with pytest.raises(InvalidContractStateError, match="缺少必填项"):
        FetchSpec(**payload)


def test_html_xpath_accepts_selectors() -> None:
    spec = FetchSpec(
        type="html_xpath",
        list_selector="article.post",
        title_selector="h2 > a",
        content_selector="div.body",
        time_selector="time[datetime]",
    )
    assert spec.title_selector == "h2 > a"


def test_rss_spec_rejects_selector_fields() -> None:
    """不认识的字段会被 adapter 静默忽略 —— 所以保存时就拒绝。"""
    with pytest.raises(InvalidContractStateError, match="不接受字段"):
        FetchSpec(type="rss", list_selector="div.item")


def test_json_api_rejects_selector_fields() -> None:
    with pytest.raises(InvalidContractStateError, match="不接受字段"):
        FetchSpec(type="json_api", list_path="d", title_path="t", list_selector="div")


def test_html_xpath_rejects_field_path_fields() -> None:
    with pytest.raises(InvalidContractStateError, match="不接受字段"):
        FetchSpec(type="html_xpath", list_selector="d", title_selector="t", list_path="x")


def test_blank_spec_field_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="不得为空白"):
        FetchSpec(type="json_api", list_path="   ", title_path="title")


def test_spec_field_families_are_disjoint_and_cover_every_declared_field() -> None:
    json_family = set(SPEC_FIELDS_BY_FAMILY[FetchType.JSON_API])
    xpath_family = set(SPEC_FIELDS_BY_FAMILY[FetchType.HTML_XPATH])
    assert json_family.isdisjoint(xpath_family)
    declared = json_family | xpath_family
    assert declared <= set(FetchSpec.model_fields)


# --- 规则 5：interval / rate_limit 下限 ----------------------------------------


def test_interval_below_floor_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        rss_channel(interval_seconds=MIN_INTERVAL_SECONDS - 1)


def test_interval_at_floor_accepted() -> None:
    assert rss_channel(interval_seconds=MIN_INTERVAL_SECONDS).interval_seconds == MIN_INTERVAL_SECONDS


def test_negative_rate_limit_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="低于下限"):
        rss_channel(rate_limit_seconds=-1)


def test_rate_limit_none_means_no_extra_throttle() -> None:
    assert rss_channel(rate_limit_seconds=None).rate_limit_seconds is None


# --- 规则 6：endpoint 必须是合法 http(s) URL -----------------------------------


@pytest.mark.parametrize(
    "bad_endpoint",
    ["", "ftp://example.com/feed", "example.com/feed", "https://", "file:///etc/passwd", "//example.com"],
)
def test_bad_endpoint_rejected(bad_endpoint: str) -> None:
    with pytest.raises(InvalidContractStateError):
        rss_channel(endpoint=bad_endpoint)


@pytest.mark.parametrize("good", ["http://example.com/feed", "https://example.com/a/b?c=d#e"])
def test_good_endpoint_accepted(good: str) -> None:
    assert rss_channel(endpoint=good).endpoint == good


# --- user_agent 合规底线（SPEC §2.9 / §7.1）------------------------------------


def test_browser_masquerade_forbidden_by_default() -> None:
    with pytest.raises(InvalidContractStateError, match="浏览器伪装"):
        rss_channel(user_agent="chrome-desktop")


def test_browser_masquerade_allowed_with_explicit_reason() -> None:
    channel = rss_channel(
        user_agent="chrome-desktop",
        user_agent_justification="该站点对非浏览器 UA 返回 403，已在 issue #12 记录",
    )
    assert channel.user_agent_justification


def test_justification_without_user_agent_rejected() -> None:
    with pytest.raises(InvalidContractStateError, match="不需要"):
        rss_channel(user_agent_justification="没用的理由")


def test_plain_ua_name_allowed() -> None:
    assert rss_channel(user_agent="atlas-bot").user_agent == "atlas-bot"


# --- 记录不可变 + 闭环约束 -----------------------------------------------------


def test_records_are_frozen_and_model_copy_update_is_sealed() -> None:
    industry = industry("ai", name="AI")
    with pytest.raises(ValidationError):
        industry.name = "改名"  # type: ignore[misc]
    with pytest.raises(ImmutabilityError):
        industry.model_copy(update={"name": "改名"})


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        industry("ai", name="AI", owner="me")  # type: ignore[call-arg]


def test_keywords_must_be_unique_and_non_blank() -> None:
    with pytest.raises(InvalidContractStateError, match="重复"):
        industry("ai", name="AI", keywords=("ml", "ml"))
    with pytest.raises(InvalidContractStateError, match="空白词"):
        industry("ai", name="AI", keywords=("",))


def test_registry_package_hardcodes_no_industry_enum() -> None:
    """SPEC §2.9 闭环约束：禁止在代码中硬编码任何行业枚举。

    代码层检查：registry 包里不允许出现行业预置常量（标签空间只能来自配置数据）。
    """
    source_dir = Path(registry.__file__).parent
    forbidden = re.compile(r"^\s*(DEFAULT_INDUSTRIES|PRESET_INDUSTRIES|INDUSTRY_ENUM|INDUSTRY_LIST)\s*[:=]", re.M)
    offenders = [
        path.name
        for path in sorted(source_dir.glob("*.py"))
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_arbitrary_industry_id_is_accepted_without_code_change() -> None:
    """新增行业只配置、不改代码：任意合法 id 都能构造。"""
    assert industry("quantum-sensing", name="量子传感").id == "quantum-sensing"
