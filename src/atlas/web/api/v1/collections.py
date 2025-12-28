"""
采集任务API路由

提供采集任务管理的API端点：
- GET  /api/v1/collections        - 任务列表
- GET  /api/v1/collections/{id}   - 任务详情
- POST /api/v1/collections        - 创建任务
- POST /api/v1/collections/batch  - 批量采集
- DELETE /api/v1/collections/{id} - 取消任务
- GET  /api/v1/collections/{id}/logs - 任务日志
- POST /api/v1/collections/{id}/retry - 重试任务
"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import User
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.services.collection_service import CollectionService
from atlas.web.schemas.collections import (
    CollectionTaskCreate,
    BatchCollectionCreate,
    CollectionTaskResponse,
    CollectionTaskListItem,
    TaskLogsResponse,
    BatchCollectionResponse
)
from atlas.web.utils.response import success_response


router = APIRouter(prefix="/collections", tags=["采集任务管理"])


# =============================================================================
# 采集任务查询
# =============================================================================

@router.get("")
async def list_tasks(
    source_name: str = Query(None, description="数据源名称"),
    task_type: str = Query(None, description="任务类型"),
    status: str = Query(None, description="任务状态"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取采集任务列表

    支持分页和筛选功能。
    """
    tasks, total = await CollectionService.list_tasks(
        db=db,
        source_name=source_name,
        task_type=task_type,
        status=status,
        page=page,
        page_size=page_size
    )

    # 转换为列表项格式
    items = [CollectionTaskListItem.model_validate(task) for task in tasks]

    return {
        "success": True,
        "message": f"获取任务列表成功，共{total}条",
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        "error": None
    }


@router.get("/{task_id}", response_model=CollectionTaskResponse)
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取任务详情
    """
    task = await CollectionService.get_task_by_id(db, task_id)

    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务 {task_id} 不存在"
        )

    return CollectionTaskResponse.model_validate(task)


# =============================================================================
# 采集任务创建
# =============================================================================

@router.post("", response_model=CollectionTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task_data: CollectionTaskCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建采集任务

    为指定数据源创建一个手动采集任务。
    """
    try:
        task = await CollectionService.create_task(db, task_data)
        return CollectionTaskResponse.model_validate(task)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/batch")
async def create_batch_tasks(
    batch_data: BatchCollectionCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    批量创建采集任务

    为多个数据源同时创建采集任务。
    """
    result = await CollectionService.create_batch_tasks(db, batch_data)

    return {
        "success": True,
        "message": f"批量任务创建完成，成功{result.success_count}个，失败{result.failed_count}个",
        "data": {
            "total_tasks": result.total_tasks,
            "success_count": result.success_count,
            "failed_count": result.failed_count,
            "tasks": result.tasks,
            "errors": result.errors
        },
        "error": None
    }


# =============================================================================
# 采集任务操作
# =============================================================================

@router.delete("/{task_id}")
async def cancel_task(
    task_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    取消任务

    取消尚未开始的任务。
    注意：运行中的任务无法取消。
    """
    try:
        task = await CollectionService.cancel_task(db, task_id)
        return success_response(
            data={
                "task_id": task.id,
                "status": task.status
            },
            message=f"任务已取消"
        )
    except ValueError as e:
        if "不存在" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.post("/{task_id}/retry", response_model=CollectionTaskResponse)
async def retry_task(
    task_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    重试失败的任务

    为失败的任务创建一个新的重试任务。
    """
    try:
        new_task = await CollectionService.retry_task(db, task_id)
        return CollectionTaskResponse.model_validate(new_task)
    except ValueError as e:
        if "不存在" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/{task_id}/logs")
async def get_task_logs(
    task_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取任务日志

    返回任务的执行日志和详细信息。
    """
    try:
        logs_info = await CollectionService.get_task_logs(db, task_id)
        return {
            "success": True,
            "message": "获取任务日志成功",
            "data": logs_info,
            "error": None
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
