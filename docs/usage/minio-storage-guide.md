# MinIO对象存储使用指南

## 概述

Atlas系统支持两种存储后端：
- **文件系统存储** (默认): 将数据存储在本地文件系统
- **MinIO对象存储**: 将数据存储在MinIO对象存储服务

统一存储接口允许无缝切换存储后端，无需修改业务代码。

## 快速开始

### 1. 使用文件系统存储（默认）

无需额外配置，系统默认使用文件系统存储：

```bash
# 运行采集器
python -m atlas.cli collect

# 启动Web服务
python -m atlas.web.app
```

数据将存储在 `data/raw/` 目录下。

### 2. 使用MinIO存储

#### 步骤1: 启动MinIO服务

使用Docker快速启动MinIO：

```bash
docker run -d \
  -p 9000:9000 \
  -p 9001:9001 \
  --name atlas-minio \
  -e "MINIO_ROOT_USER=minioadmin" \
  -e "MINIO_ROOT_PASSWORD=minioadmin123456" \
  minio/minio server /data --console-address ":9001"
```

访问MinIO控制台: http://localhost:9001
- 用户名: `minioadmin`
- 密码: `minioadmin123456`

#### 步骤2: 创建存储桶

在MinIO控制台中创建存储桶 `atlas-raw-data`，或使用mc命令行工具：

```bash
# 配置mc别名
mc alias set local http://localhost:9000 minioadmin minioadmin123456

# 创建存储桶
mc mb local/atlas-raw-data

# 设置公共访问策略（可选）
mc anonymous set download local/atlas-raw-data
```

#### 步骤3: 配置Atlas使用MinIO

方法1: 使用环境变量

```bash
export ATLAS_STORAGE_TYPE=minio
export ATLAS_STORAGE_MINIO_ENDPOINT=localhost:9000
export ATLAS_STORAGE_MINIO_ACCESS_KEY=minioadmin
export ATLAS_STORAGE_MINIO_SECRET_KEY=minioadmin123456
export ATLAS_STORAGE_MINIO_BUCKET_NAME=atlas-raw-data
export ATLAS_STORAGE_MINIO_SECURE=false
```

方法2: 使用配置文件

复制配置示例：

```bash
cp config/.env.minio.example config/.env.minio
```

编辑 `config/.env.minio` 文件，修改MinIO配置。

然后设置环境：

```bash
export ATLAS_ENV=minio
```

方法3: 在Python代码中配置

```python
from atlas.core.config import get_config
from atlas.core.unified_storage import get_unified_storage

# 配置使用MinIO
os.environ['ATLAS_STORAGE_TYPE'] = 'minio'
os.environ['ATLAS_STORAGE_MINIO_ENDPOINT'] = 'localhost:9000'
os.environ['ATLAS_STORAGE_MINIO_ACCESS_KEY'] = 'minioadmin'
os.environ['ATLAS_STORAGE_MINIO_SECRET_KEY'] = 'minioadmin123456'
os.environ['ATLAS_STORAGE_MINIO_BUCKET_NAME'] = 'atlas-raw-data'

# 获取统一存储接口
storage = get_unified_storage()
print(f"存储类型: {storage.get_storage_type()}")
```

#### 步骤4: 验证配置

运行系统并检查存储类型：

```bash
# 启动Web服务
python -m atlas.web.app
```

访问 http://localhost:5000，在"存储管理"部分查看：
- 存储类型应显示为"MinIO对象存储"
- 存储桶名称应显示为 `atlas-raw-data`

## 配置参考

### 环境变量

| 变量名 | 描述 | 默认值 |
|--------|------|--------|
| `ATLAS_STORAGE_TYPE` | 存储类型 (`filesystem` 或 `minio`) | `filesystem` |
| `ATLAS_STORAGE_MINIO_ENDPOINT` | MinIO服务端点 | `localhost:9000` |
| `ATLAS_STORAGE_MINIO_ACCESS_KEY` | MinIO访问密钥 | `minioadmin` |
| `ATLAS_STORAGE_MINIO_SECRET_KEY` | MinIO密钥 | `minioadmin123456` |
| `ATLAS_STORAGE_MINIO_BUCKET_NAME` | 存储桶名称 | `atlas-raw-data` |
| `ATLAS_STORAGE_MINIO_SECURE` | 是否使用HTTPS | `false` |
| `ATLAS_STORAGE_MINIO_PREFIX` | 对象前缀 | `atlas-documents` |

### 文件系统存储配置

| 变量名 | 描述 | 默认值 |
|--------|------|--------|
| `ATLAS_STORAGE_FILESYSTEM_BASE_DIR` | 基础目录 | `data/raw` |
| `ATLAS_STORAGE_FILESYSTEM_COMPRESSION` | 是否压缩 | `true` |

## 使用示例

### 在采集器中使用

采集器自动使用统一存储接口，无需修改代码：

```python
from atlas.collectors import CollectorFactory
from atlas.core.config import CollectionConfig

# 创建采集器
factory = CollectorFactory()
config = CollectionConfig()
collector = factory.create_collector('rss', config)

# 执行采集
result = collector.collect(source_config)

# 保存结果 - 自动使用配置的存储后端
collector.save_results(result, "data/raw/source_name", "source_name")
```

### 在Python代码中使用

