#!/usr/bin/env python3
"""
Atlas LLM 集成示例

演示如何使用 LLM 功能进行智能内容处理。
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.core.config import get_config
from atlas.llm import (
    LocalLLMClient, GenerationConfig,
    SemanticDeduplicator, SemanticConfig, DedupStrategy,
    LLMQueueManager, LLMTaskType, LLMTaskPriority,
    AdaptiveController, ControllerConfig
)
from atlas.core.logging import get_logger

logger = get_logger(__name__)


async def demo_llm_client():
    """演示 LLM 客户端功能"""
    print("=== LLM 客户端演示 ===")

    try:
        # 创建 LLM 客户端
        client = LocalLLMClient(
            base_url="http://localhost:11434",
            default_model="llama2",
            timeout=30.0
        )

        async with client:
            # 检查服务健康状态
            print("1. 检查 Ollama 服务状态...")
            health = await client.health_check()
            print(f"   状态: {health['status']}")
            print(f"   可用模型: {health['models_count']}")
            print(f"   模型列表: {health['models'][:3]}...")

            # 列出所有模型
            print("\n2. 列出可用模型...")
            models = await client.list_models()
            for i, model in enumerate(models[:5], 1):
                print(f"   {i}. {model['name']}")

            # 文本生成
            print("\n3. 文本生成演示...")
            config = GenerationConfig(
                prompt="请简单介绍什么是机器学习？",
                system_prompt="你是一个知识渊博的AI助手，请用简洁明了的语言回答。",
                temperature=0.7,
                max_tokens=150
            )

            result = await client.generate(config)
            print(f"   生成结果: {result.text[:100]}...")
            print(f"   使用模型: {result.model}")
            print(f"   执行时间: {result.total_duration}ms")

    except Exception as e:
        print(f"   错误: {e}")
        print("   请确保 Ollama 服务正在运行 (ollama serve)")


async def demo_semantic_deduplication():
    """演示语义去重功能"""
    print("\n=== 语义去重演示 ===")

    try:
        # 创建语义去重器（使用TF-IDF后备方案）
        config = SemanticConfig(
            strategy=DedupStrategy.HYBRID,
            similarity_threshold=0.8,
            enable_tfidf_fallback=True,
            cache_enabled=False
        )

        deduplicator = SemanticDeduplicator(config=config, llm_client=None)

        # 测试文档
        documents = [
            ("doc1", "机器学习是人工智能的一个重要分支，它使用算法来从数据中学习模式。"),
            ("doc2", "深度学习是机器学习中的先进技术，特别适合处理复杂的非线性问题。"),
            ("doc3", "ML是AI领域中的核心技术，广泛应用于各种实际应用场景。"),
            ("doc4", "今天的天气很好，适合出去散步。"),
            ("doc5", "数据库管理系统用于存储、检索和管理结构化数据。"),
        ]

        print("1. 文档内容预览:")
        for doc_id, content in documents:
            preview = content[:50] + "..." if len(content) > 50 else content
            print(f"   {doc_id}: {preview}")

        # 批量检查重复
        print("\n2. 批量去重检查...")
        results = await deduplicator.batch_check_duplicates([
            (doc_id, content, "", "", {})
            for doc_id, content in documents
        ])

        print("3. 去重结果:")
        for (doc_id, content, _, _), (is_dup, dup_id) in zip(documents, results):
            status = "重复" if is_dup else "唯一"
            dup_info = f" (重复于 {dup_id})" if is_dup and dup_id else ""
            print(f"   {doc_id}: {status}{dup_info}")
            print(f"      内容: {content[:30]}...")

        # 获取统计信息
        stats = deduplicator.get_stats()
        print(f"\n4. 去重统计:")
        print(f"   总处理文档: {stats['total_processed']}")
        print(f"   哈希重复: {stats['hash_duplicates']}")
        print(f"   语义重复: {stats['semantic_duplicates']}")
        print(f"   总重复率: {stats['duplicate_rate']:.1f}%")

        # 查找相似文档
        print("\n5. 相似文档查找...")
        query_content = "人工智能和机器学习的关系"
        similar_docs = await deduplicator.find_similar_documents(
            content=query_content,
            max_results=3
        )

        if similar_docs:
            print(f"   查询: {query_content}")
            print(f"   找到 {len(similar_docs)} 个相似文档:")
            for i, (doc_id, similarity) in enumerate(similar_docs, 1):
                print(f"   {i}. 文档ID: {doc_id}, 相似度: {similarity:.3f}")
        else:
            print(f"   未找到相似文档")

    except Exception as e:
        print(f"   错误: {e}")


async def demo_queue_manager():
    """演示任务队列管理器"""
    print("\n=== 任务队列管理器演示 ===")

    try:
        # 创建队列管理器
        manager = LLMQueueManager(
            max_concurrent_tasks=3,
            max_queue_size=10,
            persistence_file=None
        )

        await manager.start()

        try:
            print("1. 队列管理器状态:")
            status = manager.get_queue_status()
            print(f"   总任务数: {status['total_tasks']}")
            print(f"   等待任务: {status['pending_tasks']}")
            print(f"   运行中任务: {status['running_tasks']}")
            print(f"   最大并发: {status['max_concurrent_tasks']}")
            print(f"   队列状态: {'运行中' if status['running'] else '已停止'}")

            # 提交不同优先级的任务
            print("\n2. 提交任务到队列:")

            # 低优先级任务
            async def background_task():
                await asyncio.sleep(1)
                return "后台任务完成"

            task_id1 = await manager.submit(
                task_type=LLMTaskType.GENERATE,
                func=background_task,
                priority=LLMTaskPriority.LOW,
                metadata={"category": "background"}
            )
            print(f"   提交低优先级任务: {task_id1[:8]}...")

            # 高优先级任务
            async def urgent_task():
                await asyncio.sleep(0.5)
                return "紧急任务完成"

            task_id2 = await manager.submit(
                task_type=LLMTaskType.EMBED,
                func=urgent_task,
                priority=LLMTaskPriority.URGENT,
                metadata={"category": "urgent"}
            )
            print(f"   提交高优先级任务: {task_id2[:8]}...")

            # 普通优先级任务
            async def normal_task(name):
                await asyncio.sleep(0.3)
                return f"普通任务{name}完成"

            task_id3 = await manager.submit(
                taskment_type=LLMTaskType.GENERATE,
                func=normal_task,
                args=("处理",),
                priority=LLMTaskPriority.NORMAL,
                metadata={"category": "normal"}
            )
            print(f"   提交普通优先级任务: {task_id3[:8]}...")

            # 获取任务结果
            print("\n3. 获取任务结果:")
            for i, task_id in enumerate([task_id1, task_id2, task_id3], 1):
                try:
                    result = await manager.get_result(task_id, timeout=10)
                    print(f"   任务{i}结果: {result}")
                except TimeoutError:
                    print(f"   任务{i}超时")
                except Exception as e:
                    print(f"   任务{i}错误: {e}")

            # 批量操作演示
            print("\n4. 批量操作演示:")
            async def batch_task(batch_id):
                await asyncio.sleep(0.2)
                return f"批量任务{batch_id}完成"

            batch_tasks = [
                {
                    "task_type": LLMTaskType.GENERATE,
                    "func": batch_task,
                    "args": (i,),
                    "priority": LLMTaskPriority.NORMAL
                }
                for i in range(3)
            ]

            batch_task_ids = await manager.batch_submit(batch_tasks)
            print(f"   批量提交 {len(batch_task_ids)} 个任务")

            batch_results = await manager.batch_get_results(batch_task_ids, timeout=5)
            print(f"   批量获取 {len(batch_results)} 个结果")

            for i, result in enumerate(batch_results):
                if isinstance(result, dict) and "error" in result:
                    print(f"   任务{i+1}错误: {result['error']}")
                else:
                    print(f"   任务{i+1}结果: {result}")

        finally:
            await manager.stop()
            print("\n   队列管理器已停止")

    except Exception as e:
        print(f"   错误: {e}")


async def demo_adaptive_controller():
    """演示自适应控制器"""
    print("\n=== 自适应控制器演示 ===")

    try:
        # 创建LLM客户端（用于获取统计）
        client = LocalLLMClient(
            base_url="http://localhost:11434",
            default_model="llama2",
            timeout=10.0
        )

        # 创建队列管理器
        queue_manager = LLMQueueManager(
            max_concurrent_tasks=3,
            max_queue_size=10,
            persistence_file=None
        )
        await queue_manager.start()

        # 创建控制器
        config = ControllerConfig(
            monitoring_interval=2.0,  # 2秒监控一次
            min_concurrent_tasks=1,
            max_concurrent_tasks=5,
            base_request_rate=1.0,
            enable_auto_scaling=True,
            cooldown_period=5.0,
            emergency_stop_enabled=True
        )

        controller = AdaptiveController(
            config=config,
            llm_client=client,
            queue_manager=queue_manager
        )

        await controller.start()

        try:
            print("1. 控制器初始状态:")
            status = controller.get_status()
            print(f"   运行状态: {status['running']}")
            print(f"   当前并发任务: {status['current_concurrent_tasks']}")
            print(f"   当前请求率: {status['current_request_rate']:.2f}/s")
            print(f"   系统状态: {status['system_state']}")

            print("\n2. 模拟系统负载变化...")

            # 模拟一些任务执行来触发控制器
            async def simulation_task(task_id):
                """模拟任务执行"""
                await asyncio.sleep(1)
                return f"任务{task_id}完成"

            # 提交一些任务来触发控制
            for i in range(5):
                await queue_manager.submit(
                    task_type=LLMTaskType.GENERATE,
                    func=simulation_task,
                    args=(i,),
                    priority=LLMTaskPriority.NORMAL
                )
                await asyncio.sleep(0.5)

            # 等待监控周期
            print("   监控系统运行 10 秒...")
            await asyncio.sleep(10)

            print("\n3. 控制器运行后状态:")
            status = controller.get_status()
            print(f"   运行状态: {status['running']}")
            print(f"   当前并发任务: {status['current_concurrent_tasks']}")
            print(f"   当前请求率: {status['current_request_rate']:.2f}/s")
            print(f"   系统状态: {status['system_state']}")
            print(f"   总控制动作: {status['stats']['total_control_actions']}")

            if status["stats"]["total_control_actions"] > 0:
                print(f"   最后动作: {status['last_control_action']}")

            # 显示指标历史
            history = controller.get_metrics_history(limit=5)
            if history:
                print(f"\n4. 最近的指标历史:")
                for i, snapshot in enumerate(history, 1):
                    print(f"   时间 {i}: {snapshot['timestamp']} - 状态: {snapshot['control_state']}")

        finally:
            await controller.stop()
            print("\n   自适应控制器已停止")
            await queue_manager.stop()

    except Exception as e:
        print(f"   错误: {e}")
        print("   注意: 自适应控制需要系统监控支持")


async def main():
    """主函数"""
    print("Atlas LLM 集成功能演示")
    print("=" * 50)
    print("此演示将展示 Atlas 系统的 LLM 集成功能")
    print("包括: Ollama客户端、语义去重、任务队列、自适应控制")
    print("注意: 某些功能需要 Ollama 服务在后台运行")
    print("=" * 50)

    try:
        # 选择要演示的功能
        print("\n请选择要演示的功能 (输入数字):")
        print("1. LLM 客户端功能")
        print("2. 语义去重功能")
        print("3. 任务队列管理")
        print("4. 自适应控制器")
        print("5. 运行所有演示")

        while True:
            try:
                choice = input("\n请输入选择 (1-5, 或 'q' 退出): ").strip()
                if choice == 'q':
                    print("\n演示结束")
                    break

                if choice == '1':
                    await demo_llm_client()
                elif choice == '2':
                    await demo_semantic_deduplication()
                elif choice == '3':
                    await demo_queue_manager()
                elif choice == '4':
                    await demo_adaptive_controller()
                elif choice == '5':
                    print("\n运行所有演示...")
                    await demo_llm_client()
                    await demo_semantic_deduplication()
                    await demo_queue_manager()
                    await demo_adaptive_controller()
                else:
                    print("无效选择，请输入 1-5 或 'q'")

            except KeyboardInterrupt:
                print("\n\n检测到中断，退出演示")
                break
            except Exception as e:
                print(f"\n输入错误: {e}")
                continue

    except KeyboardInterrupt:
        print("\n演示被用户中断")
    except Exception as e:
        print(f"\n演示过程中发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())