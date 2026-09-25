"""T-102 采集执行器测试：全部注入假 fetcher / 假 robots / 假时钟，不打网络、不落盘。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.collect.executor import (
    CollectionFailure,
    CollectionResult,
    Collector,
    ResultStatus,
    RetryPolicy,
    collect_channels,
    collect_one,
)
from atlas.collect.fetch import (
    DEFAULT_USER_AGENT,
    FailureKind,
    FetchRequest,
    FetchResult,
    FetchTimeout,
)
from atlas.collect.robots import RobotsCache, RobotsFetchResult
from atlas.collect.throttle import DomainThrottle, domain_of
from atlas.contracts import (
    InvalidContractStateError,
    RawRecord,
    RawStore,
    content_sha256,
    raw_id_for,
)
from atlas.registry.schema import Channel, FetchSpec, FetchType

URL_A = "https://a.test/feed.xml"
URL_A2 = "https://a.test/other.xml"  # 同域（a.test），用于限速断言
URL_B = "https://b.test/feed.xml"
URL_C = "https://c.test/feed.xml"
BODY_A = b"<rss version='2.0'><channel><title>A</title></channel></rss>"
BODY_B = b"<rss version='2.0'><channel><title>B</title></channel></rss>"
FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
ROBOTS_DENY_ALL = "User-agent: *\nDisallow: /\n"


class FakeClock:
    """假时钟 + 假 sleeper：不睡真实时间，但能断言"睡了几次、睡了多久"。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeFetcher:
    """按 URL 预置响应（或异常）的假 fetcher；同一 URL 预置多个值即模拟重试。"""

    def __init__(self, table: dict[str, list[object]]) -> None:
        self.table = {url: list(values) for url, values in table.items()}
        self.calls: list[FetchRequest] = []

    def __call__(self, request: FetchRequest) -> FetchResult:
        self.calls.append(request)
        if request.url not in self.table:
            raise AssertionError(f"未预置的 URL 被请求：{request.url}（测试想抓网络了？）")
        queue = self.table[request.url]
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]

    def calls_for(self, url: str) -> list[FetchRequest]:
        return [call for call in self.calls if call.url == url]


class FakeRobotsFetcher:
    def __init__(self, body: str = "", *, status: int = 200, raises: BaseException | None = None):
        self.body = body
        self.status = status
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, robots_url: str, *, user_agent: str) -> RobotsFetchResult:
        self.calls.append(robots_url)
        if self.raises is not None:
            raise self.raises
        return RobotsFetchResult(url=robots_url, status_code=self.status, body=self.body.encode())


def ok(url: str, body: bytes = BODY_A) -> FetchResult:
    return FetchResult(url=url, status_code=200, content=body)


def make_channel(
    channel_id: str,
    *,
    endpoint: str = URL_A,
    interval_seconds: int = 3600,
    rate_limit_seconds: int | None = 0,
    enabled: bool = True,
    user_agent: str | None = None,
    user_agent_justification: str | None = None,
) -> Channel:
    return Channel(
        id=channel_id,
        industry_id="ai",
        type=FetchType.RSS,
        endpoint=endpoint,
        fetch_spec=FetchSpec(type=FetchType.RSS),
        interval_seconds=interval_seconds,
        rate_limit_seconds=rate_limit_seconds,
        user_agent=user_agent,
        user_agent_justification=user_agent_justification,
        enabled=enabled,
    )


def make_collector(
    fetcher: FakeFetcher,
    *,
    robots_fetcher: FakeRobotsFetcher | None = None,
    clock: FakeClock | None = None,
    retry: RetryPolicy | None = None,
    throttle: DomainThrottle | None = None,
    **kwargs: object,
) -> tuple[Collector, FakeClock, FakeRobotsFetcher]:
    clock = clock or FakeClock()
    robots_fetcher = robots_fetcher or FakeRobotsFetcher("")
    robots = RobotsCache(robots_fetcher, user_agent=DEFAULT_USER_AGENT)
    throttle = throttle or DomainThrottle(
        clock=clock, sleeper=clock.sleep, global_min_interval=0.0
    )
    collector = Collector(
        fetcher=fetcher,
        robots=robots,
        throttle=throttle,
        retry=retry,
        sleeper=clock.sleep,
        clock=clock,
        wall_clock=lambda: FIXED_TIME,
        **kwargs,  # type: ignore[arg-type]
    )
    return collector, clock, robots_fetcher


# --- 成功路径与幂等 -----------------------------------------------------------


