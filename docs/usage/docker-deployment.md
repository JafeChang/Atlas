# Docker部署指南

> 🐳 一键启动完整的Atlas开发环境

## 概述

Atlas项目使用Docker Compose来简化多组件管理。通过容器化，可以快速启动所有必需的服务。

## 服务架构

### 基础设施服务
- **PostgreSQL 16** - 主数据库
- **Redis 7** - 任务队列 broker
- **MinIO** - 对象存储（S3兼容）

### 应用服务
- **Celery Worker** - 任务执行器
- **Celery Beat** - 定时任务调度器
- **Flower** - 任务监控界面

## 快速开始

### 方式1: 完整环境（推荐新手）

```bash
# 1. 复制环境配置
cp .env.local.example .env.local
# 根据需要修改配置（可选）

# 2. 启动所有服务
docker-compose up -d

# 3. 查看服务状态
docker-compose ps

# 4. 查看日志
docker-compose logs -f

# 5. 停止所有服务
docker-compose down
```

服务地址：
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001`
- Flower监控: `http://localhost:5555`

### 方式2: 开发环境（推荐开发者）

```bash
# 1. 仅启动基础设施服务
docker-compose -f docker-compose.dev.yml up -d

# 2. 手动启动Celery Worker（本地运行）
python3 scripts/celery_manager.py worker --loglevel=INFO

# 3. 启动Celery Beat（另一个终端）
python3 scripts/celery_manager.py beat --loglevel=INFO
```

这种方式的优势：
- 可以使用本地Python环境调试
- Worker代码修改后立即生效（无需重新构建镜像）
- 更灵活的开发体验

## 详细命令

### 启动服务

```bash
# 启动所有服务
docker-compose up -d

# 启动特定服务
docker-compose up -d postgres redis

# 重新构建并启动
docker-compose up -d --build

# 查看服务状态
docker-compose ps

# 查看资源使用
docker stats
```

### 日志管理

```bash
# 查看所有日志
docker-compose logs

# 查看特定服务日志
docker-compose logs -f worker
docker-compose logs -f beat
docker-compose logs -f flower

# 查看最近100行
docker-compose logs --tail=100 worker
```

### 停止服务

```bash
# 停止所有服务（保留数据）
docker-compose stop

# 停止并删除容器（保留数据卷）
docker-compose down

# 停止并删除容器和数据卷（⚠️ 数据会丢失）
docker-compose down -v

# 重启服务
docker-compose restart
docker-compose restart worker
```

### 服务管理

```bash
# 进入容器
docker-compose exec worker bash
docker-compose exec postgres psql -U atlas_user -d atlas_db

# 在容器中执行命令
docker-compose exec worker python scripts/check_redis_env.py

# 查看容器资源使用
docker-compose top
```

## 环境配置

### .env.local 配置文件

```bash
# 数据库配置
ATLAS_DATABASE_NAME=atlas_db
ATLAS_DATABASE_USER=atlas_user
ATLAS_DATABASE_PASSWORD=your_secure_password_here

# Redis配置（可选）
# ATLAS_REDIS_PASSWORD=your_redis_password

# MinIO配置（可选）
ATLAS_STORAGE_MINIO_ACCESS_KEY=minioadmin
ATLAS_STORAGE_MINIO_SECRET_KEY=minioadmin
```

### 端口映射

默认端口映射（可在.env.local中修改）：

| 服务 | 容器端口 | 主机端口 | 用途 |
|------|----------|----------|------|
| PostgreSQL | 5432 | 5432 | 数据库连接 |
| Redis | 6379 | 6379 | 任务队列 |
| MinIO API | 9000 | 9000 | 对象存储API |
| MinIO Console | 9001 | 9001 | Web管理界面 |
| Flower | 5555 | 5555 | 任务监控 |

## 数据持久化

Docker Compose使用命名卷来持久化数据：

```bash
# 查看数据卷
docker volume ls | grep atlas

# 备份数据卷
docker run --rm -v atlas_postgres_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/postgres_backup.tar.gz -C /data .

# 恢复数据卷
docker run --rm -v atlas_postgres_data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/postgres_backup.tar.gz -C /data
```

