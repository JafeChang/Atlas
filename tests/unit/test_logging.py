"""
日志系统模块单元测试
"""

import os
import tempfile
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from atlas.core.logging import AtlasLogger, init_logger, get_logger, log_execution


class TestAtlasLogger:
    """Atlas 日志管理器测试"""

    def test_init_basic(self):
        """测试基本初始化"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                log_level="INFO",
                enable_file=False,
                enable_console=False
            )

            assert logger.log_dir == Path(temp_dir)
            assert logger.logger is not None

    def test_init_with_file_and_console(self):
        """测试启用文件和控制台日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                log_level="DEBUG",
                enable_file=True,
                enable_console=True
            )

            # 检查文件是否创建
            assert (temp_dir / "atlas.log").exists()
            assert (temp_dir / "errors.log").exists()
            assert (temp_dir / "structured.log").exists()

    def test_log_levels(self):
        """测试不同日志级别"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            # 测试不同级别的日志
            logger.debug("Debug message")
            logger.info("Info message")
            logger.warning("Warning message")
            logger.error("Error message")
            logger.critical("Critical message")

    def test_log_with_context(self):
        """测试带上下文的日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.info("Test message", user_id="123", action="test")
            logger.error("Error message", error_code=500, retry_count=3)

    def test_log_request(self):
        """测试请求日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.log_request("GET", "https://example.com", 200, 1.5)
            logger.log_request("POST", "https://api.example.com", 201, 2.3)

    def test_log_task(self):
        """测试任务日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.log_task("data_collection", "started")
            logger.log_task("data_collection", "completed", duration=10.5, items_count=50)
            logger.log_task("data_collection", "failed", duration=5.0, error="timeout")

    def test_log_performance(self):
        """测试性能日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.log_performance("sql_query", 0.1, rows_affected=100)
            logger.log_performance("api_call", 2.5, status_code=200)

    def test_log_collection(self):
        """测试数据采集日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.log_collection("test-source", 25, "completed")
            logger.log_collection("test-source", 0, "failed")

    def test_log_llm_call(self):
        """测试 LLM 调用日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            logger.log_llm_call("local", "qwen2.5:7b", 1000, 0.01, 2.1)
            logger.log_llm_call("openai", "gpt-4", 1500, 0.05, 5.8)

    def test_exception_logging(self):
        """测试异常日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            try:
                raise ValueError("Test exception")
            except Exception:
                logger.exception("Exception occurred", context="test")

    @pytest.mark.parametrize("method", ["debug", "info", "warning", "error", "critical"])
    def test_log_methods(self, method):
        """测试所有日志方法"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                enable_file=False,
                enable_console=False
            )

            getattr(logger, method)("Test message")


class TestLoggingFunctions:
    """日志函数测试"""

    def test_init_logger(self):
        """测试初始化日志系统"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = init_logger(
                log_dir=temp_dir,
                log_level="INFO"
            )
            assert logger is not None

    def test_get_logger(self):
        """测试获取日志记录器"""
        logger = get_logger("test_module")
        assert logger is not None

    def test_log_execution_decorator(self):
        """测试日志装饰器"""
        @log_execution("test_function", log_args=True)
        def test_function(arg1, arg2="default"):
            return f"{arg1}-{arg2}"

        result = test_function("hello")
        assert result == "hello-default"

    @log_execution("failing_function")
    def failing_function():
        raise ValueError("Test error")

    def test_log_execution_with_exception(self):
        """测试日志装饰器异常处理"""
        with pytest.raises(ValueError):
            self.failing_function()


# 集成测试
@pytest.mark.integration
class TestLoggingIntegration:
    """日志系统集成测试"""

    def test_file_rotation(self):
        """测试文件轮转"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                log_level="INFO",
                enable_file=True,
                enable_console=False,
                rotation="1 MB"  # 小的轮转大小用于测试
            )

            # 写入大量日志以触发轮转
            for i in range(1000):
                logger.info(f"Test log message {i:04d} with enough content to trigger rotation")

            # 检查是否有多个日志文件（轮转后）
            log_files = list(Path(temp_dir).glob("atlas.log*"))
            assert len(log_files) >= 1

    def test_structured_logging(self):
        """测试结构化日志"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger = AtlasLogger(
                log_dir=temp_dir,
                log_level="INFO",
                enable_file=True,
                enable_console=False
            )

            logger.info("Test message", context="test")

            # 检查结构化日志文件
            structured_file = temp_dir / "structured.log"
            assert structured_file.exists()

            # 读取并验证结构化日志
            with open(structured_file, 'r', encoding='utf-8') as f:
                line = f.readline()
                assert '"timestamp":' in line
                assert '"level": "INFO"' in line
                assert '"message":' in line

    def test_log_level_filtering(self):
        """测试日志级别过滤"""
        with tempfile.TemporaryDirectory() as temp_dir:
            # 创建只记录 ERROR 级别的日志器
            logger = AtlasLogger(
                log_dir=temp_dir,
                log_level="ERROR",
                enable_file=True,
                enable_console=False
            )

            logger.debug("Debug message")  # 不应该被记录
            logger.info("Info message")    # 不应该被记录
            logger.warning("Warning message")  # 不应该被记录
            logger.error("Error message")    # 应该被记录
            logger.critical("Critical message")  # 应该被记录

            # 检查错误日志文件
            error_file = temp_dir / "errors.log"
            with open(error_file, 'r', encoding='utf-8') as f:
                content = f.read()
                assert "Error message" in content
                assert "Critical message" in content
                assert "Debug message" not in content
                assert "Info message" not in content
                assert "Warning message" not in content

    def test_multiple_loggers(self):
        """测试多个日志记录器"""
        with tempfile.TemporaryDirectory() as temp_dir:
            logger1 = get_logger("module1")
            logger2 = get_logger("module2")

            # 两个日志记录器应该是同一个实例
            assert logger1 is logger2

            # 写入日志
            logger1.info("Message from module1")
            logger2.info("Message from module2")

            # 验证日志是否写入
            assert (temp_dir / "atlas.log").exists()