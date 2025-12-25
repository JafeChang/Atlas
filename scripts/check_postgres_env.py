#!/usr/bin/env python3
"""
PostgreSQL环境检查脚本

检查PostgreSQL环境是否就绪，提供安装指导。
"""

import sys
import subprocess
import importlib
from pathlib import Path

def check_postgres_cli():
    """检查PostgreSQL命令行工具"""
    print("🔍 检查PostgreSQL命令行工具...")

    try:
        result = subprocess.run(
            ["psql", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"✅ 找到PostgreSQL: {version}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("❌ 未找到PostgreSQL命令行工具")
    return False

def check_postgres_service():
    """检查PostgreSQL服务运行状态"""
    print("\n🔍 检查PostgreSQL服务状态...")

    try:
        # 尝试连接到默认PostgreSQL端口
        result = subprocess.run(
            ["pg_isready", "-h", "localhost", "-p", "5432"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print("✅ PostgreSQL服务正在运行")
            return True
        else:
            print(f"⚠️ PostgreSQL服务未就绪: {result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("❌ 无法检查PostgreSQL服务状态")

    return False

def check_python_dependencies():
    """检查Python依赖包"""
    print("\n🔍 检查Python依赖包...")

    required_packages = [
        ("sqlalchemy", "SQLAlchemy 2.0+"),
        ("asyncpg", "asyncpg (PostgreSQL驱动)"),
        ("aiosqlite", "aiosqlite (SQLite驱动)"),
    ]

    all_ok = True
    for package, description in required_packages:
        try:
            module = importlib.import_module(package)
            version = getattr(module, "__version__", "unknown")
            print(f"✅ {description}: {version}")
        except ImportError:
            print(f"❌ 缺少: {description}")
            all_ok = False

    return all_ok

def check_migration_script():
    """检查迁移脚本"""
    print("\n🔍 检查迁移脚本...")

    script_path = Path(__file__).parent / "migrate_to_postgres.py"
    if script_path.exists():
        print(f"✅ 找到迁移脚本: {script_path}")
        return True
    else:
        print(f"❌ 未找到迁移脚本: {script_path}")
        return False

def check_sqlite_database():
    """检查SQLite数据库"""
    print("\n🔍 检查SQLite数据库...")

    # 查找SQLite数据库文件
    db_paths = [
        Path("data/atlas.db"),
        Path("atlas.db"),
        Path("data/db/atlas.db"),
    ]

    for db_path in db_paths:
        if db_path.exists():
            size_mb = db_path.stat().st_size / (1024 * 1024)
            print(f"✅ 找到SQLite数据库: {db_path} ({size_mb:.2f} MB)")
            return db_path

    print("⚠️ 未找到SQLite数据库文件")
    return None

def test_sqlite_connection(db_path):
    """测试SQLite连接和数据"""
    print(f"\n🔍 测试SQLite连接: {db_path}...")

    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 获取表列表
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        tables = cursor.fetchall()

        if tables:
            print(f"✅ SQLite数据库包含 {len(tables)} 个表:")
            for (table_name,) in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
                count = cursor.fetchone()[0]
                print(f"   - {table_name}: {count} 条记录")

        conn.close()
        return True

    except Exception as e:
        print(f"❌ SQLite连接失败: {e}")
        return False

def provide_installation_guide():
    """提供安装指导"""
    print("\n" + "=" * 60)
    print("📋 PostgreSQL安装指南")
    print("=" * 60)

    print("""
方法1: 使用apt安装 (Ubuntu/Debian WSL)

    sudo apt-get update
    sudo apt-get install -y postgresql postgresql-contrib
    sudo service postgresql start

方法2: 使用Docker

    # 从.env.local读取配置（推荐）
    source .env.local 2>/dev/null || true
    docker run -d \\
      --name atlas-postgres \\
      -e POSTGRES_DB=${ATLAS_DATABASE_NAME:-atlas_db} \\
      -e POSTGRES_USER=${ATLAS_DATABASE_USER:-atlas_user} \\
      -e POSTGRES_PASSWORD=${ATLAS_DATABASE_PASSWORD:-your_password} \\
      -p 5432:5432 \\
      postgres:16-alpine

方法3: 启动Windows Docker Desktop并使用Docker Compose

    # 在PowerShell中启动Docker Desktop
    # 然后在项目目录运行:
    docker-compose up -d

详细安装指南: docs/guidelines/postgresql-setup-guide.md

⚠️  重要：安装后请创建 .env.local 文件并配置数据库凭证
    cp .env.local.example .env.local
    # 然后编辑 .env.local 填入实际配置
""")

    print("=" * 60)

def main():
    """主函数"""
    print("=" * 60)
    print("Atlas PostgreSQL环境检查")
    print("=" * 60)

    # 检查各项
    postgres_cli = check_postgres_cli()
    postgres_service = check_postgres_service()
    python_deps = check_python_dependencies()
    migration_script = check_migration_script()
    sqlite_db = check_sqlite_database()

    if sqlite_db:
        test_sqlite_connection(sqlite_db)

    # 汇总结果
    print("\n" + "=" * 60)
    print("📊 检查结果汇总")
    print("=" * 60)

    checks = {
        "PostgreSQL CLI": postgres_cli,
        "PostgreSQL服务": postgres_service,
        "Python依赖": python_deps,
        "迁移脚本": migration_script,
        "SQLite数据库": sqlite_db is not None,
    }

    for name, result in checks.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{status}: {name}")

    # 判断下一步
    print("\n" + "=" * 60)
    if all(checks.values()):
        print("🎉 所有检查通过！可以执行迁移:")
        print("   python scripts/migrate_to_postgres.py --dry-run")
    elif postgres_cli and postgres_service:
        print("✅ PostgreSQL已就绪！可以执行迁移:")
        print("   python scripts/migrate_to_postgres.py --dry-run")
    else:
        print("⚠️ PostgreSQL环境未就绪，请先安装PostgreSQL")
        provide_installation_guide()

    print("=" * 60)

    # 返回退出码
    return 0 if all(checks.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
