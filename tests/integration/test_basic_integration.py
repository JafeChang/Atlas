"""
基础集成测试

测试已经确认工作的组件集成。
"""

import pytest
import asyncio
import tempfile
from pathlib import Path

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import FileStorageManager
from atlas.core.logging import get_logger

logger = get_logger(__name__)

pytest_plugins = ["tests.utils"]


class TestBasicIntegration:
    """基础集成测试类"""

    @pytest.fixture(scope="function")
    async def test_environment(self):
        """创建测试环境"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            data_dir = Path(temp_dir) / "data"
            log_dir = Path(temp_dir) / "logs"

            for directory in [config_dir, data_dir, log_dir]:
                directory.mkdir(parents=True, exist_ok=True)

            config = get_config(config_dir=config_dir)
            config._config_data["data_dir"] = str(data_dir)
            config._config_data["log_dir"] = str(log_dir)

            yield {
                "config": config,
                "config_dir": config_dir,
                "data_dir": data_dir,
                "log_dir": log_dir
            }

    @pytest.fixture(scope="function")
    async def basic_components(self, test_environment):
        """初始化基础组件"""
        config = test_environment["config"]
        data_dir = test_environment["data_dir"]

        # 数据库管理器
        db_manager = AtlasDatabase(data_dir / "test.db")

        # 存储管理器
        storage_manager = FileStorageManager(data_dir)

        yield {
            "db_manager": db_manager,
            "storage_manager": storage_manager
        }

        # 清理资源
        db_manager.close()

    @pytest.mark.asyncio
    async def test_database_operations(self, basic_components):
        """测试数据库操作"""
        db_manager = basic_components["db_manager"]

        # 测试基本查询
        result = db_manager.execute_query("SELECT 1 as test")
        assert len(result) == 1
        assert result[0]["test"] == 1

        # 测试表存在
        tables = ["raw_documents", "processed_documents", "data_sources"]
        for table in tables:
            table_count = db_manager.get_table_count(table)
            assert table_count >= 0

        # 测试数据库统计
        stats = db_manager.get_database_stats()
        assert "database_size_mb" in stats

        print("✓ 数据库操作测试通过")

    @pytest.mark.asyncio
    async def test_storage_operations(self, basic_components):
        """测试存储操作"""
        storage_manager = basic_components["storage_manager"]

        # 测试目录创建
        test_file = storage_manager.data_dir / "test.json"
        test_content = {"test": "data", "timestamp": "2025-01-20"}

        # 测试文件写入
        await storage_manager.save_json_file(test_file, test_content)

        # 测试文件读取
        loaded_content = await storage_manager.load_json_file(test_file)
        assert loaded_content == test_content

        # 测试文件存在检查
        assert await storage_manager.file_exists(test_file)

        # 清理测试文件
        test_file.unlink()

        print("✓ 存储操作测试通过")

    @pytest.mark.asyncio
    async def test_config_system(self, test_environment):
        """测试配置系统"""
        config = test_environment["config"]

        # 测试配置属性
        assert config.data_dir.exists()
        assert config.log_dir.exists()
        assert config.env is not None
        assert config.debug is not None

        # 测试配置获取
        database_config = config.database
        assert database_config.url is not None

        collection_config = config.collection
        assert collection_config.default_user_agent is not None

        print("✓ 配置系统测试通过")

    @pytest.mark.asyncio
    async def test_system_logging(self, test_environment):
        """测试系统日志"""
        config = test_environment["config"]
        logger = get_logger(__name__)

        # 测试日志级别
        assert config.log_level is not None

        # 测试日志目录
        assert config.log_dir.exists()

        # 测试日志记录
        logger.info("测试日志记录")
        logger.warning("测试警告记录")

        print("✓ 系统日志测试通过")

    @pytest.mark.asyncio
    async def test_file_operations(self, test_environment):
        """测试文件操作"""
        data_dir = test_environment["data_dir"]

        # 创建测试文件
        test_file = data_dir / "integration_test.txt"
        test_content = "Atlas Integration Test Content"

        # 写入文件
        test_file.write_text(test_content, encoding='utf-8')

        # 读取文件
        read_content = test_file.read_text(encoding='utf-8')
        assert read_content == test_content

        # 删除文件
        test_file.unlink()
        assert not test_file.exists()

        print("✓ 文件操作测试通过")

    @pytest.mark.asyncio
    async def test_path_operations(self, test_environment):
        """测试路径操作"""
        data_dir = test_environment["data_dir"]

        # 测试路径创建
        test_dir = data_dir / "test_integration"
        test_dir.mkdir(parents=True, exist_ok=True)

        # 测试路径存在性
        assert test_dir.exists()
        assert test_dir.is_dir()

        # 测试路径清理
        test_dir.rmdir()
        assert not test_dir.exists()

        print("✓ 路径操作测试通过")

    @pytest.mark.asyncio
    async def test_json_operations(self, test_environment):
        """测试JSON操作"""
        data_dir = test_environment["data_dir"]

        test_data = {
            "name": "Atlas Test",
            "version": "0.1.0",
            "timestamp": "2025-01-20T13:00:00Z",
            "components": ["database", "storage", "config"]
        }

        # 测试JSON文件
        json_file = data_dir / "test_config.json"

        # 写入JSON
        import json
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(test_data, f, indent=2)

        # 读取JSON
        with open(json_file, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)

        assert loaded_data == test_data

        # 清理
        json_file.unlink()

        print("✓ JSON操作测试通过")


if __name__ == "__main__":
    pytest.main([__file__])