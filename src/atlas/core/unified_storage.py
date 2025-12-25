#!/usr/bin/env python3
"""
Atlas 统一存储接口

提供统一的文档存储接口，支持多种存储后端：
- 文件系统存储 (FileStorageManager)
- MinIO对象存储 (MinIOStorageAdapter)

根据配置自动选择存储后端，保持接口一致性。
"""

import os
import logging
from typing import Any, Dict, List, Optional, Union
from uuid import UUID
from pathlib import Path

try:
    from ..config import get_config
except ImportError:
    # 支持直接运行脚本
    from atlas.core.config import get_config

logger = logging.getLogger(__name__)


class UnifiedStorageInterface:
    """统一存储接口，提供一致的文档存储操作"""

    def __init__(self, config=None):
        """初始化统一存储接口

        Args:
            config: Atlas配置对象，如果为None则使用默认配置
        """
        self.config = config or get_config()
        self.storage_config = self.config.storage

        # 根据配置选择存储后端
        self.backend = self._create_backend()

        logger.info(f"初始化统一存储接口，存储类型: {self.storage_config.type}")

    def _create_backend(self):
        """根据配置创建存储后端"""
        storage_type = self.storage_config.type

        if storage_type == 'filesystem':
            # 使用文件系统存储
            try:
                from .storage import FileStorageManager
            except ImportError:
                from atlas.core.storage import FileStorageManager
            return FileStorageManager(
                base_dir=self.storage_config.filesystem_base_dir,
                enable_compression=self.storage_config.filesystem_compression
            )

        elif storage_type == 'minio':
            # 使用MinIO存储
            try:
                from .minio_adapter import MinIOStorageAdapter
            except ImportError:
                from atlas.core.minio_adapter import MinIOStorageAdapter
            return MinIOStorageAdapter(
                endpoint=self.storage_config.minio_endpoint,
                access_key=self.storage_config.minio_access_key,
                secret_key=self.storage_config.minio_secret_key,
                bucket_name=self.storage_config.minio_bucket_name,
                secure=self.storage_config.minio_secure
            )

        else:
            raise ValueError(f"不支持的存储类型: {storage_type}")

    async def store_raw_document(self, document_data: Dict[str, Any]) -> str:
        """存储原始文档

        Args:
            document_data: 文档数据字典

        Returns:
            存储路径或对象路径
        """
        return await self.backend.store_raw_document(document_data)

    async def store_processed_document(self, document_data: Dict[str, Any]) -> str:
        """存储处理后文档

        Args:
            document_data: 文档数据字典

        Returns:
            存储路径或对象路径
        """
        return await self.backend.store_processed_document(document_data)

    async def retrieve_raw_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索原始文档

        Args:
            document_id: 文档ID

        Returns:
            文档数据，如果不存在返回None
        """
        return await self.backend.retrieve_raw_document(document_id)

    async def retrieve_processed_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索处理后文档

        Args:
            document_id: 文档ID

        Returns:
            文档数据，如果不存在返回None
        """
        return await self.backend.retrieve_processed_document(document_id)

    async def delete_document(self, document_id: Union[str, UUID], document_type: str) -> bool:
        """删除文档

        Args:
            document_id: 文档ID
            document_type: 文档类型 (raw/processed)

        Returns:
            是否成功删除
        """
        return await self.backend.delete_document(document_id, document_type)

    async def search_documents(self, query: str, document_type: str = "raw",
                              limit: int = 100) -> List[Dict[str, Any]]:
        """搜索文档

        Args:
            query: 搜索查询
            document_type: 文档类型 (raw/processed)
            limit: 结果数量限制

        Returns:
            搜索结果列表
        """
        return await self.backend.search_documents(query, document_type, limit)

    async def list_documents(self, document_type: str = "raw", prefix: str = None) -> List[str]:
        """列出文档

        Args:
            document_type: 文档类型 (raw/processed)
            prefix: 路径前缀

        Returns:
            文档ID列表
        """
        if hasattr(self.backend, 'list_documents'):
            return await self.backend.list_documents(document_type, prefix)
        else:
            # 文件系统存储没有list_documents方法，返回空列表
            logger.warning("当前存储后端不支持list_documents方法")
            return []

    async def get_storage_stats(self) -> Dict[str, Any]:
        """获取存储统计信息

        Returns:
            存储统计信息
        """
        return await self.backend.get_storage_stats()

    def get_storage_type(self) -> str:
        """获取当前存储类型"""
        return self.storage_config.type

    def get_storage_info(self) -> Dict[str, Any]:
        """获取存储信息"""
        info = {
            "storage_type": self.storage_config.type,
        }

        if self.storage_config.type == 'filesystem':
            info.update({
                "base_dir": str(self.storage_config.filesystem_base_dir),
                "compression_enabled": self.storage_config.filesystem_compression
            })
        elif self.storage_config.type == 'minio':
            info.update({
                "endpoint": self.storage_config.minio_endpoint,
                "bucket_name": self.storage_config.minio_bucket_name,
                "prefix": self.storage_config.minio_prefix,
                "secure": self.storage_config.minio_secure
            })

        return info


# 全局统一存储实例
_unified_storage: Optional[UnifiedStorageInterface] = None


def get_unified_storage(config=None) -> UnifiedStorageInterface:
    """获取全局统一存储实例

    Args:
        config: Atlas配置对象

    Returns:
        统一存储接口实例
    """
    global _unified_storage

    if _unified_storage is None:
        _unified_storage = UnifiedStorageInterface(config)
        logger.info("全局统一存储实例已初始化")

    return _unified_storage


def reset_unified_storage():
    """重置全局统一存储实例"""
    global _unified_storage
    _unified_storage = None
    logger.info("全局统一存储实例已重置")
