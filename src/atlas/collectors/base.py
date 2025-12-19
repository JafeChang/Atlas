"""
Atlas 数据采集器基础类

提供数据采集的基础功能和通用接口。
"""

import time
import random
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union
from pathlib import Path
from urllib.parse import urlparse
import requests
import feedparser
from bs4 import BeautifulSoup

from ..core.config import CollectionConfig
from ..core.logging import get_logger
from .http_client import HTTPClient, RequestConfig, Response
from .rate_limiter import RateLimiter, RateLimitConfig, RateLimitStrategy, AdaptiveRateLimiter


class BaseCollector(ABC):
    """数据采集器基础类"""

    def __init__(self, config: CollectionConfig, use_rate_limiter: bool = True):
        """初始化采集器

        Args:
            config: 采集配置
            use_rate_limiter: 是否使用频率限制
        """
        self.config = config
        self.logger = get_logger()
        self.use_rate_limiter = use_rate_limiter

        # 设置 HTTP 客户端
        request_config = RequestConfig(
            timeout=config.request_timeout,
            max_retries=3,
            use_cache=True,
            cache_ttl=3600,
            verify_ssl=True
        )
        self.http_client = HTTPClient(config, request_config)

        # 设置频率限制器
        if use_rate_limiter:
            rate_limit_config = RateLimitConfig(
                requests_per_second=1.0 / config.rate_limit_delay,
                strategy=RateLimitStrategy.SLIDING_WINDOW,
                burst_size=5
            )
            self.rate_limiter = AdaptiveRateLimiter(rate_limit_config)
        else:
            self.rate_limiter = None

        # 统计信息
        self.stats = {
            'total_collections': 0,
            'successful_collections': 0,
            'failed_collections': 0,
            'total_items': 0,
            'start_time': time.time()
        }

        # 兼容性：保留旧版 session 接口
        self.session = self.http_client.session if self.http_client.session else requests.Session()
        self._setup_session()

    def _setup_session(self) -> None:
        """设置 HTTP 会话"""
        # 设置默认请求头
        self.session.headers.update({
            'User-Agent': self.config.default_user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Cache-Control': 'max-age=0',
        })

        # 设置超时和重试
        self.session.timeout = self.config.request_timeout

        # 如果配置了随机 User-Agent，则随机选择一个
        if self.config.use_random_user_agent:
            self.set_random_user_agent()

    def set_user_agent(self, user_agent: str) -> None:
        """设置 User-Agent

        Args:
            user_agent: User-Agent 字符串
        """
        self.session.headers['User-Agent'] = user_agent
        self.logger.debug("User-Agent 已更新", user_agent=user_agent)

    def get_random_user_agent(self) -> str:
        """获取随机 User-Agent

        Returns:
            随机选择的 User-Agent
        """
        user_agents = [
            # Modern browsers
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",

            # Safari
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",

            # Mobile browsers
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",

            # Atlas custom
            "Atlas/0.1.0 (Information Aggregation System; +https://github.com/your-username/atlas)",
            "Atlas-Crawler/0.1.0 (+https://github.com/your-username/atlas)",
        ]
        return random.choice(user_agents)

    def set_random_user_agent(self) -> None:
        """设置随机 User-Agent"""
        random_ua = self.get_random_user_agent()
        self.set_user_agent(random_ua)
        self.logger.debug("使用随机 User-Agent", user_agent=random_ua)

    def set_custom_headers(self, headers: Dict[str, str]) -> None:
        """设置自定义请求头

        Args:
            headers: 请求头字典
        """
        self.session.headers.update(headers)
        self.logger.debug("自定义请求头已设置", headers=headers)

    def get_domain_from_url(self, url: str) -> str:
        """从 URL 中提取域名

        Args:
            url: URL 字符串

        Returns:
            域名字符串
        """
        parsed = urlparse(url)
        return parsed.netloc.lower()

    @abstractmethod
    def collect(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """采集数据的抽象方法

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        pass

    async def collect_async(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """异步采集数据的抽象方法

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        # 默认实现：同步转异步
        return await asyncio.get_event_loop().run_in_executor(
            None, self.collect, source_config
        )

    def collect_with_stats(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """带统计信息的采集

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        self.stats['total_collections'] += 1
        source_name = source_config.get('name', 'unknown')

        try:
            start_time = time.time()
            items = self.collect(source_config)
            elapsed_time = time.time() - start_time

            self.stats['successful_collections'] += 1
            self.stats['total_items'] += len(items)

            self.logger.info(f"采集完成", source=source_name, items=len(items),
                            elapsed_time=f"{elapsed_time:.3f}s")
            return items

        except Exception as e:
            self.stats['failed_collections'] += 1
            self.logger.exception(f"采集失败", source=source_name, error=str(e))
            return []

    async def collect_async_with_stats(self, source_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """异步带统计信息的采集

        Args:
            source_config: 数据源配置

        Returns:
            采集到的数据列表
        """
        self.stats['total_collections'] += 1
        source_name = source_config.get('name', 'unknown')

        try:
            start_time = time.time()
            items = await self.collect_async(source_config)
            elapsed_time = time.time() - start_time

            self.stats['successful_collections'] += 1
            self.stats['total_items'] += len(items)

            self.logger.info(f"异步采集完成", source=source_name, items=len(items),
                            elapsed_time=f"{elapsed_time:.3f}s")
            return items

        except Exception as e:
            self.stats['failed_collections'] += 1
            self.logger.exception(f"异步采集失败", source=source_name, error=str(e))
            return []

    def make_request(self, url: str, method: str = 'GET', **kwargs) -> Optional[Response]:
        """发送 HTTP 请求

        Args:
            url: 请求 URL
            method: HTTP 方法
            **kwargs: 其他请求参数

        Returns:
            HTTP 响应对象，失败时返回 None
        """
        domain = self.get_domain_from_url(url)

        # 频率限制检查
        if self.rate_limiter and not self.rate_limiter.acquire(domain, block=True, timeout=30):
            self.logger.warning(f"频率限制超时", url=url, domain=domain)
            return None

        try:
            self.logger.debug(f"发送请求", url=url, method=method, domain=domain)

            response = self.http_client.request(method, url, **kwargs)

            if response:
                self.logger.debug(f"请求成功", url=url, status_code=response.status_code,
                                from_cache=response.from_cache, elapsed_time=f"{response.elapsed_time:.3f}s")
            else:
                self.logger.warning(f"请求失败", url=url, method=method)

            return response

        except Exception as e:
            self.logger.exception(f"请求异常", url=url, method=method, error=str(e))
            return None

    async def make_request_async(self, url: str, method: str = 'GET', **kwargs) -> Optional[Response]:
        """发送异步 HTTP 请求

        Args:
            url: 请求 URL
            method: HTTP 方法
            **kwargs: 其他请求参数

        Returns:
            HTTP 响应对象，失败时返回 None
        """
        domain = self.get_domain_from_url(url)

        # 频率限制检查
        if self.rate_limiter:
            if not await self.rate_limiter.acquire_async(domain, block=True, timeout=30):
                self.logger.warning(f"异步频率限制超时", url=url, domain=domain)
                return None

        try:
            self.logger.debug(f"发送异步请求", url=url, method=method, domain=domain)

            response = await self.http_client.arequest(method, url, **kwargs)

            if response:
                self.logger.debug(f"异步请求成功", url=url, status_code=response.status_code,
                                elapsed_time=f"{response.elapsed_time:.3f}s")
            else:
                self.logger.warning(f"异步请求失败", url=url, method=method)

            return response

        except Exception as e:
            self.logger.exception(f"异步请求异常", url=url, method=method, error=str(e))
            return None

    def parse_html(self, html_content: str, parser: str = 'html.parser') -> BeautifulSoup:
        """解析 HTML 内容

        Args:
            html_content: HTML 内容
            parser: 解析器类型

        Returns:
            BeautifulSoup 对象
        """
        return BeautifulSoup(html_content, parser)

    def parse_rss(self, rss_content: str) -> feedparser.FeedParserDict:
        """解析 RSS 内容

        Args:
            rss_content: RSS 内容

        Returns:
            feedparser 解析结果
        """
        return feedparser.parse(rss_content)

    def extract_text(self, element, max_length: Optional[int] = None) -> str:
        """提取元素的文本内容

        Args:
            element: BeautifulSoup 元素
            max_length: 最大长度限制

        Returns:
            清理后的文本内容
        """
        if element is None:
            return ""

        text = element.get_text(strip=True)

        # 清理多余的空白字符
        text = ' '.join(text.split())

        if max_length and len(text) > max_length:
            text = text[:max_length].rstrip() + "..."

        return text

    def clean_content(self, content: str) -> str:
        """清理内容

        Args:
            content: 原始内容

        Returns:
            清理后的内容
        """
        if not content:
            return ""

        # 移除多余的空白字符
        content = ' '.join(content.split())

        # 移除 HTML 标签（如果有）
        import re
        content = re.sub(r'<[^>]+>', '', content)

        return content.strip()

    def validate_content(self, content: str, min_length: int = 100, max_length: int = 100000) -> bool:
        """验证内容是否符合要求

        Args:
            content: 内容
            min_length: 最小长度
            max_length: 最大长度

        Returns:
            是否有效
        """
        if not content:
            return False

        length = len(content)
        return min_length <= length <= max_length

    def standardize_item(self, item: Dict[str, Any], source_config: Dict[str, Any]) -> Dict[str, Any]:
        """标准化采集到的数据项

        Args:
            item: 原始数据项
            source_config: 数据源配置

        Returns:
            标准化后的数据项
        """
        standardized = {
            'source_name': source_config.get('name', 'unknown'),
            'source_type': source_config.get('type', 'unknown'),
            'source_url': source_config.get('url', ''),
            'title': item.get('title', ''),
            'content': item.get('content', item.get('description', '')),
            'link': item.get('link', ''),
            'pub_date': item.get('pub_date', item.get('published', item.get('date', ''))),
            'tags': source_config.get('tags', []),
            'category': source_config.get('category', ''),
            'collected_at': time.time(),
            'raw_data': item
        }

        # 清理内容
        standardized['content'] = self.clean_content(standardized['content'])

        # 验证内容长度
        if not self.validate_content(standardized['content']):
            self.logger.warning(f"内容长度不符合要求", title=standardized['title'],
                            length=len(standardized['content']))
            return None

        return standardized

    def configure_user_agent(self, source_config: Dict[str, Any]) -> None:
        """根据数据源配置 User-Agent

        Args:
            source_config: 数据源配置
        """
        # 优先使用数据源特定的 User-Agent 配置
        if 'user_agent' in source_config:
            ua_key = source_config['user_agent']

            # 获取采集器配置中的 User-Agent 选项
            collectors_config = source_config.get('_collectors_config', {})
            user_agents = collectors_config.get('user_agents', {}).get('options', {})

            if ua_key in user_agents:
                user_agent = user_agents[ua_key]
                self.set_user_agent(user_agent)
                self.logger.info(f"使用配置的 User-Agent", source=source_config.get('name'), ua_key=ua_key)
            else:
                self.logger.warning(f"未找到 User-Agent 配置", source=source_config.get('name'), ua_key=ua_key)

        # 如果启用了轮换，则随机选择
        elif self.config.rotate_user_agent or self.config.use_random_user_agent:
            self.set_random_user_agent()
            self.logger.info(f"使用随机 User-Agent", source=source_config.get('name'))

        # 否则使用默认配置
        else:
            self.logger.debug(f"使用默认 User-Agent", source=source_config.get('name'))

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.close()

    def get_stats(self) -> Dict[str, Any]:
        """获取采集器统计信息"""
        stats = self.stats.copy()

        # 计算运行时间
        if 'start_time' in stats:
            stats['runtime'] = time.time() - stats['start_time']

        # 计算成功率
        if stats['total_collections'] > 0:
            stats['success_rate'] = stats['successful_collections'] / stats['total_collections']
        else:
            stats['success_rate'] = 0.0

        # 添加 HTTP 客户端统计
        if self.http_client:
            stats['http_stats'] = self.http_client.get_stats()

        # 添加频率限制器统计
        if self.rate_limiter:
            stats['rate_limiter_stats'] = self.rate_limiter.get_stats()

        return stats

    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats = {
            'total_collections': 0,
            'successful_collections': 0,
            'failed_collections': 0,
            'total_items': 0,
            'start_time': time.time()
        }

        if self.http_client:
            self.http_client.reset_stats()

        if self.rate_limiter:
            self.rate_limiter.reset_history()

    def close(self) -> None:
        """关闭采集器，释放资源"""
        try:
            if self.http_client:
                # HTTP 客户端会在 __exit__ 中自动关闭
                pass
        except Exception as e:
            self.logger.warning(f"关闭 HTTP 客户端时出错", error=str(e))

    async def aclose(self) -> None:
        """异步关闭采集器"""
        try:
            if self.http_client:
                await self.http_client.aclose()
        except Exception as e:
            self.logger.warning(f"异步关闭 HTTP 客户端时出错", error=str(e))


class CollectorFactory:
    """采集器工厂类"""

    @staticmethod
    def create_collector(source_type: str, config: CollectionConfig) -> BaseCollector:
        """创建采集器实例

        Args:
            source_type: 数据源类型
            config: 采集配置

        Returns:
            采集器实例
        """
        from .rss_collector import RSSCollector
        from .web_collector import WebCollector

        collectors = {
            'rss': RSSCollector,
            'atom': RSSCollector,  # Atom 和 RSS 使用相同的采集器
            'web': WebCollector,
            'html': WebCollector,
        }

        collector_class = collectors.get(source_type.lower())
        if not collector_class:
            raise ValueError(f"不支持的数据源类型: {source_type}")

        return collector_class(config)

    @staticmethod
    def create_collector_with_config(source_config: Dict[str, Any], collection_config: CollectionConfig) -> BaseCollector:
        """创建采集器实例并传入完整配置

        Args:
            source_config: 数据源配置（包含采集器配置）
            collection_config: 采集配置

        Returns:
            采集器实例
        """
        collector = CollectorFactory.create_collector(source_config.get('type', ''), collection_config)

        # 将采集器配置传递给采集器实例
        if hasattr(collector, '_collectors_config'):
            collector._collectors_config = source_config.get('_collectors_config', {})
        else:
            collector._collectors_config = source_config.get('_collectors_config', {})

        return collector