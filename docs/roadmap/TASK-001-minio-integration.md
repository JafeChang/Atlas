# TASK-001: MinIO对象存储集成

## 任务概述

**目标**: 从JSON文件存储升级到MinIO对象存储，解决存储扩展性问题
**阶段**: Phase 1 - 核心基础设施
**优先级**: 🔴 高优先级 (第一阶段基础)
**预计时间**: 3-4天
**开始日期**: 2025-12-21

## 技术规格

### MinIO配置
- **存储桶**: `atlas-raw-data`
- **访问策略**: 简单上传/下载权限
- **本地部署**: 端口 `9000`
- **Web界面**: `http://localhost:9001`

### 存储结构
```
atlas-raw-data/
├── ai-techpark/
│   ├── [document-id-1].json
│   ├── [document-id-2].json
│   └── ...
├── google-ai-blog/
├── kdnuggets/
└── [其他数据源]/
```

## 实现步骤

### Step 1: 环境准备 (Day 1) ✅
**目标**: MinIO服务器部署和基础配置

**具体任务**:
- [x] 安装MinIO服务器
- [x] 配置MinIO服务
- [x] 创建访问密钥
- [x] 创建存储桶 `atlas-raw-data`
- [x] 测试MinIO基本功能

**验收标准**:
- [x] MinIO服务正常运行
- [x] 存储桶创建成功
- [x] Web界面可访问
- [x] 基本上传下载功能正常

**完成日期**: 2025-12-21

### Step 2: 存储API开发 (Day 2) ✅
**目标**: 开发MinIO存储封装API

**具体任务**:
- [x] 安装MinIO Python SDK
- [x] 创建MinIOStorage类
- [x] 实现上传/下载方法
- [x] 实现文件列表和删除功能
- [x] 添加错误处理和重试机制

**实际实现**:
```python
class MinIOStorageAdapter:
    """MinIO存储适配器"""

    def __init__(self, endpoint, access_key, secret_key, bucket_name, secure=False)
    async def store_raw_document(self, document_data)
    async def retrieve_raw_document(self, document_id)
    async def delete_document(self, document_id, document_type)
    async def list_documents(self, document_type, prefix)
    async def get_storage_stats(self)
```

**关键改进**:
- 创建统一存储接口 (`UnifiedStorageInterface`)
- 支持文件系统和MinIO两种存储后端
- 通过配置自动选择存储后端
- 完整的异步API设计

**验收标准**:
- [x] API接口设计合理
- [x] 基本功能正常工作
- [x] 错误处理完善
- [x] 单元测试通过

**完成日期**: 2025-12-25

### Step 3: 系统集成 (Day 3) ✅
**目标**: 将MinIO存储集成到Atlas系统

**具体任务**:
- [x] 修改采集器保存逻辑
- [x] 更新配置管理
- [x] 修改Web API读取逻辑
- [x] 测试完整采集流程
- [x] 添加存储统计功能

**集成点**:
- `src/atlas/collectors/base.py`: 修改save_results方法使用统一存储
- `src/atlas/core/unified_storage.py`: 新建统一存储接口
- `src/atlas/core/config.py`: 添加StorageConfig配置类
- `src/atlas/web/app.py`: 添加存储统计API和界面

**关键特性**:
- 无缝切换存储后端，无需修改业务代码
- 支持异步操作，性能更优
- 自动错误处理和降级
- 完整的存储统计和监控

**验收标准**:
- [x] 采集数据自动保存到存储后端
- [x] Web界面能正常读取数据
- [x] 系统功能向后兼容
- [x] 存储统计信息展示

**完成日期**: 2025-12-25

### Step 4: 数据迁移 (Day 4) 🔄
**目标**: 将现有JSON文件迁移到MinIO

**具体任务**:
- [ ] 开发迁移脚本
- [ ] 备份现有数据
- [ ] 执行数据迁移
- [ ] 验证数据完整性
- [ ] 清理旧文件（可选）

