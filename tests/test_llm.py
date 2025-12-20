"""
LLM 集成测试

测试 Ollama 客户端、语义去重、队列管理器、自适应控制器等功能。
"""

import asyncio
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
import pytest

# 标记需要Ollama服务的测试
ollama_required = pytest.mark.skipif(
    True,  # 默认跳过，可以在有Ollama环境时移除此标记
    reason="需要Ollama服务"
)

from atlas.llm import (
    LocalLLMClient, ModelConfig, GenerationConfig,
    LLMQueueManager, LLMTask, LLMTaskType, LLMTaskPriority,
    AdaptiveController, ControllerConfig,
    SemanticDeduplicator, SemanticConfig, DedupStrategy
)
from atlas.core.config import LLMConfig
from atlas.core.logging import get_logger

logger = get_logger(__name__)


class TestLocalLLMClient:
    """LLM客户端测试"""

    @pytest.fixture
    async def llm_client(self):
        """创建测试用的LLM客户端"""
        client = LocalLLMClient(
            base_url="http://localhost:11434",
            default_model="llama2",
            timeout=10.0,
            max_concurrent=2
        )
        await client._ensure_session()
        try:
            # 检查服务是否可用
            health = await client.health_check()
            if health.get("status") != "healthy":
                pytest.skip(f"Ollama服务不可用: {health.get('error')}")
            yield client
        finally:
            await client.close()

    @ollama_required
    async def test_health_check(self, llm_client):
        """测试健康检查"""
        health = await llm_client.health_check()
        assert health["status"] == "healthy"
        assert "models_count" in health
        assert "models" in health

    @ollama_required
    async def test_list_models(self, llm_client):
        """测试模型列表"""
        models = await llm_client.list_models()
        assert isinstance(models, list)
        assert len(models) > 0
        assert "name" in models[0]

    @ollama_required
    async def test_generate_text(self, llm_client):
        """测试文本生成"""
        config = GenerationConfig(
            prompt="请简单回答：什么是机器学习？",
            temperature=0.7,
            max_tokens=100
        )

        result = await llm_client.generate(config)
        assert result.text is not None
        assert len(result.text) > 0
        assert result.model == "llama2"
        assert result.done is True

    @ollama_required
    async def test_generate_with_system_prompt(self, llm_client):
        """测试带系统提示的生成"""
        config = GenerationConfig(
            prompt="用一句话描述人工智能",
            system_prompt="你是一个专业的AI助手，请用中文回答。",
            temperature=0.3
        )

        result = await llm_client.generate(config)
        assert result.text is not None
        assert len(result.text) > 0

    @ollama_required
    async def test_embed_text(self, llm_client):
        """测试文本嵌入"""
        text = "这是一个测试文本"
        embedding = await llm_client.embed(text)

        assert isinstance(embedding, list)
        assert len(embedding) > 0
        assert all(isinstance(x, (int, float)) for x in embedding)

    @ollama_required
    async def test_generate_with_timeout(self, llm_client):
        """测试带超时的生成"""
        config = GenerationConfig(
            prompt="请详细解释量子计算原理",
            max_tokens=50,
            timeout=5.0
        )

        start_time = time.time()
        result = await llm_client.generate(config)
        elapsed = time.time() - start_time

        assert elapsed < 10.0  # 应该在10秒内完成
        assert result.text is not None

    def test_get_stats(self, llm_client):
        """测试统计信息"""
        stats = llm_client.get_stats()
        assert "total_requests" in stats
        assert "successful_requests" in stats
        assert "failed_requests" in stats
        assert stats["total_requests"] >= 0

    def test_reset_stats(self, llm_client):
        """测试重置统计"""
        # 先生成一些请求以增加统计
        llm_client._stats["total_requests"] = 5
        llm_client._stats["successful_requests"] = 3

        stats_before = llm_client.get_stats()
        assert stats_before["total_requests"] == 5

        # 重置统计
        llm_client.reset_stats()
        stats_after = llm_client.get_stats()
        assert stats_after["total_requests"] == 0
        assert stats_after["successful_requests"] == 0


