#!/usr/bin/env python3
"""
Atlas 测试运行脚本

用于运行项目的所有测试或指定测试
"""

import sys
import subprocess
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))


def run_pytest(test_args=None, coverage=False):
    """运行pytest测试"""
    cmd = ["python", "-m", "pytest"]

    if coverage:
        cmd.extend([
            "--cov=src/atlas",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
            "--cov-fail-under=80"
        ])

    if test_args:
        cmd.extend(test_args)
    else:
        # 默认运行所有测试
        cmd.append("tests/")

    # 添加其他有用的pytest参数
    cmd.extend([
        "-v",  # 详细输出
        "--tb=short",  # 简短的错误回溯
        "--strict-markers",  # 严格标记检查
        "--disable-warnings",  # 禁用警告
    ])

    print(f"运行命令: {' '.join(cmd)}")
    return subprocess.run(cmd)


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="Atlas 测试运行器")
    parser.add_argument("--coverage", "-c", action="store_true", help="生成覆盖率报告")
    parser.add_argument("--database-only", action="store_true", help="仅运行数据库测试")
    parser.add_argument("--storage-only", action="store_true", help="仅运行存储测试")
    parser.add_argument("--integration", action="store_true", help="运行集成测试")
    parser.add_argument("--specific", "-s", help="运行特定的测试文件或函数")
    parser.add_argument("pytest_args", nargs="*", help="传递给pytest的其他参数")

    args = parser.parse_args()

    # 构建测试参数
    test_args = []

    if args.database_only:
        test_args.append("tests/test_database.py")
    elif args.storage_only:
        test_args.append("tests/test_storage.py")
    elif args.integration:
        test_args.append("-m")
        test_args.append("integration")
    elif args.specific:
        test_args.append(args.specific)

    if args.pytest_args:
        test_args.extend(args.pytest_args)

    # 运行测试
    result = run_pytest(test_args, args.coverage)

    if args.coverage and result.returncode == 0:
        print("\n📊 覆盖率报告已生成:")
        print("- 终端输出: 已显示在上方")
        print("- HTML报告: htmlcov/index.html")

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())