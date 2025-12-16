"""
Atlas 核心模块

提供系统的基础功能，包括配置管理、数据库连接、存储系统等核心组件。
"""

from .config import Config
from .logging import get_logger

__all__ = [
    "Config",
    "get_logger"
]