#!/usr/bin/env python3
"""
Atlas 简单监控工具

提供基础的系统状态监控和数据查看功能。
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path


def get_system_status():
    """获取系统状态"""
    db_path = "data/atlas.db"

    if not Path(db_path).exists():
        print("❌ 数据库文件不存在")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("🗄️ Atlas 系统状态报告")
    print("=" * 50)
    print(f"🕒 查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 数据库文件大小
    db_file = Path(db_path)
    size_mb = db_file.stat().st_size / (1024 * 1024)
    print(f"💾 数据库大小: {size_mb:.1f} MB")

    # 数据源统计
    cursor.execute("SELECT COUNT(*) FROM data_sources")
    total_sources = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM data_sources WHERE enabled = 1")
    enabled_sources = cursor.fetchone()[0]

    print(f"📡 数据源: {enabled_sources}/{total_sources} 启用")

    # 采集统计
    cursor.execute("SELECT SUM(collection_count), SUM(success_count), SUM(error_count) FROM data_sources")
    result = cursor.fetchone()
    total_coll, success_coll, error_coll = result

    print(f"🔄 采集统计: 总{total_coll or 0}次, 成功{success_coll or 0}次, 失败{error_coll or 0}次")

    # 文档统计
    cursor.execute("SELECT COUNT(*) FROM raw_documents")
    raw_docs = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM processed_documents")
    processed_docs = cursor.fetchone()[0]

    print(f"📄 文档数量: 原始{raw_docs}个, 处理{processed_docs}个")

    # 今日新增
    today = datetime.now().date().isoformat()
    cursor.execute("SELECT COUNT(*) FROM raw_documents WHERE DATE(collected_at) = ?", (today,))
    today_docs = cursor.fetchone()[0]
    print(f"🗓️ 今日新增: {today_docs}个")

    print()


def show_sources_status():
    """显示数据源状态"""
    db_path = "data/atlas.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("📡 数据源状态详情:")
    print("-" * 80)
    print(f"{'数据源':<25} {'类型':<10} {'状态':<8} {'采集次数':<8} {'成功率':<8}")
    print("-" * 80)

    cursor.execute('''
        SELECT name, source_type, enabled, collection_count, success_count
        FROM data_sources
        ORDER BY enabled DESC, name
    ''')

    sources = cursor.fetchall()

    for name, stype, enabled, total, success in sources:
        status = "✅启用" if enabled else "❌禁用"
        success_rate = f"{(success/total*100):.1f}%" if total > 0 else "N/A"

        print(f"{name:<25} {stype:<10} {status:<8} {total:<8} {success_rate:<8}")

    conn.close()


def show_collected_files():
    """显示采集的文件"""
    print("\n📁 采集的文件:")
    print("-" * 60)

    data_dir = Path("data")
    if not data_dir.exists():
        print("❌ 数据目录不存在")
        return

    # 查找所有JSON文件
    json_files = list(data_dir.rglob("*.json"))

    if not json_files:
        print("📭 暂无采集文件")
        return

    print(f"📊 找到 {len(json_files)} 个数据文件:")
    print()

    for i, json_file in enumerate(json_files[:10], 1):  # 只显示前10个
        rel_path = json_file.relative_to(Path.cwd())
        file_size = json_file.stat().st_size

        # 尝试读取文件内容
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if 'items' in data:
                item_count = len(data['items'])
                source_name = data.get('source_name', 'Unknown')
                print(f"{i:2d}. 📄 {rel_path}")
                print(f"     📊 数据源: {source_name}")
                print(f"     📝 项目数: {item_count}")
                print(f"     📏 文件大小: {file_size} bytes")

                if data.get('items'):
                    print(f"     📋 最新项目: {data['items'][0].get('title', 'No Title')[:50]}...")

        except Exception as e:
            print(f"❌ 读取失败 {rel_path}: {e}")

        print()

    if len(json_files) > 10:
        print(f"📊 ... 还有 {len(json_files) - 10} 个文件未显示")


def show_recent_activity():
    """显示最近活动"""
    db_path = "data/atlas.db"

    if not Path(db_path).exists():
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("📋 最近活动:")
    print("-" * 60)

    # 查看最近的采集任务
    cursor.execute('''
        SELECT source_id, created_at, status, items_collected
        FROM collection_tasks
        ORDER BY created_at DESC
        LIMIT 5
    ''')

    tasks = cursor.fetchall()

    if not tasks:
        print("📭 暂无活动记录")
    else:
        for task in tasks:
            source_id, created_at, status, items = task
            status_icon = {"completed": "✅", "failed": "❌", "running": "🔄"}.get(status, "❓")

            created_time = created_at[:16] if created_at else "Unknown"
            print(f"{status_icon} {created_time} | {source_id} | {status} | {items or 0}项")

    conn.close()


def main():
    """主函数"""
    print("🚀 Atlas 系统监控工具")
    print("=" * 50)

    # 获取系统状态
    get_system_status()

    # 显示数据源状态
    show_sources_status()

    # 显示最近活动
    show_recent_activity()

    # 显示采集的文件
    show_collected_files()

    print("\n💡 使用提示:")
    print("  - 运行数据采集: uv run python -m atlas collect --dry-run")
    print("  - 查看详细审计: uv run python scripts/db_view.py --summary")
    print("  - 查看系统日志: tail -f logs/atlas.log")


if __name__ == "__main__":
    main()