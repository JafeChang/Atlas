#!/usr/bin/env python3
"""
Atlas Web 仪表板

提供Web界面查看Atlas系统状态和数据。
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import threading
import time
import webbrowser


class AtlasDashboard:
    """Atlas Web 仪表板"""

    def __init__(self, db_path: str = "data/atlas.db", port: int = 8080):
        self.db_path = db_path
        self.port = port

    def connect_db(self):
        """连接数据库"""
        if not Path(self.db_path).exists():
            return None
        return sqlite3.connect(self.db_path)

    def get_dashboard_data(self) -> Dict:
        """获取仪表板数据"""
        conn = self.connect_db()
        if not conn:
            return {}

        cursor = conn.cursor()
        data = {
            "timestamp": datetime.now().isoformat(),
            "overview": {},
            "sources": [],
            "recent_collections": [],
            "daily_stats": []
        }

        try:
            # 系统概览
            cursor.execute("SELECT COUNT(*) FROM data_sources")
            total_sources = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM data_sources WHERE enabled = 1")
            enabled_sources = cursor.fetchone()[0]

            cursor.execute("SELECT SUM(collection_count), SUM(success_count), SUM(error_count) FROM data_sources")
            coll_result = cursor.fetchone()

            cursor.execute("SELECT COUNT(*) FROM raw_documents")
            raw_docs = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM processed_documents")
            processed_docs = cursor.fetchone()[0]

            # 今日数据
            today = datetime.now().date().isoformat()
            cursor.execute("SELECT COUNT(*) FROM raw_documents WHERE DATE(collected_at) = ?", (today,))
            today_docs = cursor.fetchone()[0]

            data["overview"] = {
                "total_sources": total_sources,
                "enabled_sources": enabled_sources,
                "total_collections": coll_result[0] or 0,
                "successful_collections": coll_result[1] or 0,
                "failed_collections": coll_result[2] or 0,
                "raw_documents": raw_docs,
                "processed_documents": processed_docs,
                "today_documents": today_docs
            }

            # 数据源详情
            cursor.execute('''
                SELECT name, source_type, enabled, collection_count,
                       success_count, error_count, last_success_at, last_error,
                       created_at, url
                FROM data_sources
                ORDER BY enabled DESC, name
            ''')

            for row in cursor.fetchall():
                data["sources"].append({
                    "name": row[0],
                    "type": row[1],
                    "enabled": bool(row[2]),
                    "collections": row[3] or 0,
                    "successes": row[4] or 0,
                    "errors": row[5] or 0,
                    "last_success": row[6],
                    "last_error": row[7],
                    "created_at": row[8],
                    "url": row[9]
                })

            # 最近采集任务
            cursor.execute('''
                SELECT source_id, created_at, started_at, completed_at,
                       status, items_collected, items_processed, items_failed,
                   error_message
                FROM collection_tasks
                ORDER BY created_at DESC
                LIMIT 10
            ''')

            for row in cursor.fetchall():
                data["recent_collections"].append({
                    "source_id": row[0],
                    "created_at": row[1],
                    "started_at": row[2],
                    "completed_at": row[3],
                    "status": row[4],
                    "items_collected": row[5] or 0,
                    "items_processed": row[6] or 0,
                    "items_failed": row[7] or 0,
                    "error_message": row[8]
                })

        except Exception as e:
            print(f"获取数据失败: {e}")
        finally:
            conn.close()

        return data

    def generate_html(self) -> str:
        """生成仪表板HTML"""
        data = self.get_dashboard_data()

        html_template = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Atlas 系统仪表板</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px 0;
            text-align: center;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        }

        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
        }

        .header p {
            font-size: 1.2em;
            opacity: 0.9;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            text-align: center;
            transition: transform 0.2s;
        }

        .stat-card:hover {
            transform: translateY(-5px);
        }

        .stat-number {
            font-size: 2.5em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }

        .stat-label {
            color: #666;
            font-size: 1em;
        }

        .section {
            background: white;
            margin-bottom: 30px;
            border-radius: 10px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
            overflow: hidden;
        }

        .section-header {
            background: #667eea;
            color: white;
            padding: 15px 20px;
            font-size: 1.2em;
            font-weight: bold;
        }

        .section-content {
            padding: 20px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }

        th {
            background-color: #f8f9fa;
            font-weight: 600;
            color: #495057;
        }

        .status-enabled {
            color: #28a745;
            font-weight: bold;
        }

        .status-disabled {
            color: #dc3545;
            font-weight: bold;
        }

        .status-success {
            background-color: #d4edda;
            color: #155724;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.9em;
        }

        .status-failed {
            background-color: #f8d7da;
            color: #721c24;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.9em;
        }

        .status-pending {
            background-color: #fff3cd;
            color: #856404;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.9em;
        }

        .refresh-btn {
            background: #667eea;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1em;
            transition: background 0.2s;
        }

        .refresh-btn:hover {
            background: #5a67d8;
        }

        .auto-refresh {
            margin-left: 10px;
            font-size: 0.9em;
            color: #666;
        }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: #666;
        }

        .progress-bar {
            width: 100%;
            height: 8px;
            background-color: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            background-color: #28a745;
            transition: width 0.3s ease;
        }

        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }

            .header h1 {
                font-size: 2em;
            }

            .stats-grid {
                grid-template-columns: 1fr;
            }

            table {
                font-size: 0.9em;
            }

            th, td {
                padding: 8px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🗄️ Atlas 系统仪表板</h1>
            <p>个人信息聚合系统监控中心</p>
            <div style="margin-top: 20px;">
                <button class="refresh-btn" onclick="location.reload()">🔄 刷新数据</button>
                <span class="auto-refresh">最后更新: {timestamp}</span>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-number">{total_sources}</div>
                <div class="stat-label">📡 数据源总数</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{enabled_sources}</div>
                <div class="stat-label">✅ 启用数据源</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{total_collections}</div>
                <div class="stat-label">🔄 总采集次数</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{success_rate}%</div>
                <div class="stat-label">📈 采集成功率</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{raw_documents}</div>
                <div class="stat-label">📄 原始文档</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{today_documents}</div>
                <div class="stat-label">🗓️ 今日新增</div>
            </div>
        </div>

        <div class="section">
            <div class="section-header">📡 数据源状态</div>
            <div class="section-content">
                {sources_table}
            </div>
        </div>

        <div class="section">
            <div class="section-header">📋 最近采集任务</div>
            <div class="section-content">
                {collections_table}
            </div>
        </div>
    </div>

    <script>
        // 自动刷新
        setTimeout(() => {
            location.reload();
        }, 60000); // 60秒后自动刷新
    </script>
</body>
</html>
        """

        # 计算成功率
        total_coll = data["overview"].get("total_collections", 0)
        success_coll = data["overview"].get("successful_collections", 0)
        success_rate = round((success_coll / total_coll * 100), 1) if total_coll > 0 else 0

        # 生成数据源表格
        sources_table = ""
        if data["sources"]:
            sources_table = '<table><thead><tr><th>名称</th><th>类型</th><th>状态</th><th>采集次数</th><th>成功率</th><th>最后成功</th></tr></thead><tbody>'
            for source in data["sources"]:
                status_class = "status-enabled" if source["enabled"] else "status-disabled"
                status_text = "✅ 启用" if source["enabled"] else "❌ 禁用"

                total = source["collections"]
                successes = source["successes"]
                source_success_rate = round((successes / total * 100), 1) if total > 0 else 0

                last_success = source["last_success"][:16] if source["last_success"] else "N/A"

                sources_table += f'''
                <tr>
                    <td>{source["name"]}</td>
                    <td>{source["type"]}</td>
                    <td><span class="{status_class}">{status_text}</span></td>
                    <td>{total}</td>
                    <td>{source_success_rate}%</td>
                    <td>{last_success}</td>
                </tr>
                '''
            sources_table += '</tbody></table>'
        else:
            sources_table = '<div class="empty-state">暂无数据源信息</div>'

        # 生成采集任务表格
        collections_table = ""
        if data["recent_collections"]:
            collections_table = '<table><thead><tr><th>数据源</th><th>开始时间</th><th>状态</th><th>采集/处理/失败</th><th>耗时</th></tr></thead><tbody>'
            for task in data["recent_collections"]:
                started_at = task["started_at"][:16] if task["started_at"] else "N/A"

                status_class = {
                    "completed": "status-success",
                    "failed": "status-failed",
                    "running": "status-pending",
                    "pending": "status-pending"
                }.get(task["status"], "status-pending")

                status_text = {
                    "completed": "✅ 完成",
                    "failed": "❌ 失败",
                    "running": "🔄 运行中",
                    "pending": "⏳ 等待中"
                }.get(task["status"], task["status"])

                items_str = f"{task['items_collected']}/{task['items_processed']}/{task['items_failed']}"

                # 计算耗时
                if task["started_at"] and task["completed_at"]:
                    try:
                        start = datetime.fromisoformat(task["started_at"].replace('Z', '+00:00'))
                        end = datetime.fromisoformat(task["completed_at"].replace('Z', '+00:00'))
                        duration = (end - start).total_seconds()
                        duration_str = f"{duration:.0f}s"
                    except:
                        duration_str = "N/A"
                else:
                    duration_str = "N/A"

                collections_table += f'''
                <tr>
                    <td>{task["source_id"]}</td>
                    <td>{started_at}</td>
                    <td><span class="{status_class}">{status_text}</span></td>
                    <td>{items_str}</td>
                    <td>{duration_str}</td>
                </tr>
                '''
            collections_table += '</tbody></table>'
        else:
            collections_table = '<div class="empty-state">暂无采集任务记录</div>'

        # 填充模板
        return html_template.format(
            timestamp=data["timestamp"][:19],
            total_sources=data["overview"].get("total_sources", 0),
            enabled_sources=data["overview"].get("enabled_sources", 0),
            total_collections=data["overview"].get("total_collections", 0),
            success_rate=success_rate,
            raw_documents=data["overview"].get("raw_documents", 0),
            today_documents=data["overview"].get("today_documents", 0),
            sources_table=sources_table,
            collections_table=collections_table
        )

    def save_dashboard(self, filename: str = "dashboard.html"):
        """保存仪表板到HTML文件"""
        html_content = self.generate_html()

        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html_content)

        print(f"✅ 仪表板已保存到: {filename}")
        return filename

    def start_server(self):
        """启动Web服务器"""
        class DashboardHandler(SimpleHTTPRequestHandler):
            def __init__(self, dashboard, *args, **kwargs):
                self.dashboard = dashboard
                super().__init__(*args, **kwargs)

            def do_GET(self):
                if self.path == '/' or self.path == '/dashboard':
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html; charset=utf-8')
                    self.end_headers()

                    html_content = self.dashboard.generate_html()
                    self.wfile.write(html_content.encode('utf-8'))
                else:
                    super().do_GET()

        handler = lambda *args, **kwargs: DashboardHandler(self, *args, **kwargs)

        server = HTTPServer(('localhost', self.port), handler)
        print(f"🚀 Atlas 仪表板已启动: http://localhost:{self.port}")
        print("按 Ctrl+C 停止服务器")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 仪表板服务器已停止")
            server.shutdown()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Atlas Web 仪表板")
    parser.add_argument("--db", default="data/atlas.db", help="数据库文件路径")
    parser.add_argument("--port", type=int, default=8080, help="Web服务器端口")
    parser.add_argument("--output", help="保存HTML文件路径")
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")

    args = parser.parse_args()

    dashboard = AtlasDashboard(args.db, args.port)

    if args.output:
        filename = dashboard.save_dashboard(args.output)
        if args.open:
            import webbrowser
            webbrowser.open(f'file://{Path.cwd() / filename}')
    else:
        # 启动Web服务器
        if args.open:
            threading.Timer(1, lambda: webbrowser.open(f'http://localhost:{args.port}')).start()
        dashboard.start_server()


if __name__ == "__main__":
    main()