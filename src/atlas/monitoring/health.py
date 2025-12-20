"""
系统健康检查模块

提供系统各组件的健康状态检查功能。
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable, Tuple
from dataclasses import dataclass, field
from enum import Enum

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class HealthStatus(Enum):
    """健康状态枚举"""
    HEALTHY = "healthy"
    WARNING = "warning"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """健康检查结果"""
    component: str
    status: HealthStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    check_time: datetime = field(default_factory=datetime.now)
    duration_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class SystemHealth:
    """系统整体健康状态"""
    overall_status: HealthStatus
    component_results: List[HealthCheckResult]
    check_time: datetime
    total_duration_ms: float
    summary: str

    @property
    def healthy_components(self) -> List[HealthCheckResult]:
        """健康的组件列表"""
        return [r for r in self.component_results if r.status == HealthStatus.HEALTHY]

    @property
    def warning_components(self) -> List[HealthCheckResult]:
        """警告状态的组件列表"""
        return [r for r in self.component_results if r.status == HealthStatus.WARNING]

    @property
    def unhealthy_components(self) -> List[HealthCheckResult]:
        """不健康的组件列表"""
        return [r for r in self.component_results if r.status == HealthStatus.UNHEALTHY]

    @property
    def unknown_components(self) -> List[HealthCheckResult]:
        """未知状态的组件列表"""
        return [r for r in self.component_results if r.status == HealthStatus.UNKNOWN]


class HealthChecker:
    """健康检查器"""

    def __init__(self, check_interval: float = 60.0):
        """初始化健康检查器

        Args:
            check_interval: 检查间隔（秒）
        """
        self.check_interval = check_interval
        self.check_functions: Dict[str, Callable[[], HealthCheckResult]] = {}
        self.last_check_time: Optional[datetime] = None
        self.last_health_result: Optional[SystemHealth] = None
        self._running = False
        self._check_task: Optional[asyncio.Task] = None

    def register_check(
        self,
        component_name: str,
        check_func: Callable[[], HealthCheckResult]
    ) -> None:
        """注册健康检查函数

        Args:
            component_name: 组件名称
            check_func: 检查函数，返回HealthCheckResult
        """
        self.check_functions[component_name] = check_func
        logger.info(f"注册健康检查函数: {component_name}")

    def unregister_check(self, component_name: str) -> None:
        """注销健康检查函数

        Args:
            component_name: 组件名称
        """
        if component_name in self.check_functions:
            del self.check_functions[component_name]
            logger.info(f"注销健康检查函数: {component_name}")

    async def check_all_components(self) -> SystemHealth:
        """检查所有组件的健康状态

        Returns:
            系统整体健康状态
        """
        start_time = time.time()
        component_results = []

        for component_name, check_func in self.check_functions.items():
            try:
                component_start = time.time()
                result = check_func()
                if asyncio.iscoroutinefunction(check_func):
                    result = await check_func()
                component_end = time.time()

                # 确保结果包含必要信息
                if not isinstance(result, HealthCheckResult):
                    # 如果返回的不是HealthCheckResult，尝试转换
                    if isinstance(result, tuple) and len(result) >= 2:
                        status, message = result[0], result[1]
                        details = result[2] if len(result) > 2 else {}
                        result = HealthCheckResult(
                            component=component_name,
                            status=status,
                            message=message,
                            details=details
                        )
                    else:
                        result = HealthCheckResult(
                            component=component_name,
                            status=HealthStatus.UNKNOWN,
                            message="Invalid check result format"
                        )

                result.component = component_name
                result.check_time = datetime.now()
                result.duration_ms = (component_end - component_start) * 1000

                component_results.append(result)

            except Exception as e:
                error_result = HealthCheckResult(
                    component=component_name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Health check failed: {str(e)}",
                    error=str(e),
                    check_time=datetime.now()
                )
                component_results.append(error_result)

                logger.error(f"组件 {component_name} 健康检查失败: {e}")

        end_time = time.time()
        total_duration_ms = (end_time - start_time) * 1000

        # 计算整体健康状态
        overall_status, summary = self._calculate_overall_status(component_results)

        system_health = SystemHealth(
            overall_status=overall_status,
            component_results=component_results,
            check_time=datetime.now(),
            total_duration_ms=total_duration_ms,
            summary=summary
        )

        self.last_check_time = datetime.now()
        self.last_health_result = system_health

        return system_health

    def _calculate_overall_status(
        self,
        component_results: List[HealthCheckResult]
    ) -> Tuple[HealthStatus, str]:
        """计算整体健康状态

        Args:
            component_results: 组件检查结果列表

        Returns:
            (整体状态, 摘要信息)
        """
        if not component_results:
            return HealthStatus.UNKNOWN, "No components to check"

        status_counts = {
            HealthStatus.HEALTHY: 0,
            HealthStatus.WARNING: 0,
            HealthStatus.UNHEALTHY: 0,
            HealthStatus.UNKNOWN: 0
        }

        for result in component_results:
            status_counts[result.status] += 1

        total_components = len(component_results)
        unhealthy_count = status_counts[HealthStatus.UNHEALTHY]
        warning_count = status_counts[HealthStatus.WARNING]
        healthy_count = status_counts[HealthStatus.HEALTHY]

        # 判断整体状态
        if unhealthy_count > 0:
            overall_status = HealthStatus.UNHEALTHY
            summary = f"System unhealthy: {unhealthy_count}/{total_components} components unhealthy"
        elif warning_count > 0:
            overall_status = HealthStatus.WARNING
            summary = f"System warning: {warning_count}/{total_components} components have warnings"
        elif healthy_count == total_components:
            overall_status = HealthStatus.HEALTHY
            summary = f"All {total_components} components healthy"
        else:
            overall_status = HealthStatus.UNKNOWN
            summary = f"System status unknown: {status_counts[HealthStatus.UNKNOWN]}/{total_components} components unknown"

        return overall_status, summary

    async def start_monitoring(self) -> None:
        """开始持续监控

        Returns:
            None
        """
        if self._running:
            logger.warning("健康检查监控已经在运行")
            return

        self._running = True
        logger.info(f"启动健康检查监控，检查间隔: {self.check_interval}秒")

        self._check_task = asyncio.create_task(self._monitoring_loop())

    async def stop_monitoring(self) -> None:
        """停止持续监控

        Returns:
            None
        """
        if not self._running:
            return

        self._running = False
        logger.info("停止健康检查监控")

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _monitoring_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                await self.check_all_components()
            except Exception as e:
                logger.error(f"健康检查监控失败: {e}")

            await asyncio.sleep(self.check_interval)

    def get_last_health(self) -> Optional[SystemHealth]:
        """获取最后的健康检查结果

        Returns:
            最后的健康检查结果，如果没有则返回None
        """
        return self.last_health_result


# 预定义的健康检查函数
class HealthChecks:
    """预定义的健康检查函数集合"""

    @staticmethod
    async def database_health_check(db_manager) -> HealthCheckResult:
        """数据库健康检查"""
        try:
            start_time = time.time()
            # 执行简单查询测试连接
            await db_manager.execute_query("SELECT 1")
            duration_ms = (time.time() - start_time) * 1000

            # 获取连接池状态
            pool_status = await db_manager.get_pool_status() if hasattr(db_manager, 'get_pool_status') else {}

            return HealthCheckResult(
                component="database",
                status=HealthStatus.HEALTHY,
                message="Database connection healthy",
                details={
                    "query_duration_ms": duration_ms,
                    "pool_status": pool_status
                }
            )

        except Exception as e:
            return HealthCheckResult(
                component="database",
                status=HealthStatus.UNHEALTHY,
                message=f"Database connection failed: {str(e)}",
                error=str(e)
            )

    @staticmethod
    async def llm_service_health_check(llm_client) -> HealthCheckResult:
        """LLM服务健康检查"""
        try:
            start_time = time.time()
            health_info = await llm_client.health_check()
            duration_ms = (time.time() - start_time) * 1000

            if health_info.get("status") == "healthy":
                return HealthCheckResult(
                    component="llm_service",
                    status=HealthStatus.HEALTHY,
                    message="LLM service healthy",
                    details={
                        "response_time_ms": duration_ms,
                        "available_models": health_info.get("models_count", 0),
                        "service_info": health_info
                    }
                )
            else:
                return HealthCheckResult(
                    component="llm_service",
                    status=HealthStatus.WARNING,
                    message=f"LLM service degraded: {health_info.get('error', 'Unknown error')}",
                    details={
                        "response_time_ms": duration_ms,
                        "service_info": health_info
                    }
                )

        except Exception as e:
            return HealthCheckResult(
                component="llm_service",
                status=HealthStatus.UNHEALTHY,
                message=f"LLM service check failed: {str(e)}",
                error=str(e)
            )

    @staticmethod
    async def storage_health_check(storage_manager) -> HealthCheckResult:
        """存储系统健康检查"""
        try:
            start_time = time.time()

            # 测试写入和读取
            test_file_path = storage_manager.data_dir / ".health_check_test"
            test_content = f"Health check test at {datetime.now().isoformat()}"

            await storage_manager.save_raw_content(test_file_path, test_content)
            read_content = await storage_manager.load_raw_content(test_file_path)

            # 清理测试文件
            test_file_path.unlink(missing_ok=True)

            duration_ms = (time.time() - start_time) * 1000

            if read_content == test_content:
                return HealthCheckResult(
                    component="storage",
                    status=HealthStatus.HEALTHY,
                    message="Storage system healthy",
                    details={
                        "read_write_duration_ms": duration_ms,
                        "data_dir": str(storage_manager.data_dir),
                        "free_space_gb": storage_manager.get_free_space() if hasattr(storage_manager, 'get_free_space') else None
                    }
                )
            else:
                return HealthCheckResult(
                    component="storage",
                    status=HealthStatus.UNHEALTHY,
                    message="Storage read/write test failed"
                )

        except Exception as e:
            return HealthCheckResult(
                component="storage",
                status=HealthStatus.UNHEALTHY,
                message=f"Storage health check failed: {str(e)}",
                error=str(e)
            )

    @staticmethod
    async def task_queue_health_check(task_queue) -> HealthCheckResult:
        """任务队列健康检查"""
        try:
            start_time = time.time()

            # 获取队列状态
            queue_status = task_queue.get_queue_status() if hasattr(task_queue, 'get_queue_status') else {}

            # 检查队列是否响应
            is_running = getattr(task_queue, 'running', False)

            duration_ms = (time.time() - start_time) * 1000

            if is_running:
                return HealthCheckResult(
                    component="task_queue",
                    status=HealthStatus.HEALTHY,
                    message="Task queue running normally",
                    details={
                        "response_time_ms": duration_ms,
                        "queue_status": queue_status,
                        "running": is_running
                    }
                )
            else:
                return HealthCheckResult(
                    component="task_queue",
                    status=HealthStatus.WARNING,
                    message="Task queue not running",
                    details={
                        "response_time_ms": duration_ms,
                        "queue_status": queue_status,
                        "running": is_running
                    }
                )

        except Exception as e:
            return HealthCheckResult(
                component="task_queue",
                status=HealthStatus.UNHEALTHY,
                message=f"Task queue health check failed: {str(e)}",
                error=str(e)
            )

    @staticmethod
    async def http_client_health_check(http_client) -> HealthCheckResult:
        """HTTP客户端健康检查"""
        try:
            start_time = time.time()

            # 测试HTTP请求到一个可靠的URL
            test_url = "https://httpbin.org/status/200"
            response = await http_client.get(test_url)

            duration_ms = (time.time() - start_time) * 1000

            if response and response.status_code == 200:
                return HealthCheckResult(
                    component="http_client",
                    status=HealthStatus.HEALTHY,
                    message="HTTP client working normally",
                    details={
                        "response_time_ms": duration_ms,
                        "test_url": test_url,
                        "status_code": response.status_code,
                        "cache_enabled": getattr(http_client, 'cache_enabled', False)
                    }
                )
            else:
                return HealthCheckResult(
                    component="http_client",
                    status=HealthStatus.WARNING,
                    message=f"HTTP client test failed: status {response.status_code if response else 'None'}",
                    details={
                        "response_time_ms": duration_ms,
                        "test_url": test_url,
                        "status_code": response.status_code if response else None
                    }
                )

        except Exception as e:
            return HealthCheckResult(
                component="http_client",
                status=HealthStatus.WARNING,
                message=f"HTTP client health check failed: {str(e)}",
                error=str(e)
            )

    @staticmethod
    def system_resource_health_check(
        cpu_threshold: float = 80.0,
        memory_threshold: float = 85.0,
        disk_threshold: float = 90.0
    ) -> HealthCheckResult:
        """系统资源健康检查"""
        try:
            import psutil

            # CPU使用率
            cpu_percent = psutil.cpu_percent(interval=0.1)

            # 内存使用率
            memory = psutil.virtual_memory()
            memory_percent = memory.percent

            # 磁盘使用率
            disk = psutil.disk_usage('/')
            disk_percent = disk.percent

            details = {
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "memory_used_gb": memory.used / (1024**3),
                "memory_total_gb": memory.total / (1024**3),
                "disk_percent": disk_percent,
                "disk_free_gb": disk.free / (1024**3),
                "disk_total_gb": disk.total / (1024**3)
            }

            # 判断健康状态
            issues = []
            status = HealthStatus.HEALTHY

            if cpu_percent > cpu_threshold:
                status = HealthStatus.WARNING if status == HealthStatus.HEALTHY else HealthStatus.UNHEALTHY
                issues.append(f"High CPU usage: {cpu_percent:.1f}%")

            if memory_percent > memory_threshold:
                status = HealthStatus.WARNING if status == HealthStatus.HEALTHY else HealthStatus.UNHEALTHY
                issues.append(f"High memory usage: {memory_percent:.1f}%")

            if disk_percent > disk_threshold:
                status = HealthStatus.WARNING if status == HealthStatus.HEALTHY else HealthStatus.UNHEALTHY
                issues.append(f"Low disk space: {disk_percent:.1f}% used")

            message = "System resources healthy" if not issues else "; ".join(issues)

            return HealthCheckResult(
                component="system_resources",
                status=status,
                message=message,
                details=details
            )

        except Exception as e:
            return HealthCheckResult(
                component="system_resources",
                status=HealthStatus.UNKNOWN,
                message=f"System resource check failed: {str(e)}",
                error=str(e)
            )


# 全局健康检查实例
_global_health_checker: Optional[HealthChecker] = None


def get_global_health_checker() -> Optional[HealthChecker]:
    """获取全局健康检查实例"""
    return _global_health_checker


def set_global_health_checker(checker: HealthChecker) -> None:
    """设置全局健康检查实例"""
    global _global_health_checker
    _global_health_checker = checker