"""
压力测试和韧性测试

测试系统在高负载和异常情况下的表现。
"""

import asyncio
import pytest
import tempfile
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

from atlas.core.config import get_config
from atlas.core.database import AtlasDatabase
from atlas.core.storage import FileStorageManager
from atlas.core.error_handler import ErrorHandler
from atlas.core.resource_manager import ResourceLimits, SystemResourceManager
from atlas.monitoring.performance import PerformanceMonitor
from atlas.monitoring.health import HealthChecker
from atlas.monitoring.alerts import AlertManager, AlertRules
from atlas.scheduler.queue import TaskQueue

pytest_plugins = ["tests.utils"]


class TestStressAndResilience:
    """压力测试和韧性测试类"""

    @pytest.fixture
    async def test_environment(self):
        """创建压力测试环境"""
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
    async def resilient_system(self, test_environment):
        """创建韧性测试系统"""
        config = test_environment["config"]
        data_dir = test_environment["data_dir"]

        # 使用较小的资源限制进行压力测试
        resource_limits = ResourceLimits(
            max_memory_mb=256.0,  # 较小的内存限制
            max_cpu_percent=70.0,
            max_concurrent_tasks=4,
            max_open_files=50,
            gc_threshold_mb=30.0
        )

        # 初始化组件
        db_manager = AtlasDatabase(data_dir / "test.db")

        storage_manager = FileStorageManager(data_dir)

        error_handler = ErrorHandler(
            error_log_file=data_dir / "errors.log",
            enable_retry=True,
            enable_classification=True
        )

        resource_manager = SystemResourceManager(resource_limits)
        await resource_manager.start()

        performance_monitor = PerformanceMonitor(
            collect_interval=0.5,  # 更频繁的收集
            enable_auto_collection=True
        )
        await performance_monitor.start()

        health_checker = HealthChecker(check_interval=10.0)

        alert_manager = AlertManager(check_interval=5.0)
        # 注册告警规则
        alert_manager.register_rule(AlertRules.high_memory_usage(threshold=60.0))
        alert_manager.register_rule(AlertRules.high_cpu_usage(threshold=50.0))

        task_queue = TaskQueue(max_workers=2)  # 较少的工作线程
        await task_queue.start()

        await health_checker.start_monitoring()
        await alert_manager.start()

        yield {
            "db_manager": db_manager,
            "storage_manager": storage_manager,
            "error_handler": error_handler,
            "resource_manager": resource_manager,
            "performance_monitor": performance_monitor,
            "health_checker": health_checker,
            "alert_manager": alert_manager,
            "task_queue": task_queue
        }

        # 清理
        await alert_manager.stop()
        await health_checker.stop_monitoring()
        await task_queue.stop()
        await performance_monitor.stop()
        await resource_manager.stop()
        db_manager.close()

    @pytest.mark.asyncio
    async def test_high_load_scenario(self, resilient_system):
        """测试高负载场景"""
        components = resilient_system

        # 高负载参数
        concurrent_tasks = 20
        task_duration = 2.0
        memory_per_task = 10  # MB

        async def resource_intensive_task(task_id: int):
            """资源密集型任务"""
            import random

            # 获取资源
            acquired = await components["resource_manager"].memory_manager.acquire(
                f"task_{task_id}", memory_per_task
            )

            if not acquired:
                return {"task_id": task_id, "status": "resource_denied"}

            try:
                # 模拟CPU密集型工作
                start_time = time.time()
                while time.time() - start_time < task_duration:
                    # CPU密集计算
                    _ = sum(i * i for i in range(1000))
                    await asyncio.sleep(0.01)

                # 模拟内存分配
                memory_data = ['x'] * (memory_per_task * 1024 * 1024 // 8)  # 分配内存

                # 随机失败
                if random.random() < 0.1:  # 10% 失败率
                    raise RuntimeError(f"Task {task_id} random failure")

                return {"task_id": task_id, "status": "success", "memory_size": len(memory_data)}

            except Exception as e:
                # 记录错误
                await components["error_handler"].handle_error(
                    e, component="stress_test", operation=f"task_{task_id}"
                )
                return {"task_id": task_id, "status": "error", "error": str(e)}

            finally:
                await components["resource_manager"].memory_manager.release(f"task_{task_id}", None)

        # 执行高负载测试
        print(f"启动 {concurrent_tasks} 个并发任务...")
        start_time = time.time()

        tasks = [
            asyncio.create_task(resource_intensive_task(i))
            for i in range(concurrent_tasks)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        end_time = time.time()
        total_time = end_time - start_time

        # 分析结果
        success_count = 0
        error_count = 0
        resource_denied_count = 0

        for result in results:
            if isinstance(result, dict):
                if result.get("status") == "success":
                    success_count += 1
                elif result.get("status") == "error":
                    error_count += 1
                elif result.get("status") == "resource_denied":
                    resource_denied_count += 1

        # 验证系统韧性
        total_processed = success_count + error_count + resource_denied_count
        success_rate = success_count / total_processed if total_processed > 0 else 0

        print(f"高负载测试结果:")
        print(f"  总任务数: {total_processed}")
        print(f"  成功任务: {success_count}")
        print(f"  错误任务: {error_count}")
        print(f"  资源拒绝: {resource_denied_count}")
        print(f"  成功率: {success_rate:.1%}")
        print(f"  总耗时: {total_time:.1f}秒")
        print(f"  平均每任务: {total_time/concurrent_tasks:.2f}秒")

        # 验证系统没有崩溃
        assert total_processed > 0, "应该有任务被处理"
        assert success_rate >= 0.3, f"成功率太低: {success_rate:.1%}"

        # 验证错误处理
        error_stats = components["error_handler"].get_error_statistics()
        assert error_stats["total_errors"] > 0, "应该有错误被处理"

        # 验证系统健康
        await asyncio.sleep(2)  # 等待系统稳定
        system_health = await components["health_checker"].check_all_components()
        assert system_health.overall_status.value in ["healthy", "warning"], "系统应该仍然健康"

    @pytest.mark.asyncio
    async def test_memory_pressure_resilience(self, resilient_system):
        """测试内存压力下的韧性"""
        components = resilient_system

        # 内存压力测试
        memory_allocations = []

        async def allocate_memory(size_mb: int, duration: float):
            """分配内存并保持一段时间"""
            try:
                # 分配内存
                memory_data = ['x'] * (size_mb * 1024 * 1024 // 8)
                memory_allocations.append(memory_data)

                # 保持内存分配
                await asyncio.sleep(duration)

                return {"status": "success", "size_mb": size_mb}

            except MemoryError:
                return {"status": "memory_error", "size_mb": size_mb}
            except Exception as e:
                await components["error_handler"].handle_error(
                    e, component="memory_test", operation="allocate_memory"
                )
                return {"status": "error", "size_mb": size_mb, "error": str(e)}

        # 记录开始时的内存状态
        start_status = components["performance_monitor"].get_current_status()
        start_memory = start_status["metrics"]["memory_percent"]

        print(f"开始内存使用率: {start_memory:.1f}%")

        # 逐步增加内存压力
        memory_tasks = []
        allocation_sizes = [20, 30, 40, 50, 60]  # MB

        for size in allocation_sizes:
            task = asyncio.create_task(allocate_memory(size, 1.0))
            memory_tasks.append(task)

            # 等待一下让系统稳定
            await asyncio.sleep(0.5)

            # 检查当前状态
            current_status = components["performance_monitor"].get_current_status()
            current_memory = current_status["metrics"]["memory_percent"]

            print(f"分配 {size}MB 后内存使用率: {current_memory:.1f}%")

            # 如果内存使用率过高，停止分配
            if current_memory > 85:
                print("内存使用率过高，停止分配")
                break

        # 等待所有内存任务完成
        results = await asyncio.gather(*memory_tasks, return_exceptions=True)

        # 清理内存
        memory_allocations.clear()

        # 强制垃圾回收
        import gc
        gc.collect()

        # 等待内存回收
        await asyncio.sleep(2)

        # 检查最终状态
        end_status = components["performance_monitor"].get_current_status()
        end_memory = end_status["metrics"]["memory_percent"]

        print(f"结束内存使用率: {end_memory:.1f}%")

        # 分析结果
        success_allocations = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "success")
        total_allocations = len([r for r in results if isinstance(r, dict)])

        print(f"内存分配结果: {success_allocations}/{total_allocations} 成功")

        # 验证系统韧性
        assert success_allocations > 0, "应该有内存分配成功"
        assert end_memory < 90, f"内存使用率应该得到控制，当前: {end_memory:.1f}%"

    @pytest.mark.asyncio
    async def test_concurrent_stress(self, resilient_system):
        """测试并发压力"""
        components = resilient_system

        # 并发压力测试参数
        concurrent_workers = 10
        operations_per_worker = 20
        operation_delay = 0.1

        async def worker_task(worker_id: int):
            """工作任务"""
            results = []

            for op_id in range(operations_per_worker):
                try:
                    # 模拟不同类型的操作
                    operation_type = op_id % 4

                    if operation_type == 0:
                        # CPU密集型操作
                        start_time = time.time()
                        while time.time() - start_time < operation_delay:
                            _ = sum(i * i for i in range(100))
                        result = {"type": "cpu", "status": "success"}

                    elif operation_type == 1:
                        # IO等待操作
                        await asyncio.sleep(operation_delay)
                        result = {"type": "io", "status": "success"}

                    elif operation_type == 2:
                        # 内存操作
                        data = ['x'] * 10000
                        await asyncio.sleep(operation_delay / 2)
                        result = {"type": "memory", "status": "success", "size": len(data)}

                    else:
                        # 混合操作
                        _ = sum(i * i for i in range(100))
                        await asyncio.sleep(operation_delay / 2)
                        data = ['x'] * 5000
                        result = {"type": "mixed", "status": "success"}

                    results.append(result)

                except Exception as e:
                    await components["error_handler"].handle_error(
                        e, component="concurrent_test", operation=f"worker_{worker_id}_op_{op_id}"
                    )
                    results.append({"type": "error", "status": "error", "error": str(e)})

            return {
                "worker_id": worker_id,
                "total_operations": len(results),
                "success_operations": len([r for r in results if r.get("status") == "success"]),
                "operations": results
            }

        # 执行并发压力测试
        print(f"启动 {concurrent_workers} 个并发工作进程...")
        start_time = time.time()

        worker_tasks = [
            asyncio.create_task(worker_task(i))
            for i in range(concurrent_workers)
        ]

        worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)

        end_time = time.time()
        total_time = end_time - start_time

        # 分析结果
        total_operations = 0
        total_success = 0
        operation_types = {}

        for result in worker_results:
            if isinstance(result, dict):
                total_operations += result["total_operations"]
                total_success += result["success_operations"]

                for op in result.get("operations", []):
                    op_type = op.get("type", "unknown")
                    if op_type not in operation_types:
                        operation_types[op_type] = {"total": 0, "success": 0}
                    operation_types[op_type]["total"] += 1
                    if op.get("status") == "success":
                        operation_types[op_type]["success"] += 1

        success_rate = total_success / total_operations if total_operations > 0 else 0
        throughput = total_operations / total_time

        print(f"并发压力测试结果:")
        print(f"  总操作数: {total_operations}")
        print(f"  成功操作: {total_success}")
        print(f"  成功率: {success_rate:.1%}")
        print(f"  总耗时: {total_time:.1f}秒")
        print(f"  吞吐量: {throughput:.1f} ops/sec")

        print(f"\n按操作类型统计:")
        for op_type, stats in operation_types.items():
            op_success_rate = stats["success"] / stats["total"] if stats["total"] > 0 else 0
            print(f"  {op_type}: {stats['success']}/{stats['total']} ({op_success_rate:.1%})")

        # 验证系统性能
        assert success_rate >= 0.8, f"成功率太低: {success_rate:.1%}"
        assert throughput >= 10, f"吞吐量太低: {throughput:.1f} ops/sec"

    @pytest.mark.asyncio
    async def test_error_recovery_resilience(self, resilient_system):
        """测试错误恢复韧性"""
        components = resilient_system

        # 错误注入测试
        error_scenarios = [
            {"type": "network_error", "rate": 0.3},
            {"type": "timeout_error", "rate": 0.2},
            {"type": "memory_error", "rate": 0.1},
            {"type": "validation_error", "rate": 0.2},
            {"type": "unknown_error", "rate": 0.1}
        ]

        async def error_prone_task(scenario: Dict[str, Any], task_id: int):
            """易错任务"""
            import random

            # 根据概率注入错误
            if random.random() < scenario["rate"]:
                error_type = scenario["type"]

                if error_type == "network_error":
                    raise ConnectionError(f"Network error in task {task_id}")
                elif error_type == "timeout_error":
                    await asyncio.sleep(5)  # 模拟超时
                    raise TimeoutError(f"Timeout in task {task_id}")
                elif error_type == "memory_error":
                    # 分配大量内存
                    try:
                        data = ['x'] * (100 * 1024 * 1024)  # 100MB
                        return {"status": "success", "task_id": task_id}
                    except MemoryError:
                        raise MemoryError(f"Memory error in task {task_id}")
                elif error_type == "validation_error":
                    raise ValueError(f"Validation error in task {task_id}")
                else:
                    raise RuntimeError(f"Unknown error in task {task_id}")

            # 正常执行
            await asyncio.sleep(0.1)
            return {"status": "success", "task_id": task_id}

        # 执行错误恢复测试
        print("执行错误恢复韧性测试...")

        total_tasks = 0
        successful_tasks = 0
        failed_tasks = 0

        for scenario in error_scenarios:
            print(f"\n测试场景: {scenario['type']} (错误率: {scenario['rate']:.1%})")

            scenario_tasks = []
            scenario_results = []

            # 为每个场景创建任务
            for i in range(20):
                task = asyncio.create_task(error_prone_task(scenario, i))
                scenario_tasks.append(task)

            # 等待任务完成
            results = await asyncio.gather(*scenario_tasks, return_exceptions=True)

            # 统计结果
            scenario_success = 0
            scenario_failed = 0

            for result in results:
                if isinstance(result, dict) and result.get("status") == "success":
                    scenario_success += 1
                else:
                    scenario_failed += 1
                    # 错误应该被错误处理器处理
                    if isinstance(result, Exception):
                        await components["error_handler"].handle_error(
                            result, component="error_recovery", operation="scenario_test"
                        )

            scenario_total = scenario_success + scenario_failed
            scenario_success_rate = scenario_success / scenario_total if scenario_total > 0 else 0

            print(f"  成功: {scenario_success}, 失败: {scenario_failed}, 成功率: {scenario_success_rate:.1%}")

            total_tasks += scenario_total
            successful_tasks += scenario_success
            failed_tasks += scenario_failed

            scenario_results.append({
                "scenario": scenario["type"],
                "total": scenario_total,
                "success": scenario_success,
                "failed": scenario_failed,
                "success_rate": scenario_success_rate
            })

        # 总体统计
        overall_success_rate = successful_tasks / total_tasks if total_tasks > 0 else 0

        print(f"\n总体错误恢复测试结果:")
        print(f"  总任务数: {total_tasks}")
        print(f"  成功任务: {successful_tasks}")
        print(f"  失败任务: {failed_tasks}")
        print(f"  总体成功率: {overall_success_rate:.1%}")

        # 验证错误处理
        error_stats = components["error_handler"].get_error_statistics()
        print(f"  错误处理统计: {error_stats['total_errors']} 个错误被处理")

        # 验证系统韧性
        assert overall_success_rate >= 0.4, f"错误恢复能力太低: {overall_success_rate:.1%}"
        assert error_stats["total_errors"] > 0, "应该有错误被处理"

        # 验证系统仍然健康
        await asyncio.sleep(1)
        system_health = await components["health_checker"].check_all_components()
        assert system_health.overall_status.value != "unhealthy", "系统不应该处于不健康状态"

    @pytest.mark.asyncio
    async def test_resource_exhaustion_resilience(self, resilient_system):
        """测试资源耗尽韧性"""
        components = resilient_system

        # 资源耗尽测试
        resource_exhaustion_tasks = []

        async def consume_resources(resource_type: str, amount: int):
            """消耗资源"""
            try:
                if resource_type == "memory":
                    # 消耗内存
                    data = ['x'] * (amount * 1024 * 1024 // 8)
                    await asyncio.sleep(1.0)
                    return {"status": "success", "resource": "memory", "amount": amount}

                elif resource_type == "cpu":
                    # 消耗CPU
                    start_time = time.time()
                    while time.time() - start_time < 1.0:
                        _ = sum(i * i for i in range(amount))
                    return {"status": "success", "resource": "cpu", "amount": amount}

                elif resource_type == "tasks":
                    # 消耗任务槽位
                    tasks = []
                    for i in range(amount):
                        task = asyncio.create_task(asyncio.sleep(0.5))
                        tasks.append(task)

                    await asyncio.gather(*tasks, return_exceptions=True)
                    return {"status": "success", "resource": "tasks", "amount": amount}

                else:
                    return {"status": "error", "resource": resource_type, "message": "Unknown resource type"}

            except Exception as e:
                await components["error_handler"].handle_error(
                    e, component="resource_test", operation=f"consume_{resource_type}"
                )
                return {"status": "error", "resource": resource_type, "error": str(e)}

        # 测试各种资源耗尽场景
        test_scenarios = [
            {"resource": "memory", "amount": 30, "count": 5},    # 5个任务各分配30MB
            {"resource": "cpu", "amount": 1000, "count": 8},     # 8个CPU密集任务
            {"resource": "tasks", "amount": 2, "count": 6},      # 6个任务各创建2个子任务
        ]

        for scenario in test_scenarios:
            print(f"\n测试资源耗尽: {scenario['resource']} (每次 {scenario['amount']} * {scenario['count']} 次)")

            scenario_results = []
            tasks = []

            for i in range(scenario["count"]):
                task = asyncio.create_task(consume_resources(
                    scenario["resource"], scenario["amount"]
                ))
                tasks.append(task)

            # 等待任务完成
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统计结果
            success_count = 0
            error_count = 0

            for result in results:
                if isinstance(result, dict) and result.get("status") == "success":
                    success_count += 1
                else:
                    error_count += 1

            total_count = success_count + error_count
            success_rate = success_count / total_count if total_count > 0 else 0

            print(f"  成功: {success_count}, 失败: {error_count}, 成功率: {success_rate:.1%}")

            # 检查系统状态
            current_status = components["performance_monitor"].get_current_status()
            print(f"  当前CPU: {current_status['metrics']['cpu_percent']:.1f}%")
            print(f"  当前内存: {current_status['metrics']['memory_percent']:.1f}%")

            # 等待系统恢复
            await asyncio.sleep(2)

        # 验证系统韧性
        print(f"\n资源耗尽韧性测试完成")

        # 系统应该仍然可用
        final_health = await components["health_checker"].check_all_components()
        assert final_health.overall_status.value in ["healthy", "warning"], "系统应该仍然可用"

        # 验证错误处理统计
        error_stats = components["error_handler"].get_error_statistics()
        print(f"错误处理统计: {error_stats['total_errors']} 个错误被正确处理")


if __name__ == "__main__":
    pytest.main([__file__])