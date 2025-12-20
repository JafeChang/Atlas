"""
Cron 任务管理器

提供 cron 表达式解析、任务调度和管理功能。
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Callable, Any, Set
from dataclasses import dataclass, field
from pathlib import Path
import json

from atlas.core.logging import get_logger
from .queue import TaskQueue, Task, TaskPriority
from .status import StatusManager, TaskState

logger = get_logger(__name__)


@dataclass
class CronJob:
    """Cron 任务定义"""
    name: str
    cron_expression: str
    func: Callable[..., Any]
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    max_retries: int = 3
    timeout: Optional[float] = None
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 运行时信息
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    def __post_init__(self) -> None:
        """初始化后处理"""
        if not self.description:
            self.description = f"Cron job: {self.name}"

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "cron_expression": self.cron_expression,
            "enabled": self.enabled,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "description": self.description,
            "metadata": self.metadata,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }


class CronExpression:
    """Cron 表达式解析器"""

    # 预定义的 cron 表达式
    PREDEFINED = {
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
        "@monthly": "0 0 1 * *",
        "@weekly": "0 0 * * 0",
        "@daily": "0 0 * * *",
        "@hourly": "0 * * * *",
        "@every_minute": "* * * * *",
    }

    def __init__(self, expression: str):
        """初始化 cron 表达式

        Args:
            expression: cron 表达式

        Raises:
            ValueError: 表达式格式错误
        """
        self.expression = expression.strip()
        self.original = expression

        # 处理预定义表达式
        if self.expression in self.PREDEFINED:
            self.expression = self.PREDEFINED[self.expression]

        # 解析表达式
        self._parse_expression()

    def _parse_expression(self) -> None:
        """解析 cron 表达式"""
        parts = self.expression.split()
        if len(parts) != 5:
            raise ValueError(f"无效的 cron 表达式: {self.expression}")

        self.minute = self._parse_field(parts[0], 0, 59)
        self.hour = self._parse_field(parts[1], 0, 23)
        self.day = self._parse_field(parts[2], 1, 31)
        self.month = self._parse_field(parts[3], 1, 12)
        self.day_of_week = self._parse_field(parts[4], 0, 6)

    def _parse_field(self, field: str, min_val: int, max_val: int) -> Set[int]:
        """解析 cron 字段

        Args:
            field: 字段值
            min_val: 最小值
            max_val: 最大值

        Returns:
            允许的值集合
        """
        if field == "*":
            return set(range(min_val, max_val + 1))

        values = set()

        # 处理逗号分隔的多个值
        for part in field.split(","):
            # 处理范围 (如 1-5)
            if "-" in part:
                start, end = part.split("-", 1)
                start = self._parse_value(start, min_val, max_val)
                end = self._parse_value(end, min_val, max_val)

                if start > end:
                    raise ValueError(f"无效范围: {part}")

                values.update(range(start, end + 1))
            # 处理步长 (如 */2 或 1-5/2)
            elif "/" in part:
                base, step = part.split("/", 1)
                step = int(step)

                if base == "*":
                    base_values = set(range(min_val, max_val + 1))
                elif "-" in base:
                    start, end = base.split("-", 1)
                    start = self._parse_value(start, min_val, max_val)
                    end = self._parse_value(end, min_val, max_val)
                    base_values = set(range(start, end + 1))
                else:
                    base_values = {self._parse_value(base, min_val, max_val)}

                values.update({v for v in base_values if (v - min_val) % step == 0})
            else:
                values.add(self._parse_value(part, min_val, max_val))

        # 验证值范围
        for value in values:
            if value < min_val or value > max_val:
                raise ValueError(f"值 {value} 超出范围 [{min_val}, {max_val}]")

        return values

    def _parse_value(self, value: str, min_val: int, max_val: int) -> int:
        """解析单个值

        Args:
            value: 值字符串
            min_val: 最小值
            max_val: 最大值

        Returns:
            整数值
        """
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"无效的数值: {value}")

    def next_run_time(self, after: Optional[datetime] = None) -> datetime:
        """计算下次运行时间

        Args:
            after: 基准时间，默认为当前时间

        Returns:
            下次运行时间
        """
        if after is None:
            after = datetime.now(timezone.utc)

        # 从下一分钟开始查找
        current = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # 最多查找一年（防止死循环）
        max_attempts = 365 * 24 * 60
        attempts = 0

        while attempts < max_attempts:
            if self._matches_time(current):
                return current

            current += timedelta(minutes=1)
            attempts += 1

        raise ValueError(f"无法找到下次运行时间: {self.expression}")

    def _matches_time(self, dt: datetime) -> bool:
        """检查时间是否匹配 cron 表达式

        Args:
            dt: 时间

        Returns:
            是否匹配
        """
        return (
            dt.minute in self.minute and
            dt.hour in self.hour and
            dt.day in self.day and
            dt.month in self.month and
            dt.weekday() in self.day_of_week
        )

    def __str__(self) -> str:
        """字符串表示"""
        return self.original


class CronManager:
    """Cron 任务管理器"""

    def __init__(self, task_queue: TaskQueue, status_manager: StatusManager,
                 config_file: Optional[Path] = None):
        """初始化 Cron 管理器

        Args:
            task_queue: 任务队列
            status_manager: 状态管理器
            config_file: 配置文件
        """
        self.task_queue = task_queue
        self.status_manager = status_manager
        self.config_file = config_file

        # Cron 任务
        self._jobs: Dict[str, CronJob] = {}
        self._job_lock = asyncio.Lock()

        # 调度器状态
        self._running = False
        self._scheduler_task: Optional[asyncio.Task] = None

        # 加载配置
        if config_file and config_file.exists():
            self._load_config()

        logger.info("Cron 管理器初始化完成")

    async def start(self) -> None:
        """启动 Cron 调度器"""
        if self._running:
            return

        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

        logger.info("Cron 调度器已启动")

    async def stop(self) -> None:
        """停止 Cron 调度器"""
        if not self._running:
            return

        self._running = False

        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

        logger.info("Cron 调度器已停止")

    async def add_job(self, name: str, cron_expression: str, func: Callable[..., Any],
                     args: tuple = (), kwargs: Optional[Dict[str, Any]] = None,
                     enabled: bool = True, max_retries: int = 3,
                     timeout: Optional[float] = None, description: str = "",
                     metadata: Optional[Dict[str, Any]] = None,
                     overwrite: bool = False) -> bool:
        """添加 Cron 任务

        Args:
            name: 任务名称
            cron_expression: cron 表达式
            func: 任务函数
            args: 位置参数
            kwargs: 关键字参数
            enabled: 是否启用
            max_retries: 最大重试次数
            timeout: 超时时间
            description: 任务描述
            metadata: 任务元数据
            overwrite: 是否覆盖已存在的任务

        Returns:
            是否添加成功
        """
        async with self._job_lock:
            if name in self._jobs and not overwrite:
                raise ValueError(f"任务已存在: {name}")

            # 验证 cron 表达式
            cron_expr = CronExpression(cron_expression)

            # 创建任务
            job = CronJob(
                name=name,
                cron_expression=cron_expression,
                func=func,
                args=args,
                kwargs=kwargs or {},
                enabled=enabled,
                max_retries=max_retries,
                timeout=timeout,
                description=description,
                metadata=metadata or {}
            )

            # 计算下次运行时间
            if enabled:
                job.next_run = cron_expr.next_run_time()

            self._jobs[name] = job

            logger.info(f"已添加 Cron 任务: {name} ({cron_expression})")
            return True

    async def remove_job(self, name: str) -> bool:
        """移除 Cron 任务

        Args:
            name: 任务名称

        Returns:
            是否移除成功
        """
        async with self._job_lock:
            if name not in self._jobs:
                return False

            del self._jobs[name]
            logger.info(f"已移除 Cron 任务: {name}")
            return True

    async def enable_job(self, name: str) -> bool:
        """启用 Cron 任务

        Args:
            name: 任务名称

        Returns:
            是否启用成功
        """
        async with self._job_lock:
            job = self._jobs.get(name)
            if not job:
                return False

            job.enabled = True
            job.next_run = CronExpression(job.cron_expression).next_run_time()
            logger.info(f"已启用 Cron 任务: {name}")
            return True

    async def disable_job(self, name: str) -> bool:
        """禁用 Cron 任务

        Args:
            name: 任务名称

        Returns:
            是否禁用成功
        """
        async with self._job_lock:
            job = self._jobs.get(name)
            if not job:
                return False

            job.enabled = False
            job.next_run = None
            logger.info(f"已禁用 Cron 任务: {name}")
            return True

    async def get_job(self, name: str) -> Optional[CronJob]:
        """获取 Cron 任务

        Args:
            name: 任务名称

        Returns:
            Cron 任务对象
        """
        async with self._job_lock:
            return self._jobs.get(name)

    async def get_all_jobs(self) -> List[CronJob]:
        """获取所有 Cron 任务

        Returns:
            Cron 任务列表
        """
        async with self._job_lock:
            return list(self._jobs.values())

    async def get_enabled_jobs(self) -> List[CronJob]:
        """获取启用的 Cron 任务

        Returns:
            启用的 Cron 任务列表
        """
        async with self._job_lock:
            return [job for job in self._jobs.values() if job.enabled]

    async def run_job_now(self, name: str) -> bool:
        """立即运行 Cron 任务

        Args:
            name: 任务名称

        Returns:
            是否提交成功
        """
        async with self._job_lock:
            job = self._jobs.get(name)
            if not job:
                return False

        # 创建异步任务函数
        async def async_func():
            if asyncio.iscoroutinefunction(job.func):
                return await job.func(*job.args, **job.kwargs)
            else:
                return job.func(*job.args, **job.kwargs)

        # 提交到任务队列
        task_id = await self.task_queue.submit(
            async_func,
            name=f"cron_{name}",
            priority=TaskPriority.NORMAL,
            max_retries=job.max_retries,
            timeout=job.timeout,
            metadata={"cron_job": name, "manual_run": True}
        )

        logger.info(f"已手动提交 Cron 任务: {name} (Task ID: {task_id})")
        return True

    async def get_job_status(self, name: str) -> Optional[Dict[str, Any]]:
        """获取 Cron 任务状态

        Args:
            name: 任务名称

        Returns:
            任务状态信息
        """
        async with self._job_lock:
            job = self._jobs.get(name)
            if not job:
                return None

            # 计算下次运行时间
            next_run = None
            if job.enabled:
                try:
                    next_run = CronExpression(job.cron_expression).next_run_time()
                except Exception as e:
                    logger.warning(f"计算下次运行时间失败: {name} - {e}")

            return {
                "name": job.name,
                "cron_expression": job.cron_expression,
                "enabled": job.enabled,
                "description": job.description,
                "last_run": job.last_run.isoformat() if job.last_run else None,
                "next_run": next_run.isoformat() if next_run else None,
                "run_count": job.run_count,
                "success_count": job.success_count,
                "failure_count": job.failure_count,
                "success_rate": (job.success_count / job.run_count * 100) if job.run_count > 0 else 0.0,
            }

    async def _scheduler_loop(self) -> None:
        """调度器主循环"""
        logger.info("Cron 调度器循环已启动")

        while self._running:
            try:
                await self._check_and_schedule_jobs()
                await asyncio.sleep(60)  # 每分钟检查一次
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cron 调度器循环异常: {e}")
                await asyncio.sleep(60)

        logger.info("Cron 调度器循环已停止")

    async def _check_and_schedule_jobs(self) -> None:
        """检查并调度任务"""
        now = datetime.now(timezone.utc)

        async with self._job_lock:
            for job in self._jobs.values():
                if not job.enabled:
                    continue

                # 检查是否到了运行时间
                if job.next_run and job.next_run <= now:
                    await self._schedule_job(job)

    async def _schedule_job(self, job: CronJob) -> None:
        """调度任务

        Args:
            job: Cron 任务
        """
        try:
            # 创建异步任务函数
            async def async_func():
                # 创建任务状态
                task_id = f"cron_{job.name}_{int(now.timestamp())}"
                self.status_manager.create_task(
                    task_id=task_id,
                    task_name=job.name,
                    priority="normal",
                    max_retries=job.max_retries,
                    metadata={"cron_job": job.name, "scheduled": True}
                )

                # 开始执行
                self.status_manager.start_task(task_id)

                try:
                    if asyncio.iscoroutinefunction(job.func):
                        result = await job.func(*job.args, **job.kwargs)
                    else:
                        result = job.func(*job.args, **job.kwargs)

                    # 执行成功
                    self.status_manager.complete_task(task_id, result)
                    job.success_count += 1

                except Exception as e:
                    # 执行失败
                    self.status_manager.fail_task(task_id, str(e))
                    job.failure_count += 1
                    raise

            # 提交到任务队列
            task_id = await self.task_queue.submit(
                async_func,
                name=f"cron_{job.name}",
                priority=TaskPriority.NORMAL,
                max_retries=job.max_retries,
                timeout=job.timeout,
                metadata={"cron_job": job.name, "scheduled_run": True}
            )

            # 更新任务信息
            job.last_run = datetime.now(timezone.utc)
            job.run_count += 1

            # 计算下次运行时间
            try:
                job.next_run = CronExpression(job.cron_expression).next_run_time(job.last_run)
            except Exception as e:
                logger.error(f"计算下次运行时间失败: {job.name} - {e}")
                job.enabled = False  # 禁用有问题的任务

            logger.info(f"已调度 Cron 任务: {job.name} (Task ID: {task_id})")

        except Exception as e:
            logger.error(f"调度 Cron 任务失败: {job.name} - {e}")
            job.failure_count += 1

    def _load_config(self) -> None:
        """加载配置文件"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 加载 cron 任务配置
            jobs_config = config.get("cron_jobs", {})
            for job_name, job_config in jobs_config.items():
                # 注意：这里只加载配置，函数需要在运行时动态注册
                logger.info(f"已加载 Cron 任务配置: {job_name}")

        except Exception as e:
            logger.error(f"加载 Cron 配置文件失败: {e}")

    def _save_config(self) -> None:
        """保存配置文件"""
        if not self.config_file:
            return

        try:
            config = {
                "cron_jobs": {
                    name: job.to_dict()
                    for name, job in self._jobs.items()
                }
            }

            # 确保目录存在
            self.config_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            logger.debug(f"Cron 配置已保存到: {self.config_file}")

        except Exception as e:
            logger.error(f"保存 Cron 配置文件失败: {e}")