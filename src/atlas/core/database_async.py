"""
Atlas 异步数据库管理器

支持SQLite和PostgreSQL双后端的异步数据库访问。
使用SQLAlchemy 2.0异步API。
"""

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy import select, update, delete, func

try:
    from ..config import get_config
    from ..models.schema import Base, DataSource, RawDocument, ProcessedDocument, CollectionTask
except ImportError:
    from atlas.core.config import get_config
    from atlas.models.schema import Base, DataSource, RawDocument, ProcessedDocument, CollectionTask


class AsyncDatabaseManager:
    """异步数据库管理器

    支持SQLite和PostgreSQL双后端，提供统一的异步数据库访问接口。
    """

    def __init__(self, database_url: Optional[str] = None):
        """初始化数据库管理器

        Args:
            database_url: 数据库连接URL，如果为None则从配置读取
        """
        if database_url is None:
            database_url = self._get_database_url()

        self.database_url = database_url
        self.engine: Optional[AsyncEngine] = None
        self.async_session_maker: Optional[async_sessionmaker] = None

        logger.info(f"初始化异步数据库管理器: {self._get_database_type()}")

    def _get_database_url(self) -> str:
        """从配置获取数据库URL

        Returns:
            数据库连接URL
        """
        config = get_config()

        # 检查环境变量
        db_type = os.environ.get("ATLAS_DATABASE_TYPE", "").lower()

        if db_type == "postgresql":
            # PostgreSQL配置
            pg_host = os.environ.get("ATLAS_DATABASE_HOST", "localhost")
            pg_port = os.environ.get("ATLAS_DATABASE_PORT", "5432")
            pg_user = os.environ.get("ATLAS_DATABASE_USER", "atlas")
            pg_password = os.environ.get("ATLAS_DATABASE_PASSWORD", "atlas")
            pg_database = os.environ.get("ATLAS_DATABASE_NAME", "atlas")

            return f"postgresql+asyncpg://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}"

        else:
            # SQLite配置（默认）
            db_path = os.environ.get("ATLAS_DATABASE_PATH", "data/atlas.db")
            # 确保目录存在
            os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else '.', exist_ok=True)
            return f"sqlite+aiosqlite:///{db_path}"

    def _get_database_type(self) -> str:
        """获取数据库类型

        Returns:
            数据库类型名称
        """
        if self.database_url.startswith("postgresql"):
            return "PostgreSQL"
        elif self.database_url.startswith("sqlite"):
            return "SQLite"
        else:
            return "Unknown"

    async def initialize(self) -> None:
        """初始化数据库连接和表结构"""
        # 创建引擎
        if self.database_url.startswith("sqlite"):
            # SQLite使用NullPool
            self.engine = create_async_engine(
                self.database_url,
                echo=False,
                poolclass=NullPool,
                connect_args={"check_same_thread": False}
            )
        else:
            # PostgreSQL使用连接池（异步引擎使用默认的AsyncAdaptedQueuePool）
            self.engine = create_async_engine(
                self.database_url,
                echo=False,
                pool_size=10,
                max_overflow=20
            )

        # 创建会话工厂
        self.async_session_maker = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

        logger.info(f"数据库引擎已创建: {self._get_database_type()}")

        # 创建表（如果不存在）
        await self.create_tables()

    async def create_tables(self) -> None:
        """创建所有表"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("数据库表已创建")

    async def drop_tables(self) -> None:
        """删除所有表（谨慎使用）"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

        logger.warning("数据库表已删除")

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话

        Yields:
            异步数据库会话
        """
        if self.async_session_maker is None:
            raise RuntimeError("数据库管理器未初始化，请先调用 initialize()")

        async with self.async_session_maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    async def close(self) -> None:
        """关闭数据库连接"""
        if self.engine:
            await self.engine.dispose()
            logger.info("数据库连接已关闭")

    async def health_check(self) -> bool:
        """检查数据库健康状态

        Returns:
            数据库是否健康
        """
        try:
            async with self.get_session() as session:
                # 执行简单查询测试连接
                await session.execute(select(1))
            return True
        except Exception as e:
            logger.error(f"数据库健康检查失败: {e}")
            return False

    async def get_stats(self) -> dict:
        """获取数据库统计信息

        Returns:
            统计信息字典
        """
        async with self.get_session() as session:
            # 数据源统计
            source_count = await session.scalar(select(func.count()).select_from(DataSource))
            enabled_sources = await session.scalar(
                select(func.count()).select_from(DataSource).where(DataSource.enabled == True)
            )

            # 文档统计
            raw_doc_count = await session.scalar(select(func.count()).select_from(RawDocument))
            processed_doc_count = await session.scalar(select(func.count()).select_from(ProcessedDocument))

            # 任务统计
            task_count = await session.scalar(select(func.count()).select_from(CollectionTask))
            pending_tasks = await session.scalar(
                select(func.count()).select_from(CollectionTask).where(CollectionTask.status == "pending")
            )

            return {
                "database_type": self._get_database_type(),
                "data_sources": {
                    "total": source_count or 0,
                    "enabled": enabled_sources or 0
                },
                "documents": {
                    "raw": raw_doc_count or 0,
                    "processed": processed_doc_count or 0
                },
                "tasks": {
                    "total": task_count or 0,
                    "pending": pending_tasks or 0
                }
            }

    def get_info(self) -> dict:
        """获取数据库信息

        Returns:
            数据库信息字典
        """
        return {
            "database_type": self._get_database_type(),
            "database_url": self._database_url.split("@")[-1] if "@" in self._database_url else self._database_url,
            "initialized": self.engine is not None,
        }


# 全局数据库管理器实例
_db_manager: Optional[AsyncDatabaseManager] = None


async def get_async_db_manager() -> AsyncDatabaseManager:
    """获取全局异步数据库管理器实例

    Returns:
        异步数据库管理器实例
    """
    global _db_manager

    if _db_manager is None:
        _db_manager = AsyncDatabaseManager()
        await _db_manager.initialize()

    return _db_manager


def reset_async_db_manager() -> None:
    """重置全局数据库管理器（用于测试）"""
    global _db_manager
    _db_manager = None


# 便捷函数
@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话的便捷函数

    Yields:
        异步数据库会话
    """
    db_manager = await get_async_db_manager()
    async with db_manager.get_session() as session:
        yield session
