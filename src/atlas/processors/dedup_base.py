"""
基础内容去重模块

提供多种内容去重算法，包括哈希去重、相似度去重和语义去重。
"""

import hashlib
import re
import difflib
from typing import Dict, List, Set, Optional, Any, Tuple, Union
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from atlas.core.logging import get_logger

logger = get_logger(__name__)


class HashStrategy(Enum):
    """哈希策略枚举"""
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SIMHASH = "simhash"


@dataclass
class DeduplicationConfig:
    """去重配置"""
    hash_strategy: HashStrategy = HashStrategy.SHA256
    similarity_threshold: float = 0.85
    min_content_length: int = 50
    ignore_whitespace: bool = True
    case_sensitive: bool = False
    check_title: bool = True
    title_weight: float = 0.3
    content_weight: float = 0.7


@dataclass
class ContentFingerprint:
    """内容指纹"""
    content_hash: str
    title_hash: Optional[str] = None
    content_length: int = 0
    word_count: int = 0
    fingerprint_data: Dict[str, Any] = None

    def __post_init__(self):
        if self.fingerprint_data is None:
            self.fingerprint_data = {}


class ContentDeduplicator:
    """内容去重器"""

    def __init__(self, config: Optional[DeduplicationConfig] = None):
        """
        初始化内容去重器

        Args:
            config: 去重配置
        """
        self.config = config or DeduplicationConfig()
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

        # 存储已见过的内容指纹
        self.seen_hashes: Set[str] = set()
        self.title_hashes: Set[str] = set()
        self.fingerprints: Dict[str, ContentFingerprint] = {}

        # 统计信息
        self.stats = {
            'total_checked': 0,
            'duplicates_found': 0,
            'title_duplicates': 0,
            'content_duplicates': 0,
            'similar_content': 0
        }

    def is_duplicate(self, document: Dict[str, Any]) -> bool:
        """
        检查文档是否重复

        Args:
            document: 文档数据

        Returns:
            是否重复
        """
        try:
            self.stats['total_checked'] += 1

            # 提取内容
            title = document.get('title', '')
            content = document.get('content', '')

            if not content or len(content.strip()) < self.config.min_content_length:
                return False

            # 生成内容指纹
            fingerprint = self._generate_fingerprint(title, content)

            # 检查标题重复
            if self.config.check_title and fingerprint.title_hash:
                if fingerprint.title_hash in self.title_hashes:
                    self.stats['title_duplicates'] += 1
                    self.stats['duplicates_found'] += 1
                    self.logger.debug(f"Title duplicate found: {title[:50]}...")
                    return True

            # 检查内容哈希重复
            if fingerprint.content_hash in self.seen_hashes:
                self.stats['content_duplicates'] += 1
                self.stats['duplicates_found'] += 1
                self.logger.debug(f"Content hash duplicate found")
                return True

            # 检查相似内容
            if self._is_similar_content(fingerprint):
                self.stats['similar_content'] += 1
                self.stats['duplicates_found'] += 1
                self.logger.debug(f"Similar content found (threshold: {self.config.similarity_threshold})")
                return True

            # 记录新的指纹
            self._record_fingerprint(fingerprint)

            return False

        except Exception as e:
            self.logger.error(f"Error checking duplicate: {e}")
            return False

    def _generate_fingerprint(self, title: str, content: str) -> ContentFingerprint:
        """生成内容指纹"""
        # 预处理内容
        processed_content = self._preprocess_content(content)
        processed_title = self._preprocess_content(title) if title else ""

        # 生成哈希
        content_hash = self._generate_hash(processed_content, self.config.hash_strategy)
        title_hash = self._generate_hash(processed_title, self.config.hash_strategy) if title else None

        # 计算其他特征
        content_length = len(processed_content)
        word_count = len(processed_content.split())

        # 生成额外的指纹数据
        fingerprint_data = {
            'first_line': processed_content.split('\n')[0][:100],
            'last_line': processed_content.split('\n')[-1][:100],
            'keywords': self._extract_keywords(processed_content),
            'structure_hash': self._generate_structure_hash(processed_content)
        }

        return ContentFingerprint(
            content_hash=content_hash,
            title_hash=title_hash,
            content_length=content_length,
            word_count=word_count,
            fingerprint_data=fingerprint_data
        )

    def _preprocess_content(self, content: str) -> str:
        """预处理内容"""
        if not content:
            return ""

        processed = content

        # 转换为小写
        if not self.config.case_sensitive:
            processed = processed.lower()

        # 忽略空白字符差异
        if self.config.ignore_whitespace:
            processed = re.sub(r'\s+', ' ', processed).strip()

        return processed

    def _generate_hash(self, content: str, strategy: HashStrategy) -> str:
        """生成哈希值"""
        if not content:
            return ""

        if strategy == HashStrategy.MD5:
            return hashlib.md5(content.encode('utf-8')).hexdigest()
        elif strategy == HashStrategy.SHA1:
            return hashlib.sha1(content.encode('utf-8')).hexdigest()
        elif strategy == HashStrategy.SHA256:
            return hashlib.sha256(content.encode('utf-8')).hexdigest()
        elif strategy == HashStrategy.SIMHASH:
            return self._simhash(content)
        else:
            return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def _simhash(self, content: str) -> str:
        """计算SimHash值"""
        # 简化的SimHash实现
        if not content:
            return ""

        words = content.split()
        if not words:
            return "0" * 64

        # 计算词频
        word_weights = {}
        for word in words:
            word_weights[word] = word_weights.get(word, 0) + 1

        # 初始化SimHash向量
        hash_bits = 64
        simhash_vector = [0] * hash_bits

        # 为每个词计算哈希并累加
        for word, weight in word_weights.items():
            word_hash = hashlib.md5(word.encode('utf-8')).hexdigest()
            for i in range(hash_bits):
                bit = int(word_hash[i * 4:(i + 1) * 4], 16) & 1
                simhash_vector[i] += weight if bit else -weight

        # 生成最终的SimHash值
        simhash_value = 0
        for i in range(hash_bits):
            if simhash_vector[i] > 0:
                simhash_value |= (1 << (hash_bits - 1 - i))

        return format(simhash_value, f'0{hash_bits}x')

    def _is_similar_content(self, fingerprint: ContentFingerprint) -> bool:
        """检查内容相似性"""
        if not self.fingerprints:
            return False

        best_similarity = 0.0

        for existing_hash, existing_fingerprint in self.fingerprints.items():
            # 计算哈希相似度
            hash_similarity = self._calculate_hash_similarity(
                fingerprint.content_hash, existing_fingerprint.content_hash
            )

            # 计算长度相似度
            length_similarity = self._calculate_length_similarity(
                fingerprint.content_length, existing_fingerprint.content_length
            )

            # 计算词数相似度
            word_similarity = self._calculate_word_count_similarity(
                fingerprint.word_count, existing_fingerprint.word_count
            )

            # 计算结构相似度
            structure_similarity = self._calculate_structure_similarity(
                fingerprint.fingerprint_data.get('structure_hash', ''),
                existing_fingerprint.fingerprint_data.get('structure_hash', '')
            )

            # 综合相似度（可调整权重）
            overall_similarity = (
                0.4 * hash_similarity +
                0.2 * length_similarity +
                0.2 * word_similarity +
                0.2 * structure_similarity
            )

            best_similarity = max(best_similarity, overall_similarity)

        return best_similarity >= self.config.similarity_threshold

    def _calculate_hash_similarity(self, hash1: str, hash2: str) -> float:
        """计算哈希相似度"""
        if not hash1 or not hash2 or len(hash1) != len(hash2):
            return 0.0

        # 计算汉明距离
        hamming_distance = sum(c1 != c2 for c1, c2 in zip(hash1, hash2))
        max_distance = len(hash1) * 4  # 每个字符4位
        similarity = 1.0 - (hamming_distance / max_distance)

        return max(0.0, similarity)

    def _calculate_length_similarity(self, len1: int, len2: int) -> float:
        """计算长度相似度"""
        if len1 == 0 and len2 == 0:
            return 1.0
        if len1 == 0 or len2 == 0:
            return 0.0

        shorter, longer = min(len1, len2), max(len1, len2)
        return shorter / longer

    def _calculate_word_count_similarity(self, count1: int, count2: int) -> float:
        """计算词数相似度"""
        return self._calculate_length_similarity(count1, count2)

    def _calculate_structure_similarity(self, struct1: str, struct2: str) -> float:
        """计算结构相似度"""
        if not struct1 or not struct2:
            return 0.0

        # 简单的字符串相似度
        return difflib.SequenceMatcher(None, struct1, struct2).ratio()

    def _extract_keywords(self, content: str) -> List[str]:
        """提取关键词"""
        if not content:
            return []

        # 简化的关键词提取
        words = re.findall(r'\b\w+\b', content.lower())
        word_freq = {}

        for word in words:
            if len(word) > 3:  # 忽略短词
                word_freq[word] = word_freq.get(word, 0) + 1

        # 返回频率最高的5个词
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [word for word, freq in sorted_words[:5]]

    def _generate_structure_hash(self, content: str) -> str:
        """生成内容结构哈希"""
        if not content:
            return ""

        # 提取结构信息
        lines = content.split('\n')
        structure_info = []

        for line in lines:
            # 记录每行的长度特征
            line_length = len(line.strip())
            if line_length > 0:
                # 将长度分级：短(1-20)、中(21-100)、长(100+)
                if line_length <= 20:
                    structure_info.append('S')
                elif line_length <= 100:
                    structure_info.append('M')
                else:
                    structure_info.append('L')

        # 生成结构哈希
        structure_pattern = ''.join(structure_info)
        return self._generate_hash(structure_pattern, HashStrategy.MD5)

    def _record_fingerprint(self, fingerprint: ContentFingerprint) -> None:
        """记录内容指纹"""
        self.seen_hashes.add(fingerprint.content_hash)
        if fingerprint.title_hash:
            self.title_hashes.add(fingerprint.title_hash)
        self.fingerprints[fingerprint.content_hash] = fingerprint

    def get_stats(self) -> Dict[str, Any]:
        """获取去重统计信息"""
        total = self.stats['total_checked']
        if total == 0:
            return self.stats

        return {
            **self.stats,
            'duplicate_rate': self.stats['duplicates_found'] / total,
            'unique_content': total - self.stats['duplicates_found'],
            'unique_rate': (total - self.stats['duplicates_found']) / total,
            'total_fingerprints': len(self.fingerprints)
        }

    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats = {
            'total_checked': 0,
            'duplicates_found': 0,
            'title_duplicates': 0,
            'content_duplicates': 0,
            'similar_content': 0
        }

    def clear_cache(self) -> None:
        """清空指纹缓存"""
        self.seen_hashes.clear()
        self.title_hashes.clear()
        self.fingerprints.clear()
        self.reset_stats()
        self.logger.info("Deduplicator cache cleared")

    def export_fingerprints(self, file_path: Union[str, Path]) -> None:
        """导出指纹数据"""
        file_path = Path(file_path)

        fingerprint_data = []
        for hash_val, fingerprint in self.fingerprints.items():
            fingerprint_data.append({
                'content_hash': fingerprint.content_hash,
                'title_hash': fingerprint.title_hash,
                'content_length': fingerprint.content_length,
                'word_count': fingerprint.word_count,
                'fingerprint_data': fingerprint.fingerprint_data
            })

        import json
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(fingerprint_data, f, ensure_ascii=False, indent=2)

        self.logger.info(f"Exported {len(fingerprint_data)} fingerprints to {file_path}")

    def import_fingerprints(self, file_path: Union[str, Path]) -> None:
        """导入指纹数据"""
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Fingerprint file not found: {file_path}")

        import json
        with open(file_path, 'r', encoding='utf-8') as f:
            fingerprint_data = json.load(f)

        imported_count = 0
        for item in fingerprint_data:
            fingerprint = ContentFingerprint(
                content_hash=item['content_hash'],
                title_hash=item.get('title_hash'),
                content_length=item.get('content_length', 0),
                word_count=item.get('word_count', 0),
                fingerprint_data=item.get('fingerprint_data', {})
            )

            self._record_fingerprint(fingerprint)
            imported_count += 1

        self.logger.info(f"Imported {imported_count} fingerprints from {file_path}")


