---
version: "1.0.0"
last_updated: "2025-01-20"
updated_by: "Claude Sonnet"
document_type: "mutable"
category: "deployment_guide"
status: "active"
tags: ["deployment", "atlas", "production", "setup"]
reviewer: "用户"
---

# Atlas 部署指南

> Atlas 个人信息聚合系统生产环境部署指南

---

## 文档信息

- **创建时间**：2025-01-20
- **文档类型**：可变更文档
- **系统版本**：Atlas v0.1.0 MVP
- **部署环境**：Linux 生产环境
- **难度级别**：中级到高级

---

## 📖 目录

- [部署概览](#部署概览)
- [环境准备](#环境准备)
- [快速部署](#快速部署)
- [生产环境部署](#生产环境部署)
- [容器化部署](#容器化部署)
- [云服务部署](#云服务部署)
- [监控和维护](#监控和维护)
- [安全配置](#安全配置)
- [升级和迁移](#升级和迁移)

---

## 部署概览

### 🎯 部署架构

```
┌─────────────────────────────────────────────────────────────┐
│                        Atlas 系统                           │
├─────────────────────────────────────────────────────────────┤
│  Web界面/CLI (用户交互层)                                    │
├─────────────────────────────────────────────────────────────┤
│  应用服务层 (Core Services)                                  │
│  ├── 数据采集服务                                             │
│  ├── 数据处理服务                                             │
│  ├── 定时任务服务                                             │
│  └── 监控服务                                                │
├─────────────────────────────────────────────────────────────┤
│  数据存储层                                                  │
│  ├── SQLite 数据库                                            │
│  ├── JSON 文件存储                                            │
│  └── 索引文件                                                │
├─────────────────────────────────────────────────────────────┤
│  系统服务层                                                  │
│  ├── Nginx (反向代理)                                         │
│  ├── Supervisor (进程管理)                                    │
│  ├── Cron (定时任务)                                          │
│  └── Logrotate (日志轮转)                                     │
└─────────────────────────────────────────────────────────────┘
```

### 📋 部署清单

- [ ] 系统环境检查
- [ ] 依赖软件安装
- [ ] Atlas应用部署
- [ ] 数据库初始化
- [ ] 配置文件设置
- [ ] 服务进程配置
- [ ] 监控和日志配置
- [ ] 安全设置
- [ ] 备份策略配置
- [ ] 测试验证

---

## 环境准备

### 🔧 系统要求

#### 硬件要求

| 组件 | 最低配置 | 推荐配置 | 生产配置 |
|------|----------|----------|----------|
| CPU | 2核 | 4核 | 8核 |
| 内存 | 2GB | 4GB | 8GB+ |
| 存储 | 10GB | 50GB | 100GB+ |
| 网络 | 1Mbps | 10Mbps | 100Mbps |

#### 软件要求

- **操作系统**：Ubuntu 20.04+ / CentOS 8+ / Debian 11+
- **Python**：3.13.x
- **数据库**：SQLite 3.31+ (内置)
- **Web服务器**：Nginx 1.18+ (可选)
- **进程管理**：Supervisor 4.0+
- **其他工具**：Git, Curl, Wget, Cron

### 📦 依赖安装

#### Ubuntu/Debian

```bash
#!/bin/bash
# 安装系统依赖
sudo apt update
sudo apt install -y \
    python3.13 \
    python3.13-venv \
    python3.13-dev \
    python3-pip \
    git \
    curl \
    wget \
    nginx \
    supervisor \
    sqlite3 \
    cron \
    htop \
    iotop \
    logrotate

# 安装 uv (Python 包管理器)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

#### CentOS/RHEL/Fedora

```bash
#!/bin/bash
# 安装系统依赖
sudo dnf update -y
sudo dnf install -y \
    python3.13 \
    python3.13-devel \
    python3-pip \
    git \
    curl \
    wget \
    nginx \
    supervisor \
    sqlite \
    cronie \
    htop \
    iotop \
    logrotate

# 启用 cron 服务
sudo systemctl enable crond
sudo systemctl start crond

# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 👤 用户和权限设置

```bash
#!/bin/bash
# 创建专用用户
sudo useradd -r -s /bin/false atlas

# 创建应用目录
sudo mkdir -p /opt/atlas
sudo mkdir -p /var/log/atlas
sudo mkdir -p /var/lib/atlas
sudo mkdir -p /etc/atlas

# 设置权限
sudo chown -R atlas:atlas /opt/atlas
sudo chown -R atlas:atlas /var/log/atlas
sudo chown -R atlas:atlas /var/lib/atlas
sudo chown -R atlas:atlas /etc/atlas

# 添加当前用户到 atlas 组 (开发时使用)
sudo usermod -a -G atlas $USER
```

---

## 快速部署

### 🚀 一键部署脚本

```bash
#!/bin/bash
# deploy.sh - Atlas 快速部署脚本

set -e

# 配置变量
ATLAS_USER="atlas"
ATLAS_HOME="/opt/atlas"
ATLAS_DATA="/var/lib/atlas"
ATLAS_LOG="/var/log/atlas"
ATLAS_CONFIG="/etc/atlas"
REPO_URL="https://github.com/JafeChang/Atlas.git"
BRANCH="main"

echo "🚀 开始部署 Atlas 系统..."

# 1. 创建用户和目录
echo "📁 创建用户和目录..."
if ! id "$ATLAS_USER" &>/dev/null; then
    sudo useradd -r -s /bin/false $ATLAS_USER
fi

sudo mkdir -p $ATLAS_HOME $ATLAS_DATA $ATLAS_LOG $ATLAS_CONFIG
sudo chown -R $ATLAS_USER:$ATLAS_USER $ATLAS_HOME $ATLAS_DATA $ATLAS_LOG $ATLAS_CONFIG

# 2. 获取源代码
echo "📥 获取源代码..."
cd $ATLAS_HOME
sudo -u $ATLAS_USER git clone $REPO_URL .
sudo -u $ATLAS_USER git checkout $BRANCH

# 3. 安装Python依赖
echo "🐍 安装Python依赖..."
sudo -u $ATLAS_USER uv sync

# 4. 配置文件
echo "⚙️ 配置文件..."
sudo -u $ATLAS_USER cp config/config.yaml.example $ATLAS_CONFIG/config.yaml
sudo -u $ATLAS_USER cp config/sources.yaml.example $ATLAS_CONFIG/sources.yaml
sudo -u $ATLAS_USER cp .env.example $ATLAS_CONFIG/.env

# 5. 初始化数据库
echo "💾 初始化数据库..."
sudo -u $ATLAS_USER $ATLAS_HOME/.venv/bin/python -m atlas init \
    --config-dir $ATLAS_CONFIG \
    --data-dir $ATLAS_DATA

# 6. 创建服务文件
echo "🔧 创建服务文件..."
sudo tee /etc/systemd/system/atlas-collector.service > /dev/null <<EOF
[Unit]
Description=Atlas Data Collector Service
After=network.target

[Service]
Type=simple
User=$ATLAS_USER
Group=$ATLAS_USER
WorkingDirectory=$ATLAS_HOME
Environment=PATH=$ATLAS_HOME/.venv/bin
Environment=ATLAS_CONFIG_DIR=$ATLAS_CONFIG
Environment=ATLAS_DATA_DIR=$ATLAS_DATA
ExecStart=$ATLAS_HOME/.venv/bin/python -m atlas collect --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/atlas-scheduler.service > /dev/null <<EOF
[Unit]
Description=Atlas Scheduler Service
After=network.target

[Service]
Type=simple
User=$ATLAS_USER
Group=$ATLAS_USER
WorkingDirectory=$ATLAS_HOME
Environment=PATH=$ATLAS_HOME/.venv/bin
Environment=ATLAS_CONFIG_DIR=$ATLAS_CONFIG
Environment=ATLAS_DATA_DIR=$ATLAS_DATA
ExecStart=$ATLAS_HOME/.venv/bin/python -m atlas scheduler start
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 7. 启用服务
echo "🎯 启用服务..."
sudo systemctl daemon-reload
sudo systemctl enable atlas-collector
sudo systemctl enable atlas-scheduler

# 8. 配置日志轮转
echo "📝 配置日志轮转..."
sudo tee /etc/logrotate.d/atlas > /dev/null <<EOF
$ATLAS_LOG/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 $ATLAS_USER $ATLAS_USER
    postrotate
        systemctl reload atlas-collector || true
        systemctl reload atlas-scheduler || true
    endscript
}
EOF

echo "✅ Atlas 部署完成!"
echo ""
echo "下一步操作:"
echo "1. 编辑配置文件: sudo nano $ATLAS_CONFIG/config.yaml"
echo "2. 添加数据源: sudo -u $ATLAS_USER $ATLAS_HOME/.venv/bin/python -m atlas source add ..."
echo "3. 启动服务: sudo systemctl start atlas-collector atlas-scheduler"
echo "4. 查看状态: sudo systemctl status atlas-collector atlas-scheduler"
```

### ⚡ 快速验证

```bash
#!/bin/bash
# verify.sh - 部署验证脚本

echo "🔍 验证 Atlas 部署..."

# 检查服务状态
echo "📊 服务状态:"
sudo systemctl is-active atlas-collector || echo "❌ atlas-collector 未运行"
sudo systemctl is-active atlas-scheduler || echo "❌ atlas-scheduler 未运行"

# 检查端口
echo "🌐 网络端口:"
netstat -tlnp | grep :80 || echo "⚠️ 端口 80 未监听 (Nginx 未配置)"

# 检查磁盘空间
echo "💾 磁盘空间:"
df -h /opt/atlas /var/lib/atlas

# 检查内存使用
echo "🧠 内存使用:"
free -h

# 测试基本功能
echo "🧪 功能测试:"
sudo -u atlas /opt/atlas/.venv/bin/python -m atlas --version || echo "❌ Atlas CLI 不可用"
sudo -u atlas /opt/atlas/.venv/bin/python -m atlas status || echo "❌ Atlas 状态检查失败"

echo "✅ 验证完成!"
```

---

## 生产环境部署

### 🔧 详细配置

#### 1. Nginx 配置

```nginx
# /etc/nginx/sites-available/atlas
server {
    listen 80;
    server_name your-domain.com;

    # 安全头
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;
    add_header Content-Security-Policy "default-src 'self' http: https: data: blob: 'unsafe-inline'" always;

    # 日志配置
    access_log /var/log/nginx/atlas.access.log;
    error_log /var/log/nginx/atlas.error.log;

    # 反向代理配置 (如果需要Web界面)
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 超时设置
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;

        # 缓冲设置
        proxy_buffering on;
        proxy_buffer_size 4k;
        proxy_buffers 8 4k;
    }

    # API端点
    location /api/ {
        proxy_pass http://127.0.0.1:8080/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 静态文件
    location /static/ {
        alias /opt/atlas/static/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}

# HTTPS 配置 (生产环境推荐)
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    # SSL 证书
    ssl_certificate /etc/ssl/certs/your-domain.crt;
    ssl_certificate_key /etc/ssl/private/your-domain.key;

    # SSL 配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # HSTS
    add_header Strict-Transport-Security "max-age=31536000" always;

    # 其他配置与 HTTP 相同...
    include /etc/nginx/sites-available/atlas;
}

# HTTP 重定向到 HTTPS
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

#### 2. Supervisor 配置

```ini
# /etc/supervisor/conf.d/atlas.conf
[program:atlas-collector]
command=/opt/atlas/.venv/bin/python -m atlas collect --daemon
directory=/opt/atlas
user=atlas
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/atlas/collector.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=ATLAS_CONFIG_DIR="/etc/atlas",ATLAS_DATA_DIR="/var/lib/atlas"

[program:atlas-scheduler]
command=/opt/atlas/.venv/bin/python -m atlas scheduler start
directory=/opt/atlas
user=atlas
autostart=true
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/atlas/scheduler.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=ATLAS_CONFIG_DIR="/etc/atlas",ATLAS_DATA_DIR="/var/lib/atlas"

[program:atlas-web]
command=/opt/atlas/.venv/bin/python -m atlas web --host 127.0.0.1 --port 8080
directory=/opt/atlas
user=atlas
autostart=false
autorestart=true
redirect_stderr=true
stdout_logfile=/var/log/atlas/web.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=10
environment=ATLAS_CONFIG_DIR="/etc/atlas",ATLAS_DATA_DIR="/var/lib/atlas"
```

#### 3. 生产环境配置

```yaml
# /etc/atlas/config.yaml
# 基础配置
env: "production"
debug: false
log_level: "INFO"

# 数据目录
data_dir: "/var/lib/atlas"
log_dir: "/var/log/atlas"

# 数据库配置
database:
  url: "sqlite:///var/lib/atlas/atlas.db"
  pool_size: 20
  max_overflow: 40
  connection_timeout: 30

# 采集配置
collection:
  default_user_agent: "Atlas/1.0 (Production)"
  request_timeout: 60
  max_concurrent_requests: 10
  rate_limit_delay: 2.0
  retry_attempts: 5
  retry_backoff_factor: 2.0

# 存储配置
storage:
  compression: true
  file_retention_days: 180
  index_enabled: true
  backup_enabled: true
  backup_interval: 86400  # 24小时

# 去重配置
deduplication:
  strategy: "hybrid"
  similarity_threshold: 0.85
  batch_size: 500
  cache_size: 10000

# 监控配置
monitoring:
  enable_performance_monitoring: true
  enable_health_checks: true
  enable_alerts: true
  metrics_retention_days: 90
  alert_webhook: "https://your-webhook-url.com/alerts"

# 安全配置
security:
  enable_authentication: false
  allowed_ips: []
  rate_limiting: true
  max_requests_per_minute: 120
  request_timeout: 120
  block_suspicious_ips: true

# 性能配置
performance:
  max_memory_mb: 4096
  max_cpu_percent: 80
  gc_threshold_mb: 100
  connection_pool_size: 50
```

#### 4. 系统服务配置

```bash
#!/bin/bash
# 生产环境服务配置

# 1. 创建 systemd 服务文件
sudo tee /etc/systemd/system/atlas.target > /dev/null <<EOF
[Unit]
Description=Atlas Services Target
After=network.target

[Install]
WantedBy=multi-user.target
EOF

# 2. 启用服务
sudo systemctl daemon-reload
sudo systemctl enable atlas.target
sudo systemctl enable atlas-collector
sudo systemctl enable atlas-scheduler

# 3. 配置防火墙 (如果使用 UFW)
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw --force enable

# 4. 配置系统限制
sudo tee /etc/security/limits.d/atlas.conf > /dev/null <<EOF
atlas soft nofile 65536
atlas hard nofile 65536
atlas soft nproc 32768
atlas hard nproc 32768
EOF

# 5. 配置内核参数
sudo tee /etc/sysctl.d/99-atlas.conf > /dev/null <<EOF
# 网络优化
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 5000
net.ipv4.tcp_max_syn_backlog = 65535

# 文件描述符限制
fs.file-max = 2097152

# 内存优化
vm.swappiness = 10
vm.dirty_ratio = 15
vm.dirty_background_ratio = 5
EOF

sudo sysctl -p /etc/sysctl.d/99-atlas.conf
```

---

## 容器化部署

### 🐳 Docker 配置

#### 1. Dockerfile

```dockerfile
# Dockerfile
FROM python:3.13-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    ATLAS_CONFIG_DIR=/app/config \
    ATLAS_DATA_DIR=/app/data \
    ATLAS_LOG_DIR=/app/logs

# 创建用户
RUN groupadd -r atlas && useradd -r -g atlas atlas

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN pip install uv

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY pyproject.toml uv.lock ./

# 安装Python依赖
RUN uv sync --frozen

# 复制应用代码
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# 创建数据目录
RUN mkdir -p /app/data /app/logs && \
    chown -R atlas:atlas /app

# 切换用户
USER atlas

# 暴露端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/health')" || exit 1

# 启动命令
CMD ["python", "-m", "atlas", "web", "--host", "0.0.0.0", "--port", "8080"]
```

#### 2. Docker Compose

```yaml
# docker-compose.yml
version: '3.8'

services:
  atlas:
    build: .
    container_name: atlas-app
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./config:/app/config
    environment:
      - ATLAS_ENV=production
      - ATLAS_LOG_LEVEL=INFO
    networks:
      - atlas-network
    depends_on:
      - db
      - redis

  db:
    image: sqlite:latest
    container_name: atlas-db
    restart: unless-stopped
    volumes:
      - ./data:/data
    networks:
      - atlas-network

  redis:
    image: redis:7-alpine
    container_name: atlas-redis
    restart: unless-stopped
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    networks:
      - atlas-network

  nginx:
    image: nginx:alpine
    container_name: atlas-nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./ssl:/etc/nginx/ssl
      - ./static:/var/www/static
    depends_on:
      - atlas
    networks:
      - atlas-network

  collector:
    build: .
    container_name: atlas-collector
    restart: unless-stopped
    command: python -m atlas collect --daemon
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./config:/app/config
    environment:
      - ATLAS_ENV=production
    networks:
      - atlas-network
    depends_on:
      - db
      - redis

  scheduler:
    build: .
    container_name: atlas-scheduler
    restart: unless-stopped
    command: python -m atlas scheduler start
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./config:/app/config
    environment:
      - ATLAS_ENV=production
    networks:
      - atlas-network
    depends_on:
      - db
      - redis

networks:
  atlas-network:
    driver: bridge

volumes:
  redis-data:
```

#### 3. 容器部署脚本

```bash
#!/bin/bash
# docker-deploy.sh - Docker 部署脚本

set -e

# 配置变量
ATLAS_HOME="/opt/atlas"
DATA_DIR="$ATLAS_HOME/data"
LOGS_DIR="$ATLAS_HOME/logs"
CONFIG_DIR="$ATLAS_HOME/config"

echo "🐳 开始 Docker 部署 Atlas..."

# 1. 创建目录结构
echo "📁 创建目录结构..."
sudo mkdir -p $DATA_DIR $LOGS_DIR $CONFIG_DIR
sudo chown -R $USER:$USER $ATLAS_HOME

# 2. 获取源代码
echo "📥 获取源代码..."
cd $ATLAS_HOME
git clone https://github.com/JafeChang/Atlas.git .

# 3. 复制配置文件
echo "⚙️ 配置文件..."
cp config/config.yaml.example $CONFIG_DIR/config.yaml
cp config/sources.yaml.example $CONFIG_DIR/sources.yaml

# 4. 构建和启动容器
echo "🏗️ 构建容器..."
docker-compose build

echo "🚀 启动服务..."
docker-compose up -d

# 5. 等待服务启动
echo "⏳ 等待服务启动..."
sleep 30

# 6. 验证部署
echo "🔍 验证部署..."
docker-compose ps

# 7. 初始化数据
echo "💾 初始化数据..."
docker-compose exec atlas python -m atlas init

echo "✅ Docker 部署完成!"
echo ""
echo "管理命令:"
echo "- 查看日志: docker-compose logs -f"
echo "- 重启服务: docker-compose restart"
echo "- 停止服务: docker-compose down"
echo "- 查看状态: docker-compose ps"
```

---

## 云服务部署

### ☁️ AWS 部署

#### 1. EC2 实例配置

```bash
#!/bin/bash
# aws-deploy.sh - AWS 部署脚本

# EC2 实例规格
INSTANCE_TYPE="t3.medium"
AMI_ID="ami-0c02fb55956c7d316"  # Ubuntu 20.04 LTS
KEY_NAME="your-key-pair"
SECURITY_GROUP="sg-xxxxxxxx"
SUBNET_ID="subnet-xxxxxxxx"

# 创建实例
aws ec2 run-instances \
  --image-id $AMI_ID \
  --instance-type $INSTANCE_TYPE \
  --key-name $KEY_NAME \
  --security-group-ids $SECURITY_GROUP \
  --subnet-id $SUBNET_ID \
  --user-data file://cloud-init.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=Atlas-Server}]'

# 等待实例启动
aws ec2 wait instance-running --instance-ids $INSTANCE_ID

# 获取公网IP
PUBLIC_IP=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)

echo "实例启动完成，公网IP: $PUBLIC_IP"
```

#### 2. Cloud-Init 配置

```bash
#!/bin/bash
# cloud-init.sh - EC2 用户数据脚本

# 更新系统
apt-get update -y
apt-get upgrade -y

# 安装依赖
apt-get install -y python3.13 python3.13-venv git nginx supervisor

# 创建用户
useradd -r -s /bin/false atlas

# 部署 Atlas
cd /opt
git clone https://github.com/JafeChang/Atlas.git
chown -R atlas:atlas Atlas

cd Atlas
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv sync

# 配置服务
# ... (类似本地部署的配置)

# 启动服务
systemctl enable atlas-collector atlas-scheduler
systemctl start atlas-collector atlas-scheduler
```

### 🔵 Google Cloud Platform

#### 1. GCE 实例配置

```bash
#!/bin/bash
# gcp-deploy.sh - GCP 部署脚本

# 设置项目
PROJECT_ID="your-project-id"
ZONE="us-central1-a"
INSTANCE_NAME="atlas-server"

# 创建实例
gcloud compute instances create $INSTANCE_NAME \
  --project=$PROJECT_ID \
  --zone=$ZONE \
  --machine-type=e2-medium \
  --image-family=ubuntu-2004-lts \
  --image-project=ubuntu-os-cloud \
  --metadata-from-file startup-script=startup-script.sh \
  --tags=http-server,https-server

# 创建防火墙规则
gcloud compute firewall-rules create allow-http \
  --allow tcp:80 \
  --target-tags=http-server

gcloud compute firewall-rules create allow-https \
  --allow tcp:443 \
  --target-tags=https-server

echo "GCP 部署完成!"
```

---

## 监控和维护

### 📊 监控配置

#### 1. Prometheus 配置

```yaml
# prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'atlas'
    static_configs:
      - targets: ['localhost:9090']
    metrics_path: '/metrics'
    scrape_interval: 30s

  - job_name: 'node-exporter'
    static_configs:
      - targets: ['localhost:9100']

  - job_name: 'nginx-exporter'
    static_configs:
      - targets: ['localhost:9113']
```

#### 2. Grafana 仪表板

```json
{
  "dashboard": {
    "title": "Atlas 监控仪表板",
    "panels": [
      {
        "title": "系统资源使用率",
        "type": "graph",
        "targets": [
          {
            "expr": "rate(cpu_usage_total[5m])",
            "legendFormat": "CPU使用率"
          },
          {
            "expr": "memory_usage_bytes / memory_total_bytes * 100",
            "legendFormat": "内存使用率"
          }
        ]
      },
      {
        "title": "采集任务状态",
        "type": "stat",
        "targets": [
          {
            "expr": "atlas_collection_success_total",
            "legendFormat": "成功采集"
          },
          {
            "expr": "atlas_collection_errors_total",
            "legendFormat": "采集错误"
          }
        ]
      }
    ]
  }
}
```

#### 3. 告警规则

```yaml
# alerts.yml
groups:
  - name: atlas-alerts
    rules:
      - alert: AtlasDown
        expr: up{job="atlas"} == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Atlas 服务已停止"
          description: "Atlas 服务已停止超过5分钟"

      - alert: HighErrorRate
        expr: rate(atlas_collection_errors_total[5m]) > 0.1
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "采集错误率过高"
          description: "采集错误率超过10%"

      - alert: HighMemoryUsage
        expr: memory_usage_bytes / memory_total_bytes * 100 > 90
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "内存使用率过高"
          description: "内存使用率超过90%"
```

### 📝 日志管理

#### 1. 日志配置

```yaml
# config/logging.yaml
version: 1
disable_existing_loggers: false

formatters:
  standard:
    format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt: "%Y-%m-%d %H:%M:%S"

  json:
    format: '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}'

handlers:
  file:
    class: logging.handlers.RotatingFileHandler
    level: INFO
    formatter: standard
    filename: /var/log/atlas/atlas.log
    maxBytes: 52428800  # 50MB
    backupCount: 10

  error_file:
    class: logging.handlers.RotatingFileHandler
    level: ERROR
    formatter: standard
    filename: /var/log/atlas/error.log
    maxBytes: 52428800  # 50MB
    backupCount: 10

  json_file:
    class: logging.handlers.RotatingFileHandler
    level: INFO
    formatter: json
    filename: /var/log/atlas/atlas.json
    maxBytes: 52428800  # 50MB
    backupCount: 10

loggers:
  atlas:
    level: INFO
    handlers: [file, error_file, json_file]
    propagate: false

root:
  level: INFO
  handlers: [file]
```

#### 2. 日志轮转配置

```bash
# /etc/logrotate.d/atlas
/var/log/atlas/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 atlas atlas
    sharedscripts
    postrotate
        systemctl reload atlas-collector || true
        systemctl reload atlas-scheduler || true
    endscript
}

/var/log/atlas/*.json {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 atlas atlas
    postrotate
        systemctl reload atlas-collector || true
        systemctl reload atlas-scheduler || true
    endscript
}
```

### 🔧 维护脚本

#### 1. 系统维护脚本

```bash
#!/bin/bash
# maintenance.sh - 系统维护脚本

set -e

LOG_FILE="/var/log/atlas/maintenance.log"
BACKUP_DIR="/opt/atlas/backups"
DATE=$(date +%Y%m%d_%H%M%S)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a $LOG_FILE
}

# 1. 系统健康检查
log "开始系统健康检查..."

# 检查服务状态
services=("atlas-collector" "atlas-scheduler")
for service in "${services[@]}"; do
    if systemctl is-active --quiet $service; then
        log "✅ $service 运行正常"
    else
        log "❌ $service 未运行，尝试重启..."
        systemctl restart $service
    fi
done

# 检查磁盘空间
disk_usage=$(df /opt/atlas | tail -1 | awk '{print $5}' | sed 's/%//')
if [ $disk_usage -gt 85 ]; then
    log "⚠️ 磁盘使用率过高: $disk_usage%"

    # 清理旧日志
    find /var/log/atlas -name "*.log.*" -mtime +7 -delete
    log "🧹 清理旧日志文件"
fi

# 检查内存使用
memory_usage=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100.0}')
if [ $memory_usage -gt 90 ]; then
    log "⚠️ 内存使用率过高: $memory_usage%"

    # 重启高内存占用的服务
    systemctl restart atlas-collector
    log "🔄 重启采集服务以释放内存"
fi

# 2. 数据库维护
log "开始数据库维护..."

# 数据库优化
sqlite3 /var/lib/atlas/atlas.db "VACUUM; ANALYZE;"
log "🔧 数据库优化完成"

# 清理过期数据
/opt/atlas/.venv/bin/python -m atlas cleanup --days 180
log "🧹 清理过期数据完成"

# 3. 备份数据
log "开始数据备份..."

mkdir -p $BACKUP_DIR
backup_file="$BACKUP_DIR/atlas_backup_$DATE.tar.gz"

tar -czf $backup_file \
    /var/lib/atlas \
    /etc/atlas \
    --exclude=/var/lib/atlas/cache

log "✅ 备份完成: $backup_file"

# 清理旧备份 (保留30天)
find $BACKUP_DIR -name "atlas_backup_*.tar.gz" -mtime +30 -delete
log "🧹 清理旧备份完成"

log "维护完成"
```

#### 2. 自动化维护配置

```bash
# 添加到 crontab
# 编辑 crontab: crontab -e

# 每日凌晨2点执行维护
0 2 * * * /opt/atlas/scripts/maintenance.sh >> /var/log/atlas/maintenance_cron.log 2>&1

# 每小时检查服务状态
0 * * * * /opt/atlas/scripts/health_check.sh >> /var/log/atlas/health_check.log 2>&1

# 每天备份数据
0 3 * * * /opt/atlas/scripts/backup.sh >> /var/log/atlas/backup.log 2>&1
```

---

## 安全配置

### 🔒 基础安全设置

#### 1. 系统安全

```bash
#!/bin/bash
# security.sh - 安全配置脚本

# 1. 更新系统
apt update && apt upgrade -y

# 2. 安装安全工具
apt install -y fail2ban ufw logwatch

# 3. 配置防火墙
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow http
ufw allow https
ufw --force enable

# 4. 配置 fail2ban
cat > /etc/fail2ban/jail.local <<EOF
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log

[nginx-http-auth]
enabled = true
filter = nginx-http-auth
logpath = /var/log/nginx/error.log

[nginx-limit-req]
enabled = true
filter = nginx-limit-req
logpath = /var/log/nginx/error.log
EOF

systemctl enable fail2ban
systemctl start fail2ban

# 5. SSH 安全配置
sed -i 's/#PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/#Port 22/Port 2222/' /etc/ssh/sshd_config

systemctl restart ssh

echo "安全配置完成"
```

#### 2. 应用安全

```yaml
# /etc/atlas/security.yaml
# 安全配置
security:
  # 访问控制
  enable_authentication: false  # MVP版本暂不支持
  allowed_ips: []              # 允许的IP地址列表
  blocked_ips: []              # 屏蔽的IP地址列表

  # 速率限制
  rate_limiting: true
  max_requests_per_minute: 60
  max_requests_per_hour: 1000

  # 请求超时
  request_timeout: 120

  # SSL/TLS 配置
  ssl_verify: true
  ssl_cert_file: "/etc/ssl/certs/atlas.crt"
  ssl_key_file: "/etc/ssl/private/atlas.key"

  # 数据加密
  encrypt_database: false  # MVP版本暂不支持
  encrypt_backups: false   # MVP版本暂不支持

  # 审计日志
  enable_audit_log: true
  audit_log_file: "/var/log/atlas/audit.log"

  # 头部安全
  security_headers:
    x_frame_options: "DENY"
    x_content_type_options: "nosniff"
    x_xss_protection: "1; mode=block"
    strict_transport_security: "max-age=31536000; includeSubDomains"
    content_security_policy: "default-src 'self'"
```

#### 3. SSL/TLS 配置

```bash
#!/bin/bash
# ssl-setup.sh - SSL/TLS 配置脚本

DOMAIN="your-domain.com"
EMAIL="admin@your-domain.com"

# 1. 安装 Certbot
apt install -y certbot python3-certbot-nginx

# 2. 获取SSL证书
certbot --nginx -d $DOMAIN --email $EMAIL --agree-tos --non-interactive

# 3. 配置自动续期
echo "0 12 * * * /usr/bin/certbot renew --quiet" | crontab -

# 4. 强化SSL配置
cat >> /etc/nginx/nginx.conf <<EOF

# SSL 强化配置
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384;
ssl_prefer_server_ciphers off;
ssl_session_cache shared:SSL:10m;
ssl_session_timeout 10m;

# HSTS
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;

# 其他安全头
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "no-referrer-when-downgrade" always;
EOF

# 5. 重启Nginx
systemctl restart nginx

echo "SSL/TLS 配置完成"
```

---

## 升级和迁移

### 🔄 系统升级

#### 1. 升级脚本

```bash
#!/bin/bash
# upgrade.sh - 系统升级脚本

set -e

BACKUP_DIR="/opt/atlas/backups"
DATE=$(date +%Y%m%d_%H%M%S)
CURRENT_VERSION=$(cat /opt/atlas/VERSION || echo "unknown")
NEW_VERSION=$1

if [ -z "$NEW_VERSION" ]; then
    echo "用法: $0 <新版本号>"
    exit 1
fi

echo "开始升级 Atlas: $CURRENT_VERSION -> $NEW_VERSION"

# 1. 创建备份
echo "📦 创建备份..."
mkdir -p $BACKUP_DIR
backup_file="$BACKUP_DIR/pre_upgrade_$DATE.tar.gz"

tar -czf $backup_file \
    /opt/atlas \
    /var/lib/atlas \
    /etc/atlas \
    --exclude=/opt/atlas/.git \
    --exclude=/opt/atlas/node_modules

echo "✅ 备份完成: $backup_file"

# 2. 停止服务
echo "⏹️ 停止服务..."
systemctl stop atlas-collector atlas-scheduler

# 3. 备份配置
echo "⚙️ 备份配置..."
cp -r /etc/atlas /etc/atlas.backup.$DATE

# 4. 更新代码
echo "📥 更新代码..."
cd /opt/atlas
git fetch origin
git checkout $NEW_VERSION
git pull origin $NEW_VERSION

# 5. 更新依赖
echo "🐍 更新依赖..."
uv sync

# 6. 运行迁移
echo "🔄 运行数据库迁移..."
.venv/bin/python -m atlas migrate --from-version $CURRENT_VERSION

# 7. 验证升级
echo "🔍 验证升级..."
.venv/bin/python -m atlas --version
.venv/bin/python -m atlas status

# 8. 重启服务
echo "▶️ 重启服务..."
systemctl start atlas-collector atlas-scheduler

# 9. 验证服务
sleep 10
systemctl status atlas-collector atlas-scheduler

# 10. 清理旧版本
echo "🧹 清理旧版本..."
find /opt/atlas -name "*.pyc" -delete
find /opt/atlas -name "__pycache__" -type d -exec rm -rf {} +

echo "✅ 升级完成: $CURRENT_VERSION -> $NEW_VERSION"
```

#### 2. 回滚脚本

```bash
#!/bin/bash
# rollback.sh - 系统回滚脚本

set -e

BACKUP_FILE=$1

if [ -z "$BACKUP_FILE" ]; then
    echo "用法: $0 <备份文件路径>"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "错误: 备份文件不存在: $BACKUP_FILE"
    exit 1
fi

echo "开始回滚到备份: $BACKUP_FILE"

# 1. 停止服务
echo "⏹️ 停止服务..."
systemctl stop atlas-collector atlas-scheduler

# 2. 备份当前状态
echo "📦 备份当前状态..."
current_backup="/opt/atlas/backups/before_rollback_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf $current_backup \
    /opt/atlas \
    /var/lib/atlas \
    /etc/atlas

# 3. 恢复备份
echo "📥 恢复备份..."
tar -xzf $BACKUP_FILE -C /

# 4. 修复权限
echo "🔧 修复权限..."
chown -R atlas:atlas /opt/atlas /var/lib/atlas /etc/atlas

# 5. 重启服务
echo "▶️ 重启服务..."
systemctl daemon-reload
systemctl start atlas-collector atlas-scheduler

# 6. 验证回滚
echo "🔍 验证回滚..."
systemctl status atlas-collector atlas-scheduler

echo "✅ 回滚完成"
```

### 📊 数据迁移

#### 1. 数据导出脚本

```bash
#!/bin/bash
# export-data.sh - 数据导出脚本

EXPORT_DIR="/opt/atlas/exports"
DATE=$(date +%Y%m%d_%H%M%S)
EXPORT_FILE="$EXPORT_DIR/atlas_export_$DATE.json"

mkdir -p $EXPORT_DIR

echo "开始导出数据..."

# 导出配置
echo "📋 导出配置..."
tar -czf $EXPORT_DIR/config_$DATE.tar.gz /etc/atlas

# 导出数据库
echo "💾 导出数据库..."
sqlite3 /var/lib/atlas/atlas.db ".dump" > $EXPORT_DIR/database_$DATE.sql

# 导出JSON数据
echo "📄 导出JSON数据..."
/opt/atlas/.venv/bin/python -m atlas export \
    --format json \
    --output $EXPORT_FILE \
    --all

# 导出文件数据
echo "📁 导出文件数据..."
tar -czf $EXPORT_DIR/files_$DATE.tar.gz /var/lib/atlas/raw /var/lib/atlas/processed

# 创建导出清单
cat > $EXPORT_DIR/manifest_$DATE.txt <<EOF
Atlas 数据导出清单
导出时间: $(date)
导出文件:
- 配置文件: config_$DATE.tar.gz
- 数据库: database_$DATE.sql
- JSON数据: atlas_export_$DATE.json
- 文件数据: files_$DATE.tar.gz
EOF

echo "✅ 数据导出完成: $EXPORT_DIR"
```

---

## 📞 技术支持

### 🐛 部署问题排查

如果遇到部署问题，请检查以下常见问题：

1. **权限问题**：确保用户有正确的文件和目录权限
2. **端口冲突**：检查端口是否被其他服务占用
3. **依赖缺失**：确认所有Python和系统依赖已安装
4. **配置错误**：验证配置文件语法和参数
5. **资源限制**：检查系统资源是否满足要求

### 📞 联系方式

- **GitHub Issues**: https://github.com/JafeChang/Atlas/issues
- **文档仓库**: https://github.com/JafeChang/Atlas/docs
- **技术讨论**: https://github.com/JafeChang/Atlas/discussions

---

## 文档状态

- **当前版本**: v1.0.0
- **最后更新**: 2025-01-20
- **适用平台**: Linux, Docker, 云服务
- **维护状态**: 活跃维护
- **质量评级**: ⭐⭐⭐⭐⭐

---

*本文档遵循 [Atlas 文档体系规范](../documentation-system.md)*