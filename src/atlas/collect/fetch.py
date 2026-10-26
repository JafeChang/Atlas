"""单渠道 HTTP 抓取（SPEC §2.9 / T-102）。

职责边界：

- `Fetcher` 是**注入**的（`FetchRequest -> FetchResult` 的可调用对象）。
  执行器自己 new 客户端，测试就没法摆脱真实网络；注入之后测试用假 fetcher，
  一条网络都不会打。
- `UrllibFetcher` 是基于 stdlib `urllib.request` 的默认实现——**零新增依赖**。
- 失败一律**结构化**：`FailureKind` 区分 HTTP 状态 / 超时 / 连接 / 其它传输异常 /
  空响应体 / robots 拒绝 / 限速等待超时 / UA 选项非法。绝不把原因揉成一句"失败"。

关于 `except`：本模块捕获异常，但**不掩盖**——每条被捕获的异常都带
"异常类型名 + 原始消息"进入 `FetchFailure`，再计入采集结果与失败日志。
未预期的接线错误（如 fetcher 返回了不是 `FetchResult` 的东西）会**响亮失败**，
不会被当成"来源暂时不可用"。

**UA 策略**（SPEC §2.9 / §7.1）：`channel.user_agent` 是 **UA 选项名**，不是 UA 字符串。
- `None` → `DEFAULT_USER_AGENT`（自有 UA，声明身份）
- 命中 `USER_AGENT_OPTIONS` → 取对应字符串
- 看起来就是完整 UA 字符串（含 "/" 或空格）→ 原样使用
- 其余 → `UserAgentError`（**不静默退回默认 UA**，否则配置写错就会伪装成别的身份）
- 解析结果以 `Mozilla/` 开头（浏览器伪装）→ 必须带 `user_agent_justification`，
  否则 `UserAgentError`。`registry.schema` 已在配置层禁止无理由伪装，
  执行器**不绕过**该校验，而是再兜一层。
"""

from __future__ import annotations

import http.client
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Mapping, Optional, Protocol

if TYPE_CHECKING:  # 只用 atlas.registry.schema 的**类型**
    from atlas.registry.schema import Channel

__all__ = [
    "DEFAULT_USER_AGENT",
    "DEFAULT_TIMEOUT_SECONDS",
    "USER_AGENT_OPTIONS",
    "BROWSER_UA_PREFIX",
    "FailureKind",
    "FetchRequest",
    "FetchResult",
    "FetchFailure",
    "FetchOutcome",
    "FetchError",
    "FetchTimeout",
    "FetchConnectionError",
    "FetchHTTPError",
    "FetchTransportError",
    "UserAgentError",
    "Fetcher",
    "UrllibFetcher",
    "fetch_once",
    "resolve_user_agent",
]

#: 自有 UA：声明身份与用途，不冒充任何浏览器。
#: 运维如需加联系方式，优先在 `USER_AGENT_OPTIONS` 里登记一个自有选项，
#: 而不是改这里（改这里会让所有渠道一起变）。
DEFAULT_USER_AGENT = "Atlas/0.1.0 (Information Aggregation System)"

DEFAULT_TIMEOUT_SECONDS = 30.0

#: UA 选项名 → UA 字符串。可被调用方覆盖（`Collector(user_agent_options=...)`），
#: 以便沿用 `config/sources.yaml` 里已有的 `user_agents.options`。
USER_AGENT_OPTIONS: Mapping[str, str] = {
    "default": DEFAULT_USER_AGENT,
    "atlas": DEFAULT_USER_AGENT,
    "atlas_custom": DEFAULT_USER_AGENT,
    "atlas_crawler": "Atlas-Crawler/0.1.0 (+information aggregation; declared bot)",
    "curl": "curl/8.5.0",
    "wget": "Wget/1.21.4",
    "python_requests": "python-requests/2.32.0",
}

#: 浏览器伪装前缀（出现即需要显式理由）。
BROWSER_UA_PREFIX = "Mozilla/"


class FailureKind(str, Enum):
    """失败的判别字段：一条失败日志**必须**能落到这里的某一类。"""

    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    TRANSPORT = "transport"
    EMPTY_BODY = "empty_body"
    ROBOTS_DENIED = "robots_denied"
    RATE_LIMIT_WAIT_TIMEOUT = "rate_limit_wait_timeout"
    INVALID_URL = "invalid_url"
    UNKNOWN_USER_AGENT = "unknown_user_agent"
    USER_AGENT_POLICY = "user_agent_policy"


@dataclass(frozen=True)
class FetchRequest:
    """一次 HTTP 获取请求（只含 fetcher 需要知道的字段）。"""

    url: str
    user_agent: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    method: str = "GET"


@dataclass(frozen=True)
class FetchResult:
    """fetcher 的原始返回（成功拿到响应，无论状态码）。"""

    url: str
    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class Fetcher(Protocol):
    """HTTP 获取器协议。实现可以是 `UrllibFetcher`，也可以是测试里的假对象。"""

    def __call__(self, request: FetchRequest) -> FetchResult: ...


# --- 结构化异常 ---------------------------------------------------------------


class FetchError(Exception):
    """抓取失败基类：带判别类型，供执行器原样转成结构化失败。"""

    kind: FailureKind = FailureKind.TRANSPORT
    status_code: Optional[int] = None

    def __init__(self, message: str, *, url: Optional[str] = None) -> None:
        self.url = url
        super().__init__(message)


class FetchTimeout(FetchError):
    kind = FailureKind.TIMEOUT


class FetchConnectionError(FetchError):
    kind = FailureKind.CONNECTION


class FetchHTTPError(FetchError):
    kind = FailureKind.HTTP_STATUS

    def __init__(
        self, message: str, *, url: Optional[str] = None, status_code: Optional[int] = None
    ) -> None:
        self.status_code = status_code
        super().__init__(message, url=url)


class FetchTransportError(FetchError):
    kind = FailureKind.TRANSPORT


class UserAgentError(FetchError):
    """UA 选项名无法解析，或解析结果违反伪装禁令。"""

    def __init__(self, message: str, *, kind: FailureKind, url: Optional[str] = None) -> None:
        self.kind = kind
        super().__init__(message, url=url)


# --- 结构化失败 / 结果 ---------------------------------------------------------


@dataclass(frozen=True)
class FetchFailure:
    """结构化失败：原因可区分、可入日志、可断言。"""

    kind: FailureKind
    message: str
    url: str
    status_code: Optional[int] = None
    exception_type: Optional[str] = None

    def as_dict(self) -> dict:
        payload = {
            "kind": self.kind.value,
            "message": self.message,
            "url": self.url,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.exception_type is not None:
            payload["exception_type"] = self.exception_type
        return payload


@dataclass(frozen=True)
class FetchOutcome:
    """一次抓取尝试的结果：要么拿到 `FetchResult`，要么是结构化失败（互斥）。"""

    result: Optional[FetchResult] = None
    failure: Optional[FetchFailure] = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.failure is None):
            raise ValueError("FetchOutcome 必须恰好有 result 或 failure 之一（不允许半成品）")

    @property
    def ok(self) -> bool:
        return self.result is not None

    @classmethod
    def success(cls, result: FetchResult) -> "FetchOutcome":
        return cls(result=result)

    @classmethod
    def failed(cls, failure: FetchFailure) -> "FetchOutcome":
        return cls(failure=failure)

    def unwrap(self) -> FetchResult:
        if self.result is None:
            assert self.failure is not None
            raise FetchError(
                f"抓取失败（{self.failure.kind.value}）：{self.failure.message}",
                url=self.failure.url,
            )
        return self.result


# --- 单次抓取 ----------------------------------------------------------------


