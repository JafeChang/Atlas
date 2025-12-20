"""
基础同步集成测试

测试已经确认工作的组件集成，使用同步方式避免异步配置问题。
"""

import pytest
import tempfile
from pathlib import Path

from atlas.core.config import get_config


class TestBasicSync:
    """基础同步集成测试类"""

    @pytest.fixture
    def test_environment(self):
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

    def test_config_system(self, test_environment):
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

    def test_file_operations(self, test_environment):
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

    def test_path_operations(self, test_environment):
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

    def test_json_operations(self, test_environment):
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