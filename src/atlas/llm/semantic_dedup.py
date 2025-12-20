"""
语义去重模块

使用 LLM 进行智能语义相似度分析，实现高级内容去重。
结合传统哈希去重和语义分析，提供准确的内容重复检测。
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class DedupStrategy(Enum):
    """去重策略"""
    HASH_ONLY = "hash_only"           # 仅哈希去重
    SEMANTIC_ONLY = "semantic_only"   # 仅语义去重
    HYBRID = "hybrid"                 # 混合策略（推荐）


class SimilarityThreshold(Enum):
    """相似度阈值"""
    VERY_STRICT = 0.95    # 极严格
    STRICT = 0.90          # 严格
    NORMAL = 0.85          // 正常（推荐）
    LOOSE = 0.80           // 宽松
    VERY_LOOSE = 0.75      // 极宽松


@dataclass
class SemanticConfig:
    """语义去重配置"""
    # 策略配置
    strategy: DedupStrategy = DedupStrategy.HYBRID
    similarity_threshold: float = SimilarityThreshold.NORMAL.value

    # LLM 配置
    llm_model: str = "llama2"
    max_content_length: int = 2000  # 用于相似度分析的最大内容长度
    batch_size: int = 5             # 批处理大小

    # 缓存配置
    cache_enabled: bool = True
    cache_ttl: int = 3600          # 缓存TTL（秒）

    # 性能配置
    min_content_length: int = 50   # 最小内容长度
    enable_tfidf_fallback: bool = True  # 启用TF-IDF作为后备

    # 特殊处理
    ignore_urls: bool = True        # 忽略URL比较
    normalize_text: bool = True     # 标准化文本


@dataclass
class ContentSignature:
    """内容签名"""
    doc_id: str
    hash_signature: str            # 传统哈希签名
    semantic_signature: Optional[np.ndarray] = None  # 语义向量签名
    title: str = ""
    url: str = ""
    content_preview: str = ""     # 内容预览
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SemanticDeduplicator:
    """语义去重器"""

    def __init__(
        self,
        config: SemanticConfig,
        llm_client=None,
        cache_file: Optional[Path] = None
    ):
        """初始化语义去重器

        Args:
            config: 去重配置
            llm_client: LLM客户端实例
            cache_file: 缓存文件路径
        """
        self.config = config
        self.llm_client = llm_client

        # 内容存储
        self._signatures: Dict[str, ContentSignature] = {}
        self._hash_index: Dict[str, Set[str]] = {}  # 哈希索引
        self._semantic_cache: Dict[str, np.ndarray] = {}  # 语义向量缓存

        # 缓存管理
        self.cache_file = cache_file
        self._cache_time: float = 0

        # TF-IDF向量化器（作为后备）
        self._tfidf_vectorizer: Optional[TfidfVectorizer] = None

        # 统计信息
        self._stats = {
            "total_processed": 0,
            "hash_duplicates": 0,
            "semantic_duplicates": 0,
            "hybrid_duplicates": 0,
            "llm_calls": 0,
            "tfidf_calls": 0,
            "cache_hits": 0,
        }

        # 加载缓存
        if self.config.cache_enabled and cache_file and cache_file.exists():
            self._load_cache()

        logger.info(f"语义去重器初始化完成，策略: {self.config.strategy.value}")

    async def is_duplicate(
        self,
        doc_id: str,
        content: str,
        title: str = "",
        url: str = "",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str]]:
        """检查是否重复

        Args:
            doc_id: 文档ID
            content: 文档内容
            title: 文档标题
            url: 文档URL
            metadata: 元数据

        Returns:
            (是否重复, 重复的文档ID)
        """
        if doc_id in self._signatures:
            logger.warning(f"文档ID已存在: {doc_id}")
            return True, doc_id

        # 检查内容长度
        if len(content) < self.config.min_content_length:
            logger.debug(f"内容过短，跳过去重检查: {len(content)} < {self.config.min_content_length}")
            return False, None

        self._stats["total_processed"] += 1

        try:
            # 生成哈希签名
            hash_signature = self._generate_hash_signature(content)

            # 根据策略进行检查
            if self.config.strategy == DedupStrategy.HASH_ONLY:
                return await self._check_hash_duplicate(doc_id, hash_signature)

            elif self.config.strategy == DedupStrategy.SEMANTIC_ONLY:
                return await self._check_semantic_duplicate(doc_id, content, title, url, metadata)

            else:  # HYBRID
                # 先检查哈希重复（快速）
                is_hash_dup, dup_id = await self._check_hash_duplicate(doc_id, hash_signature)
                if is_hash_dup:
                    return True, dup_id

                # 再检查语义重复
                return await self._check_semantic_duplicate(doc_id, content, title, url, metadata)

        except Exception as e:
            logger.error(f"去重检查失败: {e}")
            return False, None

    async def add_document(
        self,
        doc_id: str,
        content: str,
        title: str = "",
        url: str = "",
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """添加文档到去重库

        Args:
            doc_id: 文档ID
            content: 文档内容
            title: 文档标题
            url: 文档URL
            metadata: 元数据
        """
        if doc_id in self._signatures:
            logger.warning(f"文档ID已存在，跳过添加: {doc_id}")
            return

        # 生成签名
        hash_signature = self._generate_hash_signature(content)
        semantic_signature = None

        # 生成语义签名
        if self.config.strategy in [DedupStrategy.SEMANTIC_ONLY, DedupStrategy.HYBRID]:
            semantic_signature = await self._generate_semantic_signature(content)

        # 创建内容签名
        signature = ContentSignature(
            doc_id=doc_id,
            hash_signature=hash_signature,
            semantic_signature=semantic_signature,
            title=title,
            url=url,
            content_preview=content[:200],
            metadata=metadata or {}
        )

        # 存储
        self._signatures[doc_id] = signature

        # 更新哈希索引
        if hash_signature not in self._hash_index:
            self._hash_index[hash_signature] = set()
        self._hash_index[hash_signature].add(doc_id)

        # 保存缓存
        if self.config.cache_enabled:
            self._save_cache()

        logger.debug(f"文档已添加到去重库: {doc_id}")

    async def remove_document(self, doc_id: str) -> bool:
        """移除文档

        Args:
            doc_id: 文档ID

        Returns:
            是否成功移除
        """
        if doc_id not in self._signatures:
            return False

        signature = self._signatures[doc_id]

        # 从哈希索引中移除
        if signature.hash_signature in self._hash_index:
            self._hash_index[signature.hash_signature].discard(doc_id)
            if not self._hash_index[signature.hash_signature]:
                del self._hash_index[signature.hash_signature]

        # 从签名库中移除
        del self._signatures[doc_id]

        # 保存缓存
        if self.config.cache_enabled:
            self._save_cache()

        logger.debug(f"文档已从去重库移除: {doc_id}")
        return True

    async def find_similar_documents(
        self,
        content: str,
        max_results: int = 5,
        threshold: Optional[float] = None
    ) -> List[Tuple[str, float]]:
        """查找相似文档

        Args:
            content: 查询内容
            max_results: 最大返回结果数
            threshold: 相似度阈值

        Returns:
            相似文档列表 [(doc_id, similarity_score), ...]
        """
        if self.config.strategy == DedupStrategy.HASH_ONLY:
            return []

        # 生成查询的语义签名
        query_signature = await self._generate_semantic_signature(content)
        if query_signature is None:
            return []

        similarities = []
        threshold = threshold or self.config.similarity_threshold

        # 计算相似度
        for doc_id, signature in self._signatures.items():
            if signature.semantic_signature is not None:
                similarity = self._calculate_similarity(query_signature, signature.semantic_signature)
                if similarity >= threshold:
                    similarities.append((doc_id, similarity))

        # 排序并返回
        similarities.sort(key=lambda x: x[1], reverse=True)
        return similarities[:max_results]

    async def batch_check_duplicates(
        self,
        documents: List[Tuple[str, str, str, str, Dict[str, Any]]]
    ) -> List[Tuple[bool, Optional[str]]]:
        """批量检查重复

        Args:
            documents: 文档列表 [(doc_id, content, title, url, metadata), ...]

        Returns:
            检查结果列表 [(is_duplicate, duplicate_doc_id), ...]
        """
        results = []

        for doc_id, content, title, url, metadata in documents:
            is_dup, dup_id = await self.is_duplicate(doc_id, content, title, url, metadata)
            results.append((is_dup, dup_id))

        return results

    def _generate_hash_signature(self, content: str) -> str:
        """生成哈希签名"""
        # 标准化内容
        if self.config.normalize_text:
            content = self._normalize_text(content)

        # 生成哈希
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    async def _generate_semantic_signature(self, content: str) -> Optional[np.ndarray]:
        """生成语义签名"""
        try:
            # 检查缓存
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
            if content_hash in self._semantic_cache:
                self._stats["cache_hits"] += 1
                return self._semantic_cache[content_hash]

            # 截断内容
            truncated_content = content[:self.config.max_content_length]

            if self.llm_client:
                # 使用LLM生成嵌入
                embedding = await self.llm_client.embed(truncated_content)
                if embedding:
                    signature = np.array(embedding)
                    self._stats["llm_calls"] += 1
                else:
                    signature = None
            else:
                # 使用TF-IDF作为后备
                signature = await self._generate_tfidf_signature(truncated_content)
                self._stats["tfidf_calls"] += 1

            # 缓存结果
            if signature is not None and self.config.cache_enabled:
                self._semantic_cache[content_hash] = signature

            return signature

        except Exception as e:
            logger.error(f"生成语义签名失败: {e}")
            return None

    async def _generate_tfidf_signature(self, content: str) -> Optional[np.ndarray]:
        """生成TF-IDF签名（后备方案）"""
        try:
            if self._tfidf_vectorizer is None:
                # 收集所有文档内容用于训练TF-IDF
                all_content = [s.content_preview for s in self._signatures.values() if s.content_preview]
                all_content.append(content)

                if len(all_content) < 2:
                    return None

                # 训练TF-IDF向量化器
                self._tfidf_vectorizer = TfidfVectorizer(
                    max_features=1000,
                    stop_words='english',
                    ngram_range=(1, 2)
                )
                self._tfidf_vectorizer.fit(all_content)

            # 生成向量
            vector = self._tfidf_vectorizer.transform([content])
            return vector.toarray()[0]

        except Exception as e:
            logger.error(f"生成TF-IDF签名失败: {e}")
            return None

    async def _check_hash_duplicate(self, doc_id: str, hash_signature: str) -> Tuple[bool, Optional[str]]:
        """检查哈希重复"""
        if hash_signature in self._hash_index and self._hash_index[hash_signature]:
            self._stats["hash_duplicates"] += 1
            # 返回第一个重复的文档ID（排除当前文档）
            for existing_doc_id in self._hash_index[hash_signature]:
                if existing_doc_id != doc_id:
                    return True, existing_doc_id

        return False, None

    async def _check_semantic_duplicate(
        self,
        doc_id: str,
        content: str,
        title: str = "",
        url: str = "",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, Optional[str]]:
        """检查语义重复"""
        # 生成语义签名
        semantic_signature = await self._generate_semantic_signature(content)
        if semantic_signature is None:
            return False, None

        # 查找相似文档
        similar_docs = await self.find_similar_documents(
            content,
            max_results=1,
            threshold=self.config.similarity_threshold
        )

        if similar_docs:
            self._stats["semantic_duplicates"] += 1
            return True, similar_docs[0][0]

        return False, None

    def _normalize_text(self, text: str) -> str:
        """标准化文本"""
        import re

        # 移除多余空白
        text = re.sub(r'\s+', ' ', text)

        # 移除URL（如果配置要求）
        if self.config.ignore_urls:
            text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', text)

        # 转换为小写
        text = text.lower().strip()

        return text

    def _calculate_similarity(self, signature1: np.ndarray, signature2: np.ndarray) -> float:
        """计算相似度"""
        try:
            # 确保向量形状一致
            if len(signature1.shape) == 1:
                signature1 = signature1.reshape(1, -1)
            if len(signature2.shape) == 1:
                signature2 = signature2.reshape(1, -1)

            # 计算余弦相似度
            similarity = cosine_similarity(signature1, signature2)[0][0]
            return float(similarity)

        except Exception as e:
            logger.error(f"计算相似度失败: {e}")
            return 0.0

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._stats.copy()
        stats["total_documents"] = len(self._signatures)
        stats["cache_size"] = len(self._semantic_cache)
        stats["hash_index_size"] = len(self._hash_index)

        if stats["total_processed"] > 0:
            stats["duplicate_rate"] = (stats["hash_duplicates"] + stats["semantic_duplicates"]) / stats["total_processed"] * 100
        else:
            stats["duplicate_rate"] = 0.0

        return stats

    def reset_stats(self) -> None:
        """重置统计信息"""
        self._stats = {
            "total_processed": 0,
            "hash_duplicates": 0,
            "semantic_duplicates": 0,
            "hybrid_duplicates": 0,
            "llm_calls": 0,
            "tfidf_calls": 0,
            "cache_hits": 0,
        }

    def _save_cache(self) -> None:
        """保存缓存"""
        if not self.cache_file:
            return

        try:
            # 准备缓存数据
            cache_data = {
                "signatures": {
                    doc_id: {
                        "doc_id": sig.doc_id,
                        "hash_signature": sig.hash_signature,
                        "title": sig.title,
                        "url": sig.url,
                        "content_preview": sig.content_preview,
                        "timestamp": sig.timestamp,
                        "metadata": sig.metadata,
                        # 不保存语义签名，因为numpy数组不能直接JSON序列化
                    }
                    for doc_id, sig in self._signatures.items()
                },
                "hash_index": {
                    hash_sig: list(doc_ids)
                    for hash_sig, doc_ids in self._hash_index.items()
                },
                "cache_time": time.time(),
                "stats": self._stats,
            }

            # 保存到文件
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, indent=2, ensure_ascii=False)

            self._cache_time = time.time()
            logger.debug(f"缓存已保存到: {self.cache_file}")

        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    def _load_cache(self) -> None:
        """加载缓存"""
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)

            # 恢复签名
            self._signatures = {}
            for doc_id, sig_data in cache_data.get("signatures", {}).items():
                self._signatures[doc_id] = ContentSignature(**sig_data)

            # 恢复哈希索引
            self._hash_index = {
                hash_sig: set(doc_ids)
                for hash_sig, doc_ids in cache_data.get("hash_index", {}).items()
            }

            # 恢复统计信息
            self._stats = cache_data.get("stats", self._stats)
            self._cache_time = cache_data.get("cache_time", time.time())

            logger.debug(f"缓存已加载: {len(self._signatures)} 个文档")

        except Exception as e:
            logger.error(f"加载缓存失败: {e}")
            self._signatures = {}
            self._hash_index = {}

    def clear_cache(self) -> None:
        """清空缓存"""
        self._signatures.clear()
        self._hash_index.clear()
        self._semantic_cache.clear()
        self._cache_time = 0

        if self.cache_file and self.cache_file.exists():
            self.cache_file.unlink()

        logger.info("去重缓存已清空")


# 便利函数
def create_semantic_deduplicator(
    strategy: DedupStrategy = DedupStrategy.HYBRID,
    similarity_threshold: float = SimilarityThreshold.NORMAL.value,
    llm_client=None,
    cache_file: Optional[Path] = None,
    **kwargs
) -> SemanticDeduplicator:
    """创建语义去重器

    Args:
        strategy: 去重策略
        similarity_threshold: 相似度阈值
        llm_client: LLM客户端
        cache_file: 缓存文件路径
        **kwargs: 其他配置参数

    Returns:
        语义去重器实例
    """
    config = SemanticConfig(
        strategy=strategy,
        similarity_threshold=similarity_threshold,
        **kwargs
    )
    return SemanticDeduplicator(config, llm_client, cache_file)