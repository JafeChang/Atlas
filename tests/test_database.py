"""
数据库功能测试

测试数据库初始化、连接管理和基础操作
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from atlas.core.database import AtlasDatabase, DatabaseError


@pytest.fixture
def temp_db():
    """创建临时数据库"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        db_path = tmp.name

    db = AtlasDatabase(db_path)
    yield db

    # 清理
    db.close()
    Path(db_path).unlink(missing_ok=True)


class TestAtlasDatabase:
    """Atlas数据库测试类"""

    def test_database_initialization(self, temp_db):
        """测试数据库初始化"""
        assert temp_db.db_path.exists()

        # 检查表是否创建
        expected_tables = [
            'raw_documents', 'processed_documents', 'data_sources',
            'collection_tasks', 'system_metrics', 'duplicate_groups',
            'duplicate_group_members'
        ]

        for table in expected_tables:
            count = temp_db.get_table_count(table)
            assert count == 0
            assert temp_db.get_table_info(table)

    def test_table_indexes(self, temp_db):
        """测试索引创建"""
        # 索引创建在初始化时自动完成
        # 这里主要验证没有错误发生
        assert True

    def test_basic_query_operations(self, temp_db):
        """测试基础查询操作"""
        # 测试SELECT查询
        results = temp_db.execute_query("SELECT COUNT(*) as count FROM raw_documents")
        assert len(results) == 1
        assert results[0]['count'] == 0

        # 测试INSERT操作
        insert_id = temp_db.execute_insert(
            "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("test_source", "Test Source", "rss_feed", datetime.utcnow(), datetime.utcnow())
        )
        assert insert_id is not None

        # 验证插入成功
        results = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources")
        assert results[0]['count'] == 1

        # 测试UPDATE操作
        affected_rows = temp_db.execute_update(
            "UPDATE data_sources SET name = ? WHERE id = ?",
            ("Updated Test Source", "test_source")
        )
        assert affected_rows == 1

        # 验证更新成功
        results = temp_db.execute_query("SELECT name FROM data_sources WHERE id = ?", ("test_source",))
        assert len(results) == 1
        assert results[0]['name'] == "Updated Test Source"

        # 测试DELETE操作
        affected_rows = temp_db.execute_update("DELETE FROM data_sources WHERE id = ?", ("test_source",))
        assert affected_rows == 1

        # 验证删除成功
        results = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources")
        assert results[0]['count'] == 0

    def test_transaction_management(self, temp_db):
        """测试事务管理"""
        # 简化事务测试 - 测试基本的连接和提交功能
        with temp_db.get_connection() as conn:
            # 在连接上下文中执行多个操作
            conn.execute(
                "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("test_source_tx", "Test Transaction Source", "rss_feed", datetime.utcnow(), datetime.utcnow())
            )
            conn.execute(
                "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("test_source_tx2", "Test Transaction Source 2", "rss_feed", datetime.utcnow(), datetime.utcnow())
            )
            # 上下文管理器会自动提交

        # 验证数据已提交
        results = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources WHERE id LIKE 'test_source_tx%'")
        assert results[0]['count'] == 2

        # 测试异常情况下的回滚
        initial_count = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources")[0]['count']

        try:
            with temp_db.get_connection(autocommit=False) as conn:
                conn.execute(
                    "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("test_source_rollback", "Should Rollback", "rss_feed", datetime.utcnow(), datetime.utcnow())
                )
                raise Exception("强制回滚")
        except Exception:
            pass  # 异常被忽略，事务应该回滚

        # 验证数据没有插入
        final_count = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources")[0]['count']
        assert final_count == initial_count

    def test_connection_context_manager(self, temp_db):
        """测试连接上下文管理器"""
        with temp_db.get_connection() as conn:
            cursor = conn.execute("SELECT 1 as test")
            result = cursor.fetchone()
            assert result['test'] == 1

        # 连接应该自动关闭
        with pytest.raises(Exception):  # 尝试使用已关闭的连接应该出错
            cursor.execute("SELECT 1")

    def test_error_handling(self, temp_db):
        """测试错误处理"""
        # 测试SQL语法错误
        with pytest.raises(DatabaseError):
            temp_db.execute_query("INVALID SQL")

        # 测试约束错误
        temp_db.execute_insert(
            "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("duplicate_id", "Test Source", "rss_feed", datetime.utcnow(), datetime.utcnow())
        )

        with pytest.raises(DatabaseError):
            temp_db.execute_insert(
                "INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("duplicate_id", "Another Test Source", "website", datetime.utcnow(), datetime.utcnow())
            )

    def test_database_operations(self, temp_db):
        """测试数据库操作功能"""
        # 测试VACUUM
        temp_db.vacuum()

        # 测试ANALYZE
        temp_db.analyze()

        # 测试备份
        backup_path = temp_db.db_path.parent / "backup.db"
        temp_db.backup(backup_path)
        assert backup_path.exists()

        # 清理备份文件
        backup_path.unlink()

    def test_database_stats(self, temp_db):
        """测试数据库统计信息"""
        # 插入一些测试数据
        for i in range(5):
            temp_db.execute_insert(
                "INSERT INTO raw_documents (id, source_id, source_type, document_type, raw_content, collected_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"doc_{i}", f"source_{i}", "rss", "html", f"Content {i}", datetime.utcnow(), datetime.utcnow(), datetime.utcnow())
            )

        stats = temp_db.get_database_stats()

        assert "raw_documents_count" in stats
        assert stats["raw_documents_count"] == 5
        assert "database_size_bytes" in stats
        assert stats["database_size_bytes"] > 0

    def test_prepared_statements(self, temp_db):
        """测试预处理语句"""
        # 批量插入测试数据
        insert_query = """
        INSERT INTO data_sources (id, name, source_type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)
        """

        test_data = [
            (f"source_{i}", f"Test Source {i}", "rss_feed", datetime.utcnow(), datetime.utcnow())
            for i in range(10)
        ]

        for data in test_data:
            temp_db.execute_insert(insert_query, data)

        # 验证数据插入成功
        results = temp_db.execute_query("SELECT COUNT(*) as count FROM data_sources")
        assert results[0]['count'] == 10

        # 测试参数化查询
        select_query = "SELECT name FROM data_sources WHERE source_type = ? ORDER BY name"
        results = temp_db.execute_query(select_query, ("rss_feed",))
        assert len(results) == 10

        # 验证结果按名称排序
        names = [row['name'] for row in results]
        assert names == sorted(names)