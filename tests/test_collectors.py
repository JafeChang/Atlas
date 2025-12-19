"""
采集器模块测试

测试 HTTP 客户端、频率限制器和采集器功能。
"""

import pytest
import time
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from pathlib import Path

from atlas.collectors.http_client import HTTPClient, RequestConfig, Response, CacheManager
from atlas.collectors.rate_limiter import (
    RateLimiter, RateLimitConfig, RateLimitStrategy,
    AdaptiveRateLimiter, MultiDomainRateLimiter
)
from atlas.collectors.base import BaseCollector
from atlas.collectors.rss_collector import RSSCollector
from tests.utils import create_sample_config, TestDataGenerator
from tests.test_config import TEST_CONFIG, TEST_RSS_CONFIG


class TestHTTPClient:
    """HTTP 客户端测试"""

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        config = Mock()
        config.default_user_agent = "Atlas/0.1.0 Test"
        config.request_timeout = 30
        config.rate_limit_delay = 300
        config.use_random_user_agent = False
        config.rotate_user_agent = False
        return config

    @pytest.fixture
    def request_config(self):
        """请求配置"""
        return RequestConfig(
            timeout=30,
            max_retries=3,
            use_cache=True,
            cache_ttl=3600
        )

    def test_http_client_init(self, mock_config, request_config):
        """测试 HTTP 客户端初始化"""
        client = HTTPClient(mock_config, request_config)
        assert client.config == mock_config
        assert client.request_config == request_config
        assert client.cache_manager is not None
        assert client.rate_limiter is not None
        assert client.stats['total_requests'] == 0

    @patch('requests.Session.request')
    def test_http_client_get_success(self, mock_request, mock_config, request_config):
        """测试成功 GET 请求"""
        # 模拟响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "test content"
        mock_response.content = b"test content"
        mock_response.encoding = "utf-8"
        mock_response.url = TEST_CONFIG.get_url("rss_feed")
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_request.return_value = mock_response

        client = HTTPClient(mock_config, request_config)
        response = client.get(TEST_CONFIG.get_url("rss_feed"))

        assert response is not None
        assert response.status_code == 200
        assert response.text == "test content"
        assert response.from_cache is False

    @patch('requests.Session.request')
    def test_http_client_timeout(self, mock_request, mock_config, request_config):
        """测试请求超时"""
        import requests
        mock_request.side_effect = requests.exceptions.Timeout()

        client = HTTPClient(mock_config, request_config)
        response = client.get(TEST_CONFIG.get_url("rss_feed"))

        assert response is None
        assert client.stats['failed_requests'] == 1

    def test_cache_manager(self):
        """测试缓存管理器"""
        cache_manager = CacheManager()
        test_url = TEST_CONFIG.get_url("rss_feed")

        # 测试缓存设置和获取
        response = Response(
            url=test_url,
            status_code=200,
            headers={"Content-Type": "text/plain"},
            content="test content".encode('utf-8'),
            text="test content",
            encoding="utf-8",
            elapsed_time=0.5
        )

        # 设置缓存
        cache_manager.set(response, test_url)

        # 获取缓存
        cached_response = cache_manager.get(test_url)
        assert cached_response is not None
        assert cached_response.text == "test content"
        assert cached_response.from_cache is True

    def test_cache_expiration(self):
        """测试缓存过期"""
        cache_manager = CacheManager()
        test_url = TEST_CONFIG.get_url("rss_feed")

        response = Response(
            url=test_url,
            status_code=200,
            headers={},
            content="test content".encode('utf-8'),
            text="test content",
            encoding="utf-8",
            elapsed_time=0.5
        )

        # 设置短缓存时间
        cache_manager.set(response, test_url, ttl=1)

        # 立即获取应该成功
        cached_response = cache_manager.get(test_url)
        assert cached_response is not None

        # 等待过期
        time.sleep(1.1)

        # 再次获取应该失败
        cached_response = cache_manager.get(test_url)
        assert cached_response is None


