"""
Atlas 数据审计 CLI 命令

提供数据库查询、数据审计、统计分析等功能。
"""

import sqlite3
import json
import click
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.tree import Tree
from rich import box


console = Console()


def get_db_connection(db_path: str = "data/atlas.db"):
    """获取数据库连接"""
    if not Path(db_path).exists():
        raise click.ClickException(f"数据库文件不存在: {db_path}")
    return sqlite3.connect(db_path)


def format_timestamp(timestamp_str: Optional[str]) -> str:
    """格式化时间戳"""
    if not timestamp_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return timestamp_str


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


@click.group()
def audit():
    """Atlas 数据审计命令组"""
    pass


@audit.command()
@click.option('--db-path', default='data/atlas.db', help='数据库文件路径')
def overview(db_path: str):
    """数据库概览"""
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()

        # 数据库文件信息
        db_file = Path(db_path)
        file_size = db_file.stat().st_size

        console.print(f"[bold blue]📊 数据库概览[/bold blue]")
        console.print(f"📍 文件路径: {db_file.absolute()}")
        console.print(f"📏 文件大小: {format_size(file_size)}")
        console.print()

        # 表统计
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        table_stats = []
        total_records = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("正在统计表数据...", total=len(tables))

            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                total_records += count

                cursor.execute(f"PRAGMA table_info({table})")
                columns = cursor.fetchall()

                table_stats.append((table, count, len(columns)))
                progress.advance(task)

        # 显示表统计
        table = Table(title="数据表统计", box=box.ROUNDED)
        table.add_column("表名", style="cyan")
        table.add_column("记录数", justify="right", style="green")
        table.add_column("列数", justify="right", style="blue")
        table.add_column("状态", justify="center")

        for table_name, count, cols in table_stats:
            if count > 0:
                status = "✅ 有数据"
            else:
                status = "⚪ 空"
            table.add_row(table_name, f"{count:,}", str(cols), status)

        console.print(table)
        console.print(f"\n📊 总记录数: {total_records:,}")
        console.print(f"📋 总表数: {len(tables)}")

        conn.close()

    except Exception as e:
        console.print(f"[red]❌ 错误: {e}[/red]")


@audit.command()
@click.option('--db-path', default='data/atlas.db', help='数据库文件路径')
@click.option('--source', help='指定数据源名称')
@click.option('--status', type=click.Choice(['enabled', 'disabled', 'all']), default='all', help='过滤状态')
def sources(db_path: str, source: Optional[str], status: str):
    """数据源审计"""
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()

        where_clause = ""
        params = []

        if source:
            where_clause = "AND name = ?"
            params.append(source)

        if status == 'enabled':
            where_clause += " AND enabled = 1"
        elif status == 'disabled':
            where_clause += " AND enabled = 0"

        cursor.execute(f'''
            SELECT name, description, source_type, url, enabled, collection_interval,
                   created_at, updated_at, last_collected_at, last_success_at,
                   collection_count, success_count, error_count, last_error
            FROM data_sources
            WHERE 1=1 {where_clause}
            ORDER BY created_at
        ''', params)

        sources_data = cursor.fetchall()

        if not sources_data:
            console.print("[yellow]⚠️  没有找到匹配的数据源[/yellow]")
            return

        # 统计信息
        total_sources = len(sources_data)
        enabled_count = len([s for s in sources_data if s[4]])
        total_collections = sum(s[10] for s in sources_data)
        total_successes = sum(s[11] for s in sources_data)
        total_errors = sum(s[12] for s in sources_data)

        # 显示统计
        console.print(f"[bold blue]📡 数据源审计报告[/bold blue]")
        console.print(f"📊 数据源总数: {total_sources}")
        console.print(f"✅ 启用数量: {enabled_count}")
        console.print(f"❌ 禁用数量: {total_sources - enabled_count}")
        console.print(f"🔄 总采集次数: {total_collections}")
        console.print(f"✅ 总成功次数: {total_successes}")
        console.print(f"❌ 总失败次数: {total_errors}")

        if total_collections > 0:
            success_rate = (total_successes / total_collections) * 100
            console.print(f"📈 成功率: {success_rate:.1f}%")

        console.print()

        # 详细信息表
        table = Table(title="数据源详情", box=box.ROUNDED)
        table.add_column("名称", style="cyan")
        table.add_column("类型", style="green")
        table.add_column("状态", justify="center")
        table.add_column("采集次数", justify="right")
        table.add_column("成功率", justify="right")
        table.add_column("最后成功")
        table.add_column("最后错误", style="red")

        for source_data in sources_data:
            (name, description, source_type, url, enabled, interval, created_at, updated_at,
             last_collected_at, last_success_at, collection_count, success_count,
             error_count, last_error) = source_data

            status = "✅ 启用" if enabled else "❌ 禁用"

            if collection_count > 0:
                success_rate = (success_count / collection_count) * 100
                rate_str = f"{success_rate:.1f}%"
            else:
                rate_str = "N/A"

            last_success = format_timestamp(last_success_at)
            error_summary = (last_error or "")[:30] + "..." if last_error and len(last_error) > 30 else (last_error or "")

            table.add_row(
                name,
                source_type,
                status,
                str(collection_count),
                rate_str,
                last_success,
                error_summary
            )

        console.print(table)

        conn.close()

    except Exception as e:
        console.print(f"[red]❌ 错误: {e}[/red]")


