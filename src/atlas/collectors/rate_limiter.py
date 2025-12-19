"""
Atlas 频率控制机制

提供多种频率限制策略，支持全局、域名、IP等多种粒度的控制。
"""

import time
import asyncio
import threading
from typing import Dict, Optional, Callable, Any
from collections import deque, defaultdict
from dataclasses import dataclass
from enum import Enum

from ..core.logging import get_logger


class RateLimitStrategy(Enum):
    """频率限制策略"""
    FIXED_WINDOW = "fixed_window"      # 固定窗口
    SLIDING_WINDOW = "sliding_window"  # 滑动窗口
    TOKEN_BUCKET = "token_bucket"      # 令牌桶
    LEAKY_BUCKET = "leaky_bucket"      # 漏桶


@dataclass
class RateLimitConfig:
    """频率限制配置"""
    requests_per_second: float = 1.0
    burst_size: int = 5
    strategy: RateLimitStrategy = RateLimitStrategy.SLIDING_WINDOW
    max_queue_size: int = 100
    timeout: float = 30.0


class FixedWindowLimiter:
    """固定窗口频率限制器"""

    def __init__(self, requests_per_second: float, window_size: float = 1.0):
        self.requests_per_second = requests_per_second
        self.window_size = window_size
        self.max_requests = int(requests_per_second * window_size)
        self.current_requests = 0
        self.window_start = time.time()
        self.lock = threading.Lock()
        self.logger = get_logger()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        with self.lock:
            current_time = time.time()

            # 检查是否需要重置窗口
            if current_time - self.window_start >= self.window_size:
                self.current_requests = 0
                self.window_start = current_time
                self.logger.debug("频率窗口已重置")

            # 检查是否超过限制
            if self.current_requests < self.max_requests:
                self.current_requests += 1
                return True

            return False

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        return self.acquire(timeout)


class SlidingWindowLimiter:
    """滑动窗口频率限制器"""

    def __init__(self, requests_per_second: float, window_size: float = 1.0):
        self.requests_per_second = requests_per_second
        self.window_size = window_size
        self.max_requests = int(requests_per_second * window_size)
        self.requests = deque()  # 存储 (timestamp, count)
        self.lock = threading.Lock()
        self.logger = get_logger()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        with self.lock:
            current_time = time.time()

            # 清理过期的请求记录
            while self.requests and current_time - self.requests[0][0] >= self.window_size:
                self.requests.popleft()

            # 检查是否超过限制
            if len(self.requests) < self.max_requests:
                self.requests.append((current_time, 1))
                return True

            return False

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        return self.acquire(timeout)


class TokenBucketLimiter:
    """令牌桶频率限制器"""

    def __init__(self, refill_rate: float, bucket_size: int):
        self.refill_rate = refill_rate  # 每秒添加的令牌数
        self.bucket_size = bucket_size  # 桶的最大容量
        self.tokens = bucket_size  # 当前令牌数
        self.last_refill = time.time()
        self.lock = threading.Lock()
        self.logger = get_logger()

    def _refill(self) -> None:
        """补充令牌"""
        current_time = time.time()
        time_passed = current_time - self.last_refill

        if time_passed > 0:
            tokens_to_add = time_passed * self.refill_rate
            self.tokens = min(self.bucket_size, self.tokens + tokens_to_add)
            self.last_refill = current_time

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        with self.lock:
            self._refill()

            if self.tokens >= 1:
                self.tokens -= 1
                return True

            return False

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        return self.acquire(timeout)

    def get_available_tokens(self) -> float:
        """获取当前可用令牌数"""
        with self.lock:
            self._refill()
            return self.tokens


class LeakyBucketLimiter:
    """漏桶频率限制器"""

    def __init__(self, leak_rate: float, bucket_size: int):
        self.leak_rate = leak_rate  # 每秒漏出的请求数
        self.bucket_size = bucket_size  # 桶的最大容量
        self.current_volume = 0.0  # 当前桶中的水量
        self.last_leak = time.time()
        self.lock = threading.Lock()
        self.logger = get_logger()

    def _leak(self) -> None:
        """漏水"""
        current_time = time.time()
        time_passed = current_time - self.last_leak

        if time_passed > 0:
            leaked = time_passed * self.leak_rate
            self.current_volume = max(0, self.current_volume - leaked)
            self.last_leak = current_time

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        with self.lock:
            self._leak()

            if self.current_volume < self.bucket_size:
                self.current_volume += 1
                return True

            return False

    async def acquire_async(self, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        return self.acquire(timeout)


class RateLimiter:
    """通用频率限制器"""

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self.logger = get_logger()

        # 根据策略创建具体的限制器
        if config.strategy == RateLimitStrategy.FIXED_WINDOW:
            self.limiter = FixedWindowLimiter(config.requests_per_second)
        elif config.strategy == RateLimitStrategy.SLIDING_WINDOW:
            self.limiter = SlidingWindowLimiter(config.requests_per_second)
        elif config.strategy == RateLimitStrategy.TOKEN_BUCKET:
            self.limiter = TokenBucketLimiter(config.requests_per_second, config.burst_size)
        elif config.strategy == RateLimitStrategy.LEAKY_BUCKET:
            self.limiter = LeakyBucketLimiter(config.requests_per_second, config.burst_size)
        else:
            raise ValueError(f"不支持的频率限制策略: {config.strategy}")

        self.wait_queue = deque()  # 等待队列
        self.processing = False

    def acquire(self, block: bool = True, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        if not block:
            return self.limiter.acquire(timeout)

        start_time = time.time()
        while True:
            if self.limiter.acquire():
                return True

            if timeout and (time.time() - start_time) >= timeout:
                return False

            # 计算等待时间
            wait_time = 0.1  # 默认等待 100ms
            if self.config.requests_per_second > 0:
                wait_time = 1.0 / self.config.requests_per_second

            time.sleep(min(wait_time, 1.0))  # 最多等待 1 秒

    async def acquire_async(self, block: bool = True, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        if not block:
            return await self.limiter.acquire_async(timeout)

        start_time = time.time()
        while True:
            if await self.limiter.acquire_async():
                return True

            if timeout and (time.time() - start_time) >= timeout:
                return False

            # 计算等待时间
            wait_time = 0.1  # 默认等待 100ms
            if self.config.requests_per_second > 0:
                wait_time = 1.0 / self.config.requests_per_second

            await asyncio.sleep(min(wait_time, 1.0))  # 最多等待 1 秒

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            'strategy': self.config.strategy.value,
            'requests_per_second': self.config.requests_per_second,
            'burst_size': self.config.burst_size,
        }

        if isinstance(self.limiter, TokenBucketLimiter):
            stats['available_tokens'] = self.limiter.get_available_tokens()

        return stats


class MultiDomainRateLimiter:
    """多域名频率限制器"""

    def __init__(self, default_config: RateLimitConfig):
        self.default_config = default_config
        self.domain_limiters: Dict[str, RateLimiter] = {}
        self.global_limiter = RateLimiter(default_config)
        self.logger = get_logger()

    def set_domain_config(self, domain: str, config: RateLimitConfig) -> None:
        """设置特定域名的频率限制配置"""
        self.domain_limiters[domain] = RateLimiter(config)
        self.logger.info(f"域名频率限制已设置", domain=domain,
                        requests_per_second=config.requests_per_second,
                        strategy=config.strategy.value)

    def get_limiter(self, domain: str) -> RateLimiter:
        """获取域名对应的限制器"""
        if domain not in self.domain_limiters:
            self.domain_limiters[domain] = RateLimiter(self.default_config)
            self.logger.debug(f"创建域名限制器", domain=domain)
        return self.domain_limiters[domain]

    def acquire(self, domain: str, block: bool = True, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        # 先检查全局限制
        if not self.global_limiter.acquire(block, timeout):
            return False

        # 再检查域名限制
        domain_limiter = self.get_limiter(domain)
        return domain_limiter.acquire(block, timeout)

    async def acquire_async(self, domain: str, block: bool = True, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        # 先检查全局限制
        if not await self.global_limiter.acquire_async(block, timeout):
            return False

        # 再检查域名限制
        domain_limiter = self.get_limiter(domain)
        return await domain_limiter.acquire_async(block, timeout)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            'global': self.global_limiter.get_stats(),
            'domains': {}
        }

        for domain, limiter in self.domain_limiters.items():
            stats['domains'][domain] = limiter.get_stats()

        return stats


class AdaptiveRateLimiter:
    """自适应频率限制器"""

    def __init__(self, base_config: RateLimitConfig):
        self.base_config = base_config
        self.current_config = base_config
        self.domain_limiters: Dict[str, MultiDomainRateLimiter] = {}
        self.success_history = defaultdict(list)  # 成功请求历史
        self.error_history = defaultdict(list)   # 错误请求历史
        self.adaptation_interval = 60  # 自适应调整间隔（秒）
        self.last_adaptation = time.time()
        self.logger = get_logger()

    def _adapt_config(self, domain: str) -> None:
        """自适应调整配置"""
        current_time = time.time()
        if current_time - self.last_adaptation < self.adaptation_interval:
            return

        success_count = len(self.success_history[domain])
        error_count = len(self.error_history[domain])
        total_count = success_count + error_count

        if total_count == 0:
            return

        success_rate = success_count / total_count

        # 根据成功率调整频率
        if success_rate > 0.9:  # 成功率高，可以稍微提高频率
            new_rate = self.base_config.requests_per_second * 1.2
        elif success_rate < 0.5:  # 成功率低，降低频率
            new_rate = self.base_config.requests_per_second * 0.5
        else:
            new_rate = self.base_config.requests_per_second

        # 确保频率在合理范围内
        new_rate = max(0.1, min(10.0, new_rate))

        if abs(new_rate - self.current_config.requests_per_second) > 0.1:
            self.current_config.requests_per_second = new_rate
            self.logger.info(f"自适应调整频率限制", domain=domain,
                            success_rate=success_rate, new_rate=new_rate)

        # 清理历史记录
        self.success_history[domain].clear()
        self.error_history[domain].clear()
        self.last_adaptation = current_time

    def get_limiter(self, domain: str) -> MultiDomainRateLimiter:
        """获取域名对应的限制器"""
        if domain not in self.domain_limiters:
            self.domain_limiters[domain] = MultiDomainRateLimiter(self.current_config)
            self.logger.debug(f"创建自适应限制器", domain=domain)
        return self.domain_limiters[domain]

    def acquire(self, domain: str, block: bool = True, timeout: Optional[float] = None) -> bool:
        """获取访问许可"""
        self._adapt_config(domain)
        limiter = self.get_limiter(domain)
        result = limiter.acquire(domain, block, timeout)

        # 记录结果
        if result:
            self.success_history[domain].append(time.time())
        else:
            self.error_history[domain].append(time.time())

        return result

    async def acquire_async(self, domain: str, block: bool = True, timeout: Optional[float] = None) -> bool:
        """异步获取访问许可"""
        self._adapt_config(domain)
        limiter = self.get_limiter(domain)
        result = await limiter.acquire_async(domain, block, timeout)

        # 记录结果
        if result:
            self.success_history[domain].append(time.time())
        else:
            self.error_history[domain].append(time.time())

        return result

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            'current_config': {
                'requests_per_second': self.current_config.requests_per_second,
                'strategy': self.current_config.strategy.value
            },
            'domains': {}
        }

        for domain, limiter in self.domain_limiters.items():
            success_count = len(self.success_history[domain])
            error_count = len(self.error_history[domain])
            total_count = success_count + error_count

            stats['domains'][domain] = {
                'limiter_stats': limiter.get_stats(),
                'success_count': success_count,
                'error_count': error_count,
                'success_rate': success_count / total_count if total_count > 0 else 0.0
            }

        return stats

    def reset_history(self, domain: Optional[str] = None) -> None:
        """重置历史记录"""
        if domain:
            self.success_history[domain].clear()
            self.error_history[domain].clear()
            self.logger.info(f"重置域名历史记录", domain=domain)
        else:
            self.success_history.clear()
            self.error_history.clear()
            self.logger.info("重置所有历史记录")