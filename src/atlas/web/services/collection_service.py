"""
采集任务服务层

提供采集任务管理的业务逻辑。
"""

from typing import Optional, List, Tuple, Dict
from datetime import datetime
from sqlalchemy import select, update, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from atlas.models.schema import CollectionTask, DataSource
from atlas.web.schemas.collections import (
    CollectionTaskCreate,
    BatchCollectionCreate,
    CollectionTaskResponse,
    BatchCollectionResponse
)


class CollectionService:
    """采集任务服务类"""

    @staticmethod
    async def list_tasks(
        db: AsyncSession,
        source_name: Optional[str] = None,
        task_type: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20
    ) -> Tuple[List[CollectionTask], int]:
        """
        获取采集任务列表

        Args:
            db: 数据库会话
            source_name: 数据源名称筛选
            task_type: 任务类型筛选
            status: 任务状态筛选
            page: 页码
            page_size: 每页数量

        Returns:
            Tuple[List[CollectionTask], int]: (任务列表, 总数)
        """
        # 构建查询
        query = select(CollectionTask)

        # 应用筛选条件
        if source_name:
            query = query.where(CollectionTask.source_id == source_name)

        if task_type:
            query = query.where(CollectionTask.task_type == task_type)

        if status:
            query = query.where(CollectionTask.status == status)

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # 应用分页和排序
        query = query.order_by(CollectionTask.created_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await db.execute(query)
        tasks = result.scalars().all()

        return list(tasks), total

    @staticmethod
    async def get_task_by_id(
        db: AsyncSession,
        task_id: int
    ) -> Optional[CollectionTask]:
        """
        根据ID获取任务

        Args:
            db: 数据库会话
            task_id: 任务ID

        Returns:
            Optional[CollectionTask]: 任务对象
        """
        result = await db.execute(
            select(CollectionTask).where(CollectionTask.id == task_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_task(
        db: AsyncSession,
        task_data: CollectionTaskCreate
    ) -> CollectionTask:
        """
        创建采集任务

        Args:
            db: 数据库会话
            task_data: 任务数据

        Returns:
            CollectionTask: 创建的任务对象

        Raises:
            ValueError: 数据源不存在或未启用
        """
        # 检查数据源是否存在
        source_result = await db.execute(
            select(DataSource).where(DataSource.name == task_data.source_name)
        )
        source = source_result.scalar_one_or_none()

        if not source:
            raise ValueError(f"数据源 '{task_data.source_name}' 不存在")

        if not source.enabled:
            raise ValueError(f"数据源 '{task_data.source_name}' 未启用")

        # 创建任务
        task = CollectionTask(
            source_id=task_data.source_name,
            task_type=task_data.task_type,
            status="pending"
        )

        db.add(task)
        await db.commit()
        await db.refresh(task)

        return task

    @staticmethod
    async def create_batch_tasks(
        db: AsyncSession,
        batch_data: BatchCollectionCreate
    ) -> BatchCollectionResponse:
        """
        批量创建采集任务

        Args:
            db: 数据库会话
            batch_data: 批量任务数据

        Returns:
            BatchCollectionResponse: 批量任务响应
        """
        tasks = []
        errors = []
        success_count = 0
        failed_count = 0

        for source_name in batch_data.source_names:
            try:
                task_data = CollectionTaskCreate(
                    source_name=source_name,
                    task_type="manual"
                )
                task = await CollectionService.create_task(db, task_data)
                tasks.append(CollectionTaskResponse.model_validate(task))
                success_count += 1
            except ValueError as e:
                errors.append({
                    "source_name": source_name,
                    "error": str(e)
                })
                failed_count += 1

        return BatchCollectionResponse(
            total_tasks=len(batch_data.source_names),
            success_count=success_count,
            failed_count=failed_count,
            tasks=tasks,
            errors=errors
        )

    @staticmethod
    async def cancel_task(
        db: AsyncSession,
        task_id: int
    ) -> CollectionTask:
        """
        取消任务

        Args:
            db: 数据库会话
            task_id: 任务ID

        Returns:
            CollectionTask: 更新后的任务

        Raises:
            ValueError: 任务不存在或已完成
        """
        task = await CollectionService.get_task_by_id(db, task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")

        if task.status in ["completed", "cancelled"]:
            raise ValueError(f"任务已经{task.status}，无法取消")

        if task.status == "running":
            raise ValueError("任务正在运行中，无法取消")

        # 更新状态为cancelled
        await db.execute(
            update(CollectionTask)
            .where(CollectionTask.id == task_id)
            .values(status="cancelled", completed_at=datetime.utcnow())
        )
        await db.commit()

        await db.refresh(task)
        return task

    @staticmethod
    async def retry_task(
        db: AsyncSession,
        task_id: int
    ) -> CollectionTask:
        """
        重试失败的任务

        Args:
            db: 数据库会话
            task_id: 任务ID

        Returns:
            CollectionTask: 新创建的任务

        Raises:
            ValueError: 原任务不存在或未失败
        """
        original_task = await CollectionService.get_task_by_id(db, task_id)
        if not original_task:
            raise ValueError(f"任务 {task_id} 不存在")

        if original_task.status != "failed":
            raise ValueError(f"只能重试失败的任务，当前状态：{original_task.status}")

        # 创建新任务
        task_data = CollectionTaskCreate(
            source_name=original_task.source_id,
            task_type="manual"
        )
        new_task = await CollectionService.create_task(db, task_data)

        return new_task

    @staticmethod
    async def get_task_logs(
        db: AsyncSession,
        task_id: int
    ) -> Dict:
        """
        获取任务日志

        Args:
            db: 数据库会话
            task_id: 任务ID

        Returns:
            Dict: 任务日志信息

        Raises:
            ValueError: 任务不存在
        """
        task = await CollectionService.get_task_by_id(db, task_id)
        if not task:
            raise ValueError(f"任务 {task_id} 不存在")

        # 构造日志条目
        logs = []

        # 添加创建日志
        logs.append({
            "timestamp": task.created_at,
            "level": "INFO",
            "message": f"任务创建成功，数据源：{task.source_id}"
        })

        # 添加开始日志
        if task.started_at:
            logs.append({
                "timestamp": task.started_at,
                "level": "INFO",
                "message": "任务开始执行"
            })

        # 添加完成日志
        if task.completed_at:
            if task.status == "completed":
                logs.append({
                    "timestamp": task.completed_at,
                    "level": "INFO",
                    "message": f"任务完成，采集：{task.items_collected}项，处理：{task.items_processed}项，失败：{task.items_failed}项"
                })
            elif task.status == "failed":
                logs.append({
                    "timestamp": task.completed_at,
                    "level": "ERROR",
                    "message": f"任务失败：{task.error_message}"
                })
            elif task.status == "cancelled":
                logs.append({
                    "timestamp": task.completed_at,
                    "level": "WARNING",
                    "message": "任务已取消"
                })

        return {
            "task_id": task.id,
            "source_name": task.source_id,
            "status": task.status,
            "logs": logs,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at
        }
