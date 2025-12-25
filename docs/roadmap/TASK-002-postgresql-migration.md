# TASK-002: PostgreSQL数据库迁移

## 任务概述

**目标**: 从SQLite迁移到PostgreSQL，提升数据库性能和并发能力
**阶段**: Phase 1 - 核心基础设施
**优先级**: 🔴 高优先级 (存储能力升级)
**预计时间**: 3-4天
**开始日期**: 2025-12-25
**依赖**: TASK-001 (已完成)

## 为什么需要PostgreSQL

### SQLite的局限
- **并发写入**: SQLite在高并发写入时性能下降
- **查询性能**: 复杂查询性能不如PostgreSQL
- **数据类型**: 类型系统相对简单
- **扩展性**: 不支持水平扩展

### PostgreSQL的优势
- **并发性能**: MVCC机制，支持高并发读写
- **查询优化**: 强大的查询优化器和执行计划
- **数据类型**: 丰富的数据类型（JSONB, ARRAY等）
- **扩展性**: 支持分区表、复制、分片
- **成熟稳定**: 生产级数据库，久经考验

## 技术规格

### 数据库配置
- **版本**: PostgreSQL 15+
- **端口**: 5432
- **编码**: UTF-8
- **时区**: UTC
- **连接池**: psycopg3 + pool

### 性能目标
- **并发连接**: 100+
- **查询延迟**: < 50ms (P95)
- **写入TPS**: > 1000
- **数据量**: 支持百万级文档

## 实现步骤

### Step 1: 环境准备 (Day 1)
**目标**: 部署PostgreSQL服务

**具体任务**:
- [ ] 安装PostgreSQL服务器
- [ ] 配置数据库参数
- [ ] 创建Atlas数据库和用户
- [ ] 配置连接池
- [ ] 测试基本连接

**部署方案**:

```bash
# 方案1: Docker部署（推荐）
docker run -d \
  --name atlas-postgres \
  -e POSTGRES_USER=atlas \
  -e POSTGRES_PASSWORD=atlas123 \
  -e POSTGRES_DB=atlas \
  -p 5432:5432 \
  -v atlas_pgdata:/var/lib/postgresql/data \
  postgres:15-alpine

# 方案2: 本地安装
sudo apt install postgresql-15 postgresql-contrib-15
```

**配置参数**:
```ini
# postgresql.conf
shared_buffers = 256MB
effective_cache_size = 1GB
max_connections = 100
work_mem = 16MB
```

**验收标准**:
- PostgreSQL服务正常运行
- 可以创建表和插入数据
- 连接池配置正确
- 基础性能测试通过

### Step 2: 数据库Schema设计 (Day 2)
**目标**: 设计PostgreSQL表结构

**具体任务**:
- [ ] 分析现有SQLite表结构
- [ ] 设计PostgreSQL表结构
- [ ] 添加索引优化
- [ ] 创建外键约束
- [ ] 编写迁移脚本

**表结构设计**:

```sql
-- 数据源表
CREATE TABLE data_sources (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    url TEXT,
    enabled BOOLEAN DEFAULT TRUE,
    config JSONB,
    tags TEXT[],
    category VARCHAR(100),
    language VARCHAR(10) DEFAULT 'zh-CN',
    collection_interval INTEGER DEFAULT 3600,
    max_items_per_run INTEGER DEFAULT 100,
    retry_count INTEGER DEFAULT 3,
    timeout INTEGER DEFAULT 30,
    collection_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    last_collected_at TIMESTAMP,
    last_success_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT check_interval CHECK (collection_interval >= 60)
);

-- 创建索引
CREATE INDEX idx_data_sources_enabled ON data_sources(enabled);
CREATE INDEX idx_data_sources_type ON data_sources(source_type);
CREATE INDEX idx_data_sources_tags ON data_sources USING GIN(tags);

-- 原始文档表
CREATE TABLE raw_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id VARCHAR(255) NOT NULL,
    source_url TEXT,
    source_type VARCHAR(50),
    document_type VARCHAR(50),
    raw_content TEXT,
    raw_metadata JSONB,
    collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    collector_version VARCHAR(20),
    processing_status VARCHAR(20) DEFAULT 'pending',
    processing_error TEXT,
    processing_attempts INTEGER DEFAULT 0,
    content_hash VARCHAR(64),
    title TEXT,
    author VARCHAR(255),
    published_at TIMESTAMP,
    language VARCHAR(10),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_id) REFERENCES data_sources(name) ON DELETE CASCADE
);

-- 创建索引
CREATE INDEX idx_raw_documents_source_id ON raw_documents(source_id);
CREATE INDEX idx_raw_documents_collected_at ON raw_documents(collected_at DESC);
CREATE INDEX idx_raw_documents_status ON raw_documents(processing_status);
CREATE INDEX idx_raw_documents_hash ON raw_documents(content_hash);
CREATE INDEX idx_raw_documents_metadata ON raw_documents USING GIN(raw_metadata);

-- 处理后文档表
CREATE TABLE processed_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_document_id UUID NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    content TEXT,
    structured_content JSONB,
    extracted_metadata JSONB,
    entities JSONB,
    keywords TEXT[],
    categories TEXT[],
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processor_version VARCHAR(20),
    processing_time_ms INTEGER,
    content_hash VARCHAR(64),
    similarity_group_id UUID,
    similarity_score FLOAT,
    is_duplicate BOOLEAN DEFAULT FALSE,
    quality_score FLOAT,
    relevance_score FLOAT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (raw_document_id) REFERENCES raw_documents(id) ON DELETE CASCADE
);

-- 创建索引
CREATE INDEX idx_processed_documents_raw_id ON processed_documents(raw_document_id);
CREATE INDEX idx_processed_documents_keywords ON processed_documents USING GIN(keywords);
CREATE INDEX idx_processed_documents_categories ON processed_documents USING GIN(categories);
CREATE INDEX idx_processed_documents_similarity ON processed_documents(similarity_group_id);
CREATE INDEX idx_processed_documents_quality ON processed_documents(quality_score DESC);

-- 采集任务表
CREATE TABLE collection_tasks (
    id SERIAL PRIMARY KEY,
    source_id VARCHAR(255) NOT NULL,
    task_type VARCHAR(20) DEFAULT 'manual',
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    items_collected INTEGER DEFAULT 0,
    items_processed INTEGER DEFAULT 0,
    items_failed INTEGER DEFAULT 0,
    error_message TEXT,
    FOREIGN KEY (source_id) REFERENCES data_sources(name) ON DELETE CASCADE
);

-- 创建索引
CREATE INDEX idx_collection_tasks_source ON collection_tasks(source_id);
CREATE INDEX idx_collection_tasks_status ON collection_tasks(status);
CREATE INDEX idx_collection_tasks_created ON collection_tasks(created_at DESC);
```

**验收标准**:
- Schema设计完成
- 所有表和索引创建成功
- 外键约束正确
- 性能测试通过

### Step 3: 数据库适配层开发 (Day 2-3)
**目标**: 开发统一的数据库访问层

**具体任务**:
- [ ] 安装psycopg3和SQLAlchemy
- [ ] 创建数据库连接池管理
- [ ] 实现统一的ORM模型
- [ ] 支持SQLite和PostgreSQL双后端
- [ ] 添加数据库迁移工具

**技术方案**:

```python
# 使用SQLAlchemy 2.0 + 异步
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, DeclarativeBase

class Base(DeclarativeBase):
    pass

class DatabaseManager:
    """数据库管理器"""

    def __init__(self, db_url: str):
        self.engine = create_async_engine(
            db_url,
            echo=False,
            pool_size=10,
            max_overflow=20
        )
        self.async_session = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

    async def get_session(self) -> AsyncSession:
        """获取数据库会话"""
        return self.async_session()
```

**双后端支持**:

```python
# 配置文件
DATABASE_URL = "postgresql+asyncpg://atlas:atlas123@localhost/atlas"
# 或
DATABASE_URL = "sqlite+aiosqlite:///data/atlas.db"
```

**验收标准**:
- 数据库适配层开发完成
- 支持SQLite和PostgreSQL
- 异步操作正常
- 单元测试通过

### Step 4: 数据迁移 (Day 3-4)
**目标**: 将SQLite数据迁移到PostgreSQL

