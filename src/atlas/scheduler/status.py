"""
任务状态管理模块

提供任务状态跟踪、监控和统计功能。
"""

import time
import threading
from enum import Enum
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class TaskState(Enum):
    """任务状态枚举"""
    PENDING = "pending"          # 等待执行
    RUNNING = "running"          # 正在执行
    SUCCESS = "success"          # 执行成功
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # 已取消
    TIMEOUT = "timeout"          # 执行超时
    RETRYING = "retrying"        # 重试中


@dataclass
class TaskMetrics:
    """任务指标统计"""
    total_tasks: int = 0
    success_tasks: int = 0
    failed_tasks: int = 0
    cancelled_tasks: int = 0
    timeout_tasks: int = 0
    running_tasks: int = 0
    pending_tasks: int = 0
    retrying_tasks: int = 0

    # 时间统计
    total_execution_time: float = 0.0
    avg_execution_time: float = 0.0
    max_execution_time: float = 0.0
    min_execution_time: float = float('inf')

    # 成功率统计
    success_rate: float = 0.0

    # 最后更新时间
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def update_success_rate(self) -> None:
        """更新成功率"""
        if self.total_tasks == 0:
            self.success_rate = 0.0
        else:
            completed_tasks = self.success_tasks + self.failed_tasks + self.timeout_tasks
            self.success_rate = (self.success_tasks / completed_tasks * 100) if completed_tasks > 0 else 0.0

    def update_execution_stats(self, execution_time: float) -> None:
        """更新执行时间统计"""
        self.total_execution_time += execution_time
        self.max_execution_time = max(self.max_execution_time, execution_time)
        self.min_execution_time = min(self.min_execution_time, execution_time)

        completed_tasks = self.success_tasks + self.failed_tasks + self.timeout_tasks
        if completed_tasks > 0:
            self.avg_execution_time = self.total_execution_time / completed_tasks


@dataclass
class TaskStatus:
    """单个任务状态"""
    task_id: str
    task_name: str
    state: TaskState = TaskState.PENDING
    priority: str = "normal"

    # 时间信息
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # 执行信息
    execution_time: float = 0.0
    retry_count: int = 0
    max_retries: int = 3

    # 结果信息
    result: Optional[Any] = None
    error_message: Optional[str] = None
    error_traceback: Optional[str] = None

    # 元数据
    metadata: Dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        """开始执行任务"""
        self.state = TaskState.RUNNING
        self.started_at = datetime.now(timezone.utc)

    def complete(self, result: Any = None) -> None:
        """完成任务"""
        self.state = TaskState.SUCCESS
        self.completed_at = datetime.now(timezone.utc)
        self.result = result
        if self.started_at:
            self.execution_time = (self.completed_at - self.started_at).total_seconds()

    def fail(self, error_message: str, error_traceback: Optional[str] = None) -> None:
        """任务失败"""
        self.state = TaskState.FAILED
        self.completed_at = datetime.now(timezone.utc)
        self.error_message = error_message
        self.error_traceback = error_traceback
        if self.started_at:
            self.execution_time = (self.completed_at - self.started_at).total_seconds()

    def cancel(self) -> None:
        """取消任务"""
        self.state = TaskState.CANCELLED
        self.completed_at = datetime.now(timezone.utc)
        if self.started_at:
            self.execution_time = (self.completed_at - self.started_at).total_seconds()

    def timeout(self) -> None:
        """任务超时"""
        self.state = TaskState.TIMEOUT
        self.completed_at = datetime.now(timezone.utc)
        if self.started_at:
            self.execution_time = (self.completed_at - self.started_at).total_seconds()

    def retry(self) -> bool:
        """重试任务"""
        if self.retry_count < self.max_retries:
            self.retry_count += 1
            self.state = TaskState.RETRYING
            self.started_at = None
            self.completed_at = None
            self.execution_time = 0.0
            self.error_message = None
            self.error_traceback = None
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "state": self.state.value,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "execution_time": self.execution_time,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "result": str(self.result) if self.result else None,
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "metadata": self.metadata,
        }


