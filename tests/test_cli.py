"""
CLI 模块测试

测试命令行接口的各项功能。
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from click.testing import CliRunner

from atlas.cli import main
from tests.utils import TempFileManager, create_sample_config


@pytest.fixture
def runner():
    """Click CLI 测试运行器"""
    return CliRunner()


@pytest.fixture
def temp_config_dir():
    """临时配置目录"""
    with TempFileManager() as temp_mgr:
        config_dir = temp_mgr.create_temp_dir()
        create_sample_config(config_dir)
        yield config_dir


class TestCLI:
    """CLI 测试类"""

    def test_main_help(self, runner):
        """测试主命令帮助"""
        result = runner.invoke(main, ['--help'])
        assert result.exit_code == 0
        assert 'Atlas 个人信息聚合系统命令行工具' in result.output
        assert 'check' in result.output
        assert 'init' in result.output
        assert 'status' in result.output

    def test_version(self, runner):
        """测试版本信息"""
        result = runner.invoke(main, ['--version'])
        assert result.exit_code == 0
        assert 'Atlas' in result.output

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_check_command_success(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试系统检查命令 - 成功情况"""
        # 模拟配置
        mock_config = Mock()
        mock_config.config_dir_path = temp_config_dir
        mock_config.data_dir = temp_config_dir / "data"
        mock_config.log_dir = temp_config_dir / "logs"
        mock_config.ensure_directories.return_value = None

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'check'])
        assert result.exit_code == 0
        assert '检查 Atlas 系统状态' in result.output

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_check_command_with_errors(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试系统检查命令 - 有错误情况"""
        # 模拟配置错误
        mock_config = Mock()
        mock_config.config_dir_path = temp_config_dir / "nonexistent"
        mock_config.data_dir = temp_config_dir / "nonexistent"
        mock_config.log_dir = temp_config_dir / "nonexistent"
        mock_config.ensure_directories.side_effect = Exception("Directory creation failed")

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'check'])
        assert result.exit_code == 1

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_init_command(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试系统初始化命令"""
        mock_config = Mock()
        mock_config.config_dir_path = temp_config_dir
        mock_config.data_dir = temp_config_dir / "data"
        mock_config.log_dir = temp_config_dir / "logs"
        mock_config.ensure_directories.return_value = None

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        with patch('atlas.cli.init_database') as mock_init_db:
            mock_init_db.return_value = None

            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'init'])
            assert result.exit_code == 0
            assert '初始化 Atlas 系统' in result.output

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_status_command(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试状态显示命令"""
        mock_config = Mock()
        mock_config.env = "test"
        mock_config.debug = True
        mock_config.log_level = "DEBUG"
        mock_config.data_dir = temp_config_dir / "data"
        mock_config.log_dir = temp_config_dir / "logs"
        mock_config.config_dir_path = temp_config_dir
        mock_config.database.url = "sqlite:///test.db"
        mock_config.database.pool_size = 5
        mock_config.llm.provider = "local"
        mock_config.llm.model = "qwen2.5:7b"
        mock_config.sources = {"sources": [{"enabled": True}, {"enabled": False}]}

        mock_env_info = {
            'current_env': 'test',
            'loaded_files': ['config.yaml', 'sources.yaml']
        }
        mock_config.get_env_info.return_value = mock_env_info

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'status'])
        assert result.exit_code == 0
        assert 'Atlas 系统状态' in result.output

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_logs_command(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试日志查看命令"""
        mock_config = Mock()
        mock_config.log_dir = temp_config_dir
        mock_config.config_dir_path = temp_config_dir

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        # 创建日志文件
        log_file = temp_config_dir / "atlas.log"
        log_file.write_text("2024-01-01 12:00:00 INFO Test log message\n"
                           "2024-01-01 12:01:00 ERROR Test error message\n")

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'logs'])
        assert result.exit_code == 0
        assert '日志文件信息' in result.output

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_config_show_command(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试配置显示命令"""
        mock_config = Mock()
        mock_config.env = "test"
        mock_config.debug = True
        mock_config.log_level = "DEBUG"
        mock_config.data_dir = temp_config_dir / "data"
        mock_config.log_dir = temp_config_dir / "logs"

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'config', 'show'])
        assert result.exit_code == 0

    @patch('atlas.cli.get_config')
    @patch('atlas.cli.init_logger')
    def test_config_validate_command(self, mock_init_logger, mock_get_config, runner, temp_config_dir):
        """测试配置验证命令"""
        mock_config = Mock()
        mock_config.config_dir_path = temp_config_dir

        mock_get_config.return_value = mock_config
        mock_logger = Mock()
        mock_init_logger.return_value = mock_logger

        result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'config', 'validate'])
        assert result.exit_code == 0
        assert '验证配置文件' in result.output


class TestCLIIntegration:
    """CLI 集成测试"""

    def test_full_workflow(self, runner, temp_config_dir):
        """测试完整工作流程"""
        # 这里测试一个完整的工作流程
        # 1. 初始化系统
        # 2. 检查系统状态
        # 3. 查看配置
        # 4. 查看日志

        with patch('atlas.cli.get_config') as mock_get_config, \
             patch('atlas.cli.init_logger') as mock_init_logger:

            mock_config = Mock()
            mock_config.config_dir_path = temp_config_dir
            mock_config.data_dir = temp_config_dir / "data"
            mock_config.log_dir = temp_config_dir / "logs"
            mock_config.env = "test"
            mock_config.debug = True
            mock_config.log_level = "DEBUG"

            mock_get_config.return_value = mock_config
            mock_logger = Mock()
            mock_init_logger.return_value = mock_logger

            # 初始化
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'init'])
            assert result.exit_code == 0

            # 检查
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'check'])
            assert result.exit_code == 0

            # 状态
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'status'])
            assert result.exit_code == 0

            # 配置
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'config', 'show'])
            assert result.exit_code == 0

    def test_error_handling(self, runner):
        """测试错误处理"""
        # 测试无效配置目录
        result = runner.invoke(main, ['--config-dir', '/nonexistent', 'check'])
        assert result.exit_code != 0

        # 测试无效命令
        result = runner.invoke(main, ['nonexistent-command'])
        assert result.exit_code != 0
        assert 'No such command' in result.output

    def test_command_options(self, runner, temp_config_dir):
        """测试命令选项"""
        with patch('atlas.cli.get_config') as mock_get_config, \
             patch('atlas.cli.init_logger') as mock_init_logger:

            mock_config = Mock()
            mock_config.config_dir_path = temp_config_dir
            mock_config.data_dir = temp_config_dir / "data"
            mock_config.log_dir = temp_config_dir / "logs"

            mock_get_config.return_value = mock_config
            mock_logger = Mock()
            mock_init_logger.return_value = mock_logger

            # 测试 status --detailed 选项
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'status', '--detailed'])
            assert result.exit_code == 0

            # 测试 logs --lines 选项
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'logs', '--lines', '5'])
            assert result.exit_code == 0

            # 测试 init --force 选项
            result = runner.invoke(main, ['--config-dir', str(temp_config_dir), 'init', '--force'])
            assert result.exit_code == 0