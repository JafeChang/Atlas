#!/usr/bin/env python3
"""
测试MinIO适配器
"""

import asyncio
import json
from uuid import uuid4
from datetime import datetime
from src.atlas.core.minio_adapter import create_minio_adapter


async def test_minio_adapter():
    """测试MinIO适配器功能"""
    print("🧪 测试MinIO适配器")
    print("=" * 50)

    try:
        # 创建适配器
        adapter = create_minio_adapter({
            'endpoint': 'localhost:9000',
            'access_key': 'minioadmin',
            'secret_key': 'minioadmin123456',
            'bucket_name': 'atlas-raw-data',
            'secure': False
        })
        print("✅ MinIO适配器创建成功")

        # 测试文档数据
        test_doc = {
            "id": str(uuid4()),
            "source_id": "test-source",
            "source_url": "https://example.com/test",
            "source_type": "RSS_FEED",
            "document_type": "RSS",
            "raw_content": "这是一个测试文档的内容",
            "raw_metadata": {"test": True},
            "collected_at": datetime.utcnow().isoformat(),
            "collector_version": "1.0.0",
            "processing_status": "COMPLETED",
            "content_hash": "test-hash-123",
            "title": "测试文档",
            "author": "测试作者",
            "language": "zh",
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        print(f"\n📝 测试文档ID: {test_doc['id']}")

        # 测试存储原始文档
        print("\n📤 测试存储原始文档...")
        object_path = await adapter.store_raw_document(test_doc)
        print(f"✅ 原始文档存储成功: {object_path}")

        # 测试检索原始文档
        print("\n📥 测试检索原始文档...")
        retrieved_doc = await adapter.retrieve_raw_document(test_doc['id'])
        if retrieved_doc and retrieved_doc['id'] == test_doc['id']:
            print("✅ 原始文档检索成功")
        else:
            print("❌ 原始文档检索失败")

        # 测试处理后文档
        processed_doc = {
            "id": str(uuid4()),
            "raw_document_id": test_doc['id'],
            "title": "处理后的测试文档",
            "summary": "这是文档摘要",
            "content": "这是处理后的文档内容",
            "structured_content": {"sections": ["section1", "section2"]},
            "extracted_metadata": {"processed": True},
            "entities": [{"name": "测试实体", "type": "TEST"}],
            "keywords": ["测试", "文档", "MinIO"],
            "categories": ["技术", "测试"],
            "processed_at": datetime.utcnow().isoformat(),
            "processor_version": "1.0.0",
            "processing_time_ms": 150,
            "content_hash": "processed-hash-456",
            "similarity_group_id": None,
            "similarity_score": 0.0,
            "is_duplicate": False,
            "quality_score": 0.9,
            "relevance_score": 0.8,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }

        print("\n📤 测试存储处理后文档...")
        processed_path = await adapter.store_processed_document(processed_doc)
        print(f"✅ 处理后文档存储成功: {processed_path}")

        # 测试检索处理后文档
        print("\n📥 测试检索处理后文档...")
        retrieved_processed = await adapter.retrieve_processed_document(processed_doc['id'])
        if retrieved_processed and retrieved_processed['id'] == processed_doc['id']:
            print("✅ 处理后文档检索成功")
        else:
            print("❌ 处理后文档检索失败")

        # 测试搜索功能
        print("\n🔍 测试搜索功能...")
        search_results = await adapter.search_documents("测试", "raw")
        print(f"✅ 搜索到 {len(search_results)} 个原始文档")

        # 测试列表功能
        print("\n📋 测试文档列表...")
        raw_docs = await adapter.list_documents("raw")
        processed_docs = await adapter.list_documents("processed")
        print(f"✅ 原始文档数量: {len(raw_docs)}")
        print(f"✅ 处理后文档数量: {len(processed_docs)}")

        # 测试统计信息
        print("\n📊 测试存储统计...")
        stats = await adapter.get_storage_stats()
        print(f"✅ 存储统计: {json.dumps(stats, indent=2, ensure_ascii=False)}")

        # 测试删除功能
        print("\n🗑️ 测试删除文档...")
        delete_success = await adapter.delete_document(test_doc['id'], "raw")
        if delete_success:
            print("✅ 原始文档删除成功")
        else:
            print("❌ 原始文档删除失败")

        delete_success = await adapter.delete_document(processed_doc['id'], "processed")
        if delete_success:
            print("✅ 处理后文档删除成功")
        else:
            print("❌ 处理后文档删除失败")

        print("\n🎉 MinIO适配器测试完成！所有功能正常")

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_minio_adapter())