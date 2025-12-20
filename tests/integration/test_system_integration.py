"""
系统集成测试

测试系统各组件的集成和协作，包括监控、错误处理、资源管理等。
"""

import asyncio
import pytest
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

from atlas.core.config import get_config
from atlas.core.database import DatabaseManager
from atlas.core.storage import StorageManager
from atlas.core.error_handler import ErrorHandler, ErrorCategory, ErrorSeverity
from atlas.core.resource_manager import ResourceLimits, SystemResourceManager
from atlas.monitoring.performance import PerformanceMonitor, get_global_monitor, set_global_monitor
from atlas.monitoring.health import HealthChecker, HealthChecks, get_global_health_checker, set_global_health_checker
from atlas.scheduler.queue import TaskQueue
from atlas.scheduler.status import TaskStatusManager

pytest_plugins = ["tests.utils"]


class TestSystemIntegration:
    """系统集成测试类"""

    @pytest.fixture
    async def test_environment(self):
        """创建测试环境"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            data_dir = Path(temp_dir) / "data"
            log_dir = Path(temp_dir) / "logs"

            # 创建目录
            for directory in [config_dir, data_dir, log_dir]:
                directory.mkdir(parents=True, exist_ok=True)

            # 获取配置
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
    async def system_components(self, test_environment):
        """初始化系统组件"""
        config = test_environment["config"]
        data_dir = test_environment["data_dir"]

        # 数据库管理器
        db_manager = DatabaseManager(config.database.url)
        await db_manager.initialize()

        # 存储管理器
        storage_manager = StorageManager(data_dir)

        # 错误处理器
        error_handler = ErrorHandler(
            error_log_file=data_dir / "errors.log",
            enable_retry=True,
            enable_classification=True
        )

        # 资源管理器
        resource_limits = ResourceLimits(
            max_memory_mb=512.0,
            max_cpu_percent=80.0,
            max_concurrent_tasks=4,
            max_open_files=100,
            gc_threshold_mb=50.0
        )
        resource_manager = SystemResourceManager(resource_limits)

        # 性能监控器
        performance_monitor = PerformanceMonitor(
            collect_interval=1.0,
            enable_auto_collection=True,
            metrics_file=data_dir / "metrics.jsonl"
        )

        # 健康检查器
        health_checker = HealthChecker(check_interval=30.0)

        # 任务队列
        task_queue = TaskQueue(max_workers=2)

        # 任务状态管理器
        status_manager = TaskStatusManager(storage_manager.cache_dir)

        # 启动组件
        await resource_manager.start()
        await performance_monitor.start()
        await health_checker.start_monitoring()

        # 注册健康检查
        health_checker.register_check(
            "database",
            lambda: asyncio.create_task(HealthChecks.database_health_check(db_manager))
        )

        health_checker.register_check(
            "storage",
            lambda: asyncio.create_task(HealthChecks.storage_health_check(storage_manager))
        )

        health_checker.register_check(
            "task_queue",
            lambda: asyncio.create_task(HealthChecks.task_queue_health_check(task_queue))
        )

        health_checker.register_check(
            "system_resources",
            lambda: HealthChecks.system_resource_health_check()
        )

        # 设置全局实例
        set_global_monitor(performance_monitor)
        set_global_health_checker(health_checker)

        yield {
            "db_manager": db_manager,
            "storage_manager": storage_manager,
            "error_handler": error_handler,
            "resource_manager": resource_manager,
            "performance_monitor": performance_monitor,
            "health_checker": health_checker,
            "task_queue": task_queue,
            "status_manager": status_manager
        }

        # 清理资源
        await resource_manager.stop()
        await performance_monitor.stop()
        await health_checker.stop_monitoring()
        await task_queue.stop()
        await db_manager.close()

    @pytest.mark.asyncio
    async def test_error_handling_integration(self, system_components):
        """测试错误处理集成"""
        components = system_components
        error_handler = components["error_handler"]

        # 模拟各种错误
        test_errors = [
            ValueError("Test validation error"),
            ConnectionError("Test connection error"),
            FileNotFoundError("Test file not found"),
            Exception("Test unknown error")
        ]

        error_infos = []
        for error in test_errors:
            error_info = await error_handler.handle_error(
                error,
                component="test_component",
                operation="test_operation"
            )
            error_infos.append(error_info)

        # 验证错误分类
        assert len(error_infos) == len(test_errors)

        validation_error = next(e for e in error_infos if "validation" in e.message.lower())
        assert validation_error.category == ErrorCategory.VALIDATION
        assert validation_error.severity == ErrorSeverity.LOW

        connection_error = next(e for e in error_infos if "connection" in e.message.lower())
        assert connection_error.category == ErrorCategory.NETWORK
        assert connection_error.severity == ErrorSeverity.MEDIUM

        file_error = next(e for e in error_infos if "not found" in e.message.lower())
        assert file_error.category == ErrorCategory.FILE_SYSTEM
        assert file_error.severity in [ErrorSeverity.HIGH, ErrorSeverity.MEDIUM]

        # 验证统计信息
        stats = error_handler.get_error_statistics()
        assert stats["total_errors"] == len(test_errors)
        assert len(stats["by_category"]) > 0
        assert len(stats["by_severity"]) > 0

        # 验证错误历史
        recent_errors = error_handler.get_recent_errors(hours=1)
        assert len(recent_errors) == len(test_errors)

    @pytest.mark.asyncio
    async def test_performance_monitoring_integration(self, system_components):
        """测试性能监控集成"""
        components = system_components
        performance_monitor = components["performance_monitor"]

        # 等待一些指标收集
        await asyncio.sleep(2)

        # 获取当前状态
        status = performance_monitor.get_current_status()
        assert "status" in status
        assert "metrics" in status
        assert "timestamp" in status

        # 记录一些组件操作
        from atlas.monitoring.performance import monitor_performance

        # 使用性能监控装饰器
        @monitor_performance("test_component", "test_operation")
        async def test_operation():
            await asyncio.sleep(0.1)
            return "success"

        result = await test_operation()
        assert result == "success"

        # 获取组件性能统计
        perf_stats = performance_monitor.get_component_performance("test_component")
        assert "component" in perf_stats
        assert perf_stats["component"] == "test_component"

    @pytest.mark.asyncio
    async def test_health_checking_integration(self, system_components):
        """测试健康检查集成"""
        components = system_components
        health_checker = components["health_checker"]

        # 等待一次健康检查
        await asyncio.sleep(2)

        # 执行健康检查
        system_health = await health_checker.check_all_components()
        assert system_health is not None
        assert hasattr(system_health, 'overall_status')
        assert hasattr(system_health, 'component_results')
        assert hasattr(system_health, 'summary')

        # 验证健康检查结果
        assert len(system_health.component_results) > 0

        # 检查各个组件的健康状态
        for result in system_health.component_results:
            assert result.component is not None
            assert result.status is not None
            assert result.message is not None
            assert result.check_time is not None

        # 验证整体健康状态
        assert system_health.overall_status in ["healthy", "warning", "unhealthy", "unknown"]

    @pytest.mark.asyncio
    async def test_resource_management_integration(self, system_components):
        """测试资源管理集成"""
        components = system_components
        resource_manager = components["resource_manager"]

        # 获取系统状态
        status = resource_manager.get_system_status()
        assert "current_usage" in status
        assert "limits" in status
        assert "managers" in status
        assert status["running"] is True

        # 测试托管操作
        operation_id = "test_operation"

        async def test_operation():
            await asyncio.sleep(0.1)
            return "operation completed"

        async with resource_manager.managed_operation(operation_id, memory_mb=10) as acquired:
            assert acquired is True
            result = await test_operation()
            assert result == "operation completed"

        # 验证资源统计
        memory_stats = resource_manager.memory_manager.get_usage_stats()
        assert "current_memory_mb" in memory_stats
        assert "active_allocations" in memory_stats

        concurrency_stats = resource_manager.concurrency_manager.get_usage_stats()
        assert "active_tasks" in concurrency_stats
        assert "available_slots" in concurrency_stats

    @pytest.mark.asyncio
    async def test_monitoring_alerts_integration(self, system_components):
        """测试监控告警集成"""
        from atlas.monitoring.alerts import AlertManager, AlertLevel, AlertRule

        # 创建告警管理器
        alert_manager = AlertManager()

        # 注册告警规则
        cpu_rule = AlertRule(
            name="high_cpu_usage",
            condition=lambda metrics: metrics.get("cpu_percent", 0) > 50,
            level=AlertLevel.WARNING,
            message="CPU使用率过高"
        )

        memory_rule = AlertRule(
            name="high_memory_usage",
            condition=lambda metrics: metrics.get("memory_percent", 0) > 80,
            level=AlertLevel.CRITICAL,
            message="内存使用率过高"
        )

        alert_manager.register_rule(cpu_rule)
        alert_manager.register_rule(memory_rule)

        # 获取当前指标
        performance_monitor = components["performance_monitor"]
        current_metrics = performance_monitor.get_current_status()

        # 检查告警
        alerts = alert_manager.check_alerts(current_metrics.get("metrics", {}))

        # 验证告警结果
        assert isinstance(alerts, list)

        # 清理
        await alert_manager.stop()

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self, system_components):
        """测试端到端工作流"""
        components = system_components

        # 模拟完整的数据处理工作流
        workflow_results = []

        async def data_collection_task():
            """数据采集任务"""
            await asyncio.sleep(0.1)
            return {"status": "success", "items_collected": 10}

        async def data_processing_task(items_count):
            """数据处理任务"""
            await asyncio.sleep(0.2)
            return {"status": "success", "items_processed": items_count}

        async def data_storage_task(items_count):
            """数据存储任务"""
            await asyncio.sleep(0.1)
            return {"status": "success", "items_stored": items_count}

        # 使用任务队列执行工作流
        collection_task = await components["task_queue"].submit(
            func=data_collection_task,
            priority=1
        )

        # 等待采集完成
        collection_result = await components["task_queue"].get_result(collection_task, timeout=10)
        workflow_results.append(("collection", collection_result))

        if collection_result.get("status") == "success":
            items_count = collection_result.get("items_collected", 0)

            # 提交处理任务
            processing_task = await components["task_queue"].submit(
                func=data_processing_task,
                args=(items_count,),
                priority=2
            )

            # 等待处理完成
            processing_result = await components["task_queue"].get_result(processing_task, timeout=10)
            workflow_results.append(("processing", processing_result))

            if processing_result.get("status") == "success":
                processed_items = processing_result.get("items_processed", 0)

                # 提交存储任务
                storage_task = await components["task_queue"].submit(
                    func=data_storage_task,
                    args=(processed_items,),
                    priority=3
                )

                # 等待存储完成
                storage_result = await components["task_queue"].get_result(storage_task, timeout=10)
                workflow_results.append(("storage", storage_result))

        # 验证工作流结果
        assert len(workflow_results) == 3

        collection_status = next(result for stage, result in workflow_results if stage == "collection")
        assert collection_status["status"] == "success"
        assert collection_status["items_collected"] == 10

        processing_status = next(result for stage, result in workflow_results if stage == "processing")
        assert processing_status["status"] == "success"
        assert processing_status["items_processed"] == 10

        storage_status = next(result for stage, result in workflow_results if stage == "storage")
        assert storage_status["status"] == "success"
        assert storage_status["items_stored"] == 10

    @pytest.mark.asyncio
    async def test_system_resilience(self, system_components):
        """测试系统韧性"""
        components = system_components
        error_handler = components["error_handler"]

        # 模拟系统压力测试
        stress_tasks = []

        async def stress_task(task_id: int):
            """压力测试任务"""
            try:
                # 模拟各种可能的错误
                import random
                error_chance = random.random()

                if error_chance < 0.1:  # 10% 概率发生网络错误
                    raise ConnectionError(f"Network error in task {task_id}")
                elif error_chance < 0.2:  # 10% 概率发生超时
                    await asyncio.sleep(2)  # 模拟超时
                    raise TimeoutError(f"Timeout in task {task_id}")
                else:
                    # 正常执行
                    await asyncio.sleep(0.1)
                    return {"task_id": task_id, "status": "success"}

            except Exception as e:
                # 记录错误但不抛出，测试错误处理
                await error_handler.handle_error(
                    e,
                    component="stress_test",
                    operation=f"stress_task_{task_id}"
                )
                return {"task_id": task_id, "status": "error", "error": str(e)}

        # 启动多个压力测试任务
        for i in range(20):
            task = asyncio.create_task(stress_task(i))
            stress_tasks.append(task)

        # 等待所有任务完成
        results = await asyncio.gather(*stress_tasks, return_exceptions=True)

        # 分析结果
        success_count = 0
        error_count = 0

        for result in results:
            if isinstance(result, dict):
                if result.get("status") == "success":
                    success_count += 1
                elif result.get("status") == "error":
                    error_count += 1

        total_tasks = success_count + error_count
        success_rate = success_count / total_tasks if total_tasks > 0 else 0

        # 验证系统韧性（至少应该有70%的任务成功）
        assert success_rate >= 0.7, f"系统韧性测试失败，成功率只有: {success_rate:.1%}"

        # 验证错误处理
        error_stats = error_handler.get_error_statistics()
        assert error_stats["total_errors"] > 0, "应该有错误被处理"

        # 验证系统仍然健康
        health_checker = components["health_checker"]
        system_health = await health_checker.check_all_components()
        assert system_health.overall_status in ["healthy", "warning"], "系统应该仍然保持健康状态"

    @pytest.mark.asyncio
    async def test_component_coordination(self, system_components):
        """测试组件协调"""
        components = system_components
        performance_monitor = components["performance_monitor"]
        health_checker = components["health_checker"]
        resource_manager = components["resource_manager"]

        # 测试组件之间的协调
        coordination_results = {}

        # 1. 性能监控和资源管理协调
        async def resource_intensive_task():
            """资源密集型任务"""
            async with resource_manager.managed_operation("intensive_task", memory_mb=50) as acquired:
                if acquired:
                    await asyncio.sleep(0.5)
                    return "success"
                return "failed"

        # 记录开始时的性能指标
        start_status = performance_monitor.get_current_status()
        coordination_results["start_memory"] = start_status.get("metrics", {}).get("memory_percent", 0)

        # 执行资源密集型任务
        result = await resource_intensive_task()
        assert result == "success"

        # 记录结束时的性能指标
        end_status = performance_monitor.get_current_status()
        coordination_results["end_memory"] = end_status.get("metrics", {}).get("memory_percent", 0)

        # 2. 健康检查和错误处理协调
        # 模拟一个会导致健康检查失败的情况
        error_handler = components["error_handler"]

        # 记录一个严重错误
        await error_handler.handle_error(
            MemoryError("Simulated memory error"),
            component="test_component",
            operation="test_operation"
        )

        # 等待健康检查检测到问题
        await asyncio.sleep(1)
        system_health = await health_checker.check_all_components()
        coordination_results["health_status"] = system_health.overall_status

        # 3. 验证协调结果
        assert coordination_results["start_memory"] >= 0
        assert coordination_results["end_memory"] >= 0
        assert coordination_results["health_status"] in ["healthy", "warning", "unhealthy"]

        # 4. 验证组件状态
        assert performance_monitor._running is True
        assert health_checker._running is True
        assert resource_manager._running is True

        # 验证各组件的统计信息
        perf_stats = performance_monitor.get_component_performance("intensive_task")
        assert perf_stats["component"] == "intensive_task"

        resource_status = resource_manager.get_system_status()
        assert resource_status["running"] is True


if __name__ == "__main__":
    pytest.main([__file__])