def fetch_once(fetcher: Fetcher, request: FetchRequest) -> FetchOutcome:
    """执行一次 HTTP 获取，把一切失败归类成 `FetchOutcome.failed`。

    本函数**不重试**（重试策略属于执行器）：一次调用 = 一个请求。
    """
    if not request.user_agent or not request.user_agent.strip():
        raise UserAgentError(
            "FetchRequest.user_agent 不得为空：无声明 UA 的请求不得发出（合规底线）",
            kind=FailureKind.USER_AGENT_POLICY,
            url=request.url,
        )

    try:
        result = fetcher(request)
    except FetchTimeout as exc:
        return FetchOutcome.failed(_failure_from(exc, request.url))
    except FetchConnectionError as exc:
        return FetchOutcome.failed(_failure_from(exc, request.url))
    except FetchHTTPError as exc:
        return FetchOutcome.failed(_failure_from(exc, request.url, status_code=exc.status_code))
    except FetchError as exc:
        return FetchOutcome.failed(_failure_from(exc, request.url))
    except (TimeoutError, socket.timeout) as exc:
        return FetchOutcome.failed(
            FetchFailure(
                kind=FailureKind.TIMEOUT,
                message=f"请求超时：{exc}",
                url=request.url,
                exception_type=type(exc).__name__,
            )
        )
    except OSError as exc:
        return FetchOutcome.failed(
            FetchFailure(
                kind=FailureKind.CONNECTION,
                message=f"连接失败：{exc}",
                url=request.url,
                exception_type=type(exc).__name__,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 归类而非掩盖：类型名与消息都保留
        return FetchOutcome.failed(
            FetchFailure(
                kind=FailureKind.TRANSPORT,
                message=f"fetcher 抛出未归类异常：{type(exc).__name__}: {exc}",
                url=request.url,
                exception_type=type(exc).__name__,
            )
        )

    if not isinstance(result, FetchResult):
        raise TypeError(
            "fetcher 必须返回 FetchResult，收到 "
            f"{type(result).__name__}（接线错误，不用 except 掩盖）"
        )
    if not isinstance(result.content, (bytes, bytearray)):
        raise TypeError(
            "FetchResult.content 必须是 bytes，收到 "
            f"{type(result.content).__name__}（接线错误，不用 except 掩盖）"
        )

    content = bytes(result.content)
    if not result.ok:
        return FetchOutcome.failed(
            FetchFailure(
                kind=FailureKind.HTTP_STATUS,
                message=f"HTTP {result.status_code}（非 2xx）",
                url=result.url or request.url,
                status_code=result.status_code,
            )
        )
    if not content:
        return FetchOutcome.failed(
            FetchFailure(
                kind=FailureKind.EMPTY_BODY,
                message=f"HTTP {result.status_code} 但响应体为空",
                url=result.url or request.url,
                status_code=result.status_code,
            )
        )
    return FetchOutcome.success(replace(result, content=content))


def _failure_from(
    exc: FetchError, url: str, *, status_code: Optional[int] = None
) -> FetchFailure:
    return FetchFailure(
        kind=exc.kind,
        message=str(exc),
        url=exc.url or url,
        status_code=status_code if status_code is not None else exc.status_code,
        exception_type=type(exc).__name__,
    )


# --- UA 解析 -----------------------------------------------------------------


def resolve_user_agent(
    channel: "Channel", *, options: Optional[Mapping[str, str]] = None
) -> str:
    """把 `channel.user_agent`（选项名）解析成实际 UA 字符串。

    规则见模块文档；解析不了就**响亮失败**，不猜、不退回默认。
    """
    table = USER_AGENT_OPTIONS if options is None else options
    declared = channel.user_agent
    if declared is None:
        return DEFAULT_USER_AGENT

    name = declared.strip()
    if not name:
        raise UserAgentError(
            f"channel.id={channel.id!r} 的 user_agent 为空白字符串",
            kind=FailureKind.UNKNOWN_USER_AGENT,
            url=channel.endpoint,
        )
    if name in table:
        resolved = table[name]
    elif "/" in name or " " in name:
        resolved = name  # 已经是完整 UA 字符串
    else:
        raise UserAgentError(
            f"channel.id={channel.id!r} 的 user_agent={name!r} 不是已知 UA 选项名，"
            f"也不是完整 UA 字符串（已知选项：{sorted(table)}）；"
            "配置写错不得静默退回默认 UA",
            kind=FailureKind.UNKNOWN_USER_AGENT,
            url=channel.endpoint,
        )

    if resolved.startswith(BROWSER_UA_PREFIX):
        justification = (channel.user_agent_justification or "").strip()
        if not justification:
            raise UserAgentError(
                f"channel.id={channel.id!r} 解析出的 UA 属于浏览器伪装（{resolved[:32]}…），"
                "且没有 user_agent_justification；执行器不绕过 SPEC §7.1 的禁令",
                kind=FailureKind.USER_AGENT_POLICY,
                url=channel.endpoint,
            )
    return resolved


# --- 默认实现（stdlib） -------------------------------------------------------


class UrllibFetcher:
    """基于 `urllib.request` 的默认 fetcher（零新增依赖）。

    `opener` 可注入（测试用假 opener 验证状态码/异常映射，仍然不打网络），
    默认 `urllib.request.build_opener()` 支持环境变量里的代理设置。
    """

    def __init__(
        self,
        *,
        opener: Optional[object] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._clock = clock

    def __call__(self, request: FetchRequest) -> FetchResult:
        http_request = urllib.request.Request(
            request.url,
            headers={
                "User-Agent": request.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
            method=request.method,
        )
        started = self._clock()
        try:
            response = self._opener.open(http_request, timeout=request.timeout_seconds)  # type: ignore[attr-defined]
            try:
                content = response.read()
                headers = {k.lower(): v for k, v in dict(response.headers).items()}
                final_url = response.geturl()
                status = int(response.status)
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            raise FetchHTTPError(
                f"HTTP {exc.code} {exc.reason}", url=request.url, status_code=exc.code
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise FetchTimeout(
                f"请求超时（{request.timeout_seconds}s）：{exc}", url=request.url
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise FetchTimeout(f"请求超时：{reason}", url=request.url) from exc
            raise FetchConnectionError(
                f"连接失败：{reason}", url=request.url
            ) from exc
        except http.client.HTTPException as exc:
            raise FetchTransportError(
                f"HTTP 协议层异常：{type(exc).__name__}: {exc}", url=request.url
            ) from exc

        elapsed = self._clock() - started
        return FetchResult(
            url=final_url,
            status_code=status,
            content=content,
            headers=headers,
            elapsed_seconds=elapsed,
        )
