"""
数据源服务层

提供数据源管理的业务逻辑。
"""

from typing import Optional, List, Tuple
from datetime import datetime
from sqlalchemy import select, update, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import DataSource, CollectionTask
from atlas.web.schemas.sources import (
    DataSourceCreate,
    DataSourceUpdate,
    DataSourceStats
)


class DataSourceService:
    """数据源服务类"""

    @staticmethod
    async def list_sources(
        db: AsyncSession,
        enabled: Optional[bool] = None,
        source_type: Optional[str] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[List[DataSource], int]:
        """
        获取数据源列表

        Args:
            db: 数据库会话
            enabled: 启用状态筛选
            source_type: 数据源类型筛选
            category: 分类筛选
            search: 搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            Tuple[List[DataSource], int]: (数据源列表, 总数)
        """
        # 构建查询
        query = select(DataSource)

        # 应用筛选条件
        if enabled is not None:
            query = query.where(DataSource.enabled == enabled)

        if source_type:
            query = query.where(DataSource.source_type == source_type)

        if category:
            query = query.where(DataSource.category == category)

        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                or_(
                    DataSource.name.like(search_pattern),
                    DataSource.url.like(search_pattern)
                )
            )

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # 应用分页和排序
        query = query.order_by(DataSource.created_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await db.execute(query)
        sources = result.scalars().all()

        return list(sources), total

    @staticmethod
    async def get_source_by_name(
        db: AsyncSession,
        name: str
    ) -> Optional[DataSource]:
        """
        根据名称获取数据源

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            Optional[DataSource]: 数据源对象
        """
        result = await db.execute(
            select(DataSource).where(DataSource.name == name)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_source(
        db: AsyncSession,
        source_data: DataSourceCreate
    ) -> DataSource:
        """
        创建数据源

        Args:
            db: 数据库会话
            source_data: 创建数据源的数据

        Returns:
            DataSource: 创建的数据源对象

        Raises:
            ValueError: 数据源名称已存在
        """
        # 检查名称是否已存在
        existing = await DataSourceService.get_source_by_name(db, source_data.name)
        if existing:
            raise ValueError(f"数据源 '{source_data.name}' 已存在")

        # 转换tags为逗号分隔的字符串
        tags_str = None
        if source_data.tags:
            tags_str = ",".join(source_data.tags)

        # 创建数据源对象
        source = DataSource(
            name=source_data.name,
            source_type=source_data.source_type,
            url=source_data.url,
            enabled=source_data.enabled,
            config=source_data.config,
            tags=tags_str,
            category=source_data.category,
            language=source_data.language,
            collection_interval=source_data.collection_interval,
            max_items_per_run=source_data.max_items_per_run,
            retry_count=source_data.retry_count,
            timeout=source_data.timeout
        )

        db.add(source)
        await db.commit()
        await db.refresh(source)

        return source

    @staticmethod
    async def update_source(
        db: AsyncSession,
        name: str,
        source_data: DataSourceUpdate
    ) -> Optional[DataSource]:
        """
        更新数据源

        Args:
            db: 数据库会话
            name: 数据源名称
            source_data: 更新数据

        Returns:
            Optional[DataSource]: 更新后的数据源对象

        Raises:
            ValueError: 数据源不存在
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        # 构建更新数据
        update_data = source_data.model_dump(exclude_unset=True)

        # 处理tags字段
        if 'tags' in update_data and update_data['tags'] is not None:
            update_data['tags'] = ",".join(update_data['tags'])

        # 执行更新
        await db.execute(
            update(DataSource)
            .where(DataSource.name == name)
            .values(**update_data)
        )
        await db.commit()

        # 刷新并返回
        await db.refresh(source)
        return source

    @staticmethod
    async def delete_source(
        db: AsyncSession,
        name: str
    ) -> bool:
        """
        删除数据源

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            bool: 是否删除成功

        Raises:
            ValueError: 数据源不存在
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        await db.execute(
            delete(DataSource).where(DataSource.name == name)
        )
        await db.commit()

        return True

    @staticmethod
    async def enable_source(
        db: AsyncSession,
        name: str
    ) -> DataSource:
        """
        启用数据源

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            DataSource: 更新后的数据源

        Raises:
            ValueError: 数据源不存在
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        if not source.enabled:
            await db.execute(
                update(DataSource)
                .where(DataSource.name == name)
                .values(enabled=True)
            )
            await db.commit()
            await db.refresh(source)

        return source

    @staticmethod
    async def disable_source(
        db: AsyncSession,
        name: str
    ) -> DataSource:
        """
        禁用数据源

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            DataSource: 更新后的数据源

        Raises:
            ValueError: 数据源不存在
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        if source.enabled:
            await db.execute(
                update(DataSource)
                .where(DataSource.name == name)
                .values(enabled=False)
            )
            await db.commit()
            await db.refresh(source)

        return source

    @staticmethod
    async def get_source_stats(
        db: AsyncSession,
        name: str
    ) -> DataSourceStats:
        """
        获取数据源统计信息

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            DataSourceStats: 统计信息

        Raises:
            ValueError: 数据源不存在
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        # 计算成功率
        total = source.collection_count or 0
        success_rate = (source.success_count / total * 100) if total > 0 else 0.0

        return DataSourceStats(
            source_id=source.id,
            source_name=source.name,
            collection_count=source.collection_count or 0,
            success_count=source.success_count or 0,
            error_count=source.error_count or 0,
            success_rate=round(success_rate, 2),
            last_collected_at=source.last_collected_at,
            last_success_at=source.last_success_at,
            last_error=source.last_error
        )

    @staticmethod
    async def trigger_collection(
        db: AsyncSession,
        name: str
    ) -> CollectionTask:
        """
        触发立即采集

        Args:
            db: 数据库会话
            name: 数据源名称

        Returns:
            CollectionTask: 创建的采集任务

        Raises:
            ValueError: 数据源不存在或未启用
        """
        source = await DataSourceService.get_source_by_name(db, name)
        if not source:
            raise ValueError(f"数据源 '{name}' 不存在")

        if not source.enabled:
            raise ValueError(f"数据源 '{name}' 未启用")

        # 创建采集任务
        task = CollectionTask(
            source_id=source.name,
            task_type="manual",
            status="pending"
        )

        db.add(task)
        await db.commit()
        await db.refresh(task)

        return task
