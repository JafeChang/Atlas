"""T-111 渠道健康探测（SPEC §2.8「目录会腐烂」/ §2.4 只增不改精神）。

目录**没有"完成"状态，只有持续保鲜**。本模块提供保鲜的探测端：

```
Channel ──robots 检查──> 同域限速 ──> 一次 HTTP 请求 ──> ChannelHealth（分类 + 状态码 + 时间）
             │                            │
             └─ 拒绝：robots_denied ──────┴─ 失败：按状态码/异常类别分类
```

三条纪律
--------

1. **复用 `atlas.collect` 的合规层**：`RobotsCache` 与 `DomainThrottle` 都是**必填**
   构造参数，本模块不提供"默认放行"的实现 —— 任何调用路径都绕不过 robots 与限速。
   抓取用 `atlas.collect.fetch.fetch_once`（同一套 UA 解析与结构化失败）。
2. **只探测、不裁决**：探测结果是**证据**（分类 + HTTP 状态 + 时间 + 原因），
   持久化与"标记"是 `atlas.catalog.store` 的事。本模块不删除、不停用任何渠道 ——
   SPEC §2.4 精神：目录层只做"隐藏/标注"，永不改变条目身份。
3. **分类必须可区分**：`ok` / `dead` / `robots_denied` / `forbidden` / `server_error` /
   `network_error` / `empty_body` 各自对应一个明确成因（SPEC §2.12：拿不到规则 ≠ 没有规则；
   2xx 空响应体 = 端点失效，不重试）。另加一个 `local_error` 表示**本地原因导致没发请求**
   （UA 选项非法、限速等待超预算）—— 它**不是**对源站的判断，不能与"源站坏了"混为一谈。

本模块不做任何 I/O 之外的持久化；不发并发请求；不重试（重试属于采集执行器，
health 要的是"现在这一刻这个端点还能不能用"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, List, Mapping, Optional, Tuple

from atlas.collect.fetch import (
    DEFAULT_TIMEOUT_SECONDS,
    FailureKind,
    Fetcher,
    FetchError,
    FetchFailure,
    FetchRequest,
    fetch_once,
    resolve_user_agent,
)
from atlas.collect.robots import RobotsCache, RobotsDecision
from atlas.collect.throttle import (
    DomainThrottle,
    ThrottleError,
    ThrottleTimeout,
    domain_of,
)
from atlas.registry.schema import Channel

__all__ = [
    "HealthStatus",
    "HEALTHY_STATUSES",
    "UNHEALTHY_STATUSES",
    "ChannelHealth",
    "HealthReport",
    "HealthProbe",
    "probe_channel",
    "probe_registry",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HealthStatus(str, Enum):
    """一次健康探测的分类。

    只有 `OK` 是健康；其余全部是"不健康"，但**原因可区分**（结构化失败日志的前提）。
    """

    #: 2xx 且响应体非空。
    OK = "ok"
    #: 端点已失效：4xx 中除 401/403 之外的状态码（含 404/410），以及其它确定的非 2xx。
    DEAD = "dead"
    #: robots.txt 不允许抓取（含"拿不到 robots 规则"的保守判定，SPEC §2.12）。
    ROBOTS_DENIED = "robots_denied"
    #: 401 / 403：站方明确拒绝访问。
    FORBIDDEN = "forbidden"
    #: 5xx：服务端错误。可能是暂时故障，与 `DEAD` 区别对待。
    SERVER_ERROR = "server_error"
    #: 超时 / 连接失败 / 传输层异常 / DNS 等：请求没能得到一个 HTTP 响应。
    NETWORK_ERROR = "network_error"
    #: 2xx 但响应体为 0 字节：按 SPEC §2.12 视为端点失效（重试无意义）。
    EMPTY_BODY = "empty_body"
    #: **本地**原因导致本次没发起请求（UA 选项非法、限速等待超预算、endpoint 非法）。
    #: 这不是对源站的判断，也不代表渠道坏了。
    LOCAL_ERROR = "local_error"


#: 健康的分类集合（目前只有一种；用集合表达，将来加"降级但可用"时有地方放）。
HEALTHY_STATUSES = frozenset({HealthStatus.OK})

#: 不健康的分类集合 = 全部减去健康集合（不硬编码枚举，避免加分类时漏改）。
UNHEALTHY_STATUSES = frozenset(HealthStatus) - HEALTHY_STATUSES


@dataclass(frozen=True)
class ChannelHealth:
    """一个渠道的一次健康探测结果（写入 `catalog_health` 的原始事实）。"""

    channel_id: str
    endpoint: str
    status: HealthStatus
    checked_at: datetime
    reason: str
    http_status: Optional[int] = None
    robots_outcome: Optional[str] = None
    robots_url: Optional[str] = None
    waited_seconds: float = 0.0
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None:
            raise ValueError(
                "ChannelHealth.checked_at 必须带时区（否则跨时区比较会静默出错）"
            )
        if not self.reason or not self.reason.strip():
            raise ValueError(
                f"channel={self.channel_id!r} 的健康结果缺少原因说明（不静默分类）"
            )

    @property
    def healthy(self) -> bool:
        return self.status in HEALTHY_STATUSES


@dataclass(frozen=True)
class HealthReport:
    """一批渠道的探测报告：结果 + 被跳过的渠道（跳过必须带原因）。"""

    results: Tuple[ChannelHealth, ...]
    skipped: Tuple[Tuple[str, str], ...]

    @property
    def healthy(self) -> Tuple[ChannelHealth, ...]:
        return tuple(r for r in self.results if r.healthy)

    @property
    def unhealthy(self) -> Tuple[ChannelHealth, ...]:
        return tuple(r for r in self.results if not r.healthy)

    def by_status(self) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        """按分类归拢渠道 id（顺序稳定，便于落日志与断言）。"""
        grouped: dict[str, List[str]] = {}
        for result in self.results:
            grouped.setdefault(result.status.value, []).append(result.channel_id)
        return tuple(
            (status, tuple(sorted(ids))) for status, ids in sorted(grouped.items())
        )

    def summary(self) -> str:
        rendered = ", ".join(
            f"{status}={len(ids)}" for status, ids in self.by_status()
        )
        tail = f"；跳过 {len(self.skipped)} 条" if self.skipped else ""
        return f"健康探测 {len(self.results)} 条（{rendered}）{tail}"


def _status_for_failure(failure: FetchFailure) -> Tuple[HealthStatus, Optional[int]]:
    """把 `atlas.collect` 的结构化失败映射成健康分类（分类理由写在注释里）。

    映射是**全覆盖**的：`FailureKind` 里没有一类会被漏掉变成"未知失败"。
    """
    code = failure.status_code
    if failure.kind is FailureKind.EMPTY_BODY:
        # SPEC §2.12：2xx + 0 字节 → 端点失效，且不重试。
        return HealthStatus.EMPTY_BODY, code
    if failure.kind is FailureKind.HTTP_STATUS:
        if code in (401, 403):
            return HealthStatus.FORBIDDEN, code
        if code is not None and 500 <= code < 600:
            return HealthStatus.SERVER_ERROR, code
        # 其余 4xx（404/410/451…）以及未预期的非 2xx：端点已不可用。
        return HealthStatus.DEAD, code
    if failure.kind is FailureKind.ROBOTS_DENIED:
        return HealthStatus.ROBOTS_DENIED, code
    if failure.kind in (
        FailureKind.TIMEOUT,
        FailureKind.CONNECTION,
        FailureKind.TRANSPORT,
    ):
        return HealthStatus.NETWORK_ERROR, code
    # INVALID_URL / UNKNOWN_USER_AGENT / USER_AGENT_POLICY / RATE_LIMIT_WAIT_TIMEOUT：
    # 这些是我们自己没发出（或不该发出）请求，不是源站的健康问题。
    return HealthStatus.LOCAL_ERROR, code


class HealthProbe:
    """渠道健康探测器：所有外部依赖（fetcher / robots / 限速 / 时钟）都注入。"""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        robots: RobotsCache,
        throttle: DomainThrottle,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent_options: Optional[Mapping[str, str]] = None,
        wall_clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._fetcher = fetcher
        self._robots = robots
        self._throttle = throttle
        self.timeout_seconds = timeout_seconds
        self._user_agent_options = user_agent_options
        self._wall_clock = wall_clock

    # --- 单渠道 -----------------------------------------------------------

    def probe(self, channel: Channel) -> ChannelHealth:
        """探测一个渠道。**不抛异常**：任何失败都变成可区分的健康结果。

        唯一的例外是接线错误（fetcher 返回了不是 `FetchResult` 的东西）——
        那会由 `fetch_once` 响亮抛出，不被这里吞掉。
        """
        endpoint = channel.endpoint

        # 1) UA：解析失败即本地错误，不发请求（不退回默认 UA，SPEC §2.12）。
        try:
            user_agent = resolve_user_agent(channel, options=self._user_agent_options)
        except FetchError as exc:
            return self._result(
                channel,
                HealthStatus.LOCAL_ERROR,
                reason=f"user_agent 无法解析，未发起请求：{exc}",
                detail=type(exc).__name__,
                http_status=exc.status_code,
            )

        # 2) robots：先合规再探测；拒绝时**一个请求都不发**。
        decision = self._robots.check(endpoint, user_agent=user_agent)
        if not decision.allowed:
            return self._from_robots(channel, decision)

        # 3) 限速：同域最小间隔（与采集同一套取值规则）。
        try:
            domain = domain_of(endpoint)
        except ThrottleError as exc:
            return self._result(
                channel,
                HealthStatus.LOCAL_ERROR,
                reason=f"无法为 endpoint 计算同域限速 key，未发起请求：{exc}",
                detail=type(exc).__name__,
            )

        min_interval = self._throttle.min_interval_for(channel)
        try:
            throttle_decision = self._throttle.reserve(domain, min_interval)
        except ThrottleTimeout as exc:
            return self._result(
                channel,
                HealthStatus.LOCAL_ERROR,
                reason=f"限速等待超预算，未发起请求：{exc}",
                detail=type(exc).__name__,
            )

        # 4) 一次请求，不重试。
        outcome = fetch_once(
            self._fetcher,
            FetchRequest(
                url=endpoint,
                user_agent=user_agent,
                timeout_seconds=self.timeout_seconds,
            ),
        )
        if outcome.ok:
            result = outcome.result
            assert result is not None
            return self._result(
                channel,
                HealthStatus.OK,
                reason=f"HTTP {result.status_code}，响应体 {len(result.content)} 字节",
                http_status=result.status_code,
                waited_seconds=throttle_decision.waited_seconds,
            )

        assert outcome.failure is not None
        failure = outcome.failure
        status, code = _status_for_failure(failure)
        return self._result(
            channel,
            status,
            reason=f"{failure.kind.value}：{failure.message}",
            http_status=code,
            detail=failure.exception_type,
            waited_seconds=throttle_decision.waited_seconds,
        )

    # --- 批量 -------------------------------------------------------------

    def probe_all(
        self, channels: Iterable[Channel], *, include_disabled: bool = False
    ) -> HealthReport:
        """逐渠道探测；停用渠道默认跳过（SPEC §2.9：停用后不再采集）。

        跳过**必须带原因**，且被跳过的渠道不会产生健康记录 —— 它没有被探测过，
        不能凭空写一条"结果"。
        """
        results: List[ChannelHealth] = []
        skipped: List[Tuple[str, str]] = []
        for channel in channels:
            if not channel.enabled and not include_disabled:
                skipped.append(
                    (channel.id, "channel.enabled=False（SPEC §2.9：停用后不再采集）")
                )
                continue
            results.append(self.probe(channel))
        return HealthReport(results=tuple(results), skipped=tuple(skipped))

    # --- 内部 -------------------------------------------------------------

    def _result(
        self,
        channel: Channel,
        status: HealthStatus,
        *,
        reason: str,
        http_status: Optional[int] = None,
        detail: Optional[str] = None,
        waited_seconds: float = 0.0,
        robots_outcome: Optional[str] = None,
        robots_url: Optional[str] = None,
    ) -> ChannelHealth:
        return ChannelHealth(
            channel_id=channel.id,
            endpoint=channel.endpoint,
            status=status,
            checked_at=self._wall_clock(),
            reason=reason,
            http_status=http_status,
            robots_outcome=robots_outcome,
            robots_url=robots_url,
            waited_seconds=waited_seconds,
            detail=detail,
        )

    def _from_robots(self, channel: Channel, decision: RobotsDecision) -> ChannelHealth:
        """robots 拒绝 / 无法确认 → `robots_denied`，并保留 robots 自己的状态码。

        注意 `http_status` 这里是 **robots.txt 的状态码**（如 403/503），不是
        endpoint 的状态码 —— 我们没请求 endpoint。区分这一点才不会把
        "站方拒绝我们看 robots" 误读成 "这个 feed 返回了 403"。
        """
        return self._result(
            channel,
            HealthStatus.ROBOTS_DENIED,
            reason=f"robots 检查未通过（{decision.outcome.value}）：{decision.reason}",
            http_status=decision.status_code,
            detail=decision.outcome.value,
            robots_outcome=decision.outcome.value,
            robots_url=decision.robots_url,
        )


def probe_channel(
    channel: Channel,
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    **kwargs: object,
) -> ChannelHealth:
    """单渠道探测的便捷入口。

    `robots` 与 `throttle` 是**必填**的：合规层不可省略（与
    `atlas.collect.collect_one` 同一纪律）。
    """
    probe = HealthProbe(fetcher=fetcher, robots=robots, throttle=throttle, **kwargs)  # type: ignore[arg-type]
    return probe.probe(channel)


def probe_registry(
    service: object,
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    include_disabled: bool = False,
    **kwargs: object,
) -> HealthReport:
    """对注册表里**当前启用**的渠道做一轮探测。

    `service` 需要提供 `list_channels()`（`atlas.registry.service.RegistryService`）；
    这里用结构化协议而不是具体类型，是为了让测试可以注入一个最小替身。
    """
    list_channels = getattr(service, "list_channels", None)
    if list_channels is None:
        raise TypeError(
            "probe_registry 需要一个提供 list_channels() 的注册表服务，"
            f"收到 {type(service).__name__}（接线错误，不掩盖）"
        )
    probe = HealthProbe(fetcher=fetcher, robots=robots, throttle=throttle, **kwargs)  # type: ignore[arg-type]
    return probe.probe_all(list_channels(), include_disabled=include_disabled)