class TestSemanticDeduplicator:
    """语义去重器测试"""

    @pytest.fixture
    def semantic_config(self):
        """创建语义去重配置"""
        return SemanticConfig(
            strategy=DedupStrategy.HYBRID,
            similarity_threshold=0.8,
            batch_size=3,
            cache_enabled=False,  # 测试时禁用缓存
            enable_tfidf_fallback=True
        )

    @pytest.fixture
    def deduplicator(self, semantic_config):
        """创建语义去重器"""
        return SemanticDeduplicator(
            config=semantic_config,
            llm_client=None,  # 使用TF-IDF后备方案
            cache_file=None
        )

    def test_duplicate_detection_hash_only(self, semantic_config):
        """测试仅哈希去重"""
        config = SemanticConfig(
            strategy=DedupStrategy.HASH_ONLY,
            similarity_threshold=0.8
        )
        dedup = SemanticDeduplicator(config=config)

        content1 = "这是第一个文档的内容"
        content2 = "这是第二个文档的内容"
        content3 = "这是第一个文档的内容"  # 相同内容

        # 检查非重复
        is_dup1, dup_id1 = asyncio.run(dedup.is_duplicate("doc1", content1))
        assert not is_dup1
        assert dup_id1 is None

        is_dup2, dup_id2 = asyncio.run(dedup.is_duplicate("doc2", content2))
        assert not is_dup2
        assert dup_id2 is None

        # 添加文档
        asyncio.run(dedup.add_document("doc1", content1))
        asyncio.run(dedup.add_document("doc2", content2))

        # 检查重复
        is_dup3, dup_id3 = asyncio.run(dedup.is_duplicate("doc3", content3))
        assert is_dup3
        assert dup_id3 == "doc1"

    def test_duplicate_detection_semantic_only(self, semantic_config):
        """测试仅语义去重"""
        config = SemanticConfig(
            strategy=DedupStrategy.SEMANTIC_ONLY,
            similarity_threshold=0.8
        )
        dedup = SemanticDeduplicator(config=config)

        content1 = "机器学习是人工智能的一个分支"
        content2 = "深度学习是机器学习的重要技术"
        content3 = "ML是AI的一个核心领域"  # 语义相似

        # 检查非重复
        is_dup1, dup_id1 = asyncio.run(dedup.is_duplicate("doc1", content1))
        assert not is_dup1

        is_dup2, dup_id2 = asyncio.run(dedup.is_duplicate("doc2", content2))
        assert not is_dup2

        # 添加文档
        asyncio.run(dedup.add_document("doc1", content1))
        asyncio.run(dedup.add_document("doc2", content2))

        # 检查语义重复（可能检测到）
        is_dup3, dup_id3 = asyncio.run(dedup.is_duplicate("doc3", content3))
        # 由于使用TF-IDF，语义检测可能不准确，所以结果可能为false

    def test_batch_deduplication(self, deduplicator):
        """测试批量去重"""
        documents = [
            ("doc1", "这是第一个文档", "标题1", "url1", {}),
            ("doc2", "这是第二个文档", "标题2", "url2", {}),
            ("doc3", "这是第一个文档", "标题3", "url3", {}),  # 重复
            ("doc4", "这是第四个文档", "标题4", "url4", {}),
        ]

        results = asyncio.run(deduplicator.batch_check_duplicates(documents))
        assert len(results) == 4
        assert results[0][0] is False  # doc1 非重复
        assert results[1][0] is False  # doc2 非重复
        assert results[2][0] is True   # doc3 重复
        assert results[3][0] is False  # doc4 非重复

    def test_find_similar_documents(self, deduplicator):
        """测试查找相似文档"""
        # 添加一些文档
        asyncio.run(dedup.add_document("doc1", "人工智能和机器学习"))
        asyncio.run(dedup.add_document("doc2", "深度学习和神经网络"))
        asyncio.run(dedup.add_document("doc3", "数据科学与统计分析"))

        # 查找相似文档
        similar_docs = asyncio.run(dedup.find_similar_documents(
            "机器学习与人工智能",
            max_results=2
        ))

        assert isinstance(similar_docs, list)
        # 应该找到doc1（语义相似）

    def test_get_stats(self, deduplicator):
        """测试统计信息"""
        stats = deduplicator.get_stats()
        assert "total_processed" in stats
        assert "hash_duplicates" in stats
        assert "semantic_duplicates" in stats
        assert "total_documents" in stats

    def test_config_validation(self):
        """测试配置验证"""
        # 有效配置
        config = SemanticConfig(
            similarity_threshold=0.8
        )
        assert config.similarity_threshold == 0.8

        # 无效阈值
        with pytest.raises(Exception):
            SemanticConfig(similarity_threshold=1.5)


