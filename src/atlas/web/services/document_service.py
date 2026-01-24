"""
文档服务层

提供原始文档和处理后文档管理的业务逻辑。
"""

from typing import Optional, List, Tuple
from datetime import datetime, timedelta
from uuid import UUID
from sqlalchemy import select, update, delete, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import RawDocument, ProcessedDocument
from atlas.web.schemas.documents import (
    RawDocumentCreate,
    RawDocumentUpdate,
    ProcessedDocumentCreate,
    ProcessedDocumentUpdate,
    DocumentBatchOperation,
    DocumentBatchResponse
)


class DocumentService:
    """文档服务类"""

    # =========================================================================
    # RawDocument 服务
    # =========================================================================

    @staticmethod
    async def list_raw_documents(
        db: AsyncSession,
        source_id: Optional[str] = None,
        source_type: Optional[str] = None,
        document_type: Optional[str] = None,
        processing_status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[List[RawDocument], int]:
        """
        获取原始文档列表

        Args:
            db: 数据库会话
            source_id: 数据源ID筛选
            source_type: 来源类型筛选
            document_type: 文档类型筛选
            processing_status: 处理状态筛选
            start_date: 开始日期
            end_date: 结束日期
            search: 搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            Tuple[List[RawDocument], int]: (文档列表, 总数)
        """
        # 构建查询
        query = select(RawDocument)

        # 应用筛选条件
        if source_id:
            query = query.where(RawDocument.source_id == source_id)

        if source_type:
            query = query.where(RawDocument.source_type == source_type)

        if document_type:
            query = query.where(RawDocument.document_type == document_type)

        if processing_status:
            query = query.where(RawDocument.processing_status == processing_status)

        if start_date:
            query = query.where(RawDocument.collected_at >= start_date)

        if end_date:
            query = query.where(RawDocument.collected_at <= end_date)

        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    RawDocument.title.like(search_pattern),
                    RawDocument.source_url.like(search_pattern)
                )
            )

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # 应用分页和排序
        query = query.order_by(RawDocument.collected_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await db.execute(query)
        documents = result.scalars().all()

        return list(documents), total

    @staticmethod
    async def get_raw_document(db: AsyncSession, document_id: UUID) -> Optional[RawDocument]:
        """
        获取单个原始文档

        Args:
            db: 数据库会话
            document_id: 文档ID

        Returns:
            Optional[RawDocument]: 文档对象或None
        """
        query = select(RawDocument).where(RawDocument.id == document_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def create_raw_document(
        db: AsyncSession,
        document_data: RawDocumentCreate
    ) -> RawDocument:
        """
        创建原始文档

        Args:
            db: 数据库会话
            document_data: 文档数据

        Returns:
            RawDocument: 创建的文档对象
        """
        # 创建新文档
        new_document = RawDocument(**document_data.model_dump())

        db.add(new_document)
        await db.commit()
        await db.refresh(new_document)

        return new_document

    @staticmethod
    async def update_raw_document(
        db: AsyncSession,
        document_id: UUID,
        document_data: RawDocumentUpdate
    ) -> Optional[RawDocument]:
        """
        更新原始文档

        Args:
            db: 数据库会话
            document_id: 文档ID
            document_data: 更新数据

        Returns:
            Optional[RawDocument]: 更新后的文档对象或None
        """
        # 构建更新数据（只更新非None字段）
        update_data = document_data.model_dump(exclude_unset=True)

        if not update_data:
            return await DocumentService.get_raw_document(db, document_id)

        # 执行更新
        query = update(RawDocument).where(RawDocument.id == document_id).values(**update_data)
        await db.execute(query)
        await db.commit()

        return await DocumentService.get_raw_document(db, document_id)

    @staticmethod
    async def delete_raw_document(db: AsyncSession, document_id: UUID) -> bool:
        """
        删除原始文档

        Args:
            db: 数据库会话
            document_id: 文档ID

        Returns:
            bool: 是否删除成功
        """
        query = delete(RawDocument).where(RawDocument.id == document_id)
        result = await db.execute(query)
        await db.commit()

        return result.rowcount > 0

    @staticmethod
    async def batch_delete_raw_documents(
        db: AsyncSession,
        document_ids: List[UUID]
    ) -> DocumentBatchResponse:
        """
        批量删除原始文档

        Args:
            db: 数据库会话
            document_ids: 文档ID列表

        Returns:
            DocumentBatchResponse: 批量操作结果
        """
        success_count = 0
        failed_count = 0
        errors = []

        for doc_id in document_ids:
            try:
                result = await DocumentService.delete_raw_document(db, doc_id)
                if result:
                    success_count += 1
                else:
                    failed_count += 1
                    errors.append(f"文档 {doc_id} 不存在")
            except Exception as e:
                failed_count += 1
                errors.append(f"删除文档 {doc_id} 失败: {str(e)}")

        return DocumentBatchResponse(
            success_count=success_count,
            failed_count=failed_count,
            errors=errors
        )

    # =========================================================================
    # ProcessedDocument 服务
    # =========================================================================

    @staticmethod
    async def list_processed_documents(
        db: AsyncSession,
        raw_document_id: Optional[UUID] = None,
        keyword: Optional[str] = None,
        category: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[List[ProcessedDocument], int]:
        """
        获取处理后文档列表

        Args:
            db: 数据库会话
            raw_document_id: 原始文档ID筛选
            keyword: 关键词筛选
            category: 分类筛选
            start_date: 开始日期
            end_date: 结束日期
            search: 搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            Tuple[List[ProcessedDocument], int]: (文档列表, 总数)
        """
        # 构建查询
        query = select(ProcessedDocument)

        # 应用筛选条件
        if raw_document_id:
            query = query.where(ProcessedDocument.raw_document_id == raw_document_id)

        if keyword:
            # keywords字段是逗号分隔的字符串，需要模糊匹配
            query = query.where(ProcessedDocument.keywords.like(f"%{keyword}%"))

        if category:
            query = query.where(ProcessedDocument.categories.like(f"%{category}%"))

        if start_date:
            query = query.where(ProcessedDocument.processed_at >= start_date)

        if end_date:
            query = query.where(ProcessedDocument.processed_at <= end_date)

        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    ProcessedDocument.title.like(search_pattern),
                    ProcessedDocument.summary.like(search_pattern),
                    ProcessedDocument.content.like(search_pattern)
                )
            )

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # 应用分页和排序
        query = query.order_by(ProcessedDocument.processed_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await db.execute(query)
        documents = result.scalars().all()

        return list(documents), total

    @staticmethod
    async def get_processed_document(db: AsyncSession, document_id: UUID) -> Optional[ProcessedDocument]:
        """
        获取单个处理后文档

        Args:
            db: 数据库会话
            document_id: 文档ID

        Returns:
            Optional[ProcessedDocument]: 文档对象或None
        """
        query = select(ProcessedDocument).where(ProcessedDocument.id == document_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    @staticmethod
    async def create_processed_document(
        db: AsyncSession,
        document_data: ProcessedDocumentCreate
    ) -> ProcessedDocument:
        """
        创建处理后文档

        Args:
            db: 数据库会话
            document_data: 文档数据

        Returns:
            ProcessedDocument: 创建的文档对象
        """
        # 创建新文档
        new_document = ProcessedDocument(**document_data.model_dump())

        db.add(new_document)
        await db.commit()
        await db.refresh(new_document)

        return new_document

    @staticmethod
    async def update_processed_document(
        db: AsyncSession,
        document_id: UUID,
        document_data: ProcessedDocumentUpdate
    ) -> Optional[ProcessedDocument]:
        """
        更新处理后文档

        Args:
            db: 数据库会话
            document_id: 文档ID
            document_data: 更新数据

        Returns:
            Optional[ProcessedDocument]: 更新后的文档对象或None
        """
        # 构建更新数据（只更新非None字段）
        update_data = document_data.model_dump(exclude_unset=True)

        if not update_data:
            return await DocumentService.get_processed_document(db, document_id)

        # 执行更新
        query = update(ProcessedDocument).where(ProcessedDocument.id == document_id).values(**update_data)
        await db.execute(query)
        await db.commit()

        return await DocumentService.get_processed_document(db, document_id)

    @staticmethod
    async def delete_processed_document(db: AsyncSession, document_id: UUID) -> bool:
        """
        删除处理后文档

        Args:
            db: 数据库会话
            document_id: 文档ID

        Returns:
            bool: 是否删除成功
        """
        query = delete(ProcessedDocument).where(ProcessedDocument.id == document_id)
        result = await db.execute(query)
        await db.commit()

        return result.rowcount > 0

    # =========================================================================
    # 统计服务
    # =========================================================================

    @staticmethod
    async def get_document_stats(
        db: AsyncSession,
        days: int = 7
    ) -> dict:
        """
        获取文档统计信息

        Args:
            db: 数据库会话
            days: 统计天数

        Returns:
            dict: 统计信息
        """
        # 计算时间范围
        start_date = datetime.now() - timedelta(days=days)

        # 原始文档统计
        total_raw = await db.execute(
            select(func.count()).select_from(RawDocument)
        )
        total_raw_count = total_raw.scalar() or 0

        recent_raw = await db.execute(
            select(func.count()).select_from(RawDocument).where(
                RawDocument.collected_at >= start_date
            )
        )
        recent_raw_count = recent_raw.scalar() or 0

        # 按处理状态统计
        pending_raw = await db.execute(
            select(func.count()).select_from(RawDocument).where(
                RawDocument.processing_status == "pending"
            )
        )
        pending_raw_count = pending_raw.scalar() or 0

        processing_raw = await db.execute(
            select(func.count()).select_from(RawDocument).where(
                RawDocument.processing_status == "processing"
            )
        )
        processing_raw_count = processing_raw.scalar() or 0

        completed_raw = await db.execute(
            select(func.count()).select_from(RawDocument).where(
                RawDocument.processing_status == "completed"
            )
        )
        completed_raw_count = completed_raw.scalar() or 0

        failed_raw = await db.execute(
            select(func.count()).select_from(RawDocument).where(
                RawDocument.processing_status == "failed"
            )
        )
        failed_raw_count = failed_raw.scalar() or 0

        # 处理后文档统计
        total_processed = await db.execute(
            select(func.count()).select_from(ProcessedDocument)
        )
        total_processed_count = total_processed.scalar() or 0

        recent_processed = await db.execute(
            select(func.count()).select_from(ProcessedDocument).where(
                ProcessedDocument.processed_at >= start_date
            )
        )
        recent_processed_count = recent_processed.scalar() or 0

        return {
            "raw_documents": {
                "total": total_raw_count,
                "recent": recent_raw_count,
                "by_status": {
                    "pending": pending_raw_count,
                    "processing": processing_raw_count,
                    "completed": completed_raw_count,
                    "failed": failed_raw_count
                }
            },
            "processed_documents": {
                "total": total_processed_count,
                "recent": recent_processed_count
            },
            "period_days": days
        }
