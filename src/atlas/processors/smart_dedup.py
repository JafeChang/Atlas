"""
智能去重系统

提供多种去重策略和增量去重功能。
简化版本，不依赖外部向量数据库。

支持的去重策略：
- 精确去重（哈希）
- 相似度去重（编辑距离、余弦相似度）
- 增量去重（流式处理）
"""

import hashlib
import re
from typing import Dict, List, Set, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class DedupResult:
    """去重结果"""
    is_duplicate: bool
    duplicate_id: Optional[str] = None
    similarity_score: Optional[float] = None
    dedup_strategy: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class DocumentFingerprint:
    """文档指纹"""
    doc_id: str
    title_hash: str
    content_hash: str
    url_hash: Optional[str] = None
    simhash: Optional[int] = None
    word_count: int = 0
    char_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)


class SimHash:
    """SimHash算法实现"""

    def __init__(self, hash_bits: int = 64):
        self.hash_bits = hash_bits

    def compute(self, text: str) -> int:
        """
        计算SimHash值

        Args:
            text: 输入文本

        Returns:
            int: SimHash值
        """
        # 分词
        words = self._tokenize(text)

        if not words:
            return 0

        # 计算每个词的哈希
        hash_vectors = [0] * self.hash_bits

        for word, weight in words:
            # 计算词的哈希
            word_hash = int(hashlib.md5(word.encode()).hexdigest(), 16)

            # 根据哈希值更新向量
            for i in range(self.hash_bits):
                if (word_hash >> i) & 1:
                    hash_vectors[i] += weight
                else:
                    hash_vectors[i] -= weight

        # 生成SimHash
        simhash = 0
        for i in range(self.hash_bits):
            if hash_vectors[i] >= 0:
                simhash |= (1 << i)

        return simhash

    def _tokenize(self, text: str) -> List[Tuple[str, int]]:
        """
        分词并计算词频

        Args:
            text: 输入文本

        Returns:
            List[Tuple[str, int]]: (词, 权重)列表
        """
        # 简单分词（中文按字符，英文按单词）
        words = []

        # 移除特殊字符
        text = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', text)

        # 分词
        tokens = text.lower().split()

        # 计算词频
        word_count = defaultdict(int)
        for token in tokens:
            word_count[token] += 1

        # 转换为(词, 权重)列表
        total = sum(word_count.values())
        for word, count in word_count.items():
            weight = count / total if total > 0 else 0
            words.append((word, weight))

        return words

    def similarity(self, hash1: int, hash2: int) -> float:
        """
        计算两个SimHash的相似度（汉明距离）

        Args:
            hash1: 第一个哈希值
            hash2: 第二个哈希值

        Returns:
            float: 相似度（0-1）
        """
        if hash1 == hash2:
            return 1.0

        # 计算汉明距离
        x = hash1 ^ hash2
        distance = 0
        while x:
            distance += 1
            x &= x - 1

        # 转换为相似度
        similarity = 1.0 - (distance / self.hash_bits)
        return similarity


class TextSimilarity:
    """文本相似度计算"""

    @staticmethod
    def jaccard_similarity(text1: str, text2: str) -> float:
        """
        计算Jaccard相似度

        Args:
            text1: 文本1
            text2: 文本2

        Returns:
            float: 相似度（0-1）
        """
        # 分词
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 and not words2:
            return 1.0

        if not words1 or not words2:
            return 0.0

        # 计算Jaccard
        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return intersection / union if union > 0 else 0.0

    @staticmethod
    def cosine_similarity(text1: str, text2: str) -> float:
        """
        计算余弦相似度（基于TF）

        Args:
            text1: 文本1
            text2: 文本2

        Returns:
            float: 相似度（0-1）
        """
        # 词频统计
        words1 = text1.lower().split()
        words2 = text2.lower().split()

        # 计算词频向量
        tf1 = defaultdict(int)
        tf2 = defaultdict(int)

        for word in words1:
            tf1[word] += 1

        for word in words2:
            tf2[word] += 1

        # 计算余弦相似度
        dot_product = sum(tf1[w] * tf2[w] for w in set(tf1) & set(tf2))
        norm1 = sum(v ** 2 for v in tf1.values()) ** 0.5
        norm2 = sum(v ** 2 for v in tf2.values()) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)


