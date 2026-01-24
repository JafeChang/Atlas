"""
系统统计和监控API路由

提供系统级别的统计和监控API端点：
- GET /api/v1/system/overview    - 系统概览
- GET /api/v1/system/stats       - 系统统计
- GET /api/v1/system/health      - 健康检查
- GET /api/v1/database/stats     - 数据库统计
- GET /api/v1/storage/stats      - 存储统计
"""

from typing import Dict, Any
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from atlas.models.schema import (
    User, DataSource, CollectionTask, RawDocument, ProcessedDocument
)
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.utils.response import success_response


router = APIRouter(prefix="/system", tags=["系统监控"])


@router.get("/overview", response_model=dict)
async def get_system_overview(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取系统概览

    包括数据源、任务、文档等核心指标的总览。
    """
    # 数据源统计
    sources_count = await db.execute(
        select(func.count()).select_from(DataSource)
    )
    total_sources = sources_count.scalar() or 0

    enabled_sources = await db.execute(
        select(func.count()).select_from(DataSource).where(DataSource.enabled == True)
    )
    enabled_sources_count = enabled_sources.scalar() or 0

    # 采集任务统计
    tasks_count = await db.execute(
        select(func.count()).select_from(CollectionTask)
    )
    total_tasks = tasks_count.scalar() or 0

    running_tasks = await db.execute(
        select(func.count()).select_from(CollectionTask).where(
            CollectionTask.status == "running"
        )
    )
    running_tasks_count = running_tasks.scalar() or 0

    # 文档统计
    raw_docs_count = await db.execute(
        select(func.count()).select_from(RawDocument)
    )
    total_raw_docs = raw_docs_count.scalar() or 0

    processed_docs_count = await db.execute(
        select(func.count()).select_from(ProcessedDocument)
    )
    total_processed_docs = processed_docs_count.scalar() or 0

    return success_response(
        data={
            "data_sources": {
                "total": total_sources,
                "enabled": enabled_sources_count,
                "disabled": total_sources - enabled_sources_count
            },
            "collection_tasks": {
                "total": total_tasks,
                "running": running_tasks_count,
                "completed": total_tasks - running_tasks_count
            },
            "documents": {
                "raw": total_raw_docs,
                "processed": total_processed_docs,
                "processing_rate": (
                    f"{(total_processed_docs / total_raw_docs * 100):.1f}%"
                    if total_raw_docs > 0 else "N/A"
                )
            }
        },
        message="获取系统概览成功"
    )


@router.get("/stats", response_model=dict)
async def get_system_stats(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取详细系统统计

    包括各个维度的详细统计信息。
    """
    # 数据源按类型统计
    sources_by_type = await db.execute(
        select(
            DataSource.source_type,
            func.count().label('count')
        ).group_by(DataSource.source_type)
    )
    sources_by_type_result = {
        row.source_type: row.count
        for row in sources_by_type.all()
    }

    # 任务按状态统计
    tasks_by_status = await db.execute(
        select(
            CollectionTask.status,
            func.count().label('count')
        ).group_by(CollectionTask.status)
    )
    tasks_by_status_result = {
        row.status: row.count
        for row in tasks_by_status.all()
    }

    # 原始文档按状态统计
    raw_docs_by_status = await db.execute(
        select(
            RawDocument.processing_status,
            func.count().label('count')
        ).group_by(RawDocument.processing_status)
    )
    raw_docs_by_status_result = {
        row.processing_status: row.count
        for row in raw_docs_by_status.all()
    }

    # 原始文档按类型统计
    raw_docs_by_type = await db.execute(
        select(
            RawDocument.document_type,
            func.count().label('count')
        ).group_by(RawDocument.document_type)
    )
    raw_docs_by_type_result = {
        row.document_type or "unknown": row.count
        for row in raw_docs_by_type.all()
    }

    return success_response(
        data={
            "data_sources": {
                "by_type": sources_by_type_result
            },
            "collection_tasks": {
                "by_status": tasks_by_status_result
            },
            "documents": {
                "raw_by_status": raw_docs_by_status_result,
                "raw_by_type": raw_docs_by_type_result
            }
        },
        message="获取系统统计成功"
    )


@router.get("/health", response_model=dict)
async def get_system_health(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    系统健康检查

    检查数据库连接、关键服务状态等。
    """
    health_status = {
        "status": "healthy",
        "checks": {}
    }

    # 检查数据库连接
    try:
        await db.execute(select(func.count()).select_from(DataSource))
        health_status["checks"]["database"] = {
            "status": "healthy",
            "message": "数据库连接正常"
        }
    except Exception as e:
        health_status["status"] = "unhealthy"
        health_status["checks"]["database"] = {
            "status": "unhealthy",
            "message": f"数据库连接异常: {str(e)}"
        }

    # 检查数据采集状态
    try:
        recent_tasks = await db.execute(
            select(func.count()).select_from(CollectionTask).where(
                CollectionTask.status == "running"
            )
        )
        running_count = recent_tasks.scalar() or 0
        health_status["checks"]["collector"] = {
            "status": "healthy",
            "message": f"{running_count} 个运行中的采集任务"
        }
    except Exception as e:
        health_status["checks"]["collector"] = {
            "status": "unknown",
            "message": f"无法获取采集状态: {str(e)}"
        }

    # 整体状态
    all_healthy = all(
        check.get("status") == "healthy"
        for check in health_status["checks"].values()
    )
    health_status["overall_status"] = "healthy" if all_healthy else "degraded"

    return success_response(
        data=health_status,
        message="系统健康检查完成"
    )


@router.get("/database/stats", response_model=dict)
async def get_database_stats(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取数据库统计信息

    包括各表的记录数、存储大小等。
    """
    # 各表记录数
    tables = {
        "users": User,
        "data_sources": DataSource,
        "collection_tasks": CollectionTask,
        "raw_documents": RawDocument,
        "processed_documents": ProcessedDocument
    }

    table_stats = {}
    for table_name, table_model in tables.items():
        count = await db.execute(
            select(func.count()).select_from(table_model)
        )
        table_stats[table_name] = {
            "rows": count.scalar() or 0
        }

    return success_response(
        data={
            "tables": table_stats
        },
        message="获取数据库统计成功"
    )
