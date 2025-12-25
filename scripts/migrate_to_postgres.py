#!/usr/bin/env python3
"""
Atlas 数据库迁移脚本 - SQLite到PostgreSQL

将现有的SQLite数据库迁移到PostgreSQL数据库。

使用方法:
    # 预演模式（不实际迁移）
    uv run python -m scripts.migrate_to_postgres --dry-run

    # 完整迁移
    uv run python -m scripts.migrate_to_postgres

    # 仅迁移指定表
    uv run python -m scripts.migrate_to_postgres --table data_sources

    # 详细日志
    uv run python -m scripts.migrate_to_postgres --verbose
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from uuid import UUID, uuid4

import click
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from atlas.core.database import AtlasDatabase
from atlas.core.database_async import AsyncDatabaseManager, get_async_session
from atlas.models.schema import DataSource, RawDocument, ProcessedDocument, CollectionTask
from atlas.models.documents import (
    DataSource as OldDataSource,
    RawDocument as OldRawDocument,
    ProcessedDocument as OldProcessedDocument,
    CollectionTask as OldCollectionTask,
)


class DatabaseMigration:
    """数据库迁移管理器"""

    def __init__(self, dry_run: bool = False, verbose: bool = False):
        """初始化迁移管理器

        Args:
            dry_run: 预演模式，不实际迁移
            verbose: 详细日志
        """
        self.dry_run = dry_run
        self.verbose = verbose

        # 统计信息
        self.stats = {
            'data_sources': {'total': 0, 'migrated': 0, 'failed': 0},
            'raw_documents': {'total': 0, 'migrated': 0, 'failed': 0},
            'processed_documents': {'total': 0, 'migrated': 0, 'failed': 0},
            'collection_tasks': {'total': 0, 'migrated': 0, 'failed': 0},
            'start_time': None,
            'end_time': None
        }

        # 失败记录
        self.failures: List[tuple] = []

        # 配置日志
        self._setup_logging()

    def _setup_logging(self):
        """配置日志"""
        log_level = "DEBUG" if self.verbose else "INFO"
        logger.remove()
        logger.add(sys.stderr, level=log_level)
        logger.add(
            "logs/migration_{time}.log",
            rotation="10 MB",
            level="DEBUG",
            encoding="utf-8"
        )

    async def migrate_data_sources(
        self,
        old_db: AtlasDatabase,
        new_session: AsyncSession
    ) -> int:
        """迁移数据源表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话

        Returns:
            迁移数量
        """
        logger.info("开始迁移数据源表...")

        # 从SQLite读取（使用SQL查询）
        query = "SELECT * FROM data_sources"
        old_sources = old_db.execute_query(query)
        self.stats['data_sources']['total'] = len(old_sources)

        migrated_count = 0

        for old_source in old_sources:
            try:
                # 创建新对象
                new_source = DataSource(
                    name=old_source['name'],
                    source_type=old_source['source_type'],
                    url=old_source.get('url'),
                    enabled=bool(old_source['enabled']),
                    config=old_source.get('config'),
                    tags=old_source.get('tags'),
                    category=old_source.get('category'),
                    language=old_source.get('language', 'zh-CN'),
                    collection_interval=old_source.get('collection_interval', 3600),
                    max_items_per_run=old_source.get('max_items_per_run', 100),
                    retry_count=old_source.get('retry_count', 3),
                    timeout=old_source.get('timeout', 30),
                    collection_count=old_source.get('collection_count', 0),
                    success_count=old_source.get('success_count', 0),
                    error_count=old_source.get('error_count', 0),
                    last_collected_at=old_source.get('last_collected_at'),
                    last_success_at=old_source.get('last_success_at'),
                    last_error=old_source.get('last_error'),
                )

                if self.dry_run:
                    logger.debug(f"[DRY-RUN] 将迁移数据源: {old_source['name']}")
                else:
                    new_session.add(new_source)
                    await new_session.flush()

                migrated_count += 1

            except Exception as e:
                logger.error(f"迁移数据源失败 {old_source.get('name')}: {e}")
                self.failures.append(('data_sources', old_source.get('name'), str(e)))
                self.stats['data_sources']['failed'] += 1

        self.stats['data_sources']['migrated'] = migrated_count
        logger.info(f"数据源迁移完成: {migrated_count}/{len(old_sources)}")

        return migrated_count

    async def migrate_raw_documents(
        self,
        old_db: AtlasDatabase,
        new_session: AsyncSession,
        limit: Optional[int] = None
    ) -> int:
        """迁移原始文档表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话
            limit: 限制迁移数量

        Returns:
            迁移数量
        """
        logger.info("开始迁移原始文档表...")

        # 从SQLite读取
        old_docs = old_db.get_all_raw_documents(limit=limit)
        self.stats['raw_documents']['total'] = len(old_docs)

        migrated_count = 0

        for old_doc in old_docs:
            try:
                # 创建新对象
                new_doc = RawDocument(
                    id=UUID(str(old_doc.id)) if old_doc.id else uuid4(),
                    source_id=old_doc.source_id,
                    source_url=str(old_doc.source_url) if old_doc.source_url else None,
                    source_type=old_doc.source_type.value if old_doc.source_type else None,
                    document_type=old_doc.document_type.value if old_doc.document_type else None,
                    raw_content=old_doc.raw_content,
                    raw_metadata=old_doc.raw_metadata,
                    collected_at=old_doc.collected_at,
                    collector_version=old_doc.collector_version,
                    processing_status=old_doc.processing_status.value,
                    processing_error=old_doc.processing_error,
                    processing_attempts=old_doc.processing_attempts,
                    content_hash=old_doc.content_hash,
                    title=old_doc.title,
                    author=old_doc.author,
                    published_at=old_doc.published_at,
                    language=old_doc.language,
                )

                if self.dry_run:
                    if migrated_count < 5 or self.verbose:
                        logger.debug(f"[DRY-RUN] 将迁移文档: {new_doc.id}")
                else:
                    new_session.add(new_doc)
                    if migrated_count % 100 == 0:
                        await new_session.flush()

                migrated_count += 1

            except Exception as e:
                logger.error(f"迁移文档失败 {old_doc.id}: {e}")
                self.failures.append(('raw_documents', str(old_doc.id), str(e)))
                self.stats['raw_documents']['failed'] += 1

        self.stats['raw_documents']['migrated'] = migrated_count
        logger.info(f"原始文档迁移完成: {migrated_count}/{len(old_docs)}")

        return migrated_count

    async def migrate_processed_documents(
        self,
        old_db: AtlasDatabase,
        new_session: AsyncSession,
        limit: Optional[int] = None
    ) -> int:
        """迁移处理后文档表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话
            limit: 限制迁移数量

        Returns:
            迁移数量
        """
        logger.info("开始迁移处理后文档表...")

        # 从SQLite读取
        old_docs = old_db.get_all_processed_documents(limit=limit)
        self.stats['processed_documents']['total'] = len(old_docs)

        migrated_count = 0

        for old_doc in old_docs:
            try:
                # 创建新对象
                new_doc = ProcessedDocument(
                    id=UUID(str(old_doc.id)) if old_doc.id else uuid4(),
                    raw_document_id=UUID(str(old_doc.raw_document_id)) if old_doc.raw_document_id else uuid4(),
                    title=old_doc.title,
                    summary=old_doc.summary,
                    content=old_doc.content,
                    structured_content=old_doc.structured_content,
                    extracted_metadata=old_doc.extracted_metadata,
                    entities=old_doc.entities,
                    keywords=old_doc.keywords,
                    categories=old_doc.categories,
                    processed_at=old_doc.processed_at,
                    processor_version=old_doc.processor_version,
                    processing_time_ms=old_doc.processing_time_ms,
                    content_hash=old_doc.content_hash,
                    similarity_group_id=UUID(str(old_doc.similarity_group_id)) if old_doc.similarity_group_id else None,
                    similarity_score=old_doc.similarity_score,
                    is_duplicate=old_doc.is_duplicate,
                    quality_score=old_doc.quality_score,
                    relevance_score=old_doc.relevance_score,
                )

                if self.dry_run:
                    if migrated_count < 5 or self.verbose:
                        logger.debug(f"[DRY-RUN] 将迁移处理后文档: {new_doc.id}")
                else:
                    new_session.add(new_doc)
                    if migrated_count % 100 == 0:
                        await new_session.flush()

                migrated_count += 1

            except Exception as e:
                logger.error(f"迁移处理后文档失败 {old_doc.id}: {e}")
                self.failures.append(('processed_documents', str(old_doc.id), str(e)))
                self.stats['processed_documents']['failed'] += 1

        self.stats['processed_documents']['migrated'] = migrated_count
        logger.info(f"处理后文档迁移完成: {migrated_count}/{len(old_docs)}")

        return migrated_count

    async def migrate_collection_tasks(
        self,
        old_db: AtlasDatabase,
        new_session: AsyncSession
    ) -> int:
        """迁移采集任务表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话

        Returns:
            迁移数量
        """
        logger.info("开始迁移采集任务表...")

        # 从SQLite读取
        old_tasks = old_db.get_all_collection_tasks()
        self.stats['collection_tasks']['total'] = len(old_tasks)

        migrated_count = 0

        for old_task in old_tasks:
            try:
                # 创建新对象
                new_task = CollectionTask(
                    source_id=old_task.source_id,
                    task_type=old_task.task_type.value,
                    status=old_task.status.value,
                    created_at=old_task.created_at,
                    started_at=old_task.started_at,
                    completed_at=old_task.completed_at,
                    items_collected=old_task.items_collected,
                    items_processed=old_task.items_processed,
                    items_failed=old_task.items_failed,
                    error_message=old_task.error_message,
                )

                if self.dry_run:
                    logger.debug(f"[DRY-RUN] 将迁移任务: {new_task.id}")
                else:
                    new_session.add(new_task)

                migrated_count += 1

            except Exception as e:
                logger.error(f"迁移任务失败 {old_task.id}: {e}")
                self.failures.append(('collection_tasks', str(old_task.id), str(e)))
                self.stats['collection_tasks']['failed'] += 1

        self.stats['collection_tasks']['migrated'] = migrated_count
        logger.info(f"采集任务迁移完成: {migrated_count}/{len(old_tasks)}")

        return migrated_count

    async def migrate(self, tables: Optional[List[str]] = None, limit: Optional[int] = None):
        """执行迁移

        Args:
            tables: 要迁移的表列表，None表示全部
            limit: 限制每个表迁移的记录数
        """
        self.stats['start_time'] = datetime.now()

        logger.info("=" * 60)
        logger.info("Atlas 数据库迁移 - SQLite -> PostgreSQL")
        logger.info("=" * 60)

        if self.dry_run:
            logger.info("⚠️  预演模式：不会实际迁移数据")

        # 初始化数据库连接
        logger.info("\n初始化数据库连接...")

        # SQLite数据库（旧）
        old_db = AtlasDatabase("data/atlas.db")
        logger.info(f"SQLite数据库: {old_db.db_path}")

        # PostgreSQL数据库（新）
        new_db = AsyncDatabaseManager()
        await new_db.initialize()
        logger.info(f"PostgreSQL数据库: {new_db._get_database_type()}")

        # 执行迁移
        async with get_async_session() as session:
            if tables is None or 'data_sources' in tables:
                await self.migrate_data_sources(old_db, session)

            if tables is None or 'raw_documents' in tables:
                await self.migrate_raw_documents(old_db, session, limit)

            if tables is None or 'processed_documents' in tables:
                await self.migrate_processed_documents(old_db, session, limit)

            if tables is None or 'collection_tasks' in tables:
                await self.migrate_collection_tasks(old_db, session)

            if not self.dry_run:
                await session.commit()

        await new_db.close()

        self.stats['end_time'] = datetime.now()

        # 最终统计
        logger.info("\n" + "=" * 60)
        logger.info("迁移完成！")
        logger.info("=" * 60)
        self._print_final_stats()

        # 显示失败列表
        if self.failures:
            logger.warning(f"\n失败记录 ({len(self.failures)}):")
            for table, id_, error in self.failures[:10]:
                logger.warning(f"  - {table}: {id_}")
                logger.warning(f"    错误: {error}")

            if len(self.failures) > 10:
                logger.warning(f"  ... 还有 {len(self.failures) - 10} 个失败记录")

    def _print_final_stats(self):
        """显示最终统计"""
        elapsed = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        logger.info(f"\n数据源:")
        logger.info(f"  总数: {self.stats['data_sources']['total']}")
        logger.info(f"  迁移: {self.stats['data_sources']['migrated']}")
        logger.info(f"  失败: {self.stats['data_sources']['failed']}")

        logger.info(f"\n原始文档:")
        logger.info(f"  总数: {self.stats['raw_documents']['total']}")
        logger.info(f"  迁移: {self.stats['raw_documents']['migrated']}")
        logger.info(f"  失败: {self.stats['raw_documents']['failed']}")

        logger.info(f"\n处理后文档:")
        logger.info(f"  总数: {self.stats['processed_documents']['total']}")
        logger.info(f"  迁移: {self.stats['processed_documents']['migrated']}")
        logger.info(f"  失败: {self.stats['processed_documents']['failed']}")

        logger.info(f"\n采集任务:")
        logger.info(f"  总数: {self.stats['collection_tasks']['total']}")
        logger.info(f"  迁移: {self.stats['collection_tasks']['migrated']}")
        logger.info(f"  失败: {self.stats['collection_tasks']['failed']}")

        total_migrated = (
            self.stats['data_sources']['migrated'] +
            self.stats['raw_documents']['migrated'] +
            self.stats['processed_documents']['migrated'] +
            self.stats['collection_tasks']['migrated']
        )

        total_failed = (
            self.stats['data_sources']['failed'] +
            self.stats['raw_documents']['failed'] +
            self.stats['processed_documents']['failed'] +
            self.stats['collection_tasks']['failed']
        )

        logger.info(f"\n总计:")
        logger.info(f"  迁移: {total_migrated}")
        logger.info(f"  失败: {total_failed}")
        logger.info(f"  耗时: {elapsed:.2f} 秒")

        if total_migrated + total_failed > 0:
            success_rate = (total_migrated / (total_migrated + total_failed)) * 100
            logger.info(f"  成功率: {success_rate:.1f}%")


@click.command()
@click.option('--dry-run', is_flag=True, help='预演模式，不实际迁移数据')
@click.option('--verbose', '-v', is_flag=True, help='详细日志')
@click.option('--table', multiple=True, help='仅迁移指定表')
@click.option('--limit', type=int, help='限制每个表迁移的记录数')
def main(dry_run: bool, verbose: bool, table: tuple, limit: int):
    """Atlas数据库迁移工具"""

    if not dry_run:
        print("\n⚠️  即将开始数据库迁移！")
        print("⚠️  建议先备份SQLite数据库！")
        print("\n建议先使用 --dry-run 预演模式测试\n")

        response = input("确定要继续吗？(yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("迁移已取消")
            return

    # 执行迁移
    migration = DatabaseMigration(
        dry_run=dry_run,
        verbose=verbose
    )

    tables = list(table) if table else None

    try:
        asyncio.run(migration.migrate(tables=tables, limit=limit))
    except KeyboardInterrupt:
        logger.warning("\n迁移被用户中断")
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        raise


if __name__ == '__main__':
    main()