@audit.command()
@click.option('--db-path', default='data/atlas.db', help='数据库文件路径')
@click.option('--days', default=7, help='统计天数')
def metrics(db_path: str, days: int):
    """系统指标分析"""
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()

        # 查询指定天数内的指标
        since_date = (datetime.now() - timedelta(days=days)).isoformat()

        cursor.execute('''
            SELECT timestamp, total_raw_documents, total_processed_documents,
                   documents_last_24h, total_sources, active_sources, failed_sources,
                   avg_processing_time_ms, avg_collection_time_ms, error_rate_last_24h
            FROM system_metrics
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
        ''', (since_date,))

        metrics_data = cursor.fetchall()

        if not metrics_data:
            console.print(f"[yellow]⚠️  最近 {days} 天内没有系统指标数据[/yellow]")
            return

        console.print(f"[bold blue]📈 系统指标分析 (最近 {days} 天)[/bold blue]")
        console.print()

        # 计算统计信息
        if metrics_data:
            latest = metrics_data[0]
            _, total_raw, total_processed, docs_24h, total_sources, active, failed, _, _, error_rate = latest

            # 指标卡片
            metrics_panel = Panel(
                f"📄 原始文档: {total_raw:,}\n"
                f"📋 处理文档: {total_processed:,}\n"
                f"📊 24小时文档: {docs_24h:,}\n"
                f"📡 总数据源: {total_sources}\n"
                f"✅ 活跃数据源: {active}\n"
                f"❌ 失败数据源: {failed}\n"
                f"📈 24小时错误率: {error_rate:.2f}%",
                title="最新系统状态",
                border_style="blue"
            )
            console.print(metrics_panel)
            console.print()

        # 趋势表
        table = Table(title="系统指标趋势", box=box.ROUNDED)
        table.add_column("时间")
        table.add_column("原始文档", justify="right")
        table.add_column("处理文档", justify="right")
        table.add_column("活跃数据源", justify="right")
        table.add_column("24小时错误率", justify="right")

        for metric in metrics_data[:10]:  # 显示最近10条记录
            (timestamp, total_raw, total_processed, docs_24h, total_sources,
             active, failed, avg_proc_time, avg_coll_time, error_rate) = metric

            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            time_str = dt.strftime("%m-%d %H:%M")

            table.add_row(
                time_str,
                f"{total_raw:,}",
                f"{total_processed:,}",
                str(active),
                f"{error_rate:.1f}%"
            )

        console.print(table)

        conn.close()

    except Exception as e:
        console.print(f"[red]❌ 错误: {e}[/red]")


