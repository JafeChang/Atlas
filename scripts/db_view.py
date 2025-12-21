#!/usr/bin/env python3
"""
Atlas 数据库查看工具

简单直接的数据库查询和查看功能。
"""

import sqlite3
import json
import argparse
from datetime import datetime
from pathlib import Path


def connect_db(db_path="data/atlas.db"):
    """连接数据库"""
    if not Path(db_path).exists():
        print(f"❌ 数据库文件不存在: {db_path}")
        return None
    return sqlite3.connect(db_path)


def show_tables(conn):
    """显示所有表"""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()

    print("📊 数据库表:")
    for i, (table_name,) in enumerate(tables, 1):
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        print(f"  {i}. {table_name}: {count:,} 条记录")


def show_table_structure(conn, table_name):
    """显示表结构"""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()

    print(f"\n📋 表 '{table_name}' 结构:")
    print(f"{'列名':<20} {'类型':<15} {'是否为空':<8} {'默认值':<15} {'主键':<5}")
    print("-" * 80)

    for col in columns:
        cid, name, col_type, not_null, default_val, pk = col
        print(f"{name:<20} {col_type:<15} {'NO' if not_null else 'YES':<8} {str(default_val or ''):<15} {'PK' if pk else ''}")


def show_table_data(conn, table_name, limit=10):
    """显示表数据"""
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    total_count = cursor.fetchone()[0]

    if total_count == 0:
        print(f"📝 表 '{table_name}' 没有数据")
        return

    print(f"\n📄 表 '{table_name}' 数据 (总共 {total_count:,} 条, 显示前 {limit} 条):")

    cursor.execute(f"SELECT * FROM {table_name} LIMIT {limit}")
    rows = cursor.fetchall()

    # 获取列名
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [col[1] for col in cursor.fetchall()]

    # 显示表头
    print(f"{'#':<4} | {' | '.join(col[:12] for col in columns)}")
    print("-" * (4 + len(columns) * 15))

    # 显示数据
    for i, row in enumerate(rows, 1):
        row_str = " | ".join(str(cell)[:12] if cell is not None else 'NULL' for cell in row)
        print(f"{i:<4} | {row_str}")


def show_sources_summary(conn):
    """显示数据源汇总"""
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) as enabled,
            SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) as disabled,
            SUM(collection_count) as total_collections,
            SUM(success_count) as total_successes,
            SUM(error_count) as total_errors
        FROM data_sources
    ''')

    stats = cursor.fetchone()
    total, enabled, disabled, total_coll, total_success, total_errors = stats

    print("\n📡 数据源汇总:")
    print(f"  总数: {total}")
    print(f"  启用: {enabled}")
    print(f"  禁用: {disabled}")
    print(f"  总采集次数: {total_coll or 0}")
    print(f"  总成功次数: {total_success or 0}")
    print(f"  总失败次数: {total_errors or 0}")

    if total_coll and total_coll > 0:
        success_rate = (total_success / total_coll) * 100
        print(f"  成功率: {success_rate:.1f}%")


def query_data_sources(conn, enabled_only=True):
    """查询数据源"""
    cursor = conn.cursor()

    where_clause = "WHERE enabled = 1" if enabled_only else ""

    cursor.execute(f'''
        SELECT name, source_type, url, collection_count,
               success_count, error_count, last_success_at
        FROM data_sources
        {where_clause}
        ORDER BY name
    ''')

    sources = cursor.fetchall()

    print(f"\n📊 {'启用' if enabled_only else '所有'}数据源:")
    print(f"{'名称':<25} {'类型':<10} {'采集次数':<8} {'成功次数':<8} {'失败次数':<8} {'最后成功'}")
    print("-" * 80)

    for source in sources:
        name, stype, url, coll_count, succ_count, err_count, last_success = source

        last_success_str = "N/A"
        if last_success:
            try:
                dt = datetime.fromisoformat(last_success.replace('Z', '+00:00'))
                last_success_str = dt.strftime("%m-%d %H:%M")
            except:
                last_success_str = "Invalid"

        print(f"{name:<25} {stype:<10} {coll_count or 0:<8} {succ_count or 0:<8} {err_count or 0:<8} {last_success_str}")


def export_to_json(conn, output_file="atlas_audit.json"):
    """导出审计数据到JSON"""
    cursor = conn.cursor()

    audit_data = {
        "export_time": datetime.now().isoformat(),
        "tables": {},
        "data_sources": []
    }

    # 导出所有表的统计信息
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        audit_data["tables"][table] = count

    # 导出数据源详情
    cursor.execute('''
        SELECT name, source_type, url, enabled, collection_interval,
               collection_count, success_count, error_count, last_success_at,
               created_at, updated_at
        FROM data_sources
        ORDER BY name
    ''')

    for row in cursor.fetchall():
        audit_data["data_sources"].append({
            "name": row[0],
            "type": row[1],
            "url": row[2],
            "enabled": bool(row[3]),
            "interval": row[4],
            "collections": row[5] or 0,
            "successes": row[6] or 0,
            "errors": row[7] or 0,
            "last_success": row[8],
            "created_at": row[9],
            "updated_at": row[10]
        })

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(audit_data, f, indent=2, ensure_ascii=False)

    print(f"✅ 审计数据已导出到: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Atlas 数据库查看工具")
    parser.add_argument("--db", default="data/atlas.db", help="数据库文件路径")
    parser.add_argument("--tables", action="store_true", help="显示所有表")
    parser.add_argument("--structure", metavar="TABLE", help="显示指定表的结构")
    parser.add_argument("--data", metavar="TABLE", help="显示指定表的数据")
    parser.add_argument("--limit", type=int, default=10, help="数据显示条数限制")
    parser.add_argument("--sources", action="store_true", help="显示数据源信息")
    parser.add_argument("--all-sources", action="store_true", help="显示所有数据源（包括禁用的）")
    parser.add_argument("--export", metavar="FILE", help="导出审计数据到JSON文件")
    parser.add_argument("--summary", action="store_true", help="显示汇总信息")

    args = parser.parse_args()

    conn = connect_db(args.db)
    if not conn:
        return

    try:
        if args.tables:
            show_tables(conn)

        elif args.structure:
            show_table_structure(conn, args.structure)

        elif args.data:
            show_table_data(conn, args.data, args.limit)

        elif args.sources:
            query_data_sources(conn, enabled_only=True)

        elif args.all_sources:
            query_data_sources(conn, enabled_only=False)

        elif args.export:
            export_to_json(conn, args.export)

        elif args.summary:
            show_tables(conn)
            show_sources_summary(conn)

        else:
            # 默认显示基本信息
            print("🗄️ Atlas 数据库查看工具")
            print(f"📁 数据库: {args.db}")

            db_file = Path(args.db)
            if db_file.exists():
                size_mb = db_file.stat().st_size / (1024 * 1024)
                print(f"📏 大小: {size_mb:.1f} MB")

            show_tables(conn)
            show_sources_summary(conn)

            print(f"\n💡 使用 --help 查看更多选项")

    finally:
        conn.close()


if __name__ == "__main__":
    main()