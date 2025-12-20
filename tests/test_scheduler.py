"""
任务调度系统测试

测试任务队列、cron 管理器和状态管理器的功能。
"""

import asyncio
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest

from atlas.scheduler import (
    TaskQueue, Task, TaskPriority, TaskStatus as QueueTaskStatus,
    CronManager, CronExpression, CronJob,
    StatusManager, TaskState
)
from atlas.core.logging import get_logger

logger = get_logger(__name__)


class TestTaskQueue:
    """任务队列测试"""

    @pytest.fixture
    async def task_queue(self):
        """创建测试用的任务队列"""
        async with TaskQueue(max_workers=2) as queue:
            yield queue

    @pytest.mark.asyncio
    async def test_submit_and_execute_task(self, task_queue):
        """测试任务提交和执行"""
        # 创建测试任务
        async def test_task(x, y):
            await asyncio.sleep(0.1)  # 模拟异步操作
            return x + y

        # 提交任务
        task_id = await task_queue.submit(test_task, 2, 3, name="test_add")
        assert task_id is not None

        # 等待任务完成
        result = await task_queue.get_task_result(task_id, timeout=5)
        assert result == 5

    @pytest.mark.asyncio
    async def test_priority_ordering(self, task_queue):
        """测试任务优先级排序"""
        results = []

        async def task_func(value):
            results.append(value)
            return value

        # 提交不同优先级的任务
        await task_queue.submit(task_func, "low", priority=TaskPriority.LOW)
        await task_queue.submit(task_func, "urgent", priority=TaskPriority.URGENT)
        await task_queue.submit(task_func, "normal", priority=TaskPriority.NORMAL)
        await task_queue.submit(task_func, "high", priority=TaskPriority.HIGH)

        # 等待所有任务完成
        await asyncio.sleep(1)

        # 验证执行顺序（urgent -> high -> normal -> low）
        expected_order = ["urgent", "high", "normal", "low"]
        assert results == expected_order

    @pytest.mark.asyncio
    async def test_task_retry(self):
        """测试任务重试机制"""
        attempt_count = 0

        async def failing_task():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise ValueError(f"Attempt {attempt_count} failed")
            return "success"

        async with TaskQueue(max_workers=1) as queue:
            task_id = await queue.submit(failing_task, max_retries=3)
            result = await queue.get_task_result(task_id, timeout=10)

        assert result == "success"
        assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_task_timeout(self):
        """测试任务超时"""
        async def slow_task():
            await asyncio.sleep(2)  # 超过超时时间
            return "should_not_reach"

        async with TaskQueue(max_workers=1) as queue:
            task_id = await queue.submit(slow_task, timeout=0.5)

            with pytest.raises(TimeoutError):
                await queue.get_task_result(task_id, timeout=2)

    @pytest.mark.asyncio
    async def test_cancel_task(self, task_queue):
        """测试任务取消"""
        async def long_task():
            await asyncio.sleep(10)
            return "should_not_reach"

        task_id = await task_queue.submit(long_task)
        success = task_queue.cancel_task(task_id)
        assert success is True

    def test_queue_status(self, task_queue):
        """测试队列状态"""
        status = task_queue.get_queue_status()
        assert "queue_size" in status
        assert "running_tasks" in status
        assert "max_workers" in status
        assert "stopped" in status
        assert status["max_workers"] == 2


