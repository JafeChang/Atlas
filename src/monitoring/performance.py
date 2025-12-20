"""
性能监控模块

提供系统性能监控和指标收集功能。
"""

import asyncio
import time
import psutil
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from collections import defaultdict, deque
from pathlib import Path
import json

from atlas.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PerformanceMetrics:
    """性能指标数据类"""
    timestamp: datetime
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_available_mb: float
    disk_usage_percent: float
    disk_free_gb: float
    network_io_sent_mb: float
    network_io_recv_mb: float
    active_threads: int
    open_files: int
    load_average: Optional[List[float]] = None
    custom_metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ComponentMetrics:
    """组件性能指标"""
    component_name: str
    operation_name: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    success: bool
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class MetricsCollector:
    """指标收集器"""

    def __init__(self, max_history_size: int = 1000):
        """初始化指标收集器

        Args:
            max_history_size: 最大历史记录数量
        """
        self.max_history_size = max_history_size
        self.system_metrics: deque = deque(maxlen=max_history_size)
        self.component_metrics: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max_history_size)
        )
        self.custom_collectors: Dict[str, Callable[[], Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def add_system_metrics(self, metrics: PerformanceMetrics) -> None:
        """添加系统指标"""
        with self._lock:
            self.system_metrics.append(metrics)

    def add_component_metrics(self, metrics: ComponentMetrics) -> None:
        """添加组件指标"""
        with self._lock:
            key = f"{metrics.component_name}.{metrics.operation_name}"
            self.component_metrics[key].append(metrics)

    def get_latest_system_metrics(self) -> Optional[PerformanceMetrics]:
        """获取最新的系统指标"""
        with self._lock:
            return self.system_metrics[-1] if self.system_metrics else None

    def get_system_metrics_history(
        self,
        minutes: int = 60
    ) -> List[PerformanceMetrics]:
        """获取指定时间范围内的系统指标历史"""
        cutoff_time = datetime.now() - timedelta(minutes=minutes)

        with self._lock:
            return [
                metrics for metrics in self.system_metrics
                if metrics.timestamp >= cutoff_time
            ]

    def get_component_metrics(
        self,
        component_name: str,
        operation_name: Optional[str] = None,
        minutes: int = 60
    ) -> List[ComponentMetrics]:
        """获取组件指标"""
        cutoff_time = datetime.now() - timedelta(minutes=minutes)

        with self._lock:
            if operation_name:
                key = f"{component_name}.{operation_name}"
                metrics_list = self.component_metrics.get(key, [])
            else:
                # 获取组件的所有操作指标
                metrics_list = []
                for key, metrics in self.component_metrics.items():
                    if key.startswith(f"{component_name}."):
                        metrics_list.extend(metrics)

            return [
                metrics for metrics in metrics_list
                if metrics.end_time >= cutoff_time
            ]

    def register_custom_collector(
        self,
        name: str,
        collector_func: Callable[[], Dict[str, Any]]
    ) -> None:
        """注册自定义指标收集器

        Args:
            name: 收集器名称
            collector_func: 收集器函数，返回指标字典
        """
        self.custom_collectors[name] = collector_func

    def get_performance_summary(self, minutes: int = 60) -> Dict[str, Any]:
        """获取性能摘要

        Args:
            minutes: 统计时间范围（分钟）

        Returns:
            性能摘要字典
        """
        system_metrics = self.get_system_metrics_history(minutes)

        if not system_metrics:
            return {}

        # 计算统计信息
        cpu_values = [m.cpu_percent for m in system_metrics]
        memory_values = [m.memory_percent for m in system_metrics]
        disk_values = [m.disk_usage_percent for m in system_metrics]

        return {
            "time_range_minutes": minutes,
            "sample_count": len(system_metrics),
            "cpu": {
                "avg": sum(cpu_values) / len(cpu_values),
                "max": max(cpu_values),
                "min": min(cpu_values),
                "current": cpu_values[-1] if cpu_values else 0
            },
            "memory": {
                "avg": sum(memory_values) / len(memory_values),
                "max": max(memory_values),
                "min": min(memory_values),
                "current": memory_values[-1] if memory_values else 0
            },
            "disk": {
                "avg": sum(disk_values) / len(disk_values),
                "max": max(disk_values),
                "min": min(disk_values),
                "current": disk_values[-1] if disk_values else 0
            }
        }


class PerformanceMonitor:
    """性能监控器"""

    def __init__(
        self,
        collect_interval: float = 5.0,
        enable_auto_collection: bool = True,
        metrics_file: Optional[Path] = None
    ):
        """初始化性能监控器

        Args:
            collect_interval: 收集间隔（秒）
            enable_auto_collection: 是否启用自动收集
            metrics_file: 指标保存文件路径
        """
        self.collect_interval = collect_interval
        self.enable_auto_collection = enable_auto_collection
        self.metrics_file = metrics_file
        self.metrics_collector = MetricsCollector()

        self._running = False
        self._collection_task: Optional[asyncio.Task] = None
        self._save_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动性能监控"""
        if self._running:
            logger.warning("性能监控已经在运行")
            return

        self._running = True
        logger.info(f"启动性能监控，收集间隔: {self.collect_interval}秒")

        if self.enable_auto_collection:
            self._collection_task = asyncio.create_task(self._collect_loop())

        if self.metrics_file:
            self._save_task = asyncio.create_task(self._save_loop())

    async def stop(self) -> None:
        """停止性能监控"""
        if not self._running:
            return

        self._running = False
        logger.info("停止性能监控")

        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass

        if self._save_task:
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass

    async def _collect_loop(self) -> None:
        """指标收集循环"""
        while self._running:
            try:
                metrics = await self._collect_system_metrics()
                self.metrics_collector.add_system_metrics(metrics)

                # 收集自定义指标
                for name, collector in self.metrics_collector.custom_collectors.items():
                    try:
                        custom_metrics = collector()
                        if custom_metrics:
                            metrics.custom_metrics[name] = custom_metrics
                    except Exception as e:
                        logger.warning(f"自定义指标收集器 {name} 失败: {e}")

            except Exception as e:
                logger.error(f"系统指标收集失败: {e}")

            await asyncio.sleep(self.collect_interval)

    async def _save_loop(self) -> None:
        """指标保存循环"""
        while self._running:
            try:
                await self._save_metrics()
            except Exception as e:
                logger.error(f"指标保存失败: {e}")

            # 每5分钟保存一次
            await asyncio.sleep(300)

    async def _collect_system_metrics(self) -> PerformanceMetrics:
        """收集系统指标"""
        # CPU指标
        cpu_percent = psutil.cpu_percent(interval=0.1)

        # 内存指标
        memory = psutil.virtual_memory()
        memory_used_mb = memory.used / (1024 * 1024)
        memory_available_mb = memory.available / (1024 * 1024)

        # 磁盘指标
        disk = psutil.disk_usage('/')
        disk_usage_percent = disk.percent
        disk_free_gb = disk.free / (1024 * 1024 * 1024)

        # 网络指标
        network = psutil.net_io_counters()
        network_io_sent_mb = network.bytes_sent / (1024 * 1024)
        network_io_recv_mb = network.bytes_recv / (1024 * 1024)

        # 系统指标
        active_threads = threading.active_count()
        try:
            process = psutil.Process()
            open_files = len(process.open_files())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            open_files = 0

        # 负载平均值（仅Unix系统）
        load_average = None
        try:
            load_average = list(psutil.getloadavg())
        except (AttributeError, OSError):
            pass

        return PerformanceMetrics(
            timestamp=datetime.now(),
            cpu_percent=cpu_percent,
            memory_percent=memory.percent,
            memory_used_mb=memory_used_mb,
            memory_available_mb=memory_available_mb,
            disk_usage_percent=disk_usage_percent,
            disk_free_gb=disk_free_gb,
            network_io_sent_mb=network_io_sent_mb,
            network_io_recv_mb=network_io_recv_mb,
            active_threads=active_threads,
            open_files=open_files,
            load_average=load_average
        )

    async def _save_metrics(self) -> None:
        """保存指标到文件"""
        if not self.metrics_file:
            return

        try:
            # 确保目录存在
            self.metrics_file.parent.mkdir(parents=True, exist_ok=True)

            # 准备保存数据
            latest_metrics = self.metrics_collector.get_latest_system_metrics()
            if latest_metrics:
                data = {
                    "timestamp": latest_metrics.timestamp.isoformat(),
                    "cpu_percent": latest_metrics.cpu_percent,
                    "memory_percent": latest_metrics.memory_percent,
                    "memory_used_mb": latest_metrics.memory_used_mb,
                    "disk_usage_percent": latest_metrics.disk_usage_percent,
                    "network_io_sent_mb": latest_metrics.network_io_sent_mb,
                    "network_io_recv_mb": latest_metrics.network_io_recv_mb,
                    "active_threads": latest_metrics.active_threads,
                    "open_files": latest_metrics.open_files,
                    "custom_metrics": latest_metrics.custom_metrics
                }

                # 追加写入文件
                with open(self.metrics_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(data) + '\n')

        except Exception as e:
            logger.error(f"保存指标文件失败: {e}")

    def record_component_operation(
        self,
        component_name: str,
        operation_name: str,
        start_time: datetime,
        end_time: datetime,
        success: bool = True,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """记录组件操作指标

        Args:
            component_name: 组件名称
            operation_name: 操作名称
            start_time: 开始时间
            end_time: 结束时间
            success: 是否成功
            error_message: 错误信息
            metadata: 额外元数据
        """
        duration_ms = (end_time - start_time).total_seconds() * 1000

        metrics = ComponentMetrics(
            component_name=component_name,
            operation_name=operation_name,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            success=success,
            error_message=error_message,
            metadata=metadata or {}
        )

        self.metrics_collector.add_component_metrics(metrics)

    def get_current_status(self) -> Dict[str, Any]:
        """获取当前监控状态"""
        latest_metrics = self.metrics_collector.get_latest_system_metrics()

        if not latest_metrics:
            return {
                "status": "stopped",
                "message": "监控未启动或无数据"
            }

        # 判断系统状态
        status = "healthy"
        issues = []

        if latest_metrics.cpu_percent > 90:
            status = "critical"
            issues.append(f"CPU使用率过高: {latest_metrics.cpu_percent:.1f}%")
        elif latest_metrics.cpu_percent > 80:
            status = "warning"
            issues.append(f"CPU使用率较高: {latest_metrics.cpu_percent:.1f}%")

        if latest_metrics.memory_percent > 90:
            status = "critical"
            issues.append(f"内存使用率过高: {latest_metrics.memory_percent:.1f}%")
        elif latest_metrics.memory_percent > 80:
            if status != "critical":
                status = "warning"
            issues.append(f"内存使用率较高: {latest_metrics.memory_percent:.1f}%")

        if latest_metrics.disk_usage_percent > 95:
            status = "critical"
            issues.append(f"磁盘空间不足: {latest_metrics.disk_usage_percent:.1f}%")
        elif latest_metrics.disk_usage_percent > 90:
            if status != "critical":
                status = "warning"
            issues.append(f"磁盘空间较少: {latest_metrics.disk_usage_percent:.1f}%")

        return {
            "status": status,
            "timestamp": latest_metrics.timestamp.isoformat(),
            "metrics": {
                "cpu_percent": latest_metrics.cpu_percent,
                "memory_percent": latest_metrics.memory_percent,
                "memory_used_mb": latest_metrics.memory_used_mb,
                "disk_usage_percent": latest_metrics.disk_usage_percent,
                "disk_free_gb": latest_metrics.disk_free_gb,
                "active_threads": latest_metrics.active_threads,
                "open_files": latest_metrics.open_files
            },
            "issues": issues,
            "running": self._running
        }

    def get_component_performance(
        self,
        component_name: str,
        minutes: int = 60
    ) -> Dict[str, Any]:
        """获取组件性能统计

        Args:
            component_name: 组件名称
            minutes: 统计时间范围（分钟）

        Returns:
            组件性能统计
        """
        metrics_list = self.metrics_collector.get_component_metrics(
            component_name, minutes=minutes
        )

        if not metrics_list:
            return {
                "component": component_name,
                "operations": {},
                "total_operations": 0,
                "success_rate": 0,
                "avg_duration_ms": 0
            }

        # 按操作分组统计
        operations = defaultdict(lambda: {
            "count": 0,
            "success_count": 0,
            "total_duration_ms": 0,
            "min_duration_ms": float('inf'),
            "max_duration_ms": 0
        })

        for metrics in metrics_list:
            op_name = metrics.operation_name
            op_stats = operations[op_name]

            op_stats["count"] += 1
            if metrics.success:
                op_stats["success_count"] += 1

            op_stats["total_duration_ms"] += metrics.duration_ms
            op_stats["min_duration_ms"] = min(op_stats["min_duration_ms"], metrics.duration_ms)
            op_stats["max_duration_ms"] = max(op_stats["max_duration_ms"], metrics.duration_ms)

        # 计算最终统计
        total_operations = len(metrics_list)
        total_success = sum(1 for m in metrics_list if m.success)
        total_duration = sum(m.duration_ms for m in metrics_list)

        operation_stats = {}
        for op_name, stats in operations.items():
            operation_stats[op_name] = {
                "count": stats["count"],
                "success_count": stats["success_count"],
                "success_rate": stats["success_count"] / stats["count"] if stats["count"] > 0 else 0,
                "avg_duration_ms": stats["total_duration_ms"] / stats["count"] if stats["count"] > 0 else 0,
                "min_duration_ms": stats["min_duration_ms"] if stats["min_duration_ms"] != float('inf') else 0,
                "max_duration_ms": stats["max_duration_ms"]
            }

        return {
            "component": component_name,
            "operations": operation_stats,
            "total_operations": total_operations,
            "success_count": total_success,
            "success_rate": total_success / total_operations if total_operations > 0 else 0,
            "avg_duration_ms": total_duration / total_operations if total_operations > 0 else 0
        }


# 装饰器：自动记录组件操作性能
def monitor_performance(component_name: str, operation_name: Optional[str] = None):
    """性能监控装饰器

    Args:
        component_name: 组件名称
        operation_name: 操作名称（可选，默认使用函数名）
    """
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            start_time = datetime.now()
            op_name = operation_name or func.__name__

            try:
                result = await func(*args, **kwargs)
                end_time = datetime.now()

                # 记录成功的操作
                # 这里需要获取全局的PerformanceMonitor实例
                # 为了简化，我们暂时使用一个全局实例
                if hasattr(async_wrapper, '_monitor'):
                    async_wrapper._monitor.record_component_operation(
                        component_name=component_name,
                        operation_name=op_name,
                        start_time=start_time,
                        end_time=end_time,
                        success=True
                    )

                return result

            except Exception as e:
                end_time = datetime.now()

                # 记录失败的操作
                if hasattr(async_wrapper, '_monitor'):
                    async_wrapper._monitor.record_component_operation(
                        component_name=component_name,
                        operation_name=op_name,
                        start_time=start_time,
                        end_time=end_time,
                        success=False,
                        error_message=str(e)
                    )

                raise

        def sync_wrapper(*args, **kwargs):
            start_time = datetime.now()
            op_name = operation_name or func.__name__

            try:
                result = func(*args, **kwargs)
                end_time = datetime.now()

                if hasattr(sync_wrapper, '_monitor'):
                    sync_wrapper._monitor.record_component_operation(
                        component_name=component_name,
                        operation_name=op_name,
                        start_time=start_time,
                        end_time=end_time,
                        success=True
                    )

                return result

            except Exception as e:
                end_time = datetime.now()

                if hasattr(sync_wrapper, '_monitor'):
                    sync_wrapper._monitor.record_component_operation(
                        component_name=component_name,
                        operation_name=op_name,
                        start_time=start_time,
                        end_time=end_time,
                        success=False,
                        error_message=str(e)
                    )

                raise

        # 根据函数类型返回相应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# 全局性能监控实例
_global_monitor: Optional[PerformanceMonitor] = None


def get_global_monitor() -> Optional[PerformanceMonitor]:
    """获取全局性能监控实例"""
    return _global_monitor


def set_global_monitor(monitor: PerformanceMonitor) -> None:
    """设置全局性能监控实例"""
    global _global_monitor
    _global_monitor = monitor


# 自动装饰器：使用全局监控实例
def auto_monitor(component_name: str, operation_name: Optional[str] = None):
    """使用全局监控实例的性能监控装饰器"""
    def decorator(func):
        wrapper = monitor_performance(component_name, operation_name)(func)

        # 如果有全局监控实例，设置到包装器上
        if _global_monitor:
            if asyncio.iscoroutinefunction(func):
                wrapper._monitor = _global_monitor
            else:
                wrapper._monitor = _global_monitor

        return wrapper

    return decorator