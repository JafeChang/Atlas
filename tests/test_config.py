"""
测试配置文件

管理测试中使用的URL、域名等配置，避免硬编码。
"""

from typing import Dict, Any


class TestConfig:
    """测试配置类"""

    # 测试URL配置
    TEST_URLS = {
        "base_domain": "example.com",
        "subdomain": "sub.test.com",
        "port_domain": "example.com:8080",

        # RSS相关
        "rss_feed": "https://example.com/rss.xml",
        "rss_feed_with_auth": "https://secure.example.com/rss.xml",

        # API相关
        "api_base": "https://api.example.com",
        "api_endpoint": "https://api.example.com/v1/data",

        # 采集器测试
        "tech_rss": "https://feeds.example.com/tech",
        "news_rss": "https://feeds.example.com/news",
        "blog_site": "https://blog.example.com",

        # 错误测试
        "invalid_url": "https://invalid-url.com/rss.xml",
        "timeout_url": "https://slow.example.com",
    }

    # 测试域名配置
    TEST_DOMAINS = {
        "example": "example.com",
        "subdomain": "sub.example.com",
        "restricted": "restricted.example.com",
        "test": "test.example.com",
    }

    # 测试内容配置
    TEST_CONTENT = {
        "article_links": [
            "https://example.com/article1",
            "https://example.com/article2",
            "https://example.com/article3",
        ],
        "relative_paths": [
            "/relative/path",
            "/api/v1/data",
            "/blog/test-article",
        ]
    }

    @classmethod
    def get_url(cls, key: str) -> str:
        """获取测试URL"""
        return cls.TEST_URLS.get(key, f"https://{cls.TEST_URLS['base_domain']}")

    @classmethod
    def get_domain(cls, key: str) -> str:
        """获取测试域名"""
        return cls.TEST_DOMAINS.get(key, cls.TEST_DOMAINS["example"])

    @classmethod
    def get_full_url(cls, domain_key: str, path: str) -> str:
        """构建完整URL"""
        domain = cls.get_domain(domain_key)
        return f"https://{domain}{path}"


class TestRSSConfig:
    """RSS测试配置"""

    # 标准RSS配置
    STANDARD_RSS_CONFIG = {
        "name": "test-rss",
        "type": "rss",
        "url": TestConfig.get_url("rss_feed"),
        "enabled": True,
        "tags": ["test", "rss"],
        "category": "test"
    }

    # 技术博客配置
    TECH_BLOG_CONFIG = {
        "name": "tech-blog",
        "type": "rss",
        "url": TestConfig.get_url("tech_rss"),
        "enabled": True,
        "tags": ["tech", "programming"],
        "category": "technology"
    }

    # 新闻站点配置
    NEWS_SITE_CONFIG = {
        "name": "news-site",
        "type": "rss",
        "url": TestConfig.get_url("news_rss"),
        "enabled": False,
        "tags": ["news", "general"],
        "category": "news"
    }

    # 需要认证的RSS
    AUTH_RSS_CONFIG = {
        "name": "secure-rss",
        "type": "rss",
        "url": TestConfig.get_url("rss_feed_with_auth"),
        "enabled": True,
        "tags": ["secure", "private"],
        "category": "premium"
    }

    @classmethod
    def get_config(cls, config_type: str) -> Dict[str, Any]:
        """获取指定类型的RSS配置"""
        configs = {
            "standard": cls.STANDARD_RSS_CONFIG,
            "tech": cls.TECH_BLOG_CONFIG,
            "news": cls.NEWS_SITE_CONFIG,
            "auth": cls.AUTH_RSS_CONFIG,
        }
        return configs.get(config_type, cls.STANDARD_RSS_CONFIG)


class TestWebConfig:
    """Web采集器测试配置"""

    # 标准网站配置
    STANDARD_WEB_CONFIG = {
        "name": "test-website",
        "type": "web",
        "url": TestConfig.get_url("blog_site"),
        "enabled": True,
        "tags": ["test", "web"],
        "category": "blog",
        "selectors": {
            "title": "h1.title",
            "content": "div.content",
            "date": "time.date",
            "author": "span.author"
        }
    }

    @classmethod
    def get_config(cls, config_type: str) -> Dict[str, Any]:
        """获取指定类型的Web配置"""
        configs = {
            "standard": cls.STANDARD_WEB_CONFIG,
        }
        return configs.get(config_type, cls.STANDARD_WEB_CONFIG)


class TestErrorConfig:
    """错误测试配置"""

    # 超时配置
    TIMEOUT_CONFIG = {
        "name": "timeout-test",
        "type": "rss",
        "url": TestConfig.get_url("timeout_url"),
        "enabled": True,
        "tags": ["test", "error"],
        "category": "test"
    }

    # 无效URL配置
    INVALID_URL_CONFIG = {
        "name": "invalid-url-test",
        "type": "rss",
        "url": TestConfig.get_url("invalid_url"),
        "enabled": True,
        "tags": ["test", "error"],
        "category": "test"
    }

    @classmethod
    def get_config(cls, error_type: str) -> Dict[str, Any]:
        """获取指定类型的错误配置"""
        configs = {
            "timeout": cls.TIMEOUT_CONFIG,
            "invalid": cls.INVALID_URL_CONFIG,
        }
        return configs.get(error_type, cls.INVALID_URL_CONFIG)


# 全局测试配置实例
TEST_CONFIG = TestConfig()
TEST_RSS_CONFIG = TestRSSConfig()
TEST_WEB_CONFIG = TestWebConfig()
TEST_ERROR_CONFIG = TestErrorConfig()