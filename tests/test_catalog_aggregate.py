"""T-111 目录聚合测试：全部用本地 fixture 字符串 + 注入的假 fetcher，不打网络、不写仓库 data/。

覆盖任务书的四条判据：
- 去重：同 endpoint 不同 id / 同 id 不同 endpoint（清单内 + 与注册表对账两个层面）
- 校验失败的候选被**逐条**报告（带原因），不静默丢弃
- 合并冲突保留已有记录、不静默覆盖；写入只经 `RegistryService`
- 抓取外部清单本身也走 robots + 限速（robots 拒绝时**一个请求都不发**）
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.catalog.aggregate import (
    AggregationError,
    CandidateOrigin,
    DuplicateKind,
    ListingFetchFailed,
    ListingRobotsDenied,
    MergeConflictKind,
    aggregate_listing,
    aggregate_registry,
    aggregate_registry_from_url,
    apply_merge,
    fetch_listing,
    normalize_endpoint,
    plan_merge,
)
from atlas.collect.fetch import (
    FetchRequest,
    FetchResult,
    FetchTimeout,
    FailureKind,
)
from atlas.collect.robots import RobotsCache, RobotsFetchResult
from atlas.collect.throttle import DomainThrottle, ThrottleTimeout
from atlas.contracts import ContractError
from atlas.registry import (
    ChangeKind,
    Channel,
    FetchSpec,
    Industry,
    RegistryService,
    SqliteConfigStore,
    channels_to_opml,
)
from atlas.registry.opml import registry_to_opml

LISTING_URL = "https://lists.test/catalog.opml"
BODY = b"<opml version='2.0'><head><title>x</title></head><body/></opml>"
FIXED_TIME = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


# --- 测试替身 -----------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 500.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeFetcher:
    """按 URL 预置响应或异常的假 fetcher；未预置的 URL 直接失败（测试想抓网络了？）。"""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = dict(table)
        self.calls: list[FetchRequest] = []

    def __call__(self, request: FetchRequest) -> FetchResult:
        self.calls.append(request)
        if request.url not in self.table:
            raise AssertionError(f"未预置的 URL 被请求：{request.url}")
        value = self.table[request.url]
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]


class FakeRobotsFetcher:
    def __init__(self, body: str = "", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[str] = []

    def __call__(self, robots_url: str, *, user_agent: str) -> RobotsFetchResult:
        self.calls.append(robots_url)
        return RobotsFetchResult(
            url=robots_url, status_code=self.status, body=self.body.encode()
        )


def ok(url: str, body: bytes = BODY) -> FetchResult:
    return FetchResult(url=url, status_code=200, content=body)


def fake_robots(body: str = "", *, status: int = 200) -> tuple[RobotsCache, FakeRobotsFetcher]:
    fetcher = FakeRobotsFetcher(body, status=status)
    return RobotsCache(fetcher, user_agent="Atlas/0.1.0"), fetcher


# --- 夹具：服务与 OPML --------------------------------------------------------


def industry(industry_id: str, name: str, **overrides: object) -> Industry:
    payload: dict = {"id": industry_id, "name": name, "enabled": True}
    payload.update(overrides)
    return Industry(**payload)


def channel(channel_id: str = "openai-blog", **overrides: object) -> Channel:
    payload: dict = {
        "id": channel_id,
        "industry_id": "ai",
        "type": "rss",
        "endpoint": f"https://example.com/{channel_id}.xml",
        "fetch_spec": FetchSpec(type="rss"),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(overrides)
    return Channel(**payload)


def make_service(tmp_path: Path, *, industries: object = (), channels: object = ()) -> RegistryService:
    store = SqliteConfigStore(
        author="tester",
        industries=tuple(industries),  # type: ignore[arg-type]
        channels=tuple(channels),  # type: ignore[arg-type]
        db_path=tmp_path / "atlas.db",
    )
    return RegistryService(store)


def external_opml(entries: str) -> str:
    """通用 OPML 清单（无 atlas-* 字段），条目挂在分组 outline 下。"""
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<opml version='2.0'><head><title>外部清单</title></head><body>\n"
        "  <outline text='分组'>\n"
        f"{entries}"
        "  </outline>\n"
        "</body></opml>\n"
    )


def entry(title: str, url: str, *, kind: str = "rss", extra: str = "") -> str:
    return f"    <outline text='{title}' type='{kind}' xmlUrl='{url}'{extra}/>\n"


# --- endpoint 规范化 ---------------------------------------------------------


def test_normalize_endpoint_is_identity_preserving() -> None:
    assert normalize_endpoint("HTTPS://A.Test:443/Feed") == "https://a.test/Feed"
    assert normalize_endpoint("https://a.test") == "https://a.test/"
    assert normalize_endpoint("http://a.test:8080/x?y=1#frag") == "http://a.test:8080/x?y=1"
    # query 参与身份：不同 query 是不同资源
    assert normalize_endpoint("https://a.test/f") != normalize_endpoint("https://a.test/f?x=1")


def test_normalize_endpoint_rejects_non_http() -> None:
    with pytest.raises(ContractError):
        normalize_endpoint("ftp://a.test/feed")


# --- 归一化与逐条报告 ---------------------------------------------------------


def test_external_listing_is_normalized_through_registry_path() -> None:
    text = external_opml(
        entry("A 博客", "https://a.test/feed.xml")
        + entry("B 论文", "https://b.test/rss", kind="atom")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert result.title == "外部清单"
    assert result.channel_ids == ("a-test-feed-xml", "b-test-rss")
    first, second = result.candidates
    assert first.origin is CandidateOrigin.EXTERNAL
    assert first.channel.type.value == "rss"
    assert first.channel.industry_id == "ai"
    assert first.channel.interval_seconds == 600
    assert first.channel.endpoint == "https://a.test/feed.xml"
    assert second.channel.type.value == "atom"
    assert result.rejected == ()


def test_rejected_candidates_are_reported_one_by_one() -> None:
    text = external_opml(
        entry("好条目", "https://ok.test/feed.xml")
        + entry("未知协议", "https://pod.test/feed", kind="podcast")
        + entry("非 http", "ftp://bad.test/feed")
        + "    <outline text='无 type' xmlUrl='https://notype.test/feed'/>\n"
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert result.channel_ids == ("ok-test-feed-xml",)
    assert len(result.rejected) == 3
    titles = [r.title for r in result.rejected]
    assert titles == ["未知协议", "非 http", "无 type"]
    for rejected in result.rejected:
        assert rejected.reason
        assert rejected.index >= 0
    assert "podcast" in result.rejected[0].reason
    assert result.rejected[1].endpoint == "ftp://bad.test/feed"
    assert "type" in result.rejected[2].reason or "抓取方式" in result.rejected[2].reason


def test_missing_defaults_are_rejected_not_guessed() -> None:
    text = external_opml(entry("A", "https://a.test/feed.xml"))

    without_industry = aggregate_listing(text, source="inline", default_interval_seconds=600)
    assert without_industry.candidates == ()
    assert "industry_id" in without_industry.rejected[0].reason

    without_interval = aggregate_listing(text, source="inline", default_industry_id="ai")
    assert without_interval.candidates == ()
    assert "interval" in without_interval.rejected[0].reason


def test_atlas_marked_outline_uses_its_own_fields() -> None:
    text = (
        "<?xml version='1.0'?><opml version='2.0'><head><title>Atlas 导出</title></head><body>"
        "<outline atlas-object='industry' atlas-id='ai' atlas-name='人工智能' atlas-enabled='true'>"
        "<outline atlas-object='channel' atlas-id='mine' atlas-industry-id='ai' "
        "atlas-type='rss' atlas-endpoint='https://mine.test/feed' "
        "atlas-interval-seconds='900' atlas-enabled='false' atlas-tags='a,b'/>"
        "</outline></body></opml>"
    )
    result = aggregate_listing(text, source="inline")

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.origin is CandidateOrigin.ATLAS
    assert candidate.channel.id == "mine"
    assert candidate.channel.enabled is False
    assert candidate.channel.tags == ("a", "b")
    assert [i.id for i in result.industries] == ["ai"]


def test_document_level_parse_failure_raises() -> None:
    with pytest.raises(AggregationError) as exc:
        aggregate_listing("<opml version='2.0'></opml>", source="inline")
    assert "<body>" in str(exc.value)


# --- 清单内去重：双键 ---------------------------------------------------------


def test_dedup_same_endpoint_different_id() -> None:
    text = external_opml(
        entry("第一次", "https://dup.test/feed.xml")
        + entry("第二次", "https://dup.test/feed.xml")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].title == "第一次"
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind is DuplicateKind.IDENTICAL
    assert conflict.kept_channel_id == conflict.dropped_channel_id == "dup-test-feed-xml"


def test_dedup_same_id_different_endpoint() -> None:
    text = external_opml(
        entry("A", "https://one.test/feed.xml", extra=" atlas-id='shared'")
        + entry("B", "https://two.test/feed.xml", extra=" atlas-id='shared'")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert [c.channel.id for c in result.candidates] == ["shared"]
    assert result.candidates[0].channel.endpoint == "https://one.test/feed.xml"
    assert len(result.conflicts) == 1
    assert result.conflicts[0].kind is DuplicateKind.SAME_ID_DIFFERENT_ENDPOINT
    assert result.conflicts[0].dropped_channel_id == "shared"
    assert "id 撞车" in result.conflicts[0].reason


def test_dedup_same_endpoint_different_id_explicit_ids() -> None:
    text = external_opml(
        entry("A", "https://same.test/feed.xml", extra=" atlas-id='alpha'")
        + entry("B", "https://same.test/feed.xml", extra=" atlas-id='beta'")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert [c.channel.id for c in result.candidates] == ["alpha"]
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind is DuplicateKind.SAME_ENDPOINT_DIFFERENT_ID
    assert conflict.key == "https://same.test/feed.xml"
    assert conflict.kept_channel_id == "alpha"
    assert conflict.dropped_channel_id == "beta"
    assert "endpoint 重复" in conflict.reason


def test_dedup_sees_through_endpoint_cosmetic_differences() -> None:
    text = external_opml(
        entry("A", "https://cosmetic.test/feed.xml", extra=" atlas-id='alpha'")
        + entry("B", "HTTPS://Cosmetic.Test:443/feed.xml", extra=" atlas-id='beta'")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert [c.channel.id for c in result.candidates] == ["alpha"]
    assert len(result.conflicts) == 1
    assert result.conflicts[0].kind is DuplicateKind.SAME_ENDPOINT_DIFFERENT_ID


# --- 合规抓取外部清单 ---------------------------------------------------------


def test_fetch_listing_robots_denied_sends_no_request() -> None:
    robots, robots_fetcher = fake_robots("User-agent: *\nDisallow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    with pytest.raises(ListingRobotsDenied) as exc:
        fetch_listing(LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle)

    assert "disallowed" in str(exc.value)
    assert robots_fetcher.calls == ["https://lists.test/robots.txt"]
    assert fetcher.calls == []  # 合规底线：一个请求都没发


def test_fetch_listing_robots_unavailable_is_conservative() -> None:
    robots, _ = fake_robots("", status=503)
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    with pytest.raises(ListingRobotsDenied):
        fetch_listing(LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle)
    assert fetcher.calls == []


def test_fetch_listing_goes_through_throttle_and_robots() -> None:
    robots, _ = fake_robots("User-agent: *\nAllow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL, b"<opml version='2.0'><body/></opml>")})
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep)

    # 先占用一次同域许可，第二次必须等待（证明清单抓取走的是限速层）。
    throttle.reserve("lists.test", 1.0)
    document = fetch_listing(
        LISTING_URL,
        fetcher=fetcher,
        robots=robots,
        throttle=throttle,
        wall_clock=lambda: FIXED_TIME,
    )

    assert document.waited_seconds == pytest.approx(1.0)
    assert clock.sleeps == [pytest.approx(1.0)]
    assert document.status_code == 200
    assert document.robots_outcome.value == "allowed"
    assert document.fetched_at == FIXED_TIME
    assert document.replacement_chars == 0
    assert len(fetcher.calls) == 1


def test_fetch_listing_throttle_timeout_is_loud() -> None:
    robots, _ = fake_robots("User-agent: *\nAllow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL)})
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep, max_wait_seconds=0.0)
    throttle.reserve("lists.test", 5.0)

    with pytest.raises(ThrottleTimeout):
        fetch_listing(LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle)
    assert fetcher.calls == []


def test_fetch_listing_failure_is_structured() -> None:
    robots, _ = fake_robots("User-agent: *\nAllow: /\n")
    fetcher = FakeFetcher({LISTING_URL: FetchTimeout("超时", url=LISTING_URL)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    with pytest.raises(ListingFetchFailed) as exc:
        fetch_listing(LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle)
    assert exc.value.failure.kind is FailureKind.TIMEOUT


def test_fetch_listing_requires_declared_user_agent() -> None:
    robots, _ = fake_robots("User-agent: *\nAllow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    with pytest.raises(ContractError):
        fetch_listing(
            LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle, user_agent=" "
        )
    assert fetcher.calls == []


def test_fetch_listing_reports_decode_damage() -> None:
    robots, _ = fake_robots("User-agent: *\nAllow: /\n")
    damaged = b"<opml version='2.0'><body><outline text='\xff\xfe'/></body></opml>"
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL, damaged)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    document = fetch_listing(LISTING_URL, fetcher=fetcher, robots=robots, throttle=throttle)
    assert document.replacement_chars == 2


# --- 与注册表对账 -------------------------------------------------------------


def test_plan_merge_creates_missing_channels(tmp_path: Path) -> None:
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    text = external_opml(
        entry("A", "https://a.test/feed.xml") + entry("B", "https://b.test/feed.xml")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )
    plan = plan_merge(
        result,
        existing_industries=service.snapshot.industries,
        existing_channels=service.snapshot.channels,
    )

    assert plan.create_industries == ()
    assert [c.id for c in plan.create_channels] == ["a-test-feed-xml", "b-test-feed-xml"]
    assert plan.conflicts == ()
    assert not plan.is_empty

    version = apply_merge(service, plan, author="tester", note="聚合测试")
    assert version is not None
    assert service.current_version == 2
    assert {c.id for c in service.list_channels()} == {"a-test-feed-xml", "b-test-feed-xml"}


def test_second_merge_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    text = external_opml(entry("A", "https://a.test/feed.xml"))

    first = aggregate_registry(
        service,
        text,
        source="inline",
        default_industry_id="ai",
        default_interval_seconds=600,
        author="tester",
    )
    assert first.version is not None
    before = service.require_channel("a-test-feed-xml").payload()

    second = aggregate_registry(
        service,
        text,
        source="inline",
        default_industry_id="ai",
        default_interval_seconds=600,
        author="tester",
    )
    assert second.version is None  # 明确的"无变化"
    assert second.plan.existing == ("a-test-feed-xml",)
    assert service.current_version == first.version.version  # 没有新版本
    assert service.require_channel("a-test-feed-xml").payload() == before


def test_merge_conflict_same_id_different_content_keeps_registry(tmp_path: Path) -> None:
    existing = channel("shared", endpoint="https://mine.test/feed.xml")
    service = make_service(
        tmp_path, industries=(industry("ai", "人工智能"),), channels=(existing,)
    )
    text = external_opml(
        entry("别的", "https://other.test/feed.xml", extra=" atlas-id='shared'")
    )
    outcome = aggregate_registry(
        service,
        text,
        source="inline",
        default_industry_id="ai",
        default_interval_seconds=600,
        author="tester",
    )

    assert outcome.version is None
    assert outcome.plan.create_channels == ()
    assert len(outcome.plan.conflicts) == 1
    conflict = outcome.plan.conflicts[0]
    assert conflict.kind is MergeConflictKind.ID
    assert conflict.kept == "shared"
    assert "保留注册表记录" in conflict.reason
    assert service.require_channel("shared").endpoint == "https://mine.test/feed.xml"


def test_merge_conflict_same_endpoint_different_id_keeps_registry(tmp_path: Path) -> None:
    existing = channel("mine", endpoint="https://dup.test/feed.xml")
    service = make_service(
        tmp_path, industries=(industry("ai", "人工智能"),), channels=(existing,)
    )
    text = external_opml(
        entry("外部", "HTTPS://Dup.Test:443/feed.xml", extra=" atlas-id='external'")
    )
    outcome = aggregate_registry(
        service,
        text,
        source="inline",
        default_industry_id="ai",
        default_interval_seconds=600,
        author="tester",
    )

    assert outcome.version is None
    assert outcome.plan.conflicts[0].kind is MergeConflictKind.ENDPOINT
    assert outcome.plan.conflicts[0].kept == "mine"
    assert outcome.plan.conflicts[0].key == "https://dup.test/feed.xml"
    assert [c.id for c in service.list_channels()] == ["mine"]


def test_merge_reports_missing_industry_per_candidate(tmp_path: Path) -> None:
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    text = external_opml(entry("A", "https://a.test/feed.xml"))
    result = aggregate_listing(
        text, source="inline", default_industry_id="ghost", default_interval_seconds=600
    )
    plan = plan_merge(
        result,
        existing_industries=service.snapshot.industries,
        existing_channels=service.snapshot.channels,
    )

    assert plan.create_channels == ()
    assert plan.conflicts[0].kind is MergeConflictKind.INDUSTRY_MISSING
    assert "ghost" in plan.conflicts[0].reason
    assert apply_merge(service, plan, author="tester") is None


def test_merge_can_create_declared_industry_when_allowed(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    text = (
        "<?xml version='1.0'?><opml version='2.0'><head><title>导出</title></head><body>"
        "<outline atlas-object='industry' atlas-id='ai' atlas-name='人工智能' atlas-enabled='true'>"
        "<outline atlas-object='channel' atlas-id='mine' atlas-industry-id='ai' "
        "atlas-type='rss' atlas-endpoint='https://mine.test/feed' "
        "atlas-interval-seconds='900'/>"
        "</outline></body></opml>"
    )
    result = aggregate_listing(text, source="inline")

    strict = plan_merge(
        result,
        existing_industries=service.snapshot.industries,
        existing_channels=service.snapshot.channels,
    )
    assert strict.conflicts[0].kind is MergeConflictKind.INDUSTRY_MISSING

    allowed = plan_merge(
        result,
        existing_industries=service.snapshot.industries,
        existing_channels=service.snapshot.channels,
        create_missing_industries=True,
    )
    assert [i.id for i in allowed.create_industries] == ["ai"]
    assert [c.id for c in allowed.create_channels] == ["mine"]

    version = apply_merge(service, allowed, author="tester")
    assert version is not None
    assert version.kind is ChangeKind.IMPORT
    assert service.require_industry("ai").name == "人工智能"
    assert service.require_channel("mine").industry_id == "ai"


def test_apply_merge_is_noop_on_empty_plan(tmp_path: Path) -> None:
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    result = aggregate_listing("<opml version='2.0'><body/></opml>", source="inline")
    plan = plan_merge(
        result,
        existing_industries=service.snapshot.industries,
        existing_channels=service.snapshot.channels,
    )
    assert plan.is_empty
    assert apply_merge(service, plan, author="tester") is None
    assert service.current_version == 1


def test_merge_never_touches_disabled_existing_channel(tmp_path: Path) -> None:
    existing = channel("stopped", endpoint="https://stopped.test/feed.xml", enabled=False)
    service = make_service(
        tmp_path, industries=(industry("ai", "人工智能"),), channels=(existing,)
    )
    text = external_opml(entry("外部", "https://stopped.test/feed.xml"))
    outcome = aggregate_registry(service, text, source="inline", author="tester")

    assert outcome.version is None
    assert service.require_channel("stopped").enabled is False  # 不因聚合而被"复活"
    audit = service.audit_for("stopped")
    assert all(entry.change is ChangeKind.CREATE for entry in audit)


def test_registry_export_round_trips_into_another_registry(tmp_path: Path) -> None:
    source_service = make_service(
        tmp_path / "a",
        industries=(industry("ai", "人工智能"),),
        channels=(
            channel("openai-blog", endpoint="https://openai.test/blog.xml", tags=("official",)),
            channel("arxiv-lg", endpoint="https://arxiv.test/cs.LG"),
        ),
    )
    exported = registry_to_opml(
        source_service.snapshot.industries, source_service.snapshot.channels
    )

    target_service = make_service(tmp_path / "b")
    outcome = aggregate_registry(
        target_service,
        exported,
        source="导出文件",
        create_missing_industries=True,
        author="tester",
    )

    assert outcome.version is not None
    assert {c.id for c in target_service.list_channels()} == {"openai-blog", "arxiv-lg"}
    assert target_service.require_channel("openai-blog").tags == ("official",)
    assert target_service.require_channel("openai-blog").endpoint == (
        "https://openai.test/blog.xml"
    )


def test_channels_to_opml_plain_listing_of_atlas_channels() -> None:
    plain = channels_to_opml(
        [channel("alpha", endpoint="https://alpha.test/feed.xml")],
        title="纯渠道清单",
        created_at=FIXED_TIME,
    )
    result = aggregate_listing(plain, source="inline")
    assert [c.channel.id for c in result.candidates] == ["alpha"]


def test_mixed_listing_reports_parse_skips_and_rejections() -> None:
    text = external_opml(
        entry("好条目", "https://ok.test/feed.xml")
        + "    <outline text='空节点'/>\n"
        + entry("坏协议", "https://bad.test/feed", kind="soap")
    )
    result = aggregate_listing(
        text, source="inline", default_industry_id="ai", default_interval_seconds=600
    )

    assert result.channel_ids == ("ok-test-feed-xml",)
    assert len(result.rejected) == 1
    assert result.rejected[0].title == "坏协议"
    assert len(result.skipped) == 1
    assert result.skipped[0][0] == "空节点"
    assert "候选 1 条" in result.summary()


def test_url_listing_is_fetched_through_the_compliance_layer(tmp_path: Path) -> None:
    """URL 输入：抓取走 robots + 限速，再进同一条聚合管道。"""
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    listing = external_opml(
        entry("A", "https://a.test/feed.xml") + entry("B", "https://b.test/feed.xml")
    ).encode()
    robots, robots_fetcher = fake_robots("User-agent: *\nAllow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL, listing)})
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep)

    outcome = aggregate_registry_from_url(
        service,
        LISTING_URL,
        fetcher=fetcher,
        robots=robots,
        throttle=throttle,
        default_industry_id="ai",
        default_interval_seconds=600,
        author="tester",
    )

    assert robots_fetcher.calls == ["https://lists.test/robots.txt"]
    assert outcome.result.source == LISTING_URL
    assert outcome.version is not None
    assert {c.id for c in service.list_channels()} == {
        "a-test-feed-xml",
        "b-test-feed-xml",
    }


def test_url_listing_robots_denied_never_touches_the_registry(tmp_path: Path) -> None:
    service = make_service(tmp_path, industries=(industry("ai", "人工智能"),))
    robots, _ = fake_robots("User-agent: *\nDisallow: /\n")
    fetcher = FakeFetcher({LISTING_URL: ok(LISTING_URL)})
    throttle = DomainThrottle(clock=FakeClock(), sleeper=FakeClock().sleep)

    with pytest.raises(ListingRobotsDenied):
        aggregate_registry_from_url(
            service,
            LISTING_URL,
            fetcher=fetcher,
            robots=robots,
            throttle=throttle,
            default_industry_id="ai",
            default_interval_seconds=600,
            author="tester",
        )

    assert fetcher.calls == []
    assert service.current_version == 1
    assert service.list_channels() == ()
