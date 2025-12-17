"""
Atlas 数据访问层 (CRUD Operations)

提供对数据库和文件存储的统一数据访问接口，包括：
- 原始文档的增删改查
- 处理后文档的增删改查
- 数据源管理
- 任务管理
- 统计查询
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from loguru import logger

from .database import AtlasDatabase, get_database
from .storage import FileStorageManager, get_storage_manager
from ..models.documents import (
    DocumentType, ProcessingStatus, SourceType, TaskStatus,
    RawDocument, ProcessedDocument, DataSource, CollectionTask,
    SystemMetrics, DuplicateGroup
)


class DataRepository:
    """数据仓库类，提供统一的数据访问接口"""

    def __init__(self,
                 database: Optional[AtlasDatabase] = None,
                 storage: Optional[FileStorageManager] = None):
        """初始化数据仓库

        Args:
            database: 数据库实例，如果为None则使用全局实例
            storage: 存储管理器实例，如果为None则使用全局实例
        """
        self.database = database or get_database()
        self.storage = storage or get_storage_manager()

        logger.info("数据仓库初始化完成")

    # ==================== 原始文档操作 ====================

    async def create_raw_document(self, document: RawDocument) -> UUID:
        """创建原始文档

        Args:
            document: 原始文档对象

        Returns:
            文档ID
        """
        try:
            # 存储到文件系统
            file_path = await self.storage.store_raw_document(document)

            # 存储到数据库
            with self.database.transaction():
                query = """
                INSERT INTO raw_documents (
                    id, source_id, source_url, source_type, document_type,
                    raw_content, raw_metadata, collected_at, collector_version,
                    processing_status, processing_error, processing_attempts,
                    content_hash, title, author, published_at, language,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """

                params = (
                    str(document.id),
                    document.source_id,
                    str(document.source_url) if document.source_url else None,
                    document.source_type.value,
                    document.document_type.value,
                    document.raw_content,
                    json.dumps(document.raw_metadata),
                    document.collected_at,
                    document.collector_version,
                    document.processing_status.value,
                    document.processing_error,
                    document.processing_attempts,
                    document.content_hash,
                    document.title,
                    document.author,
                    document.published_at,
                    document.language,
                    document.created_at,
                    document.updated_at,
                )

                self.database.execute_insert(query, params)

            logger.info(f"原始文档创建成功: {document.id}")
            return document.id

        except Exception as e:
            logger.error(f"创建原始文档失败: {e}")
            raise

    async def get_raw_document(self, document_id: Union[str, UUID]) -> Optional[RawDocument]:
        """获取原始文档

        Args:
            document_id: 文档ID

        Returns:
            原始文档对象，如果不存在返回None
        """
        try:
            # 从数据库获取基础信息
            query = """
            SELECT * FROM raw_documents WHERE id = ?
            """
            results = self.database.execute_query(query, (str(document_id),))

            if not results:
                return None

            doc_data = results[0]

            # 从文件系统获取完整内容
            file_data = await self.storage.retrieve_raw_document(document_id)
            if not file_data:
                logger.warning(f"文件系统中未找到文档: {document_id}")
                return None

            # 构造文档对象
            return RawDocument(
                id=UUID(doc_data['id']),
                source_id=doc_data['source_id'],
                source_url=doc_data['source_url'],
                source_type=SourceType(doc_data['source_type']),
                document_type=DocumentType(doc_data['document_type']),
                raw_content=doc_data['raw_content'],
                raw_metadata=json.loads(doc_data['raw_metadata']),
                collected_at=datetime.fromisoformat(doc_data['collected_at']),
                collector_version=doc_data['collector_version'],
                processing_status=ProcessingStatus(doc_data['processing_status']),
                processing_error=doc_data['processing_error'],
                processing_attempts=doc_data['processing_attempts'],
                content_hash=doc_data['content_hash'],
                title=doc_data['title'],
                author=doc_data['author'],
                published_at=datetime.fromisoformat(doc_data['published_at']) if doc_data['published_at'] else None,
                language=doc_data['language'],
                created_at=datetime.fromisoformat(doc_data['created_at']),
                updated_at=datetime.fromisoformat(doc_data['updated_at']),
            )

        except Exception as e:
            logger.error(f"获取原始文档失败 {document_id}: {e}")
            return None

    async def update_raw_document(self, document: RawDocument) -> bool:
        """更新原始文档

        Args:
            document: 更新后的文档对象

        Returns:
            是否更新成功
        """
        try:
            # 更新文件存储
            await self.storage.store_raw_document(document)

            # 更新数据库
            query = """
            UPDATE raw_documents SET
                source_url = ?, source_type = ?, document_type = ?,
                raw_content = ?, raw_metadata = ?,
                processing_status = ?, processing_error = ?, processing_attempts = ?,
                content_hash = ?, title = ?, author = ?, published_at = ?, language = ?,
                updated_at = ?
            WHERE id = ?
            """

            params = (
                str(document.source_url) if document.source_url else None,
                document.source_type.value,
                document.document_type.value,
                document.raw_content,
                json.dumps(document.raw_metadata),
                document.processing_status.value,
                document.processing_error,
                document.processing_attempts,
                document.content_hash,
                document.title,
                document.author,
                document.published_at,
                document.language,
                datetime.utcnow(),  # 更新时间
                str(document.id),
            )

            affected_rows = self.database.execute_update(query, params)

            if affected_rows > 0:
                logger.info(f"原始文档更新成功: {document.id}")
                return True
            else:
                logger.warning(f"原始文档不存在或未更新: {document.id}")
                return False

        except Exception as e:
            logger.error(f"更新原始文档失败 {document.id}: {e}")
            return False

    async def delete_raw_document(self, document_id: Union[str, UUID]) -> bool:
        """删除原始文档

        Args:
            document_id: 文档ID

        Returns:
            是否删除成功
        """
        try:
            # 从数据库删除（会级联删除相关处理文档）
            with self.database.transaction():
                query = "DELETE FROM raw_documents WHERE id = ?"
                affected_rows = self.database.execute_update(query, (str(document_id),))

            # 从文件系统删除
            if affected_rows > 0:
                await self.storage.delete_document(document_id, "raw")
                logger.info(f"原始文档删除成功: {document_id}")
                return True
            else:
                logger.warning(f"原始文档不存在: {document_id}")
                return False

        except Exception as e:
            logger.error(f"删除原始文档失败 {document_id}: {e}")
            return False

    async def list_raw_documents(self,
                                source_id: Optional[str] = None,
                                processing_status: Optional[ProcessingStatus] = None,
                                limit: int = 100,
                                offset: int = 0) -> List[RawDocument]:
        """列出原始文档

        Args:
            source_id: 数据源ID过滤
            processing_status: 处理状态过滤
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            原始文档列表
        """
        try:
            # 构建查询条件
            conditions = []
            params = []

            if source_id:
                conditions.append("source_id = ?")
                params.append(source_id)

            if processing_status:
                conditions.append("processing_status = ?")
                params.append(processing_status.value)

            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

            query = f"""
            SELECT id FROM raw_documents {where_clause}
            ORDER BY collected_at DESC
            LIMIT ? OFFSET ?
            """

            params.extend([limit, offset])
            results = self.database.execute_query(query, tuple(params))

            documents = []
            for result in results:
                doc = await self.get_raw_document(result['id'])
                if doc:
                    documents.append(doc)

            return documents

        except Exception as e:
            logger.error(f"列出原始文档失败: {e}")
            return []

    # ==================== 处理后文档操作 ====================

    async def create_processed_document(self, document: ProcessedDocument) -> UUID:
        """创建处理后文档

        Args:
            document: 处理后文档对象

        Returns:
            文档ID
        """
        try:
            # 存储到文件系统
            await self.storage.store_processed_document(document)

            # 存储到数据库
            with self.database.transaction():
                query = """
                INSERT INTO processed_documents (
                    id, raw_document_id, title, summary, content, structured_content,
                    extracted_metadata, entities, keywords, categories, processed_at,
                    processor_version, processing_time_ms, content_hash, similarity_group_id,
                    similarity_score, is_duplicate, quality_score, relevance_score,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """

                params = (
                    str(document.id),
                    str(document.raw_document_id),
                    document.title,
                    document.summary,
                    document.content,
                    json.dumps(document.structured_content),
                    json.dumps(document.extracted_metadata),
                    json.dumps(document.entities),
                    json.dumps(document.keywords),
                    json.dumps(document.categories),
                    document.processed_at,
                    document.processor_version,
                    document.processing_time_ms,
                    document.content_hash,
                    document.similarity_group_id,
                    document.similarity_score,
                    document.is_duplicate,
                    document.quality_score,
                    document.relevance_score,
                    document.created_at,
                    document.updated_at,
                )

                self.database.execute_insert(query, params)

            logger.info(f"处理后文档创建成功: {document.id}")
            return document.id

        except Exception as e:
            logger.error(f"创建处理后文档失败: {e}")
            raise

    async def get_processed_document(self, document_id: Union[str, UUID]) -> Optional[ProcessedDocument]:
        """获取处理后文档

        Args:
            document_id: 文档ID

        Returns:
            处理后文档对象，如果不存在返回None
        """
        try:
            # 从数据库获取基础信息
            query = """
            SELECT * FROM processed_documents WHERE id = ?
            """
            results = self.database.execute_query(query, (str(document_id),))

            if not results:
                return None

            doc_data = results[0]

            # 从文件系统获取完整内容
            file_data = await self.storage.retrieve_processed_document(document_id)
            if not file_data:
                logger.warning(f"文件系统中未找到处理文档: {document_id}")
                return None

            # 构造文档对象
            return ProcessedDocument(
                id=UUID(doc_data['id']),
                raw_document_id=UUID(doc_data['raw_document_id']),
                title=doc_data['title'],
                summary=doc_data['summary'],
                content=doc_data['content'],
                structured_content=json.loads(doc_data['structured_content']),
                extracted_metadata=json.loads(doc_data['extracted_metadata']),
                entities=json.loads(doc_data['entities']),
                keywords=json.loads(doc_data['keywords']),
                categories=json.loads(doc_data['categories']),
                processed_at=datetime.fromisoformat(doc_data['processed_at']),
                processor_version=doc_data['processor_version'],
                processing_time_ms=doc_data['processing_time_ms'],
                content_hash=doc_data['content_hash'],
                similarity_group_id=doc_data['similarity_group_id'],
                similarity_score=doc_data['similarity_score'],
                is_duplicate=doc_data['is_duplicate'],
                quality_score=doc_data['quality_score'],
                relevance_score=doc_data['relevance_score'],
                created_at=datetime.fromisoformat(doc_data['created_at']),
                updated_at=datetime.fromisoformat(doc_data['updated_at']),
            )

        except Exception as e:
            logger.error(f"获取处理后文档失败 {document_id}: {e}")
            return None

    async def update_processed_document(self, document: ProcessedDocument) -> bool:
        """更新处理后文档

        Args:
            document: 更新后的文档对象

        Returns:
            是否更新成功
        """
        try:
            # 更新文件存储
            await self.storage.store_processed_document(document)

            # 更新数据库
            query = """
            UPDATE processed_documents SET
                title = ?, summary = ?, content = ?, structured_content = ?,
                extracted_metadata = ?, entities = ?, keywords = ?, categories = ?,
                content_hash = ?, similarity_group_id = ?, similarity_score = ?,
                is_duplicate = ?, quality_score = ?, relevance_score = ?, updated_at = ?
            WHERE id = ?
            """

            params = (
                document.title,
                document.summary,
                document.content,
                json.dumps(document.structured_content),
                json.dumps(document.extracted_metadata),
                json.dumps(document.entities),
                json.dumps(document.keywords),
                json.dumps(document.categories),
                document.content_hash,
                document.similarity_group_id,
                document.similarity_score,
                document.is_duplicate,
                document.quality_score,
                document.relevance_score,
                datetime.utcnow(),  # 更新时间
                str(document.id),
            )

            affected_rows = self.database.execute_update(query, params)

            if affected_rows > 0:
                logger.info(f"处理后文档更新成功: {document.id}")
                return True
            else:
                logger.warning(f"处理后文档不存在或未更新: {document.id}")
                return False

        except Exception as e:
            logger.error(f"更新处理后文档失败 {document.id}: {e}")
            return False

    async def delete_processed_document(self, document_id: Union[str, UUID]) -> bool:
        """删除处理后文档

        Args:
            document_id: 文档ID

        Returns:
            是否删除成功
        """
        try:
            # 从数据库删除
            query = "DELETE FROM processed_documents WHERE id = ?"
            affected_rows = self.database.execute_update(query, (str(document_id),))

            # 从文件系统删除
            if affected_rows > 0:
                await self.storage.delete_document(document_id, "processed")
                logger.info(f"处理后文档删除成功: {document_id}")
                return True
            else:
                logger.warning(f"处理后文档不存在: {document_id}")
                return False

        except Exception as e:
            logger.error(f"删除处理后文档失败 {document_id}: {e}")
            return False

    async def search_processed_documents(self,
                                       query: str,
                                       limit: int = 100) -> List[ProcessedDocument]:
        """搜索处理后文档

        Args:
            query: 搜索查询
            limit: 结果数量限制

        Returns:
            处理后文档列表
        """
        try:
            # 使用文件存储的搜索功能
            file_results = await self.storage.search_documents(query, "processed", limit)

            documents = []
            for file_data in file_results:
                doc = await self.get_processed_document(file_data['id'])
                if doc:
                    documents.append(doc)

            return documents

        except Exception as e:
            logger.error(f"搜索处理后文档失败: {e}")
            return []

    # ==================== 数据源操作 ====================

    async def create_data_source(self, source: DataSource) -> str:
        """创建数据源

        Args:
            source: 数据源对象

        Returns:
            数据源ID
        """
        try:
            query = """
            INSERT INTO data_sources (
                id, name, description, source_type, url, config,
                enabled, collection_interval, max_items_per_run, retry_count, timeout,
                tags, category, language, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            params = (
                source.id,
                source.name,
                source.description,
                source.source_type.value,
                str(source.url) if source.url else None,
                json.dumps(source.config),
                source.enabled,
                source.collection_interval,
                source.max_items_per_run,
                source.retry_count,
                source.timeout,
                json.dumps(source.tags),
                source.category,
                source.language,
                source.created_at,
                source.updated_at,
            )

            self.database.execute_insert(query, params)

            logger.info(f"数据源创建成功: {source.id}")
            return source.id

        except Exception as e:
            logger.error(f"创建数据源失败: {e}")
            raise

    async def get_data_source(self, source_id: str) -> Optional[DataSource]:
        """获取数据源

        Args:
            source_id: 数据源ID

        Returns:
            数据源对象，如果不存在返回None
        """
        try:
            query = "SELECT * FROM data_sources WHERE id = ?"
            results = self.database.execute_query(query, (source_id,))

            if not results:
                return None

            source_data = results[0]

            return DataSource(
                id=source_data['id'],
                name=source_data['name'],
                description=source_data['description'],
                source_type=SourceType(source_data['source_type']),
                url=source_data['url'],
                config=json.loads(source_data['config']),
                enabled=source_data['enabled'],
                collection_interval=source_data['collection_interval'],
                max_items_per_run=source_data['max_items_per_run'],
                retry_count=source_data['retry_count'],
                timeout=source_data['timeout'],
                tags=json.loads(source_data['tags']),
                category=source_data['category'],
                language=source_data['language'],
                created_at=datetime.fromisoformat(source_data['created_at']),
                updated_at=datetime.fromisoformat(source_data['updated_at']),
                last_collected_at=datetime.fromisoformat(source_data['last_collected_at']) if source_data.get('last_collected_at') else None,
                last_success_at=datetime.fromisoformat(source_data['last_success_at']) if source_data.get('last_success_at') else None,
                collection_count=source_data['collection_count'],
                success_count=source_data['success_count'],
                error_count=source_data['error_count'],
                last_error=source_data['last_error'],
            )

        except Exception as e:
            logger.error(f"获取数据源失败 {source_id}: {e}")
            return None

    async def update_data_source(self, source: DataSource) -> bool:
        """更新数据源

        Args:
            source: 更新后的数据源对象

        Returns:
            是否更新成功
        """
        try:
            query = """
            UPDATE data_sources SET
                name = ?, description = ?, source_type = ?, url = ?, config = ?,
                enabled = ?, collection_interval = ?, max_items_per_run = ?,
                retry_count = ?, timeout = ?, tags = ?, category = ?, language = ?,
                updated_at = ?
            WHERE id = ?
            """

            params = (
                source.name,
                source.description,
                source.source_type.value,
                str(source.url) if source.url else None,
                json.dumps(source.config),
                source.enabled,
                source.collection_interval,
                source.max_items_per_run,
                source.retry_count,
                source.timeout,
                json.dumps(source.tags),
                source.category,
                source.language,
                datetime.utcnow(),  # 更新时间
                source.id,
            )

            affected_rows = self.database.execute_update(query, params)

            if affected_rows > 0:
                logger.info(f"数据源更新成功: {source.id}")
                return True
            else:
                logger.warning(f"数据源不存在或未更新: {source.id}")
                return False

        except Exception as e:
            logger.error(f"更新数据源失败 {source.id}: {e}")
            return False

    async def list_data_sources(self,
                               source_type: Optional[SourceType] = None,
                               enabled: Optional[bool] = None) -> List[DataSource]:
        """列出数据源

        Args:
            source_type: 数据源类型过滤
            enabled: 启用状态过滤

        Returns:
            数据源列表
        """
        try:
            conditions = []
            params = []

            if source_type:
                conditions.append("source_type = ?")
                params.append(source_type.value)

            if enabled is not None:
                conditions.append("enabled = ?")
                params.append(enabled)

            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

            query = f"""
            SELECT id FROM data_sources {where_clause}
            ORDER BY created_at DESC
            """

            results = self.database.execute_query(query, tuple(params))

            sources = []
            for result in results:
                source = await self.get_data_source(result['id'])
                if source:
                    sources.append(source)

            return sources

        except Exception as e:
            logger.error(f"列出数据源失败: {e}")
            return []

    # ==================== 任务操作 ====================

    async def create_task(self, task: CollectionTask) -> UUID:
        """创建采集任务

        Args:
            task: 任务对象

        Returns:
            任务ID
        """
        try:
            query = """
            INSERT INTO collection_tasks (
                id, source_id, task_type, status, priority, created_at,
                started_at, completed_at, worker_id, retry_count, max_retries,
                items_collected, items_processed, items_failed, error_message,
                error_details, config
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            params = (
                str(task.id),
                task.source_id,
                task.task_type,
                task.status.value,
                task.priority,
                task.created_at,
                task.started_at,
                task.completed_at,
                task.worker_id,
                task.retry_count,
                task.max_retries,
                task.items_collected,
                task.items_processed,
                task.items_failed,
                task.error_message,
                json.dumps(task.error_details) if task.error_details else '{}',
                json.dumps(task.config),
            )

            self.database.execute_insert(query, params)

            logger.info(f"采集任务创建成功: {task.id}")
            return task.id

        except Exception as e:
            logger.error(f"创建采集任务失败: {e}")
            raise

    async def get_task(self, task_id: Union[str, UUID]) -> Optional[CollectionTask]:
        """获取采集任务

        Args:
            task_id: 任务ID

        Returns:
            任务对象，如果不存在返回None
        """
        try:
            query = "SELECT * FROM collection_tasks WHERE id = ?"
            results = self.database.execute_query(query, (str(task_id),))

            if not results:
                return None

            task_data = results[0]

            return CollectionTask(
                id=UUID(task_data['id']),
                source_id=task_data['source_id'],
                task_type=task_data['task_type'],
                status=TaskStatus(task_data['status']),
                priority=task_data['priority'],
                created_at=datetime.fromisoformat(task_data['created_at']),
                started_at=datetime.fromisoformat(task_data['started_at']) if task_data['started_at'] else None,
                completed_at=datetime.fromisoformat(task_data['completed_at']) if task_data['completed_at'] else None,
                worker_id=task_data['worker_id'],
                retry_count=task_data['retry_count'],
                max_retries=task_data['max_retries'],
                items_collected=task_data['items_collected'],
                items_processed=task_data['items_processed'],
                items_failed=task_data['items_failed'],
                error_message=task_data['error_message'],
                error_details=json.loads(task_data['error_details']),
                config=json.loads(task_data['config']),
            )

        except Exception as e:
            logger.error(f"获取采集任务失败 {task_id}: {e}")
            return None

    async def update_task(self, task: CollectionTask) -> bool:
        """更新采集任务

        Args:
            task: 更新后的任务对象

        Returns:
            是否更新成功
        """
        try:
            query = """
            UPDATE collection_tasks SET
                status = ?, started_at = ?, completed_at = ?, worker_id = ?,
                retry_count = ?, items_collected = ?, items_processed = ?,
                items_failed = ?, error_message = ?, error_details = ?
            WHERE id = ?
            """

            params = (
                task.status.value,
                task.started_at,
                task.completed_at,
                task.worker_id,
                task.retry_count,
                task.items_collected,
                task.items_processed,
                task.items_failed,
                task.error_message,
                json.dumps(task.error_details) if task.error_details else '{}',
                str(task.id),
            )

            affected_rows = self.database.execute_update(query, params)

            if affected_rows > 0:
                logger.info(f"采集任务更新成功: {task.id}")
                return True
            else:
                logger.warning(f"采集任务不存在或未更新: {task.id}")
                return False

        except Exception as e:
            logger.error(f"更新采集任务失败 {task.id}: {e}")
            return False

    async def list_pending_tasks(self, limit: int = 50) -> List[CollectionTask]:
        """列出待处理任务

        Args:
            limit: 返回数量限制

        Returns:
            待处理任务列表
        """
        try:
            query = """
            SELECT id FROM collection_tasks
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
            """

            results = self.database.execute_query(query, (limit,))

            tasks = []
            for result in results:
                task = await self.get_task(result['id'])
                if task:
                    tasks.append(task)

            return tasks

        except Exception as e:
            logger.error(f"列出待处理任务失败: {e}")
            return []

    # ==================== 统计查询 ====================

    async def get_statistics(self) -> Dict[str, Any]:
        """获取系统统计信息

        Returns:
            统计信息字典
        """
        try:
            stats = {}

            # 文档统计
            stats['raw_documents'] = self.database.get_table_count('raw_documents')
            stats['processed_documents'] = self.database.get_table_count('processed_documents')

            # 数据源统计
            stats['total_sources'] = self.database.get_table_count('data_sources')

            query = "SELECT COUNT(*) as count FROM data_sources WHERE enabled = 1"
            result = self.database.execute_query(query)
            stats['active_sources'] = result[0]['count'] if result else 0

            # 任务统计
            stats['total_tasks'] = self.database.get_table_count('collection_tasks')

            for status in ['pending', 'running', 'completed']:
                query = f"SELECT COUNT(*) as count FROM collection_tasks WHERE status = '{status}'"
                result = self.database.execute_query(query)
                stats[f'{status}_tasks'] = result[0]['count'] if result else 0

            # 最近24小时统计
            yesterday = datetime.utcnow() - timedelta(hours=24)

            query = """
            SELECT COUNT(*) as count FROM raw_documents
            WHERE collected_at >= ?
            """
            result = self.database.execute_query(query, (yesterday,))
            stats['raw_documents_last_24h'] = result[0]['count'] if result else 0

            query = """
            SELECT COUNT(*) as count FROM processed_documents
            WHERE processed_at >= ?
            """
            result = self.database.execute_query(query, (yesterday,))
            stats['processed_documents_last_24h'] = result[0]['count'] if result else 0

            # 存储统计
            storage_stats = await self.storage.get_storage_stats()
            stats['storage'] = storage_stats

            return stats

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}

    async def cleanup_old_data(self, days: int = 30) -> Dict[str, int]:
        """清理旧数据

        Args:
            days: 保留天数

        Returns:
            清理结果统计
        """
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            cleanup_stats = {}

            # 清理旧任务记录
            query = "DELETE FROM collection_tasks WHERE completed_at < ? AND status = 'completed'"
            affected_rows = self.database.execute_update(query, (cutoff_date,))
            cleanup_stats['old_tasks_deleted'] = affected_rows

            # 清理旧系统指标
            query = "DELETE FROM system_metrics WHERE timestamp < ?"
            affected_rows = self.database.execute_update(query, (cutoff_date,))
            cleanup_stats['old_metrics_deleted'] = affected_rows

            # 清理临时文件
            temp_files_deleted = await self.storage.cleanup_temp_files(days * 24)
            cleanup_stats['temp_files_deleted'] = temp_files_deleted

            # 数据库优化
            self.database.vacuum()
            self.database.analyze()
            cleanup_stats['database_optimized'] = True

            logger.info(f"数据清理完成: {cleanup_stats}")
            return cleanup_stats

        except Exception as e:
            logger.error(f"清理旧数据失败: {e}")
            return {}


# 全局数据仓库实例
_repository: Optional[DataRepository] = None


def get_repository(database: Optional[AtlasDatabase] = None,
                  storage: Optional[FileStorageManager] = None) -> DataRepository:
    """获取全局数据仓库实例

    Args:
        database: 数据库实例，如果为None则使用全局实例
        storage: 存储管理器实例，如果为None则使用全局实例

    Returns:
        数据仓库实例
    """
    global _repository
    if _repository is None:
        # 确保使用相同的数据库实例
        db = database or get_database()
        stor = storage or get_storage_manager()
        _repository = DataRepository(db, stor)
    return _repository