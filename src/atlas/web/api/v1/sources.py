"""
数据源API路由

提供数据源管理的API端点：
- GET  /api/v1/sources               - 列表（分页、筛选）
- GET  /api/v1/sources/{name}        - 详情
- POST /api/v1/sources               - 创建
- PUT  /api/v1/sources/{name}        - 更新
- DELETE /api/v1/sources/{name}      - 删除
- POST /api/v1/sources/{name}/enable - 启用
- POST /api/v1/sources/{name}/disable - 禁用
- GET  /api/v1/sources/{name}/stats  - 统计
- POST /api/v1/sources/{name}/collect - 立即采集
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import User
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.services.source_service import DataSourceService
from atlas.web.schemas.sources import (
    DataSourceCreate,
    DataSourceUpdate,
    DataSourceResponse,
    DataSourceListItem,
    DataSourceStats,
    CollectionTaskCreate
)
from atlas.web.utils.response import success_response


router = APIRouter(prefix="/sources", tags=["数据源管理"])


# =============================================================================
# 数据源CRUD
# =============================================================================

@router.get("")
async def list_sources(
    enabled: Optional[bool] = Query(None, description="启用状态"),
    source_type: Optional[str] = Query(None, description="数据源类型"),
    category: Optional[str] = Query(None, description="分类"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取数据源列表

    支持分页、筛选和搜索功能。
    """
    sources, total = await DataSourceService.list_sources(
        db=db,
        enabled=enabled,
        source_type=source_type,
        category=category,
        search=search,
        page=page,
        page_size=page_size
    )

    # 转换为列表项格式
    items = [DataSourceListItem.model_validate(source) for source in sources]

    return {
        "success": True,
        "message": f"获取数据源列表成功，共{total}条",
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        "error": None
    }


@router.get("/{name}", response_model=DataSourceResponse)
async def get_source(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取数据源详情
    """
    source = await DataSourceService.get_source_by_name(db, name)

    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据源 '{name}' 不存在"
        )

    return DataSourceResponse.model_validate(source)


@router.post("", response_model=DataSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source(
    source_data: DataSourceCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建数据源
    """
    try:
        source = await DataSourceService.create_source(db, source_data)
        return DataSourceResponse.model_validate(source)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.put("/{name}", response_model=DataSourceResponse)
async def update_source(
    name: str,
    source_data: DataSourceUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    更新数据源
    """
    try:
        source = await DataSourceService.update_source(db, name, source_data)
        return DataSourceResponse.model_validate(source)
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


@router.delete("/{name}")
async def delete_source(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    删除数据源

    注意：删除数据源会级联删除其关联的文档和采集任务。
    """
    try:
        await DataSourceService.delete_source(db, name)
        return success_response(
            data={"name": name},
            message=f"数据源 '{name}' 已删除"
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


# =============================================================================
# 数据源操作
# =============================================================================

@router.post("/{name}/enable", response_model=DataSourceResponse)
async def enable_source(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    启用数据源
    """
    try:
        source = await DataSourceService.enable_source(db, name)
        return DataSourceResponse.model_validate(source)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/{name}/disable", response_model=DataSourceResponse)
async def disable_source(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    禁用数据源
    """
    try:
        source = await DataSourceService.disable_source(db, name)
        return DataSourceResponse.model_validate(source)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.get("/{name}/stats", response_model=DataSourceStats)
async def get_source_stats(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取数据源统计信息
    """
    try:
        stats = await DataSourceService.get_source_stats(db, name)
        return stats
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/{name}/collect")
async def trigger_collection(
    name: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    触发立即采集

    为指定数据源创建一个手动采集任务。
    """
    try:
        task = await DataSourceService.trigger_collection(db, name)

        return success_response(
            data={
                "task_id": task.id,
                "source_name": task.source_id,
                "task_type": task.task_type,
                "status": task.status,
                "created_at": task.created_at
            },
            message=f"已为数据源 '{name}' 创建采集任务"
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
