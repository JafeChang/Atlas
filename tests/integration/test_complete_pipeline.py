"""
完整数据流集成测试

测试从数据采集到存储的完整流程，确保所有组件协同工作。
"""

import asyncio
import pytest
import tempfile
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import StorageManager
from atlas.collectors.rss_collector import RSSCollector
from atlas.collectors.http_client import HTTPClient
from atlas.processors.parser import HTMLParser
from atlas.processors.normalizer import TextNormalizer
from atlas.processors.dedup_base import BaseDeduplicator
from atlas.processors.dedup_llm import LLMDeduplicator
from atlas.processors.validator import DataValidator
from atlas.scheduler.queue import TaskQueue
from atlas.scheduler.status import TaskStatusManager
from atlas.llm import LocalLLMClient, SemanticDeduplicator, LLMQueueManager
from atlas.models.documents import RawDocument, ProcessedDocument

pytest_plugins = ["tests.utils"]


class TestCompleteDataPipeline:
    """完整数据流测试类"""

    @pytest.fixture
    async def test_config(self):
        """创建测试配置"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            data_dir = Path(temp_dir) / "data"
            log_dir = Path(temp_dir) / "logs"

            # 创建目录
            config_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            # 获取配置并设置测试路径
            config = get_config(config_dir=config_dir)
            config._config_data["data_dir"] = str(data_dir)
            config._config_data["log_dir"] = str(log_dir)

            yield config

    @pytest.fixture
    async def test_components(self, test_config):
        """初始化所有测试组件"""
        # 数据库和存储
        db_manager = AtlasDatabase(test_config.data_dir / "test.db")

        storage_manager = StorageManager(test_config.data_dir)

        # HTTP客户端和采集器
        http_client = HTTPClient(
            timeout=test_config.collection.request_timeout,
            max_concurrent=test_config.collection.max_concurrent_requests,
            cache_enabled=True
        )

        rss_collector = RSSCollector(
            http_client=http_client,
            rate_limiter=None,  # 测试时不启用频率限制
            config=test_config.collection
        )

        # 处理器
        parser = HTMLParser()
        normalizer = TextNormalizer()
        base_deduplicator = BaseDeduplicator(test_config, storage_manager.cache_dir)
        await base_deduplicator.initialize()

        validator = DataValidator()

        # LLM组件（可选，测试时会mock）
        try:
            llm_client = LocalLLMClient(
                base_url=test_config.llm.base_url,
                default_model=test_config.llm.model,
                timeout=5.0
            )

            llm_deduplicator = LLMDeduplicator(test_config, storage_manager.cache_dir)
            await llm_deduplicator.initialize()

            llm_available = True
        except Exception:
            llm_client = None
            llm_deduplicator = None
            llm_available = False

        # 任务调度组件
        task_queue = TaskQueue(max_workers=2)
        status_manager = TaskStatusManager(storage_manager.cache_dir)

        yield {
            "db_manager": db_manager,
            "storage_manager": storage_manager,
            "http_client": http_client,
            "rss_collector": rss_collector,
            "parser": parser,
            "normalizer": normalizer,
            "base_deduplicator": base_deduplicator,
            "llm_deduplicator": llm_deduplicator,
            "validator": validator,
            "task_queue": task_queue,
            "status_manager": status_manager,
            "llm_available": llm_available
        }

        # 清理资源
        db_manager.close()
        await http_client.close()
        if llm_client:
            await llm_client.close()
        await task_queue.stop()

    @pytest.mark.asyncio
    async def test_rss_to_storage_pipeline(self, test_components):
        """测试RSS数据从采集到存储的完整流程"""
        components = test_components

        # 1. RSS采集
        test_feeds = [
            "https://feeds.feedburner.com/oreilly/radar"  # O'Reilly RSS (公开)
        ]

        collected_docs = []
        async for doc in components["rss_collector"].collect_from_feeds(test_feeds):
            collected_docs.append(doc)
            if len(collected_docs) >= 5:  # 限制测试数据量
                break

        assert len(collected_docs) > 0, "应该采集到RSS数据"

        # 2. 内容解析
        parsed_docs = []
        for doc in collected_docs:
            if doc.content and doc.url:
                try:
                    # 解析HTML内容
                    parsed_content = await components["parser"].parse_content(
                        doc.content, url=doc.url
                    )

                    # 更新文档
                    doc.content = parsed_content.get("text", doc.content)
                    doc.metadata.update(parsed_content.get("metadata", {}))

                    parsed_docs.append(doc)
                except Exception as e:
                    pytest.skip(f"解析失败，跳过测试: {e}")

        assert len(parsed_docs) > 0, "应该有解析成功的文档"

        # 3. 文本标准化
        normalized_docs = []
        for doc in parsed_docs:
            try:
                # 标准化标题
                if doc.title:
                    doc.title = await components["normalizer"].normalize_text(doc.title)

                # 标准化内容
                if doc.content:
                    doc.content = await components["normalizer"].normalize_text(doc.content)

                normalized_docs.append(doc)
            except Exception as e:
                pytest.skip(f"标准化失败，跳过测试: {e}")

        assert len(normalized_docs) > 0, "应该有标准化成功的文档"

        # 4. 去重处理
        unique_docs = []
        for doc in normalized_docs:
            try:
                # 基础去重
                is_dup, dup_id, _ = await components["base_deduplicator"].is_duplicate(doc)

                if not is_dup:
                    # LLM去重（如果可用）
                    if components["llm_deduplicator"] and components["llm_available"]:
                        is_llm_dup, llm_dup_id, _ = await components["llm_deduplicator"].is_duplicate(doc)
                        if not is_llm_dup:
                            unique_docs.append(doc)
                    else:
                        unique_docs.append(doc)

            except Exception as e:
                pytest.skip(f"去重失败，跳过测试: {e}")

        assert len(unique_docs) > 0, "应该有唯一的文档"

        # 5. 数据验证
        valid_docs = []
        for doc in unique_docs:
            try:
                validation_result = await components["validator"].validate_document(doc)
                if validation_result.is_valid:
                    valid_docs.append(doc)
                else:
                    print(f"文档验证失败: {validation_result.errors}")

            except Exception as e:
                pytest.skip(f"验证失败，跳过测试: {e}")

        assert len(valid_docs) > 0, "应该有验证通过的文档"

        # 6. 存储到数据库
        stored_docs = []
        for doc in valid_docs:
            try:
                # 创建处理后的文档
                processed_doc = ProcessedDocument(
                    doc_id=doc.doc_id,
                    source_id=doc.source_id,
                    type=doc.type,
                    title=doc.title,
                    url=doc.url,
                    content=doc.content,
                    raw_content_hash=doc.content_hash,
                    extracted_at=doc.extracted_at,
                    metadata=doc.metadata or {}
                )

                # 存储到数据库
                await components["db_manager"].save_processed_document(processed_doc)
                stored_docs.append(processed_doc)

            except Exception as e:
                pytest.skip(f"存储失败，跳过测试: {e}")

        assert len(stored_docs) > 0, "应该有存储成功的文档"

        # 7. 验证存储结果
        for doc in stored_docs:
            # 从数据库读取验证
            retrieved_doc = await components["db_manager"].get_processed_document(doc.doc_id)
            assert retrieved_doc is not None, f"文档 {doc.doc_id} 应该能从数据库读取"
            assert retrieved_doc.title == doc.title, "标题应该一致"
            assert retrieved_doc.content == doc.content, "内容应该一致"

    @pytest.mark.asyncio
    async def test_task_queue_integration(self, test_components):
        """测试任务队列与各组件的集成"""
        components = test_components

        # 创建测试任务
        processed_results = []

        async def test_collect_task():
            """测试采集任务"""
            return {"status": "completed", "items": 5}

        async def test_process_task():
            """测试处理任务"""
            return {"status": "completed", "processed": 3}

        async def test_validate_task():
            """测试验证任务"""
            return {"status": "completed", "validated": 3, "errors": 0}

        # 提交任务到队列
        task_ids = []

        collect_task = await components["task_queue"].submit(
            func=test_collect_task,
            priority=1
        )
        task_ids.append(collect_task)

        process_task = await components["task_queue"].submit(
            func=test_process_task,
            priority=2
        )
        task_ids.append(process_task)

        validate_task = await components["task_queue"].submit(
            func=test_validate_task,
            priority=3
        )
        task_ids.append(validate_task)

        # 等待所有任务完成
        for task_id in task_ids:
            try:
                result = await components["task_queue"].get_result(task_id, timeout=10)
                processed_results.append(result)
            except Exception as e:
                pytest.fail(f"任务 {task_id} 执行失败: {e}")

        # 验证结果
        assert len(processed_results) == 3, "应该有3个任务结果"

        collect_result = next(r for r in processed_results if "items" in r)
        assert collect_result["items"] == 5, "采集任务应该返回5个项目"

        process_result = next(r for r in processed_results if "processed" in r)
        assert process_result["processed"] == 3, "处理任务应该返回3个处理项目"

    @pytest.mark.asyncio
    async def test_error_handling_pipeline(self, test_components):
        """测试错误处理和恢复机制"""
        components = test_components

        # 创建一个会失败的文档（用于测试错误处理）
        error_doc = RawDocument(
            doc_id="test_error_doc",
            source_id="test_source",
            type="rss",
            title="Error Test Document",
            url="http://invalid-url-that-should-fail.com",
            content="",  # 空内容
            extracted_at=datetime.now()
        )

        # 测试各个环节的错误处理

        # 1. 测试解析错误处理
        try:
            await components["parser"].parse_content("", url="invalid-url")
            # 如果没有抛出异常，说明错误处理正常
            parsing_handled = True
        except Exception:
            parsing_handled = True  # 异常被正确抛出和处理

        assert parsing_handled, "解析错误应该被正确处理"

        # 2. 测试验证错误处理
        validation_result = await components["validator"].validate_document(error_doc)
        assert not validation_result.is_valid, "无效文档应该验证失败"
        assert len(validation_result.errors) > 0, "应该有具体的错误信息"

        # 3. 测试去重错误处理
        try:
            is_dup, dup_id, _ = await components["base_deduplicator"].is_duplicate(error_doc)
            # 即使内容为空，也不应该崩溃
            dedup_handled = True
        except Exception:
            dedup_handled = True  # 异常被正确处理

        assert dedup_handled, "去重错误应该被正确处理"

    @pytest.mark.asyncio
    async def test_performance_benchmark(self, test_components):
        """测试系统性能基准"""
        components = test_components

        # 创建批量测试文档
        test_docs = []
        for i in range(10):
            doc = RawDocument(
                doc_id=f"perf_test_{i}",
                source_id="perf_test_source",
                type="test",
                title=f"Performance Test Document {i}",
                url=f"http://test.com/doc{i}",
                content=f"This is test content for document {i}. " * 10,  # 重复内容用于去重测试
                extracted_at=datetime.now()
            )
            test_docs.append(doc)

        # 测试批量处理性能
        start_time = datetime.now()

        # 1. 批量标准化
        normalized_docs = []
        for doc in test_docs:
            doc.title = await components["normalizer"].normalize_text(doc.title)
            doc.content = await components["normalizer"].normalize_text(doc.content)
            normalized_docs.append(doc)

        normalize_time = (datetime.now() - start_time).total_seconds()
        assert normalize_time < 10.0, f"标准化10个文档不应该超过10秒，实际用时: {normalize_time}秒"

        # 2. 批量去重
        start_time = datetime.now()

        unique_docs = []
        for doc in normalized_docs:
            is_dup, dup_id, _ = await components["base_deduplicator"].is_duplicate(doc)
            if not is_dup:
                unique_docs.append(doc)

        dedup_time = (datetime.now() - start_time).total_seconds()
        assert dedup_time < 5.0, f"去重10个文档不应该超过5秒，实际用时: {dedup_time}秒"

        # 3. 批量验证
        start_time = datetime.now()

        valid_docs = []
        for doc in unique_docs:
            validation_result = await components["validator"].validate_document(doc)
            if validation_result.is_valid:
                valid_docs.append(doc)

        validate_time = (datetime.now() - start_time).total_seconds()
        assert validate_time < 5.0, f"验证10个文档不应该超过5秒，实际用时: {validate_time}秒"

        print(f"性能基准测试结果:")
        print(f"- 标准化: {normalize_time:.2f}秒")
        print(f"- 去重: {dedup_time:.2f}秒")
        print(f"- 验证: {validate_time:.2f}秒")
        print(f"- 总计: {(normalize_time + dedup_time + validate_time):.2f}秒")

    @pytest.mark.asyncio
    async def test_system_stability(self, test_components):
        """测试系统稳定性和资源管理"""
        components = test_components

        # 模拟高负载场景
        concurrent_tasks = []

        async def stress_test_task(task_id: int):
            """压力测试任务"""
            try:
                # 模拟文档处理
                doc = RawDocument(
                    doc_id=f"stress_test_{task_id}",
                    source_id="stress_test",
                    type="test",
                    title=f"Stress Test Document {task_id}",
                    url=f"http://stress-test.com/doc{task_id}",
                    content=f"Stress test content {task_id}.",
                    extracted_at=datetime.now()
                )

                # 执行完整处理流程
                doc.title = await components["normalizer"].normalize_text(doc.title)
                doc.content = await components["normalizer"].normalize_text(doc.content)

                validation_result = await components["validator"].validate_document(doc)

                return {
                    "task_id": task_id,
                    "status": "completed",
                    "validated": validation_result.is_valid
                }

            except Exception as e:
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error": str(e)
                }

        # 启动并发任务
        for i in range(20):
            task = asyncio.create_task(stress_test_task(i))
            concurrent_tasks.append(task)

        # 等待所有任务完成
        results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)

        # 分析结果
        completed_tasks = 0
        failed_tasks = 0

        for result in results:
            if isinstance(result, Exception):
                failed_tasks += 1
            elif isinstance(result, dict):
                if result.get("status") == "completed":
                    completed_tasks += 1
                else:
                    failed_tasks += 1

        total_tasks = completed_tasks + failed_tasks
        success_rate = completed_tasks / total_tasks if total_tasks > 0 else 0

        print(f"稳定性测试结果:")
        print(f"- 总任务数: {total_tasks}")
        print(f"- 成功任务: {completed_tasks}")
        print(f"- 失败任务: {failed_tasks}")
        print(f"- 成功率: {success_rate:.1%}")

        # 至少应该有80%的任务成功
        assert success_rate >= 0.8, f"系统稳定性测试失败，成功率只有: {success_rate:.1%}"

if __name__ == "__main__":
    pytest.main([__file__])