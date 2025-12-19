"""
数据采集器模块单元测试
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from atlas.collectors.base import BaseCollector, CollectorFactory
from atlas.collectors.rss_collector import RSSCollector
from atlas.collectors.web_collector import WebCollector
from atlas.core.config import CollectionConfig
from tests.test_config import TEST_CONFIG


class TestBaseCollector:
    """基础采集器测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = Mock(spec=CollectionConfig)
        self.config.default_user_agent = "Atlas/0.1.0 Test"
        self.config.request_timeout = 30
        self.config.max_concurrent_requests = 3
        self.config.rate_limit_delay = 300
        self.config.use_random_user_agent = False
        self.config.rotate_user_agent = False

        self.collector = BaseCollector(self.config)

    def test_init(self):
        """测试初始化"""
        assert self.collector.config is self.config
        assert self.collector.session is not None

    def test_setup_session(self):
        """测试设置会话"""
        assert 'User-Agent' in self.collector.session.headers
        assert self.collector.session.headers['User-Agent'] == self.config.default_user_agent

    def test_get_domain_from_url(self):
        """测试从 URL 提取域名"""
        test_domain = TEST_CONFIG.get_domain("example")
        assert self.collector.get_domain_from_url(f"https://{test_domain}/path") == test_domain
        assert self.collector.get_domain_from_url(f"http://sub.{test_domain}/path") == f"sub.{test_domain}"
        assert self.collector.get_domain_from_url(f"https://{test_domain}:8080") == test_domain

    def test_set_user_agent(self):
        """测试设置 User-Agent"""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.collector.set_user_agent(ua)
        assert self.collector.session.headers['User-Agent'] == ua

    def test_get_random_user_agent(self):
        """测试获取随机 User-Agent"""
        ua = self.collector.get_random_user_agent()
        assert isinstance(ua, str)
        assert len(ua) > 0
        # 应该包含常见的浏览器标识
        assert any(browser in ua.lower() for browser in ['mozilla', 'chrome', 'safari', 'firefox'])

    def test_set_random_user_agent(self):
        """测试设置随机 User-Agent"""
        self.collector.set_random_user_agent()
        ua = self.collector.session.headers['User-Agent']
        assert isinstance(ua, str)
        assert len(ua) > 0

    def test_make_request_success(self):
        """测试成功的 HTTP 请求"""
        with patch.object(self.collector.session, 'request') as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_request.return_value = mock_response

            test_url = TEST_CONFIG.get_url("rss_feed")
            response = self.collector.make_request(test_url)

            assert response is not None
            mock_request.assert_called_once_with("GET", test_url)

    def test_make_request_failure(self):
        """测试失败的 HTTP 请求"""
        with patch.object(self.collector.session, 'request') as mock_request:
            mock_request.side_effect = Exception("Network error")

            test_url = TEST_CONFIG.get_url("rss_feed")
            response = self.collector.make_request(test_url)

            assert response is None

    def test_parse_html(self):
        """测试 HTML 解析"""
        html_content = "<html><body><h1>Test</h1></body></html>"
        soup = self.collector.parse_html(html_content)
        assert soup.find('h1').text == "Test"

    def test_parse_rss(self):
        """测试 RSS 解析"""
        rss_content = """<?xml version="1.0"?>
        <rss version="2.0">
        <channel>
        <title>Test Feed</title>
        </channel>
        </rss>"""

        feed = self.collector.parse_rss(rss_content)
        assert hasattr(feed, 'entries')
        assert feed.feed.get('title') == 'Test Feed'

    def test_extract_text(self):
        """测试提取文本内容"""
        from bs4 import BeautifulSoup

        html = "<div>Test <b>content</b></div>"
        soup = BeautifulSoup(html, 'html.parser')
        element = soup.find('div')

        text = self.collector.extract_text(element)
        assert text == "Test content"

        # 测试长度限制
        short_text = self.collector.extract_text(element, max_length=5)
        assert short_text == "Test..."

    def test_clean_content(self):
        """测试内容清理"""
        content = "  Test   content  with   spaces  "
        cleaned = self.collector.clean_content(content)
        assert cleaned == "Test content with spaces"

        content_with_tags = "Test <b>content</b> with <i>tags</i>"
        cleaned = self.collector.clean_content(content_with_tags)
        assert cleaned == "Test content with tags"

    def test_validate_content(self):
        """测试内容验证"""
        # 有效内容
        assert self.collector.validate_content("Valid content with enough length")

        # 内容太短
        assert not self.collector.validate_content("Too short")

        # 内容太长
        long_content = "a" * 200000
        assert not self.collector.validate_content(long_content)

    def test_standardize_item(self):
        """测试数据项标准化"""
        item = {
            'title': 'Test Title',
            'description': 'Test Description',
            'link': 'https://example.com'
        }

        source_config = {
            'name': 'test_source',
            'type': 'rss',
            'url': 'https://example.com/rss',
            'tags': ['test', 'rss'],
            'category': 'test_category'
        }

        standardized = self.collector.standardize_item(item, source_config)

        assert standardized['source_name'] == 'test_source'
        assert standardized['source_type'] == 'rss'
        assert standardized['title'] == 'Test Title'
        assert standardized['content'] == 'Test Description'
        assert standardized['link'] == 'https://example.com'
        assert standardized['tags'] == ['test', 'rss']
        assert standardized['category'] == 'test_category'

    def test_standardize_item_with_minimal_data(self):
        """测试最小数据项的标准化"""
        item = {}
        source_config = {}

        standardized = self.collector.standardize_item(item, source_config)

        assert standardized['source_name'] == 'unknown'
        assert standardized['source_type'] == 'unknown'
        assert standardized['content'] == ''

    def test_context_manager(self):
        """测试上下文管理器"""
        with self.collector as collector:
            assert collector is not None

        # 会话应该已关闭
        with patch.object(self.collector.session, 'close') as mock_close:
            del self.collector
            # 触发析构函数
            mock_close.assert_called_once()


class TestCollectorFactory:
    """采集器工厂测试"""

    def test_create_rss_collector(self):
        """测试创建 RSS 采集器"""
        config = Mock(spec=CollectionConfig)
        collector = CollectorFactory.create_collector('rss', config)
        assert isinstance(collector, RSSCollector)

    def test_create_web_collector(self):
        """测试创建 Web 采集器"""
        config = Mock(spec=CollectionConfig)
        collector = CollectorFactory.create_collector('web', config)
        assert isinstance(collector, WebCollector)

    def test_create_atom_collector(self):
        """测试创建 Atom 采集器（应该使用 RSS 采集器）"""
        config = Mock(spec=CollectionConfig)
        collector = CollectorFactory.create_collector('atom', config)
        assert isinstance(collector, RSSCollector)

    def test_create_html_collector(self):
        """测试创建 HTML 采集器（应该使用 Web 采集器）"""
        config = Mock(spec=CollectionConfig)
        collector = CollectorFactory.create_collector('html', config)
        assert isinstance(collector, WebCollector)

    def test_create_invalid_collector(self):
        """测试创建无效类型的采集器"""
        config = Mock(spec=CollectionConfig)
        with pytest.raises(ValueError, match="不支持的数据源类型"):
            CollectorFactory.create_collector('invalid', config)

    def test_create_collector_with_config(self):
        """测试带配置的采集器创建"""
        config = Mock(spec=CollectionConfig)
        source_config = {
            'type': 'rss',
            '_collectors_config': {
                'user_agents': {
                    'options': {
                        'test_ua': 'Test User-Agent'
                    }
                }
            }
        }

        collector = CollectorFactory.create_collector_with_config(source_config, config)
        assert isinstance(collector, RSSCollector)
        assert hasattr(collector, '_collectors_config')
        assert collector._collectors_config == source_config['_collectors_config']


