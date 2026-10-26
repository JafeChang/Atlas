"""T-102 抓取层测试：注入假 fetcher / 假 opener，不打任何网络。"""

from __future__ import annotations

import email.message
import http.client
import io
import socket
import urllib.error

import pytest

from atlas.collect.fetch import (
    DEFAULT_USER_AGENT,
    FailureKind,
    FetchConnectionError,
    FetchError,
    FetchFailure,
    FetchHTTPError,
    FetchOutcome,
    FetchRequest,
    FetchResult,
    FetchTimeout,
    FetchTransportError,
    UrllibFetcher,
    UserAgentError,
    fetch_once,
    resolve_user_agent,
)
from atlas.contracts import InvalidContractStateError
from atlas.registry.schema import Channel, FetchSpec, FetchType

URL = "https://example.com/feed.xml"
UA = "Atlas/0.1.0 (test)"


def make_request(**kwargs: object) -> FetchRequest:
    payload = {"url": URL, "user_agent": UA}
    payload.update(kwargs)
    return FetchRequest(**payload)  # type: ignore[arg-type]


def make_channel(**kwargs: object) -> Channel:
    payload = {
        "id": "example",
        "industry_id": "ai",
        "type": FetchType.RSS,
        "endpoint": URL,
        "fetch_spec": FetchSpec(type=FetchType.RSS),
        "interval_seconds": 3600,
        "enabled": True,
    }
    payload.update(kwargs)
    return Channel(**payload)  # type: ignore[arg-type]


class StubFetcher:
    """假 fetcher：返回预设结果或抛预设异常，并记录调用。"""

    def __init__(self, result: object = None, raises: BaseException | None = None) -> None:
        self.result = result
        self.raises = raises
        self.requests: list[FetchRequest] = []

    def __call__(self, request: FetchRequest) -> FetchResult:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.result  # type: ignore[return-value]


# --- fetch_once：成功路径 -----------------------------------------------------


def test_successful_fetch_returns_result() -> None:
    body = b"<rss>hello</rss>"
    fetcher = StubFetcher(
        FetchResult(url=URL, status_code=200, content=body, headers={"etag": "abc"})
    )

    outcome = fetch_once(fetcher, make_request())

    assert outcome.ok is True
    assert outcome.failure is None
    assert outcome.unwrap().content == body
    assert outcome.unwrap().status_code == 200
    assert fetcher.requests[0].user_agent == UA


def test_non_utf8_bytes_are_preserved_verbatim() -> None:
    body = "\u4e2d\u6587".encode("gb18030")
    fetcher = StubFetcher(FetchResult(url=URL, status_code=200, content=body))

    outcome = fetch_once(fetcher, make_request())

    assert outcome.unwrap().content == body


# --- fetch_once：失败分类 -----------------------------------------------------


def test_non_2xx_status_is_http_status_failure() -> None:
    fetcher = StubFetcher(FetchResult(url=URL, status_code=404, content=b"nope"))

    outcome = fetch_once(fetcher, make_request())

    assert outcome.ok is False
    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.HTTP_STATUS
    assert outcome.failure.status_code == 404
    assert "404" in outcome.failure.message


