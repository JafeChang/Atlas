"""
测试工具函数

提供测试中常用的工具函数和辅助类。
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Generator, Optional
from unittest.mock import Mock, patch
import sqlite3

import pytest
import yaml
from httpx import Response as HttpxResponse

# 导入测试配置
from .test_config import TEST_CONFIG


class TempFileManager:
    """临时文件管理器"""

    def __init__(self):
        self.temp_files = []
        self.temp_dirs = []

    def create_temp_file(self, content: str, suffix: str = ".tmp") -> Path:
        """创建临时文件"""
        with tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False, encoding='utf-8') as f:
            f.write(content)
            temp_path = Path(f.name)
            self.temp_files.append(temp_path)
            return temp_path

    def create_temp_dir(self) -> Path:
        """创建临时目录"""
        temp_dir = Path(tempfile.mkdtemp())
        self.temp_dirs.append(temp_dir)
        return temp_dir

    def cleanup(self):
        """清理所有临时文件和目录"""
        for temp_file in self.temp_files:
            try:
                temp_file.unlink()
            except (FileNotFoundError, PermissionError):
                pass

        for temp_dir in self.temp_dirs:
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except (FileNotFoundError, PermissionError):
                pass

        self.temp_files.clear()
        self.temp_dirs.clear()


class MockHttpServer:
    """模拟 HTTP 服务器"""

    def __init__(self):
        self.responses = {}
        self.request_log = []

    def add_response(self, url: str, response_data: Any, status_code: int = 200,
                    headers: Optional[Dict] = None):
        """添加模拟响应"""
        self.responses[url] = {
            'data': response_data,
            'status_code': status_code,
            'headers': headers or {}
        }

    def get_response(self, url: str) -> Optional[Dict]:
        """获取模拟响应"""
        return self.responses.get(url)

    def log_request(self, url: str, method: str = "GET", data: Any = None):
        """记录请求"""
        self.request_log.append({
            'url': url,
            'method': method,
            'data': data,
            'timestamp': time.time()
        })

    def clear_log(self):
        """清空请求日志"""
        self.request_log.clear()


class AsyncTestCase:
    """异步测试用例基类"""

    @pytest.fixture(autouse=True)
    def setup_event_loop(self):
        """设置事件循环"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        yield
        loop.close()

    async def run_async(self, coro):
        """运行异步协程"""
        return await coro


def create_sample_config(config_dir: Path, env: str = "test") -> Dict:
    """创建示例配置文件"""
    config_data = {
        "env": env,
        "debug": True,
        "log_level": "DEBUG",
        "data_dir": str(config_dir / "data"),
        "log_dir": str(config_dir / "logs"),
        "database": {
            "url": f"sqlite:///{config_dir}/atlas_test.db",
            "pool_size": 5,
            "max_overflow": 10
        },
        "collection": {
            "default_user_agent": "Atlas/0.1.0 (Test)",
            "request_timeout": 30,
            "max_concurrent_requests": 3,
            "rate_limit_delay": 300
        },
        "llm": {
            "provider": "local",
            "model": "qwen2.5:7b",
            "base_url": "http://localhost:11434",
            "api_key": None
        }
    }

    config_file = config_dir / "config.yaml"
    with open(config_file, 'w', encoding='utf-8') as f:
        yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

    return config_data


def create_sample_sources(sources_dir: Path) -> Dict:
    """创建示例数据源配置"""
    sources_data = {
        "sources": [
            {
                "name": "tech-rss",
                "type": "rss",
                "url": TEST_CONFIG.get_url("tech_rss"),
                "interval": 3600,
                "enabled": True,
                "tags": ["tech", "programming"],
                "category": "technology"
            },
            {
                "name": "news-rss",
                "type": "rss",
                "url": TEST_CONFIG.get_url("news_rss"),
                "interval": 1800,
                "enabled": False,
                "tags": ["news", "general"],
                "category": "news"
            },
            {
                "name": "blog-site",
                "type": "web",
                "url": TEST_CONFIG.get_url("blog_site"),
                "interval": 7200,
                "enabled": True,
                "tags": ["blog", "personal"],
                "category": "blog",
                "selectors": {
                    "title": "h1.title",
                    "content": "div.content",
                    "date": "time.date",
                    "author": "span.author"
                }
            }
        ]
    }

    sources_file = sources_dir / "sources.yaml"
    with open(sources_file, 'w', encoding='utf-8') as f:
        yaml.dump(sources_data, f, default_flow_style=False, allow_unicode=True)

    return sources_data


