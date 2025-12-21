#!/usr/bin/env python3
"""
Atlas 系统监控工具

提供实时监控、状态查看、数据分析等功能。
"""

import time
import sqlite3
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import threading
import sys


class AtlasMonitor:
    """Atlas 系统监控器"""

    def __init__(self, db_path: str = "data/atlas.db", log_path: str = "logs"):
        self.db_path = db_path
        self.log_path = Path(log_path)
        self.running = False

    def connect_db(self):
        """连接数据库"""
        if not Path(self.db_path).exists():
            print(f"❌ 数据库文件不存在: {self.db_path}")
            return None
        return sqlite3.connect(self.db_path)

    def get_system_overview(self) -> Dict:
        """获取系统概览"""
        conn = self.connect_db()
        if not conn:
            return {}

        cursor = conn.cursor()

        overview = {
            "timestamp": datetime.now().isoformat(),
            "database_size": 0,
            "data_sources": {"total": 0, "enabled": 0, "disabled": 0},
            "collections": {"total": 0, "success": 0, "failed": 0},
            "documents": {"raw": 0, "processed": 0, "today": 0},
            "storage": {"files_count": 0, "total_size": 0}
        }

        try:
            # 数据库文件大小
            db_file = Path(self.db_path)
            if db_file.exists():
                overview["database_size"] = db_file.stat().st_size

            # 数据源统计
            cursor.execute("SELECT COUNT(*) FROM data_sources")
            overview["data_sources"]["total"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM data_sources WHERE enabled = 1")
            overview["data_sources"]["enabled"] = cursor.fetchone()[0]

            overview["data_sources"]["disabled"] = (
                overview["data_sources"]["total"] - overview["data_sources"]["enabled"]
            )

            # 采集统计
            cursor.execute("SELECT SUM(collection_count), SUM(success_count), SUM(error_count) FROM data_sources")
            result = cursor.fetchone()
            overview["collections"]["total"] = result[0] or 0
            overview["collections"]["success"] = result[1] or 0
            overview["collections"]["failed"] = result[2] or 0

            # 文档统计
            cursor.execute("SELECT COUNT(*) FROM raw_documents")
            overview["documents"]["raw"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM processed_documents")
            overview["documents"]["processed"] = cursor.fetchone()[0]

            # 今日采集
            today = datetime.now().date().isoformat()
            cursor.execute(
                "SELECT COUNT(*) FROM raw_documents WHERE DATE(collected_at) = ?",
                (today,)
            )
            overview["documents"]["today"] = cursor.fetchone()[0]

            # 文件存储统计
            data_dir = Path("data/raw")
            if data_dir.exists():
                json_files = list(data_dir.rglob("*.json"))
                overview["storage"]["files_count"] = len(json_files)

                total_size = sum(f.stat().st_size for f in json_files)
                overview["storage"]["total_size"] = total_size

        except Exception as e:
            print(f"❌ 获取系统概览失败: {e}")

        finally:
            conn.close()

        return overview

    def format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    def format_timestamp(self, timestamp_str: Optional[str]) -> str:
        """格式化时间戳"""
        if not timestamp_str:
            return "N/A"
        try:
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            return dt.strftime("%m-%d %H:%M")
        except:
            return timestamp_str

    def display_overview(self):
        """显示系统概览"""
        overview = self.get_system_overview()

        print("🗄️  Atlas 系统概览")
        print("=" * 50)
        print(f"📊 查询时间: {overview['timestamp']}")
        print(f"💾 数据库大小: {self.format_size(overview['database_size'])}")
        print(f"📁 存储文件: {overview['storage']['files_count']} 个 ({self.format_size(overview['storage']['total_size'])})")
        print()

        print("📡 数据源状态:")
        print(f"  总数: {overview['data_sources']['total']}")
        print(f"  ✅ 启用: {overview['data_sources']['enabled']}")
        print(f"  ❌ 禁用: {overview['data_sources']['disabled']}")
        print()

        print("🔄 采集统计:")
        print(f"  总采集次数: {overview['collections']['total']}")
        print(f"  ✅ 成功次数: {overview['collections']['success']}")
        print(f"  ❌ 失败次数: {overview['collections']['failed']}")

        if overview['collections']['total'] > 0:
            success_rate = (overview['collections']['success'] / overview['collections']['total']) * 100
            print(f"  📈 成功率: {success_rate:.1f}%")
        print()

        print("📄 文档统计:")
        print(f"  原始文档: {overview['documents']['raw']}")
        print(f"  处理文档: {overview['documents']['processed']}")
        print(f"  🗓️  今日新增: {overview['documents']['today']}")

    def show_data_sources_status(self):
        """显示数据源状态"""
        conn = self.connect_db()
        if not conn:
            return

        cursor = conn.cursor()

        cursor.execute('''
            SELECT name, source_type, enabled, collection_count,
                   success_count, error_count, last_success_at, last_error
            FROM data_sources
            ORDER BY enabled DESC, name
        ''')

        sources = cursor.fetchall()

        print("\n📡 数据源详细状态:")
        print("=" * 80)
        print(f"{'名称':<25} {'类型':<10} {'状态':<8} {'采集/成功/失败':<15} {'最后成功':<12} {'状态描述'}")
        print("-" * 80)

        for source in sources:
            name, stype, enabled, coll_count, succ_count, err_count, last_success, last_error = source

            status = "✅ 启用" if enabled else "❌ 禁用"
            coll_str = f"{coll_count or 0}/{succ_count or 0}/{err_count or 0}"
            last_success_str = self.format_timestamp(last_success)

            # 状态描述
            if enabled and coll_count > 0:
                if err_count > 0:
                    status_desc = f"⚠️  {last_error[:30] if last_error else '有错误'}"
                else:
                    status_desc = "✅ 正常"
            elif enabled and coll_count == 0:
                status_desc = "⏳ 待采集"
            else:
                status_desc = "❌ 已禁用"

            print(f"{name:<25} {stype:<10} {status:<8} {coll_str:<15} {last_success_str:<12} {status_desc}")

        conn.close()

    def show_recent_collections(self, limit: int = 10):
        """显示最近的采集活动"""
        conn = self.connect_db()
        if not conn:
            return

        cursor = conn.cursor()

        cursor.execute('''
            SELECT source_id, created_at, started_at, completed_at,
                   status, items_collected, items_processed, items_failed,
                   error_message
            FROM collection_tasks
            ORDER BY created_at DESC
            LIMIT ?
        ''', (limit,))

        tasks = cursor.fetchall()

        if not tasks:
            print("\n📋 暂无采集任务记录")
            conn.close()
            return

        print(f"\n📋 最近采集任务 (最近{limit}个):")
        print("=" * 90)
        print(f"{'数据源':<20} {'开始时间':<12} {'状态':<8} {'采集/处理/失败':<15} {'耗时':<8} {'错误信息'}")
        print("-" * 90)

        for task in tasks:
            (source_id, created_at, started_at, completed_at, status,
             items_collected, items_processed, items_failed, error_message) = task

            start_time = self.format_timestamp(started_at)
            status_icon = {"completed": "✅", "running": "🔄", "failed": "❌", "pending": "⏳"}.get(status, "❓")

            items_str = f"{items_collected or 0}/{items_processed or 0}/{items_failed or 0}"

            # 计算耗时
            if started_at and completed_at:
                try:
                    start_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(completed_at.replace('Z', '+00:00'))
                    duration = (end_dt - start_dt).total_seconds()
                    duration_str = f"{duration:.0f}s"
                except:
                    duration_str = "N/A"
            else:
                duration_str = "N/A"

            error_str = (error_message or "")[:20] + "..." if error_message and len(error_message) > 20 else (error_message or "")

            print(f"{source_id:<20} {start_time:<12} {status_icon+status:<8} {items_str:<15} {duration_str:<8} {error_str}")

        conn.close()

    def show_collected_data(self, limit: int = 20):
        """显示采集到的数据"""
        # 检查JSON文件
        data_dir = Path("data/raw")
        if not data_dir.exists():
            print("\n📁 数据目录不存在")
            return

        json_files = list(data_dir.rglob("summary_*.json"))

        if not json_files:
            print("\n📄 暂无采集数据")
            return

        # 按修改时间排序
        json_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        print(f"\n📄 采集数据概览 (最新{min(limit, len(json_files))}个文件):")
        print("=" * 80)

        for i, json_file in enumerate(json_files[:limit]):
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                file_time = datetime.fromtimestamp(json_file.stat().st_mtime)
                source_name = data.get('source_name', 'Unknown')
                items_count = data.get('items_count', 0)
                collected_at = data.get('collected_at', 'Unknown')

                print(f"{i+1:2d}. 📁 {source_name:<20} ({json_file.parent.name})")
                print(f"     📊 项目数: {items_count}")
                print(f"     🕒 采集时间: {collected_at[:19]}")
                print(f"     📂 文件位置: {json_file.relative_to(Path.cwd())}")

                # 显示最新的几个项目标题
                if data.get('items') and len(data['items']) > 0:
                    print(f"     📋 最新项目:")
                    for item in data['items'][:3]:
                        title = item.get('title', 'No Title')
                        # 截断长标题
                        if len(title) > 50:
                            title = title[:47] + "..."
                        print(f"        • {title}")

                print()

            except Exception as e:
                print(f"❌ 读取文件失败 {json_file}: {e}")

    def show_log_tail(self, lines: int = 20):
        """显示日志尾部"""
        log_file = self.log_path / "atlas.log"

        if not log_file.exists():
            print("\n📝 日志文件不存在")
            return

        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            print(f"\n📝 最近日志 (最新{lines}行):")
            print("=" * 60)

            for line in all_lines[-lines:]:
                print(line.rstrip())

        except Exception as e:
            print(f"❌ 读取日志失败: {e}")

    def monitor_loop(self, interval: int = 30):
        """实时监控循环"""
        self.running = True

        try:
            while self.running:
                # 清屏
                import os
                os.system('clear' if os.name == 'posix' else 'cls')

                print("🗄️  Atlas 实时监控")
                print("=" * 50)
                print(f"⏰ 监控时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"🔄 刷新间隔: {interval}秒")
                print("按 Ctrl+C 停止监控")
                print()

                self.display_overview()
                self.show_data_sources_status()

                # 等待下次刷新
                time.sleep(interval)

        except KeyboardInterrupt:
            print("\n\n🛑 监控已停止")
        finally:
            self.running = False

    def export_dashboard_data(self, output_file: str = "dashboard.json"):
        """导出仪表板数据"""
        overview = self.get_system_overview()

        # 获取详细的数据源信息
        conn = self.connect_db()
        sources_data = []

        if conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT name, source_type, enabled, collection_count,
                       success_count, error_count, last_success_at, url
                FROM data_sources
                ORDER BY name
            ''')

            for row in cursor.fetchall():
                sources_data.append({
                    "name": row[0],
                    "type": row[1],
                    "enabled": bool(row[2]),
                    "collections": row[3] or 0,
                    "successes": row[4] or 0,
                    "errors": row[5] or 0,
                    "last_success": row[6],
                    "url": row[7]
                })

            conn.close()

        dashboard_data = {
            "overview": overview,
            "sources": sources_data,
            "export_time": datetime.now().isoformat()
        }

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(dashboard_data, f, indent=2, ensure_ascii=False)

        print(f"✅ 仪表板数据已导出到: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Atlas 系统监控工具")
    parser.add_argument("--db", default="data/atlas.db", help="数据库文件路径")
    parser.add_argument("--log", default="logs", help="日志文件路径")

    subparsers = parser.add_subparsers(dest='command', help='可用命令')

    # 概览命令
    subparsers.add_parser('overview', help='显示系统概览')

    # 数据源状态
    subparsers.add_parser('sources', help='显示数据源状态')

    # 采集历史
    subparsers.add_parser('history', help='显示采集历史')

    # 数据查看
    parser_data = subparsers.add_parser('data', help='查看采集数据')
    parser_data.add_argument('--limit', type=int, default=20, help='显示条目限制')

    # 日志查看
    parser_log = subparsers.add_parser('logs', help='查看日志')
    parser_log.add_argument('--lines', type=int, default=20, help='显示行数')

    # 实时监控
    parser_monitor = subparsers.add_parser('monitor', help='实时监控')
    parser_monitor.add_argument('--interval', type=int, default=30, help='刷新间隔(秒)')

    # 导出数据
    parser_export = subparsers.add_parser('export', help='导出仪表板数据')
    parser_export.add_argument('--output', default='dashboard.json', help='输出文件')

    args = parser.parse_args()

    monitor = AtlasMonitor(args.db, args.log)

    if args.command == 'overview':
        monitor.display_overview()
        monitor.show_data_sources_status()

    elif args.command == 'sources':
        monitor.show_data_sources_status()

    elif args.command == 'history':
        monitor.show_recent_collections()

    elif args.command == 'data':
        monitor.show_collected_data(args.limit)

    elif args.command == 'logs':
        monitor.show_log_tail(args.lines)

    elif args.command == 'monitor':
        print("🚀 启动实时监控...")
        monitor.monitor_loop(args.interval)

    elif args.command == 'export':
        monitor.export_dashboard_data(args.output)

    else:
        # 默认显示概览
        monitor.display_overview()
        monitor.show_data_sources_status()
        print(f"\n💡 使用 --help 查看更多选项")


if __name__ == "__main__":
    main()