class BatchDeduplicator:
    """批量去重器"""

    def __init__(self, config: Optional[DeduplicationConfig] = None):
        self.deduplicator = ContentDeduplicator(config)
        self.logger = get_logger(f"{__name__}.{self.__class__.__name__}")

    def deduplicate_documents(self, documents: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[int]]:
        """
        批量去重文档

        Args:
            documents: 文档列表

        Returns:
            (去重后的文档列表, 重复文档的索引列表)
        """
        unique_documents = []
        duplicate_indices = []

        for i, document in enumerate(documents):
            if not self.deduplicator.is_duplicate(document):
                unique_documents.append(document)
            else:
                duplicate_indices.append(i)

        stats = self.deduplicator.get_stats()
        self.logger.info(f"Batch deduplication completed: {len(unique_documents)} unique, {len(duplicate_indices)} duplicates")

        return unique_documents, duplicate_indices

    def find_duplicates(self, documents: List[Dict[str, Any]]) -> List[List[int]]:
        """
        查找重复文档组

        Args:
            documents: 文档列表

        Returns:
            重复文档组的索引列表
        """
        # 重置去重器状态
        self.deduplicator.clear_cache()

        document_groups = {}
        duplicate_groups = []

        for i, document in enumerate(documents):
            title = document.get('title', '')
            content = document.get('content', '')

            fingerprint = self.deduplicator._generate_fingerprint(title, content)

            # 检查是否与现有文档重复
            found_group = None
            for group_hash, group_indices in document_groups.items():
                existing_fingerprint = self.deduplicator.fingerprints.get(group_hash)
                if existing_fingerprint:
                    similarity = self.deduplicator._calculate_hash_similarity(
                        fingerprint.content_hash, existing_fingerprint.content_hash
                    )
                    if similarity >= self.deduplicator.config.similarity_threshold:
                        found_group = group_hash
                        break

            if found_group:
                document_groups[found_group].append(i)
            else:
                document_groups[fingerprint.content_hash] = [i]
                self.deduplicator._record_fingerprint(fingerprint)

        # 收集重复组
        for group_indices in document_groups.values():
            if len(group_indices) > 1:
                duplicate_groups.append(group_indices)

        return duplicate_groups