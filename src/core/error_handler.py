"""
增强的错误处理模块

提供统一的错误处理、重试机制、错误分类和恢复策略。
"""

import asyncio
import functools
import logging
import traceback
from datetime import datetime, timedelta
from typing import (
    Any, Callable, Dict, List, Optional, Type, TypeVar, Union,
    Awaitable, Tuple
)
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import json

from atlas.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


class ErrorSeverity(Enum):
    """错误严重程度"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ErrorCategory(Enum):
    """错误分类"""
    NETWORK = "network"
    DATABASE = "database"
    FILE_SYSTEM = "file_system"
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONFIGURATION = "configuration"
    MEMORY = "memory"
    CPU = "cpu"
    EXTERNAL_SERVICE = "external_service"
    BUSINESS_LOGIC = "business_logic"
    UNKNOWN = "unknown"


@dataclass
class ErrorInfo:
    """错误信息"""
    error_id: str
    category: ErrorCategory
    severity: ErrorSeverity
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    traceback_info: Optional[str] = None
    component: Optional[str] = None
    operation: Optional[str] = None
    user_id: Optional[str] = None
    request_id: Optional[str] = None
    retry_count: int = 0
    resolved: bool = False
    resolution_time: Optional[datetime] = None


@dataclass
class RetryConfig:
    """重试配置"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: List[Type[Exception]] = field(default_factory=list)
    non_retryable_exceptions: List[Type[Exception]] = field(default_factory=list)


class ErrorClassifier:
    """错误分类器"""

    def __init__(self):
        """初始化错误分类器"""
        self.classification_rules = self._init_default_rules()

    def _init_default_rules(self) -> Dict[Type[Exception], Tuple[ErrorCategory, ErrorSeverity]]:
        """初始化默认分类规则"""
        return {
            # 网络相关错误
            ConnectionError: (ErrorCategory.NETWORK, ErrorSeverity.MEDIUM),
            TimeoutError: (ErrorCategory.TIMEOUT, ErrorSeverity.MEDIUM),
            OSError: (ErrorCategory.FILE_SYSTEM, ErrorSeverity.MEDIUM),

            # 数据库相关错误
            sqlalchemy.exc.OperationalError: (ErrorCategory.DATABASE, ErrorSeverity.HIGH),
            sqlite3.OperationalError: (ErrorCategory.DATABASE, ErrorSeverity.HIGH),

            # HTTP相关错误
            requests.exceptions.ConnectionError: (ErrorCategory.NETWORK, ErrorSeverity.MEDIUM),
            requests.exceptions.Timeout: (ErrorCategory.TIMEOUT, ErrorSeverity.MEDIUM),
            requests.exceptions.HTTPError: (ErrorCategory.EXTERNAL_SERVICE, ErrorSeverity.LOW),

            # 认证授权错误
            PermissionError: (ErrorCategory.AUTHORIZATION, ErrorSeverity.HIGH),
            AuthenticationError: (ErrorCategory.AUTHENTICATION, ErrorSeverity.HIGH),

            # 验证错误
            ValueError: (ErrorCategory.VALIDATION, ErrorSeverity.LOW),
            TypeError: (ErrorCategory.VALIDATION, ErrorSeverity.LOW),
            pydantic.ValidationError: (ErrorCategory.VALIDATION, ErrorSeverity.LOW),

            # 配置错误
            FileNotFoundError: (ErrorCategory.CONFIGURATION, ErrorSeverity.HIGH),
            KeyError: (ErrorCategory.CONFIGURATION, ErrorSeverity.MEDIUM),

            # 内存错误
            MemoryError: (ErrorCategory.MEMORY, ErrorSeverity.CRITICAL),
        }

    def classify_error(
        self,
        exception: Exception,
        component: Optional[str] = None,
        operation: Optional[str] = None
    ) -> ErrorInfo:
        """分类错误

        Args:
            exception: 异常对象
            component: 组件名称
            operation: 操作名称

        Returns:
            错误信息对象
        """
        exception_type = type(exception)

        # 查找匹配的分类规则
        category, severity = ErrorCategory.UNKNOWN, ErrorSeverity.MEDIUM
        for exc_type, (cat, sev) in self.classification_rules.items():
            if issubclass(exception_type, exc_type):
                category, severity = cat, sev
                break

        # 生成错误ID
        error_id = f"{category.value}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(exception)}"

        # 提取traceback信息
        traceback_info = traceback.format_exc()

        return ErrorInfo(
            error_id=error_id,
            category=category,
            severity=severity,
            message=str(exception),
            details={
                "exception_type": exception_type.__name__,
                "exception_module": exception_type.__module__
            },
            traceback_info=traceback_info,
            component=component,
            operation=operation
        )


