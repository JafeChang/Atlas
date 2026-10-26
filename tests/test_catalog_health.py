"""T-111 健康保鲜测试：全部注入假 fetcher / 假 robots / 假时钟，不打网络、不写仓库 data/。

覆盖任务书的判据：
- 探测分类可区分：ok / dead(4xx 非 403) / robots_denied / forbidden(401/403) /
  server_error / network_error / empty_body（+ 本地原因）
- 复用 `atlas.collect` 的 robots 与限速层（robots 拒绝时一个请求都不发；同域会等待）
- `catalog_health` 只增不改：直接用 SQL `DELETE` / `UPDATE` 被触发器拒绝
- 失效渠道**只标记不删除**；恢复后能重新标记为健康
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.catalog.health import (
    HEALTHY_STATUSES,
    UNHEALTHY_STATUSES,
    ChannelHealth,
    HealthProbe,
    HealthStatus,
    probe_channel,
    probe_registry,
)
from atlas.catalog.store import (
    DEFAULT_UNHEALTHY_TAG,
    CatalogHealthStore,
)
from atlas.collect.fetch import (
    DEFAULT_USER_AGENT,
    FetchRequest,
    FetchResult,
    FetchTimeout,
    FetchConnectionError,
)
from atlas.collect.robots import RobotsCache, RobotsFetchResult
from atlas.collect.throttle import DomainThrottle, ThrottleTimeout
from atlas.registry import (
    ChangeKind,
    Channel,
    FetchSpec,
    Industry,
    RegistryService,
    SqliteConfigStore,
)

BASE = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
BODY = b"<rss version='2.0'><channel><title>ok</title></channel></rss>"


# --- 测试替身 -----------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeFetcher:
    """按 URL 预置 `FetchResult` 或异常。未预置的 URL 直接失败（测试想抓网络了？）。"""

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


class FakeWallClock:
    """假墙钟：每次调用前进 1 分钟，便于断言 checked_at 单调可区分。"""

    def __init__(self) -> None:
        self.n = -1

    def __call__(self) -> datetime:
        self.n += 1
        return BASE + timedelta(minutes=self.n)


def allow_all_robots() -> tuple[RobotsCache, FakeRobotsFetcher]:
    fetcher = FakeRobotsFetcher("User-agent: *\nAllow: /\n")
    return RobotsCache(fetcher, user_agent="Atlas/0.1.0"), fetcher


def deny_all_robots() -> tuple[RobotsCache, FakeRobotsFetcher]:
    fetcher = FakeRobotsFetcher("User-agent: *\nDisallow: /\n")
    return RobotsCache(fetcher, user_agent="Atlas/0.1.0"), fetcher


def response(url: str, status: int = 200, body: bytes = BODY) -> FetchResult:
    return FetchResult(url=url, status_code=status, content=body)


def probe(
    channel: Channel,
    *,
    robots: RobotsCache,
    fetcher: FakeFetcher | None = None,
    throttle: DomainThrottle | None = None,
    wall_clock: FakeWallClock | None = None,
) -> ChannelHealth:
    clock = FakeClock()
    return probe_channel(
        channel,
        fetcher=fetcher or FakeFetcher({channel.endpoint: response(channel.endpoint)}),
        robots=robots,
        throttle=throttle or DomainThrottle(clock=clock, sleeper=clock.sleep),
        wall_clock=wall_clock or FakeWallClock(),
    )


# --- 夹具 ---------------------------------------------------------------------


def industry(industry_id: str = "ai", name: str = "人工智能") -> Industry:
    return Industry(id=industry_id, name=name, enabled=True)


def channel(channel_id: str = "openai-blog", **overrides: object) -> Channel:
    payload: dict = {
        "id": channel_id,
        "industry_id": "ai",
        "type": "rss",
        "endpoint": f"https://{channel_id}.test/feed.xml",
        "fetch_spec": FetchSpec(type="rss"),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(overrides)
    return Channel(**payload)


@pytest.fixture()
def service(tmp_path: Path) -> RegistryService:
    store = SqliteConfigStore(
        author="tester",
        industries=(industry(),),
        channels=(
            channel("alpha"),
            channel("beta"),
            channel("stopped", enabled=False, tags=("keep",)),
        ),
        db_path=tmp_path / "atlas.db",
    )
    return RegistryService(store)


@pytest.fixture()
def health_store(tmp_path: Path) -> CatalogHealthStore:
    return CatalogHealthStore(tmp_path / "atlas.db")


# --- 分类可区分 ---------------------------------------------------------------


def test_ok_status_carries_http_status_and_time() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    wall = FakeWallClock()
    result = probe(ch, robots=robots, wall_clock=wall)

    assert result.status is HealthStatus.OK
    assert result.healthy is True
    assert result.status in HEALTHY_STATUSES
    assert result.http_status == 200
    assert result.checked_at == BASE
    assert result.endpoint == ch.endpoint
    assert "200" in result.reason


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, HealthStatus.FORBIDDEN),
        (403, HealthStatus.FORBIDDEN),
        (404, HealthStatus.DEAD),
        (410, HealthStatus.DEAD),
        (451, HealthStatus.DEAD),
        (500, HealthStatus.SERVER_ERROR),
        (503, HealthStatus.SERVER_ERROR),
    ],
)
def test_http_failures_are_classified(
    status: int, expected: HealthStatus
) -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint, status=status, body=b"x")})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is expected
    assert result.http_status == status
    assert result.healthy is False
    assert result.status in UNHEALTHY_STATUSES


def test_empty_body_is_its_own_classification() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint, status=200, body=b"")})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.EMPTY_BODY
    assert result.http_status == 200
    assert result.healthy is False


@pytest.mark.parametrize(
    "error",
    [
        FetchTimeout("超时", url="https://alpha.test/feed.xml"),
        FetchConnectionError("连接失败", url="https://alpha.test/feed.xml"),
    ],
)
def test_network_failures_are_classified(error: BaseException) -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: error})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.NETWORK_ERROR
    assert result.http_status is None
    assert result.detail in {"FetchTimeout", "FetchConnectionError"}


def test_robots_denied_sends_no_request() -> None:
    robots, robots_fetcher = deny_all_robots()
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.ROBOTS_DENIED
    assert result.healthy is False
    assert result.robots_outcome == "disallowed_by_rules"
    assert result.robots_url == "https://alpha.test/robots.txt"
    # 关键：robots 说不行就一个请求都不发（合规底线，不是"先抓再判"）
    assert fetcher.calls == []
    assert robots_fetcher.calls == ["https://alpha.test/robots.txt"]


def test_robots_unavailable_is_conservative_not_a_fetch() -> None:
    robots_fetcher = FakeRobotsFetcher("", status=503)
    robots = RobotsCache(robots_fetcher, user_agent="Atlas/0.1.0")
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.ROBOTS_DENIED
    assert result.robots_outcome == "disallowed_robots_unavailable"
    assert result.http_status == 503  # 这是 robots.txt 的状态码，不是 feed 的
    assert fetcher.calls == []


def test_robots_absent_404_allows_the_probe() -> None:
    robots_fetcher = FakeRobotsFetcher("", status=404)
    robots = RobotsCache(robots_fetcher, user_agent="Atlas/0.1.0")
    ch = channel("alpha")
    result = probe(ch, robots=robots)

    assert result.status is HealthStatus.OK


def test_unknown_user_agent_option_is_local_error_without_request() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha", user_agent="not-a-known-option")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.LOCAL_ERROR
    assert result.detail == "UserAgentError"
    assert fetcher.calls == []


def test_browser_masquerade_without_justification_is_local_error() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha", user_agent="Mozilla/5.0 (compatible)")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})
    result = probe(ch, robots=robots, fetcher=fetcher)

    assert result.status is HealthStatus.LOCAL_ERROR
    assert "伪装" in result.reason
    assert fetcher.calls == []


def test_declared_user_agent_is_actually_sent() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})
    probe(ch, robots=robots, fetcher=fetcher)

    assert len(fetcher.calls) == 1
    assert fetcher.calls[0].user_agent == DEFAULT_USER_AGENT


# --- 复用限速层 ---------------------------------------------------------------


def test_probe_goes_through_domain_throttle() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha", rate_limit_seconds=2)
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep)
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})

    probe(ch, robots=robots, fetcher=fetcher, throttle=throttle)
    assert clock.sleeps == []

    second = probe(ch, robots=robots, fetcher=fetcher, throttle=throttle)
    assert clock.sleeps == [pytest.approx(2.0)]
    assert second.waited_seconds == pytest.approx(2.0)
    assert len(fetcher.calls) == 2


def test_throttle_budget_exceeded_is_local_error_without_request() -> None:
    robots, _ = allow_all_robots()
    ch = channel("alpha")
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep, max_wait_seconds=0.0)
    throttle.reserve("alpha.test", 5.0)
    fetcher = FakeFetcher({ch.endpoint: response(ch.endpoint)})

    with pytest.raises(ThrottleTimeout):
        # 第一层：限速层自己响亮失败
        throttle.reserve("alpha.test", 5.0)

    result = probe(ch, robots=robots, fetcher=fetcher, throttle=throttle)
    assert result.status is HealthStatus.LOCAL_ERROR
    assert "限速等待超预算" in result.reason
    assert fetcher.calls == []


def test_health_probe_requires_robots_and_throttle() -> None:
    """合规层不可省略：构造 `HealthProbe` 时 robots / throttle 是必填关键字参数。"""
    with pytest.raises(TypeError):
        HealthProbe(fetcher=FakeFetcher({}))  # type: ignore[call-arg]


# --- 批量与停用渠道 -----------------------------------------------------------


def test_probe_all_skips_disabled_channels_with_reason() -> None:
    robots, _ = allow_all_robots()
    channels = (channel("alpha"), channel("stopped", enabled=False))
    fetcher = FakeFetcher({"https://alpha.test/feed.xml": response("https://alpha.test/feed.xml")})
    clock = FakeClock()
    report = HealthProbe(
        fetcher=fetcher,
        robots=robots,
        throttle=DomainThrottle(clock=clock, sleeper=clock.sleep),
        wall_clock=FakeWallClock(),
    ).probe_all(channels)

    assert [r.channel_id for r in report.results] == ["alpha"]
    assert report.skipped == (("stopped", "channel.enabled=False（SPEC §2.9：停用后不再采集）"),)
    assert "跳过 1 条" in report.summary()


def test_probe_registry_uses_current_enabled_channels(service: RegistryService) -> None:
    robots, _ = allow_all_robots()
    fetcher = FakeFetcher(
        {
            "https://alpha.test/feed.xml": response("https://alpha.test/feed.xml"),
            "https://beta.test/feed.xml": response(
                "https://beta.test/feed.xml", status=404, body=b"gone"
            ),
        }
    )
    clock = FakeClock()
    report = probe_registry(
        service,
        fetcher=fetcher,
        robots=robots,
        throttle=DomainThrottle(clock=clock, sleeper=clock.sleep),
        wall_clock=FakeWallClock(),
    )

    assert [r.channel_id for r in report.results] == ["alpha", "beta"]
    assert report.by_status() == (("dead", ("beta",)), ("ok", ("alpha",)))
    assert report.unhealthy[0].channel_id == "beta"


def test_probe_registry_rejects_wrong_service_type() -> None:
    robots, _ = allow_all_robots()
    clock = FakeClock()
    with pytest.raises(TypeError):
        probe_registry(
            object(),
            fetcher=FakeFetcher({}),
            robots=robots,
            throttle=DomainThrottle(clock=clock, sleeper=clock.sleep),
        )


# --- 持久化：只增不改 ---------------------------------------------------------


def test_health_rows_are_append_only(health_store: CatalogHealthStore) -> None:
    result = ChannelHealth(
        channel_id="alpha",
        endpoint="https://alpha.test/feed.xml",
        status=HealthStatus.OK,
        checked_at=BASE,
        reason="HTTP 200",
        http_status=200,
    )
    row_id = health_store.record(result)
    assert row_id == 1
    assert health_store.count() == 1

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        health_store.connection.execute(
            "UPDATE catalog_health SET status = 'dead' WHERE id = ?", (row_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        health_store.connection.execute("UPDATE catalog_health SET reason = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        health_store.connection.execute("DELETE FROM catalog_health WHERE id = ?", (row_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        health_store.connection.execute("DELETE FROM catalog_health")

    # 触发器拒绝之后历史仍然完整，且没有被就地改写
    assert health_store.count() == 1
    assert health_store.latest("alpha").status is HealthStatus.OK


def test_latest_is_the_current_state_and_history_is_kept(
    health_store: CatalogHealthStore,
) -> None:
    def record(status: HealthStatus, http: int | None, reason: str) -> int:
        return health_store.record(
            ChannelHealth(
                channel_id="alpha",
                endpoint="https://alpha.test/feed.xml",
                status=status,
                checked_at=BASE,
                reason=reason,
                http_status=http,
            )
        )

    record(HealthStatus.OK, 200, "HTTP 200")
    record(HealthStatus.SERVER_ERROR, 503, "HTTP 503")
    record(HealthStatus.DEAD, 404, "HTTP 404")

    assert health_store.count() == 3
    assert health_store.latest("alpha").status is HealthStatus.DEAD
    assert [h.status for h in health_store.history("alpha")] == [
        HealthStatus.OK,
        HealthStatus.SERVER_ERROR,
        HealthStatus.DEAD,
    ]


def test_latest_all_returns_one_row_per_channel(health_store: CatalogHealthStore) -> None:
    for channel_id, status, http in (
        ("alpha", HealthStatus.OK, 200),
        ("alpha", HealthStatus.DEAD, 404),
        ("beta", HealthStatus.ROBOTS_DENIED, 403),
        ("gamma", HealthStatus.OK, 200),
    ):
        health_store.record(
            ChannelHealth(
                channel_id=channel_id,
                endpoint=f"https://{channel_id}.test/feed.xml",
                status=status,
                checked_at=BASE,
                reason=f"HTTP {http}",
                http_status=http,
            )
        )

    latest = health_store.latest_all()
    assert [h.channel_id for h in latest] == ["alpha", "beta", "gamma"]
    assert {h.channel_id: h.status for h in latest}["alpha"] is HealthStatus.DEAD
    assert [h.channel_id for h in health_store.unhealthy()] == ["alpha", "beta"]
    assert [h.channel_id for h in health_store.healthy()] == ["gamma"]


def test_latest_of_unknown_channel_is_none(health_store: CatalogHealthStore) -> None:
    assert health_store.latest("never-probed") is None


def test_channel_health_rejects_missing_reason_and_naive_time() -> None:
    with pytest.raises(ValueError):
        ChannelHealth(
            channel_id="alpha",
            endpoint="https://alpha.test/feed.xml",
            status=HealthStatus.OK,
            checked_at=datetime(2026, 3, 4),
            reason="HTTP 200",
        )
    with pytest.raises(ValueError):
        ChannelHealth(
            channel_id="alpha",
            endpoint="https://alpha.test/feed.xml",
            status=HealthStatus.OK,
            checked_at=BASE,
            reason="   ",
        )


def test_store_shares_the_registry_db_without_touching_it(
    tmp_path: Path, health_store: CatalogHealthStore
) -> None:
    """健康表与注册表共用一个库文件：本模块只加自己的表，不动别人的表。"""
    store = SqliteConfigStore(
        author="tester",
        industries=(industry(),),
        channels=(channel("alpha"),),
        db_path=tmp_path / "atlas.db",
    )
    service = RegistryService(store)

    health_store.record(
        ChannelHealth(
            channel_id="alpha",
            endpoint="https://alpha.test/feed.xml",
            status=HealthStatus.DEAD,
            checked_at=BASE,
            reason="HTTP 404",
            http_status=404,
        )
    )

    # 注册表照常可读可写；健康表存在且互不干扰
    assert [c.id for c in service.list_channels()] == ["alpha"]
    service.update_channel("alpha", rate_limit_seconds=5, author="tester")
    assert service.require_channel("alpha").rate_limit_seconds == 5
    assert health_store.count() == 1
    tables = {
        row["name"]
        for row in health_store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "catalog_health" in tables
    assert {"industries", "channels", "config_versions"} <= tables


# --- 只标记不删除 / 恢复 -------------------------------------------------------


def _seed(
    health_store: CatalogHealthStore, channel_id: str, status: HealthStatus
) -> None:
    health_store.record(
        ChannelHealth(
            channel_id=channel_id,
            endpoint=f"https://{channel_id}.test/feed.xml",
            status=status,
            checked_at=BASE,
            reason=f"seed {status.value}",
            http_status=200 if status is HealthStatus.OK else 404,
        )
    )


def test_unhealthy_channel_is_marked_but_never_deleted(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    _seed(health_store, "alpha", HealthStatus.DEAD)
    _seed(health_store, "beta", HealthStatus.OK)

    versions = health_store.sync_registry(service, author="tester")

    assert len(versions) == 1
    assert DEFAULT_UNHEALTHY_TAG in service.require_channel("alpha").tags
    assert DEFAULT_UNHEALTHY_TAG not in service.require_channel("beta").tags
    # 只标记不删除：记录与启用状态原封不动
    assert {c.id for c in service.list_channels()} == {"alpha", "beta", "stopped"}
    assert service.require_channel("alpha").enabled is True
    assert service.require_channel("stopped").tags == ("keep",)
    # 审计链里没有删除
    assert all(
        entry.change is not ChangeKind.DELETE for entry in service.audit_log()
    )


def test_sync_registry_is_idempotent(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    _seed(health_store, "alpha", HealthStatus.DEAD)
    first = health_store.sync_registry(service, author="tester")
    version_after_first = service.current_version

    second = health_store.sync_registry(service, author="tester")
    assert len(first) == 1
    assert second == ()
    assert service.current_version == version_after_first


def test_recovery_clears_the_marker(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    _seed(health_store, "alpha", HealthStatus.DEAD)
    health_store.sync_registry(service, author="tester")
    assert DEFAULT_UNHEALTHY_TAG in service.require_channel("alpha").tags

    # 源恢复了：追加一条健康记录（只增不改，历史里那条 dead 仍在）
    _seed(health_store, "alpha", HealthStatus.OK)
    versions = health_store.sync_registry(service, author="tester")

    assert len(versions) == 1
    assert DEFAULT_UNHEALTHY_TAG not in service.require_channel("alpha").tags
    assert service.require_channel("alpha").enabled is True
    assert [h.status for h in health_store.history("alpha")] == [
        HealthStatus.DEAD,
        HealthStatus.OK,
    ]
    assert health_store.latest("alpha").status is HealthStatus.OK


def test_sync_registry_preserves_existing_tags(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    service.update_channel("alpha", tags=("official", "llm"), author="tester")
    _seed(health_store, "alpha", HealthStatus.SERVER_ERROR)

    health_store.sync_registry(service, author="tester")
    assert set(service.require_channel("alpha").tags) == {
        "official",
        "llm",
        DEFAULT_UNHEALTHY_TAG,
    }

    _seed(health_store, "alpha", HealthStatus.OK)
    health_store.sync_registry(service, author="tester")
    assert service.require_channel("alpha").tags == ("llm", "official")


def test_sync_registry_never_probed_channel_has_no_marker(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    assert health_store.sync_registry(service, author="tester") == ()
    assert service.current_version == 1
    assert service.require_channel("alpha").tags == ()


def test_sync_registry_rejects_blank_tag(
    service: RegistryService, health_store: CatalogHealthStore
) -> None:
    with pytest.raises(ValueError):
        health_store.sync_registry(service, author="tester", unhealthy_tag="  ")


def test_end_to_end_probe_persist_mark_and_recover(
    service: RegistryService, health_store: CatalogHealthStore, tmp_path: Path
) -> None:
    """端到端：探测 → 落 health 表 → 写回注册表 → 源恢复 → 重新标记为健康。"""
    robots, _ = allow_all_robots()
    clock = FakeClock()
    wall = FakeWallClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep)
    fetcher = FakeFetcher(
        {
            "https://alpha.test/feed.xml": response("https://alpha.test/feed.xml"),
            "https://beta.test/feed.xml": response(
                "https://beta.test/feed.xml", status=404, body=b"gone"
            ),
        }
    )
    probe_impl = HealthProbe(
        fetcher=fetcher, robots=robots, throttle=throttle, wall_clock=wall
    )

    first = probe_impl.probe_all(service.list_channels())
    health_store.record_report(first)
    health_store.sync_registry(service, author="tester", note="第一轮")

    assert DEFAULT_UNHEALTHY_TAG in service.require_channel("beta").tags
    assert DEFAULT_UNHEALTHY_TAG not in service.require_channel("alpha").tags

    # beta 恢复：第二轮探测到 200
    fetcher.table["https://beta.test/feed.xml"] = response(
        "https://beta.test/feed.xml"
    )
    second = probe_impl.probe_all(service.list_channels())
    health_store.record_report(second)
    health_store.sync_registry(service, author="tester", note="第二轮")

    assert DEFAULT_UNHEALTHY_TAG not in service.require_channel("beta").tags
    assert service.require_channel("beta").enabled is True
    assert {c.id for c in service.list_channels()} == {"alpha", "beta", "stopped"}
    assert health_store.count() == 4
    assert [h.status for h in health_store.history("beta")] == [
        HealthStatus.DEAD,
        HealthStatus.OK,
    ]
