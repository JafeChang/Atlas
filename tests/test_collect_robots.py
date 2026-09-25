"""T-102 robots.txt 合规测试：全部用注入的假 robots fetcher，不打网络。"""

from __future__ import annotations

import urllib.robotparser as robotparser

import pytest

from atlas.collect.fetch import FetchHTTPError, FetchResult, FetchTimeout
from atlas.collect.robots import (
    FetcherRobotsAdapter,
    RobotsCache,
    RobotsFetchResult,
    RobotsOutcome,
)

ROBOTS_DISALLOW = "User-agent: *\nDisallow: /private/\n"
ROBOTS_UA_SPECIFIC = "User-agent: atlas\nDisallow: /\n"


class FakeRobotsFetcher:
    """假 robots 获取器：可指定状态码 / 文本 / 错误，并记录每次调用。"""

    def __init__(
        self,
        body: str = "",
        *,
        status: int | None = 200,
        error: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.error = error
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def __call__(self, robots_url: str, *, user_agent: str) -> RobotsFetchResult:
        self.calls.append((robots_url, user_agent))
        if self.raises is not None:
            raise self.raises
        return RobotsFetchResult(
            url=robots_url,
            status_code=self.status,
            body=self.body.encode("utf-8"),
            error=self.error,
        )


def make_cache(fetcher: FakeRobotsFetcher, **kwargs: object) -> RobotsCache:
    return RobotsCache(fetcher, user_agent="Atlas/0.1.0", **kwargs)  # type: ignore[arg-type]


# --- 允许 / 拒绝 --------------------------------------------------------------


def test_disallowed_path_is_denied_with_rule_reason() -> None:
    fetcher = FakeRobotsFetcher(ROBOTS_DISALLOW)
    cache = make_cache(fetcher)

    decision = cache.check("https://example.com/private/page.html")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_BY_RULES
    assert "robots.txt" in decision.reason and "example.com" in decision.reason
    assert decision.robots_url == "https://example.com/robots.txt"
    assert decision.status_code == 200


def test_allowed_path_passes() -> None:
    cache = make_cache(FakeRobotsFetcher(ROBOTS_DISALLOW))

    decision = cache.check("https://example.com/public/page.html")

    assert decision.allowed is True
    assert decision.outcome is RobotsOutcome.ALLOWED
    assert decision.reason


def test_rules_are_evaluated_for_the_requested_user_agent() -> None:
    cache = make_cache(FakeRobotsFetcher(ROBOTS_UA_SPECIFIC))

    ours = cache.check("https://example.com/feed.xml", user_agent="Atlas/0.1.0")
    other = cache.check("https://example.com/feed.xml", user_agent="OtherBot/2.0")

    assert ours.allowed is False
    assert ours.outcome is RobotsOutcome.DISALLOWED_BY_RULES
    assert other.allowed is True


def test_non_default_port_produces_port_specific_robots_url() -> None:
    fetcher = FakeRobotsFetcher("")
    cache = make_cache(fetcher)

    cache.check("https://example.com:8443/feed.xml")

    assert fetcher.calls[0][0] == "https://example.com:8443/robots.txt"


# --- 保守策略（无法确认 = 不允许） --------------------------------------------


def test_missing_robots_txt_is_treated_as_no_restriction() -> None:
    cache = make_cache(FakeRobotsFetcher(status=404))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is True
    assert decision.outcome is RobotsOutcome.ALLOWED_NO_ROBOTS
    assert "404" in decision.reason


def test_missing_robots_txt_can_be_tightened_to_conservative() -> None:
    cache = make_cache(FakeRobotsFetcher(status=404), allow_when_absent=False)

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE


def test_server_error_is_conservatively_denied() -> None:
    cache = make_cache(FakeRobotsFetcher(status=503))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
    assert "503" in decision.reason


def test_forbidden_status_is_denied() -> None:
    cache = make_cache(FakeRobotsFetcher(status=403))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_FORBIDDEN
    assert "403" in decision.reason


def test_network_failure_is_conservatively_denied_with_reason() -> None:
    cache = make_cache(FakeRobotsFetcher(raises=ConnectionResetError("boom")))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
    assert "ConnectionResetError" in decision.reason and "boom" in decision.reason


def test_fetcher_reported_error_is_conservatively_denied() -> None:
    cache = make_cache(FakeRobotsFetcher(error="FetchTimeout: 请求超时"))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
    assert "FetchTimeout" in decision.reason


def test_missing_status_code_is_conservatively_denied() -> None:
    cache = make_cache(FakeRobotsFetcher(status=None))

    decision = cache.check("https://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE


def test_parse_failure_is_conservatively_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: robotparser.RobotFileParser, lines: object) -> None:
        raise ValueError("坏规则")

    monkeypatch.setattr(robotparser.RobotFileParser, "parse", boom)
    cache = make_cache(FakeRobotsFetcher(ROBOTS_DISALLOW))

    decision = cache.check("https://example.com/private/x")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
    assert "解析失败" in decision.reason and "ValueError" in decision.reason


def test_rule_matching_failure_is_conservatively_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(self: robotparser.RobotFileParser, useragent: str, url: str) -> bool:
        raise RuntimeError("匹配炸了")

    monkeypatch.setattr(robotparser.RobotFileParser, "can_fetch", boom)
    cache = make_cache(FakeRobotsFetcher(ROBOTS_DISALLOW))

    decision = cache.check("https://example.com/public/x")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
    assert "RuntimeError" in decision.reason


def test_invalid_url_is_denied_without_fetching() -> None:
    fetcher = FakeRobotsFetcher("")
    cache = make_cache(fetcher)

    decision = cache.check("ftp://example.com/feed.xml")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_INVALID_URL
    assert fetcher.calls == []


def test_fetcher_returning_wrong_type_is_a_loud_wiring_error() -> None:
    def bad_fetcher(robots_url: str, *, user_agent: str) -> str:
        return "not a RobotsFetchResult"

    cache = RobotsCache(bad_fetcher, user_agent="Atlas/0.1.0")

    with pytest.raises(TypeError):
        cache.check("https://example.com/feed.xml")


# --- 缓存 ---------------------------------------------------------------------


def test_robots_txt_is_fetched_once_per_origin() -> None:
    fetcher = FakeRobotsFetcher(ROBOTS_DISALLOW)
    cache = make_cache(fetcher)

    first = cache.check("https://example.com/private/a")
    second = cache.check("https://example.com/private/b")

    assert len(fetcher.calls) == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert cache.cached_origins() == ("https://example.com",)


def test_failures_are_negatively_cached() -> None:
    fetcher = FakeRobotsFetcher(status=500)
    cache = make_cache(fetcher)

    cache.check("https://example.com/a")
    cache.check("https://example.com/b")

    assert len(fetcher.calls) == 1


def test_different_origins_are_fetched_separately() -> None:
    fetcher = FakeRobotsFetcher(ROBOTS_DISALLOW)
    cache = make_cache(fetcher)

    cache.check("https://example.com/a")
    cache.check("https://other.test/a")
    cache.check("http://example.com/a")  # 不同 scheme = 不同 origin

    assert [call[0] for call in fetcher.calls] == [
        "https://example.com/robots.txt",
        "https://other.test/robots.txt",
        "http://example.com/robots.txt",
    ]


def test_invalidate_forces_refetch() -> None:
    fetcher = FakeRobotsFetcher(ROBOTS_DISALLOW)
    cache = make_cache(fetcher)

    cache.check("https://example.com/a")
    cache.invalidate("https://example.com/b")
    cache.check("https://example.com/c")

    assert len(fetcher.calls) == 2


def test_cache_ttl_expires_the_entry() -> None:
    now = [0.0]
    fetcher = FakeRobotsFetcher(ROBOTS_DISALLOW)
    cache = make_cache(fetcher, cache_ttl_seconds=60.0, clock=lambda: now[0])

    cache.check("https://example.com/a")
    now[0] = 120.0
    decision = cache.check("https://example.com/b")

    assert len(fetcher.calls) == 2
    assert decision.from_cache is False


def test_blank_user_agent_is_rejected() -> None:
    with pytest.raises(ValueError):
        RobotsCache(FakeRobotsFetcher(""), user_agent="  ")


# --- FetcherRobotsAdapter -----------------------------------------------------


class StubContentFetcher:
    def __init__(self, result: FetchResult | None = None, raises: BaseException | None = None):
        self.result = result
        self.raises = raises
        self.requests: list[object] = []

    def __call__(self, request: object) -> FetchResult:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        assert self.result is not None
        return self.result


def test_adapter_maps_successful_fetch_to_robots_result() -> None:
    fetcher = StubContentFetcher(
        FetchResult(
            url="https://example.com/robots.txt",
            status_code=200,
            content=ROBOTS_DISALLOW.encode("utf-8"),
        )
    )
    cache = RobotsCache.from_fetcher(fetcher, user_agent="Atlas/0.1.0")

    decision = cache.check("https://example.com/private/x")

    assert decision.allowed is False
    assert decision.outcome is RobotsOutcome.DISALLOWED_BY_RULES
    request = fetcher.requests[0]
    assert request.url == "https://example.com/robots.txt"  # type: ignore[attr-defined]
    assert request.user_agent == "Atlas/0.1.0"  # type: ignore[attr-defined]


def test_adapter_records_fetch_error_text() -> None:
    fetcher = StubContentFetcher(raises=FetchTimeout("超时了", url="https://example.com/robots.txt"))
    adapter = FetcherRobotsAdapter(fetcher, timeout_seconds=5.0)

    result = adapter("https://example.com/robots.txt", user_agent="Atlas/0.1.0")

    assert result.status_code is None
    assert result.error is not None and "FetchTimeout" in result.error


def test_adapter_propagates_non_fetch_errors() -> None:
    """非 FetchError 属于接线错误，不被适配器吞掉。"""
    fetcher = StubContentFetcher(raises=ValueError("接线炸了"))
    adapter = FetcherRobotsAdapter(fetcher)

    with pytest.raises(ValueError):
        adapter("https://example.com/robots.txt", user_agent="Atlas/0.1.0")


def test_adapter_maps_http_error_to_error_text() -> None:
    fetcher = StubContentFetcher(raises=FetchHTTPError("HTTP 500", status_code=500))
    adapter = FetcherRobotsAdapter(fetcher)

    result = adapter("https://example.com/robots.txt", user_agent="Atlas/0.1.0")

    assert result.error is not None and "FetchHTTPError" in result.error
