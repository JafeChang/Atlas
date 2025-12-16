"""
Atlas 日志系统

提供统一的日志管理功能，支持结构化日志、文件轮转、多级别输出等。
"""

import sys
import json
from pathlib import Path
from typing import Any, Dict, Optional, Union
from datetime import datetime
from loguru import logger


class AtlasLogger:
    """Atlas 日志管理器"""

    def __init__(self,
                 log_dir: Union[str, Path],
                 log_level: str = "INFO",
                 enable_file: bool = True,
                 enable_console: bool = True,
                 rotation: str = "10 MB",
                 retention: str = "30 days"):
        """初始化日志管理器

        Args:
            log_dir: 日志文件目录
            log_level: 日志级别
            enable_file: 是否启用文件日志
            enable_console: 是否启用控制台输出
            rotation: 日志轮转大小
            retention: 日志保留时间
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 移除默认的处理器
        logger.remove()

        # 控制台日志格式
        console_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        )

        # 文件日志格式
        file_format = (
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        )

        # 结构化日志格式
        json_format = (
            "{{\"timestamp\": \"{time:YYYY-MM-DD HH:mm:ss}\", "
            "\"level\": \"{level}\", "
            "\"logger\": \"{name}\", "
            "\"function\": \"{function}\", "
            "\"line\": {line}, "
            "\"message\": \"{message}\"}}"
        )

        # 添加控制台处理器
        if enable_console:
            logger.add(
                sys.stdout,
                format=console_format,
                level=log_level,
                colorize=True,
                enqueue=True
            )

        # 添加文件处理器
        if enable_file:
            # 应用日志
            logger.add(
                self.log_dir / "atlas.log",
                format=file_format,
                level=log_level,
                rotation=rotation,
                retention=retention,
                encoding="utf-8",
                enqueue=True
            )

            # 错误日志
            logger.add(
                self.log_dir / "errors.log",
                format=file_format,
                level="ERROR",
                rotation=rotation,
                retention=retention,
                encoding="utf-8",
                enqueue=True
            )

            # 结构化日志（JSON格式）
            logger.add(
                self.log_dir / "structured.log",
                format=json_format,
                level=log_level,
                rotation=rotation,
                retention=retention,
                encoding="utf-8",
                enqueue=True
            )

        self.logger = logger

    def debug(self, message: str, **kwargs) -> None:
        """调试日志"""
        self._log_with_context("DEBUG", message, **kwargs)

    def info(self, message: str, **kwargs) -> None:
        """信息日志"""
        self._log_with_context("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        """警告日志"""
        self._log_with_context("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs) -> None:
        """错误日志"""
        self._log_with_context("ERROR", message, **kwargs)

    def critical(self, message: str, **kwargs) -> None:
        """严重错误日志"""
        self._log_with_context("CRITICAL", message, **kwargs)

    def exception(self, message: str, **kwargs) -> None:
        """异常日志（包含堆栈信息）"""
        kwargs["exception"] = True
        self._log_with_context("ERROR", message, **kwargs)

    def _log_with_context(self, level: str, message: str, **kwargs) -> None:
        """带上下文的日志记录

        Args:
            level: 日志级别
            message: 日志消息
            **kwargs: 额外的上下文信息
        """
        # 构建带有上下文的消息
        if kwargs:
            context_parts = []
            for key, value in kwargs.items():
                if key == "exception":
                    continue  # 异常信息由loguru处理
                context_parts.append(f"{key}={value}")

            if context_parts:
                enhanced_message = f"{message} | {' | '.join(context_parts)}"
            else:
                enhanced_message = message
        else:
            enhanced_message = message

        # 记录日志
        getattr(self.logger, level.lower())(enhanced_message)

    def log_request(self, method: str, url: str, status_code: int = None,
                   duration: float = None, **kwargs) -> None:
        """记录HTTP请求日志

        Args:
            method: HTTP方法
            url: 请求URL
            status_code: 响应状态码
            duration: 请求耗时
            **kwargs: 额外信息
        """
        self.info(
            "HTTP Request",
            method=method,
            url=url,
            status_code=status_code,
            duration_ms=round(duration * 1000, 2) if duration else None,
            **kwargs
        )

    def log_task(self, task_name: str, status: str, duration: float = None,
                **kwargs) -> None:
        """记录任务执行日志

        Args:
            task_name: 任务名称
            status: 任务状态 (started, completed, failed)
            duration: 执行耗时
            **kwargs: 额外信息
        """
        self.info(
            "Task Execution",
            task=task_name,
            status=status,
            duration_ms=round(duration * 1000, 2) if duration else None,
            **kwargs
        )

    def log_performance(self, operation: str, duration: float,
                       **kwargs) -> None:
        """记录性能日志

        Args:
            operation: 操作名称
            duration: 耗时
            **kwargs: 额外性能指标
        """
        self.info(
            "Performance Metric",
            operation=operation,
            duration_ms=round(duration * 1000, 2),
            **kwargs
        )

    def log_collection(self, source_name: str, items_count: int,
                      status: str, **kwargs) -> None:
        """记录数据采集日志

        Args:
            source_name: 数据源名称
            items_count: 采集到的项目数量
            status: 采集状态
            **kwargs: 额外信息
        """
        self.info(
            "Data Collection",
            source=source_name,
            items_count=items_count,
            status=status,
            **kwargs
        )

    def log_llm_call(self, provider: str, model: str, tokens_used: int = None,
                    cost: float = None, duration: float = None, **kwargs) -> None:
        """记录LLM调用日志

        Args:
            provider: LLM提供商
            model: 模型名称
            tokens_used: 使用的token数量
            cost: 成本
            duration: 耗时
            **kwargs: 额外信息
        """
        self.info(
            "LLM Call",
            provider=provider,
            model=model,
            tokens_used=tokens_used,
            cost=cost,
            duration_ms=round(duration * 1000, 2) if duration else None,
            **kwargs
        )


# 全局日志实例
_atlas_logger: Optional[AtlasLogger] = None


def init_logger(log_dir: Union[str, Path],
               log_level: str = "INFO",
               **kwargs) -> AtlasLogger:
    """初始化全局日志系统

    Args:
        log_dir: 日志目录
        log_level: 日志级别
        **kwargs: 其他配置参数

    Returns:
        日志管理器实例
    """
    global _atlas_logger
    _atlas_logger = AtlasLogger(
        log_dir=log_dir,
        log_level=log_level,
        **kwargs
    )
    return _atlas_logger


def get_logger(name: Optional[str] = None) -> Union[logger, AtlasLogger]:
    """获取日志记录器

    Args:
        name: 日志记录器名称

    Returns:
        日志记录器实例
    """
    if _atlas_logger is not None:
        return _atlas_logger
    else:
        # 如果没有初始化，返回loguru默认logger
        if name:
            return logger.bind(name=name)
        return logger


# 日志装饰器
def log_execution(func_name: Optional[str] = None,
                 log_args: bool = True,
                 log_result: bool = False):
    """函数执行日志装饰器

    Args:
        func_name: 自定义函数名称
        log_args: 是否记录参数
        log_result: 是否记录返回值
    """
    def decorator(func):
        import functools
        import time

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            name = func_name or f"{func.__module__}.{func.__name__}"

            # 记录开始
            log_data = {"status": "started"}
            if log_args:
                log_data.update({
                    "args_count": len(args),
                    "kwargs_keys": list(kwargs.keys())
                })

            get_logger().log_task(name, **log_data)

            try:
                # 执行函数
                result = func(*args, **kwargs)
                duration = time.time() - start_time

                # 记录成功
                log_data = {
                    "status": "completed",
                    "duration_ms": round(duration * 1000, 2)
                }
                if log_result:
                    log_data["result_type"] = type(result).__name__

                get_logger().log_task(name, **log_data)
                return result

            except Exception as e:
                duration = time.time() - start_time

                # 记录失败
                get_logger().log_task(
                    name,
                    status="failed",
                    duration_ms=round(duration * 1000, 2),
                    error=str(e),
                    error_type=type(e).__name__
                )
                raise

        return wrapper
    return decorator


# 快捷函数
def debug(message: str, **kwargs) -> None:
    """快捷调试日志"""
    get_logger().debug(message, **kwargs)


def info(message: str, **kwargs) -> None:
    """快捷信息日志"""
    get_logger().info(message, **kwargs)


def warning(message: str, **kwargs) -> None:
    """快捷警告日志"""
    get_logger().warning(message, **kwargs)


def error(message: str, **kwargs) -> None:
    """快捷错误日志"""
    get_logger().error(message, **kwargs)


def critical(message: str, **kwargs) -> None:
    """快捷严重错误日志"""
    get_logger().critical(message, **kwargs)