def test_success_returns_rawrecord_and_raw_bytes() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.status is ResultStatus.COLLECTED
    assert result.ok is True
    assert result.failure is None
    assert result.attempts == 1
    raw, content = result.unwrap()
    assert content == BODY_A
    assert raw.channel_id == "alpha"
    assert raw.endpoint == URL_A
    assert raw.http_status == 200
    assert raw.byte_length == len(BODY_A)
    assert raw.fetched_at == FIXED_TIME


def test_raw_id_comes_from_contracts_raw_id_for() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    raw, content = collector.collect(make_channel("alpha")).unwrap()

    assert raw.content_sha256 == content_sha256(content)
    assert raw.raw_id == raw_id_for("alpha", URL_A, raw.content_sha256)
    assert raw.raw_id.startswith("raw_")


def test_rerun_with_same_content_is_idempotent_across_the_contract_store() -> None:
    """同内容重跑 → 同一个 raw_id；写进契约级 RawStore 也不产生第二条。"""
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)
    channel = make_channel("alpha")
    store = RawStore()

    first_raw, first_content = collector.collect(channel).unwrap()
    second_raw, second_content = collector.collect(channel).unwrap()

    assert first_raw.raw_id == second_raw.raw_id
    assert first_content == second_content
    store.put(first_raw, first_content)
    store.put(second_raw, second_content)
    assert store.all_raw_ids() == [first_raw.raw_id]


def test_changed_content_produces_a_new_raw_instead_of_overwriting() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A, BODY_A), ok(URL_A, BODY_B)]})
    collector, _, _ = make_collector(fetcher)
    channel = make_channel("alpha")
    store = RawStore()

    first = collector.collect(channel).unwrap()
    second = collector.collect(channel).unwrap()

    assert first[0].raw_id != second[0].raw_id
    store.put(*first)
    store.put(*second)
    assert store.all_raw_ids() == sorted([first[0].raw_id, second[0].raw_id])
    assert store.get_content(first[0].raw_id) == BODY_A
    assert store.get_content(second[0].raw_id) == BODY_B


def test_declared_user_agent_option_is_sent() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, robots_fetcher = make_collector(fetcher)

    collector.collect(make_channel("alpha", user_agent="curl"))

    assert fetcher.calls[0].user_agent.startswith("curl/")
    assert robots_fetcher.calls == ["https://a.test/robots.txt"]


def test_default_user_agent_does_not_masquerade_as_browser() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    collector.collect(make_channel("alpha"))

    assert fetcher.calls[0].user_agent == DEFAULT_USER_AGENT
    assert "Mozilla" not in fetcher.calls[0].user_agent


# --- 失败：有界重试 + 不留半成品 ----------------------------------------------


def test_persistent_timeout_fails_without_partial_record() -> None:
    fetcher = FakeFetcher({URL_A: [FetchTimeout("超时", url=URL_A)]})
    collector, clock, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.status is ResultStatus.FAILED
    assert result.ok is False
    assert result.raw is None and result.content is None
    assert result.failure is not None
    assert result.failure.channel_id == "alpha"
    assert result.failure.kind is FailureKind.TIMEOUT
    assert result.failure.attempts == 3
    assert result.attempts == 3
    assert result.note is not None and "重试耗尽" in result.note
    assert clock.sleeps == [1.0, 2.0]  # 有界退避，不无限重试
    assert len(fetcher.calls_for(URL_A)) == 3


def test_retry_succeeds_on_third_attempt() -> None:
    fetcher = FakeFetcher(
        {URL_A: [FetchTimeout("超时 1", url=URL_A), FetchTimeout("超时 2", url=URL_A), ok(URL_A)]}
    )
    collector, clock, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.status is ResultStatus.COLLECTED
    assert result.attempts == 3
    assert clock.sleeps == [1.0, 2.0]
    assert result.unwrap()[1] == BODY_A


def test_server_error_is_retried_and_then_reported_with_status() -> None:
    fetcher = FakeFetcher({URL_A: [FetchResult(url=URL_A, status_code=503, content=b"busy")]})
    collector, clock, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.status is ResultStatus.FAILED
    assert result.failure is not None
    assert result.failure.kind is FailureKind.HTTP_STATUS
    assert result.failure.status_code == 503
    assert result.attempts == 3
    assert clock.sleeps == [1.0, 2.0]


def test_client_error_is_not_retried() -> None:
    fetcher = FakeFetcher({URL_A: [FetchResult(url=URL_A, status_code=404, content=b"gone")]})
    collector, clock, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.failure is not None
    assert result.failure.status_code == 404
    assert result.attempts == 1
    assert clock.sleeps == []


