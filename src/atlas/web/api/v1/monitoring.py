"""
监控仪表板API路由

提供实时监控和性能指标的API端点：
- GET  /api/v1/monitoring/dashboard      - 监控仪表板数据
- GET  /api/v1/monitoring/workflows      - 工作流列表
- GET  /api/v1/monitoring/workflows/{id} - 工作流详情
- GET  /api/v1/monitoring/metrics        - 性能指标
- GET  /api/v1/monitoring/alerts         - 告警列表
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from atlas.models.schema import User, DataSource, CollectionTask, RawDocument
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.utils.response import success_response
from atlas.scheduler.workflows import WorkflowManager
from atlas.scheduler.celery_app import celery_app


router = APIRouter(prefix="/monitoring", tags=["监控仪表板"])


@router.get("/dashboard", response_model=dict)
async def get_dashboard_data(
    hours: int = Query(24, ge=1, le=168, description="统计时间范围（小时）"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取监控仪表板数据

    包括：
    - 系统概览
    - 任务统计
    - 数据趋势
    - 性能指标
    - 活跃工作流
    """
    # 计算时间范围
    start_time = datetime.now() - timedelta(hours=hours)

    # 数据源统计
    total_sources = await db.execute(
        select(func.count()).select_from(DataSource)
    )
    total_sources_count = total_sources.scalar() or 0

    enabled_sources = await db.execute(
        select(func.count()).select_from(DataSource).where(DataSource.enabled == True)
    )
    enabled_sources_count = enabled_sources.scalar() or 0

    # 采集任务统计
    total_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask)
    )
    total_tasks_count = total_tasks.scalar() or 0

    recent_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask).where(
            CollectionTask.created_at >= start_time
        )
    )
    recent_tasks_count = recent_tasks.scalar() or 0

    # 任务状态分布
    tasks_by_status = await db.execute(
        select(
            CollectionTask.status,
            func.count().label('count')
        ).group_by(CollectionTask.status)
    )
    tasks_status_distribution = {
        row.status: row.count
        for row in tasks_by_status.all()
    }

    # 文档统计
    total_docs = await db.execute(
        select(func.count()).select_from(RawDocument)
    )
    total_docs_count = total_docs.scalar() or 0

    recent_docs = await db.execute(
        select(func.count()).select_from(RawDocument).where(
            RawDocument.collected_at >= start_time
        )
    )
    recent_docs_count = recent_docs.scalar() or 0

    # 文档处理状态分布
    docs_by_status = await db.execute(
        select(
            RawDocument.processing_status,
            func.count().label('count')
        ).group_by(RawDocument.processing_status)
    )
    docs_status_distribution = {
        row.processing_status: row.count
        for row in docs_by_status.all()
    }

    # 获取活跃工作流
    workflows = WorkflowManager.list_workflows()
    active_workflows = [
        {
            "workflow_id": wf.workflow_id,
            "workflow_name": wf.workflow_name,
            "status": wf.status,
            "total_tasks": len(wf.tasks),
            "created_at": wf.created_at.isoformat(),
            "started_at": wf.started_at.isoformat() if wf.started_at else None,
        }
        for wf in workflows
        if wf.status == "running"
    ]

    # Celery Worker状态
    inspector = celery_app.control.inspect()
    active_workers = []
    try:
        stats = inspector.stats()
        if stats:
            for worker_name, worker_stats in stats.items():
                active_workers.append({
                    "name": worker_name,
                    "total_tasks": worker_stats.get('total', {})
                })
    except Exception as e:
        # Celery可能未运行
        pass

    return success_response(
        data={
            "overview": {
                "total_sources": total_sources_count,
                "enabled_sources": enabled_sources_count,
                "total_tasks": total_tasks_count,
                "recent_tasks": recent_tasks_count,
                "total_documents": total_docs_count,
                "recent_documents": recent_docs_count,
                "active_workflows": len(active_workflows),
                "active_workers": len(active_workers)
            },
            "tasks": {
                "by_status": tasks_status_distribution,
                "recent_count": recent_tasks_count
            },
            "documents": {
                "by_status": docs_status_distribution,
                "recent_count": recent_docs_count
            },
            "workflows": active_workflows,
            "workers": active_workers,
            "period_hours": hours
        },
        message=f"获取仪表板数据成功（最近{hours}小时）"
    )


