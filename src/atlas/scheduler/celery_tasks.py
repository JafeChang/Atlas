"""
Celery任务定义模块

TASK-003: Celery任务队列系统
定义所有Celery任务，包括数据采集、处理和定时任务
"""

import time
from datetime import timedelta
from typing import Dict, Any, Optional
from celery import Task
from celery.schedules import crontab

from atlas.core.logging import get_logger
from .celery_app import celery_app

logger = get_logger(__name__)


# =============================================================================
# 基础任务类
# =============================================================================

class AtlasTask(Task):
    """Atlas任务基类

    提供任务生命周期钩子和错误处理
    """

    _config: Optional[Dict[str, Any]] = None

    def on_success(self, retval: Any, task_id: str, args: tuple, kwargs: Dict[str, Any]) -> None:
        """任务成功回调"""
        logger.info(f"任务成功: {self.name} (Task ID: {task_id})")

    def on_failure(self, exc: Exception, task_id: str, args: tuple, kwargs: Dict[str, Any],
                   einfo: Any) -> None:
        """任务失败回调"""
        logger.error(f"任务失败: {self.name} (Task ID: {task_id}) - {exc}")

    def on_retry(self, exc: Exception, task_id: str, args: tuple, kwargs: Dict[str, Any],
                 einfo: Any) -> None:
        """任务重试回调"""
        logger.warning(f"任务重试: {self.name} (Task ID: {task_id}) - {exc}")


# =============================================================================
# 数据采集任务
# =============================================================================