def test_empty_body_is_reported_and_not_retried() -> None:
    fetcher = FakeFetcher({URL_A: [FetchResult(url=URL_A, status_code=200, content=b"")]})
    collector, clock, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.failure is not None
    assert result.failure.kind is FailureKind.EMPTY_BODY
    assert result.attempts == 1
    assert clock.sleeps == []


def test_max_attempts_one_gives_single_try() -> None:
    fetcher = FakeFetcher({URL_A: [FetchTimeout("超时", url=URL_A)]})
    collector, clock, _ = make_collector(fetcher, retry=RetryPolicy(max_attempts=1))

    result = collector.collect(make_channel("alpha"))

    assert result.attempts == 1
    assert clock.sleeps == []


def test_failure_log_is_structured() -> None:
    fetcher = FakeFetcher({URL_A: [FetchTimeout("超时", url=URL_A)]})
    collector, _, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha"))

    assert result.failure is not None
    payload = result.failure.as_dict()
    assert payload["channel_id"] == "alpha"
    assert payload["endpoint"] == URL_A
    assert payload["kind"] == "timeout"
    assert payload["reason"]
    assert payload["attempts"] == 3
    assert payload["detail"] == "FetchTimeout"


def test_unclassified_fetcher_error_is_reported_as_transport() -> None:
    fetcher = FakeFetcher({URL_A: [ValueError("谁把我接错了")]})
    collector, _, _ = make_collector(fetcher, retry=RetryPolicy(max_attempts=1))

    result = collector.collect(make_channel("alpha"))

    assert result.failure is not None
    assert result.failure.kind is FailureKind.TRANSPORT
    assert "ValueError" in result.failure.reason


# --- 合规：robots 与 UA -------------------------------------------------------


def test_robots_denial_skips_before_any_fetch() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher, robots_fetcher=FakeRobotsFetcher(ROBOTS_DENY_ALL))

    result = collector.collect(make_channel("alpha"))

    assert result.status is ResultStatus.FAILED
    assert result.failure is not None
    assert result.failure.kind is FailureKind.ROBOTS_DENIED
    assert result.failure.robots_url == "https://a.test/robots.txt"
    assert result.failure.attempts == 0
    assert result.failure.detail == "disallowed_by_rules"
    assert fetcher.calls == []  # 绝不"先抓再说"
    assert result.raw is None


def test_robots_fetch_failure_is_conservative_and_skips_fetch() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(
        fetcher, robots_fetcher=FakeRobotsFetcher(raises=ConnectionResetError("reset"))
    )

    result = collector.collect(make_channel("alpha"))

    assert result.failure is not None
    assert result.failure.kind is FailureKind.ROBOTS_DENIED
    assert result.failure.detail == "disallowed_robots_unavailable"
    assert "ConnectionResetError" in result.failure.reason
    assert fetcher.calls == []


def test_robots_txt_is_fetched_once_even_with_retries() -> None:
    fetcher = FakeFetcher({URL_A: [FetchTimeout("超时", url=URL_A)]})
    collector, _, robots_fetcher = make_collector(fetcher)

    collector.collect(make_channel("alpha"))

    assert robots_fetcher.calls == ["https://a.test/robots.txt"]


def test_unknown_user_agent_option_fails_without_defaulting() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha", user_agent="mystery"))

    assert result.failure is not None
    assert result.failure.kind is FailureKind.UNKNOWN_USER_AGENT
    assert result.failure.attempts == 0
    assert fetcher.calls == []
    assert result.raw is None


def test_browser_masquerade_without_justification_is_refused_by_executor() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)
    channel = make_channel(
        "alpha", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
    )

    result = collector.collect(channel)

    assert result.failure is not None
    assert result.failure.kind is FailureKind.USER_AGENT_POLICY
    assert fetcher.calls == []


# --- 限速（用假时钟证明"等了多久"，不睡真实时间） -----------------------------


def test_same_domain_second_channel_waits_the_declared_interval() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)], URL_A2: [ok(URL_A2, BODY_B)]})
    collector, clock, _ = make_collector(fetcher)
    first = make_channel("alpha", endpoint=URL_A, rate_limit_seconds=30)
    second = make_channel("beta", endpoint=URL_A2, rate_limit_seconds=30)

    first_result = collector.collect(first)
    second_result = collector.collect(second)

    assert first_result.waited_seconds == 0.0
    assert second_result.status is ResultStatus.COLLECTED
    assert second_result.waited_seconds == pytest.approx(30.0)
    assert clock.sleeps == [30.0]
    assert fetcher.calls_for(first.endpoint)[0].url == URL_A


def test_wait_beyond_budget_fails_the_channel_instead_of_burning_time() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)], URL_A2: [ok(URL_A2, BODY_B)]})
    clock = FakeClock()
    throttle = DomainThrottle(
        clock=clock, sleeper=clock.sleep, global_min_interval=0.0, max_wait_seconds=5.0
    )
    collector, _, _ = make_collector(fetcher, clock=clock, throttle=throttle)

    collector.collect(make_channel("alpha", endpoint=URL_A, rate_limit_seconds=300))
    result = collector.collect(
        make_channel("beta", endpoint=URL_A2, rate_limit_seconds=300)
    )

    assert result.status is ResultStatus.FAILED
    assert result.failure is not None
    assert result.failure.kind is FailureKind.RATE_LIMIT_WAIT_TIMEOUT
    assert result.failure.attempts == 0
    assert len(fetcher.calls) == 1  # 未发起第二次请求
    assert clock.sleeps == []


def test_missing_rate_limit_uses_global_floor() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)], URL_A2: [ok(URL_A2, BODY_B)]})
    clock = FakeClock()
    throttle = DomainThrottle(clock=clock, sleeper=clock.sleep, global_min_interval=2.0)
    collector, _, _ = make_collector(fetcher, clock=clock, throttle=throttle)

    collector.collect(make_channel("alpha", endpoint=URL_A, rate_limit_seconds=None))
    result = collector.collect(
        make_channel("beta", endpoint=URL_A2, rate_limit_seconds=None)
    )

    assert result.waited_seconds == pytest.approx(2.0)
    assert clock.sleeps == [2.0]


def test_different_domains_are_not_throttled_against_each_other() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)], URL_B: [ok(URL_B, BODY_B)]})
    collector, clock, _ = make_collector(fetcher)

    collector.collect(make_channel("alpha", endpoint=URL_A, rate_limit_seconds=300))
    result = collector.collect(make_channel("beta", endpoint=URL_B, rate_limit_seconds=300))

    assert result.status is ResultStatus.COLLECTED
    assert clock.sleeps == []
    assert domain_of(URL_A) != domain_of(URL_B)


# --- 批量 ---------------------------------------------------------------------


def test_disabled_channel_is_skipped_with_reason() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    result = collector.collect(make_channel("alpha", enabled=False))

    assert result.status is ResultStatus.SKIPPED
    assert result.note is not None and "enabled=False" in result.note
    assert result.failure is None
    assert fetcher.calls == []


def test_include_disabled_collects_disabled_channel() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher, include_disabled=True)

    result = collector.collect(make_channel("alpha", enabled=False))

    assert result.status is ResultStatus.COLLECTED


def test_collect_all_preserves_order_and_isolates_failures() -> None:
    fetcher = FakeFetcher(
        {URL_A: [ok(URL_A)], URL_B: [FetchResult(url=URL_B, status_code=500, content=b"err")]}
    )
    collector, _, _ = make_collector(
        fetcher, robots_fetcher=FakeRobotsFetcher(ROBOTS_DENY_ALL), retry=RetryPolicy(max_attempts=1)
    )
    channels = [
        make_channel("alpha", endpoint=URL_A),
        make_channel("beta", endpoint=URL_B),
        make_channel("gamma", endpoint=URL_C),
    ]

    results = collector.collect_all(channels)

    assert [r.channel_id for r in results] == ["alpha", "beta", "gamma"]
    assert [r.status for r in results] == [
        ResultStatus.FAILED,  # robots deny-all 对全部渠道生效
        ResultStatus.FAILED,
        ResultStatus.FAILED,
    ]
    assert all(r.failure is not None and r.failure.kind is FailureKind.ROBOTS_DENIED for r in results)


def test_collect_all_continues_after_a_failing_channel() -> None:
    fetcher = FakeFetcher(
        {
            URL_A: [FetchTimeout("超时", url=URL_A)],
            URL_B: [ok(URL_B, BODY_B)],
            URL_C: [ok(URL_C, b"<rss>C</rss>")],
        }
    )
    collector, _, _ = make_collector(fetcher, retry=RetryPolicy(max_attempts=1))

    results = collector.collect_all(
        [
            make_channel("alpha", endpoint=URL_A),
            make_channel("beta", endpoint=URL_B),
            make_channel("gamma", endpoint=URL_C),
        ]
    )

    assert [r.status for r in results] == [
        ResultStatus.FAILED,
        ResultStatus.COLLECTED,
        ResultStatus.COLLECTED,
    ]
    assert results[1].unwrap()[1] == BODY_B


