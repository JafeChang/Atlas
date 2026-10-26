"""robots.txt 合规检查（SPEC §7.1 合规底线 / T-102）。

**保守策略（写进代码，不写在文档里）**

抓取前必须确认目标 URL 被 robots.txt 允许；无法确认时**视为不允许**：

| 情形 | 判定 | 理由 |
|---|---|---|
| 规则明确禁止该 UA 抓该路径 | 禁止 | 合规底线 |
| robots.txt 返回 401 / 403 | 禁止 | 站方明确拒绝（RFC 9309 §2.3.2.3） |
| robots.txt 返回 5xx | **禁止（保守）** | 服务端错误无法证明允许；宁可漏抓不可违规 |
| 网络异常 / 超时 / 连接失败 | **禁止（保守）** | 同上：拿不到规则 ≠ 没有规则 |
| robots.txt 解析异常 | **禁止（保守）** | 解析失败同样无法证明允许 |
| robots.txt 返回其它 4xx（含 404） | 允许 | 文件不存在 = 没有限制（RFC 9309），且这不算"抓取失败" |
| 2xx 且解析成功 | 按规则判定 | 正常路径 |

最后两行的区别是刻意的：**"服务器明确说没有 robots.txt"与"我们没能拿到 robots.txt"
不是一回事**。前者是确定的"无限制"，后者是未知，未知一律保守。
`allow_when_absent=False` 可以把最后两行也收紧为保守，代价是绝大多数没有
robots.txt 的小站点都抓不到——默认不这么做。

缓存：按 **origin**（scheme + host + 非默认端口）缓存解析结果，含失败（负缓存），
避免同一批采集里反复请求 robots.txt，也避免对同一失败 host 反复重试。
缓存生命期默认 = `RobotsCache` 实例生命期（一次采集运行），可通过
`cache_ttl_seconds` 设定。

本模块不做任何网络 I/O：`robots_fetcher` 是注入的可调用对象
（真实实现见 `FetcherRobotsAdapter`，测试注入假 fetcher）。
"""

from __future__ import annotations

import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

from atlas.collect.fetch import (
    DEFAULT_USER_AGENT,
    Fetcher,
    FetchError,
    FetchHTTPError,
    FetchRequest,
)

__all__ = [
    "RobotsOutcome",
    "RobotsDecision",
    "RobotsFetchResult",
    "RobotsFetcher",
    "RobotsCache",
    "FetcherRobotsAdapter",
]

_DEFAULT_PORTS = {"http": 80, "https": 443}
_ABSENT_STATUS_CODES = frozenset({400, 404, 410, 451})
_FORBIDDEN_STATUS_CODES = frozenset({401, 403})


class RobotsOutcome(str, Enum):
    """robots 判定的结构化结果（失败原因必须可区分，不允许只报"失败"）。"""

    ALLOWED = "allowed"
    ALLOWED_NO_ROBOTS = "allowed_no_robots"
    DISALLOWED_BY_RULES = "disallowed_by_rules"
    DISALLOWED_ROBOTS_FORBIDDEN = "disallowed_robots_forbidden"
    DISALLOWED_ROBOTS_UNAVAILABLE = "disallowed_robots_unavailable"
    DISALLOWED_INVALID_URL = "disallowed_invalid_url"


@dataclass(frozen=True)
class RobotsFetchResult:
    """robots.txt 的原始获取结果（由注入的 fetcher 产出）。"""

    url: str
    status_code: Optional[int] = None
    body: bytes = b""
    error: Optional[str] = None


#: robots.txt 获取器：`(robots_url, user_agent=...) -> RobotsFetchResult`。
RobotsFetcher = Callable[..., RobotsFetchResult]


@dataclass(frozen=True)
class RobotsDecision:
    """一次 robots 判定：允许与否 + 明确原因 + 依据的 robots.txt 地址。"""

    url: str
    origin: str
    robots_url: str
    user_agent: str
    allowed: bool
    outcome: RobotsOutcome
    reason: str
    status_code: Optional[int] = None
    from_cache: bool = False


@dataclass
class _CacheEntry:
    robots_url: str
    fetched_at: float
    parser: Optional[robotparser.RobotFileParser] = None
    status_code: Optional[int] = None
    blocked_outcome: Optional[RobotsOutcome] = None
    blocked_reason: Optional[str] = None
    absent: bool = False
    checks: int = 0


