# Redis 安装和配置指南

> ⚠️ **安全提示**：本文档中的示例密码（如`redis_password`）仅用于演示。在生产环境中，请使用强密码并通过`.env.local`文件管理。默认情况下，Redis不需要密码，但建议在生产环境中启用认证。

## 环境要求

- Ubuntu/Debian Linux (WSL)
- Redis 7.0+ (推荐)
- Python 3.13+

## 快速安装 (Ubuntu/Debian)

### 方式1: apt安装 (推荐)

```bash
# 更新包列表
sudo apt-get update

# 安装Redis服务器
sudo apt-get install -y redis-server

# 启动Redis服务
sudo service redis-server start

# 验证安装
redis-cli --version
redis-cli ping  # 应返回 PONG
```

### 方式2: Docker安装

```bash
# 安装Docker (如果未安装)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 启动Redis容器
docker run -d \
  --name atlas-redis \
  -p 6379:6379 \
  -v redis-data:/data \
  redis:7-alpine

# 验证运行
docker ps | grep atlas-redis
docker exec -it atlas-redis redis-cli ping
```

### 方式3: 从源码安装 (最新版本)

```bash
# 安装依赖
sudo apt-get install -y build-essential tcl

# 下载Redis (示例: 7.2.3)
wget https://github.com/redis/redis/releases/download/7.2.3/redis-7.2.3.tar.gz
tar xzf redis-7.2.3.tar.gz
cd redis-7.2.3

# 编译安装
make
make test
sudo make install

# 配置Redis
sudo mkdir -p /etc/redis
sudo cp redis.conf /etc/redis/redis.conf

# 启动Redis
redis-server /etc/redis/redis.conf
```

## 配置Redis

### 基础配置

编辑 `/etc/redis/redis.conf` (apt安装) 或自定义配置文件：

```ini
# 绑定地址 (允许远程访问)
bind 0.0.0.0

# 端口
port 6379

# 持久化 - RDB
save 900 1
save 300 10
save 60 10000

# 持久化 - AOF (可选，更安全但性能略低)
appendonly yes
appendfilename "appendonly.aof"
appendfsync everysec

# 内存管理
maxmemory 256mb
maxmemory-policy allkeys-lru

# 日志
loglevel notice
logfile /var/log/redis/redis-server.log

# 数据目录
dir /var/lib/redis
```

### 启用认证 (生产环境推荐)

```bash
# 生成密码
python3 -c "import secrets; print(secrets.token_urlsafe(16))"

# 编辑配置文件
sudo nano /etc/redis/redis.conf
```

添加/修改以下配置：

```ini
# 取消注释并设置密码
requirepass your_generated_password_here

# 或使用环境变量 (推荐)
# 在启动脚本中: redis-server --requirepass $REDIS_PASSWORD
```

### 配置远程访问

编辑 `/etc/redis/redis.conf`:

```ini
# 绑定所有接口
bind 0.0.0.0

# 保护模式 (如果设置了密码，可以禁用)
protected-mode no
```

防火墙配置：

```bash
# UFW防火墙
sudo ufw allow 6379/tcp

# 或者只允许特定IP
sudo ufw allow from 192.168.1.0/24 to any port 6379
```

重启服务：

```bash
sudo service redis-server restart
```

## 验证安装

```bash
# 测试连接 (无密码)
redis-cli ping
redis-cli info server

# 测试连接 (有密码)
redis-cli -a your_password ping
redis-cli -a your_password info server

# 测试远程连接
redis-cli -h localhost -p 6379 -a your_password ping

# 查看Redis信息
redis-cli info memory
redis-cli info stats
redis-cli info clients
```

## 环境变量配置

创建 `.env.local` 文件或在项目中配置：

```bash
# Redis配置
ATLAS_REDIS_HOST=localhost
ATLAS_REDIS_PORT=6379
ATLAS_REDIS_DB=0
ATLAS_REDIS_PASSWORD=your_secure_password_here  # 可选
ATLAS_REDIS_MAX_CONNECTIONS=10

# Celery配置
ATLAS_CELERY_BROKER_URL=redis://localhost:6379/0
ATLAS_CELERY_RESULT_BACKEND=redis://localhost:6379/1
ATLAS_CELERY_TASK_TRACK_STARTED=true
ATLAS_CELERY_TASK_TIME_LIMIT=3600
```

## 常用命令

### 服务管理

```bash
# 启动服务
sudo service redis-server start

# 停止服务
sudo service redis-server stop

# 重启服务
sudo service redis-server restart

# 查看状态
sudo service redis-server status

# 查看日志
sudo tail -f /var/log/redis/redis-server.log
```

### 数据管理

```bash
# 连接到Redis CLI
redis-cli

# 在CLI中执行以下命令:
# 查看所有键
KEYS *

# 查看键的类型
TYPE key_name

# 获取键值
GET key_name

# 设置键值
SET key_name "value"

# 删除键
DEL key_name

# 清空所有数据 (危险!)
FLUSHALL

# 清空当前数据库
FLUSHDB

# 查看数据库大小
DBSIZE

# 退出
exit
```