class RetryManager:
    """重试管理器"""

    def __init__(self, default_config: Optional[RetryConfig] = None):
        """初始化重试管理器"""
        self.default_config = default_config or RetryConfig()
        self.config_registry: Dict[str, RetryConfig] = {}

    def register_config(self, name: str, config: RetryConfig) -> None:
        """注册重试配置

        Args:
            name: 配置名称
            config: 重试配置
        """
        self.config_registry[name] = config

    def get_config(self, name: Optional[str] = None) -> RetryConfig:
        """获取重试配置

        Args:
            name: 配置名称

        Returns:
            重试配置
        """
        if name and name in self.config_registry:
            return self.config_registry[name]
        return self.default_config

    async def execute_with_retry(
        self,
        func: Callable[..., Awaitable[T] or T],
        config_name: Optional[str] = None,
        *args,
        **kwargs
    ) -> T:
        """执行带重试的函数

        Args:
            func: 要执行的函数
            config_name: 重试配置名称
            *args: 函数参数
            **kwargs: 函数关键字参数

        Returns:
            函数执行结果

        Raises:
            最后一次执行的异常
        """
        config = self.get_config(config_name)

        for attempt in range(config.max_attempts):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)

            except Exception as e:
                # 检查是否为不可重试异常
                for non_retryable in config.non_retryable_exceptions:
                    if isinstance(e, non_retryable):
                        logger.error(f"Non-retryable exception occurred: {e}")
                        raise

                # 检查是否为可重试异常
                if config.retryable_exceptions:
                    is_retryable = any(isinstance(e, exc) for exc in config.retryable_exceptions)
                    if not is_retryable:
                        logger.error(f"Non-retryable exception: {e}")
                        raise

                # 最后一次尝试，直接抛出异常
                if attempt == config.max_attempts - 1:
                    logger.error(f"Final attempt failed after {config.max_attempts} attempts: {e}")
                    raise

                # 计算延迟时间
                delay = self._calculate_delay(attempt, config)
                logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.2f}s...")

                await asyncio.sleep(delay)

    def _calculate_delay(self, attempt: int, config: RetryConfig) -> float:
        """计算重试延迟时间

        Args:
            attempt: 当前尝试次数（从0开始）
            config: 重试配置

        Returns:
            延迟时间（秒）
        """
        delay = config.base_delay * (config.exponential_base ** attempt)
        delay = min(delay, config.max_delay)

        if config.jitter:
            import random
            # 添加±20%的随机抖动
            jitter_factor = 0.8 + random.random() * 0.4
            delay *= jitter_factor

        return delay


