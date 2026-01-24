"""
智能去重系统测试脚本
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from atlas.processors.smart_dedup import (
    SmartDeduplicator,
    IncrementalDeduplicator,
    deduplicate_document,
    batch_deduplicate,
    SimHash,
    TextSimilarity
)

print("=" * 80)
print("Atlas 智能去重系统测试")
print("=" * 80)

# 测试1: 精确去重
print("\n1. 测试精确去重...")
dedup = SmartDeduplicator()

result1 = dedup.deduplicate("doc1", "测试标题", "这是测试内容")
print(f"   ✓ 文档1: 是否重复={result1.is_duplicate}")

result2 = dedup.deduplicate("doc2", "测试标题", "这是测试内容")
print(f"   ✓ 文档2（相同内容）: 是否重复={result2.is_duplicate}, 策略={result2.dedup_strategy}")

result3 = dedup.deduplicate("doc3", "不同标题", "完全不同的内容")
print(f"   ✓ 文档3（不同内容）: 是否重复={result3.is_duplicate}")

# 测试2: SimHash去重
print("\n2. 测试SimHash去重...")
simhash = SimHash()

text1 = "这是一个测试文本，包含一些中文内容"
text2 = "这是一个测试文本，包含一些中文的内容"  # 微小差异
text3 = "完全不同的文本内容"

hash1 = simhash.compute(text1)
hash2 = simhash.compute(text2)
hash3 = simhash.compute(text3)

sim1_2 = simhash.similarity(hash1, hash2)
sim1_3 = simhash.similarity(hash1, hash3)

print(f"   ✓ 相似文本相似度: {sim1_2:.3f}")
print(f"   ✓ 不同文本相似度: {sim1_3:.3f}")

# 测试3: 文本相似度
print("\n3. 测试文本相似度计算...")
text_a = "机器学习是人工智能的一个分支"
text_b = "机器学习是人工智能的一个分支"  # 相同
text_c = "深度学习是机器学习的子集"    # 不同

jaccard_ab = TextSimilarity.jaccard_similarity(text_a, text_b)
cosine_ab = TextSimilarity.cosine_similarity(text_a, text_b)
cosine_ac = TextSimilarity.cosine_similarity(text_a, text_c)

print(f"   ✓ Jaccard相似度（相同）: {jaccard_ab:.3f}")
print(f"   ✓ 余弦相似度（相同）: {cosine_ab:.3f}")
print(f"   ✓ 余弦相似度（不同）: {cosine_ac:.3f}")

# 测试4: 批量去重
print("\n4. 测试批量去重...")
documents = [
    {"doc_id": "1", "title": "标题A", "content": "内容X"},
    {"doc_id": "2", "title": "标题B", "content": "内容X"},  # 重复
    {"doc_id": "3", "title": "标题C", "content": "内容Y"},
    {"doc_id": "4", "title": "标题D", "content": "内容X"},  # 重复
    {"doc_id": "5", "title": "标题E", "content": "内容Z"},
]

unique_docs = batch_deduplicate(documents, similarity_threshold=0.85)
print(f"   ✓ 原始文档数: {len(documents)}")
print(f"   ✓ 去重后文档数: {len(unique_docs)}")
print(f"   ✓ 去除重复数: {len(documents) - len(unique_docs)}")

# 测试5: 统计信息
print("\n5. 测试统计信息...")
dedup2 = SmartDeduplicator()

for i in range(10):
    dedup2.deduplicate(f"doc{i}", f"标题{i}", f"内容{i}")

# 添加重复
dedup2.deduplicate("doc11", "标题0", "内容0")

stats = dedup2.get_stats()
print(f"   ✓ 总处理数: {stats['total_processed']}")
print(f"   ✓ 唯一文档数: {stats['unique_documents']}")
print(f"   ✓ 近似重复数: {stats['near_duplicates']}")

# 测试6: URL去重
print("\n6. 测试URL去重...")
dedup3 = SmartDeduplicator()

result1 = dedup3.deduplicate("doc1", "标题1", "内容1", "http://example.com/1")
print(f"   ✓ 文档1: 是否重复={result1.is_duplicate}")

result2 = dedup3.deduplicate("doc2", "标题2", "内容2", "http://example.com/1")
print(f"   ✓ 文档2（相同URL）: 是否重复={result2.is_duplicate}, 策略={result2.dedup_strategy}")

# 测试7: 增量去重
print("\n7. 测试增量去重（流式处理）...")
inc_dedup = IncrementalDeduplicator()

docs_to_add = [
    ("d1", "T1", "C1"),
    ("d2", "T2", "C2"),
    ("d1", "T1", "C1"),  # 重复
    ("d3", "T3", "C3"),
]

for doc_id, title, content in docs_to_add:
    result = inc_dedup.check_duplicate(doc_id, title, content)
    status = "重复" if result.is_duplicate else "唯一"
    print(f"   ✓ {doc_id}: {status}")

print("\n" + "=" * 80)
print("测试完成!")
print("=" * 80)

print("\n总结:")
print("✓ 精确去重: 内容哈希、标题哈希、URL哈希")
print("✓ 近似去重: SimHash算法、文本相似度")
print("✓ 增量去重: 支持流式处理")
print("✓ 批量去重: 支持批量处理")
print("✓ 统计信息: 完整的去重统计")
