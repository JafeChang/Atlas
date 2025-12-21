#!/usr/bin/env python3
"""
Atlas MinIO适配器模块

提供MinIO对象存储的适配器实现，与现有FileStorageManager保持兼容。
"""

import os
import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import UUID
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

try:
    from minio import Minio
    from minio.error import S3Error
    MINIO_AVAILABLE = True
except ImportError:
    MINIO_AVAILABLE = False
    logger.warning("MinIO SDK不可用，将禁用MinIO存储功能")


class MinIOStorageAdapter:
    """MinIO存储适配器，兼容FileStorageManager接口"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket_name: str, secure: bool = False):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name
        self.secure = secure
        self.prefix = "atlas-documents"

        # 检查是否使用简化版MinIO服务器
        self.is_simplified_server = self._detect_simplified_server()

        if self.is_simplified_server:
            logger.info("使用简化版MinIO服务器适配器")
            # 对于简化服务器，使用HTTP客户端
            import requests
            self.session = requests.Session()
        else:
            # 使用真实MinIO SDK
            if not MINIO_AVAILABLE:
                raise ImportError("MinIO SDK不可用，请安装: uv add minio")

            self.client = Minio(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure
            )

            # 确保存储桶存在
            if not self.client.bucket_exists(bucket_name):
                self.client.make_bucket(bucket_name)
                logger.info(f"创建存储桶: {bucket_name}")

    def _detect_simplified_server(self) -> bool:
        """检测是否使用简化版MinIO服务器"""
        try:
            import requests
            response = requests.get(f"http://{self.endpoint}/health/live", timeout=5)
            # 简化服务器返回 {"status": "ok"}
            return response.status_code == 200 and "status" in response.json()
        except Exception:
            return False

    def _get_object_path(self, document_id: Union[str, UUID], document_type: str,
                        subdirectory: Optional[str] = None) -> str:
        """生成对象存储路径，兼容原文件路径结构"""
        doc_id_str = str(document_id)
        sub_dir = doc_id_str[:2]  # 使用ID前两位作为子目录

        if subdirectory:
            object_path = f"{self.prefix}/{subdirectory}/{sub_dir}/{doc_id_str}.json"
        else:
            if document_type.lower() == 'raw':
                object_path = f"{self.prefix}/raw/{sub_dir}/{doc_id_str}.json"
            elif document_type.lower() == 'processed':
                object_path = f"{self.prefix}/processed/{sub_dir}/{doc_id_str}.json"
            else:
                object_path = f"{self.prefix}/{document_type.lower()}/{sub_dir}/{doc_id_str}.json"

        return object_path

    async def store_raw_document(self, document_data: Dict[str, Any]) -> str:
        """存储原始文档"""
        document_id = document_data["id"]
        object_path = self._get_object_path(document_id, "raw")

        try:
            # 转换为JSON字符串
            json_data = json.dumps(document_data, ensure_ascii=False, indent=2)
            data_bytes = json_data.encode('utf-8')

            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{object_path}"
                response = self.session.put(url, data=data_bytes, headers={'Content-Type': 'application/json'})
                response.raise_for_status()
            else:
                # 使用真实MinIO SDK
                from io import BytesIO
                self.client.put_object(
                    self.bucket_name,
                    object_path,
                    BytesIO(data_bytes),
                    len(data_bytes),
                    content_type='application/json'
                )

            # 更新索引
            await self._update_raw_document_index(document_data, object_path)

            logger.debug(f"原始文档已存储到MinIO: {document_id} -> {object_path}")
            return object_path

        except Exception as e:
            logger.error(f"存储原始文档到MinIO失败 {document_id}: {e}")
            raise Exception(f"MinIO存储失败: {e}")

    async def store_processed_document(self, document_data: Dict[str, Any]) -> str:
        """存储处理后文档"""
        document_id = document_data["id"]
        object_path = self._get_object_path(document_id, "processed")

        try:
            # 转换为JSON字符串
            json_data = json.dumps(document_data, ensure_ascii=False, indent=2)
            data_bytes = json_data.encode('utf-8')

            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{object_path}"
                response = self.session.put(url, data=data_bytes, headers={'Content-Type': 'application/json'})
                response.raise_for_status()
            else:
                # 使用真实MinIO SDK
                from io import BytesIO
                self.client.put_object(
                    self.bucket_name,
                    object_path,
                    BytesIO(data_bytes),
                    len(data_bytes),
                    content_type='application/json'
                )

            # 更新索引
            await self._update_processed_document_index(document_data, object_path)

            logger.debug(f"处理后文档已存储到MinIO: {document_id} -> {object_path}")
            return object_path

        except Exception as e:
            logger.error(f"存储处理后文档到MinIO失败 {document_id}: {e}")
            raise Exception(f"MinIO存储失败: {e}")

    async def retrieve_raw_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索原始文档"""
        object_path = self._get_object_path(document_id, "raw")

        try:
            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{object_path}"
                response = self.session.get(url)
                response.raise_for_status()
                data = response.content
            else:
                # 使用真实MinIO SDK
                response = self.client.get_object(self.bucket_name, object_path)
                data = response.read()
                response.close()
                response.release_conn()

            return json.loads(data.decode('utf-8'))

        except Exception as e:
            if self.is_simplified_server and response.status_code == 404:
                return None
            elif not self.is_simplified_server and isinstance(e, S3Error):
                return None
            else:
                logger.error(f"从MinIO检索原始文档失败 {document_id}: {e}")
                raise Exception(f"MinIO检索失败: {e}")

    async def retrieve_processed_document(self, document_id: Union[str, UUID]) -> Optional[Dict[str, Any]]:
        """检索处理后文档"""
        object_path = self._get_object_path(document_id, "processed")

        try:
            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{object_path}"
                response = self.session.get(url)
                response.raise_for_status()
                data = response.content
            else:
                # 使用真实MinIO SDK
                response = self.client.get_object(self.bucket_name, object_path)
                data = response.read()
                response.close()
                response.release_conn()

            return json.loads(data.decode('utf-8'))

        except Exception as e:
            if self.is_simplified_server and response.status_code == 404:
                return None
            elif not self.is_simplified_server and isinstance(e, S3Error):
                return None
            else:
                logger.error(f"从MinIO检索处理后文档失败 {document_id}: {e}")
                raise Exception(f"MinIO检索失败: {e}")

    async def delete_document(self, document_id: Union[str, UUID], document_type: str) -> bool:
        """删除文档"""
        object_path = self._get_object_path(document_id, document_type)

        try:
            if self.is_simplified_server:
                # 使用简化版MinIO服务器 - 先检查对象是否存在
                url = f"http://{self.endpoint}/{object_path}"
                response = self.session.get(url)
                if response.status_code == 404:
                    logger.warning(f"要删除的对象不存在: {object_path}")
                    return False

                # 删除对象 (简化服务器不支持DELETE，这里记录日志)
                logger.info(f"简化版MinIO服务器不支持删除操作，仅从索引中移除: {object_path}")
            else:
                # 使用真实MinIO SDK
                # 检查对象是否存在
                self.client.stat_object(self.bucket_name, object_path)

                # 删除对象
                self.client.remove_object(self.bucket_name, object_path)

            # 从索引中移除
            await self._remove_from_index(document_id, document_type)

            logger.debug(f"文档已从MinIO删除: {document_id} -> {object_path}")
            return True

        except Exception as e:
            logger.error(f"从MinIO删除文档失败 {document_id}: {e}")
            raise Exception(f"MinIO删除失败: {e}")

    async def _update_raw_document_index(self, document_data: Dict[str, Any], object_path: str) -> None:
        """更新原始文档索引"""
        index_path = f"{self.prefix}/indexes/raw_documents_index.json"
        document_id = document_data["id"]

        try:
            # 加载现有索引
            index_data = await self._load_index(index_path)

            # 更新索引条目
            index_data[str(document_id)] = {
                "source_id": document_data.get("source_id"),
                "content_hash": document_data.get("content_hash"),
                "document_type": document_data.get("document_type"),
                "processing_status": document_data.get("processing_status"),
                "collected_at": document_data.get("collected_at"),
                "published_at": document_data.get("published_at"),
                "title": document_data.get("title"),
                "object_path": object_path,
                "indexed_at": datetime.utcnow().isoformat(),
            }

            # 保存索引
            await self._save_index(index_path, index_data)

        except Exception as e:
            logger.warning(f"更新原始文档索引失败: {e}")

    async def _update_processed_document_index(self, document_data: Dict[str, Any], object_path: str) -> None:
        """更新处理后文档索引"""
        index_path = f"{self.prefix}/indexes/processed_documents_index.json"
        document_id = document_data["id"]

        try:
            # 加载现有索引
            index_data = await self._load_index(index_path)

            # 更新索引条目
            index_data[str(document_id)] = {
                "raw_document_id": document_data.get("raw_document_id"),
                "content_hash": document_data.get("content_hash"),
                "similarity_group_id": document_data.get("similarity_group_id"),
                "is_duplicate": document_data.get("is_duplicate"),
                "quality_score": document_data.get("quality_score"),
                "processed_at": document_data.get("processed_at"),
                "title": document_data.get("title"),
                "keywords": document_data.get("keywords"),
                "object_path": object_path,
                "indexed_at": datetime.utcnow().isoformat(),
            }

            # 保存索引
            await self._save_index(index_path, index_data)

        except Exception as e:
            logger.warning(f"更新处理后文档索引失败: {e}")

    async def _remove_from_index(self, document_id: Union[str, UUID], document_type: str) -> None:
        """从索引中移除文档"""
        doc_id_str = str(document_id)

        if document_type.lower() == 'raw':
            index_path = f"{self.prefix}/indexes/raw_documents_index.json"
        elif document_type.lower() == 'processed':
            index_path = f"{self.prefix}/indexes/processed_documents_index.json"
        else:
            return

        try:
            index_data = await self._load_index(index_path)
            if doc_id_str in index_data:
                del index_data[doc_id_str]
                await self._save_index(index_path, index_data)
        except Exception as e:
            logger.warning(f"从索引移除文档失败: {e}")

    async def _load_index(self, index_path: str) -> Dict[str, Any]:
        """加载索引文件"""
        try:
            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{index_path}"
                response = self.session.get(url)
                if response.status_code == 404:
                    return {}
                response.raise_for_status()
                data = response.content
            else:
                # 使用真实MinIO SDK
                response = self.client.get_object(self.bucket_name, index_path)
                data = response.read()
                response.close()
                response.release_conn()
            return json.loads(data.decode('utf-8'))
        except Exception as e:
            if self.is_simplified_server and response.status_code == 404:
                return {}
            elif not self.is_simplified_server and isinstance(e, S3Error):
                return {}
            else:
                logger.warning(f"加载索引文件失败 {index_path}: {e}")
                return {}

    async def _save_index(self, index_path: str, data: Dict[str, Any]) -> None:
        """保存索引文件"""
        try:
            json_data = json.dumps(data, ensure_ascii=False, indent=2)
            data_bytes = json_data.encode('utf-8')

            if self.is_simplified_server:
                # 使用简化版MinIO服务器
                url = f"http://{self.endpoint}/{index_path}"
                response = self.session.put(url, data=data_bytes, headers={'Content-Type': 'application/json'})
                response.raise_for_status()
            else:
                # 使用真实MinIO SDK
                from io import BytesIO
                self.client.put_object(
                    self.bucket_name,
                    index_path,
                    BytesIO(data_bytes),
                    len(data_bytes),
                    content_type='application/json'
                )
        except Exception as e:
            logger.error(f"保存索引文件失败 {index_path}: {e}")

    async def search_documents(self, query: str, document_type: str = "raw",
                              limit: int = 100) -> List[Dict[str, Any]]:
        """搜索文档"""
        if document_type.lower() == 'raw':
            index_path = f"{self.prefix}/indexes/raw_documents_index.json"
        elif document_type.lower() == 'processed':
            index_path = f"{self.prefix}/indexes/processed_documents_index.json"
        else:
            return []

        index_data = await self._load_index(index_path)
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

    async def list_documents(self, document_type: str = "raw", prefix: str = None) -> List[str]:
        """列出文档"""
        if prefix:
            list_prefix = f"{self.prefix}/{document_type.lower()}/{prefix}"
        else:
            list_prefix = f"{self.prefix}/{document_type.lower()}/"

        try:
            if self.is_simplified_server:
                # 使用简化版MinIO服务器 - 调用列表接口
                url = f"http://{self.endpoint}/"
                response = self.session.get(url)
                response.raise_for_status()
                data = response.json()

                # 从响应中提取匹配的对象
                objects = data.get('objects', [])
                document_ids = []
                for obj in objects:
                    if obj.startswith(list_prefix) and obj.endswith('.json'):
                        # 提取文档ID (文件名，不含扩展名)
                        doc_id = obj.split('/')[-1][:-5]  # 移除 .json
                        document_ids.append(doc_id)
                return document_ids
            else:
                # 使用真实MinIO SDK
                objects = self.client.list_objects(self.bucket_name, prefix=list_prefix)
                document_ids = []
                for obj in objects:
                    # 从对象路径提取文档ID
                    object_name = obj.object_name
                    if object_name.endswith('.json'):
                        # 提取文档ID (文件名，不含扩展名)
                        doc_id = object_name.split('/')[-1][:-5]  # 移除 .json
                        document_ids.append(doc_id)
                return document_ids
        except Exception as e:
            logger.error(f"列出文档失败: {e}")
            return []

    async def get_storage_stats(self) -> Dict[str, Any]:
        """获取存储统计信息"""
        try:
            # 统计各类型的文档数量
            raw_docs = len(await self.list_documents("raw"))
            processed_docs = len(await self.list_documents("processed"))

            if self.is_simplified_server:
                # 简化版MinIO服务器
                return {
                    "storage_type": "minio",
                    "server_type": "simplified",
                    "endpoint": self.endpoint,
                    "bucket_name": self.bucket_name,
                    "prefix": self.prefix,
                    "raw_documents_count": raw_docs,
                    "processed_documents_count": processed_docs,
                    "total_objects": raw_docs + processed_docs,
                    "total_size_bytes": 0,  # 简化版不支持大小统计
                    "total_size_mb": 0
                }
            else:
                # 真实MinIO SDK
                # 获取存储桶统计信息
                total_size = 0
                total_objects = 0

                objects = self.client.list_objects(self.bucket_name, prefix=self.prefix)
                for obj in objects:
                    total_size += obj.size
                    total_objects += 1

                return {
                    "storage_type": "minio",
                    "server_type": "official",
                    "endpoint": self.endpoint,
                    "bucket_name": self.bucket_name,
                    "prefix": self.prefix,
                    "raw_documents_count": raw_docs,
                    "processed_documents_count": processed_docs,
                    "total_objects": total_objects,
                    "total_size_bytes": total_size,
                    "total_size_mb": round(total_size / 1024 / 1024, 2)
                }
        except Exception as e:
            logger.error(f"获取存储统计信息失败: {e}")
            return {
                "storage_type": "minio",
                "error": str(e)
            }