class StatusManager:
    """任务状态管理器"""

    def __init__(self, persistence_file: Optional[Path] = None):
        """初始化状态管理器

        Args:
            persistence_file: 状态持久化文件路径
        """
        self.persistence_file = persistence_file
        self._tasks: Dict[str, TaskStatus] = {}
        self._lock = threading.RLock()
        self._metrics = TaskMetrics()

        # 加载持久化状态
        if persistence_file and persistence_file.exists():
            self._load_status()

        logger.info(f"状态管理器初始化完成，持久化文件: {persistence_file}")

    def create_task(self, task_id: str, task_name: str, priority: str = "normal",
                   max_retries: int = 3, metadata: Optional[Dict[str, Any]] = None) -> TaskStatus:
        """创建新任务状态

        Args:
            task_id: 任务ID
            task_name: 任务名称
            priority: 任务优先级
            max_retries: 最大重试次数
            metadata: 任务元数据

        Returns:
            任务状态对象
        """
        with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"任务已存在: {task_id}")

            task = TaskStatus(
                task_id=task_id,
                task_name=task_name,
                priority=priority,
                max_retries=max_retries,
                metadata=metadata or {}
            )

            self._tasks[task_id] = task
            self._update_metrics()

            logger.debug(f"创建任务状态: {task_id} - {task_name}")
            return task

    def get_task(self, task_id: str) -> Optional[TaskStatus]:
        """获取任务状态"""
        with self._lock:
            return self._tasks.get(task_id)

    def update_task(self, task_id: str, **kwargs) -> bool:
        """更新任务状态

        Args:
            task_id: 任务ID
            **kwargs: 要更新的属性

        Returns:
            是否更新成功
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            for key, value in kwargs.items():
                if hasattr(task, key):
                    setattr(task, key, value)

            self._update_metrics()
            return True

    def start_task(self, task_id: str) -> bool:
        """开始执行任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.start()
            self._update_metrics()
            logger.info(f"任务开始执行: {task_id}")
            return True

    def complete_task(self, task_id: str, result: Any = None) -> bool:
        """完成任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.complete(result)
            self._update_metrics()
            logger.info(f"任务执行成功: {task_id}")
            return True

    def fail_task(self, task_id: str, error_message: str,
                 error_traceback: Optional[str] = None) -> bool:
        """任务失败"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.fail(error_message, error_traceback)
            self._update_metrics()
            logger.error(f"任务执行失败: {task_id} - {error_message}")
            return True

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.cancel()
            self._update_metrics()
            logger.info(f"任务已取消: {task_id}")
            return True

    def timeout_task(self, task_id: str) -> bool:
        """任务超时"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            task.timeout()
            self._update_metrics()
            logger.warning(f"任务执行超时: {task_id}")
            return True

    def retry_task(self, task_id: str) -> bool:
        """重试任务"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            if task.retry():
                self._update_metrics()
                logger.info(f"任务重试: {task_id} (第{task.retry_count}次)")
                return True
            else:
                logger.warning(f"任务重试次数已达上限: {task_id}")
                return False

    def get_tasks_by_state(self, state: TaskState) -> List[TaskStatus]:
        """根据状态获取任务列表"""
        with self._lock:
            return [task for task in self._tasks.values() if task.state == state]

    def get_tasks_by_name(self, task_name: str) -> List[TaskStatus]:
        """根据名称获取任务列表"""
        with self._lock:
            return [task for task in self._tasks.values() if task.task_name == task_name]

    def get_metrics(self) -> TaskMetrics:
        """获取任务指标"""
        with self._lock:
            return TaskMetrics(
                total_tasks=self._metrics.total_tasks,
                success_tasks=self._metrics.success_tasks,
                failed_tasks=self._metrics.failed_tasks,
                cancelled_tasks=self._metrics.cancelled_tasks,
                timeout_tasks=self._metrics.timeout_tasks,
                running_tasks=self._metrics.running_tasks,
                pending_tasks=self._metrics.pending_tasks,
                retrying_tasks=self._metrics.retrying_tasks,
                total_execution_time=self._metrics.total_execution_time,
                avg_execution_time=self._metrics.avg_execution_time,
                max_execution_time=self._metrics.max_execution_time,
                min_execution_time=self._metrics.min_execution_time if self._metrics.min_execution_time != float('inf') else 0.0,
                success_rate=self._metrics.success_rate,
                last_updated=self._metrics.last_updated,
            )

    def cleanup_old_tasks(self, days: int = 30) -> int:
        """清理旧任务状态

        Args:
            days: 保留天数

        Returns:
            清理的任务数量
        """
        with self._lock:
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
            old_tasks = [
                task_id for task_id, task in self._tasks.items()
                if task.completed_at and task.completed_at < cutoff_time
            ]

            for task_id in old_tasks:
                del self._tasks[task_id]

            if old_tasks:
                self._update_metrics()
                logger.info(f"清理了 {len(old_tasks)} 个旧任务状态")

            return len(old_tasks)

    def export_status(self, file_path: Path) -> None:
        """导出任务状态到文件"""
        with self._lock:
            status_data = {
                "export_time": datetime.now(timezone.utc).isoformat(),
                "total_tasks": len(self._tasks),
                "metrics": self.get_metrics().__dict__,
                "tasks": [task.to_dict() for task in self._tasks.values()]
            }

            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(status_data, f, indent=2, ensure_ascii=False)

            logger.info(f"任务状态已导出到: {file_path}")

    def _update_metrics(self) -> None:
        """更新任务指标"""
        self._metrics = TaskMetrics()

        for task in self._tasks.values():
            self._metrics.total_tasks += 1

            if task.state == TaskState.SUCCESS:
                self._metrics.success_tasks += 1
                self._metrics.update_execution_stats(task.execution_time)
            elif task.state == TaskState.FAILED:
                self._metrics.failed_tasks += 1
                self._metrics.update_execution_stats(task.execution_time)
            elif task.state == TaskState.CANCELLED:
                self._metrics.cancelled_tasks += 1
            elif task.state == TaskState.TIMEOUT:
                self._metrics.timeout_tasks += 1
                self._metrics.update_execution_stats(task.execution_time)
            elif task.state == TaskState.RUNNING:
                self._metrics.running_tasks += 1
            elif task.state == TaskState.PENDING:
                self._metrics.pending_tasks += 1
            elif task.state == TaskState.RETRYING:
                self._metrics.retrying_tasks += 1

        self._metrics.update_success_rate()
        self._metrics.last_updated = datetime.now(timezone.utc)

    def _load_status(self) -> None:
        """加载持久化状态"""
        try:
            with open(self.persistence_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 重建任务状态
            for task_data in data.get("tasks", []):
                task = TaskStatus(
                    task_id=task_data["task_id"],
                    task_name=task_data["task_name"],
                    state=TaskState(task_data["state"]),
                    priority=task_data["priority"],
                    created_at=datetime.fromisoformat(task_data["created_at"]),
                    started_at=datetime.fromisoformat(task_data["started_at"]) if task_data["started_at"] else None,
                    completed_at=datetime.fromisoformat(task_data["completed_at"]) if task_data["completed_at"] else None,
                    execution_time=task_data["execution_time"],
                    retry_count=task_data["retry_count"],
                    max_retries=task_data["max_retries"],
                    metadata=task_data["metadata"],
                )
                self._tasks[task.task_id] = task

            logger.info(f"从持久化文件加载了 {len(self._tasks)} 个任务状态")

        except Exception as e:
            logger.warning(f"加载持久化状态失败: {e}")

    def save_status(self) -> None:
        """保存状态到持久化文件"""
        if not self.persistence_file:
            return

        try:
            # 确保目录存在
            self.persistence_file.parent.mkdir(parents=True, exist_ok=True)

            status_data = {
                "save_time": datetime.now(timezone.utc).isoformat(),
                "total_tasks": len(self._tasks),
                "tasks": [task.to_dict() for task in self._tasks.values()]
            }

            # 原子写入
            temp_file = self.persistence_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(status_data, f, indent=2, ensure_ascii=False)

            temp_file.replace(self.persistence_file)

            logger.debug(f"任务状态已保存到: {self.persistence_file}")

        except Exception as e:
            logger.error(f"保存任务状态失败: {e}")