**具体任务**:
- [ ] 开发数据迁移脚本
- [ ] 备份SQLite数据
- [ ] 执行数据迁移
- [ ] 验证数据完整性
- [ ] 性能对比测试

**迁移策略**:

```python
# 迁移脚本
async def migrate_sqlite_to_pg():
    """迁移SQLite数据到PostgreSQL"""

    # 1. 读取SQLite数据
    sqlite_data = await read_sqlite_data()

    # 2. 批量写入PostgreSQL
    async with pg_session() as session:
        # 数据源
        for source in sqlite_data['sources']:
            pg_source = DataSource(**source)
            session.add(pg_source)

        # 文档（批量）
        for batch in chunks(sqlite_data['documents'], 1000):
            session.add_all([RawDocument(**doc) for doc in batch])
            await session.commit()

        await session.commit()
```

**验收标准**:
- 迁移脚本正常运行
- 数据100%迁移成功
- 数据完整性验证通过
- 性能提升明显

## 技术要求

### 性能要求
- **查询性能**: 复杂查询 < 50ms
- **写入性能**: > 1000 TPS
- **并发连接**: 支持100+并发
- **连接池**: 高效复用连接

### 可靠性要求
- **数据完整性**: 外键约束保证
- **事务支持**: ACID特性
- **错误恢复**: 自动重试机制
- **备份恢复**: 定期备份

### 兼容性要求
- **向后兼容**: 支持SQLite
- **平滑迁移**: 零停机迁移
- **双写支持**: 迁移期双写
- **回滚机制**: 支持回滚

## 风险和缓解措施

### 技术风险
**风险**: PostgreSQL部署失败
**缓解**:
- 提供Docker一键部署
- 详细的安装文档
- 本地开发环境配置

**风险**: 数据迁移丢失
**缓解**:
- 完整备份SQLite数据
- 分批迁移和验证
- 保留SQLite作为备份

**风险**: 性能不达标
**缓解**:
- 提前性能测试
- 索引优化
- 查询优化

### 项目风险
**风险**: 影响现有功能
**缓解**:
- 保持SQLite支持
- A/B测试
- 灰度发布

**风险**: 学习曲线
**缓解**:
- 详细文档
- 最佳实践指南
- 示例代码

## 测试计划

### 单元测试
- [ ] 数据库模型测试
- [ ] ORM操作测试
- [ ] 连接池测试
- [ ] 事务处理测试

### 集成测试
- [ ] CRUD操作测试
- [ ] 复杂查询测试
- [ ] 并发操作测试
- [ ] 数据迁移测试

### 性能测试
- [ ] 查询性能基准
- [ ] 写入性能基准
- [ ] 并发性能测试
- [ ] 与SQLite对比

## 交付物

### 代码交付
- [ ] 数据库Schema定义
- [ ] SQLAlchemy模型
- [ ] 数据库管理器
- [ ] 数据迁移脚本
- [ ] 配置文件模板

### 文档交付
- [ ] PostgreSQL部署指南
- [ ] Schema设计文档
- [ ] 数据迁移操作手册
- [ ] 性能优化指南
- [ ] 故障排除指南

## 成功标准

### 功能标准
- [ ] PostgreSQL数据库正常运行
- [ ] 数据迁移100%成功
- [ ] 所有功能正常工作
- [ ] SQLite后端仍可使用

### 性能标准
- [ ] 查询性能提升>50%
- [ ] 并发性能提升>100%
- [ ] 响应时间<50ms (P95)
- [ ] 写入TPS>1000

### 质量标准
- [ ] 代码质量达标
- [ ] 测试覆盖率>80%
- [ ] 文档完整准确
- [ ] 错误处理完善

## 后续任务

**依赖TASK-002**:
- TASK-003: Celery任务队列系统
- TASK-008: OpenSearch搜索集成

**为后续准备**:
- PostgreSQL连接池优化
- 查询性能基准数据
- 数据库监控指标

## 状态跟踪

**当前状态**: 🟡 Planning
**下一步**: 开始Step 1环境准备
**阻塞问题**: 无

---

*此任务完成后，系统将从SQLite升级到PostgreSQL，大幅提升数据库性能和并发能力。*
