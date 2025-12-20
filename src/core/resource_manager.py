"""
资源管理器

提供内存管理、并发控制、资源清理等资源优化功能。
"""

import asyncio
import gc
import threading
import time
import weakref
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import (
    Any, Dict, List, Optional, Set, Type, TypeVar, Callable,
    AsyncContextManager, Awaitable, Union
)
from pathlib import Path
import psutil

from atlas.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


@dataclass
class ResourceUsage:
    """资源使用情况"""
    memory_mb: float
    cpu_percent: float
    disk_io_read_mb: float
    disk_io_write_mb: float
    network_io_sent_mb: float
    network_io_recv_mb: float
    open_files: int
    threads: int
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ResourceLimits:
    """资源限制配置"""
    max_memory_mb: Optional[float] = None
    max_cpu_percent: Optional[float] = None
    max_concurrent_tasks: Optional[int] = None
    max_open_files: Optional[int] = None
    gc_threshold_mb: float = 100.0
    cleanup_interval_seconds: float = 60.0


class ResourceManager(ABC):
    """资源管理器抽象基类"""

    @abstractmethod
    async def acquire(self, resource_id: str, **kwargs) -> Any:
        """获取资源"""
        pass

    @abstractmethod
    async def release(self, resource_id: str, resource: Any) -> None:
        """释放资源"""
        pass

    @abstractmethod
    def get_usage_stats(self) -> Dict[str, Any]:
        """获取使用统计"""
        pass


class MemoryManager(ResourceManager):
    """内存管理器"""

    def __init__(self, limits: ResourceLimits):
        """初始化内存管理器"""
        self.limits = limits
        self.allocations: Dict[str, Dict[str, Any]] = {}
        self.memory_pools: Dict[str, List[Any]] = defaultdict(list)
        self._lock = threading.Lock()

    async def acquire(self, resource_id: str, size_mb: float = 0, **kwargs) -> bool:
        """获取内存资源

        Args:
            resource_id: 资源ID
            size_mb: 需要的内存大小（MB）

        Returns:
            是否成功获取资源
        """
        with self._lock:
            current_memory = self._get_current_memory_usage()

            # 检查内存限制
            if self.limits.max_memory_mb:
                if current_memory + size_mb > self.limits.max_memory_mb:
                    logger.warning(f"内存不足: 当前 {current_memory:.1f}MB, 需要 {size_mb:.1f}MB, 限制 {self.limits.max_memory_mb:.1f}MB")
                    return False

            # 记录分配
            self.allocations[resource_id] = {
                "size_mb": size_mb,
                "allocated_at": datetime.now(),
                "type": kwargs.get("type", "unknown")
            }

            return True

    async def release(self, resource_id: str, resource: Any) -> None:
        """释放内存资源"""
        with self._lock:
            if resource_id in self.allocations:
                del self.allocations[resource_id]

                # 如果资源是可清理的，尝试清理
                if hasattr(resource, 'close'):
                    try:
                        if asyncio.iscoroutinefunction(resource.close):
                            await resource.close()
                        else:
                            resource.close()
                    except Exception as e:
                        logger.warning(f"清理资源失败: {e}")

    def _get_current_memory_usage(self) -> float:
        """获取当前内存使用量（MB）"""
        try:
            process = psutil.Process()
            return process.memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    def get_usage_stats(self) -> Dict[str, Any]:
        """获取内存使用统计"""
        current_memory = self._get_current_memory_usage()
        allocated_memory = sum(alloc["size_mb"] for alloc in self.allocations.values())

        return {
            "current_memory_mb": current_memory,
            "allocated_memory_mb": allocated_memory,
            "active_allocations": len(self.allocations),
            "limit_mb": self.limits.max_memory_mb,
            "usage_percent": (current_memory / self.limits.max_memory_mb * 100) if self.limits.max_memory_mb else None
        }

    async def trigger_gc(self) -> int:
        """触发垃圾回收

        Returns:
            回收的对象数量
        """
        collected = gc.collect()
        logger.info(f"垃圾回收完成，回收了 {collected} 个对象")
        return collected

    def register_pool(self, pool_name: str, factory: Callable[[], Any]) -> None:
        """注册对象池

        Args:
            pool_name: 池名称
            factory: 对象创建工厂函数
        """
        # 对象池实现可以在这里扩展
        pass


