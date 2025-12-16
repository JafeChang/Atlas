"""
Atlas 个人信息聚合系统

一个长期运行的个人信息聚合与分析系统，用于定期获取公开可访问的多行业信息，
对原始数据进行分类、结构化、索引与存储，为搜索、分析与自动化决策提供统一数据基础。

优先级顺序：工程可持续性 > 可迁移性 > 可审计性 > 性能
"""

__version__ = "0.1.0"
__author__ = "Atlas Team"
__email__ = "atlas@example.com"
__description__ = "Atlas 个人信息聚合系统"

# 版本信息
VERSION_INFO = {
    "major": 0,
    "minor": 1,
    "patch": 0,
    "release": "stable"
}

# 系统常量
SYSTEM_NAME = "Atlas"
SYSTEM_VERSION = __version__
BUILD_DATE = "2025-01-17"

# 支持的 Python 版本
PYTHON_REQUIRED_VERSION = (3, 13)

# 默认配置
DEFAULT_CONFIG = {
    "env": "development",
    "log_level": "INFO",
    "data_dir": "./data",
    "config_dir": "./config",
    "log_dir": "./logs"
}

# 导出的公共接口
__all__ = [
    "__version__",
    "__author__",
    "__email__",
    "__description__",
    "VERSION_INFO",
    "SYSTEM_NAME",
    "SYSTEM_VERSION",
    "BUILD_DATE",
    "DEFAULT_CONFIG"
]