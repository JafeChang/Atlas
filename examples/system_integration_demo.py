#!/usr/bin/env python3
"""
Atlas 系统集成演示

展示完整的系统集成功能，包括：
- 性能监控
- 健康检查
- 错误处理
- 资源管理
- 任务调度
"""

import asyncio
import sys
import signal
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import StorageManager
from atlas.core.error_handler import ErrorHandler, get_global_error_handler, set_global_error_handler
from atlas.core.resource_manager import ResourceLimits, SystemResourceManager, get_global_resource_manager, set_global_resource_manager
from atlas.monitoring.performance import PerformanceMonitor, get_global_monitor, set_global_monitor
from atlas.monitoring.health import HealthChecker, HealthChecks, get_global_health_checker, set_global_health_checker
from atlas.scheduler.queue import TaskQueue
from atlas.scheduler.status import TaskStatusManager
from atlas.collectors.rss_collector import RSSCollector
from atlas.collectors.http_client import HTTPClient
from atlas.core.logging import get_logger

logger = get_logger(__name__)


class SystemIntegrationDemo:
    """系统集成演示类"""

    def __init__(self, config_dir: Path = None):
        """初始化演示

        Args:
            config_dir: 配置目录路径
        """
        self.config = get_config(config_dir)
        self.components = {}
        self.running = False

    async def initialize(self) -> None:
        """初始化系统组件"""
        logger.info("正在初始化系统集成演示...")

        # 确保必要目录存在
        self.config.ensure_directories()

        # 初始化数据库
        db_manager = AtlasDatabase(self.config.data_dir / "atlas.db")
        self.components["db_manager"] = db_manager

        # 初始化存储
        storage_manager = StorageManager(self.config.data_dir)
        self.components["storage_manager"] = storage_manager

        # 初始化错误处理器
        error_handler = ErrorHandler(
            error_log_file=self.config.log_dir / "errors.log",
            enable_retry=True,
            enable_classification=True
        )
        self.components["error_handler"] = error_handler
        set_global_error_handler(error_handler)

        # 初始化资源管理器
        resource_limits = ResourceLimits(
            max_memory_mb=1024.0,
            max_cpu_percent=80.0,
            max_concurrent_tasks=8,
            max_open_files=200,
            gc_threshold_mb=100.0
        )
        resource_manager = SystemResourceManager(resource_limits)
        await resource_manager.start()
        self.components["resource_manager"] = resource_manager
        set_global_resource_manager(resource_manager)

        # 初始化性能监控器
        performance_monitor = PerformanceMonitor(
            collect_interval=2.0,
            enable_auto_collection=True,
            metrics_file=self.config.log_dir / "metrics.jsonl"
        )
        await performance_monitor.start()
        self.components["performance_monitor"] = performance_monitor
        set_global_monitor(performance_monitor)

        # 初始化健康检查器
        health_checker = HealthChecker(check_interval=30.0)
        self.components["health_checker"] = health_checker
        set_global_health_checker(health_checker)

        # 初始化任务队列
        task_queue = TaskQueue(max_workers=4)
        await task_queue.start()
        self.components["task_queue"] = task_queue

        # 初始化任务状态管理器
        status_manager = TaskStatusManager(self.config.data_dir / "cache")
        self.components["status_manager"] = status_manager

        # 初始化HTTP客户端和RSS采集器
        http_client = HTTPClient(
            timeout=self.config.collection.request_timeout,
            max_concurrent=self.config.collection.max_concurrent_requests,
            cache_enabled=True
        )
        self.components["http_client"] = http_client

        rss_collector = RSSCollector(
            http_client=http_client,
            rate_limiter=None,  # 演示时不需要频率限制
            config=self.config.collection
        )
        self.components["rss_collector"] = rss_collector

        # 注册健康检查
        await self._register_health_checks()

        self.running = True
        logger.info("系统集成演示初始化完成")

    async def _register_health_checks(self) -> None:
        """注册健康检查"""
        health_checker = self.components["health_checker"]
        db_manager = self.components["db_manager"]
        storage_manager = self.components["storage_manager"]
        task_queue = self.components["task_queue"]
        http_client = self.components["http_client"]

        # 数据库健康检查
        health_checker.register_check(
            "database",
            lambda: asyncio.create_task(HealthChecks.database_health_check(db_manager))
        )

        # 存储健康检查
        health_checker.register_check(
            "storage",
            lambda: asyncio.create_task(HealthChecks.storage_health_check(storage_manager))
        )

        # 任务队列健康检查
        health_checker.register_check(
            "task_queue",
            lambda: asyncio.create_task(HealthChecks.task_queue_health_check(task_queue))
        )

        # HTTP客户端健康检查
        health_checker.register_check(
            "http_client",
            lambda: asyncio.create_task(HealthChecks.http_client_health_check(http_client))
        )

        # 系统资源健康检查
        health_checker.register_check(
            "system_resources",
            lambda: HealthChecks.system_resource_health_check()
        )

        # 启动健康检查监控
        await health_checker.start_monitoring()

    async def demonstrate_monitoring(self) -> None:
        """演示监控功能"""
        print("\n=== 性能监控演示 ===")

        performance_monitor = self.components["performance_monitor"]
        resource_manager = self.components["resource_manager"]

        # 等待一些指标收集
        print("等待收集性能指标...")
        await asyncio.sleep(5)

        # 显示当前性能状态
        status = performance_monitor.get_current_status()
        print(f"\n当前系统状态: {status['status']}")
        print(f"CPU使用率: {status['metrics']['cpu_percent']:.1f}%")
        print(f"内存使用率: {status['metrics']['memory_percent']:.1f}%")
        print(f"内存使用量: {status['metrics']['memory_used_mb']:.1f}MB")
        print(f"活跃线程数: {status['metrics']['active_threads']}")
        print(f"打开文件数: {status['metrics']['open_files']}")

        if status['issues']:
            print("\n⚠️ 发现的问题:")
            for issue in status['issues']:
                print(f"  - {issue}")

        # 显示资源管理状态
        resource_status = resource_manager.get_system_status()
        print(f"\n资源管理状态: {'运行中' if resource_status['running'] else '已停止'}")
        print(f"内存管理: {resource_status['managers']['memory']['current_memory_mb']:.1f}MB / {resource_status['limits']['max_memory_mb'] or '无限制'}MB")
        print(f"并发任务: {resource_status['managers']['concurrency']['active_tasks']} / {resource_status['limits']['max_concurrent_tasks']}")

    async def demonstrate_health_checking(self) -> None:
        """演示健康检查功能"""
        print("\n=== 健康检查演示 ===")

        health_checker = self.components["health_checker"]

        # 执行健康检查
        print("执行系统健康检查...")
        system_health = await health_checker.check_all_components()

        print(f"\n整体健康状态: {system_health.overall_status.value}")
        print(f"检查摘要: {system_health.summary}")
        print(f"检查耗时: {system_health.total_duration_ms:.1f}ms")

        print(f"\n组件健康状态:")
        for result in system_health.component_results:
            status_icon = {"healthy": "✅", "warning": "⚠️", "unhealthy": "❌", "unknown": "❓"}[result.status.value]
            print(f"  {status_icon} {result.component}: {result.message}")

            if result.details:
                for key, value in result.details.items():
                    if key not in ["response_time_ms", "test_url"]:
                        print(f"    - {key}: {value}")

    async def demonstrate_error_handling(self) -> None:
        """演示错误处理功能"""
        print("\n=== 错误处理演示 ===")

        error_handler = self.components["error_handler"]

        # 模拟各种错误
        test_errors = [
            (ValueError("测试验证错误"), "validation"),
            (ConnectionError("测试连接错误"), "network"),
            (FileNotFoundError("测试文件未找到"), "file_system"),
            (MemoryError("测试内存错误"), "memory"),
            (TimeoutError("测试超时错误"), "timeout")
        ]

        print("模拟各种错误类型...")
        for error, category in test_errors:
            error_info = await error_handler.handle_error(
                error,
                component="demo_component",
                operation="demo_operation",
                category_hint=category
            )
            print(f"  - {error_info.category.value}: {error_info.message}")

        # 显示错误统计
        stats = error_handler.get_error_statistics()
        print(f"\n错误统计:")
        print(f"  总错误数: {stats['total_errors']}")
        print(f"  已解决数: {stats['resolved_count']}")

        print(f"\n按分类统计:")
        for category, count in stats['by_category'].items():
            print(f"  - {category}: {count}")

        print(f"\n按严重程度统计:")
        for severity, count in stats['by_severity'].items():
            print(f"  - {severity}: {count}")

        # 显示最近的错误
        recent_errors = error_handler.get_recent_errors(hours=1)
        print(f"\n最近的错误 (最多5个):")
        for error in recent_errors[:5]:
            print(f"  - [{error.category.value}] {error.message}")
            print(f"    时间: {error.timestamp.strftime('%H:%M:%S')}")
            print(f"    组件: {error.component}")

    async def demonstrate_task_scheduling(self) -> None:
        """演示任务调度功能"""
        print("\n=== 任务调度演示 ===")

        task_queue = self.components["task_queue"]

        # 定义演示任务
        async def demo_cpu_task(task_id: int, duration: float):
            """CPU密集型任务"""
            import time
            start_time = time.time()
            while time.time() - start_time < duration:
                # 模拟CPU计算
                _ = sum(i * i for i in range(1000))
            return f"CPU任务 {task_id} 完成"

        async def demo_io_task(task_id: int, duration: float):
            """IO密集型任务"""
            await asyncio.sleep(duration)
            return f"IO任务 {task_id} 完成"

        async def demo_error_task(task_id: int):
            """错误任务"""
            if task_id % 3 == 0:
                raise ValueError(f"任务 {task_id} 模拟错误")
            return f"任务 {task_id} 成功完成"

        print("提交各种类型的任务...")

        task_ids = []

        # 提交CPU任务
        for i in range(3):
            task_id = await task_queue.submit(
                func=demo_cpu_task,
                args=(i, 0.5),
                priority=2
            )
            task_ids.append(("CPU任务", task_id))

        # 提交IO任务
        for i in range(3):
            task_id = await task_queue.submit(
                func=demo_io_task,
                args=(i, 0.3),
                priority=1
            )
            task_ids.append(("IO任务", task_id))

        # 提交错误任务
        for i in range(3):
            task_id = await task_queue.submit(
                func=demo_error_task,
                args=(i,),
                priority=3
            )
            task_ids.append(("错误任务", task_id))

        # 等待任务完成
        print("等待任务完成...")
        results = []
        for task_type, task_id in task_ids:
            try:
                result = await task_queue.get_result(task_id, timeout=10)
                results.append((task_type, "success", result))
            except Exception as e:
                results.append((task_type, "error", str(e)))

        # 显示结果
        print(f"\n任务执行结果:")
        success_count = 0
        error_count = 0

        for task_type, status, result in results:
            if status == "success":
                success_count += 1
                print(f"  ✅ {task_type}: {result}")
            else:
                error_count += 1
                print(f"  ❌ {task_type}: {result}")

        print(f"\n任务统计: 成功 {success_count}, 失败 {error_count}")

        # 显示队列状态
        queue_status = task_queue.get_queue_status()
        print(f"\n队列状态:")
        print(f"  总任务数: {queue_status['total_tasks']}")
        print(f"  等待任务: {queue_status['pending_tasks']}")
        print(f"  运行中任务: {queue_status['running_tasks']}")
        print(f"  最大并发: {queue_status['max_concurrent_tasks']}")

    async def demonstrate_data_collection(self) -> None:
        """演示数据采集功能"""
        print("\n=== 数据采集演示 ===")

        rss_collector = self.components["rss_collector"]
        storage_manager = self.components["storage_manager"]

        # 使用一些公开的RSS源进行演示
        test_feeds = [
            "https://feeds.feedburner.com/oreilly/radar",
            "https://rss.cnn.com/rss/edition.rss"
        ]

        print(f"开始从 {len(test_feeds)} 个RSS源采集数据...")

        collected_docs = []
        async for doc in rss_collector.collect_from_feeds(test_feeds):
            collected_docs.append(doc)
            print(f"  采集到: {doc.title[:50]}...")

            # 限制采集数量以加快演示速度
            if len(collected_docs) >= 5:
                break

        print(f"\n采集完成，共获得 {len(collected_docs)} 个文档")

        if collected_docs:
            # 保存采集的数据
            saved_count = 0
            for doc in collected_docs:
                try:
                    file_path = storage_manager.data_dir / "raw" / f"{doc.doc_id}.json"
                    await storage_manager.save_raw_content(file_path, doc.json())
                    saved_count += 1
                except Exception as e:
                    error_handler = self.components["error_handler"]
                    await error_handler.handle_error(
                        e, component="storage", operation="save_document"
                    )

            print(f"成功保存 {saved_count} 个文档")

            # 显示采集的文档信息
            print(f"\n文档信息示例:")
            for i, doc in enumerate(collected_docs[:2], 1):
                print(f"  文档 {i}:")
                print(f"    标题: {doc.title}")
                print(f"    来源: {doc.source_id}")
                print(f"    长度: {len(doc.content)} 字符")
                print(f"    提取时间: {doc.extracted_at.strftime('%H:%M:%S')}")

    async def demonstrate_system_integration(self) -> None:
        """演示系统集成功能"""
        print("\n=== 系统集成演示 ===")

        performance_monitor = self.components["performance_monitor"]
        resource_manager = self.components["resource_manager"]
        health_checker = self.components["health_checker"]

        print("执行综合系统测试...")

        # 记录开始状态
        start_health = await health_checker.check_all_components()
        start_status = performance_monitor.get_current_status()

        print(f"开始健康状态: {start_health.overall_status.value}")
        print(f"开始内存使用: {start_status['metrics']['memory_percent']:.1f}%")

        # 执行一些工作负载
        await self._execute_workload()

        # 等待系统稳定
        await asyncio.sleep(3)

        # 记录结束状态
        end_health = await health_checker.check_all_components()
        end_status = performance_monitor.get_current_status()

        print(f"结束健康状态: {end_health.overall_status.value}")
        print(f"结束内存使用: {end_status['metrics']['memory_percent']:.1f}%")

        # 显示组件性能统计
        print(f"\n组件性能统计:")
        perf_stats = performance_monitor.get_component_performance("demo_workload")
        if perf_stats["total_operations"] > 0:
            print(f"  demo_workload:")
            print(f"    总操作数: {perf_stats['total_operations']}")
            print(f"    成功率: {perf_stats['success_rate']:.1%}")
            print(f"    平均耗时: {perf_stats['avg_duration_ms']:.1f}ms")

        # 显示资源使用变化
        memory_change = end_status['metrics']['memory_percent'] - start_status['metrics']['memory_percent']
        print(f"\n内存使用变化: {memory_change:+.1f}%")

        # 验证系统仍然健康
        if end_health.overall_status.value in ["healthy", "warning"]:
            print("✅ 系统集成测试通过")
        else:
            print("⚠️ 系统集成测试发现问题")

    async def _execute_workload(self) -> None:
        """执行工作负载"""
        from atlas.monitoring.performance import monitor_performance

        @monitor_performance("demo_workload", "intensive_operation")
        async def intensive_operation():
            """资源密集型操作"""
            # 模拟计算密集型工作
            import time
            start_time = time.time()
            while time.time() - start_time < 1.0:
                _ = sum(i * i for i in range(10000))

            # 模拟IO操作
            await asyncio.sleep(0.5)

            return "operation completed"

        # 执行多个并发操作
        tasks = []
        for i in range(5):
            task = asyncio.create_task(intensive_operation())
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = sum(1 for r in results if not isinstance(r, Exception))
        print(f"工作负载完成: {success_count}/{len(results)} 操作成功")

    async def run_interactive_demo(self) -> None:
        """运行交互式演示"""
        print("🚀 Atlas 系统集成演示")
        print("=" * 50)
        print("此演示将展示完整的系统集成功能")
        print("包括: 性能监控、健康检查、错误处理、资源管理、任务调度")
        print("=" * 50)

        try:
            while self.running:
                print("\n请选择要演示的功能 (输入数字):")
                print("1. 性能监控")
                print("2. 健康检查")
                print("3. 错误处理")
                print("4. 任务调度")
                print("5. 数据采集")
                print("6. 系统集成")
                print("7. 运行所有演示")
                print("8. 查看系统状态")
                print("q. 退出")

                choice = input("\n请输入选择 (1-8, 或 'q' 退出): ").strip()

                if choice == 'q':
                    print("\n正在退出演示...")
                    break
                elif choice == '1':
                    await self.demonstrate_monitoring()
                elif choice == '2':
                    await self.demonstrate_health_checking()
                elif choice == '3':
                    await self.demonstrate_error_handling()
                elif choice == '4':
                    await self.demonstrate_task_scheduling()
                elif choice == '5':
                    await self.demonstrate_data_collection()
                elif choice == '6':
                    await self.demonstrate_system_integration()
                elif choice == '7':
                    print("\n运行所有演示...")
                    await self.demonstrate_monitoring()
                    await self.demonstrate_health_checking()
                    await self.demonstrate_error_handling()
                    await self.demonstrate_task_scheduling()
                    await self.demonstrate_data_collection()
                    await self.demonstrate_system_integration()
                elif choice == '8':
                    await self._show_system_status()
                else:
                    print("无效选择，请输入 1-8 或 'q'")

        except KeyboardInterrupt:
            print("\n\n检测到中断，退出演示")
        except Exception as e:
            print(f"\n演示过程中发生错误: {e}")
            error_handler = self.components.get("error_handler")
            if error_handler:
                await error_handler.handle_error(e, component="demo", operation="interactive")

    async def _show_system_status(self) -> None:
        """显示系统状态"""
        print("\n=== 系统状态概览 ===")

        # 性能监控状态
        performance_monitor = self.components["performance_monitor"]
        perf_status = performance_monitor.get_current_status()
        print(f"性能监控: {'运行中' if performance_monitor._running else '已停止'}")
        print(f"  状态: {perf_status['status']}")
        print(f"  CPU: {perf_status['metrics']['cpu_percent']:.1f}%")
        print(f"  内存: {perf_status['metrics']['memory_percent']:.1f}%")

        # 健康检查状态
        health_checker = self.components["health_checker"]
        last_health = health_checker.get_last_health()
        if last_health:
            print(f"\n健康检查: {'运行中' if health_checker._running else '已停止'}")
            print(f"  整体状态: {last_health.overall_status.value}")
            print(f"  组件数量: {len(last_health.component_results)}")
            print(f"  最后检查: {last_health.check_time.strftime('%H:%M:%S')}")

        # 资源管理状态
        resource_manager = self.components["resource_manager"]
        resource_status = resource_manager.get_system_status()
        print(f"\n资源管理: {'运行中' if resource_manager._running else '已停止'}")
        print(f"  内存管理: {resource_status['managers']['memory']['current_memory_mb']:.1f}MB")
        print(f"  并发任务: {resource_status['managers']['concurrency']['active_tasks']}")

        # 任务队列状态
        task_queue = self.components["task_queue"]
        queue_status = task_queue.get_queue_status()
        print(f"\n任务队列: {'运行中' if hasattr(task_queue, '_running') and task_queue._running else '已停止'}")
        print(f"  总任务数: {queue_status['total_tasks']}")
        print(f"  等待任务: {queue_status['pending_tasks']}")

        # 错误处理统计
        error_handler = self.components["error_handler"]
        error_stats = error_handler.get_error_statistics()
        print(f"\n错误处理:")
        print(f"  总错误数: {error_stats['total_errors']}")
        print(f"  已解决数: {error_stats['resolved_count']}")

    async def cleanup(self) -> None:
        """清理资源"""
        logger.info("正在清理系统资源...")

        self.running = False

        # 停止各个组件
        if "task_queue" in self.components:
            await self.components["task_queue"].stop()

        if "health_checker" in self.components:
            await self.components["health_checker"].stop_monitoring()

        if "performance_monitor" in self.components:
            await self.components["performance_monitor"].stop()

        if "resource_manager" in self.components:
            await self.components["resource_manager"].stop()

        if "http_client" in self.components:
            await self.components["http_client"].close()

        if "db_manager" in self.components:
            self.components["db_manager"].close()

        logger.info("系统资源清理完成")


async def main():
    """主函数"""
    demo = None

    def signal_handler(signum, frame):
        """信号处理器"""
        print(f"\n收到信号 {signum}，正在安全退出...")
        if demo:
            demo.running = False

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # 创建演示实例
        config_dir = Path(__file__).parent.parent / "config"
        demo = SystemIntegrationDemo(config_dir)

        # 初始化系统
        await demo.initialize()

        # 运行交互式演示
        await demo.run_interactive_demo()

    except Exception as e:
        logger.error(f"演示运行失败: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if demo:
            await demo.cleanup()


if __name__ == "__main__":
    asyncio.run(main())