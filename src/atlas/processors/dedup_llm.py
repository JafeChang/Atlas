"""
LLM 智能去重处理器

集成LLM的智能去重功能，结合传统哈希去重和语义分析。
"""

import asyncio
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

from atlas.core.logging import get_logger
from atlas.models.documents import ProcessedDocument, RawDocument
from atlas.processors.dedup_base import BaseDeduplicator
from atlas.llm import (
    LocalLLMClient,
    SemanticDeduplicator,
    SemanticConfig,
    DedupStrategy,
    SimilarityThreshold
)

logger = get_logger(__name__)


class LLMDeduplicator(BaseDeduplicator):
    """LLM智能去重处理器"""

    def __init__(self, config, cache_dir: Optional[Path] = None):
        """初始化LLM去重处理器

        Args:
            config: Atlas配置对象
            cache_dir: 缓存目录
        """
        super().__init__(config, cache_dir)

        # LLM组件
        self.llm_client: Optional[LocalLLMClient] = None
        self.semantic_deduplicator: Optional[SemanticDeduplicator] = None

        # 配置
        self.enabled = True
        self.dedup_strategy = DedupStrategy.HYBRID
        self.similarity_threshold = SimilarityThreshold.NORMAL.value

        # 统计信息
        self.llm_stats = {
            "semantic_checks": 0,
            "semantic_duplicates": 0,
            "llm_api_calls": 0,
            "llm_failures": 0,
        }

    async def initialize(self) -> None:
        """初始化LLM组件"""
        try:
            # 初始化LLM客户端
            self.llm_client = LocalLLMClient(
                base_url=getattr(self.config.llm, 'base_url', 'http://localhost:11434'),
                default_model=getattr(self.config.llm, 'default_model', 'llama2'),
                timeout=getattr(self.config.llm, 'timeout', 30.0),
                max_concurrent=getattr(self.config.llm, 'max_concurrent', 5)
            )

            # 检查LLM服务可用性
            health = await self.llm_client.health_check()
            if health.get("status") != "healthy":
                logger.warning(f"LLM服务不健康: {health.get('error', 'Unknown error')}")
                self.enabled = False
                return

            # 获取LLM配置
            self.dedup_strategy = DedupStrategy(
                getattr(self.config.llm, 'dedup_strategy', 'hybrid')
            )
            self.similarity_threshold = float(
                getattr(self.config.llm, 'similarity_threshold', 0.85)
            )

            # 初始化语义去重器
            cache_file = None
            if self.cache_dir:
                cache_file = self.cache_dir / "semantic_dedup_cache.json"

            semantic_config = SemanticConfig(
                strategy=self.dedup_strategy,
                similarity_threshold=self.similarity_threshold,
                llm_model=getattr(self.config.llm, 'default_model', 'llama2'),
                max_content_length=getattr(self.config.llm, 'max_content_length', 2000),
                batch_size=getattr(self.config.llm, 'batch_size', 5),
                cache_enabled=getattr(self.config.llm, 'cache_enabled', True),
                cache_ttl=getattr(self.config.llm, 'cache_ttl', 3600),
                min_content_length=getattr(self.config.llm, 'min_content_length', 50),
                enable_tfidf_fallback=getattr(self.config.llm, 'enable_tfidf_fallback', True),
                ignore_urls=getattr(self.config.llm, 'ignore_urls', True),
                normalize_text=getattr(self.config.llm, 'normalize_text', True),
            )

            self.semantic_deduplicator = SemanticDeduplicator(
                config=semantic_config,
                llm_client=self.llm_client,
                cache_file=cache_file
            )

            # 初始化传统去重器状态
            await self._load_dedup_state()

            self.enabled = True
            logger.info(f"LLM去重处理器初始化完成，策略: {self.dedup_strategy.value}")

        except Exception as e:
            logger.error(f"LLM去重处理器初始化失败: {e}")
            self.enabled = False

    async def is_duplicate(
        self,
        doc: RawDocument
    ) -> Tuple[bool, Optional[str], Optional[ProcessedDocument]]:
        """检查文档是否重复

        Args:
            doc: 原始文档

        Returns:
            (是否重复, 重复文档ID, 重复的已处理文档)
        """
        if not self.enabled:
            # 如果LLM未启用，回退到传统去重
            return await super().is_duplicate(doc)

        try:
            self.llm_stats["semantic_checks"] += 1

            # 使用语义去重器检查
            is_dup, dup_id = await self.semantic_deduplicator.is_duplicate(
                doc_id=doc.doc_id,
                content=doc.content,
                title=doc.title or "",
                url=doc.url or "",
                metadata={"source": doc.source_id, "type": doc.type.value}
            )

            if is_dup:
                self.llm_stats["semantic_duplicates"] += 1

                # 获取重复的已处理文档
                dup_processed = await self._get_duplicate_document(dup_id) if dup_id else None

                logger.info(f"语义重复检测: {doc.doc_id} 重复于 {dup_id}")
                return True, dup_id, dup_processed
            else:
                # 添加到语义去重库
                await self.semantic_deduplicator.add_document(
                    doc_id=doc.doc_id,
                    content=doc.content,
                    title=doc.title or "",
                    url=doc.url or "",
                    metadata={"source": doc.source_id, "type": doc.type.value}
                )

                # 同时进行传统哈希去重（双重保险）
                hash_dup, hash_dup_id = await super().is_duplicate(doc)
                if hash_dup:
                    logger.warning(f"语义去重未检测到重复，但哈希去重检测到: {doc.doc_id} 重复于 {hash_dup_id}")
                    return hash_dup, hash_dup_id, await self._get_duplicate_document(hash_dup_id)

                return False, None, None

        except Exception as e:
            self.llm_stats["llm_failures"] += 1
            logger.error(f"LLM去重检查失败: {e}")

            # 回退到传统去重
            logger.info("回退到传统哈希去重")
            return await super().is_duplicate(doc)

    async def process_document(
        self,
        doc: RawDocument
    ) -> ProcessedDocument:
        """处理文档（包括去重检查）

        Args:
            doc: 原始文档

        Returns:
            处理后的文档
        """
        # 先检查去重
        is_duplicate, duplicate_id, duplicate_doc = await self.is_duplicate(doc)

        if is_duplicate and duplicate_doc:
            logger.debug(f"文档重复，返回已处理文档: {doc.doc_id}")
            # 返回重复的已处理文档，但使用新的doc_id
            duplicate_doc.doc_id = doc.doc_id
            duplicate_doc.is_duplicate = True
            duplicate_doc.original_doc_id = duplicate_id
            return duplicate_doc

        # 如果不重复，创建新的处理文档
        processed_doc = ProcessedDocument(
            doc_id=doc.doc_id,
            source_id=doc.source_id,
            type=doc.type,
            title=doc.title,
            url=doc.url,
            content=doc.content,
            raw_content_hash=doc.content_hash,
            extracted_at=doc.extracted_at,
            metadata=doc.metadata or {}
        )

        processed_doc.is_duplicate = False

        # 添加到传统哈希去重库
        await self._add_to_dedup_index(processed_doc)

        # 保存状态
        await self._save_dedup_state()

        return processed_doc

    async def batch_deduplicate(
        self,
        documents: List[RawDocument]
    ) -> List[ProcessedDocument]:
        """批量去重处理

        Args:
            documents: 文档列表

        Returns:
            处理后的文档列表
        """
        if not self.enabled:
            # 回退到传统批量去重
            return await super().batch_deduplicate(documents)

        try:
            results = []

            # 批量语义去重检查
            dedup_inputs = [
                (doc.doc_id, doc.content, doc.title or "", doc.url or "",
                 {"source": doc.source_id, "type": doc.type.value})
                for doc in documents
            ]

            batch_results = await self.semantic_deduplicator.batch_check_duplicates(dedup_inputs)

            for doc, (is_dup, dup_id) in zip(documents, batch_results):
                if is_dup:
                    # 重复文档
                    dup_processed = await self._get_duplicate_document(dup_id) if dup_id else None
                    if dup_processed:
                        dup_processed.doc_id = doc.doc_id
                        dup_processed.is_duplicate = True
                        dup_processed.original_doc_id = dup_id
                        results.append(dup_processed)
                        self.llm_stats["semantic_duplicates"] += 1
                    else:
                        # 找不到重复文档，创建新的
                        processed_doc = await self._create_processed_document(doc)
                        results.append(processed_doc)
                else:
                    # 非重复文档
                    processed_doc = await self._create_processed_document(doc)
                    results.append(processed_doc)

            # 添加到语义去重库
            for doc, (is_dup, dup_id) in zip(documents, batch_results):
                if not is_dup:
                    await self.semantic_deduplicator.add_document(
                        doc_id=doc.doc_id,
                        content=doc.content,
                        title=doc.title or "",
                        url=doc.url or "",
                        metadata={"source": doc.source_id, "type": doc.type.value}
                    )

            return results

        except Exception as e:
            self.llm_stats["llm_failures"] += 1
            logger.error(f"批量LLM去重失败: {e}")

            # 回退到传统批量去重
            return await super().batch_deduplicate(documents)

    async def find_similar_documents(
        self,
        content: str,
        max_results: int = 5
    ) -> List[Dict[str, Any]]:
        """查找相似文档

        Args:
            content: 查询内容
            max_results: 最大返回结果数

        Returns:
            相似文档列表
        """
        if not self.enabled or not self.semantic_deduplicator:
            return []

        try:
            similar_docs = await self.semantic_deduplicator.find_similar_documents(
                content=content,
                max_results=max_results
            )

            # 补充文档信息
            results = []
            for doc_id, similarity_score in similar_docs:
                doc = await self._get_duplicate_document(doc_id)
                if doc:
                    results.append({
                        "doc_id": doc_id,
                        "similarity_score": similarity_score,
                        "title": doc.title,
                        "url": doc.url,
                        "source_id": doc.source_id,
                        "type": doc.type.value,
                        "extracted_at": doc.extracted_at.isoformat() if doc.extracted_at else None,
                    })

            return results

        except Exception as e:
            logger.error(f"查找相似文档失败: {e}")
            return []

    async def _create_processed_document(self, doc: RawDocument) -> ProcessedDocument:
        """创建处理后的文档"""
        processed_doc = ProcessedDocument(
            doc_id=doc.doc_id,
            source_id=doc.source_id,
            type=doc.type,
            title=doc.title,
            url=doc.url,
            content=doc.content,
            raw_content_hash=doc.content_hash,
            extracted_at=doc.extracted_at,
            metadata=doc.metadata or {}
        )

        processed_doc.is_duplicate = False

        # 添加到传统哈希去重库
        await self._add_to_dedup_index(processed_doc)

        return processed_doc

    async def _get_duplicate_document(self, doc_id: str) -> Optional[ProcessedDocument]:
        """获取重复的已处理文档"""
        try:
            # 这里应该从数据库或存储中获取已处理的文档
            # 简化实现，返回None
            return None
        except Exception as e:
            logger.error(f"获取重复文档失败: {e}")
            return None

    def get_dedup_stats(self) -> Dict[str, Any]:
        """获取去重统计信息"""
        base_stats = super().get_dedup_stats()

        # 合并LLM统计
        base_stats.update({
            "llm_enabled": self.enabled,
            "dedup_strategy": self.dedup_strategy.value,
            "similarity_threshold": self.similarity_threshold,
            "llm_stats": self.llm_stats.copy(),
        })

        # 添加语义去重统计
        if self.semantic_deduplicator:
            semantic_stats = self.semantic_deduplicator.get_stats()
            base_stats["semantic_stats"] = semantic_stats

        return base_stats

    async def cleanup(self) -> None:
        """清理资源"""
        try:
            if self.llm_client:
                await self.llm_client.close()

            if self.semantic_deduplicator:
                self.semantic_deduplicator.clear_cache()

            logger.info("LLM去重处理器资源清理完成")

        except Exception as e:
            logger.error(f"LLM去重处理器清理失败: {e}")

        finally:
            await super().cleanup()

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        health = await super().health_check()

        if not self.enabled:
            health["llm_status"] = "disabled"
            return health

        try:
            # 检查LLM客户端健康状态
            if self.llm_client:
                llm_health = await self.llm_client.health_check()
                health["llm_status"] = llm_health.get("status", "unknown")
                health["llm_models"] = llm_health.get("models_count", 0)
                health["llm_stats"] = self.llm_client.get_stats()
            else:
                health["llm_status"] = "not_initialized"

            # 检查语义去重器状态
            if self.semantic_deduplicator:
                semantic_stats = self.semantic_deduplicator.get_stats()
                health["semantic_dedup_status"] = "healthy"
                health["semantic_stats"] = semantic_stats
            else:
                health["semantic_dedup_status"] = "not_initialized"

        except Exception as e:
            health["llm_status"] = "error"
            health["llm_error"] = str(e)

        return health


# 便利函数
def create_llm_deduplicator(config, cache_dir: Optional[Path] = None) -> LLMDeduplicator:
    """创建LLM去重处理器

    Args:
        config: Atlas配置
        cache_dir: 缓存目录

    Returns:
        LLM去重处理器实例
    """
    return LLMDeduplicator(config, cache_dir)