"""
Atlas 数据模型包

包含系统中使用的所有数据结构定义
"""

from .documents import (
    # 枚举类型
    DocumentType,
    ProcessingStatus,
    TaskStatus,
    SourceType,

    # 数据模型
    BaseDocument,
    RawDocument,
    ProcessedDocument,
    DataSource,
    CollectionTask,
    SystemMetrics,
    DuplicateGroup,
)

__all__ = [
    # 枚举类型
    "DocumentType",
    "ProcessingStatus",
    "TaskStatus",
    "SourceType",

    # 数据模型
    "BaseDocument",
    "RawDocument",
    "ProcessedDocument",
    "DataSource",
    "CollectionTask",
    "SystemMetrics",
    "DuplicateGroup",
]