class IncrementalDeduplicator:
    """增量去重器（流式处理）"""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        enable_simhash: bool = True,
        enable_similarity: bool = True
    ):
        """
        初始化增量去重器

        Args:
            similarity_threshold: 相似度阈值
            enable_simhash: 是否启用SimHash
            enable_similarity: 是否启用相似度计算
        """
        self.similarity_threshold = similarity_threshold
        self.enable_simhash = enable_simhash
        self.enable_similarity = enable_similarity

        # 存储已处理的文档
        self.fingerprints: Dict[str, DocumentFingerprint] = {}
        self.content_hashes: Set[str] = set()
        self.title_hashes: Set[str] = set()
        self.url_hashes: Set[str] = set()

        # SimHash实例
        self.simhash = SimHash() if enable_simhash else None

        # 统计信息
        self.stats = {
            'total_processed': 0,
            'exact_duplicates': 0,
            'near_duplicates': 0,
            'unique_documents': 0
        }

    def check_duplicate(
        self,
        doc_id: str,
        title: str,
        content: str,
        url: Optional[str] = None
    ) -> DedupResult:
        """
        检查文档是否重复

        Args:
            doc_id: 文档ID
            title: 标题
            content: 内容
            url: URL（可选）

        Returns:
            DedupResult: 去重结果
        """
        self.stats['total_processed'] += 1

        # 1. 精确去重（哈希）
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        title_hash = hashlib.sha256(title.encode()).hexdigest()
        url_hash = hashlib.sha256(url.encode()).hexdigest() if url else None

        # 检查URL哈希
        if url_hash and url_hash in self.url_hashes:
            return DedupResult(
                is_duplicate=True,
                dedup_strategy='url_hash',
                reason='URL重复'
            )

        # 检查标题哈希
        if title_hash in self.title_hashes:
            return DedupResult(
                is_duplicate=True,
                dedup_strategy='title_hash',
                reason='标题重复'
            )

        # 检查内容哈希
        if content_hash in self.content_hashes:
            return DedupResult(
                is_duplicate=True,
                dedup_strategy='content_hash',
                reason='内容完全重复'
            )

        # 2. SimHash去重（近似重复）
        if self.enable_simhash and self.simhash:
            simhash_value = self.simhash.compute(content)

            # 查找相似的SimHash
            for fp in self.fingerprints.values():
                if fp.simhash is not None:
                    similarity = self.simhash.similarity(simhash_value, fp.simhash)
                    if similarity >= self.similarity_threshold:
                        self.stats['near_duplicates'] += 1
                        return DedupResult(
                            is_duplicate=True,
                            duplicate_id=fp.doc_id,
                            similarity_score=similarity,
                            dedup_strategy='simhash',
                            reason=f'SimHash相似度{similarity:.2f}'
                        )

        # 3. 文本相似度去重（可选，较慢）
        if self.enable_similarity:
            for fp in self.fingerprints.values():
                # 只对最近的文档计算相似度（性能优化）
                if self.stats['total_processed'] - len(self.fingerprints) < 100:
                    similarity = TextSimilarity.cosine_similarity(content, content)
                    if similarity >= self.similarity_threshold:
                        self.stats['near_duplicates'] += 1
                        return DedupResult(
                            is_duplicate=True,
                            duplicate_id=fp.doc_id,
                            similarity_score=similarity,
                            dedup_strategy='cosine_similarity',
                            reason=f'余弦相似度{similarity:.2f}'
                        )

        # 不重复，存储指纹
        fingerprint = DocumentFingerprint(
            doc_id=doc_id,
            title_hash=title_hash,
            content_hash=content_hash,
            url_hash=url_hash,
            simhash=simhash_value if self.enable_simhash else None,
            word_count=len(content.split()),
            char_count=len(content)
        )

        self.fingerprints[doc_id] = fingerprint
        self.content_hashes.add(content_hash)
        self.title_hashes.add(title_hash)
        if url_hash:
            self.url_hashes.add(url_hash)

        self.stats['unique_documents'] += 1

        return DedupResult(
            is_duplicate=False,
            dedup_strategy='none'
        )

    def add_document(self, doc_id: str, title: str, content: str, url: Optional[str] = None) -> None:
        """
        直接添加文档（不去重）

        Args:
            doc_id: 文档ID
            title: 标题
            content: 内容
            url: URL
        """
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        title_hash = hashlib.sha256(title.encode()).hexdigest()
        url_hash = hashlib.sha256(url.encode()).hexdigest() if url else None

        simhash_value = None
        if self.enable_simhash and self.simhash:
            simhash_value = self.simhash.compute(content)

        fingerprint = DocumentFingerprint(
            doc_id=doc_id,
            title_hash=title_hash,
            content_hash=content_hash,
            url_hash=url_hash,
            simhash=simhash_value,
            word_count=len(content.split()),
            char_count=len(content)
        )

        self.fingerprints[doc_id] = fingerprint
        self.content_hashes.add(content_hash)
        self.title_hashes.add(title_hash)
        if url_hash:
            self.url_hashes.add(url_hash)

        self.stats['total_processed'] += 1
        self.stats['unique_documents'] += 1

    def batch_check(
        self,
        documents: List[Dict[str, Any]]
    ) -> List[DedupResult]:
        """
        批量检查文档

        Args:
            documents: 文档列表，每个文档包含doc_id, title, content, url

        Returns:
            List[DedupResult]: 去重结果列表
        """
        results = []
        for doc in documents:
            result = self.check_duplicate(
                doc_id=doc['doc_id'],
                title=doc.get('title', ''),
                content=doc.get('content', ''),
                url=doc.get('url')
            )
            results.append(result)

        return results

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息

        Returns:
            Dict[str, Any]: 统计数据
        """
        return {
            **self.stats,
            'total_fingerprints': len(self.fingerprints),
            'unique_content_hashes': len(self.content_hashes),
            'unique_title_hashes': len(self.title_hashes),
            'unique_url_hashes': len(self.url_hashes),
            'duplicate_rate': (
                self.stats['exact_duplicates'] + self.stats['near_duplicates']
            ) / max(self.stats['total_processed'], 1)
        }

    def clear(self) -> None:
        """清空所有指纹"""
        self.fingerprints.clear()
        self.content_hashes.clear()
        self.title_hashes.clear()
        self.url_hashes.clear()
        self.stats = {
            'total_processed': 0,
            'exact_duplicates': 0,
            'near_duplicates': 0,
            'unique_documents': 0
        }


class SmartDeduplicator:
    """智能去重器（统一入口）"""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        enable_simhash: bool = True,
        enable_similarity: bool = False  # 默认关闭，较慢
    ):
        """
        初始化智能去重器

        Args:
            similarity_threshold: 相似度阈值（0-1）
            enable_simhash: 启用SimHash
            enable_similarity: 启用相似度计算
        """
        self.deduplicator = IncrementalDeduplicator(
            similarity_threshold=similarity_threshold,
            enable_simhash=enable_simhash,
            enable_similarity=enable_similarity
        )

    def deduplicate(
        self,
        doc_id: str,
        title: str,
        content: str,
        url: Optional[str] = None
    ) -> DedupResult:
        """
        去重检查

        Args:
            doc_id: 文档ID
            title: 标题
            content: 内容
            url: URL

        Returns:
            DedupResult: 去重结果
        """
        return self.deduplicator.check_duplicate(doc_id, title, content, url)

    def batch_deduplicate(
        self,
        documents: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[DedupResult]]:
        """
        批量去重

        Args:
            documents: 文档列表

        Returns:
            Tuple[唯一文档列表, 去重结果列表]
        """
        results = self.deduplicator.batch_check(documents)

        unique_docs = [
            doc for doc, result in zip(documents, results)
            if not result.is_duplicate
        ]

        return unique_docs, results

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return self.deduplicator.get_statistics()


# =============================================================================
# 便捷函数
# =============================================================================

def deduplicate_document(
    doc_id: str,
    title: str,
    content: str,
    url: Optional[str] = None,
    similarity_threshold: float = 0.85
) -> DedupResult:
    """
    文档去重（便捷函数）

    Args:
        doc_id: 文档ID
        title: 标题
        content: 内容
        url: URL
        similarity_threshold: 相似度阈值

    Returns:
        DedupResult: 去重结果
    """
    deduplicator = SmartDeduplicator(similarity_threshold=similarity_threshold)
    return deduplicator.deduplicate(doc_id, title, content, url)


def batch_deduplicate(
    documents: List[Dict[str, Any]],
    similarity_threshold: float = 0.85
) -> List[Dict[str, Any]]:
    """
    批量去重（便捷函数）

    Args:
        documents: 文档列表
        similarity_threshold: 相似度阈值

    Returns:
        List[Dict]: 唯一文档列表
    """
    deduplicator = SmartDeduplicator(similarity_threshold=similarity_threshold)
    unique_docs, results = deduplicator.batch_deduplicate(documents)
    return unique_docs
