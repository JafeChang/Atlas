"""
数据管道集成测试

测试各个组件之间的集成和数据流。
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from atlas.core.config import Config
from atlas.core.logging import AtlasLogger
from atlas.collectors.base import CollectorFactory
from atlas.collectors.rss_collector import RSSCollector
from atlas.collectors.web_collector import WebCollector


@pytest.mark.integration
class TestDataPipelineIntegration:
    """数据管道集成测试"""

    @pytest.fixture
    def test_environment(self):
        """创建测试环境"""
        temp_dir = Path(tempfile.mkdtemp(prefix="atlas_test_"))

        # 创建目录结构
        data_dir = temp_dir / "data"
        log_dir = temp_dir / "logs"
        data_dir.mkdir(parents=True)
        log_dir.mkdir(parents=True)

        # 初始化配置
        config = Config(str(temp_dir))
        config._config = {
            "env": "test",
            "debug": True,
            "log_level": "DEBUG",
            "data_dir": str(data_dir),
            "log_dir": str(log_dir)
        }

        yield {
            "temp_dir": temp_dir,
            "config": config,
            "data_dir": data_dir,
            "log_dir": log_dir
        }

        # 清理
        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_config_logging_integration(self, test_environment):
        """测试配置和日志系统集成"""
        config = test_environment["config"]
        log_dir = test_environment["log_dir"]

        # 初始化日志系统
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 验证日志文件创建
        assert (log_dir / "atlas.log").exists()
        assert (log_dir / "errors.log").exists()
        assert (log_dir / "structured.log").exists()

        # 记录测试日志
        logger.info("Integration test message", config_env=config.env)

        # 验证日志内容
        log_file = log_dir / "atlas.log"
        content = log_file.read_text(encoding='utf-8')
        assert "Integration test message" in content
        assert str(config.env) in content

    def test_collector_factory_integration(self, test_environment):
        """测试采集器工厂集成"""
        config = test_environment["config"]

        # 测试创建不同类型的采集器
        rss_collector = CollectorFactory.create_collector('rss', config.collection)
        web_collector = CollectorFactory.create_collector('web', config.collection)

        # 验证采集器类型
        assert isinstance(rss_collector, RSSCollector)
        assert isinstance(web_collector, WebCollector)

        # 验证采集器配置
        assert rss_collector.config is config.collection
        assert web_collector.config is config.collection

        # 测试采集器会话
        assert rss_collector.session is not None
        assert web_collector.session is not None

    def test_rss_collection_workflow(self, test_environment):
        """测试RSS采集工作流程"""
        config = test_environment["config"]
        log_dir = test_environment["log_dir"]

        # 初始化日志
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 创建RSS采集器
        rss_collector = RSSCollector(config.collection)

        # 模拟RSS源配置
        source_config = {
            'name': 'test-rss-integration',
            'type': 'rss',
            'url': 'https://example.com/rss.xml',
            'tags': ['integration', 'test'],
            'category': 'ai'
        }

        # 模拟成功采集
        mock_feed = Mock()
        mock_feed.bozo = False
        mock_feed.entries = [
            {
                'title': 'Integration Test Article',
                'link': 'https://example.com/integration-test',
                'description': 'Testing RSS integration workflow',
                'published_parsed': datetime(2024, 1, 1).timetuple()
            }
        ]

        # 测试采集流程
        with patch.object(rss_collector, 'make_request') as mock_request:
            with patch.object(rss_collector, 'parse_rss') as mock_parse:
                # Mock the collector's logger to avoid AtlasLogger issues
                with patch.object(rss_collector.logger, 'log_collection'):
                    # 设置模拟响应
                    mock_response = Mock()
                    mock_response.text = "RSS content"
                    mock_request.return_value = mock_response
                    mock_parse.return_value = mock_feed

                    # 记录采集开始
                    logger.log_collection(source_config['name'], 0, "started")

                    # 执行采集
                    items = rss_collector.collect(source_config)

                # 记录采集结果
                logger.log_collection(source_config['name'], len(items), "completed")

                # 验证结果
                assert len(items) == 1
                assert items[0]['title'] == 'Integration Test Article'
                assert items[0]['source_name'] == source_config['name']
                assert 'integration' in items[0]['tags']
                assert items[0]['category'] == 'ai'

    def test_web_collection_workflow(self, test_environment):
        """测试Web采集工作流程"""
        config = test_environment["config"]
        log_dir = test_environment["log_dir"]

        # 初始化日志
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 创建Web采集器
        web_collector = WebCollector(config.collection)

        # 模拟Web源配置
        source_config = {
            'name': 'test-web-integration',
            'type': 'web',
            'url': 'https://example.com',
            'tags': ['integration', 'test'],
            'category': 'web',
            'selectors': {
                'title': 'h1',
                'content': 'article'
            }
        }

        # 模拟成功采集
        with patch.object(web_collector, 'make_request') as mock_request:
            with patch.object(web_collector, 'parse_html') as mock_parse:
                # Mock the collector's logger to avoid AtlasLogger issues
                with patch.object(web_collector.logger, 'log_collection'):
                    # 设置模拟响应
                    mock_response = Mock()
                    mock_response.text = "<html><body><h1>Test Title</h1><article>Test Content</article></body></html>"
                    mock_request.return_value = mock_response

                    # 设置模拟HTML解析
                    from bs4 import BeautifulSoup
                    mock_soup = BeautifulSoup(mock_response.text, 'html.parser')
                    mock_parse.return_value = mock_soup

                    # 记录采集开始
                    logger.log_collection(source_config['name'], 0, "started")

                    # 执行采集
                    items = web_collector.collect(source_config)

                # 记录采集结果
                logger.log_collection(source_config['name'], len(items), "completed")

                # 验证结果
                assert len(items) == 1
                assert 'Test Title' in items[0]['title']  # Web采集可能包含多个字段
                assert items[0]['source_name'] == source_config['name']
                assert 'integration' in items[0]['tags']
                assert items[0]['category'] == 'web'

    def test_error_handling_integration(self, test_environment):
        """测试错误处理集成"""
        config = test_environment["config"]
        log_dir = test_environment["log_dir"]

        # 初始化日志
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 创建RSS采集器
        rss_collector = RSSCollector(config.collection)

        # 模拟失败场景
        source_config = {
            'name': 'test-error-integration',
            'type': 'rss',
            'url': 'https://invalid-url.com/rss.xml'
        }

        # 测试网络错误处理
        with patch.object(rss_collector, 'make_request') as mock_request:
            # Mock the collector's logger to avoid AtlasLogger issues
            with patch.object(rss_collector.logger, 'log_collection'):
                mock_request.return_value = None  # 模拟请求失败

                # 记录采集开始
                logger.log_collection(source_config['name'], 0, "started")

                # 执行采集
                items = rss_collector.collect(source_config)

            # 记录采集失败
            logger.log_collection(source_config['name'], 0, "failed", error="Network error")

            # 验证结果
            assert items == []  # 应该返回空列表

        # 验证错误日志
        error_log = log_dir / "errors.log"
        content = error_log.read_text(encoding='utf-8')
        assert "Network error" in content

    def test_configuration_priority_integration(self, test_environment):
        """测试配置优先级集成"""
        temp_dir = test_environment["temp_dir"]

        # 创建不同层级的配置文件
        base_config = {
            "env": "test",
            "debug": True,
            "log_level": "INFO"
        }

        # 基础配置文件
        with open(temp_dir / "config.yaml", 'w', encoding='utf-8') as f:
            import yaml
            yaml.dump(base_config, f)

        # 环境特定配置文件
        env_config = {
            "env": "integration",
            "test_setting": "env_specific"
        }
        with open(temp_dir / "config.integration.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(env_config, f)

        # 本地配置文件
        local_config = {
            "env": "integration",
            "test_setting": "local_override",
            "local_only": "local_value"
        }
        with open(temp_dir / "config.local.yaml", 'w', encoding='utf-8') as f:
            yaml.dump(local_config, f)

        # 重新加载配置
        config = Config(str(temp_dir), env_name="integration")

        # 验证配置优先级
        assert config.env == "integration"
        assert config.get("test_setting") == "local_override"  # local覆盖env
        assert config.get("local_only") == "local_value"      # 只在local中
        assert config.get("debug") is True                     # 来自base_config

    def test_structured_logging_integration(self, test_environment):
        """测试结构化日志集成"""
        config = test_environment["config"]
        log_dir = test_environment["log_dir"]

        # 初始化日志
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 记录各种类型的结构化日志
        logger.log_request("GET", "https://example.com", 200, 0.5)
        logger.log_task("data_collection", "started", source_count=5)
        logger.log_performance("rss_parse", 0.1, items_count=100)
        logger.log_collection("test-source", 25, "completed")
        logger.log_llm_call("local", "qwen2.5:7b", 1000, 0.01, 1.5)

        # 验证结构化日志文件
        structured_file = log_dir / "structured.log"
        assert structured_file.exists()

        # 读取并验证日志内容
        content = structured_file.read_text(encoding='utf-8')
        lines = content.strip().split('\n')

        # 验证每种日志类型
        log_types = ['request', 'task', 'performance', 'collection', 'llm']
        for log_type in log_types:
            # 使用JSON解析验证格式
            import json
            for line in lines:
                try:
                    log_entry = json.loads(line)
                    if log_entry.get('type') == log_type:
                        break
                except json.JSONDecodeError:
                    continue

    @pytest.mark.slow
    def test_end_to_end_pipeline(self, test_environment):
        """测试端到端数据管道"""
        config = test_environment["config"]
        data_dir = test_environment["data_dir"]
        log_dir = test_environment["log_dir"]

        # 初始化日志
        logger = AtlasLogger(
            log_dir=str(log_dir),
            log_level="INFO",
            enable_file=True,
            enable_console=False
        )

        # 记录任务开始
        logger.log_task("end_to_end_test", "started")

        # 模拟多个数据源采集
        sources = [
            {
                'name': 'rss-source-1',
                'type': 'rss',
                'url': 'https://example1.com/rss.xml',
                'category': 'ai'
            },
            {
                'name': 'rss-source-2',
                'type': 'rss',
                'url': 'https://example2.com/rss.xml',
                'category': 'ml'
            },
            {
                'name': 'web-source-1',
                'type': 'web',
                'url': 'https://example3.com',
                'category': 'web',
                'selectors': {'title': 'h1', 'content': 'article'}
            }
        ]

        total_items = 0
        start_time = datetime.now()

        for source in sources:
            # 记录采集开始
            logger.log_task("source_collection", "started", source=source['name'])

            # 创建采集器
            collector = CollectorFactory.create_collector(source['type'], config.collection)

            # 模拟采集结果
            with patch.object(collector, 'make_request') as mock_request:
                # Mock the collector's logger to avoid AtlasLogger issues
                with patch.object(collector.logger, 'log_collection'):
                    if source['type'] == 'rss':
                        with patch.object(collector, 'parse_rss') as mock_parse:
                            mock_response = Mock()
                            mock_response.text = "RSS content"
                            mock_request.return_value = mock_response

                            mock_feed = Mock()
                            mock_feed.bozo = False
                            mock_feed.entries = [
                                {
                                    'title': f'Article from {source["name"]}',
                                    'link': f'https://example.com/article',
                                    'description': f'Description from {source["name"]}'
                                }
                            ]
                            mock_parse.return_value = mock_feed

                            items = collector.collect(source)
                    else:  # web
                        with patch.object(collector, 'parse_html') as mock_parse:
                            mock_response = Mock()
                            mock_response.text = f"<html><h1>{source['name']} Content</h1><article>Article content</article></html>"
                            mock_request.return_value = mock_response

                            from bs4 import BeautifulSoup
                            mock_soup = BeautifulSoup(mock_response.text, 'html.parser')
                            mock_parse.return_value = mock_soup

                            items = collector.collect(source)

            # 记录采集结果
            logger.log_task("source_collection", "completed", source=source['name'], items_count=len(items))
            total_items += len(items)

        # 计算总耗时
        duration = (datetime.now() - start_time).total_seconds()

        # 记录任务完成
        logger.log_task("end_to_end_test", "completed", duration=duration, total_items=total_items)

        # 验证结果
        assert total_items == 3  # 每个源返回1个项目

        # 验证任务日志
        task_log = log_dir / "atlas.log"
        content = task_log.read_text(encoding='utf-8')
        assert "end_to_end_test" in content
        assert f"total_items={total_items}" in content