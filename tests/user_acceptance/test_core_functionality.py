"""
核心功能用户验收测试

验证Atlas系统的核心功能是否正常工作。
"""

import pytest
import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import FileStorageManager
from atlas.collectors.rss_collector import RSSCollector
from atlas.processors.parser import HTMLParser
from atlas.processors.normalizer import TextNormalizer
from atlas.models.documents import RawDocument, DocumentType, SourceType


class TestCoreFunctionality:
    """核心功能验收测试"""

    @pytest.fixture
    def test_environment(self):
        """创建验收测试环境"""
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
    def components(self, test_environment):
        """初始化核心组件"""
        config = test_environment["config"]
        data_dir = test_environment["data_dir"]

        # 数据库管理器
        db_manager = AtlasDatabase(data_dir / "test.db")

        # 存储管理器
        storage_manager = FileStorageManager(data_dir)

        # RSS采集器
        rss_collector = RSSCollector(
            user_agent=config.collection.default_user_agent,
            timeout=30,
            max_concurrent=2
        )

        # HTML解析器
        html_parser = HTMLParser()

        # 文本标准化器
        text_normalizer = TextNormalizer()

        yield {
            "db_manager": db_manager,
            "storage_manager": storage_manager,
            "rss_collector": rss_collector,
            "html_parser": html_parser,
            "text_normalizer": text_normalizer
        }

        # 清理资源
        db_manager.close()

    def test_requirement_01_data_collection(self, components):
        """验收要求1: 数据采集功能正常工作"""
        print("🧪 验收测试 1: 数据采集功能")

        rss_collector = components["rss_collector"]

        # 使用真实的RSS源进行测试
        test_rss_url = "https://feeds.bbci.co.uk/news/rss.xml"

        async def collect_test():
            try:
                result = await rss_collector.collect_rss(test_rss_url)

                # 验证采集结果
                assert result is not None, "RSS采集应该返回结果"
                assert len(result.items) > 0, "应该采集到至少一个RSS条目"

                # 验证条目结构
                first_item = result.items[0]
                assert hasattr(first_item, 'title'), "RSS条目应该包含标题"
                assert hasattr(first_item, 'link'), "RSS条目应该包含链接"
                assert first_item.title.strip(), "标题不能为空"

                print(f"✅ 成功采集 {len(result.items)} 个RSS条目")
                return True

            except Exception as e:
                print(f"❌ RSS采集失败: {e}")
                return False

        # 运行异步测试
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(collect_test())
            assert success, "RSS采集功能应该正常工作"
        finally:
            loop.close()

    def test_requirement_02_data_processing(self, components):
        """验收要求2: 数据处理功能正常工作"""
        print("🧪 验收测试 2: 数据处理功能")

        html_parser = components["html_parser"]
        text_normalizer = components["text_normalizer"]

        # 测试HTML内容
        test_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>测试页面</title>
            <meta name="description" content="这是一个测试页面">
            <meta name="author" content="测试作者">
        </head>
        <body>
            <h1>主标题</h1>
            <p>这是第一段内容，包含<strong>加粗文字</strong>。</p>
            <p>这是第二段内容，包含<a href="https://example.com">链接</a>。</p>
            <div class="content">
                <p>这是容器中的内容。</p>
            </div>
        </body>
        </html>
        """

        try:
            # 测试HTML解析
            parsed_data = html_parser.parse_html_content(
                test_html,
                url="https://example.com/test",
                title_selector="h1",
                content_selector="p, .content p"
            )

            assert parsed_data is not None, "HTML解析应该返回结果"
            assert parsed_data.title == "主标题", "应该正确解析标题"
            assert "第一段内容" in parsed_data.content, "应该正确提取内容"
            assert parsed_data.description == "这是一个测试页面", "应该正确提取描述"

            # 测试文本标准化
            normalized_text = text_normalizer.normalize_text(parsed_data.content)

            assert normalized_text is not None, "文本标准化应该返回结果"
            assert len(normalized_text.strip()) > 0, "标准化后的文本不能为空"
            assert "加粗文字" in normalized_text, "应该保留重要内容"

            print("✅ HTML解析和文本标准化功能正常")

        except Exception as e:
            print(f"❌ 数据处理失败: {e}")
            pytest.fail("数据处理功能应该正常工作")

    def test_requirement_03_data_storage(self, components):
        """验收要求3: 数据存储功能正常工作"""
        print("🧪 验收测试 3: 数据存储功能")

        db_manager = components["db_manager"]
        storage_manager = components["storage_manager"]

        # 创建测试文档
        test_doc = RawDocument(
            id=uuid4(),
            title="验收测试文档",
            content="这是一个用于验收测试的文档",
            source_id="acceptance_test",
            source_url="https://example.com/acceptance-test",
            source_type=SourceType.WEBSITE,
            document_type=DocumentType.HTML,
            raw_content="<p>这是一个用于验收测试的文档</p>",
            raw_metadata={"test": True, "type": "acceptance"}
        )

        async def storage_test():
            try:
                # 测试文件存储
                file_path = await storage_manager.store_raw_document(test_doc)
                assert file_path.exists(), "文件应该成功存储"

                # 测试文件检索
                retrieved_doc = await storage_manager.retrieve_raw_document(test_doc.id)
                assert retrieved_doc is not None, "应该能够检索到存储的文档"
                assert retrieved_doc["title"] == test_doc.title, "检索的文档标题应该匹配"

                # 测试数据库存储
                db_manager.execute_query(
                    """INSERT INTO raw_documents
                       (id, title, source_id, source_url, source_type, document_type, raw_content, collected_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))""",
                    (str(test_doc.id), test_doc.title, test_doc.source_id, str(test_doc.source_url),
                     test_doc.source_type.value, test_doc.document_type.value, test_doc.raw_content)
                )

                # 验证数据库记录
                records = db_manager.execute_query(
                    "SELECT * FROM raw_documents WHERE id = ?",
                    (str(test_doc.id),)
                )
                assert len(records) == 1, "数据库中应该有一条记录"
                assert records[0]["title"] == test_doc.title, "数据库记录应该匹配"

                print("✅ 文件存储和数据库存储功能正常")
                return True

            except Exception as e:
                print(f"❌ 数据存储失败: {e}")
                return False

        # 运行异步测试
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(storage_test())
            assert success, "数据存储功能应该正常工作"
        finally:
            loop.close()

    def test_requirement_04_system_integration(self, components):
        """验收要求4: 系统集成功能正常工作"""
        print("🧪 验收测试 4: 系统集成功能")

        db_manager = components["db_manager"]

        try:
            # 测试数据库连接和查询
            result = db_manager.execute_query("SELECT 1 as test")
            assert len(result) == 1, "数据库查询应该返回结果"
            assert result[0]["test"] == 1, "查询结果应该正确"

            # 测试表存在性
            tables = ["raw_documents", "processed_documents", "data_sources"]
            for table in tables:
                count = db_manager.get_table_count(table)
                assert count >= 0, f"表 {table} 应该存在且可查询"

            # 测试数据库统计
            stats = db_manager.get_database_stats()
            assert "database_size_mb" in stats, "数据库统计应该包含大小信息"
            assert stats["database_size_mb"] >= 0, "数据库大小应该为非负数"

            print("✅ 系统集成功能正常")

        except Exception as e:
            print(f"❌ 系统集成失败: {e}")
            pytest.fail("系统集成功能应该正常工作")

    def test_requirement_05_error_handling(self, components):
        """验收要求5: 错误处理功能正常工作"""
        print("🧪 验收测试 5: 错误处理功能")

        rss_collector = components["rss_collector"]

        async def error_handling_test():
            try:
                # 测试无效URL处理
                invalid_urls = [
                    "http://invalid-domain-that-does-not-exist.com/rss.xml",
                    "https://httpbin.org/status/404",
                    "not-a-valid-url"
                ]

                handled_errors = 0
                for url in invalid_urls:
                    try:
                        await rss_collector.collect_rss(url)
                        print(f"⚠️ 意外成功: {url}")
                    except Exception:
                        handled_errors += 1
                        print(f"✅ 正确处理错误: {url}")

                assert handled_errors > 0, "应该处理至少一个错误"
                print(f"✅ 成功处理 {handled_errors} 个错误情况")
                return True

            except Exception as e:
                print(f"❌ 错误处理测试失败: {e}")
                return False

        # 运行异步测试
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(error_handling_test())
            assert success, "错误处理功能应该正常工作"
        finally:
            loop.close()


if __name__ == "__main__":
    print("🚀 开始Atlas系统用户验收测试")
    print("=" * 50)

    pytest.main([__file__, "-v", "-s"])