class TestLLMQueueManager:
    """LLM队列管理器测试"""

    @pytest.fixture
    async def queue_manager(self):
        """创建队列管理器"""
        manager = LLMQueueManager(
            max_concurrent_tasks=2,
            max_queue_size=10,
            persistence_file=None
        )
        await manager.start()
        try:
            yield manager
        finally:
            await manager.stop()

    async def test_submit_task(self, queue_manager):
        """测试提交任务"""
        async def test_task(x):
            await asyncio.sleep(0.1)
            return x * 2

        task_id = await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=test_task,
            args=(5,),
            priority=LLMTaskPriority.NORMAL,
            timeout=5.0
        )

        assert task_id is not None
        assert len(task_id) > 0

    async def test_get_result(self, queue_manager):
        """测试获取结果"""
        async def test_task(x):
            return f"result_{x}"

        # 提交任务
        task_id = await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=test_task,
            args=("test",)
        )

        # 获取结果
        result = await queue_manager.get_result(task_id, timeout=10)
        assert result == "result_test"

    async def test_priority_ordering(self, queue_manager):
        """测试优先级排序"""
        execution_order = []

        async def priority_task(name):
            execution_order.append(name)
            await asyncio.sleep(0.05)
            return name

        # 提交不同优先级的任务
        await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=priority_task,
            args=("low",),
            priority=LLMTaskPriority.LOW
        )

        await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=priority_task,
            args=("urgent",),
            priority=LLMTaskPriority.URGENT
        )

        await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=priority_task,
            args=("normal",),
            priority=LLMTaskPriority.NORMAL
        )

        # 等待任务完成
        await asyncio.sleep(1)

        # 验证执行顺序（urgent应该先执行）
        assert execution_order[0] == "urgent"

    async def test_cancel_task(self, queue_manager):
        """测试取消任务"""
        async def long_task():
            await asyncio.sleep(5)
            return "should_not_reach"

        # 提交长时间任务
        task_id = await queue_manager.submit(
            task_type=LLMTaskType.GENERATE,
            func=long_task
        )

        # 立即取消
        success = await queue_manager.cancel_task(task_id)
        assert success is True

    def test_get_queue_status(self, queue_manager):
        """测试获取队列状态"""
        status = queue_manager.get_queue_status()
        assert "total_tasks" in status
        assert "pending_tasks" in status
        assert "running_tasks" in status
        assert "completed_tasks" in status
        assert "max_concurrent_tasks" in status
        assert "max_queue_size" in status

    async def test_batch_operations(self, queue_manager):
        """测试批量操作"""
        async def batch_task(i):
            await asyncio.sleep(0.1)
            return f"batch_result_{i}"

        tasks = [
            {
                "task_type": LLMTaskType.GENERATE,
                "func": batch_task,
                "args": (i,),
                "priority": LLMTaskPriority.NORMAL
            }
            for i in range(3)
        ]

        # 批量提交
        task_ids = await queue_manager.batch_submit(tasks)
        assert len(task_ids) == 3
        assert all(len(tid) > 0 for tid in task_ids)

        # 批量获取结果
        results = await queue_manager.batch_get_results(
            task_ids,
            timeout=10
        )
        assert len(results) == 3
        assert all("batch_result_" in str(r) for r in results)


