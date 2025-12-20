"""
自适应控制器

根据系统负载动态调整 LLM 调用频率，防止过载和资源耗尽。
实现智能降速、恢复策略和性能监控。
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timezone, timedelta
import json
import threading
from collections import deque

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class ControlAction(Enum):
    """控制动作"""
    INCREASE_RATE = "increase_rate"    # 增加速度
    MAINTAIN_RATE = "maintain_rate"    # 保持速度
    DECREASE_RATE = "decrease_rate"    # 降低速度
    PAUSE_PROCESSING = "pause_processing"  # 暂停处理
    EMERGENCY_STOP = "emergency_stop"  # 紧急停止


class SystemMetric(Enum):
    """系统指标"""
    CPU_USAGE = "cpu_usage"
    MEMORY_USAGE = "memory_usage"
    DISK_USAGE = "disk_usage"
    GPU_USAGE = "gpu_usage"
    GPU_MEMORY = "gpu_memory"
    NETWORK_LATENCY = "network_latency"
    REQUEST_RATE = "request_rate"
    ERROR_RATE = "error_rate"
    RESPONSE_TIME = "response_time"
    QUEUE_SIZE = "queue_size"
    CONCURRENT_TASKS = "concurrent_tasks"


@dataclass
class MetricThreshold:
    """指标阈值"""
    metric: SystemMetric
    warning_threshold: float      # 警告阈值
    critical_threshold: float     # 严重阈值
    emergency_threshold: float     # 紧急阈值
    weight: float = 1.0           # 权重

    def __post_init__(self) -> None:
        """验证阈值"""
        if not (0 <= self.warning_threshold <= self.critical_threshold <= self.emergency_threshold <= 100):
            raise ValueError(f"阈值设置错误: {self.metric}")


@dataclass
class ControllerConfig:
    """控制器配置"""
    # 监控配置
    monitoring_interval: float = 5.0          # 监控间隔（秒）
    history_window: int = 100                 # 历史数据窗口大小
    smoothing_factor: float = 0.1            # 平滑因子

    # 控制配置
    min_concurrent_tasks: int = 1            # 最小并发任务数
    max_concurrent_tasks: int = 10           # 最大并发任务数
    base_request_rate: float = 1.0           # 基础请求率（请求/秒）

    # 阈值配置
    cpu_threshold: MetricThreshold = field(default_factory=lambda: MetricThreshold(
        SystemMetric.CPU_USAGE, 70.0, 80.0, 90.0, 0.3
    ))
    memory_threshold: MetricThreshold = field(default_factory=lambda: MetricThreshold(
        SystemMetric.MEMORY_USAGE, 75.0, 85.0, 95.0, 0.2
    ))
    error_rate_threshold: MetricThreshold = field(default_factory=lambda: MetricThreshold(
        SystemMetric.ERROR_RATE, 5.0, 10.0, 20.0, 0.3
    ))
    response_time_threshold: MetricThreshold = field(default_factory=lambda: MetricThreshold(
        SystemMetric.RESPONSE_TIME, 5.0, 10.0, 30.0, 0.2
    ))

    # 控制策略
    enable_auto_scaling: bool = True          # 启用自动扩缩容
    enable_predictive_control: bool = False  # 启用预测控制
    cooldown_period: float = 30.0            # 冷却期（秒）

    # 安全配置
    emergency_stop_enabled: bool = True      # 启用紧急停止
    circuit_breaker_enabled: bool = True     # 启用熔断器
    circuit_breaker_threshold: int = 10      # 熔断器阈值


@dataclass
class MetricSnapshot:
    """指标快照"""
    timestamp: datetime
    metrics: Dict[SystemMetric, float]
    control_state: str


class AdaptiveController:
    """自适应控制器"""

    def __init__(
        self,
        config: ControllerConfig,
        llm_client=None,
        queue_manager=None
    ):
        """初始化自适应控制器

        Args:
            config: 控制器配置
            llm_client: LLM客户端
            queue_manager: 队列管理器
        """
        self.config = config
        self.llm_client = llm_client
        self.queue_manager = queue_manager

        # 状态管理
        self._running = False
        self._control_task: Optional[asyncio.Task] = None
        self._lock = threading.RLock()

        # 指标收集
        self._current_metrics: Dict[SystemMetric, float] = {}
        self._metric_history: deque = deque(maxlen=config.history_window)
        self._thresholds = {
            SystemMetric.CPU_USAGE: config.cpu_threshold,
            SystemMetric.MEMORY_USAGE: config.memory_threshold,
            SystemMetric.ERROR_RATE: config.error_rate_threshold,
            SystemMetric.RESPONSE_TIME: config.response_time_threshold,
        }

        # 控制状态
        self._current_concurrent_tasks = config.min_concurrent_tasks
        self._current_request_rate = config.base_request_rate
        self._last_control_action: Optional[ControlAction] = None
        self._last_control_time: Optional[datetime] = None
        self._control_actions_history: deque = deque(maxlen=50)

        # 熔断器状态
        self._circuit_breaker_active = False
        self._circuit_breaker_failures = 0
        self._circuit_breaker_last_failure: Optional[datetime] = None

        # 统计信息
        self._stats = {
            "total_control_actions": 0,
            "emergency_stops": 0,
            "circuit_breaker_activations": 0,
            "auto_scaling_events": 0,
            "average_response_time": 0.0,
            "peak_concurrent_tasks": 0,
        }

        logger.info("自适应控制器初始化完成")

    async def start(self) -> None:
        """启动控制器"""
        if self._running:
            return

        self._running = True
        self._control_task = asyncio.create_task(self._control_loop())
        logger.info("自适应控制器已启动")

    async def stop(self) -> None:
        """停止控制器"""
        if not self._running:
            return

        self._running = False

        if self._control_task:
            self._control_task.cancel()
            try:
                await self._control_task
            except asyncio.CancelledError:
                pass

        logger.info("自适应控制器已停止")

    async def _control_loop(self) -> None:
        """控制主循环"""
        logger.info("自适应控制循环已启动")

        while self._running:
            try:
                # 收集指标
                await self._collect_metrics()

                # 分析系统状态
                system_state = self._analyze_system_state()

                # 决定控制动作
                control_action = self._decide_control_action(system_state)

                # 执行控制动作
                if control_action:
                    await self._execute_control_action(control_action)

                # 记录快照
                snapshot = MetricSnapshot(
                    timestamp=datetime.now(timezone.utc),
                    metrics=self._current_metrics.copy(),
                    control_state=system_state
                )
                self._metric_history.append(snapshot)

                # 等待下一次监控
                await asyncio.sleep(self.config.monitoring_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"控制循环异常: {e}")
                await asyncio.sleep(self.config.monitoring_interval)

        logger.info("自适应控制循环已停止")

    async def _collect_metrics(self) -> None:
        """收集系统指标"""
        try:
            # CPU和内存使用率
            cpu_usage, memory_usage = await self._get_system_resources()
            self._current_metrics[SystemMetric.CPU_USAGE] = cpu_usage
            self._current_metrics[SystemMetric.MEMORY_USAGE] = memory_usage

            # GPU指标（如果可用）
            gpu_usage, gpu_memory = await self._get_gpu_metrics()
            if gpu_usage is not None:
                self._current_metrics[SystemMetric.GPU_USAGE] = gpu_usage
            if gpu_memory is not None:
                self._current_metrics[SystemMetric.GPU_MEMORY] = gpu_memory

            # 从LLM客户端获取统计
            if self.llm_client:
                llm_stats = self.llm_client.get_stats()
                if llm_stats.get("total_requests", 0) > 0:
                    error_rate = (llm_stats.get("failed_requests", 0) /
                                llm_stats.get("total_requests", 1)) * 100
                    avg_response_time = llm_stats.get("average_duration", 0) * 1000  # 转换为毫秒

                    self._current_metrics[SystemMetric.ERROR_RATE] = error_rate
                    self._current_metrics[SystemMetric.RESPONSE_TIME] = avg_response_time

            # 从队列管理器获取统计
            if self.queue_manager:
                queue_status = self.queue_manager.get_queue_status()
                self._current_metrics[SystemMetric.QUEUE_SIZE] = queue_status.get("pending_tasks", 0)
                self._current_metrics[SystemMetric.CONCURRENT_TASKS] = queue_status.get("running_tasks", 0)

        except Exception as e:
            logger.error(f"收集系统指标失败: {e}")

    async def _get_system_resources(self) -> tuple[float, float]:
        """获取系统资源使用率"""
        try:
            import psutil

            # CPU使用率
            cpu_usage = psutil.cpu_percent(interval=1)

            # 内存使用率
            memory = psutil.virtual_memory()
            memory_usage = memory.percent

            return cpu_usage, memory_usage

        except ImportError:
            logger.warning("psutil未安装，无法获取系统资源信息")
            return 0.0, 0.0
        except Exception as e:
            logger.error(f"获取系统资源失败: {e}")
            return 0.0, 0.0

    async def _get_gpu_metrics(self) -> tuple[Optional[float], Optional[float]]:
        """获取GPU指标"""
        try:
            import GPUtil

            gpus = GPUtil.getGPUs()
            if gpus:
                # 返回第一个GPU的使用率
                return gpus[0].load * 100, gpus[0].memoryUtil * 100

        except ImportError:
            # GPUtil未安装，尝试nvidia-ml-py
            try:
                import pynvml

                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)

                gpu_usage = util.gpu
                gpu_memory = (memory_info.used / memory_info.total) * 100
                return gpu_usage, gpu_memory

            except Exception:
                pass

        except Exception as e:
            logger.debug(f"获取GPU指标失败: {e}")

        return None, None

    def _analyze_system_state(self) -> str:
        """分析系统状态"""
        total_score = 0.0
        total_weight = 0.0

        for metric, threshold in self._thresholds.items():
            if metric in self._current_metrics:
                value = self._current_metrics[metric]
                score = self._calculate_metric_score(value, threshold)
                total_score += score * threshold.weight
                total_weight += threshold.weight

        if total_weight > 0:
            overall_score = total_score / total_weight
        else:
            overall_score = 0.0

        # 根据综合评分确定系统状态
        if overall_score >= 0.8:
            return "critical"
        elif overall_score >= 0.6:
            return "warning"
        elif overall_score >= 0.3:
            return "normal"
        else:
            return "healthy"

    def _calculate_metric_score(self, value: float, threshold: MetricThreshold) -> float:
        """计算指标评分"""
        if value >= threshold.emergency_threshold:
            return 1.0
        elif value >= threshold.critical_threshold:
            return 0.8
        elif value >= threshold.warning_threshold:
            return 0.6
        else:
            return 0.0

    def _decide_control_action(self, system_state: str) -> Optional[ControlAction]:
        """决定控制动作"""
        # 检查冷却期
        if self._last_control_time:
            time_since_last = (datetime.now(timezone.utc) - self._last_control_time).total_seconds()
            if time_since_last < self.config.cooldown_period:
                return None

        # 检查熔断器
        if self._circuit_breaker_active:
            # 尝试恢复熔断器
            if self._should_reset_circuit_breaker():
                self._reset_circuit_breaker()
                return ControlAction.INCREASE_RATE
            return None

        # 根据系统状态决定动作
        if system_state == "critical":
            if self.config.emergency_stop_enabled:
                return ControlAction.EMERGENCY_STOP
            return ControlAction.DECREASE_RATE

        elif system_state == "warning":
            return ControlAction.DECREASE_RATE

        elif system_state == "normal":
            return ControlAction.MAINTAIN_RATE

        else:  # healthy
            # 检查是否可以增加并发
            if (self._current_concurrent_tasks < self.config.max_concurrent_tasks and
                not self._recently_decreased_rate()):
                return ControlAction.INCREASE_RATE
            return ControlAction.MAINTAIN_RATE

    async def _execute_control_action(self, action: ControlAction) -> None:
        """执行控制动作"""
        with self._lock:
            logger.info(f"执行控制动作: {action.value}")

            self._last_control_action = action
            self._last_control_time = datetime.now(timezone.utc)
            self._stats["total_control_actions"] += 1

            if action == ControlAction.EMERGENCY_STOP:
                await self._emergency_stop()
                self._stats["emergency_stops"] += 1

            elif action == ControlAction.DECREASE_RATE:
                await self._decrease_rate()

            elif action == ControlAction.INCREASE_RATE:
                await self._increase_rate()
                self._stats["auto_scaling_events"] += 1

            # 记录动作历史
            self._control_actions_history.append({
                "action": action.value,
                "timestamp": self._last_control_time.isoformat(),
                "concurrent_tasks": self._current_concurrent_tasks,
                "request_rate": self._current_request_rate,
                "metrics": self._current_metrics.copy(),
            })

    async def _emergency_stop(self) -> None:
        """紧急停止"""
        logger.warning("执行紧急停止，暂停所有LLM处理")

        # 停止队列管理器
        if self.queue_manager:
            # 实现暂停逻辑（这里需要队列管理器支持暂停功能）
            pass

        # 降低并发到最小值
        self._current_concurrent_tasks = self.config.min_concurrent_tasks

    async def _decrease_rate(self) -> None:
        """降低速度"""
        # 减少并发任务数
        new_concurrent = max(
            self.config.min_concurrent_tasks,
            int(self._current_concurrent_tasks * 0.7)
        )
        self._current_concurrent_tasks = new_concurrent

        # 降低请求率
        self._current_request_rate = max(
            0.1,  # 最低请求率
            self._current_request_rate * 0.8
        )

        logger.info(f"降低处理速度: 并发任务={new_concurrent}, 请求率={self._current_request_rate:.2f}/s")

    async def _increase_rate(self) -> None:
        """增加速度"""
        # 增加并发任务数
        new_concurrent = min(
            self.config.max_concurrent_tasks,
            int(self._current_concurrent_tasks * 1.2)
        )
        self._current_concurrent_tasks = new_concurrent

        # 增加请求率
        self._current_request_rate = min(
            self.config.base_request_rate * 2,  # 最高为基础速率的2倍
            self._current_request_rate * 1.1
        )

        logger.info(f"增加处理速度: 并发任务={new_concurrent}, 请求率={self._current_request_rate:.2f}/s")

    def _recently_decreased_rate(self) -> bool:
        """检查最近是否降低了速度"""
        if not self._control_actions_history:
            return False

        # 检查最近5分钟内是否有降低速度的动作
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        for action in reversed(self._control_actions_history):
            if datetime.fromisoformat(action["timestamp"]) > recent_time:
                if action["action"] in [ControlAction.DECREASE_RATE.value, ControlAction.EMERGENCY_STOP.value]:
                    return True
            else:
                break

        return False

    def _should_reset_circuit_breaker(self) -> bool:
        """检查是否应该重置熔断器"""
        if not self._circuit_breaker_last_failure:
            return False

        # 熔断器恢复时间：5分钟
        recovery_time = timedelta(minutes=5)
        return (datetime.now(timezone.utc) - self._circuit_breaker_last_failure) > recovery_time

    def _reset_circuit_breaker(self) -> None:
        """重置熔断器"""
        logger.info("熔断器已重置，恢复处理")
        self._circuit_breaker_active = False
        self._circuit_breaker_failures = 0
        self._circuit_breaker_last_failure = None

    def get_status(self) -> Dict[str, Any]:
        """获取控制器状态"""
        with self._lock:
            return {
                "running": self._running,
                "current_concurrent_tasks": self._current_concurrent_tasks,
                "current_request_rate": self._current_request_rate,
                "last_control_action": self._last_control_action.value if self._last_control_action else None,
                "last_control_time": self._last_control_time.isoformat() if self._last_control_time else None,
                "circuit_breaker_active": self._circuit_breaker_active,
                "circuit_breaker_failures": self._circuit_breaker_failures,
                "current_metrics": {k.value: v for k, v in self._current_metrics.items()},
                "system_state": self._analyze_system_state(),
                "stats": self._stats.copy(),
                "config": {
                    "min_concurrent_tasks": self.config.min_concurrent_tasks,
                    "max_concurrent_tasks": self.config.max_concurrent_tasks,
                    "base_request_rate": self.config.base_request_rate,
                    "monitoring_interval": self.config.monitoring_interval,
                }
            }

    def get_metrics_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取指标历史"""
        history = list(self._metric_history)
        if limit:
            history = history[-limit:]

        return [
            {
                "timestamp": snapshot.timestamp.isoformat(),
                "metrics": {k.value: v for k, v in snapshot.metrics.items()},
                "control_state": snapshot.control_state,
            }
            for snapshot in history
        ]

    def reset_stats(self) -> None:
        """重置统计信息"""
        self._stats = {
            "total_control_actions": 0,
            "emergency_stops": 0,
            "circuit_breaker_activations": 0,
            "auto_scaling_events": 0,
            "average_response_time": 0.0,
            "peak_concurrent_tasks": 0,
        }

    def update_config(self, **kwargs) -> None:
        """更新配置"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
                logger.info(f"配置已更新: {key} = {value}")
            else:
                logger.warning(f"未知配置项: {key}")


# 便利函数
def create_adaptive_controller(
    min_concurrent_tasks: int = 1,
    max_concurrent_tasks: int = 10,
    base_request_rate: float = 1.0,
    llm_client=None,
    queue_manager=None,
    **kwargs
) -> AdaptiveController:
    """创建自适应控制器

    Args:
        min_concurrent_tasks: 最小并发任务数
        max_concurrent_tasks: 最大并发任务数
        base_request_rate: 基础请求率
        llm_client: LLM客户端
        queue_manager: 队列管理器
        **kwargs: 其他配置参数

    Returns:
        自适应控制器实例
    """
    config = ControllerConfig(
        min_concurrent_tasks=min_concurrent_tasks,
        max_concurrent_tasks=max_concurrent_tasks,
        base_request_rate=base_request_rate,
        **kwargs
    )
    return AdaptiveController(config, llm_client, queue_manager)