#!/bin/bash
# MinIO存储快速验证脚本

echo "======================================"
echo "Atlas MinIO 存储验证"
echo "======================================"
echo ""

# 1. 检查MinIO服务状态
echo "1. MinIO服务状态"
if curl -s http://localhost:9000/health/live > /dev/null 2>&1; then
    echo "   ✅ MinIO服务运行正常"
else
    echo "   ❌ MinIO服务未运行"
    echo "   启动命令: uv run python scripts/minio_server.py > /tmp/minio_server.log 2>&1 &"
    exit 1
fi

# 2. 检查存储桶
echo ""
echo "2. 存储桶列表"
BUCKETS=$(curl -s http://localhost:9000/ | python3 -c "import sys, json; print(', '.join(json.load(sys.stdin).get('buckets', [])))" 2>/dev/null)
echo "   存储桶: $BUCKETS"

# 3. 统计对象数量
echo ""
echo "3. 对象统计"
TOTAL_OBJECTS=$(curl -s http://localhost:9000/atlas-documents/ | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('objects', [])))" 2>/dev/null)
RAW_DOCS=$(curl -s http://localhost:9000/atlas-documents/ | python3 -c "import sys, json; objs=json.load(sys.stdin).get('objects', []); raw=[o for o in objs if o.startswith('raw/')]; print(len(raw))" 2>/dev/null)
echo "   总对象数: $TOTAL_OBJECTS"
echo "   Raw文档: $RAW_DOCS"

# 4. 磁盘使用
echo ""
echo "4. 磁盘使用"
if [ -d /tmp/minio-data ]; then
    SIZE=$(du -sh /tmp/minio-data | cut -f1)
    echo "   数据目录: /tmp/minio-data"
    echo "   占用空间: $SIZE"
else
    echo "   ⚠️  数据目录不存在"
fi

# 5. 随机验证一个文档
echo ""
echo "5. 数据完整性验证"
RANDOM_DOC=$(curl -s http://localhost:9000/atlas-documents/ | python3 -c "import sys, json; objs=[o for o in json.load(sys.stdin).get('objects', []) if o.startswith('raw/')]; print(objs[0] if objs else '')" 2>/dev/null)
if [ -n "$RANDOM_DOC" ]; then
    DOC_DATA=$(curl -s "http://localhost:9000/atlas-documents/$RANDOM_DOC" 2>/dev/null)
    if [ $? -eq 0 ]; then
        DOC_ID=$(echo "$DOC_DATA" | python3 -c "import sys, json; print(json.load(sys.stdin).get('id', 'N/A'))" 2>/dev/null)
        echo "   ✅ 随机文档验证成功"
        echo "   文档路径: $RANDOM_DOC"
        echo "   文档ID: $DOC_ID"
    else
        echo "   ❌ 文档读取失败"
    fi
else
    echo "   ⚠️  没有找到文档"
fi

echo ""
echo "======================================"
echo "验证完成"
echo "======================================"
