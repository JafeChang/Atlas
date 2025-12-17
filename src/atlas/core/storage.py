"""
Atlas 文件存储系统模块

提供JSON文件的存储、检索和管理功能，用于存储原始数据和处理结果。
支持文件压缩、索引管理和自动清理功能。
"""

import gzip
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import aiofiles
import aiofiles.os
from loguru import logger

from ..models.documents import DocumentType, RawDocument, ProcessedDocument


class StorageError(Exception):
    """存储相关错误"""
    pass


class FileNotFoundError(StorageError):
    """文件未找到错误"""
    pass


class InvalidFileFormatError(StorageError):
    """无效文件格式错误"""
    pass


class FileStorageManager:
    """文件存储管理器

    提供基于文件的文档存储功能，包括：
    - JSON文件存储和检索
    - 文件压缩和解压
    - 目录结构管理
    - 索引文件管理
    - 自动清理功能
    """

    def __init__(self, base_dir: Union[str, Path], enable_compression: bool = True):
        """初始化文件存储管理器

        Args:
            base_dir: 基础存储目录
            enable_compression: 是否启用文件压缩
        """
        self.base_dir = Path(base_dir)
        self.enable_compression = enable_compression

        # 目录结构
        self.raw_dir = self.base_dir / "raw"
        self.processed_dir = self.base_dir / "processed"
        self.index_dir = self.base_dir / "indexes"
        self.temp_dir = self.base_dir / "temp"

        # 创建目录结构
        self._ensure_directories()

        logger.info(f"初始化文件存储管理器: {self.base_dir}, 压缩: {enable_compression}")

    def _ensure_directories(self) -> None:
        """确保必要的目录存在"""
        directories = [
            self.base_dir,
            self.raw_dir,
            self.processed_dir,
            self.index_dir,
            self.temp_dir,
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, document_id: Union[str, UUID], document_type: str,
                      subdirectory: Optional[str] = None) -> Path:
        """生成文件存储路径

        Args:
            document_id: 文档ID
            document_type: 文档类型
            subdirectory: 子目录名

        Returns:
            文件路径
        """
        doc_id_str = str(document_id)
        # 使用ID前两位作为子目录，避免单个目录文件过多
        sub_dir = doc_id_str[:2]

        if subdirectory:
            base_path = self.base_dir / subdirectory / sub_dir
        else:
            # 根据文档类型选择目录
            if document_type.lower() == 'raw':
                base_path = self.raw_dir / sub_dir
            elif document_type.lower() == 'processed':
                base_path = self.processed_dir / sub_dir
            else:
                base_path = self.base_dir / document_type.lower() / sub_dir

        base_path.mkdir(parents=True, exist_ok=True)

        if self.enable_compression:
            return base_path / f"{doc_id_str}.json.gz"
        else:
            return base_path / f"{doc_id_str}.json"

    async def store_raw_document(self, document: RawDocument) -> Path:
        """存储原始文档

        Args:
            document: 原始文档对象

        Returns:
            存储的文件路径
        """
        file_path = self._get_file_path(document.id, "raw")

        try:
            # 准备存储数据
            storage_data = {
                "id": str(document.id),
                "source_id": document.source_id,
                "source_url": str(document.source_url) if document.source_url else None,
                "source_type": document.source_type.value,
                "document_type": document.document_type.value,
                "raw_content": document.raw_content,
                "raw_metadata": document.raw_metadata,
                "collected_at": document.collected_at.isoformat(),
                "collector_version": document.collector_version,
                "processing_status": document.processing_status.value,
                "processing_error": document.processing_error,
                "processing_attempts": document.processing_attempts,
                "content_hash": document.content_hash,
                "title": document.title,
                "author": document.author,
                "published_at": document.published_at.isoformat() if document.published_at else None,
                "language": document.language,
                "created_at": document.created_at.isoformat(),
                "updated_at": document.updated_at.isoformat(),
                "stored_at": datetime.utcnow().isoformat(),
            }

            # 写入文件
            await self._write_json_file(file_path, storage_data)

            # 更新索引
            await self._update_raw_document_index(document)

            logger.debug(f"原始文档已存储: {document.id} -> {file_path}")
            return file_path

        except Exception as e:
            logger.error(f"存储原始文档失败 {document.id}: {e}")
            raise StorageError(f"存储原始文档失败: {e}") from e

    async def store_processed_document(self, document: ProcessedDocument) -> Path:
        """存储处理后文档

        Args:
            document: 处理后文档对象

        Returns:
            存储的文件路径
        """
        file_path = self._get_file_path(document.id, "processed")

        try:
            # 准备存储数据
            storage_data = {
                "id": str(document.id),
                "raw_document_id": str(document.raw_document_id),
                "title": document.title,
                "summary": document.summary,
                "content": document.content,
                "structured_content": document.structured_content,
                "extracted_metadata": document.extracted_metadata,
                "entities": document.entities,
                "keywords": document.keywords,
                "categories": document.categories,
                "processed_at": document.processed_at.isoformat(),
                "processor_version": document.processor_version,
                "processing_time_ms": document.processing_time_ms,
                "content_hash": document.content_hash,
                "similarity_group_id": document.similarity_group_id,
                "similarity_score": document.similarity_score,
                "is_duplicate": document.is_duplicate,
                "quality_score": document.quality_score,
                "relevance_score": document.relevance_score,
                "created_at": document.created_at.isoformat(),
                "updated_at": document.updated_at.isoformat(),
                "stored_at": datetime.utcnow().isoformat(),
            }

            # 写入文件
            await self._write_json_file(file_path, storage_data)

            # 更新索引
            await self._update_processed_document_index(document)

            logger.debug(f"处理后文档已存储: {document.id} -> {file_path}")
            return file_path

        except Exception as e:
            logger.error(f"存储处理后文档失败 {document.id}: {e}")
            raise StorageError(f"存储处理后文档失败: {e}") from e

    async def _write_json_file(self, file_path: Path, data: Dict[str, Any]) -> None:
        """写入JSON文件

        Args:
            file_path: 文件路径
            data: 要写入的数据
        """
        if self.enable_compression and file_path.suffix == '.gz':
            async with aiofiles.open(file_path, 'wb') as f:
                json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
                compressed_data = gzip.compress(json_bytes)
                await f.write(compressed_data)
        else:
            async with aiofiles.open(file_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))

    async def _read_json_file(self, file_path: Path) -> Dict[str, Any]:
        """读取JSON文件

        Args:
            file_path: 文件路径

        Returns:
            解析后的数据
        """
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        try:
            if file_path.suffix == '.gz':
                async with aiofiles.open(file_path, 'rb') as f:
                    compressed_data = await f.read()
                    json_bytes = gzip.decompress(compressed_data)
                    return json.loads(json_bytes.decode('utf-8'))
            else:
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    return json.loads(content)
        except json.JSONDecodeError as e:
            raise InvalidFileFormatError(f"无效的JSON格式: {file_path}") from e
        except Exception as e:
            raise StorageError(f"读取文件失败: {file_path}") from e

    async def retrieve_raw_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索原始文档

        Args:
            document_id: 文档ID

        Returns:
            文档数据，如果不存在返回None
        """
        file_path = self._get_file_path(document_id, "raw")

        try:
            return await self._read_json_file(file_path)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"检索原始文档失败 {document_id}: {e}")
            raise StorageError(f"检索原始文档失败: {e}") from e

    async def retrieve_processed_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索处理后文档

        Args:
            document_id: 文档ID

        Returns:
            文档数据，如果不存在返回None
        """
        file_path = self._get_file_path(document_id, "processed")

        try:
            return await self._read_json_file(file_path)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"检索处理后文档失败 {document_id}: {e}")
            raise StorageError(f"检索处理后文档失败: {e}") from e

    async def delete_document(self, document_id: Union[str, UUID], document_type: str) -> bool:
        """删除文档

        Args:
            document_id: 文档ID
            document_type: 文档类型

        Returns:
            是否成功删除
        """
        file_path = self._get_file_path(document_id, document_type)

        try:
            if file_path.exists():
                await aiofiles.os.remove(file_path)

                # 从索引中移除
                await self._remove_from_index(document_id, document_type)

                logger.debug(f"文档已删除: {document_id} -> {file_path}")
                return True
            else:
                logger.warning(f"要删除的文件不存在: {file_path}")
                return False

        except Exception as e:
            logger.error(f"删除文档失败 {document_id}: {e}")
            raise StorageError(f"删除文档失败: {e}") from e

    async def _update_raw_document_index(self, document: RawDocument) -> None:
        """更新原始文档索引

        Args:
            document: 原始文档对象
        """
        index_file = self.index_dir / "raw_documents_index.json"

        index_data = await self._load_index_file(index_file)
        index_data[str(document.id)] = {
            "source_id": document.source_id,
            "content_hash": document.content_hash,
            "document_type": document.document_type.value,
            "processing_status": document.processing_status.value,
            "collected_at": document.collected_at.isoformat(),
            "published_at": document.published_at.isoformat() if document.published_at else None,
            "title": document.title,
            "file_path": str(self._get_file_path(document.id, "raw")),
            "indexed_at": datetime.utcnow().isoformat(),
        }

        await self._save_index_file(index_file, index_data)

    async def _update_processed_document_index(self, document: ProcessedDocument) -> None:
        """更新处理后文档索引

        Args:
            document: 处理后文档对象
        """
        index_file = self.index_dir / "processed_documents_index.json"

        index_data = await self._load_index_file(index_file)
        index_data[str(document.id)] = {
            "raw_document_id": str(document.raw_document_id),
            "content_hash": document.content_hash,
            "similarity_group_id": document.similarity_group_id,
            "is_duplicate": document.is_duplicate,
            "quality_score": document.quality_score,
            "processed_at": document.processed_at.isoformat(),
            "title": document.title,
            "keywords": document.keywords,
            "file_path": str(self._get_file_path(document.id, "processed")),
            "indexed_at": datetime.utcnow().isoformat(),
        }

        await self._save_index_file(index_file, index_data)

    async def _remove_from_index(self, document_id: Union[str, UUID], document_type: str) -> None:
        """从索引中移除文档

        Args:
            document_id: 文档ID
            document_type: 文档类型
        """
        doc_id_str = str(document_id)

        if document_type.lower() == 'raw':
            index_file = self.index_dir / "raw_documents_index.json"
        elif document_type.lower() == 'processed':
            index_file = self.index_dir / "processed_documents_index.json"
        else:
            return

        index_data = await self._load_index_file(index_file)
        if doc_id_str in index_data:
            del index_data[doc_id_str]
            await self._save_index_file(index_file, index_data)

    async def _load_index_file(self, index_file: Path) -> Dict[str, Any]:
        """加载索引文件

        Args:
            index_file: 索引文件路径

        Returns:
            索引数据
        """
        if not index_file.exists():
            return {}

        try:
            async with aiofiles.open(index_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.warning(f"加载索引文件失败 {index_file}: {e}")
            return {}

    async def _save_index_file(self, index_file: Path, data: Dict[str, Any]) -> None:
        """保存索引文件

        Args:
            index_file: 索引文件路径
            data: 索引数据
        """
        try:
            async with aiofiles.open(index_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"保存索引文件失败 {index_file}: {e}")

    async def search_documents(self, query: str, document_type: str = "raw",
                              limit: int = 100) -> List[Dict[str, Any]]:
        """搜索文档

        Args:
            query: 搜索查询
            document_type: 文档类型
            limit: 结果数量限制

        Returns:
            搜索结果列表
        """
        if document_type.lower() == 'raw':
            index_file = self.index_dir / "raw_documents_index.json"
        elif document_type.lower() == 'processed':
            index_file = self.index_dir / "processed_documents_index.json"
        else:
            return []

        index_data = await self._load_index_file(index_file)
        results = []

        query_lower = query.lower()

        for doc_id, doc_info in index_data.items():
            # 简单的文本匹配搜索
            searchable_text = " ".join([
                doc_info.get("title", ""),
                doc_info.get("source_id", ""),
                " ".join(doc_info.get("keywords", [])),
            ]).lower()

            if query_lower in searchable_text:
                # 加载完整文档数据
                try:
                    if document_type.lower() == 'raw':
                        doc_data = await self.retrieve_raw_document(doc_id)
                    else:
                        doc_data = await self.retrieve_processed_document(doc_id)

                    if doc_data:
                        results.append(doc_data)
                        if len(results) >= limit:
                            break
                except Exception as e:
                    logger.warning(f"加载文档数据失败 {doc_id}: {e}")

        return results

    async def cleanup_temp_files(self, older_than_hours: int = 24) -> int:
        """清理临时文件

        Args:
            older_than_hours: 清理多少小时前的文件

        Returns:
            清理的文件数量
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=older_than_hours)
        cleaned_count = 0

        try:
            for file_path in self.temp_dir.glob("*"):
                if file_path.is_file():
                    file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_time < cutoff_time:
                        await aiofiles.os.remove(file_path)
                        cleaned_count += 1
                        logger.debug(f"已清理临时文件: {file_path}")

            logger.info(f"临时文件清理完成，共清理 {cleaned_count} 个文件")
            return cleaned_count

        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")
            raise StorageError(f"清理临时文件失败: {e}") from e

    async def get_storage_stats(self) -> Dict[str, Any]:
        """获取存储统计信息

        Returns:
            存储统计信息
        """
        stats = {
            "base_directory": str(self.base_dir),
            "compression_enabled": self.enable_compression,
            "directories": {}
        }

        # 统计各目录的文件数量和大小
        directories = [
            ("raw", self.raw_dir),
            ("processed", self.processed_dir),
            ("indexes", self.index_dir),
            ("temp", self.temp_dir),
        ]

        total_size = 0
        total_files = 0

        for dir_name, dir_path in directories:
            if dir_path.exists():
                file_count = 0
                dir_size = 0

                for file_path in dir_path.rglob("*"):
                    if file_path.is_file():
                        file_count += 1
                        dir_size += file_path.stat().st_size

                stats["directories"][dir_name] = {
                    "path": str(dir_path),
                    "file_count": file_count,
                    "size_bytes": dir_size,
                    "size_mb": round(dir_size / 1024 / 1024, 2)
                }

                total_size += dir_size
                total_files += file_count

        stats["total"] = {
            "file_count": total_files,
            "size_bytes": total_size,
            "size_mb": round(total_size / 1024 / 1024, 2)
        }

        return stats

    async def backup_storage(self, backup_path: Union[str, Path]) -> None:
        """备份存储数据

        Args:
            backup_path: 备份路径
        """
        backup_path = Path(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"开始备份存储数据: {self.base_dir} -> {backup_path}")

        try:
            if backup_path.exists():
                shutil.rmtree(backup_path)

            shutil.copytree(self.base_dir, backup_path)
            logger.info(f"存储数据备份完成: {backup_path}")

        except Exception as e:
            logger.error(f"存储数据备份失败: {e}")
            raise StorageError(f"存储数据备份失败: {e}") from e


# 全局存储管理器实例
_storage_manager: Optional[FileStorageManager] = None


def get_storage_manager(base_dir: Optional[Union[str, Path]] = None,
                       enable_compression: Optional[bool] = None) -> FileStorageManager:
    """获取全局存储管理器实例

    Args:
        base_dir: 基础存储目录，如果为None则使用配置中的目录
        enable_compression: 是否启用压缩，如果为None则使用默认值

    Returns:
        存储管理器实例
    """
    global _storage_manager
    if _storage_manager is None:
        if base_dir is None:
            from .config import get_config
            config = get_config()
            base_dir = config.data_dir

        if enable_compression is None:
            enable_compression = True

        _storage_manager = FileStorageManager(base_dir, enable_compression)
    return _storage_manager