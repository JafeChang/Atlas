"""
任务队列模块

提供简单的任务队列实现，支持优先级、异步执行和任务持久化。
"""

import asyncio
import threading
import time
import uuid
from enum import Enum
from typing import Dict, List, Optional, Callable, Any, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import pickle
import heapq

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class TaskPriority(Enum):
    """任务优先级"""
    LOW = 3
    NORMAL = 2
    HIGH = 1
    URGENT = 0


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    RETRYING = "retrying"


@dataclass
class Task:
    """任务对象"""
    func: Callable[..., Awaitable[Any]]
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    priority: TaskPriority = TaskPriority.NORMAL
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    max_retries: int = 3
    timeout: Optional[float] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """初始化后处理"""
        if not self.name:
            self.name = f"{self.func.__name__}_{self.task_id[:8]}"

    def __lt__(self, other: 'Task') -> bool:
        """优先级比较，用于堆排序"""
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        # 相同优先级按创建时间排序
        return self.created_at < other.created_at


class TaskQueue:
    """任务队列"""

    def __init__(self, max_workers: int = 5, persistence_file: Optional[Path] = None):
        """初始化任务队列

        Args:
            max_workers: 最大工作线程数
            persistence_file: 任务持久化文件
        """
        self.max_workers = max_workers
        self.persistence_file = persistence_file

        # 任务队列（使用堆实现优先级队列）
        self._queue: List[Task] = []
        self._queue_lock = threading.Lock()

        # 运行中的任务
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._running_lock = threading.Lock()

        # 事件循环
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_tasks: List[asyncio.Task] = []

        # 队列状态
        self._stopped = False
        self._worker_semaphore = asyncio.Semaphore(max_workers)

        # 加载持久化任务
        if persistence_file and persistence_file.exists():
            self._load_tasks()

        logger.info(f"任务队列初始化完成，最大工作线程数: {max_workers}")

    async def start(self) -> None:
        """启动任务队列"""
        if self._loop is None:
            self._loop = asyncio.get_event_loop()

        if self._stopped:
            self._stopped = False

        # 启动工作线程
        for i in range(self.max_workers):
            worker_task = self._loop.create_task(self._worker(f"worker-{i}"))
            self._worker_tasks.append(worker_task)

        logger.info(f"任务队列已启动，启动了 {self.max_workers} 个工作线程")

    async def stop(self, timeout: float = 30.0) -> None:
        """停止任务队列

        Args:
            timeout: 停止超时时间（秒）
        """
        logger.info("正在停止任务队列...")
        self._stopped = True

        # 等待运行中的任务完成
        start_time = time.time()
        while self._running_tasks and (time.time() - start_time) < timeout:
            await asyncio.sleep(0.1)

        # 取消所有工作线程
        for worker_task in self._worker_tasks:
            worker_task.cancel()

        # 等待工作线程结束
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)

        self._worker_tasks.clear()

        # 取消运行中的任务
        with self._running_lock:
            for task_id, task in self._running_tasks.items():
                task.cancel()
            self._running_tasks.clear()

        logger.info("任务队列已停止")

    async def submit(self, func: Callable[..., Awaitable[Any]], *args,
                    priority: TaskPriority = TaskPriority.NORMAL,
                    name: str = "", max_retries: int = 3,
                    timeout: Optional[float] = None,
                    metadata: Optional[Dict[str, Any]] = None,
                    **kwargs) -> str:
        """提交任务到队列

        Args:
            func: 异步函数
            *args: 函数位置参数
            priority: 任务优先级
            name: 任务名称
            max_retries: 最大重试次数
            timeout: 任务超时时间
            metadata: 任务元数据
            **kwargs: 函数关键字参数

        Returns:
            任务ID
        """
        task = Task(
            func=func,
            args=args,
            kwargs=kwargs,
            priority=priority,
            name=name,
            max_retries=max_retries,
            timeout=timeout,
            metadata=metadata or {}
        )

        with self._queue_lock:
            heapq.heappush(self._queue, task)

        logger.debug(f"任务已提交到队列: {task.name} (ID: {task.task_id})")
        return task.task_id

    async def get_task_result(self, task_id: str, timeout: Optional[float] = None) -> Any:
        """获取任务结果

        Args:
            task_id: 任务ID
            timeout: 等待超时时间

        Returns:
            任务结果

        Raises:
            TimeoutError: 等待超时
            KeyError: 任务不存在
        """
        start_time = time.time()
        while True:
            with self._running_lock:
                if task_id in self._running_tasks:
                    task = self._running_tasks[task_id]
                    try:
                        return await asyncio.wait_for(task, timeout=1.0)
                    except asyncio.TimeoutError:
                        if timeout and (time.time() - start_time) > timeout:
                            raise TimeoutError(f"等待任务结果超时: {task_id}")
                        continue
                else:
                    # 检查任务是否已完成
                    with self._queue_lock:
                        for task in self._queue:
                            if task.task_id == task_id:
                                raise ValueError(f"任务仍在队列中等待: {task_id}")

            raise KeyError(f"任务不存在: {task_id}")

    def cancel_task(self, task_id: str) -> bool:
        """取消任务

        Args:
            task_id: 任务ID

        Returns:
            是否取消成功
        """
        # 尝试从队列中移除
        with self._queue_lock:
            for i, task in enumerate(self._queue):
                if task.task_id == task_id:
                    del self._queue[i]
                    heapq.heapify(self._queue)
                    logger.info(f"已取消队列中的任务: {task_id}")
                    return True

        # 尝试取消运行中的任务
        with self._running_lock:
            if task_id in self._running_tasks:
                task = self._running_tasks[task_id]
                task.cancel()
                logger.info(f"已取消运行中的任务: {task_id}")
                return True

        return False

    def get_queue_size(self) -> int:
        """获取队列大小"""
        with self._queue_lock:
            return len(self._queue)

    def get_running_tasks(self) -> int:
        """获取运行中的任务数"""
        with self._running_lock:
            return len(self._running_tasks)

    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        with self._queue_lock, self._running_lock:
            return {
                "queue_size": len(self._queue),
                "running_tasks": len(self._running_tasks),
                "max_workers": self.max_workers,
                "stopped": self._stopped,
                "pending_tasks_by_priority": {
                    priority.name: sum(1 for task in self._queue if task.priority == priority)
                    for priority in TaskPriority
                }
            }

    async def _worker(self, worker_name: str) -> None:
        """工作线程

        Args:
            worker_name: 工作线程名称
        """
        logger.debug(f"工作线程启动: {worker_name}")

        while not self._stopped:
            try:
                # 获取任务
                task = await self._get_next_task()
                if task is None:
                    await asyncio.sleep(0.1)
                    continue

                # 执行任务
                await self._execute_task(task, worker_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"工作线程异常: {worker_name} - {e}")
                await asyncio.sleep(1.0)

        logger.debug(f"工作线程停止: {worker_name}")

    async def _get_next_task(self) -> Optional[Task]:
        """获取下一个任务"""
        with self._queue_lock:
            if self._queue:
                return heapq.heappop(self._queue)
            return None

    async def _execute_task(self, task: Task, worker_name: str) -> None:
        """执行任务

        Args:
            task: 任务对象
            worker_name: 工作线程名称
        """
        async with self._worker_semaphore:
            task_coro = self._run_task_with_timeout(task)
            execution_task = asyncio.create_task(task_coro, name=f"{worker_name}-{task.name}")

            with self._running_lock:
                self._running_tasks[task.task_id] = execution_task

            try:
                logger.debug(f"开始执行任务: {task.name} (ID: {task.task_id}) - {worker_name}")
                await execution_task
                logger.debug(f"任务执行完成: {task.name} (ID: {task.task_id})")
            except asyncio.CancelledError:
                logger.info(f"任务被取消: {task.name} (ID: {task.task_id})")
            except Exception as e:
                logger.error(f"任务执行失败: {task.name} (ID: {task.task_id}) - {e}")
            finally:
                with self._running_lock:
                    self._running_tasks.pop(task.task_id, None)

    async def _run_task_with_timeout(self, task: Task) -> None:
        """运行任务（带超时）

        Args:
            task: 任务对象
        """
        retry_count = 0
        last_error = None

        while retry_count <= task.max_retries:
            try:
                if task.timeout:
                    await asyncio.wait_for(task.func(*task.args, **task.kwargs), timeout=task.timeout)
                else:
                    await task.func(*task.args, **task.kwargs)

                # 任务成功
                return

            except asyncio.TimeoutError:
                last_error = "任务执行超时"
                logger.warning(f"任务超时: {task.name} (第{retry_count + 1}次尝试)")
            except Exception as e:
                last_error = str(e)
                logger.warning(f"任务失败: {task.name} (第{retry_count + 1}次尝试) - {e}")

            retry_count += 1
            if retry_count <= task.max_retries:
                # 等待重试
                wait_time = min(2 ** retry_count, 30)  # 指数退避，最大30秒
                await asyncio.sleep(wait_time)

        # 任务重试次数用完，抛出最后一个错误
        raise RuntimeError(f"任务重试次数已达上限: {task.name} - {last_error}")

    def _load_tasks(self) -> None:
        """从持久化文件加载任务"""
        try:
            with open(self.persistence_file, 'rb') as f:
                tasks_data = pickle.load(f)

            for task_data in tasks_data:
                # 注意：这里不能直接序列化函数，实际使用中需要任务函数可被序列化
                # 这是一个简化实现，实际生产环境可能需要更复杂的序列化方案
                logger.warning(f"无法加载持久化任务，函数不支持序列化: {task_data.get('name', 'unknown')}")

        except Exception as e:
            logger.error(f"加载持久化任务失败: {e}")

    def _save_tasks(self) -> None:
        """保存任务到持久化文件"""
        if not self.persistence_file:
            return

        try:
            with self._queue_lock:
                tasks_data = []
                for task in self._queue:
                    # 只保存任务元数据，不保存函数
                    task_data = {
                        "task_id": task.task_id,
                        "name": task.name,
                        "priority": task.priority,
                        "max_retries": task.max_retries,
                        "timeout": task.timeout,
                        "created_at": task.created_at,
                        "metadata": task.metadata,
                        # 注意：这里不保存 func, args, kwargs
                    }
                    tasks_data.append(task_data)

            # 原子写入
            temp_file = self.persistence_file.with_suffix('.tmp')
            with open(temp_file, 'wb') as f:
                pickle.dump(tasks_data, f)

            temp_file.replace(self.persistence_file)

        except Exception as e:
            logger.error(f"保存持久化任务失败: {e}")

    async def __aenter__(self) -> 'TaskQueue':
        """异步上下文管理器入口"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.stop()


# 便利函数
async def create_task_queue(max_workers: int = 5,
                           persistence_file: Optional[Path] = None) -> TaskQueue:
    """创建任务队列

    Args:
        max_workers: 最大工作线程数
        persistence_file: 持久化文件

    Returns:
        任务队列实例
    """
    queue = TaskQueue(max_workers, persistence_file)
    await queue.start()
    return queue