# PostgreSQL 安装和配置指南

> ⚠️ **安全提示**：本文档中的示例密码（如`atlas_password`）仅用于演示。在生产环境中，请使用强密码并通过`.env.local`文件管理。

## 环境要求

- Ubuntu/Debian Linux (WSL)
- PostgreSQL 16+ (推荐)
- Python 3.13+

## 快速安装 (Ubuntu/Debian)

### 方式1: apt安装 (推荐)

```bash
# 更新包列表
sudo apt-get update

# 安装PostgreSQL和相关工具
sudo apt-get install -y postgresql postgresql-contrib postgresql-client-common

# 启动PostgreSQL服务
sudo service postgresql start

# 验证安装
sudo -u postgres psql --version
```

### 方式2: Docker安装

```bash
# 安装Docker (如果未安装)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 启动PostgreSQL容器
docker run -d \
  --name atlas-postgres \
  -e POSTGRES_DB=atlas_db \
  -e POSTGRES_USER=atlas_user \
  -e POSTGRES_PASSWORD=atlas_password \
  -p 5432:5432 \
  postgres:16-alpine

# 验证运行
docker ps | grep atlas-postgres
```

## 配置PostgreSQL

### 创建数据库和用户

```bash
# 连接到PostgreSQL
sudo -u postgres psql

# 在psql中执行以下命令:
CREATE DATABASE atlas_db;
CREATE USER atlas_user WITH ENCRYPTED PASSWORD 'atlas_password';
GRANT ALL PRIVILEGES ON DATABASE atlas_db TO atlas_user;
\q
```

### 配置远程访问 (可选)

编辑 `/etc/postgresql/*/main/postgresql.conf`:
```ini
listen_addresses = '*'
```

编辑 `/etc/postgresql/*/main/pg_hba.conf`:
```ini
host    all             all             0.0.0.0/0               md5
```

重启服务:
```bash
sudo service postgresql restart
```

## 验证安装

```bash
# 测试连接
psql -h localhost -U atlas_user -d atlas_db -c "SELECT version();"

# 查看数据库列表
psql -h localhost -U atlas_user -d atlas_db -c "\l"

# 查看表空间
psql -h localhost -U atlas_user -d atlas_db -c "\dn"
```

## 环境变量配置

创建 `.env` 文件或在项目中配置:

```bash
# PostgreSQL配置
ATLAS_DB_TYPE=postgresql
ATLAS_POSTGRES_HOST=localhost
ATLAS_POSTGRES_PORT=5432
ATLAS_POSTGRES_DB=atlas_db
ATLAS_POSTGRES_USER=atlas_user
ATLAS_POSTGRES_PASSWORD=atlas_password
```

## 常用命令

```bash
# 启动服务
sudo service postgresql start

# 停止服务
sudo service postgresql stop

# 重启服务
sudo service postgresql restart

# 查看状态
sudo service postgresql status

# 连接数据库
psql -h localhost -U atlas_user -d atlas_db

# 备份数据库
pg_dump -h localhost -U atlas_user atlas_db > backup.sql

# 恢复数据库
psql -h localhost -U atlas_user atlas_db < backup.sql
```

## 故障排查

### 问题1: 端口已被占用
```bash
# 检查端口占用
sudo netstat -tlnp | grep 5432

# 停止占用进程
sudo kill -9 <PID>
```

### 问题2: 连接被拒绝
```bash
# 检查PostgreSQL状态
sudo service postgresql status

# 查看日志
sudo tail -f /var/log/postgresql/postgresql-*.log
```

### 问题3: 权限错误
```bash
# 修复权限
sudo -u postgres psql
ALTER USER atlas_user WITH SUPERUSER;
```

## 下一步

安装完成后，运行数据库迁移脚本:

```bash
# 执行迁移 (预演模式)
python scripts/migrate_to_postgres.py --dry-run

# 执行迁移 (真实模式)
python scripts/migrate_to_postgres.py --execute

# 验证迁移
python scripts/migrate_to_postgres.py --verify
```

---

*最后更新: 2025-12-25*
*维护者: Claude Sonnet*