def test_empty_2xx_body_is_its_own_failure_kind() -> None:
    fetcher = StubFetcher(FetchResult(url=URL, status_code=200, content=b""))

    outcome = fetch_once(fetcher, make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.EMPTY_BODY
    assert outcome.failure.status_code == 200


def test_timeout_exception_is_classified() -> None:
    outcome = fetch_once(StubFetcher(raises=FetchTimeout("超时", url=URL)), make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.TIMEOUT
    assert outcome.failure.exception_type == "FetchTimeout"


def test_builtin_timeout_is_classified() -> None:
    outcome = fetch_once(StubFetcher(raises=TimeoutError("太慢")), make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.TIMEOUT


def test_socket_timeout_is_classified() -> None:
    outcome = fetch_once(StubFetcher(raises=socket.timeout("socket 超时")), make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.TIMEOUT


def test_os_error_is_classified_as_connection() -> None:
    outcome = fetch_once(StubFetcher(raises=OSError("连接被拒绝")), make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.CONNECTION
    assert outcome.failure.exception_type == "OSError"


def test_typed_connection_error_keeps_its_kind() -> None:
    outcome = fetch_once(
        StubFetcher(raises=FetchConnectionError("dns 失败", url=URL)), make_request()
    )

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.CONNECTION


def test_unexpected_exception_is_transport_with_type_recorded() -> None:
    outcome = fetch_once(StubFetcher(raises=ValueError("怪东西")), make_request())

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.TRANSPORT
    assert outcome.failure.exception_type == "ValueError"
    assert "ValueError" in outcome.failure.message


def test_http_error_from_fetcher_keeps_status_code() -> None:
    outcome = fetch_once(
        StubFetcher(raises=FetchHTTPError("HTTP 503", url=URL, status_code=503)),
        make_request(),
    )

    assert outcome.failure is not None
    assert outcome.failure.kind is FailureKind.HTTP_STATUS
    assert outcome.failure.status_code == 503


def test_blank_request_user_agent_is_refused() -> None:
    with pytest.raises(UserAgentError) as excinfo:
        fetch_once(StubFetcher(FetchResult(url=URL, status_code=200, content=b"x")),
                   make_request(user_agent="   "))

    assert excinfo.value.kind is FailureKind.USER_AGENT_POLICY


# --- 接线错误必须响亮失败（不用 except 掩盖） ----------------------------------


def test_fetcher_returning_wrong_type_is_a_loud_wiring_error() -> None:
    with pytest.raises(TypeError) as excinfo:
        fetch_once(StubFetcher("不是 FetchResult"), make_request())

    assert "FetchResult" in str(excinfo.value)


def test_fetcher_returning_non_bytes_content_is_a_loud_wiring_error() -> None:
    with pytest.raises(TypeError) as excinfo:
        fetch_once(
            StubFetcher(FetchResult(url=URL, status_code=200, content="文本")),  # type: ignore[arg-type]
            make_request(),
        )

    assert "bytes" in str(excinfo.value)


# --- FetchOutcome 不变量 ------------------------------------------------------


def test_outcome_requires_exactly_one_of_result_or_failure() -> None:
    with pytest.raises(ValueError):
        FetchOutcome()
    with pytest.raises(ValueError):
        FetchOutcome(
            result=FetchResult(url=URL, status_code=200, content=b"x"),
            failure=FetchFailure(kind=FailureKind.TRANSPORT, message="m", url=URL),
        )


def test_outcome_unwrap_raises_on_failure() -> None:
    outcome = FetchOutcome.failed(
        FetchFailure(kind=FailureKind.TIMEOUT, message="超时了", url=URL)
    )

    with pytest.raises(FetchError) as excinfo:
        outcome.unwrap()

    assert "timeout" in str(excinfo.value)
    assert outcome.failure is not None
    assert outcome.failure.as_dict()["kind"] == "timeout"


# --- resolve_user_agent -------------------------------------------------------


def test_default_user_agent_is_used_when_channel_declares_none() -> None:
    assert resolve_user_agent(make_channel()) == DEFAULT_USER_AGENT
    assert "Mozilla" not in DEFAULT_USER_AGENT


def test_known_option_name_is_resolved() -> None:
    assert "curl" in resolve_user_agent(make_channel(user_agent="curl"))


def test_full_user_agent_string_is_used_verbatim() -> None:
    assert resolve_user_agent(make_channel(user_agent="MyBot/2.0 (+https://x.test)")) == (
        "MyBot/2.0 (+https://x.test)"
    )


def test_unknown_option_name_fails_loudly_instead_of_defaulting() -> None:
    with pytest.raises(UserAgentError) as excinfo:
        resolve_user_agent(make_channel(user_agent="mystery-bot"))

    assert excinfo.value.kind is FailureKind.UNKNOWN_USER_AGENT
    assert "mystery-bot" in str(excinfo.value)


def test_browser_masquerade_without_justification_is_refused() -> None:
    with pytest.raises(UserAgentError) as excinfo:
        resolve_user_agent(make_channel(user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0"))

    assert excinfo.value.kind is FailureKind.USER_AGENT_POLICY


def test_browser_masquerade_with_justification_is_allowed() -> None:
    channel = make_channel(
        user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0",
        user_agent_justification="源站按 UA 分发改版页面，已获书面许可",
    )

    assert resolve_user_agent(channel).startswith("Mozilla/")


def test_blank_user_agent_option_is_rejected_at_config_layer() -> None:
    """空白 UA 选项根本构造不出来（配置层已挡），执行器因此不会见到它。"""
    with pytest.raises(InvalidContractStateError):
        make_channel(user_agent="   ")


def test_executor_guard_refuses_blank_user_agent_option() -> None:
    """执行器不依赖"配置层一定挡住了"这个前提，自己再兜一层。"""

    class StubChannel:
        id = "example"
        endpoint = URL
        user_agent = "   "
        user_agent_justification = None

    with pytest.raises(UserAgentError) as excinfo:
        resolve_user_agent(StubChannel())  # type: ignore[arg-type]

    assert excinfo.value.kind is FailureKind.UNKNOWN_USER_AGENT


# --- UrllibFetcher（用假 opener，仍然不打网络） -------------------------------


class FakeResponse:
    def __init__(
        self,
        *,
        url: str = URL,
        status: int = 200,
        body: bytes = b"payload",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self._body = body
        self.headers = email.message.Message()
        for key, value in (headers or {"Content-Type": "application/xml"}).items():
            self.headers[key] = value
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, response: FakeResponse | None = None, raises: BaseException | None = None):
        self.response = response
        self.raises = raises
        self.last_request: object | None = None
        self.last_timeout: float | None = None

    def open(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.last_request = request
        self.last_timeout = timeout
        if self.raises is not None:
            raise self.raises
        assert self.response is not None
        return self.response


def make_urllib_fetcher(opener: FakeOpener, *, clock: object = None) -> UrllibFetcher:
    ticks = iter([10.0, 10.25])
    return UrllibFetcher(opener=opener, clock=clock or (lambda: next(ticks)))  # type: ignore[arg-type]


def test_urllib_fetcher_maps_response_and_sends_declared_ua() -> None:
    response = FakeResponse(headers={"Content-Type": "application/rss+xml", "ETag": "v1"})
    opener = FakeOpener(response=response)
    fetcher = make_urllib_fetcher(opener)

    result = fetcher(make_request(timeout_seconds=12.5))

    assert result.status_code == 200
    assert result.content == b"payload"
    assert result.url == URL
    assert result.headers["etag"] == "v1" and result.headers["content-type"].startswith("application/rss")
    assert result.elapsed_seconds == pytest.approx(0.25)
    assert opener.last_timeout == 12.5
    assert response.closed is True
    headers = {k.lower(): v for k, v in opener.last_request.header_items()}  # type: ignore[attr-defined]
    assert headers["user-agent"] == UA
    assert "Mozilla" not in headers["user-agent"]


def test_urllib_fetcher_maps_http_error() -> None:
    error = urllib.error.HTTPError(
        URL, 503, "Service Unavailable", email.message.Message(), io.BytesIO(b"")
    )
    fetcher = make_urllib_fetcher(FakeOpener(raises=error))

    with pytest.raises(FetchHTTPError) as excinfo:
        fetcher(make_request())

    assert excinfo.value.status_code == 503
    assert excinfo.value.kind is FailureKind.HTTP_STATUS


def test_urllib_fetcher_maps_url_error_with_timeout_reason() -> None:
    error = urllib.error.URLError(socket.timeout("timed out"))
    fetcher = make_urllib_fetcher(FakeOpener(raises=error))

    with pytest.raises(FetchTimeout):
        fetcher(make_request())


def test_urllib_fetcher_maps_url_error_with_connection_reason() -> None:
    error = urllib.error.URLError(ConnectionRefusedError("refused"))
    fetcher = make_urllib_fetcher(FakeOpener(raises=error))

    with pytest.raises(FetchConnectionError):
        fetcher(make_request())


def test_urllib_fetcher_maps_protocol_exception() -> None:
    fetcher = make_urllib_fetcher(FakeOpener(raises=http.client.BadStatusLine("junk")))

    with pytest.raises(FetchTransportError):
        fetcher(make_request())
