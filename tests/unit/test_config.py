"""
配置管理模块单元测试
"""

import os
import tempfile
import pytest
import yaml
from pathlib import Path
from unittest.mock import patch, mock_open

from atlas.core.config import Config, DatabaseConfig, CollectionConfig, LLMConfig
from atlas.core.config import LocalEnvLoader


class TestDatabaseConfig:
    """数据库配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = DatabaseConfig()
        assert config.url == "sqlite:///data/atlas.db"
        assert config.pool_size == 5
        assert config.max_overflow == 10
        assert config.echo is False

    def test_env_prefix(self):
        """测试环境变量前缀"""
        with patch.dict(os.environ, {
            "ATLAS_DATABASE_URL": "postgresql://localhost/test",
            "ATLAS_DATABASE_POOL_SIZE": "10"
        }):
            config = DatabaseConfig()
            assert config.url == "postgresql://localhost/test"
            assert config.pool_size == 10


class TestCollectionConfig:
    """数据采集配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = CollectionConfig()
        assert "Atlas/0.1.0" in config.default_user_agent
        assert config.request_timeout == 30
        assert config.max_concurrent_requests == 3
        assert config.rate_limit_delay == 300
        assert config.use_random_user_agent is False
        assert config.rotate_user_agent is False

    def test_env_prefix(self):
        """测试环境变量前缀"""
        with patch.dict(os.environ, {
            "ATLAS_REQUEST_TIMEOUT": "60",
            "ATLAS_USE_RANDOM_USER_AGENT": "true"
        }):
            config = CollectionConfig()
            assert config.request_timeout == 60
            assert config.use_random_user_agent is True


class TestLLMConfig:
    """LLM 配置测试"""

    def test_default_values(self):
        """测试默认值"""
        config = LLMConfig()
        assert config.provider == "local"
        assert config.model == "qwen2.5:7b"
        assert config.base_url == "http://localhost:11434"
        assert config.timeout == 60

    def test_provider_validation(self):
        """测试提供商验证"""
        config = LLMConfig()
        assert config.provider == "local"  # 有效值

        with pytest.raises(ValueError):
            LLMConfig(provider="invalid")  # 无效值

    def test_env_prefix(self):
        """测试环境变量前缀"""
        with patch.dict(os.environ, {
            "ATLAS_LLM_PROVIDER": "openai",
            "ATLAS_LLM_MODEL": "gpt-4"
        }):
            config = LLMConfig()
            assert config.provider == "openai"
            assert config.model == "gpt-4"


class TestLocalEnvLoader:
    """本地环境变量加载器测试"""

    def test_detect_env_from_system(self):
        """测试从系统环境变量检测环境"""
        with patch.dict(os.environ, {"ATLAS_ENV": "production"}):
            loader = LocalEnvLoader(Path("/tmp"))
            assert loader.env_name == "production"

    def test_detect_env_from_file(self):
        """测试从文件检测环境"""
        env_content = "ATLAS_ENV=staging\nOTHER_VAR=value"

        with patch("builtins.open", mock_open(read_data=env_content)):
            with patch("pathlib.Path.exists", return_value=True):
                loader = LocalEnvLoader(Path("/tmp"))
                assert loader.env_name == "staging"

    def test_detect_env_default(self):
        """测试默认环境检测"""
        with patch("pathlib.Path.exists", return_value=False):
            loader = LocalEnvLoader(Path("/tmp"))
            assert loader.env_name == "development"

    def test_get_env_file_paths(self):
        """测试获取环境文件路径"""
        config_dir = Path("/tmp/config")
        loader = LocalEnvLoader(config_dir)

        paths = loader._get_env_file_paths()

        # 检查是否包含正确的文件路径
        path_strs = [str(p) for p in paths]
        assert any(".env" in path for path in path_strs)
        assert any("config" in path for path in path_strs)


class TestConfig:
    """配置管理器测试"""

    def test_init_with_defaults(self):
        """测试使用默认值初始化"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir)
            assert config.config_dir == Path(temp_dir)
            assert config.env == "development"
            assert config.debug is False

    def test_init_with_env_name(self):
        """测试指定环境名称初始化"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir, env_name="production")
            assert config.env_name == "production"
            assert config.env == "production"

    def test_load_config_files(self):
        """测试加载配置文件"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)

            # 创建配置文件
            config_data = {
                "env": "test",
                "debug": True,
                "log_level": "DEBUG"
            }
            config_file = config_dir / "config.yaml"
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f)

            # 创建环境特定配置
            env_data = {
                "env": "test",
                "test_setting": "test_value"
            }
            env_file = config_dir / "config.test.yaml"
            with open(env_file, 'w', encoding='utf-8') as f:
                yaml.dump(env_data, f)

            config = Config(config_dir, env_name="test")

            # 检查配置是否正确加载和合并
            assert config.env == "test"
            assert config.get("test_setting") == "test_value"
            assert config.get("debug") is True

    def test_get_set_config(self):
        """测试获取和设置配置值"""
        config = Config()

        # 设置嵌套配置
        config.set("nested.key1", "value1")
        config.set("nested.key2", "value2")

        # 获取配置
        assert config.get("nested.key1") == "value1"
        assert config.get("nested.key2") == "value2"
        assert config.get("nested.nonexistent") is None
        assert config.get("nested.nonexistent", "default") == "default"

    def test_ensure_directories(self):
        """测试确保目录存在"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir)

            # 检查目录是否创建
            assert config.data_dir.exists()
            assert config.log_dir.exists()
            assert (config.data_dir / "raw").exists()
            assert (config.data_dir / "processed").exists()

    def test_properties(self):
        """测试配置属性"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir)

            # 测试数据库配置
            assert isinstance(config.database, DatabaseConfig)

            # 测试采集配置
            assert isinstance(config.collection, CollectionConfig)

            # 测试 LLM 配置
            assert isinstance(config.llm, LLMConfig)

    def test_reload(self):
        """测试重新加载配置"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(temp_dir)
            original_env = config.env

            # 修改配置文件
            config_data = {"env": "reloaded", "debug": True}
            config_file = Path(temp_dir) / "config.yaml"
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f)

            # 重新加载
            config.reload()

            # 检查配置是否更新
            assert config.env == "reloaded"
            assert config.get("debug") is True


# 集成测试
@pytest.mark.integration
class TestConfigIntegration:
    """配置集成测试"""

    def test_env_file_loading_priority(self, test_config_dir):
        """测试环境文件加载优先级"""
        # 创建多个环境文件
        root_env_file = test_config_dir / ".env"
        config_env_file = test_config_dir / ".env"
        local_env_file = test_config_dir / ".env.local"

        # 根目录 .env
        with open(root_env_file, 'w') as f:
            f.write("ATLAS_ENV=from_root\n")

        # 配置目录 .env
        with open(config_env_file, 'w') as f:
            f.write("ATLAS_LOG_LEVEL=INFO\n")

        # 本地 .env.local (应该优先级最高)
        with open(local_env_file, 'w') as f:
            f.write("ATLAS_ENV=from_local\n")
            f.write("ATLAS_LOG_LEVEL=DEBUG\n")

        config = Config(test_config_dir, env_name="test")

        # 检查优先级：local > root > default
        assert config.env == "from_local"
        assert config.log_level == "DEBUG"