"""
Atlas 任务调度模块

提供完整的任务调度、队列管理和状态跟踪功能。
支持 cron 表达式、异步任务执行、任务状态监控等。
"""

# Celery任务队列系统 (TASK-003)
from .celery_app import celery_app, create_celery_app
from .celery_config import get_celery_config, CeleryConfig
from .celery_tasks import (
    AtlasTask,
    collect_from_source,
    collect_all_sources,
    fetch_url,
    process_document,
    batch_process,
    deduplicate_documents,
    rebuild_index,
    sync_pipeline,
    register_periodic_tasks,
)

# 原有的asyncio调度器 (保留作为备用)
from .cron_manager import CronManager, CronExpression, CronJob
from .queue import TaskQueue, Task, TaskPriority, TaskStatus
from .status import StatusManager, TaskState, TaskMetrics

__all__ = [
    # Celery (TASK-003)
    "celery_app",
    "create_celery_app",
    "get_celery_config",
    "CeleryConfig",
    "AtlasTask",
    "collect_from_source",
    "collect_all_sources",
    "fetch_url",
    "process_document",
    "batch_process",
    "deduplicate_documents",
    "rebuild_index",
    "sync_pipeline",
    "register_periodic_tasks",

    # 原有调度器
    "CronManager",
    "CronExpression",
    "CronJob",
    "TaskQueue",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "StatusManager",
    "TaskState",
    "TaskMetrics",
]

__version__ = "2.0.0"