def create_test_database(db_path: Path) -> sqlite3.Connection:
    """创建测试数据库"""
    conn = sqlite3.connect(str(db_path))

    # 创建表结构
    with open("src/atlas/core/database.py", 'r', encoding='utf-8') as f:
        # 这里应该从数据库模块获取表结构定义
        pass

    # 插入测试数据
    test_data = [
        (1, "Test Document 1", "Test content 1", "tech", "2024-01-01 12:00:00"),
        (2, "Test Document 2", "Test content 2", "news", "2024-01-02 12:00:00"),
        (3, "Test Document 3", "Test content 3", "blog", "2024-01-03 12:00:00"),
    ]

    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT,
            category TEXT,
            created_at TIMESTAMP
        )
    """)

    conn.executemany(
        "INSERT INTO documents (id, title, content, category, created_at) VALUES (?, ?, ?, ?, ?)",
        test_data
    )

    conn.commit()
    return conn


def assert_valid_document(document: Dict) -> None:
    """验证文档结构的有效性"""
    required_fields = ['title', 'content', 'source', 'created_at']

    for field in required_fields:
        assert field in document, f"Missing required field: {field}"
        assert document[field] is not None, f"Field {field} cannot be None"

    if 'title' in document:
        assert isinstance(document['title'], str), "Title must be a string"
        assert len(document['title'].strip()) > 0, "Title cannot be empty"

    if 'content' in document:
        assert isinstance(document['content'], str), "Content must be a string"
        assert len(document['content'].strip()) > 0, "Content cannot be empty"


def create_mock_response(status_code: int = 200, content: str = "",
                        headers: Optional[Dict] = None) -> Mock:
    """创建模拟 HTTP 响应"""
    response = Mock()
    response.status_code = status_code
    response.text = content
    response.content = content.encode('utf-8')
    response.headers = headers or {}
    response.json.return_value = json.loads(content) if content and content.startswith('{') else {}
    return response


def create_mock_httpx_response(status_code: int = 200, content: str = "",
                              headers: Optional[Dict] = None) -> HttpxResponse:
    """创建模拟 httpx 响应"""
    return HttpxResponse(
        status_code=status_code,
        content=content.encode('utf-8'),
        headers=headers or {}
    )


class PerformanceTimer:
    """性能计时器"""

    def __init__(self):
        self.start_time = None
        self.end_time = None

    def start(self):
        """开始计时"""
        self.start_time = time.perf_counter()

    def stop(self) -> float:
        """停止计时并返回耗时"""
        self.end_time = time.perf_counter()
        return self.elapsed()

    def elapsed(self) -> float:
        """获取耗时"""
        if self.start_time is None:
            return 0.0
        return (self.end_time or time.perf_counter()) - self.start_time

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def assert_performance(func, max_time: float):
    """断言函数执行时间不超过最大时间"""
    with PerformanceTimer() as timer:
        result = func()

    assert timer.elapsed() <= max_time, f"Function took {timer.elapsed():.2f}s, expected <= {max_time}s"
    return result


def skip_if_no_network():
    """如果没有网络连接则跳过测试"""
    try:
        import socket
        socket.create_connection(("8.8.8.8", 53), timeout=3)
        return False
    except (socket.timeout, socket.error):
        return True


def skip_if_no_docker():
    """如果没有 Docker 则跳过测试"""
    try:
        import subprocess
        result = subprocess.run(['docker', '--version'],
                              capture_output=True, timeout=5)
        return result.returncode != 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True


class TestDataGenerator:
    """测试数据生成器"""

    @staticmethod
    def generate_documents(count: int = 10) -> Generator[Dict, None, None]:
        """生成测试文档"""
        for i in range(count):
            yield {
                'id': i + 1,
                'title': f'Test Document {i + 1}',
                'content': f'This is the content of test document {i + 1}',
                'source': f'test-source-{i + 1}',
                'category': 'test',
                'tags': [f'tag-{j}' for j in range(1, 4)],
                'created_at': f'2024-01-0{(i % 9) + 1} 12:{(i % 60):02d}:00'
            }

    @staticmethod
    def generate_rss_feed(count: int = 5) -> str:
        """生成 RSS 订阅内容"""
        base_url = TEST_CONFIG.get_full_url("example", "/")
        items = []
        for i in range(count):
            items.append(f"""
            <item>
              <title>Test Article {i + 1}</title>
              <description>This is test article {i + 1} description</description>
              <link>{base_url}article{i + 1}</link>
              <pubDate>Mon, 0{i + 1} Jan 2024 12:00:00 GMT</pubDate>
              <guid>{base_url}article{i + 1}</guid>
            </item>""")

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test RSS Feed</title>
    <description>A test RSS feed for testing</description>
    <link>{base_url}</link>
    <language>en-us</language>
    <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    {''.join(items)}
  </channel>
</rss>"""

    @staticmethod
    def generate_html_content(title: str = "Test Page") -> str:
        """生成 HTML 内容"""
        return f"""
<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta name="description" content="A test HTML page">
    <meta name="author" content="Test Author">
</head>
<body>
    <header>
        <h1>{title}</h1>
    </header>
    <main>
        <article class="post">
            <h2>Article Title</h2>
            <div class="content">
                <p>This is the article content with multiple paragraphs.</p>
                <p>Second paragraph with more content.</p>
            </div>
            <time datetime="2024-01-01T10:00:00Z">January 1, 2024</time>
            <div class="author">By Test Author</div>
        </article>
    </main>
</body>
</html>"""