class ErrorHandler:
    """增强的错误处理器"""

    def __init__(
        self,
        error_log_file: Optional[Path] = None,
        enable_retry: bool = True,
        enable_classification: bool = True
    ):
        """初始化错误处理器

        Args:
            error_log_file: 错误日志文件路径
            enable_retry: 是否启用重试机制
            enable_classification: 是否启用错误分类
        """
        self.error_log_file = error_log_file
        self.enable_retry = enable_retry
        self.enable_classification = enable_classification

        self.classifier = ErrorClassifier() if enable_classification else None
        self.retry_manager = RetryManager() if enable_retry else None

        self.error_history: List[ErrorInfo] = []
        self.error_statistics: Dict[str, Any] = {
            "total_errors": 0,
            "by_category": {},
            "by_severity": {},
            "by_component": {},
            "resolved_count": 0
        }

        # 注册默认重试配置
        if self.retry_manager:
            self._register_default_retry_configs()

    def _register_default_retry_configs(self) -> None:
        """注册默认重试配置"""
        # 网络错误重试配置
        network_config = RetryConfig(
            max_attempts=5,
            base_delay=2.0,
            max_delay=30.0,
            retryable_exceptions=[
                ConnectionError,
                TimeoutError,
                OSError
            ]
        )
        self.retry_manager.register_config("network", network_config)

        # 数据库错误重试配置
        database_config = RetryConfig(
            max_attempts=3,
            base_delay=1.0,
            max_delay=10.0,
            retryable_exceptions=[]
        )
        self.retry_manager.register_config("database", database_config)

        # 外部服务重试配置
        external_service_config = RetryConfig(
            max_attempts=3,
            base_delay=5.0,
            max_delay=60.0,
            retryable_exceptions=[]
        )
        self.retry_manager.register_config("external_service", external_service_config)

    async def handle_error(
        self,
        exception: Exception,
        component: Optional[str] = None,
        operation: Optional[str] = None,
        user_id: Optional[str] = None,
        request_id: Optional[str] = None,
        **context
    ) -> ErrorInfo:
        """处理错误

        Args:
            exception: 异常对象
            component: 组件名称
            operation: 操作名称
            user_id: 用户ID
            request_id: 请求ID
            **context: 额外上下文信息

        Returns:
            错误信息对象
        """
        # 分类错误
        if self.classifier:
            error_info = self.classifier.classify_error(exception, component, operation)
        else:
            error_info = ErrorInfo(
                error_id=f"error_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(exception)}",
                category=ErrorCategory.UNKNOWN,
                severity=ErrorSeverity.MEDIUM,
                message=str(exception),
                component=component,
                operation=operation
            )

        # 添加额外信息
        error_info.user_id = user_id
        error_info.request_id = request_id
        error_info.details.update(context)

        # 添加到历史记录
        self.error_history.append(error_info)

        # 更新统计信息
        self._update_statistics(error_info)

        # 记录日志
        self._log_error(error_info)

        # 保存到文件
        if self.error_log_file:
            await self._save_error_to_file(error_info)

        return error_info

    def _update_statistics(self, error_info: ErrorInfo) -> None:
        """更新错误统计信息"""
        self.error_statistics["total_errors"] += 1

        # 按分类统计
        category = error_info.category.value
        self.error_statistics["by_category"][category] = \
            self.error_statistics["by_category"].get(category, 0) + 1

        # 按严重程度统计
        severity = error_info.severity.value
        self.error_statistics["by_severity"][severity] = \
            self.error_statistics["by_severity"].get(severity, 0) + 1

        # 按组件统计
        component = error_info.component or "unknown"
        self.error_statistics["by_component"][component] = \
            self.error_statistics["by_component"].get(component, 0) + 1

    def _log_error(self, error_info: ErrorInfo) -> None:
        """记录错误日志"""
        log_level = {
            ErrorSeverity.LOW: logging.INFO,
            ErrorSeverity.MEDIUM: logging.WARNING,
            ErrorSeverity.HIGH: logging.ERROR,
            ErrorSeverity.CRITICAL: logging.CRITICAL
        }.get(error_info.severity, logging.WARNING)

        message = f"[{error_info.category.value.upper()}] {error_info.message}"
        if error_info.component:
            message = f"[{error_info.component}] {message}"

        logger.log(
            log_level,
            message,
            extra={
                "error_id": error_info.error_id,
                "severity": error_info.severity.value,
                "component": error_info.component,
                "operation": error_info.operation
            }
        )

    async def _save_error_to_file(self, error_info: ErrorInfo) -> None:
        """保存错误信息到文件"""
        try:
            self.error_log_file.parent.mkdir(parents=True, exist_ok=True)

            error_data = {
                "error_id": error_info.error_id,
                "category": error_info.category.value,
                "severity": error_info.severity.value,
                "message": error_info.message,
                "details": error_info.details,
                "timestamp": error_info.timestamp.isoformat(),
                "traceback_info": error_info.traceback_info,
                "component": error_info.component,
                "operation": error_info.operation,
                "user_id": error_info.user_id,
                "request_id": error_info.request_id,
                "retry_count": error_info.retry_count,
                "resolved": error_info.resolved,
                "resolution_time": error_info.resolution_time.isoformat() if error_info.resolution_time else None
            }

            with open(self.error_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(error_data) + '\n')

        except Exception as e:
            logger.error(f"Failed to save error to file: {e}")

    def resolve_error(self, error_id: str) -> bool:
        """标记错误为已解决

        Args:
            error_id: 错误ID

        Returns:
            是否成功标记
        """
        for error_info in self.error_history:
            if error_info.error_id == error_id and not error_info.resolved:
                error_info.resolved = True
                error_info.resolution_time = datetime.now()
                self.error_statistics["resolved_count"] += 1
                return True
        return False

    def get_error_statistics(self) -> Dict[str, Any]:
        """获取错误统计信息"""
        return self.error_statistics.copy()

    def get_recent_errors(
        self,
        hours: int = 24,
        category: Optional[ErrorCategory] = None,
        severity: Optional[ErrorSeverity] = None,
        component: Optional[str] = None
    ) -> List[ErrorInfo]:
        """获取最近的错误

        Args:
            hours: 时间范围（小时）
            category: 错误分类过滤
            severity: 严重程度过滤
            component: 组件过滤

        Returns:
            错误信息列表
        """
        cutoff_time = datetime.now() - timedelta(hours=hours)

        filtered_errors = []
        for error_info in self.error_history:
            if error_info.timestamp < cutoff_time:
                continue

            if category and error_info.category != category:
                continue

            if severity and error_info.severity != severity:
                continue

            if component and error_info.component != component:
                continue

            filtered_errors.append(error_info)

        return sorted(filtered_errors, key=lambda x: x.timestamp, reverse=True)