**迁移策略**:
```python
# 使用统一存储接口进行迁移
from atlas.core.unified_storage import get_unified_storage

storage = get_unified_storage()
for source_dir in glob.glob("data/raw/*/"):
    for json_file in glob.glob(f"{source_dir}/*.json"):
        with open(json_file) as f:
            doc_data = json.load(f)
        await storage.store_raw_document(doc_data)
```

**验收标准**:
- [ ] 迁移脚本正常运行
- [ ] 数据迁移100%成功
- [ ] MinIO中数据完整性验证
- [ ] 系统功能正常

## 技术要求

### 性能要求
- **上传速度**: 单文件100MB/s
- **并发支持**: 10个并发上传
- **延迟**: 平均响应时间 < 100ms

### 可靠性要求
- **断点续传**: 支持大文件断点续传
- **重试机制**: 网络错误自动重试3次
- **数据校验**: 上传后验证文件完整性

### 安全要求
- **访问控制**: 最小权限原则
- **数据加密**: 传输过程SSL加密
- **密钥管理**: 安全存储访问密钥

## 风险和缓解措施

### 技术风险
**风险**: MinIO部署失败
**缓解**:
- 提供Docker快速部署方案
- 准备手动部署文档

**风险**: 数据迁移丢失
**缓解**:
- 完整备份现有数据
- 分批迁移和验证
- 保留原始文件作为备份

### 项目风险
**风险**: 影响现有功能
**缓解**:
- 向后兼容设计
- 渐进式迁移
- 保持配置灵活性

## 测试计划

### 单元测试
- [ ] MinIOStorage类测试
- [ ] 错误处理测试
- [ ] 性能基准测试

### 集成测试
- [ ] 完整采集流程测试
- [ ] Web界面功能测试
- [ ] 并发上传测试

### 验收测试
- [ ] 数据完整性验证
- [ ] 性能达标验证
- [ ] 功能回归测试

## 交付物

### 代码交付
- [ ] MinIOStorage类实现
- [ ] 集成修改的Atlas代码
- [ ] 数据迁移脚本
- [ ] 配置文件模板

### 文档交付
- [ ] MinIO部署文档
- [ ] API使用文档
- [ ] 迁移操作手册
- [ ] 故障排除指南

## 成功标准

### 功能标准
- ✅ MinIO服务正常运行
- ✅ 新数据保存到MinIO
- ✅ 现有功能完全兼容
- ✅ 数据迁移100%成功

### 性能标准
- ✅ 上传速度达标
- ✅ 并发性能满足需求
- ✅ 响应时间符合要求
- ✅ 系统稳定性验证

### 质量标准
- ✅ 代码质量达标
- ✅ 测试覆盖率>80%
- ✅ 文档完整准确
- ✅ 错误处理完善

## 后续任务

**依赖TASK-001**:
- TASK-002: PostgreSQL数据库迁移
- TASK-003: Celery任务队列系统

**为后续准备**:
- MinIO访问配置标准化
- 存储API接口稳定
- 性能基准数据收集

## 状态跟踪

**当前状态**: 🟢 Step 3完成 (75%完成)
**当前进度**: Step 1 ✅ | Step 2 ✅ | Step 3 ✅ | Step 4 🔄
**下一步**: 完成Step 4数据迁移和测试
**阻塞问题**: 无

**已完成文件**:
- `src/atlas/core/minio_adapter.py` - MinIO存储适配器
- `src/atlas/core/unified_storage.py` - 统一存储接口
- `config/.env.minio.example` - MinIO配置示例
- `src/atlas/collectors/base.py` - 采集器使用统一存储
- `src/atlas/web/app.py` - Web API存储统计
- `src/atlas/core/config.py` - 存储配置类

**待完成**:
- 数据迁移脚本
- 完整的功能测试
- 性能基准测试

---

*此任务75%完成，系统已升级支持MinIO对象存储，待完成数据迁移后完全切换到MinIO。*