### 备份和恢复

```bash
# 触发RDB快照
redis-cli BGSAVE

# 检查备份状态
redis-cli LASTSAVE

# 备份RDB文件
sudo cp /var/lib/redis/dump.rdb /backup/dump-$(date +%Y%m%d).rdb

# 恢复RDB文件
sudo service redis-server stop
sudo cp /backup/dump-20231225.rdb /var/lib/redis/dump.rdb
sudo service redis-server start

# AOF备份 (如果启用了AOF)
sudo cp /var/lib/redis/appendonly.aof /backup/appendonly-$(date +%Y%m%d).aof
```

## 性能优化

### 内存优化

```ini
# 设置最大内存
maxmemory 512mb

# 内存淘汰策略
maxmemory-policy allkeys-lru  # LRU淘汰
# 其他选项:
# - volatile-lru: 只淘汰设置了TTL的键
# - allkeys-lru: 淘汰任何LRU键
# - volatile-random: 随机淘汰设置了TTL的键
# - allkeys-random: 随机淘汰任何键
# - volatile-ttl: 淘汰即将过期的键
# - noeviction: 不淘汰，内存满时返回错误
```

### 持久化优化

```ini
# RDB优化
save 900 1       # 15分钟内有1个写操作
save 300 10      # 5分钟内有10个写操作
save 60 10000    # 1分钟内有10000个写操作

# 禁用RDB (如果不需要)
# save ""

# AOF优化
appendonly yes
appendfsync everysec  # 每秒同步一次
# 其他选项:
# - always: 每次写操作同步 (最安全，最慢)
# - everysec: 每秒同步 (推荐)
# - no: 由操作系统决定 (最快，不安全)
```

## 故障排查

### 问题1: 端口已被占用

```bash
# 检查端口占用
sudo netstat -tlnp | grep 6379
# 或
sudo ss -tlnp | grep 6379

# 停止占用进程
sudo kill -9 <PID>

# 或者修改Redis端口
# 编辑 /etc/redis/redis.conf
port 6380
```

### 问题2: 连接被拒绝

```bash
# 检查Redis状态
sudo service redis-server status

# 查看日志
sudo tail -f /var/log/redis/redis-server.log

# 检查防火墙
sudo ufw status

# 测试连接
redis-cli ping
```

### 问题3: 内存不足

```bash
# 查看内存使用
redis-cli info memory

# 设置最大内存
redis-cli CONFIG SET maxmemory 256mb
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# 清理过期键
redis-cli --scan --pattern "*" | xargs redis-cli DEL
```

### 问题4: 持久化失败

```bash
# 检查磁盘空间
df -h

# 检查Redis数据目录权限
ls -la /var/lib/redis

# 修复权限
sudo chown redis:redis /var/lib/redis
sudo chmod 755 /var/lib/redis

# 检查配置
redis-cli CONFIG GET dir
redis-cli CONFIG GET save
```

## 安全建议

1. **启用认证** (生产环境必须)
   ```bash
   # 生成强密码
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **绑定到特定接口** (不要使用0.0.0.0)
   ```ini
   bind 127.0.0.1  # 仅本地访问
   # 或
   bind 192.168.1.100  # 特定IP
   ```

3. **禁用危险命令**
   ```ini
   rename-command FLUSHDB ""
   rename-command FLUSHALL ""
   rename-command CONFIG ""
   ```

4. **使用防火墙**限制访问
   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 6379
   ```

5. **定期备份**
   ```bash
   # 添加到crontab
   0 2 * * * redis-cli BGSAVE && cp /var/lib/redis/dump.rdb /backup/dump-$(date +\%Y\%m\%d).rdb
   ```

## 监控和维护

### 实时监控

```bash
# 使用redis-cli监控
redis-cli MONITOR

# 查看实时统计
redis-cli --stat

# 查看慢查询
redis-cli SLOWLOG GET 10
```

### 健康检查

```bash
# 创建检查脚本
cat > /usr/local/bin/check_redis.sh << 'EOF'
#!/bin/bash
if redis-cli ping > /dev/null 2>&1; then
    echo "Redis is running"
    exit 0
else
    echo "Redis is not running"
    exit 1
fi
EOF

chmod +x /usr/local/bin/check_redis.sh
```

## 下一步

Redis安装完成后，继续Celery集成：

```bash
# 1. 安装Python依赖
uv sync

# 2. 验证环境
python3 scripts/check_redis_env.py

# 3. 测试Redis连接
python3 -c "from redis import Redis; r = Redis(); print(r.ping())"

# 4. 启动Celery Worker
celery -A atlas.scheduler.celery_app worker --loglevel=info

# 5. 启动Celery Beat
celery -A atlas.scheduler.celery_app beat --loglevel=info

# 6. 启动Flower监控
celery -A atlas.scheduler.celery_app flower --port=5555
```

---

*最后更新: 2025-12-28*
*维护者: Claude Sonnet*
*相关任务: TASK-003*