# 全局错误处理器实例
_global_error_handler: Optional[ErrorHandler] = None


def get_global_error_handler() -> Optional[ErrorHandler]:
    """获取全局错误处理器实例"""
    return _global_error_handler


def set_global_error_handler(handler: ErrorHandler) -> None:
    """设置全局错误处理器实例"""
    global _global_error_handler
    _global_error_handler = handler


# 装饰器：自动错误处理
def handle_errors(
    component: str,
    operation: Optional[str] = None,
    retry_config: Optional[str] = None,
    reraise: bool = True
):
    """错误处理装饰器

    Args:
        component: 组件名称
        operation: 操作名称
        retry_config: 重试配置名称
        reraise: 是否重新抛出异常
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            error_handler = get_global_error_handler()

            if error_handler and error_handler.retry_manager and retry_config:
                # 使用重试机制
                try:
                    return await error_handler.retry_manager.execute_with_retry(
                        func, retry_config, *args, **kwargs
                    )
                except Exception as e:
                    # 处理错误
                    await error_handler.handle_error(
                        e, component, operation or func.__name__
                    )
                    if reraise:
                        raise
                    return None
            else:
                # 直接执行并处理错误
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if error_handler:
                        await error_handler.handle_error(
                            e, component, operation or func.__name__
                        )
                    if reraise:
                        raise
                    return None

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            error_handler = get_global_error_handler()

            try:
                return func(*args, **kwargs)
            except Exception as e:
                if error_handler:
                    # 对于同步函数，我们需要以同步方式处理错误
                    import asyncio
                    if asyncio.get_event_loop().is_running():
                        # 如果已经在事件循环中，创建任务
                        asyncio.create_task(error_handler.handle_error(
                            e, component, operation or func.__name__
                        ))
                    else:
                        # 如果不在事件循环中，直接运行
                        asyncio.run(error_handler.handle_error(
                            e, component, operation or func.__name__
                        ))

                if reraise:
                    raise
                return None

        # 根据函数类型返回相应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# 便利装饰器：自动重试
def auto_retry(config_name: Optional[str] = None):
    """自动重试装饰器"""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            error_handler = get_global_error_handler()
            if error_handler and error_handler.retry_manager:
                return await error_handler.retry_manager.execute_with_retry(
                    func, config_name, *args, **kwargs
                )
            else:
                return await func(*args, **kwargs)

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            error_handler = get_global_error_handler()
            if error_handler and error_handler.retry_manager:
                # 对于同步函数，我们需要异步执行重试逻辑
                async def async_retry():
                    return await error_handler.retry_manager.execute_with_retry(
                        func, config_name, *args, **kwargs
                    )
                return asyncio.run(async_retry())
            else:
                return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator