"""T-102 限速（throttle）测试：全程假时钟 + 假 sleeper，不睡真实时间。"""

from __future__ import annotations

import pytest

from atlas.collect.throttle import (
    DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS,
    DomainThrottle,
    ThrottleError,
    ThrottleTimeout,
    domain_of,
)
from atlas.registry.schema import Channel, FetchSpec, FetchType


class FakeClock:
    """假时钟：`sleep()` 推进自己的时间，同时记录每次休眠时长。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_channel(
    channel_id: str,
    *,
    endpoint: str = "https://example.com/feed.xml",
    interval_seconds: int = 3600,
    rate_limit_seconds: int | None = None,
) -> Channel:
    return Channel(
        id=channel_id,
        industry_id="ai",
        type=FetchType.RSS,
        endpoint=endpoint,
        fetch_spec=FetchSpec(type=FetchType.RSS),
        interval_seconds=interval_seconds,
        rate_limit_seconds=rate_limit_seconds,
        enabled=True,
    )


def make_throttle(clock: FakeClock, **kwargs: object) -> DomainThrottle:
    return DomainThrottle(clock=clock, sleeper=clock.sleep, **kwargs)  # type: ignore[arg-type]


# --- domain_of ---------------------------------------------------------------


def test_domain_of_normalizes_scheme_and_default_ports() -> None:
    assert domain_of("https://Example.COM/feed.xml") == "example.com"
    assert domain_of("http://example.com/feed.xml") == "example.com"
    assert domain_of("http://example.com:80/feed.xml") == "example.com"
    assert domain_of("https://example.com:443/feed.xml") == "example.com"


def test_domain_of_keeps_non_default_port() -> None:
    assert domain_of("https://example.com:8443/feed.xml") == "example.com:8443"


def test_domain_of_rejects_url_without_host() -> None:
    with pytest.raises(ThrottleError):
        domain_of("file:///tmp/feed.xml")


# --- 间隔取值规则 -------------------------------------------------------------


def test_explicit_rate_limit_seconds_is_used_verbatim() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=5.0)
    assert throttle.min_interval_for(make_channel("a", rate_limit_seconds=7)) == 7.0


def test_explicit_zero_means_no_extra_throttle() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=5.0)
    channel = make_channel("a", rate_limit_seconds=0)

    assert throttle.min_interval_for(channel) == 0.0
    throttle.reserve(domain_of(channel.endpoint), throttle.min_interval_for(channel))
    throttle.reserve(domain_of(channel.endpoint), throttle.min_interval_for(channel))

    assert clock.sleeps == []


def test_missing_rate_limit_falls_back_to_global_floor() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=2.5)
    channel = make_channel("a", interval_seconds=3600, rate_limit_seconds=None)

    # interval_seconds(3600) 只作为上界，实际生效的是全局下限
    assert throttle.min_interval_for(channel) == 2.5
    assert DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS == 1.0


# --- 等待行为 ----------------------------------------------------------------


def test_second_request_on_same_domain_waits_exactly_the_interval() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    channel = make_channel("a", rate_limit_seconds=30)

    first = throttle.reserve("example.com", throttle.min_interval_for(channel))
    second = throttle.reserve("example.com", throttle.min_interval_for(channel))

    assert first.waited_seconds == 0.0
    assert second.waited_seconds == 30.0
    assert clock.sleeps == [30.0]


def test_no_wait_after_interval_has_elapsed_on_the_clock() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    channel = make_channel("a", rate_limit_seconds=30)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    clock.now += 45.0  # 用假时钟推进，不睡
    decision = throttle.reserve("example.com", throttle.min_interval_for(channel))

    assert decision.waited_seconds == 0.0
    assert clock.sleeps == []


def test_different_domains_do_not_block_each_other() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    channel = make_channel("a", rate_limit_seconds=30)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    other = throttle.reserve("other.test", throttle.min_interval_for(channel))

    assert other.waited_seconds == 0.0
    assert clock.sleeps == []


def test_strictest_interval_wins_for_same_domain() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    strict = make_channel("strict", rate_limit_seconds=60)
    loose = make_channel("loose", rate_limit_seconds=1)

    throttle.reserve("example.com", throttle.min_interval_for(strict))
    decision = throttle.reserve("example.com", throttle.min_interval_for(loose))

    # 宽松渠道不得把同域间隔拉低
    assert decision.min_interval == 60.0
    assert decision.waited_seconds == 60.0


def test_waited_time_is_partial_when_clock_already_advanced() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    channel = make_channel("a", rate_limit_seconds=30)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    clock.now += 10.0
    decision = throttle.reserve("example.com", throttle.min_interval_for(channel))

    assert decision.waited_seconds == pytest.approx(20.0)
    assert clock.sleeps == [20.0]


# --- 超预算即失败（不抢跑、不静默跳过） ---------------------------------------


def test_wait_beyond_budget_raises_instead_of_sleeping() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0, max_wait_seconds=5.0)
    channel = make_channel("a", rate_limit_seconds=300)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    with pytest.raises(ThrottleTimeout) as excinfo:
        throttle.reserve("example.com", throttle.min_interval_for(channel))

    assert excinfo.value.needed == 300.0
    assert excinfo.value.max_wait == 5.0
    assert clock.sleeps == []  # 未眠即失败：绝不违规抢跑


def test_wait_within_budget_is_allowed() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0, max_wait_seconds=120.0)
    channel = make_channel("a", rate_limit_seconds=60)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    decision = throttle.reserve("example.com", throttle.min_interval_for(channel))

    assert decision.waited_seconds == 60.0


# --- 参数校验与观测 -----------------------------------------------------------


def test_negative_intervals_are_rejected() -> None:
    clock = FakeClock()
    with pytest.raises(ThrottleError):
        DomainThrottle(clock=clock, sleeper=clock.sleep, global_min_interval=-1.0)
    throttle = make_throttle(clock)
    with pytest.raises(ThrottleError):
        throttle.reserve("example.com", -1.0)


def test_stats_report_requests_and_waited_seconds() -> None:
    clock = FakeClock()
    throttle = make_throttle(clock, global_min_interval=0.0)
    channel = make_channel("a", rate_limit_seconds=10)

    throttle.reserve("example.com", throttle.min_interval_for(channel))
    throttle.reserve("example.com", throttle.min_interval_for(channel))

    stats = throttle.stats()
    assert stats["example.com"]["requests"] == 2.0
    assert stats["example.com"]["waited_seconds"] == 10.0
    assert stats["example.com"]["enforced_min_interval"] == 10.0
