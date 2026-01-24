"""
文档API路由

提供文档管理的API端点：

原始文档 (Raw Documents):
- GET  /api/v1/documents/raw               - 列表（分页、筛选）
- GET  /api/v1/documents/raw/{id}         - 详情
- POST /api/v1/documents/raw              - 创建
- PUT  /api/v1/documents/raw/{id}         - 更新
- DELETE /api/v1/documents/raw/{id}       - 删除
- POST /api/v1/documents/raw/batch        - 批量操作

处理后文档 (Processed Documents):
- GET  /api/v1/documents/processed        - 列表（分页、筛选）
- GET  /api/v1/documents/processed/{id}   - 详情
- POST /api/v1/documents/processed        - 创建
- PUT  /api/v1/documents/processed/{id}   - 更新
- DELETE /api/v1/documents/processed/{id} - 删除

统计:
- GET  /api/v1/documents/stats            - 统计信息
"""

from typing import Optional
from datetime import datetime
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import User
from atlas.web.dependencies.auth import get_current_active_user, get_async_db
from atlas.web.services.document_service import DocumentService
from atlas.web.schemas.documents import (
    RawDocumentCreate,
    RawDocumentUpdate,
    RawDocumentResponse,
    RawDocumentListItem,
    ProcessedDocumentCreate,
    ProcessedDocumentUpdate,
    ProcessedDocumentResponse,
    ProcessedDocumentListItem,
    DocumentBatchOperation,
    DocumentBatchResponse
)
from atlas.web.utils.response import success_response


# 创建路由器
router = APIRouter(prefix="/documents", tags=["文档管理"])


# =============================================================================
# 原始文档 (Raw Documents) API
# =============================================================================

@router.get("/raw", response_model=dict)
async def list_raw_documents(
    source_id: Optional[str] = Query(None, description="数据源ID"),
    source_type: Optional[str] = Query(None, description="来源类型"),
    document_type: Optional[str] = Query(None, description="文档类型"),
    processing_status: Optional[str] = Query(None, description="处理状态"),
    start_date: Optional[datetime] = Query(None, description="开始日期"),
    end_date: Optional[datetime] = Query(None, description="结束日期"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取原始文档列表

    支持按数据源、类型、处理状态、日期范围等筛选，支持分页。
    """
    documents, total = await DocumentService.list_raw_documents(
        db=db,
        source_id=source_id,
        source_type=source_type,
        document_type=document_type,
        processing_status=processing_status,
        start_date=start_date,
        end_date=end_date,
        search=search,
        page=page,
        page_size=page_size
    )

    return success_response(
        data={
            "items": [RawDocumentListItem.model_validate(doc) for doc in documents],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        message=f"获取原始文档列表成功，共 {total} 条"
    )


@router.get("/raw/{document_id}", response_model=dict)
async def get_raw_document(
    document_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取单个原始文档详情
    """
    document = await DocumentService.get_raw_document(db, document_id)

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data=RawDocumentResponse.model_validate(document),
        message="获取文档详情成功"
    )


@router.post("/raw", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_raw_document(
    document_data: RawDocumentCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建新的原始文档

    通常由采集器自动调用，手动创建用于测试或导入。
    """
    document = await DocumentService.create_raw_document(db, document_data)

    return success_response(
        data=RawDocumentResponse.model_validate(document),
        message="创建文档成功",
        status_code=status.HTTP_201_CREATED
    )


@router.put("/raw/{document_id}", response_model=dict)
async def update_raw_document(
    document_id: UUID,
    document_data: RawDocumentUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    更新原始文档

    支持更新处理状态、标题、作者等字段。
    """
    document = await DocumentService.update_raw_document(db, document_id, document_data)

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data=RawDocumentResponse.model_validate(document),
        message="更新文档成功"
    )


@router.delete("/raw/{document_id}", response_model=dict)
async def delete_raw_document(
    document_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    删除原始文档

    注意：删除原始文档会级联删除关联的处理后文档。
    """
    success = await DocumentService.delete_raw_document(db, document_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data={"document_id": str(document_id)},
        message="删除文档成功"
    )


@router.post("/raw/batch", response_model=dict)
async def batch_operation_raw_documents(
    operation: DocumentBatchOperation,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    批量操作原始文档

    支持的操作：
    - delete: 批量删除
    - reprocess: 批量重新处理（未来实现）
    """
    if operation.operation == "delete":
        result = await DocumentService.batch_delete_raw_documents(
            db, operation.document_ids
        )
        return success_response(
            data=result.model_dump(),
            message=f"批量操作完成：成功 {result.success_count}，失败 {result.failed_count}"
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的操作: {operation.operation}"
        )


# =============================================================================
# 处理后文档 (Processed Documents) API
# =============================================================================

@router.get("/processed", response_model=dict)
async def list_processed_documents(
    raw_document_id: Optional[UUID] = Query(None, description="原始文档ID"),
    keyword: Optional[str] = Query(None, description="关键词筛选"),
    category: Optional[str] = Query(None, description="分类筛选"),
    start_date: Optional[datetime] = Query(None, description="开始日期"),
    end_date: Optional[datetime] = Query(None, description="结束日期"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取处理后文档列表

    支持按关键词、分类、日期范围等筛选，支持分页。
    """
    documents, total = await DocumentService.list_processed_documents(
        db=db,
        raw_document_id=raw_document_id,
        keyword=keyword,
        category=category,
        start_date=start_date,
        end_date=end_date,
        search=search,
        page=page,
        page_size=page_size
    )

    return success_response(
        data={
            "items": [ProcessedDocumentListItem.model_validate(doc) for doc in documents],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        },
        message=f"获取处理后文档列表成功，共 {total} 条"
    )


@router.get("/processed/{document_id}", response_model=dict)
async def get_processed_document(
    document_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取单个处理后文档详情
    """
    document = await DocumentService.get_processed_document(db, document_id)

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data=ProcessedDocumentResponse.model_validate(document),
        message="获取文档详情成功"
    )


@router.post("/processed", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_processed_document(
    document_data: ProcessedDocumentCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    创建新的处理后文档

    通常由处理器自动调用，手动创建用于测试或导入。
    """
    document = await DocumentService.create_processed_document(db, document_data)

    return success_response(
        data=ProcessedDocumentResponse.model_validate(document),
        message="创建文档成功",
        status_code=status.HTTP_201_CREATED
    )


@router.put("/processed/{document_id}", response_model=dict)
async def update_processed_document(
    document_id: UUID,
    document_data: ProcessedDocumentUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    更新处理后文档

    支持更新标题、摘要、内容、关键词、分类等字段。
    """
    document = await DocumentService.update_processed_document(
        db, document_id, document_data
    )

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data=ProcessedDocumentResponse.model_validate(document),
        message="更新文档成功"
    )


@router.delete("/processed/{document_id}", response_model=dict)
async def delete_processed_document(
    document_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    删除处理后文档

    注意：只删除处理后文档，不会删除原始文档。
    """
    success = await DocumentService.delete_processed_document(db, document_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档 {document_id} 不存在"
        )

    return success_response(
        data={"document_id": str(document_id)},
        message="删除文档成功"
    )


# =============================================================================
# 统计 API
# =============================================================================

@router.get("/stats", response_model=dict)
async def get_document_stats(
    days: int = Query(7, ge=1, le=90, description="统计天数"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    """
    获取文档统计信息

    包括原始文档和处理后文档的数量统计、状态分布等。
    """
    stats = await DocumentService.get_document_stats(db, days)

    return success_response(
        data=stats,
        message=f"获取文档统计成功（最近 {days} 天）"
    )