def create_minio_adapter(config: Optional[Dict[str, Any]] = None) -> MinIOStorageAdapter:
    """创建MinIO适配器实例"""
    if config is None:
        # 从环境变量读取配置
        config = {
            'endpoint': os.getenv('MINIO_ENDPOINT', 'localhost:9000'),
            'access_key': os.getenv('MINIO_ACCESS_KEY', 'minioadmin'),
            'secret_key': os.getenv('MINIO_SECRET_KEY', 'minioadmin123456'),
            'bucket_name': os.getenv('MINIO_BUCKET_NAME', 'atlas-raw-data'),
            'secure': os.getenv('MINIO_SECURE', 'false').lower() == 'true'
        }

    return MinIOStorageAdapter(
        endpoint=config['endpoint'],
        access_key=config['access_key'],
        secret_key=config['secret_key'],
        bucket_name=config['bucket_name'],
        secure=config.get('secure', False)
    )


# 全局MinIO适配器实例
_minio_adapter: Optional[MinIOStorageAdapter] = None


def get_minio_adapter() -> Optional[MinIOStorageAdapter]:
    """获取全局MinIO适配器实例"""
    global _minio_adapter

    if not MINIO_AVAILABLE:
        return None

    if _minio_adapter is None:
        try:
            _minio_adapter = create_minio_adapter()
            logger.info("MinIO适配器初始化成功")
        except Exception as e:
            logger.error(f"MinIO适配器初始化失败: {e}")
            _minio_adapter = None

    return _minio_adapter