@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.collect_from_source", max_retries=3)
def collect_from_source(self, source_name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从数据源采集数据

    Args:
        self: 任务实例
        source_name: 数据源名称
        config: 采集配置

    Returns:
        采集结果统计
    """
    logger.info(f"开始从数据源采集数据: {source_name}")

    try:
        # TODO: 集成实际的数据采集逻辑
        # 这里只是一个示例实现
        time.sleep(2)  # 模拟采集耗时

        result = {
            "source": source_name,
            "status": "success",
            "collected": 10,
            "processed": 10,
            "failed": 0,
            "duration": 2.0
        }

        logger.info(f"数据采集完成: {source_name} - 采集了 {result['collected']} 条数据")
        return result

    except Exception as e:
        logger.error(f"数据采集失败: {source_name} - {e}")
        # 重试逻辑
        raise self.retry(exc=e, countdown=60, max_retries=3)


@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.collect_all_sources", max_retries=2)
def collect_all_sources(self) -> Dict[str, Any]:
    """从所有启用的数据源采集数据

    Args:
        self: 任务实例

    Returns:
        采集结果统计
    """
    logger.info("开始从所有数据源采集数据")

    try:
        # TODO: 从数据库获取启用的数据源
        sources = ["source_1", "source_2", "source_3"]

        results = []
        for source in sources:
            result = collect_from_source.delay(source)
            results.append(result.id)

        logger.info(f"已提交 {len(results)} 个数据采集任务")

        return {
            "status": "submitted",
            "tasks": results,
            "total": len(results)
        }

    except Exception as e:
        logger.error(f"批量数据采集失败: {e}")
        raise


@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.fetch_url", max_retries=3)
def fetch_url(self, url: str, timeout: int = 30) -> Dict[str, Any]:
    """获取URL内容

    Args:
        self: 任务实例
        url: 目标URL
        timeout: 超时时间

    Returns:
        获取结果
    """
    logger.info(f"开始获取URL: {url}")

    try:
        import requests
        response = requests.get(url, timeout=timeout)

        result = {
            "url": url,
            "status_code": response.status_code,
            "content_length": len(response.content),
            "success": response.status_code == 200
        }

        logger.info(f"URL获取完成: {url} - Status: {response.status_code}")
        return result

    except Exception as e:
        logger.error(f"URL获取失败: {url} - {e}")
        raise self.retry(exc=e, countdown=30, max_retries=3)


# =============================================================================
# 数据处理任务
# =============================================================================

@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.process_document", max_retries=3)
def process_document(self, doc_id: str) -> Dict[str, Any]:
    """处理文档

    Args:
        self: 任务实例
        doc_id: 文档ID

    Returns:
        处理结果
    """
    logger.info(f"开始处理文档: {doc_id}")

    try:
        # TODO: 集成实际的处理逻辑
        time.sleep(1)  # 模拟处理耗时

        result = {
            "doc_id": doc_id,
            "status": "processed",
            "tags": ["tag1", "tag2"],
            "categories": ["category1"]
        }

        logger.info(f"文档处理完成: {doc_id}")
        return result

    except Exception as e:
        logger.error(f"文档处理失败: {doc_id} - {e}")
        raise self.retry(exc=e, countdown=30, max_retries=3)


@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.batch_process", max_retries=2)
def batch_process(self, doc_ids: list) -> Dict[str, Any]:
    """批量处理文档

    Args:
        self: 任务实例
        doc_ids: 文档ID列表

    Returns:
        处理结果
    """
    logger.info(f"开始批量处理 {len(doc_ids)} 个文档")

    try:
        results = []
        for doc_id in doc_ids:
            result = process_document.delay(doc_id)
            results.append(result.id)

        logger.info(f"已提交 {len(results)} 个文档处理任务")

        return {
            "status": "submitted",
            "tasks": results,
            "total": len(results)
        }

    except Exception as e:
        logger.error(f"批量处理失败: {e}")
        raise


@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.deduplicate_documents", max_retries=2)
def deduplicate_documents(self, batch_size: int = 100) -> Dict[str, Any]:
    """文档去重

    Args:
        self: 任务实例
        batch_size: 批处理大小

    Returns:
        去重结果
    """
    logger.info(f"开始文档去重: batch_size={batch_size}")

    try:
        # TODO: 集成实际去重逻辑
        time.sleep(5)  # 模拟去重耗时

        result = {
            "status": "success",
            "processed": 100,
            "duplicates": 10,
            "unique": 90
        }

        logger.info(f"文档去重完成: 处理了 {result['processed']} 个文档，发现 {result['duplicates']} 个重复")
        return result

    except Exception as e:
        logger.error(f"文档去重失败: {e}")
        raise


# =============================================================================
# 索引和搜索任务
# =============================================================================

@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.rebuild_index", max_retries=1)
def rebuild_index(self, index_type: str = "full") -> Dict[str, Any]:
    """重建索引

    Args:
        self: 任务实例
        index_type: 索引类型 (full/incremental)

    Returns:
        索引结果
    """
    logger.info(f"开始重建索引: type={index_type}")

    try:
        # TODO: 集成实际索引逻辑
        time.sleep(10)  # 模拟索引耗时

        result = {
            "index_type": index_type,
            "status": "success",
            "indexed": 1000,
            "duration": 10.0
        }

        logger.info(f"索引重建完成: 索引了 {result['indexed']} 个文档")
        return result

    except Exception as e:
        logger.error(f"索引重建失败: {e}")
        raise


# =============================================================================
# 定时任务配置
# =============================================================================

def register_periodic_tasks(sender: Any) -> None:
    """注册定时任务

    Args:
        sender: Celery应用实例
    """
    # 每5分钟采集一次数据
    sender.add_periodic_task(
        300.0,  # 5分钟
        collect_all_sources.s(),
        name="定时数据采集 (每5分钟)"
    )

    # 每小时执行一次文档去重
    sender.add_periodic_task(
        crontab(minute=0),  # 每小时整点
        deduplicate_documents.s(),
        name="定时文档去重 (每小时)"
    )

    # 每天凌晨2点重建索引
    sender.add_periodic_task(
        crontab(hour=2, minute=0),  # 每天凌晨2点
        rebuild_index.s(index_type="incremental"),
        name="定时索引重建 (每天凌晨2点)"
    )

    # 每周日凌晨3点全量重建索引
    sender.add_periodic_task(
        crontab(day_of_week=0, hour=3, minute=0),  # 每周日凌晨3点
        rebuild_index.s(index_type="full"),
        name="全量索引重建 (每周日凌晨3点)"
    )

    logger.info("定时任务注册完成")


# =============================================================================
# 任务组合和工作流
# =============================================================================

@celery_app.task(base=AtlasTask, bind=True, name="atlas.scheduler.celery_tasks.sync_pipeline")
def sync_pipeline(self, source_name: str) -> Dict[str, Any]:
    """同步数据管道

    工作流: 采集 -> 处理 -> 去重 -> 索引

    Args:
        self: 任务实例
        source_name: 数据源名称

    Returns:
        管道执行结果
    """
    logger.info(f"开始执行同步数据管道: {source_name}")

    try:
        # 步骤1: 采集数据
        collect_result = collect_from_source.delay(source_name)
        collect_data = collect_result.get()

        # 步骤2: 处理数据 (模拟)
        process_result = {
            "status": "processed",
            "count": collect_data.get("collected", 0)
        }

        # 步骤3: 去重
        dedup_result = deduplicate_documents.delay()
        dedup_data = dedup_result.get()

        # 步骤4: 更新索引 (增量)
        index_result = rebuild_index.delay(index_type="incremental")
        index_data = index_result.get()

        result = {
            "source": source_name,
            "status": "completed",
            "steps": {
                "collect": collect_data,
                "process": process_result,
                "deduplicate": dedup_data,
                "index": index_data
            }
        }

        logger.info(f"同步数据管道完成: {source_name}")
        return result

    except Exception as e:
        logger.error(f"同步数据管道失败: {source_name} - {e}")
        raise


# =============================================================================
# 导出所有任务
# =============================================================================

__all__ = [
    # 基础类
    "AtlasTask",

    # 数据采集任务
    "collect_from_source",
    "collect_all_sources",
    "fetch_url",

    # 数据处理任务
    "process_document",
    "batch_process",
    "deduplicate_documents",

    # 索引和搜索任务
    "rebuild_index",

    # 任务组合
    "sync_pipeline",

    # 定时任务注册
    "register_periodic_tasks",
]