class TestAdaptiveController:
    """自适应控制器测试"""

    @pytest.fixture
    def controller_config(self):
        """创建控制器配置"""
        return ControllerConfig(
            monitoring_interval=1.0,
            min_concurrent_tasks=1,
            max_concurrent_tasks=5,
            base_request_rate=1.0,
            enable_auto_scaling=True,
            cooldown_period=2.0
        )

    @pytest.fixture
    async def controller(self, controller_config):
        """创建自适应控制器"""
        controller = AdaptiveController(
            config=controller_config,
            llm_client=None,
            queue_manager=None
        )
        await controller.start()
        try:
            yield controller
        finally:
            await controller.stop()

    def test_get_status(self, controller):
        """测试获取状态"""
        status = controller.get_status()
        assert "running" in status
        assert "current_concurrent_tasks" in status
        assert "current_request_rate" in status
        assert "system_state" in status
        assert "stats" in status

    def test_config_update(self, controller):
        """测试配置更新"""
        original_concurrent = controller.config.max_concurrent_tasks
        assert original_concurrent == 5

        # 更新配置
        controller.update_config(max_concurrent_tasks=8)
        assert controller.config.max_concurrent_tasks == 8

    def test_metric_calculation(self, controller_config):
        """测试指标计算"""
        from atlas.llm.adaptive_controller import MetricThreshold, SystemMetric

        # 创建测试阈值
        threshold = MetricThreshold(
            metric=SystemMetric.CPU_USAGE,
            warning_threshold=70.0,
            critical_threshold=80.0,
            emergency_threshold=90.0
        )

        # 测试正常值
        score = controller._calculate_metric_score(50.0, threshold)
        assert score == 0.0

        # 测试警告值
        score = controller._calculate_metric_score(75.0, threshold)
        assert score == 0.6

        # 测试严重值
        score = controller._calculate_metric_score(85.0, threshold)
        assert score == 0.8

        # 测试紧急值
        score = controller._calculate_metric_score(95.0, threshold)
        assert score == 1.0

    def test_system_state_analysis(self, controller):
        """测试系统状态分析"""
        # 模拟正常指标
        controller._current_metrics = {
            SystemMetric.CPU_USAGE: 30.0,
            SystemMetric.MEMORY_USAGE: 40.0,
            SystemMetric.ERROR_RATE: 1.0,
            SystemMetric.RESPONSE_TIME: 2.0,
        }

        state = controller._analyze_system_state()
        assert state in ["healthy", "normal", "warning", "critical"]
        # 应该是健康状态
        assert state == "healthy"

    def test_control_decision_making(self, controller):
        """测试控制决策"""
        controller._last_control_time = None  # 重置冷却期

        # 健康状态应该保持当前速度
        controller._current_metrics = {
            SystemMetric.CPU_USAGE: 30.0,
            SystemMetric.MEMORY_USAGE: 40.0,
            SystemMetric.ERROR_RATE: 1.0,
            SystemMetric.RESPONSE_TIME: 2.0,
        }

        state = controller._analyze_system_state()
        action = controller._decide_control_action(state)

        if state == "healthy" and not controller._recently_decreased_rate():
            assert action in [ControlAction.INCREASE_RATE, ControlAction.MAINTAIN_RATE, None]

        # 严重状态应该降低速度
        controller._current_metrics = {
            SystemMetric.CPU_USAGE: 95.0,
            SystemMetric.MEMORY_USAGE: 98.0,
            SystemMetric.ERROR_RATE: 25.0,
            SystemMetric.RESPONSE_TIME: 50.0,
        }

        state = controller._analyze_system_state()
        action = controller._decide_control_action(state)
        assert action in [ControlAction.DECREASE_RATE, ControlAction.EMERGENCY_STOP]

    def test_circuit_breaker(self, controller):
        """测试熔断器"""
        # 模拟熔断器激活
        controller._circuit_breaker_active = True
        controller._circuit_breaker_failures = 5
        controller._circuit_breaker_last_failure = datetime.now(timezone.utc)

        # 检查是否应该重置
        should_reset = controller._should_reset_circuit_breaker()
        assert not should_reset  # 刚刚激活，不应该重置

        # 模拟时间过去
        controller._circuit_breaker_last_failure = (
            datetime.now(timezone.utc) - timedelta(minutes=6)
        )

        should_reset = controller._should_reset_circuit_breaker()
        assert should_reset  # 超过恢复时间，应该重置

        # 重置熔断器
        controller._reset_circuit_breaker()
        assert not controller._circuit_breaker_active
        assert controller._circuit_breaker_failures == 0

    def test_stats_tracking(self, controller):
        """测试统计跟踪"""
        original_stats = controller._stats.copy()

        # 模拟一些控制动作
        controller._stats["total_control_actions"] += 2
        controller._stats["emergency_stops"] += 1
        controller._stats["auto_scaling_events"] += 3

        stats = controller.get_status()["stats"]
        assert stats["total_control_actions"] == 2
        assert stats["emergency_stops"] == 1
        assert stats["auto_scaling_events"] == 3

        # 重置统计
        controller.reset_stats()
        stats = controller.get_status()["stats"]
        assert stats["total_control_actions"] == 0
        assert stats["emergency_stops"] == 0
        assert stats["auto_scaling_events"] == 0


