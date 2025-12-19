"""
pytest 配置文件

提供测试夹具和测试环境配置。
"""

import asyncio
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

# 添加 src 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# 导入测试配置
from tests.test_config import TEST_CONFIG


@pytest.fixture(scope="session")
def test_config_dir():
    """创建临时测试配置目录"""
    temp_dir = Path(tempfile.mkdtemp(prefix="atlas_test_"))

    # 创建必要的子目录
    (temp_dir / "data").mkdir(parents=True)
    (temp_dir / "logs").mkdir(parents=True)
    (temp_dir / "data" / "raw").mkdir(parents=True)
    (temp_dir / "data" / "processed").mkdir(parents=True)
    (temp_dir / "data" / "indexes").mkdir(parents=True)

    # 创建基础配置文件
    config_content = {
        "env": "test",
        "debug": True,
        "log_level": "DEBUG",
        "data_dir": str(temp_dir / "data"),
        "log_dir": str(temp_dir / "logs")
    }

    with open(temp_dir / "config.yaml", "w", encoding='utf-8') as f:
        yaml.dump(config_content, f, default_flow_style=False, allow_unicode=True)

    # 创建数据源配置
    sources_content = {
        "sources": [
            {
                "name": "test-rss",
                "type": "rss",
                "url": TEST_CONFIG.get_url("rss_feed"),
                "interval": 3600,
                "enabled": True,
                "tags": ["test", "rss"],
                "category": "test"
            }
        ]
    }

    with open(temp_dir / "sources.yaml", "w", encoding='utf-8') as f:
        yaml.dump(sources_content, f, default_flow_style=False, allow_unicode=True)

    yield temp_dir

    # 清理
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_env_vars():
    """模拟环境变量"""
    original_env = {}

    # 备份原始环境变量
    for key in ["ATLAS_ENV", "ATLAS_DEBUG", "ATLAS_LOG_LEVEL", "ATLAS_DATA_DIR", "ATLAS_LOG_DIR"]:
        if key in os.environ:
            original_env[key] = os.environ[key]
            del os.environ[key]

    # 设置测试环境变量
    test_env_vars = {
        "ATLAS_ENV": "test",
        "ATLAS_DEBUG": "true",
        "ATLAS_LOG_LEVEL": "DEBUG"
    }

    for key, value in test_env_vars.items():
        os.environ[key] = value

    yield test_env_vars

    # 恢复原始环境变量
    for key in os.environ:
        if key in original_env:
            os.environ[key] = original_env[key]
        elif key in test_env_vars:
            del os.environ[key]


@pytest.fixture
def sample_rss_content():
    """示例 RSS 内容"""
    base_url = TEST_CONFIG.get_full_url("example", "/")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test RSS Feed</title>
    <description>A test RSS feed for testing</description>
    <link>{base_url}</link>
    <language>en-us</language>
    <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    <item>
      <title>Test Article 1</title>
      <description>This is a test article description</description>
      <link>{base_url}article1</link>
      <pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate>
      <guid>{base_url}article1</guid>
    </item>
    <item>
      <title>Test Article 2</title>
      <description>Another test article description</description>
      <link>{base_url}article2</link>
      <pubDate>Mon, 01 Jan 2024 11:00:00 GMT</pubDate>
      <guid>{base_url}article2</guid>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def sample_html_content():
    """示例 HTML 内容"""
    base_url = TEST_CONFIG.get_full_url("example", "/")
    return f"""
<!DOCTYPE html>
<html>
<head>
    <title>Test HTML Page</title>
    <meta name="description" content="A test HTML page">
    <meta name="author" content="Test Author">
    <link rel="canonical" href="{base_url}">
</head>
<body>
    <header>
        <h1>Test Page Title</h1>
        <nav><a href="{base_url}">Home</a> | <a href="{base_url}/about">About</a></nav>
    </header>
    <main>
        <article class="post">
            <h2>Article Title</h2>
            <div class="content">
                <p>This is the article content with multiple paragraphs.</p>
                <p>Second paragraph with more content.</p>
                <p>Link to <a href="{base_url}/external">external site</a>.</p>
            </div>
            <time datetime="2024-01-01T10:00:00Z">January 1, 2024</time>
            <div class="author">By Test Author</div>
        </article>
    </main>
    <footer>
        <p>&copy; 2024 <a href="{base_url}">Example Company</a></p>
    </footer>
</body>
</html>
"""


@pytest.fixture
def mock_requests_response():
    """模拟 requests.Response 对象"""
    response = Mock()
    response.status_code = 200
    response.headers = {
        "content-type": "application/xml",
        "content-length": "1000"
    }
    response.text = TestDataGenerator.generate_rss_feed(3)
    response.content = TestDataGenerator.generate_rss_feed(3).encode('utf-8')
    return response


@pytest.fixture
def mock_config():
    """模拟配置对象"""
    from atlas.core.config import CollectionConfig, LLMConfig

    config = Mock()
    config.collection = Mock(spec=CollectionConfig)
    config.collection.default_user_agent = "Atlas/0.1.0 (Test)"
    config.collection.request_timeout = 30
    config.collection.max_concurrent_requests = 3
    config.collection.rate_limit_delay = 300
    config.collection.use_random_user_agent = False
    config.collection.rotate_user_agent = False

    config.llm = Mock(spec=LLMConfig)
    config.llm.provider = "local"
    config.llm.model = "qwen2.5:7b"
    config.llm.base_url = TEST_CONFIG.get_url("rss_feed")  # 使用测试URL代替硬编码

    return config


# 测试标记
def pytest_configure(config):
    """pytest 配置"""
    config.addinivalue_line(
        "markers", "unit: 单元测试"
    )
    config.addinivalue_line(
        "markers", "integration: 集成测试"
    )
    config.addinivalue_line(
        "markers", "slow: 慢速测试"
    )


# 测试收集器
def pytest_collection_modifyitems(config, items):
    """修改测试收集行为"""
    # 为集成测试添加标记
    for item in items:
        # 为异步测试添加标记
        if asyncio.iscoroutinefunction(item.function):
            item.add_marker(pytest.mark.asyncio)

        # 根据文件路径和测试名称判断测试类型
        if "integration" in item.nodeid or "e2e" in item.nodeid:
            item.add_marker(pytest.mark.integration)
        elif "test_" in item.nodeid and "unit" in item.nodeid:
            item.add_marker(pytest.mark.unit)
        elif "slow" in item.nodeid:
            item.add_marker(pytest.mark.slow)