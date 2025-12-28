# 环境配置指南

## 概述

Atlas项目使用环境变量进行配置管理。敏感信息（如数据库密码）通过`.env.local`文件管理，不会被提交到Git仓库。

## 配置文件说明

### .env.example
公开的配置模板，包含所有可配置的环境变量和默认值。此文件会提交到Git。

### .env.local.example
本地开发环境配置模板，包含敏感信息的占位符。此文件会提交到Git。

### .env.local
**实际配置文件，包含敏感信息，不会被Git提交。**

## 快速开始

### 1. 创建本地配置文件

```bash
# 复制模板文件
cp .env.local.example .env.local

# 编辑配置文件，填入实际值
nano .env.local  # 或使用你喜欢的编辑器
```

### 2. 配置PostgreSQL数据库

编辑`.env.local`文件，设置以下变量：

```bash
# 数据库类型
ATLAS_DATABASE_TYPE=postgresql

# PostgreSQL连接信息
ATLAS_DATABASE_HOST=localhost
ATLAS_DATABASE_PORT=5432
ATLAS_DATABASE_USER=atlas_user
ATLAS_DATABASE_PASSWORD=your_secure_password_here
ATLAS_DATABASE_NAME=atlas_db

# 连接池配置（可选）
ATLAS_DATABASE_POOL_SIZE=10
ATLAS_DATABASE_MAX_OVERFLOW=20
```

### 3. 初始化PostgreSQL数据库

首次使用需要创建数据库和用户：

```bash
# 方式1: 使用初始化脚本（推荐）
sudo -u postgres bash scripts/init_postgres_db.sh

# 方式2: 手动创建
sudo -u postgres psql
```

在psql中执行：
```sql
CREATE DATABASE atlas_db;
CREATE USER atlas_user WITH ENCRYPTED PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE atlas_db TO atlas_user;
\c atlas_db
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
\q
```

### 4. 验证配置

```bash
# 运行环境检查脚本
python3 scripts/check_postgres_env.py
```

### 5. 加载环境变量

在运行应用前，确保环境变量已加载：

```bash
# 方式1: 手动加载（推荐用于测试）
export $(cat .env.local | xargs)

# 方式2: 使用direnv（自动加载）
# 在~/.bashrc或~/.zshrc中添加：
eval "$(direnv hook bash)"
echo 'dotenv .env.local' > .envrc

# 方式3: 在Python中加载
# 应用会自动从.env.local加载
```

### 6. 验证Redis配置

```bash
# 运行Redis环境检查脚本
python3 scripts/check_redis_env.py

# 使用redis-cli测试
redis-cli ping

# 使用Python测试
python3 -c "from redis import Redis; r = Redis(); print(r.ping())"
```

### 7. 启动Celery

```bash
# 启动Celery Worker
celery -A atlas.scheduler.celery_app worker --loglevel=info

# 启动Celery Beat (定时任务调度器)
celery -A atlas.scheduler.celery_app beat --loglevel=info

# 启动Flower (监控界面)
celery -A atlas.scheduler.celery_app flower --port=5555
```

## 环境变量完整列表

### 数据库配置

| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `ATLAS_DATABASE_TYPE` | 数据库类型 (sqlite/postgresql) | sqlite | 否 |
| `ATLAS_DATABASE_HOST` | 数据库主机 | localhost | 否 |
| `ATLAS_DATABASE_PORT` | 数据库端口 | 5432 | 否 |
| `ATLAS_DATABASE_USER` | 数据库用户 | atlas | 否 |
| `ATLAS_DATABASE_PASSWORD` | 数据库密码 | atlas | 否 |
| `ATLAS_DATABASE_NAME` | 数据库名称 | atlas | 否 |
| `ATLAS_DATABASE_PATH` | SQLite文件路径 | data/atlas.db | 否 |

### 连接池配置

| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `ATLAS_DATABASE_POOL_SIZE` | 连接池大小 | 10 | 否 |
| `ATLAS_DATABASE_MAX_OVERFLOW` | 最大溢出连接数 | 20 | 否 |
| `ATLAS_DATABASE_POOL_TIMEOUT` | 连接超时(秒) | 30 | 否 |
| `ATLAS_DATABASE_POOL_RECYCLE` | 连接回收时间(秒) | 3600 | 否 |

### 存储配置

| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `ATLAS_STORAGE_TYPE` | 存储类型 (local/minio) | local | 否 |
| `ATLAS_STORAGE_MINIO_ENDPOINT` | MinIO端点 | localhost:9000 | 否 |
| `ATLAS_STORAGE_MINIO_ACCESS_KEY` | MinIO访问密钥 | - | 是* |
| `ATLAS_STORAGE_MINIO_SECRET_KEY` | MinIO秘密密钥 | - | 是* |

*仅在使用MinIO时必填

### Redis配置 (TASK-003)

| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `ATLAS_REDIS_HOST` | Redis主机 | localhost | 否 |
| `ATLAS_REDIS_PORT` | Redis端口 | 6379 | 否 |
| `ATLAS_REDIS_DB` | Redis数据库编号 | 0 | 否 |
| `ATLAS_REDIS_PASSWORD` | Redis密码 | - | 否* |
| `ATLAS_REDIS_MAX_CONNECTIONS` | 最大连接数 | 10 | 否 |

*生产环境推荐设置密码

### Celery配置 (TASK-003)

| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `ATLAS_CELERY_BROKER_URL` | Celery代理URL | redis://localhost:6379/0 | 否 |
| `ATLAS_CELERY_RESULT_BACKEND` | Celery结果后端 | redis://localhost:6379/1 | 否 |
| `ATLAS_CELERY_TASK_TRACK_STARTED` | 追踪任务开始状态 | true | 否 |
| `ATLAS_CELERY_TASK_TIME_LIMIT` | 任务硬时间限制(秒) | 3600 | 否 |
| `ATLAS_CELERY_TASK_SOFT_TIME_LIMIT` | 任务软时间限制(秒) | 3000 | 否 |
| `ATLAS_CELERY_WORKER_PREFETCH_MULTIPLIER` | Worker预取倍数 | 4 | 否 |
| `ATLAS_CELERY_WORKER_MAX_TASKS_PER_CHILD` | Worker最大任务数 | 1000 | 否 |

## 安全建议

1. **永远不要提交.env.local到Git**
   - .gitignore已配置忽略此文件

2. **使用强密码**
   ```bash
   # 生成安全密码
   python3 -c "import secrets; print(secrets.token_urlsafe(16))"
   ```

3. **区分开发和生产环境**
   - 开发: `.env.local`
   - 生产: `/etc/atlas/.env.production` (需要root权限)

4. **定期轮换密钥**
   - 每90天更换一次数据库密码
   - 每次更换后更新.env.local

5. **文件权限**
   ```bash
   # 设置.env.local为仅用户可读写
   chmod 600 .env.local
   ```

## 故障排查

### 问题1: 环境变量未生效

```bash
# 检查环境变量是否设置
echo $ATLAS_DATABASE_USER

# 手动加载环境变量
set -a  # 自动导出所有变量
source .env.local
set +a
```

### 问题2: 数据库连接失败

```bash
# 测试PostgreSQL连接
PGPASSWORD=$ATLAS_DATABASE_PASSWORD psql \
  -h $ATLAS_DATABASE_HOST \
  -U $ATLAS_DATABASE_USER \
  -d $ATLAS_DATABASE_NAME \
  -c "SELECT version();"
```

### 问题3: 文件权限错误

```bash
# 修复.env.local权限
chmod 600 .env.local
chown $USER:$USER .env.local
```

## 相关文档

- [PostgreSQL安装指南](../guidelines/postgresql-setup-guide.md)
- [数据库迁移](../testing/TASK-002-postgresql-migration-report.md)
- [配置文件规范](../documentation-system.md)