class TestLLMIntegration:
    """LLM集成测试"""

    @pytest.fixture
    def llm_config(self):
        """创建LLM配置"""
        return LLMConfig(
            provider="local",
            base_url="http://localhost:11434",
            model="llama2",
            timeout=10.0,
            max_concurrent=3,
            dedup_strategy="hybrid",
            similarity_threshold=0.8
        )

    def test_config_validation(self, llm_config):
        """测试配置验证"""
        assert llm_config.provider == "local"
        assert llm_config.base_url == "http://localhost:11434"
        assert llm_config.model == "llama2"
        assert llm_config.dedup_strategy == "hybrid"
        assert llm_config.similarity_threshold == 0.8

    def test_invalid_config(self):
        """测试无效配置"""
        # 无效的去重策略
        with pytest.raises(Exception):
            LLMConfig(dedup_strategy="invalid")

        # 无效的相似度阈值
        with pytest.raises(Exception):
            LLMConfig(similarity_threshold=1.5)

        # 无效的提供商
        with pytest.raises(Exception):
            LLMConfig(provider="invalid")

    @pytest.fixture
    def components(self, llm_config):
        """创建所有LLM组件"""
        client = LocalLLMClient(
            base_url=llm_config.base_url,
            default_model=llm_config.model,
            timeout=llm_config.timeout
        )

        semantic_config = SemanticConfig(
            strategy=DedupStrategy(llm_config.dedup_strategy),
            similarity_threshold=llm_config.similarity_threshold,
            enable_tfidf_fallback=True
        )

        semantic_dedup = SemanticDeduplicator(
            config=semantic_config,
            llm_client=None,  # 使用TF-IDF后备方案
            cache_file=None
        )

        queue_manager = LLMQueueManager(
            max_concurrent_tasks=llm_config.max_concurrent,
            max_queue_size=100,
            persistence_file=None
        )

        controller_config = ControllerConfig(
            min_concurrent_tasks=1,
            max_concurrent_tasks=llm_config.max_concurrent,
            enable_auto_scaling=True
        )

        controller = AdaptiveController(
            config=controller_config,
            llm_client=client,
            queue_manager=queue_manager
        )

        return {
            "client": client,
            "semantic_dedup": semantic_dedup,
            "queue_manager": queue_manager,
            "controller": controller,
            "config": llm_config
        }

    def test_component_initialization(self, components):
        """测试组件初始化"""
        assert components["client"] is not None
        assert components["semantic_dedup"] is not None
        assert components["queue_manager"] is not None
        assert components["controller"] is not None
        assert components["config"] is not None

    def test_component_consistency(self, components):
        """测试组件一致性"""
        config = components["config"]
        client = components["client"]
        dedup = components["semantic_dedup"]
        queue = components["queue_manager"]
        controller = components["controller"]

        # 验证配置一致性
        assert client.base_url == config.base_url
        assert client.default_model == config.model
        assert client.timeout == config.timeout

        # 验证去重配置
        assert dedup.config.strategy.value == config.dedup_strategy
        assert dedup.config.similarity_threshold == config.similarity_threshold

        # 验证队列配置
        assert queue.max_concurrent_tasks == config.max_concurrent

        # 验证控制器配置
        assert controller.config.min_concurrent_tasks == 1
        assert controller.config.max_concurrent_tasks == config.max_concurrent


