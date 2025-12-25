# Atlas 脚本工具集

此目录包含Atlas系统的实用脚本工具。

## 可用脚本

### 1. migrate_to_minio.py - 数据迁移脚本

将文件系统存储的数据迁移到MinIO对象存储。

#### 功能特性

- ✅ 支持预演模式（dry-run）
- ✅ 支持指定数据源迁移
- ✅ 详细的进度显示和统计
- ✅ 完整的错误处理和日志
- ✅ 数据完整性验证

#### 使用方法

```bash
# 预演模式（不实际迁移，仅测试）
python scripts/migrate_to_minio.py --dry-run

# 完整迁移（会要求确认）
python scripts/migrate_to_minio.py

# 详细日志
python scripts/migrate_to_minio.py --verbose

# 仅迁移指定数据源
python scripts/migrate_to_minio.py --source google-ai-blog

# 批量大小调整
python scripts/migrate_to_minio.py --batch-size 20
```

#### 输出示例

```
============================================================
Atlas 数据迁移 - 文件系统 -> MinIO
============================================================
数据源目录: /path/to/data/raw
存储类型: minio
MinIO端点: localhost:9000
存储桶: atlas-raw-data

正在扫描文件...
发现数据源 'google-ai-blog' 的 1523 个文件
共发现 1523 个文件
总大小: 234.56 MB

开始迁移:
------------------------------------------------------------
[1/1523] google-ai-blog/0b78fc14-c812-4634-9a2b-fe5e0fde3048.json
[2/1523] google-ai-blog/1a2b3c4d-5e6f-7g8h-9i0j-k1l2m3n4o5p6.json
...

============================================================
迁移完成！
============================================================
总文件数: 1523
成功: 1520
失败: 3
跳过: 0
总大小: 234.56 MB
耗时: 45.23 秒
成功率: 99.8%
平均速度: 5.18 MB/s
```

#### 日志文件

迁移日志保存在: `logs/migration_YYYYMMDD_HHMMSS.log`

### 2. backup_storage.py - 数据备份脚本

备份文件系统存储的数据，支持完整备份和增量备份。

#### 功能特性

- ✅ 完整备份和增量备份
- ✅ gzip压缩支持
- ✅ 文件哈希校验
- ✅ 备份清单生成
- ✅ 详细的统计信息

#### 使用方法

```bash
# 完整备份
python scripts/backup_storage.py

# 增量备份（仅备份修改过的文件）
python scripts/backup_storage.py --incremental

# 启用压缩
python scripts/backup_storage.py --compress

# 指定备份目录
python scripts/backup_storage.py --output /path/to/backups

# 详细日志
python scripts/backup_storage.py --verbose

# 组合使用（增量备份+压缩）
python scripts/backup_storage.py --incremental --compress
```

#### 输出示例

```
============================================================
Atlas 数据备份
============================================================
源目录: /path/to/data/raw
目标目录: backups/atlas_backup_20251225_143022
增量模式: 否
压缩: 是

正在扫描文件...
共发现 1523 个文件
总大小: 234.56 MB

开始备份:
------------------------------------------------------------
[1/1523] google-ai-blog/0b78fc14-c812-4634-9a2b-fe5e0fde3048.json
...

============================================================
备份完成！
============================================================
总文件数: 1523
已备份: 1523
跳过: 0
原始大小: 234.56 MB
压缩后大小: 45.23 MB
压缩率: 80.7%
耗时: 12.34 秒
平均速度: 19.01 MB/s

备份位置: backups/atlas_backup_20251225_143022
备份清单: backups/atlas_backup_20251225_143022/manifest.json
```

#### 备份目录结构

```
backups/
└── atlas_backup_20251225_143022/
    ├── manifest.json              # 备份清单
    ├── hash_cache.json            # 哈希缓存（增量备份）
    ├── google-ai-blog/
    │   ├── file1.json.gz          # 压缩文件
    │   ├── file2.json.gz
    │   └── ...
    └── ...
```

#### 备份清单格式

`manifest.json` 包含备份的元数据：

```json
{
  "backup_time": "2025-12-25T14:30:22",
  "backup_type": "full",
  "compressed": true,
  "source_directory": "/path/to/data/raw",
  "backup_directory": "backups/atlas_backup_20251225_143022",
  "statistics": {
    "total_files": 1523,
    "backed_up": 1523,
    "total_size_bytes": 245789526
  },
  "files": [
    {
      "path": "google-ai-blog/file1.json",
      "size": 12345,
      "modified": "2025-12-25T14:20:15"
    }
  ]
}
```

## 推荐工作流程

### 迁移到MinIO的完整流程

#### 1. 备份现有数据

```bash
# 完整备份+压缩
python scripts/backup_storage.py --compress
```

#### 2. 测试迁移（预演模式）

```bash
# 预演模式，不实际迁移
python scripts/migrate_to_minio.py --dry-run --verbose
```

#### 3. 执行迁移

```bash
# 实际迁移
python scripts/migrate_to_minio.py
```

#### 4. 验证迁移结果

```bash
# 启动Web服务
python -m atlas.web.app

# 访问 http://localhost:5000
# 查看"存储管理"部分，确认MinIO统计信息
```

### 定期备份流程

#### 每日增量备份

```bash
# 创建cron任务或定时任务
python scripts/backup_storage.py --incremental --compress
```

#### 每周完整备份

```bash
python scripts/backup_storage.py --compress
```

## 故障排除

### 迁移失败

**问题**: 迁移过程中部分文件失败

**解决方案**:
1. 查看迁移日志: `logs/migration_*.log`
2. 检查失败文件列表
3. 重新迁移失败的文件:
   ```bash
   python scripts/migrate_to_minio.py --source failed-source-name
   ```

### 备份空间不足

**问题**: 备份目录磁盘空间不足

**解决方案**:
1. 使用压缩: `--compress` 参数
2. 清理旧备份
3. 使用增量备份: `--incremental` 参数

### MinIO连接失败

**问题**: 迁移时无法连接MinIO

**解决方案**:
1. 检查MinIO服务是否运行
2. 验证配置文件中的端点设置
3. 检查网络连接和防火墙

## 依赖要求

所有脚本依赖Atlas项目的Python环境和依赖包：

```bash
# 确保在项目根目录
cd /path/to/Atlas

# 安装依赖（如果尚未安装）
uv pip install -e .
```

## 安全建议

1. **备份数据**: 在执行迁移前务必备份数据
2. **测试先行**: 先使用 `--dry-run` 模式测试
3. **验证结果**: 迁移后验证数据完整性
4. **保护密钥**: MinIO密钥存储在环境变量中，不要提交到版本控制
5. **定期备份**: 建立定期备份机制

## 相关文档

- [MinIO存储使用指南](../docs/usage/minio-storage-guide.md)
- [TASK-001任务文档](../docs/roadmap/TASK-001-minio-integration.md)
- [配置指南](../docs/usage/configuration-guide.md)

## 支持

如有问题或建议，请：
1. 查看脚本日志文件
2. 查阅相关文档
3. 提交Issue到项目仓库
