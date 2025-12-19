"""
测试配置文件

管理测试中使用的URL、域名等配置，避免硬编码。
"""

from typing import Dict, Any


class TestConfig:
    """测试配置类"""

    # 测试URL配置 - 使用真实RSS源
    TEST_URLS = {
        "base_domain": "rsshub.app",
        "subdomain": "feeds.bbci.co.uk",
        "port_domain": "example.com:8080",

        # RSS相关 - 真实可用RSS源
        "rss_feed": "https://rsshub.app/python/python/topics",
        "rss_feed_with_auth": "https://secure.example.com/rss.xml",

        # API相关
        "api_base": "https://api.example.com",
        "api_endpoint": "https://api.example.com/v1/data",

        # 采集器测试 - 真实RSS源
        "tech_rss": "https://rsshub.app/ithome/topics/hot",
        "news_rss": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "blog_site": "https://rsshub.app/zhihu/daily-hot",
        "github_python": "https://rsshub.app/github/trending/daily/python",
        "infoq_tech": "https://www.infoq.cn/feed",
        "stackoverflow_questions": "https://stackoverflow.com/feeds",
        "v2ex_hot": "https://rsshub.app/v2ex/topics/hot",
        "juejin_category": "https://rsshub.app/juejin/category/backend",

        # 错误测试
        "invalid_url": "https://invalid-url.com/rss.xml",
        "timeout_url": "https://slow.example.com",
    }

    # 测试域名配置 - 对应真实RSS源
    TEST_DOMAINS = {
        "example": "rsshub.app",
        "bbc": "feeds.bbci.co.uk",
        "github": "rsshub.app",
        "infoq": "www.infoq.cn",
        "stackoverflow": "stackoverflow.com",
        "v2ex": "rsshub.app",
        "juejin": "rsshub.app",
        "restricted": "restricted.example.com",
        "test": "test.example.com",
    }

    # 测试内容配置 - 真实RSS可能的内容
    TEST_CONTENT = {
        "article_links": [
            "https://github.com/trending/python",
            "https://www.infoq.cn/article/2024/01/tech-trends",
            "https://stackoverflow.com/questions/12345678/python-help",
            "https://www.v2ex.com/t/123456",
            "https://juejin.cn/post/1234567890",
        ],
        "relative_paths": [
            "/python/python/topics",
            "/ithome/topics/hot",
            "/github/trending/daily/python",
            "/v2ex/topics/hot",
            "/juejin/category/backend",
        ],
        "expected_tags": [
            "python", "programming", "tech", "development",
            "tutorial", "trending", "hot", "opensource",
            "architecture", "enterprise", "startup", "discussion"
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

    # 标准RSS配置 - 使用Python主题RSS
    STANDARD_RSS_CONFIG = {
        "name": "python-topics",
        "type": "rss",
        "url": TestConfig.get_url("rss_feed"),
        "enabled": True,
        "tags": ["python", "programming", "test"],
        "category": "development"
    }

    # 技术博客配置 - 使用IT之家热门主题
    TECH_BLOG_CONFIG = {
        "name": "ithome-hot",
        "type": "rss",
        "url": TestConfig.get_url("tech_rss"),
        "enabled": True,
        "tags": ["tech", "programming", "hot"],
        "category": "technology"
    }

    # 新闻站点配置 - 使用BBC世界新闻
    NEWS_SITE_CONFIG = {
        "name": "bbc-world-news",
        "type": "rss",
        "url": TestConfig.get_url("news_rss"),
        "enabled": False,
        "tags": ["news", "world", "general"],
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

    # GitHub Python趋势配置
    GITHUB_PYTHON_CONFIG = {
        "name": "github-python-trending",
        "type": "rss",
        "url": TestConfig.get_url("github_python"),
        "enabled": True,
        "tags": ["github", "python", "trending", "opensource"],
        "category": "development"
    }

    # InfoQ技术配置
    INFOQ_TECH_CONFIG = {
        "name": "infoq-tech",
        "type": "rss",
        "url": TestConfig.get_url("infoq_tech"),
        "enabled": True,
        "tags": ["infoq", "tech", "architecture", "enterprise"],
        "category": "technology"
    }

    # Stack Overflow问答配置
    STACKOVERFLOW_CONFIG = {
        "name": "stackoverflow-questions",
        "type": "rss",
        "url": TestConfig.get_url("stackoverflow_questions"),
        "enabled": True,
        "tags": ["stackoverflow", "qa", "programming", "help"],
        "category": "development"
    }

    # V2EX热门配置
    V2EX_HOT_CONFIG = {
        "name": "v2ex-hot",
        "type": "rss",
        "url": TestConfig.get_url("v2ex_hot"),
        "enabled": True,
        "tags": ["v2ex", "tech", "startup", "discussion"],
        "category": "technology"
    }

    # 掘金后端分类配置
    JUEJIN_BACKEND_CONFIG = {
        "name": "juejin-backend",
        "type": "rss",
        "url": TestConfig.get_url("juejin_category"),
        "enabled": True,
        "tags": ["juejin", "backend", "programming", "tutorial"],
        "category": "development"
    }

    @classmethod
    def get_config(cls, config_type: str) -> Dict[str, Any]:
        """获取指定类型的RSS配置"""
        configs = {
            "standard": cls.STANDARD_RSS_CONFIG,
            "tech": cls.TECH_BLOG_CONFIG,
            "news": cls.NEWS_SITE_CONFIG,
            "auth": cls.AUTH_RSS_CONFIG,
            "github": cls.GITHUB_PYTHON_CONFIG,
            "infoq": cls.INFOQ_TECH_CONFIG,
            "stackoverflow": cls.STACKOVERFLOW_CONFIG,
            "v2ex": cls.V2EX_HOT_CONFIG,
            "juejin": cls.JUEJIN_BACKEND_CONFIG,
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