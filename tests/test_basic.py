"""
基础测试 - 测试项目结构和基本功能
"""

import pytest
import sys
from pathlib import Path

# 添加 src 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_project_structure():
    """测试项目结构是否正确"""
    project_root = Path(__file__).parent.parent

    # 检查主要目录
    assert project_root.joinpath("src").exists(), "src 目录不存在"
    assert project_root.joinpath("src/atlas").exists(), "src/atlas 目录不存在"
    assert project_root.joinpath("tests").exists(), "tests 目录不存在"
    assert project_root.joinpath("config").exists(), "config 目录不存在"
    assert project_root.joinpath("scripts").exists(), "scripts 目录不存在"
    assert project_root.joinpath("docs").exists(), "docs 目录不存在"

    # 检查关键文件
    assert project_root.joinpath("pyproject.toml").exists(), "pyproject.toml 不存在"
    assert project_root.joinpath("README.md").exists(), "README.md 不存在"


def test_atlas_package_imports():
    """测试 Atlas 包的基本导入"""
    try:
        import atlas
        assert hasattr(atlas, '__version__'), "atlas 包缺少 __version__ 属性"
    except ImportError as e:
        pytest.skip(f"无法导入 atlas 包: {e}")


def test_core_modules_structure():
    """测试核心模块结构"""
    src_dir = Path(__file__).parent.parent / "src" / "atlas"

    core_modules = [
        "core/config.py",
        "core/logging.py",
        "core/database.py",
        "core/storage.py",
        "core/operations.py",
        "models/documents.py",
        "cli.py",
    ]

    for module in core_modules:
        assert src_dir.joinpath(module).exists(), f"核心模块不存在: {module}"


def test_test_structure():
    """测试测试结构"""
    tests_dir = Path(__file__).parent

    # 检查测试工具
    assert tests_dir.joinpath("conftest.py").exists(), "conftest.py 不存在"
    assert tests_dir.joinpath("utils.py").exists(), "测试工具文件 utils.py 不存在"

    # 检查测试文件
    test_files = list(tests_dir.glob("test_*.py"))
    assert len(test_files) > 0, "没有找到测试文件"


def test_scripts_exist():
    """测试开发脚本是否存在"""
    scripts_dir = Path(__file__).parent.parent / "scripts"

    required_scripts = [
        "setup.sh",
        "dev.sh",
        "cron_job.py",
        "pre-commit",
    ]

    for script in required_scripts:
        script_path = scripts_dir.joinpath(script)
        assert script_path.exists(), f"开发脚本不存在: {script}"

        # 检查脚本是否可执行
        if script.endswith('.sh') or not script.endswith('.py'):
            assert script_path.stat().st_mode & 0o111, f"脚本不可执行: {script}"


def test_config_files_exist():
    """测试配置文件是否存在"""
    config_dir = Path(__file__).parent.parent / "config"

    # 检查环境配置文件
    env_files = [
        ".env.development",
        ".env.local",
    ]

    for env_file in env_files:
        assert config_dir.joinpath(env_file).exists(), f"环境配置文件不存在: {env_file}"

    # 检查主配置文件
    assert config_dir.joinpath("config.yaml").exists(), "主配置文件不存在"
    assert config_dir.joinpath("sources.yaml").exists(), "数据源配置文件不存在"


def test_pyproject_toml_structure():
    """测试 pyproject.toml 结构"""
    project_root = Path(__file__).parent.parent
    pyproject_path = project_root.joinpath("pyproject.toml")

    assert pyproject_path.exists(), "pyproject.toml 不存在"

    content = pyproject_path.read_text(encoding='utf-8')

    # 检查必要的配置
    assert '[project]' in content, "缺少 [project] 配置"
    assert 'atlas' in content, "项目名称配置不正确"
    assert '[project.scripts]' in content, "缺少脚本配置"
    assert 'atlas = "atlas.cli:main"' in content, "CLI 脚本配置不正确"


def test_cli_module_structure():
    """测试 CLI 模块结构"""
    cli_path = Path(__file__).parent.parent / "src" / "atlas" / "cli.py"

    assert cli_path.exists(), "cli.py 不存在"

    content = cli_path.read_text(encoding='utf-8')

    # 检查必要的导入和函数
    assert 'import click' in content, "缺少 click 导入"
    assert 'from rich' in content, "缺少 rich 导入"
    assert 'def main(' in content, "缺少 main 函数"
    assert '@main.command()' in content, "缺少命令装饰器"


def test_documentation_exists():
    """测试文档是否存在"""
    docs_dir = Path(__file__).parent.parent / "docs"

    # 检查主要文档文件
    doc_files = [
        "README.md",
        "documentation-system.md",
    ]

    for doc_file in doc_files:
        assert docs_dir.joinpath(doc_file).exists(), f"文档文件不存在: {doc_file}"


class TestBasicFunctionality:
    """基础功能测试类"""

    def test_python_version_compatibility(self):
        """测试 Python 版本兼容性"""
        version_info = sys.version_info
        assert version_info >= (3, 8), f"Python 版本过低: {version_info.major}.{version_info.minor}"

    def test_import_error_handling(self):
        """测试导入错误处理"""
        # 测试导入不存在的模块
        try:
            import nonexistent_module
            assert False, "应该抛出 ImportError"
        except ImportError:
            pass  # 预期的异常

    def test_path_operations(self):
        """测试路径操作"""
        project_root = Path(__file__).parent.parent

        # 测试路径操作
        assert project_root.is_dir(), "项目根目录不是目录"
        src_path = project_root / "src"
        assert src_path.exists(), "src 目录不存在"

    def test_file_operations(self):
        """测试文件操作"""
        test_file = Path(__file__)

        # 测试文件读取
        content = test_file.read_text(encoding='utf-8')
        assert 'test_basic' in content, "文件内容不正确"

        # 测试文件信息
        assert test_file.is_file(), "不是文件"
        assert test_file.stat().st_size > 0, "文件大小为 0"