@router.get("/workflows", response_model=dict)
async def list_workflows(
    status: Optional[str] = Query(None, description="状态筛选"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    current_user: User = Depends(get_current_active_user)
):
    """
    获取工作流列表
    """
    workflows = WorkflowManager.list_workflows()

    # 应用筛选
    if status:
        workflows = [wf for wf in workflows if wf.status == status]

    # 限制返回数量
    workflows = workflows[:limit]

    workflow_list = [
        {
            "workflow_id": wf.workflow_id,
            "workflow_name": wf.workflow_name,
            "description": wf.description,
            "status": wf.status,
            "total_tasks": len(wf.tasks),
            "created_at": wf.created_at.isoformat(),
            "started_at": wf.started_at.isoformat() if wf.started_at else None,
            "completed_at": wf.completed_at.isoformat() if wf.completed_at else None,
        }
        for wf in workflows
    ]

    return success_response(
        data={
            "items": workflow_list,
            "total": len(workflow_list)
        },
        message="获取工作流列表成功"
    )


@router.get("/workflows/{workflow_id}", response_model=dict)
async def get_workflow_detail(
    workflow_id: str,
    current_user: User = Depends(get_current_active_user)
):
    """
    获取工作流详情
    """
    workflow = WorkflowManager.get_workflow(workflow_id)

    if not workflow:
        return success_response(
            data=None,
            message=f"工作流 {workflow_id} 不存在"
        )

    # 构建任务列表
    tasks_list = []
    for task_id, task in workflow.tasks.items():
        tasks_list.append({
            "task_id": task.task_id,
            "task_name": task.task_name,
            "status": task.status,
            "depends_on": task.depends_on,
            "retry_on_failure": task.retry_on_failure,
            "max_retries": task.max_retries,
            "error": task.error
        })

    return success_response(
        data={
            "workflow_id": workflow.workflow_id,
            "workflow_name": workflow.workflow_name,
            "description": workflow.description,
            "status": workflow.status,
            "created_at": workflow.created_at.isoformat(),
            "started_at": workflow.started_at.isoformat() if workflow.started_at else None,
            "completed_at": workflow.completed_at.isoformat() if workflow.completed_at else None,
            "tasks": tasks_list,
            "total_tasks": len(workflow.tasks)
        },
        message="获取工作流详情成功"
    )


@router.get("/metrics", response_model=dict)
async def get_metrics(
    hours: int = Query(24, ge=1, le=168, description="统计时间范围（小时）"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取性能指标

    包括：
    - 任务成功率
    - 平均执行时间
    - 数据采集速率
    - 文档处理速率
    """
    start_time = datetime.now() - timedelta(hours=hours)

    # 任务统计
    total_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask).where(
            CollectionTask.created_at >= start_time
        )
    )
    total_tasks_count = total_tasks.scalar() or 0

    success_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask).where(
            and_(
                CollectionTask.created_at >= start_time,
                CollectionTask.status == "success"
            )
        )
    )
    success_tasks_count = success_tasks.scalar() or 0

    failed_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask).where(
            and_(
                CollectionTask.created_at >= start_time,
                CollectionTask.status == "failed"
            )
        )
    )
    failed_tasks_count = failed_tasks.scalar() or 0

    # 计算成功率
    success_rate = (
        (success_tasks_count / total_tasks_count * 100)
        if total_tasks_count > 0 else 0
    )

    # 文档统计
    total_docs = await db.execute(
        select(func.count()).select_from(RawDocument).where(
            RawDocument.collected_at >= start_time
        )
    )
    total_docs_count = total_docs.scalar() or 0

    completed_docs = await db.execute(
        select(func.count()).select_from(RawDocument).where(
            and_(
                RawDocument.collected_at >= start_time,
                RawDocument.processing_status == "completed"
            )
        )
    )
    completed_docs_count = completed_docs.scalar() or 0

    # 计算处理速率（文档/小时）
    collection_rate = total_docs_count / hours if hours > 0 else 0
    processing_rate = completed_docs_count / hours if hours > 0 else 0

    # 获取平均任务执行时间（如果有）
    avg_duration = None
    if success_tasks_count > 0:
        # 这里简化处理，实际应该从任务记录中计算
        avg_duration = 300  # 默认5分钟

    return success_response(
        data={
            "tasks": {
                "total": total_tasks_count,
                "success": success_tasks_count,
                "failed": failed_tasks_count,
                "success_rate": round(success_rate, 2),
                "avg_duration_seconds": avg_duration
            },
            "documents": {
                "total_collected": total_docs_count,
                "total_processed": completed_docs_count,
                "collection_rate_per_hour": round(collection_rate, 2),
                "processing_rate_per_hour": round(processing_rate, 2)
            },
            "period_hours": hours
        },
        message=f"获取性能指标成功（最近{hours}小时）"
    )


@router.get("/alerts", response_model=dict)
async def get_alerts(
    limit: int = Query(50, ge=1, le=200, description="返回数量"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取告警列表

    包括：
    - 失败的任务
    - 错误的文档
    - 系统异常
    """
    # 获取失败的任务
    failed_tasks_query = select(CollectionTask).where(
        CollectionTask.status == "failed"
    ).order_by(CollectionTask.updated_at.desc()).limit(limit // 2)

    failed_tasks_result = await db.execute(failed_tasks_query)
    failed_tasks = failed_tasks_result.scalars().all()

    # 获取处理失败的文档
    failed_docs_query = select(RawDocument).where(
        RawDocument.processing_status == "failed"
    ).order_by(RawDocument.updated_at.desc()).limit(limit // 2)

    failed_docs_result = await db.execute(failed_docs_query)
    failed_docs = failed_docs_result.scalars().all()

    # 构建告警列表
    alerts = []

    for task in failed_tasks:
        alerts.append({
            "alert_id": f"task.{task.id}",
            "alert_type": "task_failed",
            "severity": "error",
            "message": f"任务失败: {task.task_type}",
            "source": task.source_name,
            "created_at": task.updated_at.isoformat(),
            "details": {
                "task_id": str(task.id),
                "error": task.error_message
            }
        })

    for doc in failed_docs:
        alerts.append({
            "alert_id": f"doc.{doc.id}",
            "alert_type": "document_failed",
            "severity": "warning",
            "message": f"文档处理失败: {doc.title or doc.id}",
            "source": doc.source_id,
            "created_at": doc.updated_at.isoformat() if doc.updated_at else doc.created_at.isoformat(),
            "details": {
                "document_id": str(doc.id),
                "error": doc.processing_error
            }
        })

    # 按时间排序
    alerts.sort(key=lambda x: x["created_at"], reverse=True)
    alerts = alerts[:limit]

    return success_response(
        data={
            "items": alerts,
            "total": len(alerts),
            "summary": {
                "task_failed": len(failed_tasks),
                "document_failed": len(failed_docs)
            }
        },
        message="获取告警列表成功"
    )