class TestRateLimiter:
    """频率限制器测试"""

    def test_fixed_window_limiter(self):
        """测试固定窗口频率限制器"""
        from atlas.collectors.rate_limiter import FixedWindowLimiter

        limiter = FixedWindowLimiter(requests_per_second=10, window_size=1.0)

        # 在限制范围内应该成功
        assert limiter.acquire() is True
        assert limiter.acquire() is True

        # 超过限制应该失败
        for _ in range(10):
            limiter.acquire()  # 消耗所有配额

        assert limiter.acquire() is False

    def test_sliding_window_limiter(self):
        """测试滑动窗口频率限制器"""
        from atlas.collectors.rate_limiter import SlidingWindowLimiter

        limiter = SlidingWindowLimiter(requests_per_second=5, window_size=1.0)

        # 测试正常使用
        assert limiter.acquire() is True
        assert limiter.acquire() is True

    def test_token_bucket_limiter(self):
        """测试令牌桶频率限制器"""
        from atlas.collectors.rate_limiter import TokenBucketLimiter

        limiter = TokenBucketLimiter(refill_rate=10, bucket_size=20)

        # 初始应该有足够的令牌
        for _ in range(20):
            assert limiter.acquire() is True

        # 超过桶容量应该失败
        assert limiter.acquire() is False

    def test_adaptive_rate_limiter(self):
        """测试自适应频率限制器"""
        config = RateLimitConfig(
            requests_per_second=10,
            strategy=RateLimitStrategy.SLIDING_WINDOW
        )
        limiter = AdaptiveRateLimiter(config)

        # 测试正常获取
        assert limiter.acquire("example.com") is True

        # 测试统计信息
        stats = limiter.get_stats()
        assert 'domains' in stats
        assert 'current_config' in stats

    def test_multi_domain_rate_limiter(self):
        """测试多域名频率限制器"""
        default_config = RateLimitConfig(requests_per_second=10)
        limiter = MultiDomainRateLimiter(default_config)

        # 测试不同域名独立限制
        assert limiter.acquire("example.com") is True
        assert limiter.acquire("test.com") is True

        # 设置域名特定限制
        domain_config = RateLimitConfig(requests_per_second=2)
        limiter.set_domain_config("restricted.com", domain_config)

        assert limiter.acquire("restricted.com") is True


class TestBaseCollector:
    """基础采集器测试"""

    def test_collector_init(self):
        """测试采集器初始化"""
        config = create_sample_config()

        class TestCollector(BaseCollector):
            def collect(self, source_config):
                return []

        collector = TestCollector(config, use_rate_limiter=True)
        assert collector.config == config
        assert collector.http_client is not None
        assert collector.rate_limiter is not None
        assert collector.stats['total_collections'] == 0

    def test_collector_stats(self):
        """测试采集器统计"""
        config = create_sample_config()

        class TestCollector(BaseCollector):
            def collect(self, source_config):
                return [{"title": "test"}]

        collector = TestCollector(config, use_rate_limiter=True)

        # 测试统计信息
        stats = collector.get_stats()
        assert 'total_collections' in stats
        assert 'success_rate' in stats
        assert 'runtime' in stats

        # 测试重置统计
        collector.reset_stats()
        stats = collector.get_stats()
        assert stats['total_collections'] == 0

    def test_domain_extraction(self):
        """测试域名提取"""
        config = create_sample_config()

        class TestCollector(BaseCollector):
            def collect(self, source_config):
                return []

        collector = TestCollector(config)

        assert collector.get_domain_from_url(f"https://{TEST_CONFIG.get_domain('example')}/path") == TEST_CONFIG.get_domain('example')
        assert collector.get_domain_from_url(f"http://{TEST_CONFIG.get_domain('subdomain')}/path") == TEST_CONFIG.get_domain('subdomain')


