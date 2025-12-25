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
import json
import sqlite3
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

from atlas.core.database_async import AsyncDatabaseManager, get_async_session
from atlas.models.schema import DataSource, RawDocument, ProcessedDocument, CollectionTask


class DatabaseMigration:
    """数据库迁移管理器"""

    def __init__(self, dry_run: bool = False, verbose: bool = False, db_path: str = "data/atlas.db"):
        """初始化迁移管理器

        Args:
            dry_run: 预演模式，不实际迁移
            verbose: 详细日志
            db_path: SQLite数据库路径
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.db_path = db_path

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

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """解析datetime字符串

        Args:
            value: datetime字符串或None

        Returns:
            datetime对象或None
        """
        if not value:
            return None

        # 如果已经是datetime对象，直接返回
        if isinstance(value, datetime):
            return value

        # 尝试解析字符串
        if isinstance(value, str):
            try:
                # ISO 8601格式
                if 'T' in value:
                    return datetime.fromisoformat(value.replace('Z', '+00:00'))
                # 其他格式
                return datetime.fromisoformat(value)
            except (ValueError, AttributeError):
                pass

        return None

    def _parse_json(self, value: Optional[str]) -> Optional[dict]:
        """解析JSON字符串

        Args:
            value: JSON字符串或None

        Returns:
            解析后的字典或None
        """
        if not value:
            return None

        if isinstance(value, dict):
            return value

        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                pass

        return None

    def _parse_json_array(self, value: Optional[str]) -> Optional[list]:
        """解析JSON数组字符串

        Args:
            value: JSON数组字符串或None

        Returns:
            解析后的列表或None
        """
        if not value:
            return None

        if isinstance(value, list):
            return value

        if isinstance(value, str):
            try:
                result = json.loads(value)
                if isinstance(result, list):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass

        return None

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

    def _query_sqlite(self, query: str) -> List[Dict]:
        """直接查询SQLite数据库

        Args:
            query: SQL查询语句

        Returns:
            查询结果列表
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            cursor.execute(query)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    async def migrate_data_sources(self, new_session: AsyncSession) -> int:
        """迁移数据源表

        Args:
            new_session: 新数据库会话

        Returns:
            迁移数量
        """
        logger.info("开始迁移数据源表...")

        # 从SQLite读取
        query = "SELECT * FROM data_sources"
        old_sources = self._query_sqlite(query)
        self.stats['data_sources']['total'] = len(old_sources)

        migrated_count = 0

        for old_source in old_sources:
            try:
                # 创建新对象（使用辅助函数转换字段）
                new_source = DataSource(
                    name=old_source['name'],
                    source_type=old_source['source_type'],
                    url=old_source.get('url'),
                    enabled=bool(old_source['enabled']),
                    config=self._parse_json(old_source.get('config')),
                    tags=self._parse_json_array(old_source.get('tags')),
                    category=old_source.get('category'),
                    language=old_source.get('language', 'zh-CN'),
                    collection_interval=old_source.get('collection_interval', 3600),
                    max_items_per_run=old_source.get('max_items_per_run', 100),
                    retry_count=old_source.get('retry_count', 3),
                    timeout=old_source.get('timeout', 30),
                    collection_count=old_source.get('collection_count', 0),
                    success_count=old_source.get('success_count', 0),
                    error_count=old_source.get('error_count', 0),
                    last_collected_at=self._parse_datetime(old_source.get('last_collected_at')),
                    last_success_at=self._parse_datetime(old_source.get('last_success_at')),
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
                # 回滚以清理Session状态
                await new_session.rollback()

        self.stats['data_sources']['migrated'] = migrated_count
        logger.info(f"数据源迁移完成: {migrated_count}/{len(old_sources)}")

        return migrated_count

    async def migrate_raw_documents(self, new_session: AsyncSession, limit: Optional[int] = None) -> int:
        """迁移原始文档表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话
            limit: 限制迁移数量

        Returns:
            迁移数量
        """
        logger.info("开始迁移原始文档表...")

        # 从SQLite读取（使用SQL查询）
        limit_clause = f"LIMIT {limit}" if limit else ""
        query = f"SELECT * FROM raw_documents ORDER BY collected_at DESC {limit_clause}"

        try:
            old_docs = self._query_sqlite(query)
            self.stats['raw_documents']['total'] = len(old_docs)
        except Exception as e:
            logger.error(f"查询原始文档失败: {e}")
            old_docs = []
            self.stats['raw_documents']['total'] = 0

        migrated_count = 0

        for old_doc in old_docs:
            try:
                # 处理UUID
                doc_id = old_doc.get('id')
                if doc_id:
                    try:
                        doc_id = UUID(doc_id)
                    except ValueError:
                        doc_id = uuid4()
                else:
                    doc_id = uuid4()

                # 创建新对象（使用辅助函数转换字段）
                new_doc = RawDocument(
                    id=doc_id,
                    source_id=old_doc.get('source_id', ''),
                    source_url=old_doc.get('source_url'),
                    source_type=old_doc.get('source_type'),
                    document_type=old_doc.get('document_type'),
                    raw_content=old_doc.get('raw_content'),
                    raw_metadata=self._parse_json(old_doc.get('raw_metadata')),
                    collected_at=self._parse_datetime(old_doc.get('collected_at')),
                    collector_version=old_doc.get('collector_version'),
                    processing_status=old_doc.get('processing_status', 'pending'),
                    processing_error=old_doc.get('processing_error'),
                    processing_attempts=old_doc.get('processing_attempts', 0),
                    content_hash=old_doc.get('content_hash'),
                    title=old_doc.get('title'),
                    author=old_doc.get('author'),
                    published_at=self._parse_datetime(old_doc.get('published_at')),
                    language=old_doc.get('language'),
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
                logger.error(f"迁移文档失败 {old_doc.get('id')}: {e}")
                self.failures.append(('raw_documents', old_doc.get('id'), str(e)))
                self.stats['raw_documents']['failed'] += 1
                # 回滚以清理Session状态
                await new_session.rollback()

        self.stats['raw_documents']['migrated'] = migrated_count
        logger.info(f"原始文档迁移完成: {migrated_count}/{len(old_docs)}")

        return migrated_count

    async def migrate_processed_documents(self, new_session: AsyncSession, limit: Optional[int] = None) -> int:
        """迁移处理后文档表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话
            limit: 限制迁移数量

        Returns:
            迁移数量
        """
        logger.info("开始迁移处理后文档表...")

        # 从SQLite读取（使用SQL查询）
        limit_clause = f"LIMIT {limit}" if limit else ""
        query = f"SELECT * FROM processed_documents ORDER BY processed_at DESC {limit_clause}"

        try:
            old_docs = self._query_sqlite(query)
            self.stats['processed_documents']['total'] = len(old_docs)
        except Exception as e:
            logger.error(f"查询处理后文档失败: {e}")
            old_docs = []
            self.stats['processed_documents']['total'] = 0

        migrated_count = 0

        for old_doc in old_docs:
            try:
                # 处理UUID
                doc_id = old_doc.get('id')
                if doc_id:
                    try:
                        doc_id = UUID(doc_id)
                    except ValueError:
                        doc_id = uuid4()
                else:
                    doc_id = uuid4()

                raw_doc_id = old_doc.get('raw_document_id')
                if raw_doc_id:
                    try:
                        raw_doc_id = UUID(raw_doc_id)
                    except ValueError:
                        raw_doc_id = uuid4()
                else:
                    raw_doc_id = uuid4()

                # 创建新对象（使用辅助函数转换字段）
                new_doc = ProcessedDocument(
                    id=doc_id,
                    raw_document_id=raw_doc_id,
                    title=old_doc.get('title', ''),
                    summary=old_doc.get('summary'),
                    content=old_doc.get('content'),
                    structured_content=self._parse_json(old_doc.get('structured_content')),
                    extracted_metadata=self._parse_json(old_doc.get('extracted_metadata')),
                    entities=self._parse_json(old_doc.get('entities')),
                    keywords=self._parse_json_array(old_doc.get('keywords')),
                    categories=self._parse_json_array(old_doc.get('categories')),
                    processed_at=self._parse_datetime(old_doc.get('processed_at')),
                    processor_version=old_doc.get('processor_version'),
                    processing_time_ms=old_doc.get('processing_time_ms'),
                    content_hash=old_doc.get('content_hash'),
                    similarity_group_id=UUID(old_doc['similarity_group_id']) if old_doc.get('similarity_group_id') else None,
                    similarity_score=old_doc.get('similarity_score'),
                    is_duplicate=bool(old_doc.get('is_duplicate', False)),
                    quality_score=old_doc.get('quality_score'),
                    relevance_score=old_doc.get('relevance_score'),
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
                logger.error(f"迁移处理后文档失败 {old_doc.get('id')}: {e}")
                self.failures.append(('processed_documents', old_doc.get('id'), str(e)))
                self.stats['processed_documents']['failed'] += 1
                # 回滚以清理Session状态
                await new_session.rollback()

        self.stats['processed_documents']['migrated'] = migrated_count
        logger.info(f"处理后文档迁移完成: {migrated_count}/{len(old_docs)}")

        return migrated_count

    async def migrate_collection_tasks(self, new_session: AsyncSession) -> int:
        """迁移采集任务表

        Args:
            old_db: 旧数据库实例
            new_session: 新数据库会话

        Returns:
            迁移数量
        """
        logger.info("开始迁移采集任务表...")

        # 从SQLite读取（使用SQL查询）
        query = "SELECT * FROM collection_tasks ORDER BY created_at DESC"

        try:
            old_tasks = self._query_sqlite(query)
            self.stats['collection_tasks']['total'] = len(old_tasks)
        except Exception as e:
            logger.error(f"查询采集任务失败: {e}")
            old_tasks = []
            self.stats['collection_tasks']['total'] = 0

        migrated_count = 0

        for old_task in old_tasks:
            try:
                # 创建新对象
                new_task = CollectionTask(
                    source_id=old_task.get('source_id', ''),
                    task_type=old_task.get('task_type', 'manual'),
                    status=old_task.get('status', 'pending'),
                    created_at=self._parse_datetime(old_task.get('created_at')),
                    started_at=self._parse_datetime(old_task.get('started_at')),
                    completed_at=self._parse_datetime(old_task.get('completed_at')),
                    items_collected=old_task.get('items_collected', 0),
                    items_processed=old_task.get('items_processed', 0),
                    items_failed=old_task.get('items_failed', 0),
                    error_message=old_task.get('error_message'),
                )

                if self.dry_run:
                    logger.debug(f"[DRY-RUN] 将迁移任务: {new_task.id}")
                else:
                    new_session.add(new_task)

                migrated_count += 1

            except Exception as e:
                logger.error(f"迁移任务失败 {old_task.get('id')}: {e}")
                self.failures.append(('collection_tasks', str(old_task.get('id')), str(e)))
                self.stats['collection_tasks']['failed'] += 1
                # 回滚以清理Session状态
                await new_session.rollback()

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

        # SQLite数据库路径
        logger.info(f"SQLite数据库: {self.db_path}")

        # PostgreSQL数据库（新）
        new_db = AsyncDatabaseManager()
        await new_db.initialize()
        logger.info(f"目标数据库: {new_db._get_database_type()}")

        # 执行迁移
        async with get_async_session() as session:
            if tables is None or 'data_sources' in tables:
                await self.migrate_data_sources(session)

            if tables is None or 'raw_documents' in tables:
                await self.migrate_raw_documents(session, limit)

            if tables is None or 'processed_documents' in tables:
                await self.migrate_processed_documents(session, limit)

            if tables is None or 'collection_tasks' in tables:
                await self.migrate_collection_tasks(session)

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
@click.option('--force', '-f', is_flag=True, help='强制执行，跳过确认')
def main(dry_run: bool, verbose: bool, table: tuple, limit: int, force: bool):
    """Atlas数据库迁移工具"""

    if not dry_run and not force:
        print("\n⚠️  即将开始数据库迁移！")
        print("⚠️  建议先备份SQLite数据库！")
        print("\n建议先使用 --dry-run 预演模式测试\n")

        response = input("确定要继续吗？(yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("迁移已取消")
            return
    elif not dry_run and force:
        print("\n⚠️  强制模式执行迁移！")

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