class ConcurrencyManager(ResourceManager):
    """并发管理器"""

    def __init__(self, limits: ResourceLimits):
        """初始化并发管理器"""
        self.limits = limits
        self.active_tasks: Set[str] = set()
        self.task_queue = asyncio.Queue()
        self.semaphore = asyncio.Semaphore(
            limits.max_concurrent_tasks or asyncio.cpu_count() * 2
        )
        self.task_stats = defaultdict(lambda: {"count": 0, "total_time": 0.0})
        self._lock = threading.Lock()

    @asynccontextmanager
    async def acquire_slot(self, task_id: str, timeout: Optional[float] = None):
        """获取任务执行槽位

        Args:
            task_id: 任务ID
            timeout: 超时时间

        Yields:
            是否获取到槽位
        """
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=timeout)

            with self._lock:
                self.active_tasks.add(task_id)
                self.task_stats[task_id]["count"] += 1

            start_time = time.time()
            try:
                yield True
            finally:
                with self._lock:
                    self.active_tasks.discard(task_id)
                    execution_time = time.time() - start_time
                    self.task_stats[task_id]["total_time"] += execution_time

                self.semaphore.release()

        except asyncio.TimeoutError:
            logger.warning(f"任务 {task_id} 获取执行槽位超时")
            yield False

    async def acquire(self, resource_id: str, **kwargs) -> bool:
        """获取并发资源"""
        try:
            await self.semaphore.acquire()
            with self._lock:
                self.active_tasks.add(resource_id)
            return True
        except Exception as e:
            logger.error(f"获取并发资源失败: {e}")
            return False

    async def release(self, resource_id: str, resource: Any) -> None:
        """释放并发资源"""
        with self._lock:
            self.active_tasks.discard(resource_id)
        self.semaphore.release()

    def get_usage_stats(self) -> Dict[str, Any]:
        """获取并发使用统计"""
        with self._lock:
            return {
                "active_tasks": len(self.active_tasks),
                "max_concurrent": self.limits.max_concurrent_tasks,
                "available_slots": self.semaphore._value,
                "task_stats": dict(self.task_stats)
            }


class FileHandleManager(ResourceManager):
    """文件句柄管理器"""

    def __init__(self, limits: ResourceLimits):
        """初始化文件句柄管理器"""
        self.limits = limits
        self.open_handles: Dict[str, Dict[str, Any]] = {}
        self.handle_pool: Dict[str, List[Any]] = defaultdict(list)
        self._lock = threading.Lock()

    @asynccontextmanager
    async def open_file(self, file_path: Path, mode: str = 'r', **kwargs):
        """安全打开文件

        Args:
            file_path: 文件路径
            mode: 打开模式
            **kwargs: 额外参数

        Yields:
            文件对象
        """
        handle_id = f"{file_path}_{mode}_{id(file_path)}"

        # 检查文件句柄限制
        if self.limits.max_open_files:
            current_handles = len(self.open_handles)
            if current_handles >= self.limits.max_open_files:
                logger.warning(f"文件句柄数量已达限制: {current_handles}/{self.limits.max_open_files}")
                # 尝试清理一些句柄
                await self._cleanup_idle_handles()

        # 打开文件
        file_obj = await asyncio.to_thread(open, file_path, mode, **kwargs)

        try:
            with self._lock:
                self.open_handles[handle_id] = {
                    "path": str(file_path),
                    "mode": mode,
                    "opened_at": datetime.now(),
                    "size": file_path.stat().st_size if file_path.exists() else 0
                }

            yield file_obj

        finally:
            file_obj.close()
            with self._lock:
                self.open_handles.pop(handle_id, None)

    async def acquire(self, resource_id: str, **kwargs) -> Optional[Any]:
        """获取文件句柄资源"""
        # 这个方法的实现取决于具体的使用场景
        return None

    async def release(self, resource_id: str, resource: Any) -> None:
        """释放文件句柄资源"""
        if hasattr(resource, 'close'):
            try:
                if asyncio.iscoroutinefunction(resource.close):
                    await resource.close()
                else:
                    await asyncio.to_thread(resource.close)
            except Exception as e:
                logger.warning(f"关闭文件句柄失败: {e}")

    async def _cleanup_idle_handles(self) -> int:
        """清理空闲文件句柄"""
        cleanup_count = 0
        current_time = datetime.now()
        idle_timeout = timedelta(minutes=5)

        with self._lock:
            idle_handles = [
                handle_id for handle_id, info in self.open_handles.items()
                if current_time - info["opened_at"] > idle_timeout
            ]

            for handle_id in idle_handles:
                # 这里我们只能记录，实际的句柄需要外部管理
                cleanup_count += 1

        return cleanup_count

    def get_usage_stats(self) -> Dict[str, Any]:
        """获取文件句柄使用统计"""
        with self._lock:
            return {
                "open_handles": len(self.open_handles),
                "limit": self.limits.max_open_files,
                "handle_details": list(self.open_handles.values())
            }