class TestRSSCollector:
    """RSS 采集器测试"""

    @pytest.fixture
    def mock_config(self):
        """模拟配置"""
        config = create_sample_config()
        return config

    @pytest.fixture
    def sample_rss_config(self):
        """示例 RSS 配置"""
        return TEST_RSS_CONFIG.get_config("standard")

    def test_rss_collector_init(self, mock_config):
        """测试 RSS 采集器初始化"""
        collector = RSSCollector(mock_config)
        assert collector.config == mock_config
        assert collector.http_client is not None

    @patch.object(RSSCollector, 'make_request')
    def test_rss_collect_success(self, mock_request, mock_config, sample_rss_config):
        """测试 RSS 采集成功"""
        # 模拟响应
        mock_response = Mock()
        mock_response.text = TestDataGenerator.generate_rss_feed(3)
        mock_request.return_value = mock_response

        collector = RSSCollector(mock_config)
        items = collector.collect(sample_rss_config)

        assert len(items) == 3
        assert items[0]['title'] == 'Test Article 1'
        assert items[0]['source_name'] == 'test-rss'
        assert 'collected_at' in items[0]

    @patch.object(RSSCollector, 'make_request')
    def test_rss_collect_failure(self, mock_request, mock_config, sample_rss_config):
        """测试 RSS 采集失败"""
        mock_request.return_value = None

        collector = RSSCollector(mock_config)
        items = collector.collect(sample_rss_config)

        assert len(items) == 0

    def test_extract_entry(self, mock_config):
        """测试 RSS 条目提取"""
        collector = RSSCollector(mock_config)
        base_url = TEST_CONFIG.get_full_url("example", "/")

        # 模拟 feedparser 条目
        mock_entry = Mock()
        mock_entry.title = "Test Article"
        mock_entry.link = "/article"
        mock_entry.description = "Test description"
        mock_entry.published_parsed = time.struct_time((2024, 1, 1, 12, 0, 0, 0, 1, 0))
        mock_entry.id = "article-123"

        item = collector._extract_entry(mock_entry, base_url)

        assert item['title'] == "Test Article"
        assert item['link'] == TEST_CONFIG.get_full_url("example", "/article")
        assert item['description'] == "Test description"
        assert item['pub_date'] == "2024-01-01 12:00:00"
        assert item['id'] == "article-123"

    def test_validate_rss_entry(self, mock_config):
        """测试 RSS 条目验证"""
        collector = RSSCollector(mock_config)

        # 有效条目
        valid_entry = {
            'title': 'Test Article',
            'link': TEST_CONFIG.get_full_url("example", "/article"),
            'content': 'Test content with enough length'
        }
        assert collector.validate_rss_entry(valid_entry) is True

        # 无效条目 - 缺少标题和链接
        invalid_entry = {
            'content': 'Test content'
        }
        assert collector.validate_rss_entry(invalid_entry) is False

        # 无效条目 - 内容太短
        invalid_entry = {
            'title': 'Test',
            'content': 'Short'
        }
        assert collector.validate_rss_entry(invalid_entry) is False

    def test_relative_link_handling(self, mock_config):
        """测试相对链接处理"""
        collector = RSSCollector(mock_config)
        base_url = TEST_CONFIG.get_full_url("example", "/")

        mock_entry = Mock()
        mock_entry.title = "Test Article"
        mock_entry.link = "/relative/path"
        mock_entry.description = "Test description"

        item = collector._extract_entry(mock_entry, base_url)

        assert item['link'] == TEST_CONFIG.get_full_url("example", "/relative/path")


class TestCollectorIntegration:
    """采集器集成测试"""

    @pytest.mark.asyncio
    async def test_async_collection(self):
        """测试异步采集"""
        config = create_sample_config()

        class AsyncTestCollector(BaseCollector):
            async def collect_async(self, source_config):
                # 模拟异步操作
                await asyncio.sleep(0.01)
                return [{"title": "async item"}]

        collector = AsyncTestCollector(config)
        source_config = {
            "name": "async-test",
            "type": "test",
            "url": TEST_CONFIG.get_url("rss_feed")
        }

        items = await collector.collect_async(source_config)
        assert len(items) == 1
        assert items[0]['title'] == "async item"

    @pytest.mark.asyncio
    async def test_collector_with_stats(self):
        """测试带统计信息的采集"""
        config = create_sample_config()

        class StatsTestCollector(BaseCollector):
            def collect(self, source_config):
                return [{"title": "item1"}, {"title": "item2"}]

        collector = StatsTestCollector(config)
        source_config = {
            "name": "stats-test",
            "type": "test",
            "url": TEST_CONFIG.get_url("rss_feed")
        }

        # 测试带统计的采集
        items = collector.collect_with_stats(source_config)
        assert len(items) == 2
        assert collector.stats['successful_collections'] == 1
        assert collector.stats['total_items'] == 2

    def test_context_manager(self, mock_config):
        """测试上下文管理器"""
        class TestCollector(BaseCollector):
            def collect(self, source_config):
                return []

        collector = TestCollector(mock_config)

        with collector as c:
            assert c == collector

        # 应该正确关闭资源
        # 这里主要测试没有异常抛出

    @pytest.mark.asyncio
    async def test_async_context_manager(self, mock_config):
        """测试异步上下文管理器"""
        class AsyncTestCollector(BaseCollector):
            async def collect_async(self, source_config):
                return []

        collector = AsyncTestCollector(mock_config)

        async with collector as c:
            assert c == collector

        # 测试异步关闭
        await collector.aclose()