## 常见问题

### 问题1: 端口已被占用

```bash
# 检查端口占用
sudo netstat -tlnp | grep 5432
sudo netstat -tlnp | grep 6379

# 修改.env.local中的端口
ATLAS_DATABASE_PORT=5433
ATLAS_REDIS_PORT=6380
```

### 问题2: 容器启动失败

```bash
# 查看详细日志
docker-compose logs worker

# 检查容器状态
docker-compose ps

# 重新构建镜像
docker-compose build --no-cache worker
docker-compose up -d worker
```

### 问题3: Worker无法连接到数据库

```bash
# 检查网络
docker network ls
docker network inspect atlas-atlas-network

# 检查服务健康状态
docker-compose ps
docker-compose exec postgres pg_isready -U atlas_user
```

### 问题4: 数据丢失

```bash
# ⚠️ 永远不要使用 docker-compose down -v，除非你想清空所有数据

# 正确的停止方式
docker-compose stop  # 停止但保留数据
docker-compose down  # 删除容器但保留数据卷

# 备份前先停止
docker-compose stop
# 然后备份volume（见上文）
```

## 生产环境部署

### 使用Docker Compose（小型部署）

```bash
# 1. 修改.env.local为生产配置
# - 设置强密码
# - 启用Redis认证
# - 调整资源限制

# 2. 启动服务
docker-compose up -d

# 3. 配置反向代理（Nginx）
# 4. 配置SSL证书
# 5. 设置定期备份
```

### 使用Kubernetes（大规模部署）

参考：`docs/deployment/kubernetes/`

## 性能优化

### 资源限制

在`docker-compose.yml`中添加资源限制：

```yaml
services:
  worker:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
```

### 扩容Worker

```bash
# 启动多个Worker实例
docker-compose up -d --scale worker=4

# 查看Worker状态
docker-compose ps worker
```

## 监控和维护

### Flower监控界面

访问 `http://localhost:5555` 查看任务状态：
- 活动任务
- 任务执行时间
- Worker状态
- 任务成功率

### 日志管理

```bash
# 实时查看日志
docker-compose logs -f

# 日志轮转（在宿主机配置logrotate）
# /etc/logrotate.d/docker-compose
/home/user/atlas/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

### 健康检查

```bash
# 检查所有服务健康状态
docker-compose ps

# 手动健康检查
docker-compose exec postgres pg_isready -U atlas_user
docker-compose exec redis redis-cli ping
docker-compose exec worker celery -A atlas.scheduler.celery_app inspect ping
```

## 升级和更新

```bash
# 1. 拉取最新代码
git pull

# 2. 停止服务
docker-compose down

# 3. 重新构建镜像
docker-compose build

# 4. 启动服务
docker-compose up -d

# 5. 数据库迁移（如果有）
docker-compose exec worker python scripts/migrate_to_postgres.py --execute
```

## 故障排查

### 查看容器日志

```bash
# 所有容器日志
docker-compose logs

# 特定容器
docker-compose logs worker

# 实时跟踪
docker-compose logs -f --tail=100 worker
```

### 进入容器调试

```bash
# 进入Worker容器
docker-compose exec worker bash

# 手动执行命令
celery -A atlas.scheduler.celery_app inspect active
celery -A atlas.scheduler.celery_app inspect stats
```

### 网络问题

```bash
# 检查网络
docker network inspect atlas-atlas-network

# 测试连接
docker-compose exec worker ping postgres
docker-compose exec worker ping redis
```

## 参考资源

- [Docker Compose官方文档](https://docs.docker.com/compose/)
- [Celery Docker最佳实践](https://docs.celeryproject.org/en/stable/userguide/daemonizing.html#docker)
- [PostgreSQL Docker镜像](https://hub.docker.com/_/postgres)
- [Redis Docker镜像](https://hub.docker.com/_/redis)

---

*最后更新: 2025-12-28*
*维护者: Claude Sonnet*
