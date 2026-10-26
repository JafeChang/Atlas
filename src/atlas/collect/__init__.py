"""T-102 采集执行器：幂等、可重试、限速、合规。

对外导出（分三层，便于调用方按需组装）：

- 抓取层：`Fetcher` / `FetchRequest` / `FetchResult` / `FetchOutcome` /
  `FetchFailure` / `FailureKind` / `UrllibFetcher` / `resolve_user_agent`
- 合规与限速层：`RobotsCache` / `RobotsDecision` / `RobotsOutcome` /
  `DomainThrottle` / `ThrottleTimeout`
- 执行层：`Collector` / `collect_one` / `collect_channels` /
  `CollectionResult` / `CollectionFailure` / `ResultStatus` / `RetryPolicy`

`RawRecord` 的 `raw_id` 一律由 `atlas.contracts.raw_id_for` 派生；执行器不另造 ID 规则。
"""

from atlas.collect.executor import (
    RETRYABLE_STATUS_CODES,
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
    Fetcher,
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
from atlas.collect.robots import (
    FetcherRobotsAdapter,
    RobotsCache,
    RobotsDecision,
    RobotsFetchResult,
    RobotsOutcome,
)
from atlas.collect.throttle import DomainThrottle, ThrottleDecision, ThrottleTimeout

__all__ = [
    "DEFAULT_USER_AGENT",
    "RETRYABLE_STATUS_CODES",
    "CollectionFailure",
    "CollectionResult",
    "Collector",
    "DomainThrottle",
    "FailureKind",
    "FetchError",
    "FetchFailure",
    "FetchHTTPError",
    "FetchOutcome",
    "FetchRequest",
    "FetchResult",
    "FetchTimeout",
    "FetchTransportError",
    "Fetcher",
    "FetcherRobotsAdapter",
    "ResultStatus",
    "RetryPolicy",
    "RobotsCache",
    "RobotsDecision",
    "RobotsFetchResult",
    "RobotsOutcome",
    "ThrottleDecision",
    "ThrottleTimeout",
    "UrllibFetcher",
    "UserAgentError",
    "collect_channels",
    "collect_one",
    "fetch_once",
    "resolve_user_agent",
]