class TestStatusManager:
    """状态管理器测试"""

    @pytest.fixture
    def status_manager(self):
        """创建测试用的状态管理器"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
            status_file = Path(f.name)
        yield StatusManager(status_file)
        # 清理
        if status_file.exists():
            status_file.unlink()

    def test_create_and_get_task(self, status_manager):
        """测试创建和获取任务"""
        task = status_manager.create_task(
            task_id="test_task",
            task_name="测试任务",
            priority="normal",
            max_retries=3,
            metadata={"test": True}
        )

        assert task.task_id == "test_task"
        assert task.task_name == "测试任务"
        assert task.state == TaskState.PENDING
        assert task.priority == "normal"
        assert task.max_retries == 3
        assert task.metadata["test"] is True

        # 获取任务
        retrieved_task = status_manager.get_task("test_task")
        assert retrieved_task is not None
        assert retrieved_task.task_id == "test_task"

    def test_task_lifecycle(self, status_manager):
        """测试任务生命周期"""
        # 创建任务
        status_manager.create_task("lifecycle_task", "生命周期测试")

        # 开始任务
        success = status_manager.start_task("lifecycle_task")
        assert success is True

        task = status_manager.get_task("lifecycle_task")
        assert task.state == TaskState.RUNNING
        assert task.started_at is not None

        # 完成任务
        success = status_manager.complete_task("lifecycle_task", "test_result")
        assert success is True

        task = status_manager.get_task("lifecycle_task")
        assert task.state == TaskState.SUCCESS
        assert task.result == "test_result"
        assert task.completed_at is not None
        assert task.execution_time > 0

    def test_task_failure(self, status_manager):
        """测试任务失败"""
        status_manager.create_task("fail_task", "失败测试")
        status_manager.start_task("fail_task")

        success = status_manager.fail_task("fail_task", "Test error", "Traceback")
        assert success is True

        task = status_manager.get_task("fail_task")
        assert task.state == TaskState.FAILED
        assert task.error_message == "Test error"
        assert task.error_traceback == "Traceback"

    def test_task_retry(self, status_manager):
        """测试任务重试"""
        status_manager.create_task("retry_task", "重试测试", max_retries=3)
        status_manager.start_task("retry_task")
        status_manager.fail_task("retry_task", "First failure")

        # 第一次重试
        success = status_manager.retry_task("retry_task")
        assert success is True

        task = status_manager.get_task("retry_task")
        assert task.state == TaskState.RETRYING
        assert task.retry_count == 1
        assert task.started_at is None  # 重置开始时间
        assert task.completed_at is None  # 重置完成时间

        # 测试重试次数用完
        for i in range(2, 4):
            status_manager.start_task("retry_task")
            status_manager.fail_task("retry_task", f"Failure {i}")
            if i < 3:
                status_manager.retry_task("retry_task")

        # 重试次数用完，无法再次重试
        success = status_manager.retry_task("retry_task")
        assert success is False

    def test_get_tasks_by_state(self, status_manager):
        """测试根据状态获取任务"""
        # 创建不同状态的任务
        status_manager.create_task("pending1", "Pending 1")
        status_manager.create_task("pending2", "Pending 2")

        status_manager.create_task("running1", "Running 1")
        status_manager.start_task("running1")

        status_manager.create_task("success1", "Success 1")
        status_manager.start_task("success1")
        status_manager.complete_task("success1")

        # 测试获取任务
        pending_tasks = status_manager.get_tasks_by_state(TaskState.PENDING)
        running_tasks = status_manager.get_tasks_by_state(TaskState.RUNNING)
        success_tasks = status_manager.get_tasks_by_state(TaskState.SUCCESS)

        assert len(pending_tasks) == 2
        assert len(running_tasks) == 1
        assert len(success_tasks) == 1

    def test_metrics(self, status_manager):
        """测试任务指标"""
        # 创建各种状态的任务
        for i in range(3):
            task_id = f"success_{i}"
            status_manager.create_task(task_id, f"Success {i}")
            status_manager.start_task(task_id)
            status_manager.complete_task(task_id)

        for i in range(2):
            task_id = f"failed_{i}"
            status_manager.create_task(task_id, f"Failed {i}")
            status_manager.start_task(task_id)
            status_manager.fail_task(task_id, "Error")

        status_manager.create_task("pending1", "Pending 1")
        status_manager.create_task("running1", "Running 1")
        status_manager.start_task("running1")

        metrics = status_manager.get_metrics()
        assert metrics.total_tasks == 6
        assert metrics.success_tasks == 3
        assert metrics.failed_tasks == 2
        assert metrics.pending_tasks == 1
        assert metrics.running_tasks == 1
        assert metrics.success_rate == 60.0  # 3/5 * 100

    def test_persistence(self, status_manager):
        """测试状态持久化"""
        # 创建一些任务
        status_manager.create_task("persist_task", "持久化测试")
        status_manager.start_task("persist_task")
        status_manager.complete_task("persist_task", "persist_result")

        # 保存状态
        status_manager.save_status()

        # 创建新的状态管理器，加载持久化状态
        new_manager = StatusManager(status_manager.persistence_file)
        task = new_manager.get_task("persist_task")

        assert task is not None
        assert task.task_name == "持久化测试"
        assert task.state == TaskState.SUCCESS
        assert task.result == "persist_result"


class TestCronExpression:
    """Cron 表达式测试"""

    def test_basic_expressions(self):
        """测试基本 cron 表达式"""
        # 每分钟
        cron = CronExpression("* * * * *")
        now = datetime.now(timezone.utc)
        next_run = cron.next_run_time(now)
        assert next_run > now
        assert next_run - now < timedelta(minutes=1)

        # 每小时
        cron = CronExpression("0 * * * *")
        now = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
        next_run = cron.next_run_time(now)
        assert next_run.hour == (now.hour + 1) % 24
        assert next_run.minute == 0

    def test_predefined_expressions(self):
        """测试预定义表达式"""
        # 每小时
        cron = CronExpression("@hourly")
        assert str(cron) == "@hourly"
        assert cron.minute == {0}
        assert cron.hour == set(range(24))

        # 每天
        cron = CronExpression("@daily")
        assert cron.minute == {0}
        assert cron.hour == {0}

        # 每周
        cron = CronExpression("@weekly")
        assert cron.minute == {0}
        assert cron.hour == {0}
        assert cron.day_of_week == {0}  # Sunday

    def test_range_and_step(self):
        """测试范围和步长"""
        # 1-5小时的每30分钟
        cron = CronExpression("*/30 1-5 * * *")
        assert cron.minute == {0, 30}
        assert cron.hour == {1, 2, 3, 4, 5}

        # 周一到周五的9-17点
        cron = CronExpression("0 9-17 * * 1-5")
        assert cron.minute == {0}
        assert cron.hour == set(range(9, 18))
        assert cron.day_of_week == {1, 2, 3, 4, 5}

    def test_invalid_expressions(self):
        """测试无效表达式"""
        with pytest.raises(ValueError):
            CronExpression("invalid")

        with pytest.raises(ValueError):
            CronExpression("60 * * * *")  # 无效分钟

        with pytest.raises(ValueError):
            CronExpression("0 24 * * *")  # 无效小时

    def test_matches_time(self):
        """测试时间匹配"""
        cron = CronExpression("30 14 * * 1")  # 每周一14:30

        # 匹配时间
        matching_time = datetime(2024, 1, 8, 14, 30)  # Monday
        assert cron._matches_time(matching_time)

        # 不匹配时间
        non_matching_time = datetime(2024, 1, 8, 14, 31)  # Monday 14:31
        assert not cron._matches_time(non_matching_time)

        non_matching_day = datetime(2024, 1, 9, 14, 30)  # Tuesday 14:30
        assert not cron._matches_time(non_matching_day)


class TestCronManager:
    """Cron 管理器测试"""

    @pytest.fixture
    async def cron_manager(self):
        """创建测试用的 Cron 管理器"""
        async with TaskQueue(max_workers=1) as queue:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
                config_file = Path(f.name)

            with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as f:
                status_file = Path(f.name)

            status_manager = StatusManager(status_file)
            manager = CronManager(queue, status_manager, config_file)
            await manager.start()
            yield manager
            await manager.stop()

            # 清理
            if config_file.exists():
                config_file.unlink()
            if status_file.exists():
                status_file.unlink()

    @pytest.mark.asyncio
    async def test_add_and_get_job(self, cron_manager):
        """测试添加和获取 Cron 任务"""
        # 测试函数
        call_count = 0

        async def test_func():
            nonlocal call_count
            call_count += 1
            return f"Call {call_count}"

        # 添加任务
        success = await cron_manager.add_job(
            name="test_job",
            cron_expression="* * * * *",  # 每分钟
            func=test_func,
            description="测试任务"
        )
        assert success is True

        # 获取任务
        job = await cron_manager.get_job("test_job")
        assert job is not None
        assert job.name == "test_job"
        assert job.cron_expression == "* * * * *"
        assert job.enabled is True
        assert job.description == "测试任务"

    @pytest.mark.asyncio
    async def test_enable_disable_job(self, cron_manager):
        """测试启用/禁用任务"""
        async def dummy_func():
            pass

        await cron_manager.add_job("toggle_job", "* * * * *", dummy_func)

        # 禁用任务
        success = await cron_manager.disable_job("toggle_job")
        assert success is True

        job = await cron_manager.get_job("toggle_job")
        assert job.enabled is False
        assert job.next_run is None

        # 重新启用
        success = await cron_manager.enable_job("toggle_job")
        assert success is True

        job = await cron_manager.get_job("toggle_job")
        assert job.enabled is True
        assert job.next_run is not None

    @pytest.mark.asyncio
    async def test_remove_job(self, cron_manager):
        """测试移除任务"""
        async def dummy_func():
            pass

        await cron_manager.add_job("remove_job", "* * * * *", dummy_func)

        # 确认任务存在
        job = await cron_manager.get_job("remove_job")
        assert job is not None

        # 移除任务
        success = await cron_manager.remove_job("remove_job")
        assert success is True

        # 确认任务不存在
        job = await cron_manager.get_job("remove_job")
        assert job is None

    @pytest.mark.asyncio
    async def test_run_job_now(self, cron_manager):
        """测试立即运行任务"""
        call_count = 0

        async def test_func():
            nonlocal call_count
            call_count += 1
            return f"Immediate call {call_count}"

        await cron_manager.add_job("immediate_job", "0 0 * * *", test_func)  # 每天午夜

        # 立即运行
        success = await cron_manager.run_job_now("immediate_job")
        assert success is True

        # 等待任务完成
        await asyncio.sleep(0.5)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_job_status(self, cron_manager):
        """测试任务状态"""
        async def dummy_func():
            pass

        await cron_manager.add_job("status_job", "* * * * *", dummy_func)

        status = await cron_manager.get_job_status("status_job")
        assert status is not None
        assert status["name"] == "status_job"
        assert status["enabled"] is True
        assert status["run_count"] == 0
        assert status["success_count"] == 0
        assert status["failure_count"] == 0
        assert status["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_duplicate_job(self, cron_manager):
        """测试重复任务"""
        async def dummy_func():
            pass

        # 添加第一个任务
        await cron_manager.add_job("duplicate_test", "* * * * *", dummy_func)

        # 尝试添加同名任务，应该失败
        with pytest.raises(ValueError):
            await cron_manager.add_job("duplicate_test", "* * * * *", dummy_func)

        # 使用 overwrite 参数，应该成功
        success = await cron_manager.add_job(
            "duplicate_test",
            "*/2 * * * *",
            dummy_func,
            overwrite=True
        )
        assert success is True

        job = await cron_manager.get_job("duplicate_test")
        assert job.cron_expression == "*/2 * * * *"

    @pytest.mark.asyncio
    async def test_get_all_jobs(self, cron_manager):
        """测试获取所有任务"""
        async def dummy_func():
            pass

        # 添加多个任务
        await cron_manager.add_job("job1", "* * * * *", dummy_func)
        await cron_manager.add_job("job2", "*/2 * * * *", dummy_func)
        await cron_manager.add_job("job3", "0 * * * *", dummy_func)

        all_jobs = await cron_manager.get_all_jobs()
        assert len(all_jobs) == 3

        job_names = {job.name for job in all_jobs}
        assert job_names == {"job1", "job2", "job3"}

        enabled_jobs = await cron_manager.get_enabled_jobs()
        assert len(enabled_jobs) == 3


class TestIntegration:
    """集成测试"""

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self):
        """端到端工作流测试"""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # 创建状态管理器
            status_file = temp_path / "status.json"
            status_manager = StatusManager(status_file)

            # 创建任务队列
            queue_file = temp_path / "queue.pkl"
            async with TaskQueue(max_workers=2, persistence_file=queue_file) as queue:
                # 创建 Cron 管理器
                config_file = temp_path / "cron.json"
                cron_manager = CronManager(queue, status_manager, config_file)
                await cron_manager.start()

                try:
                    # 定义测试任务
                    results = []

                    async def data_collection_task():
                        await asyncio.sleep(0.1)
                        results.append("data_collected")
                        return {"items": 5, "sources": 2}

                    async def cleanup_task():
                        await asyncio.sleep(0.05)
                        results.append("cleanup_done")
                        return {"cleaned": 3}

                    # 添加 Cron 任务
                    await cron_manager.add_job(
                        "data_collection",
                        "* * * * *",  # 每分钟（用于测试）
                        data_collection_task,
                        description="数据采集任务"
                    )

                    await cron_manager.add_job(
                        "system_cleanup",
                        "* * * * *",  # 每分钟（用于测试）
                        cleanup_task,
                        description="系统清理任务"
                    )

                    # 手动运行任务
                    await cron_manager.run_job_now("data_collection")
                    await cron_manager.run_job_now("system_cleanup")

                    # 等待任务完成
                    await asyncio.sleep(1)

                    # 验证结果
                    assert len(results) == 2
                    assert "data_collected" in results
                    assert "cleanup_done" in results

                    # 检查任务状态
                    data_job = await cron_manager.get_job("data_collection")
                    cleanup_job = await cron_manager.get_job("system_cleanup")

                    assert data_job.run_count == 1
                    assert data_job.success_count == 1
                    assert cleanup_job.run_count == 1
                    assert cleanup_job.success_count == 1

                    # 检查状态管理器
                    metrics = status_manager.get_metrics()
                    assert metrics.total_tasks >= 2
                    assert metrics.success_tasks >= 2

                finally:
                    await cron_manager.stop()

                # 测试持久化
                status_manager.save_status()

                # 重新加载状态管理器
                new_status_manager = StatusManager(status_file)
                new_metrics = new_status_manager.get_metrics()
                assert new_metrics.total_tasks == metrics.total_tasks
                assert new_metrics.success_tasks == metrics.success_tasks


if __name__ == "__main__":
    # 运行测试
    pytest.main([__file__, "-v"])