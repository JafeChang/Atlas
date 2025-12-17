"""
Atlas 数据库管理模块

提供SQLite数据库的初始化、连接管理和基础操作功能。
遵循MVP架构设计，使用SQLite作为主要数据库，支持后续迁移到PostgreSQL。
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import sqlite_utils
from loguru import logger

from ..models.documents import (
    DocumentType, ProcessingStatus, SourceType, TaskStatus,
    RawDocument, ProcessedDocument, DataSource, CollectionTask,
    SystemMetrics, DuplicateGroup
)


class DatabaseError(Exception):
    """数据库相关错误"""
    pass


class AtlasDatabase:
    """Atlas 数据库管理器

    提供SQLite数据库的完整管理功能，包括：
    - 数据库初始化和模式管理
    - 连接池管理
    - 基础CRUD操作
    - 事务管理
    - 迁移支持
    """

    def __init__(self, db_path: Union[str, Path]):
        """初始化数据库管理器

        Args:
            db_path: 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 线程本地存储
        self._local = threading.local()

        # 使用sqlite-utils进行高级操作
        self._db: Optional[sqlite_utils.Database] = None

        # 立即初始化表结构
        self._initialize_tables()

        logger.info(f"初始化数据库: {self.db_path}")

    @property
    def db(self) -> sqlite_utils.Database:
        """获取数据库连接"""
        if self._db is None:
            self._db = sqlite_utils.Database(str(self.db_path))
            self._initialize_tables()
        return self._db

    @contextmanager
    def get_connection(self, autocommit: bool = True) -> sqlite3.Connection:
        """获取数据库连接的上下文管理器

        Args:
            autocommit: 是否自动提交事务

        Returns:
            SQLite连接对象
        """
        conn = sqlite3.connect(
            str(self.db_path),
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        conn.row_factory = sqlite3.Row  # 返回字典式行
        conn.execute("PRAGMA foreign_keys = ON")  # 启用外键约束
        conn.execute("PRAGMA journal_mode = WAL")  # 使用WAL模式
        conn.execute("PRAGMA synchronous = NORMAL")  # 适度的同步设置
        conn.execute("PRAGMA cache_size = -64000")  # 64MB缓存

        try:
            yield conn
            if autocommit:
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise DatabaseError(f"数据库操作失败: {e}") from e
        finally:
            conn.close()

    def _initialize_tables(self) -> None:
        """初始化数据库表结构"""
        logger.info("初始化数据库表结构")

        # 原始文档表
        self.db.executescript("""
            -- 原始文档表
            CREATE TABLE IF NOT EXISTS raw_documents (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_url TEXT,
                source_type TEXT NOT NULL,
                document_type TEXT NOT NULL,
                raw_content TEXT NOT NULL,
                raw_metadata TEXT DEFAULT '{}',
                collected_at TIMESTAMP NOT NULL,
                collector_version TEXT DEFAULT '0.1.0',
                processing_status TEXT DEFAULT 'pending',
                processing_error TEXT,
                processing_attempts INTEGER DEFAULT 0,
                content_hash TEXT,
                title TEXT,
                author TEXT,
                published_at TIMESTAMP,
                language TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );

            -- 处理后文档表
            CREATE TABLE IF NOT EXISTS processed_documents (
                id TEXT PRIMARY KEY,
                raw_document_id TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                content TEXT,
                structured_content TEXT DEFAULT '{}',
                extracted_metadata TEXT DEFAULT '{}',
                entities TEXT DEFAULT '[]',
                keywords TEXT DEFAULT '[]',
                categories TEXT DEFAULT '[]',
                processed_at TIMESTAMP NOT NULL,
                processor_version TEXT DEFAULT '0.1.0',
                processing_time_ms INTEGER,
                content_hash TEXT,
                similarity_group_id TEXT,
                similarity_score REAL,
                is_duplicate BOOLEAN DEFAULT FALSE,
                quality_score REAL,
                relevance_score REAL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY (raw_document_id) REFERENCES raw_documents(id) ON DELETE CASCADE
            );

            -- 数据源表
            CREATE TABLE IF NOT EXISTS data_sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                source_type TEXT NOT NULL,
                url TEXT,
                config TEXT DEFAULT '{}',
                enabled BOOLEAN DEFAULT TRUE,
                collection_interval INTEGER DEFAULT 3600,
                max_items_per_run INTEGER DEFAULT 100,
                retry_count INTEGER DEFAULT 3,
                timeout INTEGER DEFAULT 30,
                tags TEXT DEFAULT '[]',
                category TEXT,
                language TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                last_collected_at TIMESTAMP,
                last_success_at TIMESTAMP,
                collection_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                last_error TEXT
            );

            -- 采集任务表
            CREATE TABLE IF NOT EXISTS collection_tasks (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                task_type TEXT DEFAULT 'collection',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                created_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                worker_id TEXT,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                items_collected INTEGER DEFAULT 0,
                items_processed INTEGER DEFAULT 0,
                items_failed INTEGER DEFAULT 0,
                error_message TEXT,
                error_details TEXT DEFAULT '{}',
                config TEXT DEFAULT '{}',
                FOREIGN KEY (source_id) REFERENCES data_sources(id) ON DELETE CASCADE
            );

            -- 系统指标表
            CREATE TABLE IF NOT EXISTS system_metrics (
                id TEXT PRIMARY KEY,
                timestamp TIMESTAMP NOT NULL,
                total_raw_documents INTEGER DEFAULT 0,
                total_processed_documents INTEGER DEFAULT 0,
                documents_last_24h INTEGER DEFAULT 0,
                total_sources INTEGER DEFAULT 0,
                active_sources INTEGER DEFAULT 0,
                failed_sources INTEGER DEFAULT 0,
                pending_tasks INTEGER DEFAULT 0,
                running_tasks INTEGER DEFAULT 0,
                completed_tasks_last_24h INTEGER DEFAULT 0,
                cpu_usage_percent REAL,
                memory_usage_mb REAL,
                disk_usage_mb REAL,
                avg_processing_time_ms REAL,
                avg_collection_time_ms REAL,
                error_rate_last_24h REAL
            );

            -- 重复内容组表
            CREATE TABLE IF NOT EXISTS duplicate_groups (
                group_id TEXT PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                representative_document_id TEXT NOT NULL,
                document_count INTEGER DEFAULT 0,
                similarity_threshold REAL NOT NULL,
                total_content_length INTEGER DEFAULT 0,
                avg_content_length REAL DEFAULT 0.0,
                min_published_at TIMESTAMP,
                max_published_at TIMESTAMP,
                detection_method TEXT NOT NULL,
                detection_config TEXT DEFAULT '{}',
                FOREIGN KEY (representative_document_id) REFERENCES processed_documents(id)
            );

            -- 重复组成员关系表
            CREATE TABLE IF NOT EXISTS duplicate_group_members (
                group_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                similarity_score REAL NOT NULL,
                added_at TIMESTAMP NOT NULL,
                PRIMARY KEY (group_id, document_id),
                FOREIGN KEY (group_id) REFERENCES duplicate_groups(group_id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES processed_documents(id) ON DELETE CASCADE
            );
        """)

        # 创建索引
        self._create_indexes()

        logger.info("数据库表结构初始化完成")

    def _create_indexes(self) -> None:
        """创建数据库索引"""
        indexes = [
            # 原始文档表索引
            "CREATE INDEX IF NOT EXISTS idx_raw_documents_source_id ON raw_documents(source_id);",
            "CREATE INDEX IF NOT EXISTS idx_raw_documents_content_hash ON raw_documents(content_hash);",
            "CREATE INDEX IF NOT EXISTS idx_raw_documents_processing_status ON raw_documents(processing_status);",
            "CREATE INDEX IF NOT EXISTS idx_raw_documents_collected_at ON raw_documents(collected_at);",
            "CREATE INDEX IF NOT EXISTS idx_raw_documents_published_at ON raw_documents(published_at);",

            # 处理后文档表索引
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_raw_document_id ON processed_documents(raw_document_id);",
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_content_hash ON processed_documents(content_hash);",
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_similarity_group_id ON processed_documents(similarity_group_id);",
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_processed_at ON processed_documents(processed_at);",
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_is_duplicate ON processed_documents(is_duplicate);",
            "CREATE INDEX IF NOT EXISTS idx_processed_documents_quality_score ON processed_documents(quality_score);",

            # 数据源表索引
            "CREATE INDEX IF NOT EXISTS idx_data_sources_source_type ON data_sources(source_type);",
            "CREATE INDEX IF NOT EXISTS idx_data_sources_enabled ON data_sources(enabled);",
            "CREATE INDEX IF NOT EXISTS idx_data_sources_last_collected_at ON data_sources(last_collected_at);",
            "CREATE INDEX IF NOT EXISTS idx_data_sources_category ON data_sources(category);",

            # 采集任务表索引
            "CREATE INDEX IF NOT EXISTS idx_collection_tasks_source_id ON collection_tasks(source_id);",
            "CREATE INDEX IF NOT EXISTS idx_collection_tasks_status ON collection_tasks(status);",
            "CREATE INDEX IF NOT EXISTS idx_collection_tasks_created_at ON collection_tasks(created_at);",
            "CREATE INDEX IF NOT EXISTS idx_collection_tasks_priority ON collection_tasks(priority);",

            # 系统指标表索引
            "CREATE INDEX IF NOT EXISTS idx_system_metrics_timestamp ON system_metrics(timestamp);",

            # 重复内容组成员表索引
            "CREATE INDEX IF NOT EXISTS idx_duplicate_group_members_document_id ON duplicate_group_members(document_id);",
            "CREATE INDEX IF NOT EXISTS idx_duplicate_group_members_similarity_score ON duplicate_group_members(similarity_score);",
        ]

        for index_sql in indexes:
            self.db.executescript(index_sql)

        logger.info("数据库索引创建完成")

    def execute_query(self, query: str, params: Optional[Tuple] = None) -> List[Dict[str, Any]]:
        """执行查询语句

        Args:
            query: SQL查询语句
            params: 查询参数

        Returns:
            查询结果列表
        """
        with self.get_connection() as conn:
            cursor = conn.execute(query, params or ())
            return [dict(row) for row in cursor.fetchall()]

    def execute_update(self, query: str, params: Optional[Tuple] = None) -> int:
        """执行更新语句

        Args:
            query: SQL更新语句
            params: 更新参数

        Returns:
            影响的行数
        """
        with self.get_connection() as conn:
            cursor = conn.execute(query, params or ())
            return cursor.rowcount

    def execute_insert(self, query: str, params: Optional[Tuple] = None) -> str:
        """执行插入语句并返回插入的ID

        Args:
            query: SQL插入语句
            params: 插入参数

        Returns:
            插入记录的ID
        """
        with self.get_connection() as conn:
            cursor = conn.execute(query, params or ())
            return cursor.lastrowid

    def begin_transaction(self) -> None:
        """开始事务"""
        if not hasattr(self._local, 'in_transaction') or not self._local.in_transaction:
            with self.get_connection() as conn:
                conn.execute("BEGIN;")
            self._local.in_transaction = True

    def commit_transaction(self) -> None:
        """提交事务"""
        if hasattr(self._local, 'in_transaction') and self._local.in_transaction:
            with self.get_connection() as conn:
                conn.commit()
            self._local.in_transaction = False

    def rollback_transaction(self) -> None:
        """回滚事务"""
        if hasattr(self._local, 'in_transaction') and self._local.in_transaction:
            with self.get_connection() as conn:
                conn.rollback()
            self._local.in_transaction = False

    @contextmanager
    def transaction(self):
        """事务上下文管理器"""
        with self.get_connection(autocommit=False) as conn:
            try:
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def vacuum(self) -> None:
        """清理数据库，回收空间"""
        logger.info("开始数据库VACUUM操作")
        with self.get_connection() as conn:
            conn.execute("VACUUM;")
        logger.info("数据库VACUUM操作完成")

    def analyze(self) -> None:
        """分析数据库统计信息，优化查询性能"""
        logger.info("开始数据库ANALYZE操作")
        with self.get_connection() as conn:
            conn.execute("ANALYZE;")
        logger.info("数据库ANALYZE操作完成")

    def get_table_info(self, table_name: str) -> List[Dict[str, Any]]:
        """获取表结构信息

        Args:
            table_name: 表名

        Returns:
            表结构信息列表
        """
        # PRAGMA table_info 不支持参数绑定，需要直接拼接表名
        query = f"PRAGMA table_info({table_name})"
        return self.execute_query(query)

    def get_table_count(self, table_name: str) -> int:
        """获取表的记录数

        Args:
            table_name: 表名

        Returns:
            记录数
        """
        query = f"SELECT COUNT(*) as count FROM {table_name}"
        result = self.execute_query(query)
        return result[0]['count'] if result else 0

    def backup(self, backup_path: Union[str, Path]) -> None:
        """备份数据库

        Args:
            backup_path: 备份文件路径
        """
        backup_path = Path(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"开始数据库备份: {self.db_path} -> {backup_path}")

        with self.get_connection() as source:
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                source.backup(backup_conn)
                logger.info(f"数据库备份完成: {backup_path}")
            finally:
                backup_conn.close()

    def get_database_stats(self) -> Dict[str, Any]:
        """获取数据库统计信息

        Returns:
            数据库统计信息
        """
        stats = {}

        # 获取各表的记录数
        tables = ['raw_documents', 'processed_documents', 'data_sources',
                 'collection_tasks', 'system_metrics', 'duplicate_groups']

        for table in tables:
            stats[f"{table}_count"] = self.get_table_count(table)

        # 获取数据库文件大小
        if self.db_path.exists():
            stats["database_size_bytes"] = self.db_path.stat().st_size
            stats["database_size_mb"] = round(stats["database_size_bytes"] / 1024 / 1024, 2)

        # 获取SQLite统计信息
        with self.get_connection() as conn:
            cursor = conn.execute("PRAGMA database_list;")
            stats["database_info"] = cursor.fetchone()

        return stats

    def close(self) -> None:
        """关闭数据库连接"""
        if self._db is not None:
            self._db.close()
            self._db = None
        logger.info("数据库连接已关闭")

    def __del__(self):
        """析构函数，确保连接关闭"""
        self.close()


# 全局数据库实例
_database: Optional[AtlasDatabase] = None


def get_database(db_path: Optional[Union[str, Path]] = None) -> AtlasDatabase:
    """获取全局数据库实例

    Args:
        db_path: 数据库路径，如果为None则使用默认路径

    Returns:
        数据库实例
    """
    global _database
    if _database is None:
        if db_path is None:
            from .config import get_config
            config = get_config()
            db_path = config.data_dir / "atlas.db"

        _database = AtlasDatabase(db_path)
    return _database


def close_database() -> None:
    """关闭全局数据库实例"""
    global _database
    if _database is not None:
        _database.close()
        _database = None