class RobotsCache:
    """按 origin 缓存的 robots.txt 判定器（保守策略见模块文档）。"""

    def __init__(
        self,
        robots_fetcher: RobotsFetcher,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        cache_ttl_seconds: Optional[float] = None,
        allow_when_absent: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError("RobotsCache 需要一个非空 user_agent（robots 规则按 UA 匹配）")
        if cache_ttl_seconds is not None and cache_ttl_seconds <= 0:
            raise ValueError(f"cache_ttl_seconds 必须为正数或 None：{cache_ttl_seconds}")
        self._robots_fetcher = robots_fetcher
        self.user_agent = user_agent
        self.cache_ttl_seconds = cache_ttl_seconds
        self.allow_when_absent = allow_when_absent
        self._clock = clock
        self._cache: Dict[str, _CacheEntry] = {}

    # --- 便捷构造 ---------------------------------------------------------

    @classmethod
    def from_fetcher(
        cls,
        fetcher: Fetcher,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 30.0,
        **kwargs: object,
    ) -> "RobotsCache":
        """用同一个内容 fetcher 抓 robots.txt（真实运行路径的便利入口）。"""
        return cls(
            FetcherRobotsAdapter(fetcher, timeout_seconds=timeout_seconds),
            user_agent=user_agent,
            **kwargs,  # type: ignore[arg-type]
        )

    # --- 对外接口 ---------------------------------------------------------

    def check(self, url: str, *, user_agent: Optional[str] = None) -> RobotsDecision:
        """判定 `url` 是否允许被 `user_agent` 抓取。**不抛异常**：无法确认即不允许。"""
        agent = (user_agent or self.user_agent).strip()
        if not agent:
            raise ValueError("check() 的 user_agent 不得为空")

        origin, robots_url = self._origin_and_robots_url(url)
        if origin is None:
            return RobotsDecision(
                url=url,
                origin="",
                robots_url="",
                user_agent=agent,
                allowed=False,
                outcome=RobotsOutcome.DISALLOWED_INVALID_URL,
                reason=f"URL 不是合法的 http(s) 绝对地址，无法做 robots 检查：{url!r}",
            )

        entry, from_cache = self._entry_for(origin, robots_url, agent)
        entry.checks += 1

        if entry.blocked_outcome is not None:
            return RobotsDecision(
                url=url,
                origin=origin,
                robots_url=entry.robots_url,
                user_agent=agent,
                allowed=False,
                outcome=entry.blocked_outcome,
                reason=entry.blocked_reason or "robots 检查未通过",
                status_code=entry.status_code,
                from_cache=from_cache,
            )

        parser = entry.parser
        assert parser is not None  # blocked_outcome 与 parser 互斥
        try:
            allowed = bool(parser.can_fetch(agent, url))
        except Exception as exc:  # noqa: BLE001 - 保守：解析器异常 = 无法证明允许
            return RobotsDecision(
                url=url,
                origin=origin,
                robots_url=entry.robots_url,
                user_agent=agent,
                allowed=False,
                outcome=RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE,
                reason=(
                    "robots 规则匹配时解析器抛错，按保守策略视为不允许："
                    f"{type(exc).__name__}: {exc}"
                ),
                status_code=entry.status_code,
                from_cache=from_cache,
            )

        if allowed:
            outcome = (
                RobotsOutcome.ALLOWED_NO_ROBOTS if entry.absent else RobotsOutcome.ALLOWED
            )
            reason = (
                f"robots.txt 不存在（HTTP {entry.status_code}），视为无限制"
                if entry.absent
                else f"robots.txt 未禁止 {agent!r} 抓取该路径"
            )
            return RobotsDecision(
                url=url,
                origin=origin,
                robots_url=entry.robots_url,
                user_agent=agent,
                allowed=True,
                outcome=outcome,
                reason=reason,
                status_code=entry.status_code,
                from_cache=from_cache,
            )

        return RobotsDecision(
            url=url,
            origin=origin,
            robots_url=entry.robots_url,
            user_agent=agent,
            allowed=False,
            outcome=RobotsOutcome.DISALLOWED_BY_RULES,
            reason=f"robots.txt 明确禁止 {agent!r} 抓取 {url}",
            status_code=entry.status_code,
            from_cache=from_cache,
        )

    def invalidate(self, url: Optional[str] = None) -> None:
        """清理缓存；给 URL 时只清理该 URL 所属 origin。"""
        if url is None:
            self._cache.clear()
            return
        origin, _ = self._origin_and_robots_url(url)
        if origin is not None:
            self._cache.pop(origin, None)

    def cached_origins(self) -> Tuple[str, ...]:
        return tuple(sorted(self._cache))

    # --- 内部 -------------------------------------------------------------

    @staticmethod
    def _origin_and_robots_url(url: str) -> Tuple[Optional[str], str]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme not in _DEFAULT_PORTS or not host:
            return None, ""
        try:
            port = parsed.port
        except ValueError:
            return None, ""
        netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
        netloc_with_port = f"{host}:{port}" if port is not None else host
        origin = f"{scheme}://{netloc}"
        return origin, f"{scheme}://{netloc_with_port}/robots.txt"

    def _entry_for(self, origin: str, robots_url: str, agent: str) -> Tuple[_CacheEntry, bool]:
        now = self._clock()
        entry = self._cache.get(origin)
        if entry is not None and not self._expired(entry, now):
            return entry, True

        entry = self._load(origin, robots_url, agent)
        entry.fetched_at = now
        self._cache[origin] = entry
        return entry, False

    def _expired(self, entry: _CacheEntry, now: float) -> bool:
        if self.cache_ttl_seconds is None:
            return False
        return (now - entry.fetched_at) > self.cache_ttl_seconds

    def _load(self, origin: str, robots_url: str, agent: str) -> _CacheEntry:
        entry = _CacheEntry(robots_url=robots_url, fetched_at=0.0)

        try:
            result = self._robots_fetcher(robots_url, user_agent=agent)
        except Exception as exc:  # noqa: BLE001 - 保守策略：抓取失败 = 不允许（原因入 reason）
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
            entry.blocked_reason = (
                f"抓取 robots.txt 失败，按保守策略视为不允许：{type(exc).__name__}: {exc}"
            )
            return entry

        if not isinstance(result, RobotsFetchResult):
            raise TypeError(
                "robots_fetcher 必须返回 RobotsFetchResult，"
                f"收到 {type(result).__name__}（接线错误，不掩盖）"
            )

        entry.status_code = result.status_code
        entry.robots_url = result.url or robots_url

        # **先判状态码，再判 error**：只要"HTTP 有应答"，应答本身就是规则依据
        # （404 = 站方明确说没有 robots.txt）。只有"没有状态码"才退回 error 分支。
        # 顺序反了会让 404 落到"拿不到规则 → 保守拒绝"，与 SPEC §2.12 表格矛盾。
        status = result.status_code
        if status is None:
            if result.error:
                entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
                entry.blocked_reason = (
                    f"抓取 robots.txt 失败，按保守策略视为不允许：{result.error}"
                )
            else:
                entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
                entry.blocked_reason = (
                    "robots.txt 响应既无状态码也无错误信息，视为不允许（保守）"
                )
            return entry
        if status in _FORBIDDEN_STATUS_CODES:
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_FORBIDDEN
            entry.blocked_reason = (
                f"robots.txt 返回 {status}（站方明确拒绝访问），视为不允许"
            )
            return entry
        if 500 <= status < 600:
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
            entry.blocked_reason = (
                f"robots.txt 返回 {status}（服务端错误，无法证明允许），按保守策略视为不允许"
            )
            return entry
        if 400 <= status < 500:
            if self.allow_when_absent:
                entry.parser = _allow_all_parser(entry.robots_url)
                entry.absent = True
                entry.blocked_reason = None
                return entry
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
            entry.blocked_reason = (
                f"robots.txt 返回 {status}（文件不存在），但 allow_when_absent=False，"
                "按保守策略视为不允许"
            )
            return entry
        if not (200 <= status < 300):
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
            entry.blocked_reason = (
                f"robots.txt 返回未预期的状态码 {status}，按保守策略视为不允许"
            )
            return entry

        text = result.body.decode("utf-8", errors="replace")
        parser = robotparser.RobotFileParser()
        parser.set_url(entry.robots_url)
        try:
            parser.parse(text.splitlines(keepends=True))
        except Exception as exc:  # noqa: BLE001 - 保守策略：解析失败 = 无法证明允许
            entry.parser = None
            entry.blocked_outcome = RobotsOutcome.DISALLOWED_ROBOTS_UNAVAILABLE
            entry.blocked_reason = (
                f"robots.txt 解析失败，按保守策略视为不允许：{type(exc).__name__}: {exc}"
            )
            return entry

        entry.parser = parser
        return entry


def _allow_all_parser(robots_url: str) -> robotparser.RobotFileParser:
    """构造一个"无限制"的解析器（文件不存在的确定情形）。"""
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    parser.parse([])
    return parser


@dataclass
class FetcherRobotsAdapter:
    """把内容 `Fetcher` 当成 robots 获取器用（真实运行的便利桥接）。

    归类规则（必须保住"HTTP 有应答 ≠ 拿不到规则"这条区分）：

    - fetcher 成功返回 → 状态码与响应体原样传下去；
    - fetcher 抛 `FetchHTTPError`（**HTTP 有应答**）→ 传下 `status_code`，
      `error=None`：`UrllibFetcher` 对所有非 2xx 都抛这个异常，若在这里丢掉状态码，
      404（没有 robots.txt → 允许）会被误判成"拿不到规则"（→ 保守拒绝）；
    - 其它 `FetchError`（超时 / 连接 / 传输）→ 确实没拿到规则，记 `error`（含类型名），
      由 `RobotsCache` 按保守策略拒绝。
    """

    fetcher: Fetcher
    timeout_seconds: float = 30.0
    calls: list = field(default_factory=list)

    def __call__(self, robots_url: str, *, user_agent: str) -> RobotsFetchResult:
        self.calls.append(robots_url)
        request = FetchRequest(
            url=robots_url, user_agent=user_agent, timeout_seconds=self.timeout_seconds
        )
        try:
            result = self.fetcher(request)
        except FetchHTTPError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code is not None:
                return RobotsFetchResult(
                    url=robots_url, status_code=status_code, body=b"", error=None
                )
            return RobotsFetchResult(
                url=robots_url,
                status_code=None,
                body=b"",
                error=f"{type(exc).__name__}: {exc}",
            )
        except FetchError as exc:
            return RobotsFetchResult(
                url=robots_url, status_code=None, body=b"", error=f"{type(exc).__name__}: {exc}"
            )
        return RobotsFetchResult(
            url=robots_url, status_code=result.status_code, body=result.content, error=None
        )
