"""
Atlas Web后台服务

提供REST API和前端界面，用于管理和监控Atlas系统。
"""

from flask import Flask, request, jsonify, render_template_string, send_from_directory
from flask_cors import CORS
import sqlite3
import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import threading
import time

# 导入调度器
from .scheduler import TaskScheduler

# 导入统一存储接口
try:
    from ..core.unified_storage import get_unified_storage
    from ..core.config import get_config
    UNIFIED_STORAGE_AVAILABLE = True
except ImportError:
    UNIFIED_STORAGE_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("统一存储接口不可用")

# 设置日志
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# 创建Flask应用
app = Flask(__name__,
           template_folder=str(Path(__file__).parent / "templates"),
           static_folder=str(Path(__file__).parent / "static"))

# 启用CORS
CORS(app)

# 配置
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'atlas-dev-secret-key')
app.config['JSON_AS_ASCII'] = False


class AtlasAPI:
    """Atlas API服务类"""

    def __init__(self, db_path: str = "data/atlas.db"):
        self.db_path = db_path
        self.scheduler = TaskScheduler()
        self._init_scheduler()

    def _init_scheduler(self):
        """初始化调度器"""
        try:
            # 添加默认的采集任务示例
            self.scheduler.add_task(
                name="定时采集测试",
                func=self._scheduled_collection,
                cron_expression="*/30 * * * *",  # 每30分钟执行一次
                description="定期执行数据采集任务"
            )
            logger.info("调度器初始化完成")
        except Exception as e:
            logger.error(f"调度器初始化失败: {e}")

    def _scheduled_collection(self):
        """调度器执行的采集任务"""
        try:
            logger.info("执行定时采集任务")
            result = self.trigger_collection()
            logger.info(f"定时采集完成: {result}")
        except Exception as e:
            logger.error(f"定时采集任务失败: {e}")

    def connect_db(self):
        """连接数据库"""
        if not Path(self.db_path).exists():
            return None
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def get_system_overview(self) -> Dict:
        """获取系统概览"""
        conn = self.connect_db()
        if not conn:
            return {"error": "数据库连接失败"}

        cursor = conn.cursor()

        try:
            # 数据库文件大小
            db_size = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0

            # 数据源统计
            cursor.execute("SELECT COUNT(*) FROM data_sources")
            total_sources = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM data_sources WHERE enabled = 1")
            enabled_sources = cursor.fetchone()[0]

            # 采集统计
            cursor.execute("SELECT SUM(collection_count), SUM(success_count), SUM(error_count) FROM data_sources")
            coll_result = cursor.fetchone()

            # 文档统计
            cursor.execute("SELECT COUNT(*) FROM raw_documents")
            raw_docs = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM processed_documents")
            processed_docs = cursor.fetchone()[0]

            # 今日新增
            today = datetime.now().date().isoformat()
            cursor.execute("SELECT COUNT(*) FROM raw_documents WHERE DATE(collected_at) = ?", (today,))
            today_docs = cursor.fetchone()[0]

            # 存储文件统计
            data_dir = Path("data")
            storage_files = 0
            storage_size = 0
            if data_dir.exists():
                json_files = list(data_dir.rglob("*.json"))
                storage_files = len(json_files)
                storage_size = sum(f.stat().st_size for f in json_files)

            result = {
                "timestamp": datetime.now().isoformat(),
                "database_size": db_size,
                "storage_files": storage_files,
                "storage_size": storage_size,
                "data_sources": {
                    "total": total_sources,
                    "enabled": enabled_sources,
                    "disabled": total_sources - enabled_sources
                },
                "collections": {
                    "total": coll_result[0] or 0,
                    "successful": coll_result[1] or 0,
                    "failed": coll_result[2] or 0
                },
                "documents": {
                    "raw": raw_docs,
                    "processed": processed_docs,
                    "today": today_docs
                }
            }

            # 添加统一存储统计信息
            if UNIFIED_STORAGE_AVAILABLE:
                try:
                    import asyncio
                    storage = get_unified_storage()
                    result["storage_info"] = storage.get_storage_info()
                    result["storage_type"] = storage.get_storage_type()

                    # 获取存储统计
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # 如果在运行中的事件循环中，创建新线程运行
                            import concurrent.futures
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                storage_stats = executor.submit(asyncio.run, storage.get_storage_stats()).result()
                                result["storage_stats"] = storage_stats
                        else:
                            storage_stats = loop.run_until_complete(storage.get_storage_stats())
                            result["storage_stats"] = storage_stats
                    except Exception as stats_error:
                        logger.warning(f"获取存储统计失败: {stats_error}")
                        result["storage_stats"] = {"error": str(stats_error)}

                except Exception as storage_error:
                    logger.warning(f"获取存储信息失败: {storage_error}")
                    result["storage_info"] = {"error": str(storage_error)}

            return result

        except Exception as e:
            logger.error(f"获取系统概览失败: {e}")
            return {"error": str(e)}
        finally:
            conn.close()

    def get_data_sources(self) -> List[Dict]:
        """获取数据源列表"""
        conn = self.connect_db()
        if not conn:
            return []

        cursor = conn.cursor()

        try:
            cursor.execute('''
                SELECT name, source_type, enabled, collection_count,
                       success_count, error_count, last_success_at, last_error,
                       created_at, updated_at, url, description
                FROM data_sources
                ORDER BY enabled DESC, name
            ''')

            sources = []
            for row in cursor.fetchall():
                sources.append({
                    "name": row[0],
                    "type": row[1],
                    "enabled": bool(row[2]),
                    "collections": row[3] or 0,
                    "successes": row[4] or 0,
                    "errors": row[5] or 0,
                    "last_success": row[6],
                    "last_error": row[7],
                    "created_at": row[8],
                    "updated_at": row[9],
                    "url": row[10],
                    "description": row[11] or ""
                })

            return sources

        except Exception as e:
            logger.error(f"获取数据源失败: {e}")
            return []
        finally:
            conn.close()

    def get_recent_collections(self, limit: int = 20) -> List[Dict]:
        """获取最近的采集任务"""
        conn = self.connect_db()
        if not conn:
            return []

        cursor = conn.cursor()

        try:
            cursor.execute('''
                SELECT source_id, created_at, started_at, completed_at,
                       status, items_collected, items_processed, items_failed,
                       error_message
                FROM collection_tasks
                ORDER BY created_at DESC
                LIMIT ?
            ''', (limit,))

            tasks = []
            for row in cursor.fetchall():
                tasks.append({
                    "source_id": row[0],
                    "created_at": row[1],
                    "started_at": row[2],
                    "completed_at": row[3],
                    "status": row[4],
                    "items_collected": row[5] or 0,
                    "items_processed": row[6] or 0,
                    "items_failed": row[7] or 0,
                    "error_message": row[8] or ""
                })

            return tasks

        except Exception as e:
            logger.error(f"获取采集任务失败: {e}")
            return []
        finally:
            conn.close()

    def get_collected_data(self, limit: int = 50) -> List[Dict]:
        """获取采集的数据"""
        conn = self.connect_db()
        if not conn:
            return []

        cursor = conn.cursor()

        try:
            cursor.execute('''
                SELECT id, source_id, source_url, title, collected_at, processing_status
                FROM raw_documents
                ORDER BY collected_at DESC
                LIMIT ?
            ''', (limit,))

            documents = []
            for row in cursor.fetchall():
                documents.append({
                    "id": row[0],
                    "source_id": row[1],
                    "source_url": row[2],
                    "title": row[3] or "",
                    "collected_at": row[4],
                    "processing_status": row[5]
                })

            return documents

        except Exception as e:
            logger.error(f"获取文档数据失败: {e}")
            return []
        finally:
            conn.close()

    def get_file_data(self) -> List[Dict]:
        """获取文件数据"""
        data_dir = Path("data")
        if not data_dir.exists():
            return []

        files = []
        json_files = list(data_dir.rglob("*.json"))

        for json_file in json_files:
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                file_size = json_file.stat().st_size
                files.append({
                    "path": str(json_file.relative_to(Path.cwd())),
                    "size": file_size,
                    "source": data.get('source_name', 'Unknown'),
                    "items_count": len(data.get('items', [])),
                    "collected_at": data.get('collected_at', ''),
                    "url": data.get('url', '')
                })

            except Exception as e:
                logger.error(f"读取文件失败 {json_file}: {e}")
                continue

        # 按修改时间排序
        files.sort(key=lambda x: x['collected_at'], reverse=True)
        return files

    def trigger_collection(self, source_name: Optional[str] = None) -> Dict:
        """触发数据采集"""
        logger.info(f"开始采集数据源: {source_name}")
        try:
            import asyncio
            import sys
            from pathlib import Path
            from ..collectors import CollectorFactory
            from ..core.config import CollectionConfig

            # 添加项目根目录到Python路径
            project_root = Path(__file__).parent.parent.parent.parent
            sys.path.insert(0, str(project_root / "src"))
            logger.debug(f"Python路径已添加")

            # 创建采集配置
            collection_config = CollectionConfig()
            collection_config.rate_limit_delay = 1  # 设置较短的等待时间
            factory = CollectorFactory()

            if source_name:
                # 采集单个数据源
                logger.info(f"开始采集数据源: {source_name}")

                # 加载数据源配置
                conn = self.connect_db()
                if not conn:
                    return {"status": "error", "message": "数据库连接失败"}

                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM data_sources WHERE name = ? AND enabled = 1
                ''', (source_name,))

                source_data = cursor.fetchone()
                if not source_data:
                    return {"status": "error", "message": f"数据源 {source_name} 不存在或已禁用"}

                # 构建数据源配置字典
                columns = [desc[0] for desc in cursor.description]
                logger.info(f"数据库列: {columns}")
                logger.info(f"源数据长度: {len(source_data)}")

                def safe_get(column_name):
                    try:
                        value = source_data[columns.index(column_name)]
                        logger.debug(f"获取列 {column_name}: {type(value)} = {value}")
                        return value
                    except (ValueError, IndexError):
                        logger.warning(f"列 {column_name} 未找到")
                        return None

                # 处理JSON字段
                import json

                config_value = safe_get('config')
                logger.info(f"原始config: {config_value} (类型: {type(config_value)})")
                if isinstance(config_value, str):
                    try:
                        config_value = json.loads(config_value)
                        logger.info(f"解析后config: {config_value}")
                    except Exception as e:
                        logger.warning(f"config解析失败: {e}")
                        config_value = {}

                tags_value = safe_get('tags')
                logger.info(f"原始tags: {tags_value} (类型: {type(tags_value)})")
                if isinstance(tags_value, str):
                    try:
                        tags_value = json.loads(tags_value)
                        logger.info(f"解析后tags: {tags_value}")
                    except Exception as e:
                        logger.warning(f"tags解析失败: {e}")
                        tags_value = []

                source_dict = {
                    'name': safe_get('name'),
                    'type': safe_get('source_type'),
                    'url': safe_get('url'),
                    'enabled': safe_get('enabled'),
                    'collection_interval': safe_get('collection_interval'),
                    'max_items_per_run': safe_get('max_items_per_run'),
                    'retry_count': safe_get('retry_count'),
                    'timeout': safe_get('timeout'),
                    'tags': tags_value,
                    'category': safe_get('category'),
                    'language': safe_get('language'),
                    'config': config_value,
                    'created_at': safe_get('created_at'),
                    'updated_at': safe_get('updated_at'),
                    'last_collected_at': safe_get('last_collected_at'),
                    'last_success_at': safe_get('last_success_at'),
                    'collection_count': safe_get('collection_count'),
                    'success_count': safe_get('success_count'),
                    'error_count': safe_get('error_count'),
                    'last_error': safe_get('last_error'),
                }

                conn.close()

                # 创建采集器
                collector = factory.create_collector_with_config(source_dict, collection_config)

                # 执行采集
                logger.info(f"正在使用采集器: {type(collector).__name__}")

                # 使用同步方式执行采集
                try:
                    result = collector.collect(source_dict)

                    if result and isinstance(result, list):
                        # 保存数据
                        collector.save_results(result, f"data/raw/{source_name}", source_name)

                        # 更新数据库
                        self.update_collection_stats(source_name, len(result), True, None)

                        message = f"采集完成，获得 {len(result)} 条数据"
                        return {"status": "success", "source": source_name, "items_count": len(result), "message": message}
                    else:
                        self.update_collection_stats(source_name, 0, False, "未获取到数据")
                        return {"status": "completed", "source": source_name, "items_count": 0, "message": "采集完成，但未获取到数据"}

                except Exception as collect_error:
                    logger.error(f"采集过程出错: {collect_error}")
                    self.update_collection_stats(source_name, 0, False, str(collect_error))
                    return {"status": "error", "source": source_name, "items_count": 0, "message": f"采集失败: {str(collect_error)}"}

            else:
                # 采集所有启用的数据源
                conn = self.connect_db()
                if not conn:
                    return {"status": "error", "message": "数据库连接失败"}

                cursor = conn.cursor()
                cursor.execute('''
                    SELECT name FROM data_sources WHERE enabled = 1
                ''')

                enabled_sources = [row[0] for row in cursor.fetchall()]
                conn.close()

                if not enabled_sources:
                    return {"status": "warning", "message": "没有启用的数据源"}

                # 同步采集所有数据源
                def collect_all_sources():
                    results = []
                    for source in enabled_sources:
                        try:
                            # 重新加载每个数据源的配置
                            result = self.collect_single_source(source)
                            results.append(result)
                        except Exception as e:
                            logger.error(f"采集数据源 {source} 失败: {e}")
                            results.append({"source": source, "status": "error", "message": str(e)})
                    return results

                # 执行采集
                results = collect_all_sources()

                # 统计结果
                total_items = sum(r.get("items_count", 0) for r in results if r.get("status") != "error")
                success_count = sum(1 for r in results if r.get("status") == "success")
                error_count = sum(1 for r in results if r.get("status") == "error")

                return {
                    "status": "completed",
                    "message": f"批量采集完成，{success_count}个成功，{error_count}个失败",
                    "total_sources": len(enabled_sources),
                    "total_items": total_items,
                    "results": results
                }

        except Exception as e:
            logger.error(f"触发采集失败: {e}")
            return {"status": "error", "message": str(e)}

    def collect_single_source(self, source_name: str) -> Dict:
        """采集单个数据源的辅助方法"""
        import sys
        from pathlib import Path
        from ..collectors import CollectorFactory
        from ..core.config import CollectionConfig

        # 添加项目根目录到Python路径
        project_root = Path(__file__).parent.parent.parent.parent
        sys.path.insert(0, str(project_root / "src"))

        # 获取数据源配置
        conn = self.connect_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM data_sources WHERE name = ? AND enabled = 1', (source_name,))
        source_data = cursor.fetchone()
        conn.close()

        if not source_data:
            return {"status": "error", "source": source_name, "message": f"数据源 {source_name} 不存在或已禁用"}

        # 构建数据源配置字典
        columns = [description[0] for description in cursor.description]
        source_dict = {
            'name': source_data[columns.index('name')],
            'type': source_data[columns.index('source_type')],
            'url': source_data[columns.index('url')],
            'enabled': source_data[columns.index('enabled')],
            'collection_interval': source_data[columns.index('collection_interval')],
            'max_items_per_run': source_data[columns.index('max_items_per_run')],
            'retry_count': source_data[columns.index('retry_count')],
            'timeout': source_data[columns.index('timeout')],
            'tags': source_data[columns.index('tags')],
            'category': source_data[columns.index('category')],
            'language': source_data[columns.index('language')],
            'config': source_data[columns.index('config')],
            'created_at': source_data[columns.index('created_at')],
            'updated_at': source_data[columns.index('updated_at')],
            'last_collected_at': source_data[columns.index('last_collected_at')],
            'last_success_at': source_data[columns.index('last_success_at')],
            'collection_count': source_data[columns.index('collection_count')],
            'success_count': source_data[columns.index('success_count')],
            'error_count': source_data[columns.index('error_count')],
            'last_error': source_data[columns.index('last_error')],
        }

        # 创建采集器
        collection_config = CollectionConfig()
        collection_config.rate_limit_delay = 1  # 设置较短的等待时间
        factory = CollectorFactory()
        collector = factory.create_collector_with_config(source_dict, collection_config)

        # 执行采集 - 使用同步方法避免协程问题
        result = collector.collect(source_dict)

        if result and isinstance(result, list):
            # 保存数据
            collector.save_results(result, f"data/raw/{source_name}")
            # 更新数据库
            self.update_collection_stats(source_name, len(result), True, None)

            return {
                "status": "success",
                "source": source_name,
                "items_count": len(result),
                "message": f"采集完成，获得 {len(result)} 条数据"
            }
        else:
            self.update_collection_stats(source_name, 0, False, "未获取到数据")
            return {
                "status": "completed",
                "source": source_name,
                "items_count": 0,
                "message": "采集完成，但未获取到数据"
            }

    def update_collection_stats(self, source_name: str, items_count: int, success: bool, error_message: Optional[str]):
        """更新采集统计信息"""
        conn = self.connect_db()
        if not conn:
            return

        try:
            cursor = conn.cursor()

            # 更新collection_tasks表
            cursor.execute('''
                INSERT INTO collection_tasks
                (source_id, task_type, status, created_at, started_at, completed_at,
                 items_collected, error_message)
                VALUES (?, 'manual', ?, ?, ?, ?, ?, ?)
            ''', (
                source_name,
                'success' if success else 'failed',
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                items_count,
                error_message or ''
            ))

            # 更新data_sources表
            increment_field = 'success_count' if success else 'collection_count'

            if success:
                cursor.execute(f'''
                    UPDATE data_sources
                    SET {increment_field} = {increment_field} + 1,
                        last_success_at = ?,
                        last_error = NULL
                    WHERE name = ?
                ''', (datetime.now().isoformat(), source_name))
            else:
                cursor.execute('''
                    UPDATE data_sources
                    SET collection_count = collection_count + 1,
                        last_error = ?,
                        error_count = error_count + 1
                    WHERE name = ?
                ''', (error_message or "未知错误", source_name))

            conn.commit()
            logger.info(f"更新采集统计: {source_name}, items={items_count}, success={success}")

        except Exception as e:
            logger.error(f"更新采集统计失败: {e}")
        finally:
            conn.close()

    def export_data(self, format_type: str = "json") -> Dict:
        """导出数据"""
        try:
            if format_type == "json":
                api = AtlasAPI()
                data = {
                    "export_time": datetime.now().isoformat(),
                    "overview": api.get_system_overview(),
                    "sources": api.get_data_sources(),
                    "recent_collections": api.get_recent_collections(50)
                }
                return {"status": "success", "data": data}
            else:
                return {"status": "error", "message": f"不支持的导出格式: {format_type}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# 创建API实例
atlas_api = AtlasAPI()


# HTML模板
INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Atlas 管理后台</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div class="header">
        <div class="header-content">
            <h1>🗄️ Atlas 管理后台</h1>
            <p>个人信息聚合系统监控与管理中心</p>
            <div class="header-actions">
                <button class="btn" onclick="location.reload()">
                    🔄 刷新页面
                </button>
                <a href="/health" class="btn btn-outline" target="_blank">
                    🏥 健康检查
                </a>
            </div>
        </div>
    </div>

    <div class="container">
        <!-- 系统概览 -->
        <div id="overview-loading" class="loading">
            <div>🔄 加载系统概览...</div>
        </div>

        <div id="overview-content" style="display: none;">
            <div class="dashboard">
                <div class="card">
                    <h3>📡 数据源</h3>
                    <div class="stat-value" id="data-sources-count">-</div>
                    <div class="stat-label">总数 (启用)</div>
                </div>
                <div class="card">
                    <h3>📄 采集文档</h3>
                    <div class="stat-value" id="documents-count">-</div>
                    <div class="stat-label">原始 (处理)</div>
                </div>
                <div class="card">
                    <h3>🔄 采集统计</h3>
                    <div class="stat-value" id="collections-count">-</div>
                    <div class="stat-label">总 (成功)</div>
                </div>
                <div class="card">
                    <h3>🗓️ 今日新增</h3>
                    <div class="stat-value" id="today-count">-</div>
                    <div class="stat-label">文档数量</div>
                </div>
            </div>
        </div>

        <!-- 数据源管理 -->
        <div class="section">
            <h2>
                📡 数据源管理
                <button class="btn" onclick="refreshDataSources()">🔄 刷新</button>
                <button class="btn btn-danger" onclick="startAllCollections()">▶️ 开始采集</button>
            </h2>
            <div id="sources-loading" class="loading">
                <div>🔄 加载数据源...</div>
            </div>
            <div id="sources-content" style="display: none;">
                <table id="sources-table">
                    <thead>
                        <tr>
                            <th>名称</th>
                            <th>类型</th>
                            <th>状态</th>
                            <th>采集次数</th>
                            <th>成功率</th>
                            <th>最后成功</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody id="sources-tbody">
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 采集任务 -->
        <div class="section">
            <h2>
                📋 采集任务
                <button class="btn" onclick="refreshCollections()">🔄 刷新</button>
            </h2>
            <div id="collections-loading" class="loading">
                <div>🔄 加载采集任务...</div>
            </div>
            <div id="collections-content" style="display: none;">
                <table id="collections-table">
                    <thead>
                        <tr>
                            <th>数据源</th>
                            <th>开始时间</th>
                            <th>状态</th>
                            <th>采集/处理/失败</th>
                            <th>耗时</th>
                        </tr>
                    </thead>
                    <tbody id="collections-tbody">
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 采集数据 -->
        <div class="section">
            <h2>
                📄 采集数据
                <button class="btn" onclick="refreshData()">🔄 刷新</button>
                <button class="btn" onclick="exportData()">📥 导出数据</button>
            </h2>
            <div id="data-loading" class="loading">
                <div>🔄 加载数据...</div>
            </div>
            <div id="data-content" style="display: none;">
                <table id="data-table">
                    <thead>
                        <tr>
                            <th>数据源</th>
                            <th>标题</th>
                            <th>采集时间</th>
                            <th>处理状态</th>
                        </tr>
                    </thead>
                    <tbody id="data-tbody">
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 任务调度 -->
        <div class="section">
            <h2>
                ⏰ 任务调度
                <button class="btn" onclick="refreshScheduler()">🔄 刷新</button>
                <button class="btn btn-success" onclick="startScheduler()">▶️ 启动调度器</button>
                <button class="btn btn-warning" onclick="stopScheduler()">⏸️ 停止调度器</button>
                <button class="btn" onclick="showAddTaskDialog()">➕ 添加任务</button>
            </h2>
            <div id="scheduler-status" class="card">
                <h3>调度器状态</h3>
                <div id="scheduler-status-content">加载中...</div>
            </div>
            <div class="card">
                <h3>调度任务列表</h3>
                <div id="scheduler-loading" class="loading">
                    <div>🔄 加载任务...</div>
                </div>
                <div id="scheduler-content" style="display: none;">
                    <table id="scheduler-table">
                        <thead>
                            <tr>
                                <th>任务名称</th>
                                <th>Cron表达式</th>
                                <th>描述</th>
                                <th>状态</th>
                                <th>上次运行</th>
                                <th>下次运行</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody id="scheduler-tbody">
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 文件管理 -->
        <div class="section">
            <h2>
                📁 文件管理
                <button class="btn" onclick="refreshFiles()">🔄 刷新</button>
            </h2>
            <div id="files-loading" class="loading">
                <div>🔄 加载文件...</div>
            </div>
            <div id="files-content" style="display: none;">
                <table id="files-table">
                    <thead>
                        <tr>
                            <th>文件路径</th>
                            <th>数据源</th>
                            <th>项目数</th>
                            <th>文件大小</th>
                            <th>采集时间</th>
                        </tr>
                    </thead>
                    <tbody id="files-tbody">
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 存储管理 -->
        <div class="section">
            <h2>
                💾 存储管理
                <button class="btn" onclick="refreshStorageStats()">🔄 刷新</button>
            </h2>
            <div id="storage-loading" class="loading">
                <div>🔄 加载存储信息...</div>
            </div>
            <div id="storage-content" style="display: none;">
                <div class="dashboard">
                    <div class="card">
                        <h3>📊 存储类型</h3>
                        <div class="stat-value" id="storage-type">-</div>
                        <div class="stat-label">当前存储后端</div>
                    </div>
                    <div class="card">
                        <h3>📦 存储桶/目录</h3>
                        <div class="stat-value" id="storage-bucket" style="font-size: 0.9rem;">-</div>
                        <div class="stat-label">存储位置</div>
                    </div>
                </div>
                <div id="storage-stats-content" style="margin-top: 1.5rem;">
                    <h3>存储统计详情</h3>
                    <pre id="storage-stats-json" style="background: #f5f5f5; padding: 1rem; border-radius: 4px; overflow-x: auto;"></pre>
                </div>
            </div>
        </div>
    </div>

    <script>
        // 全局变量
        let refreshInterval;

        // 页面加载时初始化
        document.addEventListener('DOMContentLoaded', function() {
            loadOverview();
            loadDataSources();
            loadCollections();
            loadData();
            loadFiles();
            loadStorageStats();
            loadSchedulerStatus();
            loadSchedulerTasks();

            // 设置自动刷新
            refreshInterval = setInterval(function() {
                loadOverview();
                loadCollections();
            }, 30000); // 30秒刷新一次
        });

        // 加载系统概览
        async function loadOverview() {
            try {
                const response = await fetch('/api/overview');
                const data = await response.json();

                if (data.error) {
                    console.error('加载概览失败:', data.error);
                    return;
                }

                document.getElementById('overview-loading').style.display = 'none';
                document.getElementById('overview-content').style.display = 'block';

                // 更新统计数据
                document.getElementById('data-sources-count').textContent =
                    `${data.data_sources.enabled}/${data.data_sources.total}`;
                document.getElementById('documents-count').textContent =
                    `${data.documents.raw}/${data.documents.processed}`;
                document.getElementById('collections-count').textContent =
                    `${data.collections.total}/${data.collections.successful}`;
                document.getElementById('today-count').textContent =
                    data.documents.today;

            } catch (error) {
                console.error('加载概览失败:', error);
            }
        }

        // 加载数据源
        async function loadDataSources() {
            try {
                const response = await fetch('/api/sources');
                const sources = await response.json();

                document.getElementById('sources-loading').style.display = 'none';
                document.getElementById('sources-content').style.display = 'block';

                const tbody = document.getElementById('sources-tbody');
                tbody.innerHTML = '';

                if (sources.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无数据源</td></tr>';
                    return;
                }

                sources.forEach(source => {
                    const tr = document.createElement('tr');

                    const statusClass = source.enabled ? 'status-enabled' : 'status-disabled';
                    const statusText = source.enabled ? '启用' : '禁用';

                    const total = source.collections;
                    const successes = source.successes;
                    const successRate = total > 0 ? ((successes / total) * 100).toFixed(1) : 'N/A';

                    const lastSuccess = source.last_success ?
                        new Date(source.last_success).toLocaleString() : 'N/A';

                    tr.innerHTML = `
                        <td><strong>${source.name}</strong><br><small>${source.description}</small></td>
                        <td>${source.type}</td>
                        <td><span class="${statusClass}">${statusText}</span></td>
                        <td>${total}</td>
                        <td>${successRate}%</td>
                        <td>${lastSuccess}</td>
                        <td>
                            <button class="btn btn-small" onclick="startCollection('${source.name}')">
                                ▶️ 采集
                            </button>
                        </td>
                    `;

                    tbody.appendChild(tr);
                });

            } catch (error) {
                console.error('加载数据源失败:', error);
            }
        }

        // 加载采集任务
        async function loadCollections() {
            try {
                const response = await fetch('/api/collections');
                const collections = await response.json();

                document.getElementById('collections-loading').style.display = 'none';
                document.getElementById('collections-content').style.display = 'block';

                const tbody = document.getElementById('collections-tbody');
                tbody.innerHTML = '';

                if (collections.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无采集任务</td></tr>';
                    return;
                }

                collections.forEach(task => {
                    const tr = document.createElement('tr');

                    const statusClass = {
                        'completed': 'status-completed',
                        'failed': 'status-failed',
                        'running': 'status-completed',
                        'pending': 'status-completed'
                    }[task.status] || 'status-completed';

                    const statusText = {
                        'completed': '✅ 完成',
                        'failed': '❌ 失败',
                        'running': '🔄 运行中',
                        'pending': '⏳ 等待中'
                    }[task.status] || task.status;

                    const startTime = task.started_at ?
                        new Date(task.started_at).toLocaleString() : 'N/A';

                    const duration = task.completed_at && task.started_at ?
                        Math.round((new Date(task.completed_at) - new Date(task.started_at)) / 1000) + 's' : 'N/A';

                    tr.innerHTML = `
                        <td>${task.source_id}</td>
                        <td>${startTime}</td>
                        <td><span class="${statusClass}">${statusText}</span></td>
                        <td>${task.items_collected}/${task.items_processed}/${task.items_failed}</td>
                        <td>${duration}</td>
                    `;

                    tbody.appendChild(tr);
                });

            } catch (error) {
                console.error('加载采集任务失败:', error);
            }
        }

        // 加载文档数据
        async function loadData() {
            try {
                const response = await fetch('/api/data');
                const data = await response.json();

                document.getElementById('data-loading').style.display = 'none';
                document.getElementById('data-content').style.display = 'block';

                const tbody = document.getElementById('data-tbody');
                tbody.innerHTML = '';

                if (data.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="4" class="empty">暂无文档数据</td></tr>';
                    return;
                }

                data.forEach(doc => {
                    const tr = document.createElement('tr');

                    const collectedAt = doc.collected_at ?
                        new Date(doc.collected_at).toLocaleString() : 'N/A';

                    tr.innerHTML = `
                        <td>${doc.source_id}</td>
                        <td>${doc.source_url ? `<a href="${doc.source_url}" target="_blank" rel="noopener noreferrer">${doc.title || '无标题'}</a>` : (doc.title || '无标题')}</td>
                        <td>${collectedAt}</td>
                        <td><span class="status-${doc.processing_status}">${doc.processing_status}</span></td>
                    `;

                    tbody.appendChild(tr);
                });

            } catch (error) {
                console.error('加载文档数据失败:', error);
            }
        }

        // 加载文件数据
        async function loadFiles() {
            try {
                const response = await fetch('/api/files');
                const files = await response.json();

                document.getElementById('files-loading').style.display = 'none';
                document.getElementById('files-content').style.display = 'block';

                const tbody = document.getElementById('files-tbody');
                tbody.innerHTML = '';

                if (files.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无文件</td></tr>';
                    return;
                }

                files.forEach(file => {
                    const tr = document.createElement('tr');

                    const collectedAt = file.collected_at ?
                        new Date(file.collected_at).toLocaleString() : 'N/A';

                    tr.innerHTML = `
                        <td><code>${file.path}</code></td>
                        <td>${file.source}</td>
                        <td>${file.items_count}</td>
                        <td>${(file.size / 1024).toFixed(1)} KB</td>
                        <td>${collectedAt}</td>
                    `;

                    tbody.appendChild(tr);
                });

            } catch (error) {
                console.error('加载文件数据失败:', error);
            }
        }

        // 刷新函数
        function refreshDataSources() {
            document.getElementById('sources-loading').style.display = 'block';
            document.getElementById('sources-content').style.display = 'none';
            loadDataSources();
        }

        function refreshCollections() {
            document.getElementById('collections-loading').style.display = 'block';
            document.getElementById('collections-content').style.display = 'none';
            loadCollections();
        }

        function refreshData() {
            document.getElementById('data-loading').style.display = 'block';
            document.getElementById('data-content').style.display = 'none';
            loadData();
        }

        function refreshFiles() {
            document.getElementById('files-loading').style.display = 'block';
            document.getElementById('files-content').style.display = 'none';
            loadFiles();
        }

        // 加载存储统计
        async function loadStorageStats() {
            try {
                const response = await fetch('/api/storage/stats');
                const data = await response.json();

                document.getElementById('storage-loading').style.display = 'none';
                document.getElementById('storage-content').style.display = 'block';

                if (data.status === 'success') {
                    // 更新存储类型
                    const storageTypeMap = {
                        'filesystem': '文件系统',
                        'minio': 'MinIO对象存储'
                    };
                    document.getElementById('storage-type').textContent =
                        storageTypeMap[data.storage_type] || data.storage_type;

                    // 更新存储桶/目录信息
                    if (data.storage_type === 'minio') {
                        document.getElementById('storage-bucket').textContent =
                            data.storage_info.bucket_name || 'N/A';
                    } else {
                        document.getElementById('storage-bucket').textContent =
                            data.storage_info.base_dir || 'N/A';
                    }

                    // 显示存储统计详情
                    document.getElementById('storage-stats-json').textContent =
                        JSON.stringify(data.storage_stats, null, 2);
                } else {
                    document.getElementById('storage-type').textContent = '加载失败';
                    document.getElementById('storage-stats-json').textContent =
                        data.message || '无法加载存储信息';
                }

            } catch (error) {
                console.error('加载存储统计失败:', error);
                document.getElementById('storage-loading').style.display = 'none';
                document.getElementById('storage-content').style.display = 'block';
                document.getElementById('storage-type').textContent = '错误';
                document.getElementById('storage-stats-json').textContent =
                    '加载失败: ' + error.message;
            }
        }

        function refreshStorageStats() {
            document.getElementById('storage-loading').style.display = 'block';
            document.getElementById('storage-content').style.display = 'none';
            loadStorageStats();
        }

        // 开始采集
        async function startCollection(sourceName) {
            try {
                const response = await fetch(`/api/collect/${encodeURIComponent(sourceName)}`, {
                    method: 'POST'
                });
                const result = await response.json();

                alert(`采集状态: ${result.message}`);

                // 刷新采集任务列表
                setTimeout(() => {
                    refreshCollections();
                }, 1000);

            } catch (error) {
                alert('启动采集失败: ' + error.message);
            }
        }

        // 开始所有采集
        async function startAllCollections() {
            if (confirm('确定要开始采集所有启用的数据源吗？')) {
                try {
                    const response = await fetch('/api/collect', {
                        method: 'POST'
                    });
                    const result = await response.json();

                    alert(`采集状态: ${result.message}`);

                    // 刷新相关数据
                    setTimeout(() => {
                        refreshCollections();
                        loadDataSources();
                    }, 1000);

                } catch (error) {
                    alert('启动采集失败: ' + error.message);
                }
            }
        }

        // 导出数据
        async function exportData() {
            try {
                const response = await fetch('/api/export');
                const result = await response.json();

                if (result.status === 'success') {
                    const dataStr = JSON.stringify(result.data, null, 2);
                    const blob = new Blob([dataStr], {type: 'application/json'});
                    const url = URL.createObjectURL(blob);

                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `atlas-export-${new Date().toISOString().split('T')[0]}.json`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);

                    alert('数据导出成功!');
                } else {
                    alert('导出失败: ' + result.message);
                }

            } catch (error) {
                alert('导出失败: ' + error.message);
            }
        }

        // 调度器相关函数
        let schedulerRefreshInterval;

        // 加载调度器状态
        async function loadSchedulerStatus() {
            try {
                const response = await fetch('/api/scheduler/status');
                const data = await response.json();

                const statusContent = document.getElementById('scheduler-status-content');
                statusContent.innerHTML = `
                    <div class="dashboard" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
                        <div class="stat-card">
                            <div class="stat-label">运行状态</div>
                            <div class="stat-value" style="color: ${data.running ? 'var(--success-color)' : 'var(--danger-color)'}">
                                ${data.running ? '运行中' : '已停止'}
                            </div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">任务总数</div>
                            <div class="stat-value">${data.tasks_count}</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-label">启用任务</div>
                            <div class="stat-value">${data.enabled_tasks}</div>
                        </div>
                    </div>
                    <div style="margin-top: 1rem;">
                        <strong>下次运行时间:</strong>
                        <ul style="margin-top: 0.5rem; padding-left: 1.5rem;">
                            ${data.next_run_times.map(time => `<li>${time}</li>`).join('')}
                        </ul>
                    </div>
                `;

            } catch (error) {
                console.error('加载调度器状态失败:', error);
                document.getElementById('scheduler-status-content').innerHTML =
                    '<div style="color: var(--danger-color);">加载失败</div>';
            }
        }

        // 加载调度任务列表
        async function loadSchedulerTasks() {
            try {
                const response = await fetch('/api/scheduler/tasks');
                const data = await response.json();

                document.getElementById('scheduler-loading').style.display = 'none';
                document.getElementById('scheduler-content').style.display = 'block';

                const tbody = document.getElementById('scheduler-tbody');

                if (data.tasks && data.tasks.length > 0) {
                    tbody.innerHTML = data.tasks.map(task => `
                        <tr>
                            <td>${task.name}</td>
                            <td><code>${task.cron}</code></td>
                            <td>${task.description || '-'}</td>
                            <td>
                                <span class="status ${task.enabled ? 'status-enabled' : 'status-disabled'}">
                                    ${task.enabled ? '启用' : '禁用'}
                                </span>
                            </td>
                            <td>${task.last_run ? new Date(task.last_run).toLocaleString() : '-'}</td>
                            <td>${task.next_run ? new Date(task.next_run).toLocaleString() : '-'}</td>
                            <td>
                                <button class="btn btn-sm btn-danger" onclick="removeTask('${task.name}')">
                                    🗑️ 删除
                                </button>
                            </td>
                        </tr>
                    `).join('');
                } else {
                    tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无调度任务</td></tr>';
                }

            } catch (error) {
                console.error('加载调度任务失败:', error);
                document.getElementById('scheduler-loading').innerHTML =
                    '<div style="color: var(--danger-color);">加载失败</div>';
            }
        }

        // 刷新调度器数据
        async function refreshScheduler() {
            await Promise.all([
                loadSchedulerStatus(),
                loadSchedulerTasks()
            ]);
        }

        // 启动调度器
        async function startScheduler() {
            try {
                const response = await fetch('/api/scheduler/start');
                const result = await response.json();

                alert(result.message);
                if (result.status === 'success') {
                    await refreshScheduler();
                }
            } catch (error) {
                alert('启动调度器失败: ' + error.message);
            }
        }

        // 停止调度器
        async function stopScheduler() {
            try {
                const response = await fetch('/api/scheduler/stop');
                const result = await response.json();

                alert(result.message);
                if (result.status === 'success') {
                    await refreshScheduler();
                }
            } catch (error) {
                alert('停止调度器失败: ' + error.message);
            }
        }

        // 显示添加任务对话框
        function showAddTaskDialog() {
            const dialog = document.createElement('div');
            dialog.style.cssText = `
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0,0,0,0.5);
                display: flex;
                align-items: center;
                justify-content: center;
                z-index: 1000;
            `;

            dialog.innerHTML = `
                <div style="background: white; padding: 2rem; border-radius: 8px; max-width: 500px; width: 90%;">
                    <h3 style="margin-top: 0;">添加调度任务</h3>
                    <form id="add-task-form">
                        <div style="margin-bottom: 1rem;">
                            <label style="display: block; margin-bottom: 0.5rem; font-weight: 500;">任务名称:</label>
                            <input type="text" name="name" required style="width: 100%; padding: 0.5rem; border: 1px solid var(--border-color); border-radius: 4px;">
                        </div>
                        <div style="margin-bottom: 1rem;">
                            <label style="display: block; margin-bottom: 0.5rem; font-weight: 500;">Cron表达式:</label>
                            <input type="text" name="cron" required placeholder="例如: 0 */2 * * * (每2小时)" style="width: 100%; padding: 0.5rem; border: 1px solid var(--border-color); border-radius: 4px;">
                            <small style="color: #6c757d;">格式: 分钟 小时 日 月 周 (*/2表示每2)</small>
                        </div>
                        <div style="margin-bottom: 1rem;">
                            <label style="display: block; margin-bottom: 0.5rem; font-weight: 500;">描述:</label>
                            <textarea name="description" rows="3" style="width: 100%; padding: 0.5rem; border: 1px solid var(--border-color); border-radius: 4px; resize: vertical;"></textarea>
                        </div>
                        <div style="display: flex; gap: 1rem; justify-content: flex-end;">
                            <button type="button" onclick="closeAddTaskDialog()" style="padding: 0.5rem 1rem; border: 1px solid var(--border-color); background: white; border-radius: 4px; cursor: pointer;">取消</button>
                            <button type="submit" class="btn">添加</button>
                        </div>
                    </form>
                </div>
            `;

            dialog.addEventListener('click', (e) => {
                if (e.target === dialog) {
                    closeAddTaskDialog();
                }
            });

            document.body.appendChild(dialog);

            // 绑定表单提交事件
            document.getElementById('add-task-form').addEventListener('submit', async (e) => {
                e.preventDefault();
                await addTask();
            });
        }

        // 关闭添加任务对话框
        function closeAddTaskDialog() {
            const dialog = document.querySelector('div[style*="position: fixed"]');
            if (dialog) {
                dialog.remove();
            }
        }

        // 添加任务
        async function addTask() {
            const form = document.getElementById('add-task-form');
            const formData = new FormData(form);

            const taskData = {
                name: formData.get('name'),
                cron: formData.get('cron'),
                description: formData.get('description'),
                task_type: 'collection'
            };

            try {
                const response = await fetch('/api/scheduler/tasks', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(taskData)
                });

                const result = await response.json();

                if (result.status === 'success') {
                    alert('任务添加成功!');
                    closeAddTaskDialog();
                    await refreshScheduler();
                } else {
                    alert('添加失败: ' + result.message);
                }
            } catch (error) {
                alert('添加任务失败: ' + error.message);
            }
        }

        // 删除任务
        async function removeTask(taskName) {
            if (confirm(`确定要删除任务 "${taskName}" 吗？`)) {
                try {
                    const response = await fetch(`/api/scheduler/tasks/${encodeURIComponent(taskName)}`, {
                        method: 'DELETE'
                    });

                    const result = await response.json();

                    if (result.status === 'success') {
                        alert('任务删除成功!');
                        await refreshScheduler();
                    } else {
                        alert('删除失败: ' + result.message);
                    }
                } catch (error) {
                    alert('删除任务失败: ' + error.message);
                }
            }
        }

    </script>
</body>
</html>
"""


# API路由
@app.route('/api/overview')
def api_overview():
    """获取系统概览"""
    return jsonify(atlas_api.get_system_overview())


@app.route('/api/sources')
def api_sources():
    """获取数据源列表"""
    return jsonify(atlas_api.get_data_sources())


@app.route('/api/collections')
def api_collections():
    """获取采集任务"""
    limit = request.args.get('limit', 20, type=int)
    return jsonify(atlas_api.get_recent_collections(limit))


@app.route('/api/data')
def api_data():
    """获取文档数据"""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(atlas_api.get_collected_data(limit))


@app.route('/api/files')
def api_files():
    """获取文件数据"""
    return jsonify(atlas_api.get_file_data())


@app.route('/api/collect', methods=['POST'])
def api_collect_all():
    """开始采集所有启用的数据源"""
    return jsonify(atlas_api.trigger_collection())


@app.route('/api/collect/<source_name>', methods=['POST'])
def api_collect_source(source_name):
    """开始采集指定数据源"""
    return jsonify(atlas_api.trigger_collection(source_name))


@app.route('/api/export')
def api_export():
    """导出数据"""
    return jsonify(atlas_api.export_data())


@app.route('/api/storage/stats')
def api_storage_stats():
    """获取存储统计信息"""
    if not UNIFIED_STORAGE_AVAILABLE:
        return jsonify({
            "status": "error",
            "message": "统一存储接口不可用"
        }), 501

    try:
        import asyncio
        storage = get_unified_storage()

        # 获取存储信息
        storage_info = storage.get_storage_info()
        storage_type = storage.get_storage_type()

        # 获取存储统计
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    storage_stats = executor.submit(asyncio.run, storage.get_storage_stats()).result()
            else:
                storage_stats = loop.run_until_complete(storage.get_storage_stats())
        except Exception as stats_error:
            logger.warning(f"获取存储统计失败: {stats_error}")
            storage_stats = {"error": str(stats_error)}

        return jsonify({
            "status": "success",
            "storage_type": storage_type,
            "storage_info": storage_info,
            "storage_stats": storage_stats
        })

    except Exception as e:
        logger.error(f"获取存储统计失败: {e}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# 调度器管理API
@app.route('/api/scheduler/status')
def api_scheduler_status():
    """获取调度器状态"""
    return jsonify(atlas_api.scheduler.get_schedule_status())


@app.route('/api/scheduler/tasks')
def api_scheduler_tasks():
    """获取调度任务列表"""
    return jsonify({
        "status": "success",
        "tasks": atlas_api.scheduler.get_tasks()
    })


@app.route('/api/scheduler/tasks', methods=['POST'])
def api_scheduler_add_task():
    """添加调度任务"""
    try:
        data = request.get_json()

        if not data or 'name' not in data or 'cron' not in data:
            return jsonify({
                "status": "error",
                "message": "缺少必要参数: name, cron"
            }), 400

        name = data['name']
        cron_expression = data['cron']
        description = data.get('description', '')

        # 创建任务函数
        if data.get('task_type') == 'collection':
            func = atlas_api._scheduled_collection
        else:
            # 默认执行采集任务
            func = atlas_api._scheduled_collection

        atlas_api.scheduler.add_task(name, func, cron_expression, description)

        return jsonify({
            "status": "success",
            "message": f"任务 '{name}' 添加成功"
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/scheduler/tasks/<task_name>', methods=['DELETE'])
def api_scheduler_remove_task(task_name):
    """删除调度任务"""
    try:
        atlas_api.scheduler.remove_task(task_name)
        return jsonify({
            "status": "success",
            "message": f"任务 '{task_name}' 删除成功"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/scheduler/start')
def api_scheduler_start():
    """启动调度器"""
    try:
        atlas_api.scheduler.start()
        return jsonify({
            "status": "success",
            "message": "调度器已启动"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route('/api/scheduler/stop')
def api_scheduler_stop():
    """停止调度器"""
    try:
        atlas_api.scheduler.stop()
        return jsonify({
            "status": "success",
            "message": "调度器已停止"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# Web界面路由
@app.route('/')
def index():
    """主页面"""
    return render_template_string(INDEX_TEMPLATE)


@app.route('/health')
def health():
    """健康检查"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })


# 静态文件服务
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


def create_directories():
    """创建必要的目录"""
    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    os.makedirs(Path(__file__).parent / "static", exist_ok=True)


def main():
    """启动Web服务"""
    create_directories()

    print("🚀 Atlas Web后台服务")
    print("=" * 50)
    print(f"📍 访问地址: http://localhost:5000")
    print(f"🔧 数据库: {Path(atlas_api.db_path).absolute()}")
    print(f"📝 日志目录: {Path('logs').absolute()}")
    print("按 Ctrl+C 停止服务")
    print()

    # 启动调度器
    try:
        atlas_api.scheduler.start()
        print("✅ 任务调度器已启动")
    except Exception as e:
        print(f"⚠️ 调度器启动失败: {e}")

    # 启动Flask应用
    try:
        app.run(host='0.0.0.0', port=5000, debug=False)
    finally:
        # 确保调度器被正确停止
        try:
            atlas_api.scheduler.stop()
            print("🛑 任务调度器已停止")
        except:
            pass


if __name__ == '__main__':
    main()