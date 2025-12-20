"""
数据库和存储同步集成测试

测试数据库和存储组件的集成，使用同步方式。
"""

import pytest
import tempfile
import asyncio
from pathlib import Path

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import FileStorageManager
from atlas.core.logging import get_logger
from atlas.models.documents import RawDocument, ProcessedDocument, DocumentType, SourceType
from uuid import uuid4

logger = get_logger(__name__)


class TestDatabaseStorageSync:
    """数据库和存储同步集成测试类"""

    @pytest.fixture
    def test_environment(self):
        """创建测试环境"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            data_dir = Path(temp_dir) / "data"
            log_dir = Path(temp_dir) / "logs"

            for directory in [config_dir, data_dir, log_dir]:
                directory.mkdir(parents=True, exist_ok=True)

            config = get_config(config_dir=config_dir)
            config._config_data["data_dir"] = str(data_dir)
            config._config_data["log_dir"] = str(log_dir)

            yield {
                "config": config,
                "config_dir": config_dir,
                "data_dir": data_dir,
                "log_dir": log_dir
            }

    @pytest.fixture
    def database_manager(self, test_environment):
        """初始化数据库管理器"""
        data_dir = test_environment["data_dir"]
        db_manager = AtlasDatabase(data_dir / "test.db")
        yield db_manager
        db_manager.close()

    @pytest.fixture
    def storage_manager(self, test_environment):
        """初始化存储管理器"""
        data_dir = test_environment["data_dir"]
        storage_manager = FileStorageManager(data_dir)
        yield storage_manager

    def test_database_operations(self, database_manager):
        """测试数据库操作"""
        # 测试基本查询
        result = database_manager.execute_query("SELECT 1 as test")
        assert len(result) == 1
        assert result[0]["test"] == 1

        # 测试表存在
        tables = ["raw_documents", "processed_documents", "data_sources"]
        for table in tables:
            table_count = database_manager.get_table_count(table)
            assert table_count >= 0

        # 测试数据库统计
        stats = database_manager.get_database_stats()
        assert "database_size_mb" in stats

        print("✓ 数据库操作测试通过")

    def test_storage_operations(self, storage_manager):
        """测试存储操作"""
        # 测试目录创建
        test_content = {
            "title": "Test Document",
            "content": "This is test content",
            "source": "test_source",
            "url": "https://example.com/test",
            "metadata": {"test": True}
        }

        # 由于 FileStorageManager 的方法是异步的，我们需要在同步测试中运行它们
        async def test_async_operations():
            # 创建原始文档
            test_doc = RawDocument(
                id=uuid4(),
                title=test_content["title"],
                content=test_content["content"],
                source_id="test_source_001",
                source_url=test_content["url"],
                source_type=SourceType.WEBSITE,
                document_type=DocumentType.HTML,
                raw_content=test_content["content"],
                raw_metadata=test_content["metadata"]
            )

            # 测试文档存储
            file_path = await storage_manager.store_raw_document(test_doc)
            assert file_path.exists()

            # 测试文档检索
            retrieved_doc = await storage_manager.retrieve_raw_document(test_doc.id)
            assert retrieved_doc is not None
            assert retrieved_doc["title"] == test_content["title"]
            assert retrieved_doc["raw_content"] == test_content["content"]

            # 清理测试文件
            if file_path.exists():
                file_path.unlink()

        # 运行异步操作
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(test_async_operations())
        finally:
            loop.close()

        print("✓ 存储操作测试通过")

    def test_database_and_storage_integration(self, database_manager, storage_manager):
        """测试数据库和存储集成"""
        async def integration_test():
            # 1. 创建测试文档
            test_doc = RawDocument(
                id=uuid4(),
                title="Integration Test Document",
                content="This is a test document for integration testing",
                source_id="integration_test_source",
                source_url="https://example.com/integration-test",
                source_type=SourceType.WEBSITE,
                document_type=DocumentType.HTML,
                raw_content="This is a test document for integration testing",
                raw_metadata={"test": True, "category": "test"}
            )

            # 2. 保存文档到存储
            file_path = await storage_manager.store_raw_document(test_doc)
            assert file_path.exists()

            # 3. 在数据库中记录文档信息
            database_manager.execute_query(
                """INSERT INTO raw_documents (id, title, source_id, source_url, source_type, document_type, raw_content, collected_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))""",
                (str(test_doc.id), test_doc.title, test_doc.source_id, str(test_doc.source_url),
                 test_doc.source_type.value, test_doc.document_type.value, test_doc.raw_content)
            )

            # 4. 验证数据库记录
            documents = database_manager.execute_query(
                "SELECT * FROM raw_documents WHERE id = ?",
                (str(test_doc.id),)
            )
            assert len(documents) == 1
            assert documents[0]["title"] == test_doc.title

            # 5. 验证文件存储和检索
            retrieved_doc = await storage_manager.retrieve_raw_document(test_doc.id)
            assert retrieved_doc is not None
            assert retrieved_doc["title"] == test_doc.title
            assert retrieved_doc["raw_content"] == test_doc.raw_content

            # 6. 清理：从数据库删除记录
            database_manager.execute_query(
                "DELETE FROM raw_documents WHERE id = ?",
                (str(test_doc.id),)
            )

            # 7. 清理：删除文件
            if file_path.exists():
                file_path.unlink()

        # 运行集成测试
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(integration_test())
        finally:
            loop.close()

        print("✓ 数据库和存储集成测试通过")


if __name__ == "__main__":
    pytest.main([__file__])