@audit.command()
@click.option('--db-path', default='data/atlas.db', help='数据库文件路径')
@click.option('--limit', default=50, help='显示记录数限制')
def documents(db_path: str, limit: int):
    """文档审计"""
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()

        # 原始文档统计
        cursor.execute(f'''
            SELECT source_id, COUNT(*) as count, MAX(collected_at) as latest
            FROM raw_documents
            GROUP BY source_id
            ORDER BY count DESC
            LIMIT {limit}
        ''')

        raw_docs = cursor.fetchall()

        # 处理文档统计
        cursor.execute(f'''
            SELECT rd.source_id, COUNT(pd.id) as processed_count,
                   AVG(pd.quality_score) as avg_quality,
                   MAX(pd.processed_at) as latest_processed
            FROM raw_documents rd
            LEFT JOIN processed_documents pd ON rd.id = pd.raw_document_id
            GROUP BY rd.source_id
            ORDER BY processed_count DESC
            LIMIT {limit}
        ''')

        processed_docs = cursor.fetchall()

        console.print("[bold blue]📄 文档审计报告[/bold blue]")
        console.print()

        # 原始文档表
        if raw_docs:
            table = Table(title="原始文档统计", box=box.ROUNDED)
            table.add_column("数据源", style="cyan")
            table.add_column("文档数量", justify="right")
            table.add_column("最后采集时间")

            for source_id, count, latest in raw_docs:
                table.add_row(
                    source_id,
                    f"{count:,}",
                    format_timestamp(latest)
                )

            console.print(table)

        # 处理文档表
        if processed_docs:
            console.print()

            table = Table(title="处理文档统计", box=box.ROUNDED)
            table.add_column("数据源", style="cyan")
            table.add_column("处理数量", justify="right")
            table.add_column("平均质量分", justify="right")
            table.add_column("最后处理时间")

            for source_id, count, avg_quality, latest in processed_docs:
                quality_str = f"{avg_quality:.1f}" if avg_quality else "N/A"
                table.add_row(
                    source_id,
                    f"{count:,}",
                    quality_str,
                    format_timestamp(latest)
                )

            console.print(table)

        conn.close()

    except Exception as e:
        console.print(f"[red]❌ 错误: {e}[/red]")


@audit.command()
@click.option('--db-path', default='data/atlas.db', help='数据库文件路径')
@click.option('--output', '-o', help='输出JSON文件路径')
def export(db_path: str, output: Optional[str]):
    """导出审计数据"""
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()

        # 收集所有审计数据
        audit_data = {
            "export_time": datetime.now().isoformat(),
            "database_path": db_path,
            "data_sources": [],
            "raw_documents_count": 0,
            "processed_documents_count": 0,
            "collection_tasks_count": 0,
            "system_metrics_count": 0
        }

        # 数据源信息
        cursor.execute('''
            SELECT name, source_type, url, enabled, collection_interval,
                   collection_count, success_count, error_count, last_success_at
            FROM data_sources
            ORDER BY name
        ''')

        for row in cursor.fetchall():
            audit_data["data_sources"].append({
                "name": row[0],
                "type": row[1],
                "url": row[2],
                "enabled": bool(row[3]),
                "interval_seconds": row[4],
                "total_collections": row[5],
                "successful_collections": row[6],
                "failed_collections": row[7],
                "last_success_at": row[8]
            })

        # 统计信息
        cursor.execute("SELECT COUNT(*) FROM raw_documents")
        audit_data["raw_documents_count"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM processed_documents")
        audit_data["processed_documents_count"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM collection_tasks")
        audit_data["collection_tasks_count"] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM system_metrics")
        audit_data["system_metrics_count"] = cursor.fetchone()[0]

        # 输出结果
        if output:
            with open(output, 'w', encoding='utf-8') as f:
                json.dump(audit_data, f, indent=2, ensure_ascii=False)
            console.print(f"[green]✅ 审计数据已导出到: {output}[/green]")
        else:
            console.print(json.dumps(audit_data, indent=2, ensure_ascii=False))

        conn.close()

    except Exception as e:
        console.print(f"[red]❌ 错误: {e}[/red]")


if __name__ == '__main__':
    audit()