class SystemResourceManager:
    """系统资源管理器"""

    def __init__(self, limits: ResourceLimits):
        """初始化系统资源管理器"""
        self.limits = limits
        self.initial_usage = self._get_current_usage()

        # 初始化各个管理器
        self.memory_manager = MemoryManager(limits)
        self.concurrency_manager = ConcurrencyManager(limits)
        self.file_manager = FileHandleManager(limits)

        self.cleanup_task: Optional[asyncio.Task] = None
        self.monitoring_task: Optional[asyncio.Task] = None
        self._running = False

    def _get_current_usage(self) -> ResourceUsage:
        """获取当前资源使用情况"""
        try:
            process = psutil.Process()

            # CPU和内存
            cpu_percent = process.cpu_percent()
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / (1024 * 1024)

            # 磁盘IO
            disk_io = process.io_counters()
            disk_io_read_mb = disk_io.read_bytes / (1024 * 1024)
            disk_io_write_mb = disk_io.write_bytes / (1024 * 1024)

            # 网络IO
            network_io = process.io_counters()
            network_io_sent_mb = network_io.write_bytes / (1024 * 1024)
            network_io_recv_mb = network_io.read_bytes / (1024 * 1024)

            # 文件句柄和线程
            open_files = len(process.open_files())
            threads = process.num_threads()

            return ResourceUsage(
                memory_mb=memory_mb,
                cpu_percent=cpu_percent,
                disk_io_read_mb=disk_io_read_mb,
                disk_io_write_mb=disk_io_write_mb,
                network_io_sent_mb=network_io_sent_mb,
                network_io_recv_mb=network_io_recv_mb,
                open_files=open_files,
                threads=threads
            )

        except Exception as e:
            logger.error(f"获取资源使用情况失败: {e}")
            return ResourceUsage(
                memory_mb=0.0, cpu_percent=0.0, disk_io_read_mb=0.0,
                disk_io_write_mb=0.0, network_io_sent_mb=0.0,
                network_io_recv_mb=0.0, open_files=0, threads=0
            )

    async def start(self) -> None:
        """启动资源管理器"""
        if self._running:
            return

        self._running = True
        logger.info("启动系统资源管理器")

        # 启动清理任务
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

        # 启动监控任务
        self.monitoring_task = asyncio.create_task(self._monitoring_loop())

    async def stop(self) -> None:
        """停止资源管理器"""
        if not self._running:
            return

        self._running = False
        logger.info("停止系统资源管理器")

        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        """清理循环"""
        while self._running:
            try:
                await self._perform_cleanup()
            except Exception as e:
                logger.error(f"资源清理失败: {e}")

            await asyncio.sleep(self.limits.cleanup_interval_seconds)

    async def _monitoring_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                await self._check_resource_limits()
            except Exception as e:
                logger.error(f"资源监控失败: {e}")

            await asyncio.sleep(30.0)  # 每30秒检查一次

    async def _perform_cleanup(self) -> None:
        """执行资源清理"""
        # 清理文件句柄
        await self.file_manager._cleanup_idle_handles()

        # 检查是否需要垃圾回收
        current_memory = self.memory_manager._get_current_memory_usage()
        if current_memory > self.limits.gc_threshold_mb:
            collected = await self.memory_manager.trigger_gc()
            logger.info(f"触发垃圾回收，回收了 {collected} 个对象")

    async def _check_resource_limits(self) -> None:
        """检查资源限制"""
        current_usage = self._get_current_usage()

        # 检查内存限制
        if self.limits.max_memory_mb:
            memory_usage_percent = (current_usage.memory_mb / self.limits.max_memory_mb) * 100
            if memory_usage_percent > 90:
                logger.warning(f"内存使用率过高: {memory_usage_percent:.1f}%")
                await self.memory_manager.trigger_gc()

        # 检查CPU限制
        if self.limits.max_cpu_percent and current_usage.cpu_percent > self.limits.max_cpu_percent:
            logger.warning(f"CPU使用率过高: {current_usage.cpu_percent:.1f}%")

        # 检查文件句柄限制
        if self.limits.max_open_files and current_usage.open_files > self.limits.max_open_files:
            logger.warning(f"文件句柄数量过多: {current_usage.open_files}")

    def get_system_status(self) -> Dict[str, Any]:
        """获取系统状态"""
        current_usage = self._get_current_usage()

        return {
            "current_usage": {
                "memory_mb": current_usage.memory_mb,
                "cpu_percent": current_usage.cpu_percent,
                "open_files": current_usage.open_files,
                "threads": current_usage.threads
            },
            "limits": {
                "max_memory_mb": self.limits.max_memory_mb,
                "max_cpu_percent": self.limits.max_cpu_percent,
                "max_open_files": self.limits.max_open_files,
                "max_concurrent_tasks": self.limits.max_concurrent_tasks
            },
            "managers": {
                "memory": self.memory_manager.get_usage_stats(),
                "concurrency": self.concurrency_manager.get_usage_stats(),
                "file_handles": self.file_manager.get_usage_stats()
            },
            "running": self._running
        }

    @asynccontextmanager
    async def managed_operation(
        self,
        operation_id: str,
        memory_mb: float = 0,
        timeout: Optional[float] = None
    ):
        """托管操作上下文管理器

        Args:
            operation_id: 操作ID
            memory_mb: 需要的内存（MB）
            timeout: 超时时间

        Yields:
            是否成功获取资源
        """
        # 获取内存资源
        memory_acquired = await self.memory_manager.acquire(operation_id, memory_mb)
        if not memory_acquired:
            logger.error(f"无法为操作 {operation_id} 获取内存资源")
            yield False
            return

        try:
            # 获取并发资源
            async with self.concurrency_manager.acquire_slot(operation_id, timeout) as slot_acquired:
                if not slot_acquired:
                    logger.error(f"无法为操作 {operation_id} 获取并发槽位")
                    yield False
                    return

                logger.debug(f"操作 {operation_id} 开始执行")
                yield True

        finally:
            # 释放内存资源
            await self.memory_manager.release(operation_id, None)
            logger.debug(f"操作 {operation_id} 完成")


