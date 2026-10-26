"""批量采集执行器（SPEC §2.9 / §2.4 / T-102）。

把"按渠道抓取"做成**幂等、可重试、限速、合规**的一步：

```
Channel ──> robots 检查 ──> 限速 ──> fetch（有界重试） ──> RawRecord（+ 原始字节）
                  │                        │
                  └── 拒绝：结构化失败 ─────└── 失败：结构化失败（不留半成品）
```

几条硬性不变量（都有测试兜住）：

1. **幂等**：`RawRecord` 由 `atlas.contracts.states.RawRecord.create` 构造，
   `raw_id = raw_id_for(channel_id, endpoint, content_sha256)`。
   执行器**不自己造 ID 规则**，也不做任何"再哈希"；内容相同 → 同一条 Raw。
2. **不留半成品**：只有在**完整**拿到响应体之后才构造 `RawRecord`；
   失败路径上 `raw` 与 `content` 必须都是 `None`（`CollectionResult` 在构造时强制）。
   重试耗尽后**向上报告失败**（`ResultStatus.FAILED`），不静默跳过。
3. **合规**：robots 检查在 fetch 之前，且 `robots` 是**必填参数**——
   本模块刻意不提供"默认放行"的实现，避免任何调用路径绕过检查。
   UA 由 `resolve_user_agent` 解析，解析失败即失败（不退回默认 UA）。
4. **限速**：每个**真实请求**（含重试）之前都重新申请许可，等待时长进结果；
   等待超预算 → `RATE_LIMIT_WAIT_TIMEOUT` 失败，而不是抢跑或静默跳过。

输出**不落盘**：本模块只产出 `RawRecord` + 原始字节，归档由 T-103 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

from atlas.collect.fetch import (
    DEFAULT_TIMEOUT_SECONDS,
    FailureKind,
    Fetcher,
    FetchError,
    FetchOutcome,
    FetchRequest,
    fetch_once,
    resolve_user_agent,
)
from atlas.collect.robots import RobotsCache, RobotsDecision
from atlas.collect.throttle import (
    DEFAULT_CLOCK,
    DEFAULT_SLEEPER,
    DomainThrottle,
    ThrottleError,
    ThrottleTimeout,
    domain_of,
)
from atlas.contracts import InvalidContractStateError, RawRecord, raw_id_for

if TYPE_CHECKING:  # 只依赖 atlas.registry.schema 的**类型**，不依赖其实现模块
    from atlas.registry.schema import Channel

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_SECONDS",
    "RETRYABLE_STATUS_CODES",
    "ResultStatus",
    "RetryPolicy",
    "CollectionFailure",
    "CollectionResult",
    "Collector",
    "collect_one",
    "collect_channels",
]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0

#: 可重试的状态码：限流与瞬时服务端故障。其余 4xx 是配置/权限问题，重试无意义。
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 可重试的失败类别（不含 robots 拒绝、UA 非法、空响应体等确定性失败）。
RETRYABLE_KINDS = frozenset(
    {FailureKind.TIMEOUT, FailureKind.CONNECTION, FailureKind.TRANSPORT}
)

#: 非法的域名（无法限速 key）等输入错误用这个前缀标记。
_SCHEMES = frozenset({"http", "https"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ResultStatus(str, Enum):
    """每个渠道一条结果的三种去向。"""

    COLLECTED = "collected"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RetryPolicy:
    """有界重试策略：次数有上限，退避可注入（测试不睡真实时间）。"""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    retryable_status_codes: frozenset = RETRYABLE_STATUS_CODES
    retryable_kinds: frozenset = RETRYABLE_KINDS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvalidContractStateError(
                f"RetryPolicy.max_attempts 必须 ≥ 1，收到 {self.max_attempts}"
            )
        if self.backoff_seconds < 0:
            raise InvalidContractStateError(
                f"RetryPolicy.backoff_seconds 不得为负：{self.backoff_seconds}"
            )
        if self.backoff_multiplier < 1:
            raise InvalidContractStateError(
                f"RetryPolicy.backoff_multiplier 必须 ≥ 1：{self.backoff_multiplier}"
            )

    def is_retryable(self, failure: "CollectionFailure") -> bool:
        if failure.kind is FailureKind.HTTP_STATUS:
            return failure.status_code in self.retryable_status_codes
        return failure.kind in self.retryable_kinds

    def delay_for(self, attempt: int) -> float:
        """第 `attempt` 次尝试失败后的退避秒数（attempt 从 1 开始）。"""
        if attempt < 1:
            raise InvalidContractStateError(f"attempt 必须 ≥ 1，收到 {attempt}")
        return self.backoff_seconds * (self.backoff_multiplier ** (attempt - 1))


@dataclass(frozen=True)
class CollectionFailure:
    """结构化失败：渠道 id + 原因 + 类型（三者缺一不可）。"""

    channel_id: str
    endpoint: str
    kind: FailureKind
    reason: str
    attempts: int
    status_code: Optional[int] = None
    robots_url: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> dict:
        payload = {
            "channel_id": self.channel_id,
            "endpoint": self.endpoint,
            "kind": self.kind.value,
            "reason": self.reason,
            "attempts": self.attempts,
        }
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.robots_url is not None:
            payload["robots_url"] = self.robots_url
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class CollectionResult:
    """一个渠道的采集结果：成功（RawRecord + 原始字节）或结构化失败，二者互斥。"""

    channel_id: str
    endpoint: str
    status: ResultStatus
    raw: Optional[RawRecord] = None
    content: Optional[bytes] = None
    failure: Optional[CollectionFailure] = None
    attempts: int = 0
    waited_seconds: float = 0.0
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status is ResultStatus.COLLECTED:
            if self.raw is None or self.content is None:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 标记为 COLLECTED 但缺少 raw/content"
                    "（不允许半成品）"
                )
            if self.failure is not None:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 同时是成功与失败（不允许半成品）"
                )
        elif self.status is ResultStatus.FAILED:
            if self.failure is None:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 标记为 FAILED 但没有失败原因"
                )
            if self.raw is not None or self.content is not None:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 失败却留下 raw/content（不允许半成品）"
                )
        elif self.status is ResultStatus.SKIPPED:
            if self.failure is not None or self.raw is not None or self.content is not None:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 被跳过却携带结果或失败"
                )
            if not self.note:
                raise InvalidContractStateError(
                    f"channel={self.channel_id!r} 被跳过但没有说明原因（不静默跳过）"
                )
        else:  # pragma: no cover - Enum 已闭合
            raise InvalidContractStateError(f"未知 ResultStatus：{self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.COLLECTED

    def unwrap(self) -> "tuple[RawRecord, bytes]":
        if self.status is not ResultStatus.COLLECTED:
            assert self.failure is not None or self.note is not None
            raise InvalidContractStateError(
                f"channel={self.channel_id!r} 没有采集结果：status={self.status.value}"
            )
        assert self.raw is not None and self.content is not None
        return self.raw, self.content


class Collector:
    """按渠道执行的采集器：所有外部依赖（fetcher / robots / 限速 / 时钟）都注入。"""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        robots: RobotsCache,
        throttle: DomainThrottle,
        retry: Optional[RetryPolicy] = None,
        user_agent_options: Optional[Mapping[str, str]] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] = DEFAULT_SLEEPER,
        clock: Callable[[], float] = DEFAULT_CLOCK,
        wall_clock: Callable[[], datetime] = _utcnow,
        include_disabled: bool = False,
    ) -> None:
        self._fetcher = fetcher
        self._robots = robots
        self._throttle = throttle
        self.retry = retry or RetryPolicy()
        self._user_agent_options = user_agent_options
        self.timeout_seconds = timeout_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._wall_clock = wall_clock
        self.include_disabled = include_disabled

    # --- 单渠道 -----------------------------------------------------------

    def collect(self, channel: "Channel") -> CollectionResult:
        """抓取一个渠道。**不抛异常**：任何失败都变成结构化失败结果。"""
        endpoint = channel.endpoint

        if not channel.enabled and not self.include_disabled:
            return CollectionResult(
                channel_id=channel.id,
                endpoint=endpoint,
                status=ResultStatus.SKIPPED,
                note="channel.enabled=False（SPEC §2.9：停用后不再采集）",
            )

        if not _is_http_url(endpoint):
            return self._failed(
                channel,
                kind=FailureKind.INVALID_URL,
                reason=f"endpoint 不是合法的 http(s) 地址：{endpoint!r}",
                attempts=0,
            )

        # 1) UA：解析失败即失败，不退回默认 UA。
        try:
            user_agent = resolve_user_agent(channel, options=self._user_agent_options)
        except FetchError as exc:
            return self._failed(
                channel,
                kind=exc.kind,
                reason=str(exc),
                attempts=0,
                detail=type(exc).__name__,
            )

        # 2) robots：抓取之前检查，拒绝即跳过（绝不"先抓再说"）。
        decision = self._robots.check(endpoint, user_agent=user_agent)
        if not decision.allowed:
            return self._failed(
                channel,
                kind=FailureKind.ROBOTS_DENIED,
                reason=f"{decision.outcome.value}：{decision.reason}",
                attempts=0,
                robots_url=decision.robots_url,
                detail=decision.outcome.value,
            )

        # 3) 限速 + 有界重试。
        try:
            domain = domain_of(endpoint)
        except ThrottleError as exc:
            return self._failed(
                channel,
                kind=FailureKind.INVALID_URL,
                reason=f"无法为 endpoint 计算同域限速 key：{exc}",
                attempts=0,
                detail=type(exc).__name__,
            )
        min_interval = self._throttle.min_interval_for(channel)

        return self._attempt_loop(
            channel,
            user_agent=user_agent,
            domain=domain,
            min_interval=min_interval,
            decision=decision,
        )

    # --- 批量 -------------------------------------------------------------

    def collect_all(self, channels: Iterable["Channel"]) -> "List[CollectionResult]":
        """按输入顺序逐渠道采集；一个渠道失败不影响其它渠道。"""
        return [self.collect(channel) for channel in channels]

    # --- 内部 -------------------------------------------------------------

    def _attempt_loop(
        self,
        channel: "Channel",
        *,
        user_agent: str,
        domain: str,
        min_interval: float,
        decision: RobotsDecision,
    ) -> CollectionResult:
        waited = 0.0
        last_failure: Optional[CollectionFailure] = None

        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                throttle_decision = self._throttle.reserve(domain, min_interval)
            except ThrottleTimeout as exc:
                return self._failed(
                    channel,
                    kind=FailureKind.RATE_LIMIT_WAIT_TIMEOUT,
                    reason=(
                        f"限速等待超预算，未发起请求：{exc}"
                        f"（robots={decision.outcome.value}）"
                    ),
                    attempts=attempt - 1,
                    waited_seconds=waited,
                    detail=type(exc).__name__,
                )
            waited += throttle_decision.waited_seconds

            outcome = fetch_once(
                self._fetcher,
                FetchRequest(
                    url=channel.endpoint,
                    user_agent=user_agent,
                    timeout_seconds=self.timeout_seconds,
                ),
            )

            if outcome.ok:
                return self._collected(channel, outcome, attempts=attempt, waited=waited)

            last_failure = _collection_failure(channel, outcome, attempts=attempt)
            if attempt < self.retry.max_attempts and self.retry.is_retryable(last_failure):
                self._sleeper(self.retry.delay_for(attempt))
                continue
            break

        assert last_failure is not None  # max_attempts >= 1 保证至少跑过一次
        return CollectionResult(
            channel_id=channel.id,
            endpoint=channel.endpoint,
            status=ResultStatus.FAILED,
            failure=CollectionFailure(
                channel_id=last_failure.channel_id,
                endpoint=last_failure.endpoint,
                kind=last_failure.kind,
                reason=last_failure.reason,
                attempts=last_failure.attempts,
                status_code=last_failure.status_code,
                detail=last_failure.detail,
            ),
            attempts=last_failure.attempts,
            waited_seconds=waited,
            note=f"重试耗尽（max_attempts={self.retry.max_attempts}）",
        )

    def _collected(
        self, channel: "Channel", outcome: FetchOutcome, *, attempts: int, waited: float
    ) -> CollectionResult:
        result = outcome.result
        assert result is not None
        content = result.content
        fetched_at = self._wall_clock()
        # 幂等：raw_id 由 contracts 派生，执行器不另造规则。
        raw = RawRecord.create(
            channel_id=channel.id,
            endpoint=channel.endpoint,
            content=content,
            fetched_at=fetched_at,
            http_status=result.status_code,
        )
        expected = raw_id_for(channel.id, channel.endpoint, raw.content_sha256)
        if raw.raw_id != expected:
            raise InvalidContractStateError(
                f"raw_id 派生不一致：{raw.raw_id!r} != {expected!r}"
                "（标识必须来自 atlas.contracts.raw_id_for）"
            )
        return CollectionResult(
            channel_id=channel.id,
            endpoint=channel.endpoint,
            status=ResultStatus.COLLECTED,
            raw=raw,
            content=content,
            attempts=attempts,
            waited_seconds=waited,
        )

    def _failed(
        self,
        channel: "Channel",
        *,
        kind: FailureKind,
        reason: str,
        attempts: int,
        waited_seconds: float = 0.0,
        status_code: Optional[int] = None,
        robots_url: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> CollectionResult:
        return CollectionResult(
            channel_id=channel.id,
            endpoint=channel.endpoint,
            status=ResultStatus.FAILED,
            failure=CollectionFailure(
                channel_id=channel.id,
                endpoint=channel.endpoint,
                kind=kind,
                reason=reason,
                attempts=attempts,
                status_code=status_code,
                robots_url=robots_url,
                detail=detail,
            ),
            attempts=attempts,
            waited_seconds=waited_seconds,
        )


def _is_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme.lower() in _SCHEMES and bool(parsed.hostname)


def _collection_failure(
    channel: "Channel", outcome: FetchOutcome, *, attempts: int
) -> CollectionFailure:
    assert outcome.failure is not None
    failure = outcome.failure
    detail = failure.exception_type
    if failure.kind is FailureKind.HTTP_STATUS and failure.status_code is not None:
        detail = f"HTTP {failure.status_code}"
    return CollectionFailure(
        channel_id=channel.id,
        endpoint=channel.endpoint,
        kind=failure.kind,
        reason=failure.message,
        attempts=attempts,
        status_code=failure.status_code,
        detail=detail,
    )


def collect_one(
    channel: "Channel",
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    retry: Optional[RetryPolicy] = None,
    **kwargs: object,
) -> CollectionResult:
    """单渠道采集的便捷入口（等价于建一个 `Collector` 再 `collect`）。

    `robots` 是必填的：合规检查不可省略，本模块不提供"默认放行"的缺省值。
    """
    collector = Collector(
        fetcher=fetcher, robots=robots, throttle=throttle, retry=retry, **kwargs  # type: ignore[arg-type]
    )
    return collector.collect(channel)


def collect_channels(
    channels: Iterable["Channel"],
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    retry: Optional[RetryPolicy] = None,
    **kwargs: object,
) -> "List[CollectionResult]":
    """批量采集入口：每个渠道一条结果，失败向上报告，不静默跳过。"""
    collector = Collector(
        fetcher=fetcher, robots=robots, throttle=throttle, retry=retry, **kwargs  # type: ignore[arg-type]
    )
    return collector.collect_all(channels)