```python
import asyncio
from atlas.core.unified_storage import get_unified_storage

async def main():
    # 获取统一存储接口
    storage = get_unified_storage()

    # 存储文档
    doc_data = {
        "id": "doc-123",
        "source_id": "test-source",
        "raw_content": "测试内容",
        "title": "测试文档"
    }
    path = await storage.store_raw_document(doc_data)
    print(f"文档已存储到: {path}")

    # 检索文档
    doc = await storage.retrieve_raw_document("doc-123")
    print(f"检索到文档: {doc}")

    # 获取存储统计
    stats = await storage.get_storage_stats()
    print(f"存储统计: {stats}")

# 运行异步代码
asyncio.run(main())
```

### 通过Web API使用

#### 获取存储统计

```bash
curl http://localhost:5000/api/storage/stats
```

响应示例：

```json
{
  "status": "success",
  "storage_type": "minio",
  "storage_info": {
    "storage_type": "minio",
    "endpoint": "localhost:9000",
    "bucket_name": "atlas-raw-data",
    "prefix": "atlas-documents",
    "secure": false
  },
  "storage_stats": {
    "total_objects": 1523,
    "total_size_bytes": 52428800,
    "total_size_mb": 50.0
  }
}
```

## 存储切换

### 从文件系统切换到MinIO

1. 启动MinIO服务并创建存储桶
2. 配置环境变量或配置文件
3. （可选）迁移现有数据
4. 重启Atlas服务

新数据将自动保存到MinIO，无需修改代码。

### 从MinIO切换到文件系统

1. 修改配置将 `ATLAS_STORAGE_TYPE` 设置为 `filesystem`
2. 重启Atlas服务

新数据将保存到本地文件系统。

## 性能优化

### MinIO性能调优

1. **启用压缩**: 在客户端启用数据压缩
   ```python
   # MinIO适配器自动处理压缩
   ```

2. **并发上传**: 调整并发参数
   ```python
   # 在采集器配置中设置
   config.max_concurrent_requests = 5
   ```

3. **连接池**: MinIO Python SDK自动管理连接池

### 文件系统性能调优

1. **启用压缩**: 减少磁盘占用
   ```bash
   export ATLAS_STORAGE_FILESYSTEM_COMPRESSION=true
   ```

2. **SSD存储**: 使用SSD提升I/O性能

3. **目录结构**: 系统自动分片存储，避免单目录文件过多

## 故障排除

### MinIO连接失败

**问题**: 无法连接到MinIO服务

**解决方案**:
1. 检查MinIO服务是否运行
   ```bash
   docker ps | grep atlas-minio
   ```

2. 检查端点配置
   ```bash
   curl http://localhost:9000/minio/health/live
   ```

3. 检查防火墙设置

### 存储桶不存在

**问题**: 上传失败，存储桶不存在

**解决方案**:
1. 在MinIO控制台创建存储桶
2. 或使用mc命令行工具创建
   ```bash
   mc mb local/atlas-raw-data
   ```

### 权限错误

**问题**: 访问密钥权限不足

**解决方案**:
1. 检查访问密钥是否正确
2. 在MinIO控制台中为用户分配适当权限
3. 确保存储桶策略允许读写操作

## 最佳实践

### 生产环境配置

1. **使用HTTPS**: 设置 `ATLAS_STORAGE_MINIO_SECURE=true`
2. **强密码**: 使用强密码作为访问密钥
3. **独立存储桶**: 为不同环境使用不同的存储桶
4. **备份策略**: 定期备份MinIO数据
5. **监控告警**: 配置MinIO监控和告警

### 安全建议

1. **密钥管理**: 使用环境变量或密钥管理服务存储密钥
2. **网络隔离**: MinIO服务不应直接暴露到公网
3. **访问控制**: 使用MinIO策略限制访问权限
4. **数据加密**: 启用传输加密和静态加密

## 进阶主题

### 多环境配置

为不同环境创建不同的配置文件：

```
config/
├── .env.development      # 开发环境
├── .env.staging          # 测试环境
├── .env.production       # 生产环境
└── .env.minio.example    # MinIO配置示例
```

切换环境：

```bash
export ATLAS_ENV=production
```

### 自定义存储后端

实现自定义存储适配器：

```python
from atlas.core.unified_storage import UnifiedStorageInterface

class CustomStorageAdapter:
    async def store_raw_document(self, document_data):
        # 自定义存储逻辑
        pass

    async def retrieve_raw_document(self, document_id):
        # 自定义检索逻辑
        pass

    # 实现其他必需方法...
```

### 数据迁移

从文件系统迁移到MinIO：

```python
import asyncio
import json
from pathlib import Path
from atlas.core.unified_storage import get_unified_storage

async def migrate_to_minio():
    storage = get_unified_storage()

    # 遍历所有JSON文件
    for json_file in Path("data/raw").rglob("*.json"):
        with open(json_file) as f:
            doc_data = json.load(f)

        # 上传到MinIO
        await storage.store_raw_document(doc_data)
        print(f"已迁移: {json_file}")

asyncio.run(migrate_to_minio())
```

## 相关文档

- [MinIO官方文档](https://min.io/docs/minio/linux/index.html)
- [MinIO Python SDK](https://min.io/docs/minio/linux/developers/python/minio-py.html)
- [Atlas架构文档](../tech/tech-architecture.md)
- [配置指南](configuration-guide.md)

## 支持

如有问题，请：
1. 查看故障排除部分
2. 检查日志文件: `logs/atlas.log`
3. 提交Issue到项目仓库
