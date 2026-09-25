"""同域最小间隔限速（SPEC §2.9「采集策略」/ T-102）。

设计要点是**可测**：本模块不直接读时间、不直接睡。
时钟（`clock`）与休眠函数（`sleeper`）都由构造参数注入，默认值是
`time.monotonic` / `time.sleep`；测试注入"假时钟 + 会推进假时钟的假 sleeper"
即可断言"是否等待、等了多久"，而不会真的睡过真实时间。

同域最小间隔的取值规则（`DomainThrottle.min_interval_for`）：

1. `channel.rate_limit_seconds` 显式给出（含 0）→ **原样采用**。
   0 是"明确声明不做额外节流"（见 `registry.schema.MIN_RATE_LIMIT_SECONDS`），
   显式配置优先于全局保护，否则这个字段就没有表达力。
2. `rate_limit_seconds is None` → 取 `channel.interval_seconds` 与全局下限
   （`global_min_interval`，默认 1s）的**较小值**。
   `interval_seconds` 是"这个源多久轮询一次"（下限 60s），把它当单次请求间隔
   会让批量采集退化到分钟级，因此它在这里只作为**上界**，实际生效的是全局下限。

同一 domain 上多条渠道一起采集时取**最严**（最大）的间隔：同域节流是对源站的
承诺，不能被同一域内更宽松的渠道拉低。

等待超时（`max_wait_seconds`）**不是**静默跳过：超出预算即抛 `ThrottleTimeout`，
由执行器转成结构化失败（`FailureKind.RATE_LIMIT_WAIT_TIMEOUT`），
而不是悄悄少抓一条或先抓了再说。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:  # 只依赖 atlas.registry.schema 的**类型**，不依赖其实现模块
    from atlas.registry.schema import Channel

__all__ = [
    "DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS",
    "DEFAULT_SLEEPER",
    "DEFAULT_CLOCK",
    "ThrottleError",
    "ThrottleTimeout",
    "ThrottleDecision",
    "DomainThrottle",
    "domain_of",
]

#: 全局下限：未配置 `rate_limit_seconds` 时同一 domain 两条请求之间的最小间隔。
DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS = 1.0

DEFAULT_CLOCK: Callable[[], float] = time.monotonic
DEFAULT_SLEEPER: Callable[[float], None] = time.sleep

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ThrottleError(Exception):
    """限速相关错误基类。"""


class ThrottleTimeout(ThrottleError):
    """需要的等待超过预算：**响亮失败**，不静默跳过、也不违规抢跑。"""

    def __init__(self, domain: str, *, min_interval: float, needed: float, max_wait: float):
        self.domain = domain
        self.min_interval = min_interval
        self.needed = needed
        self.max_wait = max_wait
        super().__init__(
            f"domain={domain!r} 需要的限速等待 {needed:.3f}s 超过预算 {max_wait:.3f}s"
            f"（同域最小间隔 {min_interval:.3f}s）"
        )


def domain_of(url: str) -> str:
    """同域节流的 key：`host` 或 `host:port`（默认端口归一化，忽略 scheme）。

    同域的判断依据是**服务器**而不是协议：`http://a.com` 与 `https://a.com`
    是同一个 host，不该被当成两个域各放一份配额。
    """
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ThrottleError(f"无法从 URL 提取主机名：{url!r}")
    try:
        port = parsed.port
    except ValueError as exc:  # 端口非法（如 ':abc'）
        raise ThrottleError(f"URL 端口非法：{url!r}") from exc
    if port is not None and port != _DEFAULT_PORTS.get(parsed.scheme.lower()):
        return f"{host}:{port}"
    return host


@dataclass(frozen=True)
class ThrottleDecision:
    """一次限速判定：等了多久、依据什么间隔。"""

    domain: str
    min_interval: float
    waited_seconds: float


class DomainThrottle:
    """按 domain 记录"上次发出请求的时刻"，由此决定本次是否需要等待。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = DEFAULT_CLOCK,
        sleeper: Callable[[float], None] = DEFAULT_SLEEPER,
        global_min_interval: float = DEFAULT_GLOBAL_MIN_INTERVAL_SECONDS,
        max_wait_seconds: Optional[float] = None,
    ) -> None:
        if global_min_interval < 0:
            raise ThrottleError(f"global_min_interval 不得为负：{global_min_interval}")
        if max_wait_seconds is not None and max_wait_seconds < 0:
            raise ThrottleError(f"max_wait_seconds 不得为负：{max_wait_seconds}")
        self._clock = clock
        self._sleeper = sleeper
        self.global_min_interval = float(global_min_interval)
        self.max_wait_seconds = max_wait_seconds
        self._last_request_at: Dict[str, float] = {}
        self._enforced_interval: Dict[str, float] = {}
        self._request_counts: Dict[str, int] = {}
        self._waited_totals: Dict[str, float] = {}

    # --- 间隔取值 ---------------------------------------------------------

    def min_interval_for(self, channel: "Channel") -> float:
        """见模块文档的第 1 / 2 条规则。"""
        explicit = channel.rate_limit_seconds
        if explicit is not None:
            return float(explicit)
        return max(
            min(float(channel.interval_seconds), self.global_min_interval),
            self.global_min_interval,
        )

    # --- 申请 -------------------------------------------------------------

    def reserve(self, domain: str, min_interval: float) -> ThrottleDecision:
        """申请一次该 domain 的发送许可；必要时睡眠（可注入），返回等待时长。

        同域已生效的更严间隔会被继承（取最大值），因此同域多渠道不会互相放宽。
        """
        if min_interval < 0:
            raise ThrottleError(f"min_interval 不得为负：{min_interval}")
        enforced = max(float(min_interval), self._enforced_interval.get(domain, 0.0))
        self._enforced_interval[domain] = enforced

        now = self._clock()
        last = self._last_request_at.get(domain)
        waited = 0.0

        if last is not None and enforced > 0:
            needed = enforced - (now - last)
            if needed > 0:
                if self.max_wait_seconds is not None and needed > self.max_wait_seconds:
                    raise ThrottleTimeout(
                        domain,
                        min_interval=enforced,
                        needed=needed,
                        max_wait=self.max_wait_seconds,
                    )
                self._sleeper(needed)
                waited = needed
                now = self._clock()

        # 记录**真实发出时刻**：即使 sleeper 少睡了一点，下一次也会补足差额，
        # 保证"相邻两次发送时刻之差 ≥ min_interval"这个不变量成立。
        self._last_request_at[domain] = now
        self._request_counts[domain] = self._request_counts.get(domain, 0) + 1
        self._waited_totals[domain] = self._waited_totals.get(domain, 0.0) + waited
        return ThrottleDecision(domain=domain, min_interval=enforced, waited_seconds=waited)

    # --- 观测 -------------------------------------------------------------

    def stats(self) -> Dict[str, Dict[str, float]]:
        """按 domain 的请求数与累计等待秒数（结构化日志/巡检用）。"""
        return {
            domain: {
                "requests": float(self._request_counts.get(domain, 0)),
                "waited_seconds": self._waited_totals.get(domain, 0.0),
                "last_request_at": self._last_request_at.get(domain, 0.0),
                "enforced_min_interval": self._enforced_interval.get(domain, 0.0),
            }
            for domain in sorted(self._enforced_interval)
        }