def test_collect_channels_function_returns_one_result_per_channel() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)], URL_B: [ok(URL_B, BODY_B)]})
    clock = FakeClock()

    results = collect_channels(
        [make_channel("alpha"), make_channel("beta", endpoint=URL_B)],
        fetcher=fetcher,
        robots=RobotsCache(FakeRobotsFetcher(""), user_agent=DEFAULT_USER_AGENT),
        throttle=DomainThrottle(clock=clock, sleeper=clock.sleep, global_min_interval=0.0),
        sleeper=clock.sleep,
        clock=clock,
        wall_clock=lambda: FIXED_TIME,
    )

    assert len(results) == 2
    assert all(r.status is ResultStatus.COLLECTED for r in results)


def test_collect_one_function_matches_collector() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    clock = FakeClock()

    result = collect_one(
        make_channel("alpha"),
        fetcher=fetcher,
        robots=RobotsCache(FakeRobotsFetcher(""), user_agent=DEFAULT_USER_AGENT),
        throttle=DomainThrottle(clock=clock, sleeper=clock.sleep, global_min_interval=0.0),
        sleeper=clock.sleep,
        clock=clock,
        wall_clock=lambda: FIXED_TIME,
    )

    assert result.status is ResultStatus.COLLECTED
    assert result.unwrap()[0].channel_id == "alpha"


# --- 结果与重试策略的不变量 ---------------------------------------------------


def test_collected_result_requires_raw_and_content() -> None:
    with pytest.raises(InvalidContractStateError):
        CollectionResult(channel_id="a", endpoint=URL_A, status=ResultStatus.COLLECTED)


def test_failed_result_requires_failure_and_no_partial_data() -> None:
    with pytest.raises(InvalidContractStateError):
        CollectionResult(channel_id="a", endpoint=URL_A, status=ResultStatus.FAILED)
    with pytest.raises(InvalidContractStateError):
        CollectionResult(
            channel_id="a",
            endpoint=URL_A,
            status=ResultStatus.FAILED,
            content=b"half",
            failure=CollectionFailure(
                channel_id="a",
                endpoint=URL_A,
                kind=FailureKind.TIMEOUT,
                reason="r",
                attempts=1,
            ),
        )


def test_collected_result_cannot_also_carry_a_failure() -> None:
    raw = RawRecord.create(channel_id="a", endpoint=URL_A, content=b"body")
    with pytest.raises(InvalidContractStateError):
        CollectionResult(
            channel_id="a",
            endpoint=URL_A,
            status=ResultStatus.COLLECTED,
            raw=raw,
            content=b"body",
            failure=CollectionFailure(
                channel_id="a",
                endpoint=URL_A,
                kind=FailureKind.TIMEOUT,
                reason="r",
                attempts=1,
            ),
        )


def test_skipped_result_requires_a_note() -> None:
    with pytest.raises(InvalidContractStateError):
        CollectionResult(channel_id="a", endpoint=URL_A, status=ResultStatus.SKIPPED)


def test_unwrap_refuses_non_collected_results() -> None:
    fetcher = FakeFetcher({URL_A: [ok(URL_A)]})
    collector, _, _ = make_collector(fetcher)

    with pytest.raises(InvalidContractStateError):
        collector.collect(make_channel("alpha", enabled=False)).unwrap()


def test_retry_policy_validation_and_backoff() -> None:
    with pytest.raises(InvalidContractStateError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(InvalidContractStateError):
        RetryPolicy(backoff_multiplier=0.5)

    policy = RetryPolicy(backoff_seconds=1.0, backoff_multiplier=3.0)
    assert [policy.delay_for(i) for i in (1, 2, 3)] == [1.0, 3.0, 9.0]
    assert policy.is_retryable(
        CollectionFailure(
            channel_id="a",
            endpoint=URL_A,
            kind=FailureKind.HTTP_STATUS,
            reason="503",
            attempts=1,
            status_code=503,
        )
    )
    assert not policy.is_retryable(
        CollectionFailure(
            channel_id="a",
            endpoint=URL_A,
            kind=FailureKind.HTTP_STATUS,
            reason="404",
            attempts=1,
            status_code=404,
        )
    )
    assert not policy.is_retryable(
        CollectionFailure(
            channel_id="a",
            endpoint=URL_A,
            kind=FailureKind.ROBOTS_DENIED,
            reason="denied",
            attempts=0,
        )
    )
