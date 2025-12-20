"""
LLM 任务队列管理器

管理 LLM 调用任务队列，优化资源使用和响应时间。
支持优先级、批处理、结果缓存等功能。
"""

import asyncio
import uuid
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Awaitable
from datetime import datetime, timezone
import json
import heapq

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class LLMTaskPriority(Enum):
    """LLM任务优先级"""
    URGENT = 0      # 紧急（如实时响应）
    HIGH = 1        # 高（如用户请求）
    NORMAL = 2      # 正常（如批处理）
    LOW = 3         # 低（如后台任务）
    BACKGROUND = 4  # 后台（如预计算）


class LLMTaskStatus(Enum):
    """LLM任务状态"""
    PENDING = "pending"          # 等待执行
    RUNNING = "running"          # 正在执行
    COMPLETED = "completed"      # 执行完成
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # 已取消
    TIMEOUT = "timeout"          # 执行超时


class LLMTaskType(Enum):
    """LLM任务类型"""
    GENERATE = "generate"        # 文本生成
    EMBED = "embed"              # 嵌入生成
    SEMANTIC_DEDUP = "semantic_dedup"  # 语义去重
    BATCH_PROCESS = "batch_process"     # 批处理
    CUSTOM = "custom"            # 自定义任务


@dataclass
class LLMTask:
    """LLM任务"""
    task_id: str
    task_type: LLMTaskType
    priority: LLMTaskPriority
    func: Callable[..., Awaitable[Any]]
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)

    # 时间信息
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # 执行信息
    timeout: Optional[float] = None
    max_retries: int = 3
    retry_count: int = 0

    # 结果信息
    result: Optional[Any] = None
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None

    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """初始化后处理"""
        if not self.task_id:
            self.task_id = str(uuid.uuid4())

    def __lt__(self, other: 'LLMTask') -> bool:
        """优先级比较（用于堆排序）"""
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        # 相同优先级按创建时间排序
        return self.created_at < other.created_at

    @property
    def status(self) -> LLMTaskStatus:
        """获取任务状态"""
        if self.completed_at:
            if self.error_message:
                return LLMTaskStatus.FAILED
            return LLMTaskStatus.COMPLETED
        elif self.started_at:
            return LLMTaskStatus.RUNNING
        else:
            return LLMTaskStatus.PENDING

    @property
    def execution_time(self) -> float:
        """获取执行时间（秒）"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "priority": self.priority.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "retry_count": self.retry_count,
            "execution_time": self.execution_time,
            "result": str(self.result) if self.result else None,
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "metadata": self.metadata,
        }


@dataclass
class QueueStats:
    """队列统计"""
    total_tasks: int = 0
    pending_tasks: int = 0
    running_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    cancelled_tasks: int = 0

    # 性能统计
    avg_execution_time: float = 0.0
    total_execution_time: float = 0.0
    success_rate: float = 0.0

    # 按类型统计
    task_type_stats: Dict[str, int] = field(default_factory=dict)

    # 按优先级统计
    priority_stats: Dict[str, int] = field(default_factory=dict)

    def update_success_rate(self) -> None:
        """更新成功率"""
        completed = self.completed_tasks + self.failed_tasks
        if completed > 0:
            self.success_rate = (self.completed_tasks / completed) * 100
        else:
            self.success_rate = 0.0

    def update_avg_execution_time(self, execution_time: float) -> None:
        """更新平均执行时间"""
        if self.completed_tasks > 0:
            self.total_execution_time += execution_time
            self.avg_execution_time = self.total_execution_time / self.completed_tasks


class LLMQueueManager:
    """LLM队列管理器"""

    def __init__(
        self,
        max_concurrent_tasks: int = 5,
        max_queue_size: int = 1000,
        result_ttl: int = 3600,  # 结果缓存时间
        persistence_file: Optional[Path] = None
    ):
        """初始化队列管理器

        Args:
            max_concurrent_tasks: 最大并发任务数
            max_queue_size: 最大队列长度
            result_ttl: 结果缓存TTL（秒）
            persistence_file: 持久化文件路径
        """
        self.max_concurrent_tasks = max_concurrent_tasks
        self.max_queue_size = max_queue_size
        self.result_ttl = result_ttl
        self.persistence_file = persistence_file

        # 任务队列（优先级队列）
        self._queue: List[LLMTask] = []
        self._queue_lock = asyncio.Lock()

        # 运行中的任务
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._running_lock = asyncio.Lock()

        # 已完成的任务（结果缓存）
        self._completed_tasks: Dict[str, LLMTask] = {}
        self._results_cache: Dict[str, Any] = {}

        # 并发控制
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)

        # 状态控制
        self._running = False
        self._worker_tasks: List[asyncio.Task] = []

        # 统计信息
        self._stats = QueueStats()

        # 加载持久化数据
        if persistence_file and persistence_file.exists():
            self._load_persistence()

        logger.info(f"LLM队列管理器初始化完成，最大并发: {max_concurrent_tasks}")

    async def start(self) -> None:
        """启动队列管理器"""
        if self._running:
            return

        self._running = True

        # 启动工作线程
        for i in range(self.max_concurrent_tasks):
            worker_task = asyncio.create_task(self._worker(f"llm-worker-{i}"))
            self._worker_tasks.append(worker_task)

        # 启动清理任务
        cleanup_task = asyncio.create_task(self._cleanup_worker())
        self._worker_tasks.append(cleanup_task)

        logger.info(f"LLM队列管理器已启动，启动了 {len(self._worker_tasks)} 个工作线程")

    async def stop(self, timeout: float = 30.0) -> None:
        """停止队列管理器

        Args:
            timeout: 停止超时时间
        """
        logger.info("正在停止LLM队列管理器...")
        self._running = False

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

        # 保存持久化数据
        if self.persistence_file:
            self._save_persistence()

        logger.info("LLM队列管理器已停止")

    async def submit(
        self,
        task_type: LLMTaskType,
        func: Callable[..., Awaitable[Any]],
        priority: LLMTaskPriority = LLMTaskPriority.NORMAL,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        max_retries: int = 3,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """提交任务到队列

        Args:
            task_type: 任务类型
            func: 异步函数
            priority: 任务优先级
            args: 位置参数
            kwargs: 关键字参数
            timeout: 超时时间
            max_retries: 最大重试次数
            metadata: 任务元数据

        Returns:
            任务ID
        """
        async with self._queue_lock:
            if len(self._queue) >= self.max_queue_size:
                raise RuntimeError("队列已满，无法提交新任务")

            task = LLMTask(
                task_id=str(uuid.uuid4()),
                task_type=task_type,
                priority=priority,
                func=func,
                args=args,
                kwargs=kwargs or {},
                timeout=timeout,
                max_retries=max_retries,
                metadata=metadata or {}
            )

            heapq.heappush(self._queue, task)
            self._update_stats("submitted", task)

            logger.debug(f"任务已提交到队列: {task.task_id} ({task_type.value})")
            return task.task_id

    async def get_result(self, task_id: str, timeout: Optional[float] = None) -> Any:
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
            # 检查任务是否已完成
            if task_id in self._completed_tasks:
                task = self._completed_tasks[task_id]
                if task.status == LLMTaskStatus.COMPLETED:
                    return task.result
                elif task.status == LLMTaskStatus.FAILED:
                    raise RuntimeError(f"任务执行失败: {task.error_message}")

            # 检查超时
            if timeout and (time.time() - start_time) > timeout:
                raise TimeoutError(f"等待任务结果超时: {task_id}")

            await asyncio.sleep(0.1)

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务

        Args:
            task_id: 任务ID

        Returns:
            是否取消成功
        """
        # 尝试从队列中移除
        async with self._queue_lock:
            for i, task in enumerate(self._queue):
                if task.task_id == task_id:
                    # 标记为取消并移除
                    task.completed_at = datetime.now(timezone.utc)
                    del self._queue[i]
                    heapq.heapify(self._queue)
                    self._completed_tasks[task_id] = task
                    self._update_stats("cancelled", task)
                    logger.info(f"任务已取消: {task_id}")
                    return True

        # 尝试取消运行中的任务
        with self._running_lock:
            if task_id in self._running_tasks:
                task = self._running_tasks[task_id]
                task.cancel()
                logger.info(f"运行中任务已取消: {task_id}")
                return True

        return False

    def get_task_status(self, task_id: str) -> Optional[LLMTask]:
        """获取任务状态

        Args:
            task_id: 任务ID

        Returns:
            任务对象
        """
        # 检查已完成任务
        if task_id in self._completed_tasks:
            return self._completed_tasks[task_id]

        # 检查运行中任务
        with self._running_lock:
            if task_id in self._running_tasks:
                # 从运行任务中获取原始任务（这里简化处理）
                return None  # 实际实现中需要维护运行任务的原始对象

        # 检查队列中的任务
        async with self._queue_lock:
            for task in self._queue:
                if task.task_id == task_id:
                    return task

        return None

    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        async with self._queue_lock, self._running_lock:
            return {
                "total_tasks": len(self._queue) + len(self._running_tasks) + len(self._completed_tasks),
                "pending_tasks": len(self._queue),
                "running_tasks": len(self._running_tasks),
                "completed_tasks": len(self._completed_tasks),
                "max_concurrent_tasks": self.max_concurrent_tasks,
                "max_queue_size": self.max_queue_size,
                "running": self._running,
                "stats": self._stats.__dict__,
                "pending_by_priority": {
                    priority.name: sum(1 for task in self._queue if task.priority == priority)
                    for priority in LLMTaskPriority
                }
            }

    async def batch_submit(
        self,
        tasks: List[Dict[str, Any]]
    ) -> List[str]:
        """批量提交任务

        Args:
            tasks: 任务列表，每个任务包含必要的参数

        Returns:
            任务ID列表
        """
        task_ids = []
        for task_config in tasks:
            task_id = await self.submit(**task_config)
            task_ids.append(task_id)
        return task_ids

    async def batch_get_results(
        self,
        task_ids: List[str],
        timeout: Optional[float] = None
    ) -> List[Any]:
        """批量获取结果

        Args:
            task_ids: 任务ID列表
            timeout: 等待超时时间

        Returns:
            结果列表
        """
        results = []
        for task_id in task_ids:
            try:
                result = await self.get_result(task_id, timeout)
                results.append(result)
            except Exception as e:
                results.append({"error": str(e), "task_id": task_id})
        return results

    def _update_stats(self, action: str, task: LLMTask) -> None:
        """更新统计信息"""
        if action == "submitted":
            self._stats.total_tasks += 1
            self._stats.pending_tasks += 1
            # 按类型统计
            task_type = task.task_type.value
            self._stats.task_type_stats[task_type] = self._stats.task_type_stats.get(task_type, 0) + 1
            # 按优先级统计
            priority = task.priority.value
            self._stats.priority_stats[priority] = self._stats.priority_stats.get(priority, 0) + 1

        elif action == "started":
            self._stats.pending_tasks -= 1
            self._stats.running_tasks += 1

        elif action == "completed":
            self._stats.running_tasks -= 1
            self._stats.completed_tasks += 1
            self._stats.update_avg_execution_time(task.execution_time)
            self._stats.update_success_rate()

        elif action == "failed":
            self._stats.running_tasks -= 1
            self._stats.failed_tasks += 1
            self._stats.update_success_rate()

        elif action == "cancelled":
            self._stats.cancelled_tasks += 1

    async def _worker(self, worker_name: str) -> None:
        """工作线程"""
        logger.debug(f"LLM工作线程启动: {worker_name}")

        while self._running:
            try:
                # 获取下一个任务
                task = await self._get_next_task()
                if task is None:
                    await asyncio.sleep(0.1)
                    continue

                # 执行任务
                await self._execute_task(task, worker_name)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"LLM工作线程异常: {worker_name} - {e}")
                await asyncio.sleep(1.0)

        logger.debug(f"LLM工作线程停止: {worker_name}")

    async def _get_next_task(self) -> Optional[LLMTask]:
        """获取下一个任务"""
        async with self._queue_lock:
            if self._queue:
                return heapq.heappop(self._queue)
            return None

    async def _execute_task(self, task: LLMTask, worker_name: str) -> None:
        """执行任务"""
        async with self._semaphore:
            execution_task = asyncio.create_task(self._run_task(task), name=f"{worker_name}-{task.task_id}")

            with self._running_lock:
                self._running_tasks[task.task_id] = execution_task

            try:
                logger.debug(f"开始执行LLM任务: {task.task_id} ({task.task_type.value}) - {worker_name}")
                await execution_task
                logger.debug(f"LLM任务执行完成: {task.task_id}")

            except asyncio.CancelledError:
                logger.info(f"LLM任务被取消: {task.task_id}")
                task.completed_at = datetime.now(timezone.utc)

            except Exception as e:
                logger.error(f"LLM任务执行失败: {task.task_id} - {e}")
                task.error_message = str(e)

            finally:
                with self._running_lock:
                    self._running_tasks.pop(task.task_id, None)

                # 添加到已完成任务
                self._completed_tasks[task.task_id] = task

                # 更新统计
                if task.error_message:
                    self._update_stats("failed", task)
                else:
                    self._update_stats("completed", task)

    async def _run_task(self, task: LLMTask) -> None:
        """运行任务"""
        task.started_at = datetime.now(timezone.utc)
        self._update_stats("started", task)

        retry_count = 0
        last_error = None

        while retry_count <= task.max_retries:
            try:
                if task.timeout:
                    result = await asyncio.wait_for(
                        task.func(*task.args, **task.kwargs),
                        timeout=task.timeout
                    )
                else:
                    result = await task.func(*task.args, **task.kwargs)

                # 任务成功
                task.result = result
                task.completed_at = datetime.now(timezone.utc)
                return

            except asyncio.TimeoutError:
                last_error = "任务执行超时"
                logger.warning(f"LLM任务超时: {task.task_id} (第{retry_count + 1}次尝试)")

            except Exception as e:
                last_error = str(e)
                logger.warning(f"LLM任务失败: {task.task_id} (第{retry_count + 1}次尝试) - {e}")

            retry_count += 1
            task.retry_count = retry_count

            if retry_count <= task.max_retries:
                # 指数退避
                wait_time = min(2 ** retry_count, 30)
                await asyncio.sleep(wait_time)

        # 任务失败
        task.error_message = last_error
        task.completed_at = datetime.now(timezone.utc)

    async def _cleanup_worker(self) -> None:
        """清理工作线程"""
        while self._running:
            try:
                await self._cleanup_expired_results()
                await asyncio.sleep(300)  # 每5分钟清理一次
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理工作线程异常: {e}")
                await asyncio.sleep(60)

    async def _cleanup_expired_results(self) -> None:
        """清理过期结果"""
        current_time = time.time()
        expired_tasks = []

        for task_id, task in self._completed_tasks.items():
            if task.completed_at:
                age = (datetime.now(timezone.utc) - task.completed_at).total_seconds()
                if age > self.result_ttl:
                    expired_tasks.append(task_id)

        for task_id in expired_tasks:
            del self._completed_tasks[task_id]
            self._results_cache.pop(task_id, None)

        if expired_tasks:
            logger.info(f"清理了 {len(expired_tasks)} 个过期任务结果")

    def _save_persistence(self) -> None:
        """保存持久化数据"""
        if not self.persistence_file:
            return

        try:
            # 准备持久化数据（不包括结果和运行时状态）
            persistence_data = {
                "stats": self._stats.__dict__,
                "completed_tasks_count": len(self._completed_tasks),
                "save_time": datetime.now(timezone.utc).isoformat(),
            }

            # 原子写入
            temp_file = self.persistence_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(persistence_data, f, indent=2, ensure_ascii=False)

            temp_file.replace(self.persistence_file)
            logger.debug(f"LLM队列持久化数据已保存: {self.persistence_file}")

        except Exception as e:
            logger.error(f"保存LLM队列持久化数据失败: {e}")

    def _load_persistence(self) -> None:
        """加载持久化数据"""
        try:
            with open(self.persistence_file, 'r', encoding='utf-8') as f:
                persistence_data = json.load(f)

            # 恢复统计信息
            if "stats" in persistence_data:
                self._stats = QueueStats(**persistence_data["stats"])

            logger.debug(f"LLM队列持久化数据已加载: {persistence_data.get('save_time')}")

        except Exception as e:
            logger.error(f"加载LLM队列持久化数据失败: {e}")


# 便利函数
async def create_llm_queue_manager(
    max_concurrent_tasks: int = 5,
    max_queue_size: int = 1000,
    persistence_file: Optional[Path] = None,
    **kwargs
) -> LLMQueueManager:
    """创建LLM队列管理器

    Args:
        max_concurrent_tasks: 最大并发任务数
        max_queue_size: 最大队列长度
        persistence_file: 持久化文件路径
        **kwargs: 其他参数

    Returns:
        LLM队列管理器实例
    """
    queue_manager = LLMQueueManager(
        max_concurrent_tasks=max_concurrent_tasks,
        max_queue_size=max_queue_size,
        persistence_file=persistence_file,
        **kwargs
    )
    await queue_manager.start()
    return queue_manager