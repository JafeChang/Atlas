"""
数据访问层操作测试

测试CRUD操作和数据仓库功能
"""

import pytest
import tempfile
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from atlas.core.database import AtlasDatabase
from atlas.core.storage import FileStorageManager
from atlas.core.operations import DataRepository, get_repository
from atlas.models.documents import (
    DocumentType, ProcessingStatus, SourceType, TaskStatus,
    RawDocument, ProcessedDocument, DataSource, CollectionTask
)


@pytest.fixture
async def temp_repository():
    """创建临时数据仓库"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 创建临时数据库和存储
        db_path = Path(tmp_dir) / "test.db"
        storage_dir = Path(tmp_dir) / "storage"

        database = AtlasDatabase(db_path)
        storage = FileStorageManager(storage_dir)

        repository = DataRepository(database, storage)
        yield repository

        # 清理
        database.close()


@pytest.fixture
def sample_raw_document():
    """创建示例原始文档"""
    return RawDocument(
        source_id="test_source",
        source_type=SourceType.RSS_FEED,
        document_type=DocumentType.HTML,
        raw_content="<html><body><h1>Test Article</h1><p>Test content</p></body></html>",
        raw_metadata={
            "author": "Test Author",
            "published_date": "2025-01-17",
            "tags": ["test", "article"]
        },
        title="Test Article",
        author="Test Author",
        language="en",
        published_at=datetime.utcnow()
    )


@pytest.fixture
def sample_processed_document():
    """创建示例处理后文档"""
    return ProcessedDocument(
        raw_document_id=uuid4(),
        title="Processed Test Article",
        summary="A test article for processing",
        content="This is the processed content of the test article.",
        structured_content={
            "headings": ["Test Article"],
            "paragraphs": ["Test content"],
            "links": []
        },
        extracted_metadata={
            "word_count": 15,
            "readability_score": 0.8
        },
        entities=[
            {"text": "Test", "type": "MISC"},
            {"text": "Article", "type": "MISC"}
        ],
        keywords=["test", "article", "processing"],
        categories=["test", "demo"],
        processing_time_ms=150,
        quality_score=0.9,
        relevance_score=0.85
    )


@pytest.fixture
def sample_data_source():
    """创建示例数据源"""
    return DataSource(
        id="test_rss_source",
        name="Test RSS Source",
        description="A test RSS source for testing",
        source_type=SourceType.RSS_FEED,
        url="https://example.com/feed.xml",
        config={
            "update_interval": 3600,
            "max_items": 100,
            "timeout": 30
        },
        enabled=True,
        collection_interval=3600,
        max_items_per_run=50,
        tags=["test", "rss"],
        category="test",
        language="en"
    )


@pytest.fixture
def sample_collection_task():
    """创建示例采集任务"""
    return CollectionTask(
        source_id="test_source",
        priority=1,
        config={
            "max_items": 10,
            "timeout": 30
        }
    )


class TestRawDocumentOperations:
    """原始文档操作测试"""

    @pytest.mark.asyncio
    async def test_create_raw_document(self, temp_repository, sample_raw_document):
        """测试创建原始文档"""
        doc_id = await temp_repository.create_raw_document(sample_raw_document)

        assert doc_id == sample_raw_document.id

        # 验证文档可以检索
        retrieved_doc = await temp_repository.get_raw_document(doc_id)
        assert retrieved_doc is not None
        assert retrieved_doc.source_id == sample_raw_document.source_id
        assert retrieved_doc.raw_content == sample_raw_document.raw_content
        assert retrieved_doc.title == sample_raw_document.title

    @pytest.mark.asyncio
    async def test_update_raw_document(self, temp_repository, sample_raw_document):
        """测试更新原始文档"""
        # 创建文档
        doc_id = await temp_repository.create_raw_document(sample_raw_document)

        # 更新文档
        sample_raw_document.title = "Updated Title"
        sample_raw_document.processing_status = ProcessingStatus.COMPLETED
        sample_raw_document.processing_attempts = 1

        success = await temp_repository.update_raw_document(sample_raw_document)
        assert success

        # 验证更新
        updated_doc = await temp_repository.get_raw_document(doc_id)
        assert updated_doc.title == "Updated Title"
        assert updated_doc.processing_status == ProcessingStatus.COMPLETED
        assert updated_doc.processing_attempts == 1

    @pytest.mark.asyncio
    async def test_delete_raw_document(self, temp_repository, sample_raw_document):
        """测试删除原始文档"""
        # 创建文档
        doc_id = await temp_repository.create_raw_document(sample_raw_document)

        # 验证文档存在
        doc = await temp_repository.get_raw_document(doc_id)
        assert doc is not None

        # 删除文档
        success = await temp_repository.delete_raw_document(doc_id)
        assert success

        # 验证文档已删除
        deleted_doc = await temp_repository.get_raw_document(doc_id)
        assert deleted_doc is None

    @pytest.mark.asyncio
    async def test_list_raw_documents(self, temp_repository):
        """测试列出原始文档"""
        # 创建多个文档
        sources = ["source1", "source2", "source3"]
        documents = []

        for source_id in sources:
            doc = RawDocument(
                source_id=source_id,
                source_type=SourceType.RSS_FEED,
                document_type=DocumentType.HTML,
                raw_content=f"<content>from {source_id}</content>",
                title=f"Article from {source_id}",
                processing_status=ProcessingStatus.PENDING
            )
            await temp_repository.create_raw_document(doc)
            documents.append(doc)

        # 列出所有文档
        all_docs = await temp_repository.list_raw_documents()
        assert len(all_docs) == 3

        # 按数据源过滤
        source1_docs = await temp_repository.list_raw_documents(source_id="source1")
        assert len(source1_docs) == 1
        assert source1_docs[0].source_id == "source1"

        # 按状态过滤
        pending_docs = await temp_repository.list_raw_documents(
            processing_status=ProcessingStatus.PENDING
        )
        assert len(pending_docs) == 3

    @pytest.mark.asyncio
    async def test_get_nonexistent_raw_document(self, temp_repository):
        """测试获取不存在的原始文档"""
        doc = await temp_repository.get_raw_document(uuid4())
        assert doc is None


class TestProcessedDocumentOperations:
    """处理后文档操作测试"""

    @pytest.mark.asyncio
    async def test_create_processed_document(self, temp_repository, sample_processed_document):
        """测试创建处理后文档"""
        doc_id = await temp_repository.create_processed_document(sample_processed_document)

        assert doc_id == sample_processed_document.id

        # 验证文档可以检索
        retrieved_doc = await temp_repository.get_processed_document(doc_id)
        assert retrieved_doc is not None
        assert retrieved_doc.title == sample_processed_document.title
        assert retrieved_doc.content == sample_processed_document.content
        assert len(retrieved_doc.keywords) == len(sample_processed_document.keywords)

    @pytest.mark.asyncio
    async def test_update_processed_document(self, temp_repository, sample_processed_document):
        """测试更新处理后文档"""
        # 创建文档
        doc_id = await temp_repository.create_processed_document(sample_processed_document)

        # 更新文档
        sample_processed_document.quality_score = 0.95
        sample_processed_document.is_duplicate = True
        sample_processed_document.similarity_group_id = "group_1"

        success = await temp_repository.update_processed_document(sample_processed_document)
        assert success

        # 验证更新
        updated_doc = await temp_repository.get_processed_document(doc_id)
        assert updated_doc.quality_score == 0.95
        assert updated_doc.is_duplicate is True
        assert updated_doc.similarity_group_id == "group_1"

    @pytest.mark.asyncio
    async def test_search_processed_documents(self, temp_repository):
        """测试搜索处理后文档"""
        # 创建多个文档
        documents = [
            ProcessedDocument(
                raw_document_id=uuid4(),
                title="Machine Learning Article",
                content="Content about ML and AI",
                keywords=["machine learning", "ai"]
            ),
            ProcessedDocument(
                raw_document_id=uuid4(),
                title="Web Development Tutorial",
                content="Learn HTML, CSS, and JavaScript",
                keywords=["web development", "tutorial"]
            ),
            ProcessedDocument(
                raw_document_id=uuid4(),
                title="AI and ML Trends",
                content="Latest trends in artificial intelligence",
                keywords=["ai", "ml", "trends"]
            )
        ]

        for doc in documents:
            await temp_repository.create_processed_document(doc)

        # 搜索AI相关文档
        ai_docs = await temp_repository.search_processed_documents("AI")
        assert len(ai_docs) >= 2  # 至少有2个AI相关文档

        # 搜索Web相关文档
        web_docs = await temp_repository.search_processed_documents("web development")
        assert len(web_docs) >= 1


class TestDataSourceOperations:
    """数据源操作测试"""

    @pytest.mark.asyncio
    async def test_create_data_source(self, temp_repository, sample_data_source):
        """测试创建数据源"""
        source_id = await temp_repository.create_data_source(sample_data_source)

        assert source_id == sample_data_source.id

        # 验证数据源可以检索
        retrieved_source = await temp_repository.get_data_source(source_id)
        assert retrieved_source is not None
        assert retrieved_source.name == sample_data_source.name
        assert retrieved_source.source_type == sample_data_source.source_type
        assert retrieved_source.url == sample_data_source.url
        assert retrieved_source.tags == sample_data_source.tags

    @pytest.mark.asyncio
    async def test_update_data_source(self, temp_repository, sample_data_source):
        """测试更新数据源"""
        # 创建数据源
        source_id = await temp_repository.create_data_source(sample_data_source)

        # 更新数据源
        sample_data_source.name = "Updated RSS Source"
        sample_data_source.collection_interval = 7200
        sample_data_source.tags.append("updated")

        success = await temp_repository.update_data_source(sample_data_source)
        assert success

        # 验证更新
        updated_source = await temp_repository.get_data_source(source_id)
        assert updated_source.name == "Updated RSS Source"
        assert updated_source.collection_interval == 7200
        assert "updated" in updated_source.tags

    @pytest.mark.asyncio
    async def test_list_data_sources(self, temp_repository):
        """测试列出数据源"""
        # 创建多个数据源
        sources = [
            DataSource(
                id="rss_1",
                name="RSS Source 1",
                source_type=SourceType.RSS_FEED,
                url="https://example1.com/feed.xml",
                enabled=True,
                category="technology"
            ),
            DataSource(
                id="rss_2",
                name="RSS Source 2",
                source_type=SourceType.RSS_FEED,
                url="https://example2.com/feed.xml",
                enabled=False,
                category="news"
            ),
            DataSource(
                id="web_1",
                name="Web Source 1",
                source_type=SourceType.WEBSITE,
                url="https://example3.com",
                enabled=True,
                category="technology"
            )
        ]

        for source in sources:
            await temp_repository.create_data_source(source)

        # 列出所有数据源
        all_sources = await temp_repository.list_data_sources()
        assert len(all_sources) == 3

        # 按类型过滤
        rss_sources = await temp_repository.list_data_sources(source_type=SourceType.RSS_FEED)
        assert len(rss_sources) == 2

        # 按启用状态过滤
        enabled_sources = await temp_repository.list_data_sources(enabled=True)
        assert len(enabled_sources) == 2

        # 按分类过滤
        tech_sources = await temp_repository.list_data_sources(category="technology")
        assert len(tech_sources) == 2

    @pytest.mark.asyncio
    async def test_get_nonexistent_data_source(self, temp_repository):
        """测试获取不存在的数据源"""
        source = await temp_repository.get_data_source("nonexistent")
        assert source is None


class TestCollectionTaskOperations:
    """采集任务操作测试"""

    @pytest.mark.asyncio
    async def test_create_collection_task(self, temp_repository, sample_collection_task):
        """测试创建采集任务"""
        task_id = await temp_repository.create_task(sample_collection_task)

        assert task_id == sample_collection_task.id

        # 验证任务可以检索
        retrieved_task = await temp_repository.get_task(task_id)
        assert retrieved_task is not None
        assert retrieved_task.source_id == sample_collection_task.source_id
        assert retrieved_task.status == TaskStatus.PENDING
        assert retrieved_task.priority == sample_collection_task.priority

    @pytest.mark.asyncio
    async def test_update_collection_task(self, temp_repository, sample_collection_task):
        """测试更新采集任务"""
        # 创建任务
        task_id = await temp_repository.create_task(sample_collection_task)

        # 更新任务状态
        sample_collection_task.status = TaskStatus.RUNNING
        sample_collection_task.started_at = datetime.utcnow()
        sample_collection_task.worker_id = "worker_1"

        success = await temp_repository.update_task(sample_collection_task)
        assert success

        # 验证更新
        updated_task = await temp_repository.get_task(task_id)
        assert updated_task.status == TaskStatus.RUNNING
        assert updated_task.worker_id == "worker_1"
        assert updated_task.started_at is not None

    @pytest.mark.asyncio
    async def test_list_pending_tasks(self, temp_repository):
        """测试列出待处理任务"""
        # 创建多个任务
        tasks = [
            CollectionTask(source_id="source1", priority=3),
            CollectionTask(source_id="source2", priority=1),
            CollectionTask(source_id="source3", priority=2),
        ]

        for task in tasks:
            await temp_repository.create_task(task)

        # 获取待处理任务
        pending_tasks = await temp_repository.list_pending_tasks()
        assert len(pending_tasks) == 3

        # 验证按优先级排序
        assert pending_tasks[0].source_id == "source2"  # priority 1
        assert pending_tasks[1].source_id == "source3"  # priority 2
        assert pending_tasks[2].source_id == "source1"  # priority 3

    @pytest.mark.asyncio
    async def test_task_lifecycle(self, temp_repository, sample_collection_task):
        """测试任务完整生命周期"""
        # 创建任务
        task_id = await temp_repository.create_task(sample_collection_task)

        # 开始执行
        sample_collection_task.status = TaskStatus.RUNNING
        sample_collection_task.started_at = datetime.utcnow()
        sample_collection_task.worker_id = "worker_1"
        await temp_repository.update_task(sample_collection_task)

        # 模拟执行过程
        sample_collection_task.items_collected = 10
        sample_collection_task.items_processed = 8
        sample_collection_task.items_failed = 2

        # 完成任务
        sample_collection_task.status = TaskStatus.COMPLETED
        sample_collection_task.completed_at = datetime.utcnow()
        success = await temp_repository.update_task(sample_collection_task)

        assert success

        # 验证最终状态
        final_task = await temp_repository.get_task(task_id)
        assert final_task.status == TaskStatus.COMPLETED
        assert final_task.items_collected == 10
        assert final_task.items_processed == 8
        assert final_task.items_failed == 2


class TestRepositoryStatistics:
    """数据仓库统计测试"""

    @pytest.mark.asyncio
    async def test_get_statistics(self, temp_repository):
        """测试获取统计信息"""
        # 创建测试数据
        # 1. 创建数据源
        source = DataSource(
            id="stats_test_source",
            name="Stats Test Source",
            source_type=SourceType.RSS_FEED,
            url="https://example.com/feed.xml",
            enabled=True
        )
        await temp_repository.create_data_source(source)

        # 2. 创建原始文档
        raw_doc = RawDocument(
            source_id="stats_test_source",
            source_type=SourceType.RSS_FEED,
            document_type=DocumentType.HTML,
            raw_content="<test>content</test>",
            title="Test Article"
        )
        await temp_repository.create_raw_document(raw_doc)

        # 3. 创建处理后文档
        processed_doc = ProcessedDocument(
            raw_document_id=raw_doc.id,
            title="Processed Test Article",
            content="Processed content"
        )
        await temp_repository.create_processed_document(processed_doc)

        # 4. 创建任务
        task = CollectionTask(source_id="stats_test_source", priority=1)
        await temp_repository.create_task(task)

        # 获取统计信息
        stats = await temp_repository.get_statistics()

        assert stats['raw_documents'] >= 1
        assert stats['processed_documents'] >= 1
        assert stats['total_sources'] >= 1
        assert stats['active_sources'] >= 1
        assert stats['total_tasks'] >= 1
        assert stats['pending_tasks'] >= 1

    @pytest.mark.asyncio
    async def test_cleanup_old_data(self, temp_repository):
        """测试清理旧数据"""
        # 创建一些旧任务
        old_date = datetime.utcnow() - timedelta(days=40)

        old_task = CollectionTask(
            source_id="test_source",
            status=TaskStatus.COMPLETED,
            completed_at=old_date
        )
        await temp_repository.create_task(old_task)

        # 清理30天前的数据
        cleanup_stats = await temp_repository.cleanup_old_data(days=30)

        assert 'old_tasks_deleted' in cleanup_stats
        assert cleanup_stats['database_optimized'] is True


class TestRepositoryIntegration:
    """数据仓库集成测试"""

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self, temp_repository):
        """测试端到端工作流程"""
        # 1. 创建数据源
        source = DataSource(
            id="integration_test_source",
            name="Integration Test Source",
            source_type=SourceType.RSS_FEED,
            url="https://example.com/feed.xml",
            enabled=True,
            tags=["test", "integration"]
        )
        source_id = await temp_repository.create_data_source(source)

        # 2. 创建采集任务
        task = CollectionTask(
            source_id=source_id,
            priority=1,
            config={"max_items": 5}
        )
        task_id = await temp_repository.create_task(task)

        # 3. 模拟采集原始文档
        raw_doc = RawDocument(
            source_id=source_id,
            source_type=SourceType.RSS_FEED,
            document_type=DocumentType.RSS,
            raw_content="<item><title>Test Item</title></item>",
            title="Test Item",
            processing_status=ProcessingStatus.COMPLETED
        )
        raw_doc_id = await temp_repository.create_raw_document(raw_doc)

        # 4. 处理文档
        processed_doc = ProcessedDocument(
            raw_document_id=raw_doc_id,
            title="Processed Test Item",
            summary="A processed test item",
            content="Processed content",
            keywords=["test", "item"],
            quality_score=0.9
        )
        processed_doc_id = await temp_repository.create_processed_document(processed_doc)

        # 5. 更新任务状态
        task.status = TaskStatus.COMPLETED
        task.completed_at = datetime.utcnow()
        task.items_collected = 1
        task.items_processed = 1
        await temp_repository.update_task(task)

        # 6. 验证整个流程
        # 检查数据源
        retrieved_source = await temp_repository.get_data_source(source_id)
        assert retrieved_source is not None
        assert retrieved_source.name == "Integration Test Source"

        # 检查原始文档
        retrieved_raw = await temp_repository.get_raw_document(raw_doc_id)
        assert retrieved_raw is not None
        assert retrieved_raw.title == "Test Item"

        # 检查处理后文档
        retrieved_processed = await temp_repository.get_processed_document(processed_doc_id)
        assert retrieved_processed is not None
        assert retrieved_processed.quality_score == 0.9

        # 检查任务
        retrieved_task = await temp_repository.get_task(task_id)
        assert retrieved_task is not None
        assert retrieved_task.status == TaskStatus.COMPLETED
        assert retrieved_task.items_collected == 1

    @pytest.mark.asyncio
    async def test_duplicate_document_handling(self, temp_repository):
        """测试重复文档处理"""
        # 创建第一个文档
        raw_doc1 = RawDocument(
            source_id="test_source",
            source_type=SourceType.RSS_FEED,
            document_type=DocumentType.HTML,
            raw_content="<p>Same content</p>",
            title="First Document"
        )
        doc1_id = await temp_repository.create_raw_document(raw_doc1)

        processed_doc1 = ProcessedDocument(
            raw_document_id=doc1_id,
            title="First Document",
            content="Same content",
            is_duplicate=False,
            content_hash=raw_doc1.content_hash
        )
        await temp_repository.create_processed_document(processed_doc1)

        # 创建重复文档
        raw_doc2 = RawDocument(
            source_id="test_source_2",
            source_type=SourceType.RSS_FEED,
            document_type=DocumentType.HTML,
            raw_content="<p>Same content</p>",  # 相同内容
            title="Second Document"
        )
        doc2_id = await temp_repository.create_raw_document(raw_doc2)

        processed_doc2 = ProcessedDocument(
            raw_document_id=doc2_id,
            title="Second Document",
            content="Same content",
            is_duplicate=True,
            similarity_group_id="group_1",
            similarity_score=0.95,
            content_hash=raw_doc2.content_hash
        )
        await temp_repository.create_processed_document(processed_doc2)

        # 验证重复检测
        all_docs = await temp_repository.list_processed_documents()
        duplicate_docs = [doc for doc in all_docs if doc.is_duplicate]

        assert len(duplicate_docs) >= 1
        assert any(doc.similarity_group_id == "group_1" for doc in duplicate_docs)


class TestGlobalRepository:
    """全局数据仓库测试"""

    def test_get_repository_singleton(self):
        """测试全局数据仓库单例"""
        repo1 = get_repository()
        repo2 = get_repository()

        # 应该返回相同的实例
        assert repo1 is repo2

    @pytest.mark.asyncio
    async def test_global_repository_functionality(self):
        """测试全局数据仓库功能"""
        repo = get_repository()

        # 创建一个简单的数据源进行测试
        source = DataSource(
            id="global_test_source",
            name="Global Test Source",
            source_type=SourceType.RSS_FEED,
            url="https://example.com/feed.xml"
        )

        try:
            source_id = await repo.create_data_source(source)

            # 验证可以检索
            retrieved = await repo.get_data_source(source_id)
            assert retrieved is not None
            assert retrieved.id == source_id

        finally:
            # 清理测试数据
            if hasattr(repo, 'database') and repo.database:
                repo.database.execute_update("DELETE FROM data_sources WHERE id = ?", (source_id,))