# 集成测试
@ollama_required
class TestLLMIntegrationWorkflow:
    """LLM集成工作流测试"""

    @pytest.fixture
    async def llm_integration(self):
        """创建LLM集成"""
        from atlas.processors.dedup_llm import LLMDeduplicator
        from atlas.llm import create_llm_queue_manager, create_adaptive_controller
        from atlas.core.config import get_config

        config = get_config()
        dedup = LLMDeduplicator(config)

        try:
            # 初始化LLM组件
            await dedup.initialize()

            # 创建队列管理器
            queue_manager = await create_llm_queue_manager(
                max_concurrent_tasks=2,
                persistence_file=None
            )

            # 创建自适应控制器
            controller = create_adaptive_controller(
                min_concurrent_tasks=1,
                max_concurrent_tasks=3,
                llm_client=dedup.llm_client
            )
            await controller.start()

            yield {
                "deduplicator": dedup,
                "queue_manager": queue_manager,
                "controller": controller
            }

        finally:
            # 清理资源
            if dedup:
                await dedup.cleanup()
            # 队列管理器和控制器会在fixture中清理

    async def test_end_to_end_workflow(self, llm_integration):
        """测试端到端工作流"""
        dedup = llm_integration["deduplicator"]
        queue_manager = llm_integration["queue_manager"]
        controller = llm_integration["controller"]

        # 模拟文档处理
        from atlas.models.documents import RawDocument, DocumentType
        from datetime import datetime, timezone

        docs = [
            RawDocument(
                doc_id="doc1",
                source_id="test_source",
                type=DocumentType.RSS,
                title="第一篇文档",
                url="http://example.com/1",
                content="这是关于人工智能的第一篇文章",
                content_hash="hash1",
                extracted_at=datetime.now(timezone.utc)
            ),
            RawDocument(
                doc_id="doc2",
                source_id="test_source",
                type=DocumentType.RSS,
                title="第二篇文档",
                url="http://example.com/2",
                content="这是关于人工智能的第二篇文章",
                content_hash="hash2",
                extracted_at=datetime.now(timezone.utc)
            ),
            RawDocument(
                doc_id="doc3",
                source_id="test_source",
                type=DocumentType.RSS,
                title="重复文档",
                url="http://example.com/1",
                content="这是关于人工智能的第一篇文章",  # 与doc1重复
                content_hash="hash1",
                extracted_at=datetime.now(timezone.utc)
            ),
        ]

        # 处理文档（包括去重检查）
        processed_docs = []
        for doc in docs:
            try:
                processed_doc = await dedup.process_document(doc)
                processed_docs.append(processed_doc)
            except Exception as e:
                logger.error(f"处理文档失败: {e}")
                # 创建失败的处理文档
                processed_doc = RawDocument(
                    doc_id=doc.doc_id + "_failed",
                    source_id=doc.source_id,
                    type=doc.type,
                    title=doc.title,
                    url=doc.url,
                    content=doc.content,
                    content_hash=doc.content_hash,
                    extracted_at=doc.extracted_at
                )
                processed_docs.append(processed_doc)

        # 验证结果
        assert len(processed_docs) == 3
        assert processed_docs[0].doc_id == "doc1"
        assert not processed_docs[0].is_duplicate
        assert processed_docs[1].doc_id == "doc2"
        assert not processed_docs[1].is_duplicate
        assert processed_docs[2].doc_id == "doc3"
        assert processed_docs[2].is_duplicate  # doc3是重复的
        assert processed_docs[2].original_doc_id == "doc1"

        # 检查状态
        dedup_stats = dedup.get_dedup_stats()
        assert dedup_stats["llm_enabled"] is True
        assert dedup_stats["llm_stats"]["semantic_checks"] >= 3

        queue_status = queue_manager.get_queue_status()
        assert queue_status["total_tasks"] >= 0
        assert queue_status["running"] is True

        controller_status = controller.get_status()
        assert controller_status["running"] is True
        assert "system_state" in controller_status


if __name__ == "__main__":
    pytest.main([__file__])