class TestRSSCollector:
    """RSS 采集器测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = Mock(spec=CollectionConfig)
        self.config.default_user_agent = "Atlas/0.1.0 Test"
        self.config.request_timeout = 30
        self.config.max_concurrent_requests = 3
        self.config.rate_limit_delay = 300

        self.collector = RSSCollector(self.config)

    def test_collect_success(self):
        """测试成功的 RSS 采集"""
        source_config = {
            'name': 'test_rss',
            'type': 'rss',
            'url': 'https://example.com/rss.xml',
            'tags': ['test'],
            'category': 'test'
        }

        with patch.object(self.collector, 'make_request') as mock_request:
            with patch.object(self.collector, 'parse_rss') as mock_parse:
                # 模拟响应
                mock_response = Mock()
                mock_response.text = "rss content"
                mock_request.return_value = mock_response

                # 模拟 RSS 解析结果
                mock_feed = Mock()
                mock_feed.bozo = False
                mock_feed.entries = [
                    {
                        'title': 'Test Article',
                        'link': 'https://example.com/article1',
                        'description': 'Test description',
                        'id': 'https://example.com/article1'
                    }
                ]
                mock_parse.return_value = mock_feed

                items = self.collector.collect(source_config)

                assert len(items) == 1
                assert items[0]['title'] == 'Test Article'
                assert items[0]['source_name'] == 'test_rss'

    def test_collect_no_url(self):
        """测试没有 URL 的配置"""
        source_config = {
            'name': 'test_rss',
            'type': 'rss'
            # 缺少 url
        }

        items = self.collector.collect(source_config)
        assert items == []

    def test_extract_entry(self):
        """测试提取 RSS 条目"""
        mock_entry = Mock()
        mock_entry.title = 'Test Title'
        mock_entry.link = 'https://example.com'
        mock_entry.description = 'Test Description'
        mock_entry.published_parsed = Mock()
        mock_entry.published_parsed.tm_year = 2024
        mock_entry.published_parsed.tm_mon = 1
        mock_entry.published_parsed.tm_mday = 1

        item = self.collector._extract_entry(mock_entry)

        assert item['title'] == 'Test Title'
        assert item['link'] == 'https://example.com'
        assert item['description'] == 'Test Description'
        assert 'pub_date' in item


class TestWebCollector:
    """Web 采集器测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = Mock(spec=CollectionConfig)
        self.config.default_user_agent = "Atlas/0.1.0 Test"
        self.config.request_timeout = 30

        self.collector = WebCollector(self.config)

    def test_collect_success(self):
        """测试成功的 Web 采集"""
        source_config = {
            'name': 'test_web',
            'type': 'web',
            'url': 'https://example.com',
            'tags': ['test'],
            'category': 'test',
            'selectors': {
                'title': 'h1',
                'content': 'div.content'
            }
        }

        with patch.object(self.collector, 'make_request') as mock_request:
            with patch.object(self.collector, 'parse_html') as mock_parse:
                # 模拟响应
                mock_response = Mock()
                mock_response.text = "<html><body><h1>Test Title</h1><div class='content'>Test Content</div></body></html>"
                mock_request.return_value = mock_response

                # 模拟 HTML 解析
                mock_soup = Mock()
                mock_title = Mock()
                mock_title.get_text.return_value = 'Test Title'
                mock_content = Mock()
                mock_content.get_text.return_value = 'Test Content'

                mock_soup.select_one.side_effect = lambda selector: {
                    'h1': mock_title,
                    'div.content': mock_content
                }.get(selector)
                mock_soup.find.return_value = Mock()  # body 模拟

                mock_parse.return_value = mock_soup

                items = self.collector.collect(source_config)

                assert len(items) == 1
                assert items[0]['title'] == 'Test Title'
                assert items[0]['content'] == 'Test Content'

    def test_extract_page_info(self):
        """测试页面信息提取"""
        source_config = {
            'name': 'test_web',
            'selectors': {
                'title': 'h1',
                'content': 'main article',
                'author': '.author'
            }
        }

        # 模拟 HTML
        from bs4 import BeautifulSoup

        html = """
        <html>
            <h1>Page Title</h1>
            <main>
                <article>
                    <div class="content">Article Content</div>
                    <div class="author">Author Name</div>
                </article>
            </main>
        </html>
        """
        soup = BeautifulSoup(html, 'html.parser')

        item = self.collector._extract_page_info(soup, source_config)

        assert item['title'] == 'Page Title'
        assert item['content'] == 'Article Content'
        assert item['author'] == 'Author Name'