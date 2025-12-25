#!/usr/bin/env python3
"""
SQLite数据清理脚本

在迁移到PostgreSQL之前清理孤立记录。
"""

import sqlite3
import sys

def cleanup_sqlite_db(db_path: str = "data/atlas.db") -> None:
    """清理SQLite数据库中的孤立记录"""

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 备份数据库
    print("📦 创建备份...")
    import shutil
    shutil.copy2(db_path, f"{db_path}.backup")
    print(f"✅ 备份已创建: {db_path}.backup")

    # 统计孤立记录
    print("\n📊 统计孤立记录...")

    # 检查raw_documents中的孤立记录
    cursor.execute("""
        SELECT COUNT(*)
        FROM raw_documents
        WHERE source_id NOT IN (SELECT name FROM data_sources)
    """)
    orphan_docs = cursor.fetchone()[0]
    print(f"  孤立的raw_documents: {orphan_docs}条")

    # 检查collection_tasks中的孤立记录
    cursor.execute("""
        SELECT COUNT(*)
        FROM collection_tasks
        WHERE source_id NOT IN (SELECT name FROM data_sources)
    """)
    orphan_tasks = cursor.fetchone()[0]
    print(f"  孤立的collection_tasks: {orphan_tasks}条")

    if orphan_docs == 0 and orphan_tasks == 0:
        print("\n✅ 没有孤立记录，无需清理")
        conn.close()
        return

    # 确认清理
    print(f"\n⚠️  将删除 {orphan_docs + orphan_tasks} 条孤立记录")
    response = input("确认继续？(yes/no): ")
    if response.lower() not in ['yes', 'y']:
        print("❌ 操作已取消")
        conn.close()
        return

    # 删除孤立的raw_documents
    if orphan_docs > 0:
        print(f"\n🗑️  删除孤立的raw_documents...")
        cursor.execute("""
            DELETE FROM raw_documents
            WHERE source_id NOT IN (SELECT name FROM data_sources)
        """)
        print(f"   已删除 {cursor.rowcount} 条记录")

    # 删除孤立的collection_tasks
    if orphan_tasks > 0:
        print(f"\n🗑️  删除孤立的collection_tasks...")
        cursor.execute("""
            DELETE FROM collection_tasks
            WHERE source_id NOT IN (SELECT name FROM data_sources)
        """)
        print(f"   已删除 {cursor.rowcount} 条记录")

    # 提交更改
    conn.commit()

    # 验证清理结果
    print("\n✅ 清理完成，验证结果...")
    cursor.execute("SELECT COUNT(*) FROM raw_documents")
    docs_left = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM collection_tasks")
    tasks_left = cursor.fetchone()[0]

    print(f"  raw_documents剩余: {docs_left}条")
    print(f"  collection_tasks剩余: {tasks_left}条")

    conn.close()
    print("\n✅ 数据库清理完成！")

if __name__ == "__main__":
    cleanup_sqlite_db()