# 全局资源管理器实例
_global_resource_manager: Optional[SystemResourceManager] = None


def get_global_resource_manager() -> Optional[SystemResourceManager]:
    """获取全局资源管理器实例"""
    return _global_resource_manager


def set_global_resource_manager(manager: SystemResourceManager) -> None:
    """设置全局资源管理器实例"""
    global _global_resource_manager
    _global_resource_manager = manager


# 装饰器：资源管理
def managed_resource(
    memory_mb: float = 0,
    timeout: Optional[float] = None,
    component: Optional[str] = None
):
    """资源管理装饰器

    Args:
        memory_mb: 需要的内存（MB）
        timeout: 超时时间
        component: 组件名称
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            resource_manager = get_global_resource_manager()
            if not resource_manager:
                return await func(*args, **kwargs)

            operation_id = f"{component or 'unknown'}_{func.__name__}_{id(func)}"

            async with resource_manager.managed_operation(operation_id, memory_mb, timeout) as acquired:
                if acquired:
                    return await func(*args, **kwargs)
                else:
                    raise RuntimeError(f"无法获取资源执行操作: {operation_id}")

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            # 对于同步函数，我们需要在异步上下文中执行
            async def async_execute():
                resource_manager = get_global_resource_manager()
                if not resource_manager:
                    return func(*args, **kwargs)

                operation_id = f"{component or 'unknown'}_{func.__name__}_{id(func)}"

                async with resource_manager.managed_operation(operation_id, memory_mb, timeout) as acquired:
                    if acquired:
                        # 同步函数在事件循环中执行
                        return func(*args, **kwargs)
                    else:
                        raise RuntimeError(f"无法获取资源执行操作: {operation_id}")

            return asyncio.run(async_execute())

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# 便利函数：内存优化
def optimize_memory():
    """内存优化装饰器"""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            result = await func(*args, **kwargs)

            # 执行后清理
            resource_manager = get_global_resource_manager()
            if resource_manager:
                await resource_manager.memory_manager.trigger_gc()

            return result

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            result = func(*args, **kwargs)

            # 执行后清理
            resource_manager = get_global_resource_manager()
            if resource_manager:
                asyncio.run(resource_manager.memory_manager.trigger_gc())

            return result

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator