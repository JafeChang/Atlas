---
version: "1.0.0"
last_updated: "2025-01-20"
updated_by: "Claude Sonnet"
document_type: "mutable"
category: "user_documentation"
status: "active"
tags: ["user-manual", "atlas", "mvp", "guide"]
reviewer: "用户"
---

# Atlas 用户手册

> Atlas 个人信息聚合系统用户使用指南

---

## 文档信息

- **创建时间**：2025-01-20
- **文档类型**：可变更文档
- **系统版本**：Atlas v0.1.0 MVP
- **目标用户**：个人信息管理需求者
- **难度级别**：初级到中级

---

## 📖 目录

- [系统概述](#系统概述)
- [快速开始](#快速开始)
- [核心功能](#核心功能)
- [配置管理](#配置管理)
- [命令行工具](#命令行工具)
- [高级功能](#高级功能)
- [常见问题](#常见问题)
- [最佳实践](#最佳实践)

---

## 系统概述

### 🎯 Atlas 是什么？

Atlas 是一个个人**信息聚合与分析系统**，专为帮助用户：

- **定期获取**公开可访问的多行业信息
- **自动分类**和结构化原始数据
- **智能去重**避免重复内容
- **统一存储**便于后续检索和分析

### ✨ 核心特性

| 特性 | 描述 | 优势 |
|------|------|------|
| 🔍 **多源采集** | 支持RSS、网站等多种信息源 | 覆盖面广，自动化程度高 |
| 🧠 **智能处理** | HTML解析、文本标准化、语义去重 | 内容质量高，减少冗余 |
| 💾 **灵活存储** | SQLite + JSON文件混合存储 | 结构化和非结构化兼顾 |
| ⏰ **定时任务** | 支持cron表达式的定时采集 | 自动化运行，无需人工干预 |
| 🚀 **高性能** | 异步架构，并发处理 | 采集效率高，系统响应快 |
| 📊 **监控告警** | 实时监控，健康检查 | 系统稳定，问题及时发现 |

---

## 快速开始

### 📋 系统要求

- **操作系统**：Linux (Ubuntu/Fedora)、macOS、Windows (WSL2)
- **Python 版本**：Python 3.13.x
- **内存要求**：最低 512MB，推荐 2GB+
- **存储空间**：最低 1GB 可用空间
- **网络要求**：稳定的互联网连接

### 🛠️ 安装步骤

#### 1. 环境准备

```bash
# 安装 Python 3.13 (如果尚未安装)
# Ubuntu/Debian
sudo apt update && sudo apt install python3.13 python3.13-venv

# CentOS/RHEL/Fedora
sudo dnf install python3.13 python3.13-pip

# macOS (使用 Homebrew)
brew install python@3.13

# 安装 uv (Python 包管理器)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### 2. 获取代码

```bash
# 克隆仓库
git clone https://github.com/JafeChang/Atlas.git
cd Atlas

# 或者下载发布版本
wget https://github.com/JafeChang/Atlas/releases/latest/download/atlas.tar.gz
tar -xzf atlas.tar.gz
cd atlas
```

#### 3. 安装依赖

```bash
# 使用 uv 安装依赖
uv sync

# 激活虚拟环境
source .venv/bin/activate  # Linux/macOS
# 或
.venv\Scripts\activate     # Windows
```

#### 4. 初始化配置

```bash
# 复制配置文件
cp config/config.yaml.example config/config.yaml
cp config/sources.yaml.example config/sources.yaml
cp .env.example .env

# 根据需要编辑配置文件
nano config/config.yaml
```

#### 5. 验证安装

```bash
# 运行基础测试
python -m atlas --help
python -m atlas status
```

### 🚀 第一次运行

```bash
# 1. 初始化数据库
python -m atlas init

# 2. 添加数据源
python -m atlas source add --name "tech-news" --type rss --url "https://feeds.bbci.co.uk/news/technology/rss.xml"

# 3. 运行一次性采集
python -m atlas collect --source tech-news

# 4. 查看采集结果
python -m atlas list --limit 10
```

---

## 核心功能

### 📡 数据采集

#### RSS 采集

```bash
# 添加RSS源
python -m atlas source add \
  --name "bbc-tech" \
  --type rss \
  --url "https://feeds.bbci.co.uk/news/technology/rss.xml" \
  --interval 3600

# 立即采集RSS源
python -m atlas collect --source bbc-tech

# 采集所有RSS源
python -m atlas collect --type rss
```

#### 网站采集

```bash
# 添加网站源
python -m atlas source add \
  --name "tech-blog" \
  --type website \
  --url "https://techblog.example.com" \
  --interval 7200 \
  --selectors 'title="h1"' 'content="article p"'

# 采集网站
python -m atlas collect --source tech-blog
```

#### 批量采集

```bash
# 采集所有激活的数据源
python -m atlas collect --all

# 按分类采集
python -m atlas collect --category technology

# 限制采集数量
python -m atlas collect --limit 100
```

### 📋 数据管理

#### 查看数据

```bash
# 查看最新数据（默认10条）
python -m atlas list

# 查看指定数量数据
python -m atlas list --limit 50

# 查看特定来源数据
python -m atlas list --source bbc-tech

# 查看特定时间范围数据
python -m atlas list --from "2025-01-01" --to "2025-01-31"

# 详细输出格式
python -m atlas list --format table
```

#### 搜索数据

```bash
# 按关键词搜索
python -m atlas search --query "人工智能"

# 按来源搜索
python -m atlas search --source tech-news

# 按分类搜索
python -m atlas search --category technology

# 组合搜索
python -m atlas search --query "AI" --source tech-blog --limit 20
```

#### 数据导出

```bash
# 导出为JSON格式
python -m atlas export --format json --output data.json

# 导出为CSV格式
python -m atlas export --format csv --output data.csv

# 按条件导出
python -m atlas export --source bbc-tech --from "2025-01-01" --output recent_news.json
```

### ⚙️ 定时任务

#### Cron 语法支持

```bash
# 启动定时任务服务
python -m atlas scheduler start

# 添加定时任务
python -m atlas scheduler add \
  --name "hourly-collect" \
  --cron "0 * * * *" \
  --command "python -m atlas collect --all"

# 预定义表达式
python -m atlas scheduler add \
  --name "daily-collect" \
  --cron "@daily" \
  --command "python -m atlas collect --all"

# 查看定时任务
python -m atlas scheduler list

# 删除定时任务
python -m atlas scheduler remove --name hourly-collect
```

#### 任务监控

```bash
# 查看任务状态
python -m atlas scheduler status

# 查看任务历史
python -m atlas scheduler history --limit 10

# 手动运行任务
python -m atlas scheduler run --name hourly-collect
```

---

## 配置管理

### 📄 主配置文件 (config/config.yaml)

```yaml
# 基础配置
env: "production"  # development/production
debug: false
log_level: "INFO"

# 数据目录
data_dir: "./data"
log_dir: "./logs"

# 数据库配置
database:
  url: "sqlite:///data/atlas.db"
  pool_size: 10
  max_overflow: 20

# 采集配置
collection:
  default_user_agent: "Atlas/1.0 (Data Collector)"
  request_timeout: 30
  max_concurrent_requests: 5
  rate_limit_delay: 1.0
  retry_attempts: 3

# 存储配置
storage:
  compression: true
  file_retention_days: 90
  index_enabled: true

# 去重配置
deduplication:
  strategy: "hybrid"  # hash/semantic/hybrid
  similarity_threshold: 0.8
  batch_size: 100

# LLM配置（可选）
llm:
  provider: "ollama"  # openai/anthropic/ollama
  model: "qwen2.5:7b"
  base_url: "http://localhost:11434"
  enable_semantic_dedup: false

# 监控配置
monitoring:
  enable_performance_monitoring: true
  enable_health_checks: true
  enable_alerts: false
  metrics_retention_days: 30
```

### 📡 数据源配置 (config/sources.yaml)

```yaml
sources:
  # RSS源示例
  - name: "techcrunch"
    type: "rss"
    url: "https://techcrunch.com/feed/"
    interval: 3600  # 每小时采集一次
    enabled: true
    tags: ["tech", "startup", "news"]
    category: "technology"

  - name: "hacker-news"
    type: "rss"
    url: "https://hnrss.org/frontpage"
    interval: 1800  # 每30分钟采集一次
    enabled: true
    tags: ["hacker", "news", "programming"]
    category: "technology"

  # 网站源示例
  - name: "example-blog"
    type: "website"
    url: "https://blog.example.com"
    interval: 7200  # 每2小时采集一次
    enabled: false  # 暂时禁用
    tags: ["blog", "personal"]
    category: "personal"
    selectors:
      title: "h1.title"
      content: "div.article-content"
      date: "time.published"
      author: "span.author"
```

### 🔧 环境变量 (.env)

```bash
# 数据库配置
DATABASE_URL=sqlite:///data/atlas.db

# LLM配置（如果使用）
OPENAI_API_KEY=your_openai_key_here
ANTHROPIC_API_KEY=your_anthropic_key_here

# 代理配置（如果需要）
HTTP_PROXY=http://proxy.example.com:8080
HTTPS_PROXY=http://proxy.example.com:8080

# 日志级别
LOG_LEVEL=INFO

# 其他配置
MAX_MEMORY_MB=2048
MAX_CONCURRENT_REQUESTS=5
```

---

## 命令行工具

### 🎯 核心命令

#### atlas collect
```bash
# 数据采集命令
python -m atlas collect [OPTIONS]

选项:
  --source TEXT        指定数据源名称
  --type TEXT          指定数据源类型 (rss/website)
  --category TEXT      指定数据源分类
  --limit INTEGER      限制采集数量
  --all                采集所有激活的数据源
  --dry-run            预览模式，不实际采集
```

#### atlas list
```bash
# 查看数据命令
python -m atlas list [OPTIONS]

选项:
  --source TEXT        过滤特定来源
  --category TEXT      过滤特定分类
  --from DATE          开始日期
  --to DATE            结束日期
  --limit INTEGER      限制显示数量
  --format TEXT        输出格式 (table/json)
```

#### atlas search
```bash
# 搜索数据命令
python -m atlas search [OPTIONS] QUERY

选项:
  --source TEXT        过滤特定来源
  --category TEXT      过滤特定分类
  --from DATE          开始日期
  --to DATE            结束日期
  --limit INTEGER      限制搜索结果数量
```

#### atlas source
```bash
# 数据源管理命令
python -m atlas source [SUBCOMMAND] [OPTIONS]

子命令:
  add                  添加新数据源
  remove               删除数据源
  list                 列出所有数据源
  enable               启用数据源
  disable              禁用数据源

示例:
  python -m atlas source add --name myrss --type rss --url http://example.com/rss.xml
  python -m atlas source list
  python -m atlas source disable --name myrss
```

#### atlas scheduler
```bash
# 定时任务管理命令
python -m atlas scheduler [SUBCOMMAND] [OPTIONS]

子命令:
  start                启动定时任务服务
  stop                 停止定时任务服务
  status               查看服务状态
  add                  添加定时任务
  remove               删除定时任务
  list                 列出所有定时任务
  history              查看任务执行历史
```

### 📊 输出格式

#### 表格格式
```bash
python -m atlas list --format table
```

输出示例：
```
┌──────────────────────────────┬─────────────────┬─────────────────┬─────────────────┐
│ 标题                           │ 来源            │ 分类            │ 创建时间        │
├──────────────────────────────┼─────────────────┼─────────────────┼─────────────────┤
│ AI技术的最新发展               │ techcrunch     │ technology     │ 2025-01-20     │
│ 机器学习实践指南               │ hacker-news    │ technology     │ 2025-01-20     │
└──────────────────────────────┴─────────────────┴─────────────────┴─────────────────┘
```

#### JSON格式
```bash
python -m atlas list --format json
```

输出示例：
```json
[
  {
    "id": "doc-123",
    "title": "AI技术的最新发展",
    "source": "techcrunch",
    "category": "technology",
    "content": "人工智能技术...",
    "created_at": "2025-01-20T10:30:00Z",
    "metadata": {
      "url": "https://techcrunch.com/ai-news",
      "tags": ["ai", "technology"]
    }
  }
]
```

---

## 高级功能

### 🤖 LLM集成（可选）

#### 启用语义去重

```yaml
# config/config.yaml
llm:
  provider: "ollama"
  model: "qwen2.5:7b"
  base_url: "http://localhost:11434"
  enable_semantic_dedup: true

deduplication:
  strategy: "hybrid"  # 启用语义去重
  similarity_threshold: 0.8
```

#### 配置Ollama

```bash
# 安装Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# 下载模型
ollama pull qwen2.5:7b

# 启动服务
ollama serve
```

### 📈 性能监控

#### 启用监控

```bash
# 启动监控服务
python -m atlas monitoring start

# 查看系统状态
python -m atlas monitoring status

# 查看性能指标
python -m atlas monitoring metrics
```

#### 监控指标

- **系统指标**：CPU使用率、内存使用率、磁盘空间
- **采集指标**：采集成功率、平均响应时间、错误率
- **存储指标**：数据库大小、文件数量、去重效果

### 🔒 安全配置

#### 访问控制

```yaml
# config/config.yaml
security:
  enable_authentication: false  # MVP版本暂不支持
  allowed_ips: []              # 允许的IP地址
  rate_limiting: true           # 启用频率限制
  request_timeout: 30          # 请求超时时间
```

#### 数据保护

```yaml
# 数据保留策略
storage:
  file_retention_days: 90      # 文件保留90天
  database_retention_days: 365  # 数据库保留365天
  backup_enabled: false         # 自动备份（MVP暂不支持）
  encryption_enabled: false     # 数据加密（MVP暂不支持）
```

---

## 常见问题

### ❓ 安装和配置

**Q: Python版本不兼容怎么办？**
A: Atlas需要Python 3.13。请使用`python3.13 --version`确认版本，如果版本不符，请升级Python或使用pyenv管理版本。

**Q: uv安装失败怎么办？**
A: 可以尝试以下替代方案：
```bash
# 使用pip安装（不推荐）
pip install -r requirements.txt

# 或使用conda
conda env create -f environment.yml
```

**Q: 配置文件在哪里？**
A: 主要配置文件位于：
- `config/config.yaml` - 主配置
- `config/sources.yaml` - 数据源配置
- `.env` - 环境变量

### ❓ 数据采集

**Q: RSS采集失败怎么办？**
A: 检查以下几点：
1. 网络连接是否正常
2. RSS URL是否有效
3. 请求频率是否过高（被限制）
4. User-Agent是否被屏蔽

**Q: 网站采集内容为空？**
A: 检查CSS选择器是否正确：
```bash
# 使用浏览器开发者工具检查元素
# 确保选择器能匹配到目标元素
python -m atlas collect --source your-source --dry-run
```

**Q: 采集速度很慢？**
A: 优化建议：
1. 增加`max_concurrent_requests`配置
2. 减少不必要的数据源
3. 检查网络延迟
4. 启用缓存机制

### ❓ 存储和管理

**Q: 数据库文件很大怎么办？**
A: 优化建议：
1. 定期清理旧数据
2. 启用数据压缩
3. 配置合适的数据保留策略
4. 考虑使用更大的存储空间

**Q: 如何备份数据？**
A: 手动备份方法：
```bash
# 备份数据库
cp data/atlas.db backup/atlas-$(date +%Y%m%d).db

# 备份JSON文件
tar -czf backup/data-$(date +%Y%m%d).tar.gz data/raw/ data/processed/

# 备份配置文件
tar -czf backup/config-$(date +%Y%m%d).tar.gz config/
```

### ❓ 性能问题

**Q: 内存使用过高？**
A: 优化建议：
1. 减少`max_concurrent_requests`
2. 启用数据压缩
3. 定期重启服务
4. 监控内存使用情况

**Q: CPU使用率100%？**
A: 可能原因和解决方案：
1. 数据采集任务过于频繁 - 调整采集间隔
2. 并发处理数过多 - 减少并发数
3. 大量文本处理 - 优化处理逻辑

### ❓ 错误排查

**Q: 如何查看详细错误信息？**
A: 检查日志文件：
```bash
# 查看最新日志
tail -f logs/atlas.log

# 查看错误日志
grep "ERROR" logs/atlas.log

# 启用调试模式
export LOG_LEVEL=DEBUG
python -m atlas your-command
```

**Q: 服务无法启动？**
A: 检查步骤：
1. 查看错误日志
2. 确认端口没有被占用
3. 检查权限设置
4. 验证配置文件语法

---

## 最佳实践

### 🎯 数据源管理

#### 1. 合理设置采集频率

```yaml
# 推荐的采集间隔
sources:
  # 新闻类：更新频繁，短间隔
  - name: "breaking-news"
    interval: 1800  # 30分钟

  # 技术博客：更新较少，长间隔
  - name: "tech-blog"
    interval: 7200  # 2小时

  # 官方发布：更新很少，长间隔
  - name: "official-blog"
    interval: 86400  # 24小时
```

#### 2. 分类和标签管理

```yaml
# 建议的分类体系
categories:
  - "technology"    # 技术类
  - "business"      # 商业类
  - "science"       # 科学类
  - "news"          # 新闻类
  - "personal"      # 个人类

# 统一的标签体系
tags:
  - "ai"           # 人工智能
  - "machine-learning"  # 机器学习
  - "startup"      # 初创公司
  - "programming"  # 编程
  - "security"     # 安全
```

### ⚡ 性能优化

#### 1. 并发配置优化

```yaml
collection:
  # 根据系统性能调整
  max_concurrent_requests: 3   # 低配置系统
  max_concurrent_requests: 5   # 中等配置系统
  max_concurrent_requests: 10  # 高配置系统

  # 请求间隔
  rate_limit_delay: 1.0        # 保守设置
  rate_limit_delay: 0.5        # 正常设置
  rate_limit_delay: 0.1        # 激进设置
```

#### 2. 存储优化

```yaml
storage:
  # 启用压缩节省空间
  compression: true

  # 定期清理策略
  file_retention_days: 90
  database_retention_days: 365

  # 启用索引提高查询性能
  index_enabled: true
```

### 📊 数据管理

#### 1. 数据质量监控

```bash
# 定期检查数据质量
python -m atlas stats --quality

# 检查重复数据
python -m atlas dedup --check

# 数据统计报告
python -m atlas report --type weekly
```

#### 2. 数据备份策略

```bash
#!/bin/bash
# backup.sh - 自动备份脚本

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="backups/$DATE"

mkdir -p $BACKUP_DIR

# 备份配置文件
cp config/ $BACKUP_DIR/ -r

# 备份数据库
sqlite3 data/atlas.db ".backup $BACKUP_DIR/atlas.db"

# 备份重要数据
cp data/ $BACKUP_DIR/ -r

# 压缩备份
tar -czf "atlas_backup_$DATE.tar.gz" $BACKUP_DIR
rm -rf $BACKUP_DIR

echo "备份完成: atlas_backup_$DATE.tar.gz"
```

### 🔒 安全实践

#### 1. 访问控制

```yaml
# 限制访问来源
collection:
  allowed_user_agents: ["Atlas/*"]
  blocked_ips: ["192.168.1.100"]

# 请求限制
security:
  rate_limiting: true
  max_requests_per_minute: 60
  block_suspicious_ips: true
```

#### 2. 数据保护

```bash
# 设置合适的文件权限
chmod 600 .env
chmod 644 config/*.yaml
chmod 755 data/

# 定期清理敏感信息
find logs/ -name "*.log" -mtime +30 -delete
find data/ -name "*.tmp" -mtime +1 -delete
```

### 📝 运维建议

#### 1. 监控和告警

```bash
# 定期检查系统状态
python -m atlas monitoring status

# 设置监控脚本
#!/bin/bash
# monitor.sh

# 检查进程
if ! pgrep -f "atlas"; then
  echo "警告: Atlas进程未运行"
  # 发送告警通知
fi

# 检查磁盘空间
DISK_USAGE=$(df /data | tail -1 | awk '{print $5}' | sed 's/%//')
if [ $DISK_USAGE -gt 80 ]; then
  echo "警告: 磁盘使用率过高: $DISK_USAGE%"
fi

# 检查内存使用
MEMORY_USAGE=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100.0}')
if [ $MEMORY_USAGE -gt 90 ]; then
  echo "警告: 内存使用率过高: $MEMORY_USAGE%"
fi
```

#### 2. 日志管理

```bash
# 日志轮转配置
# /etc/logrotate.d/atlas
/path/to/atlas/logs/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 644 atlas atlas
}
```

---

## 📞 支持和反馈

### 🐛 问题报告

如果遇到问题，请提供以下信息：

1. **系统信息**
   - 操作系统版本
   - Python版本
   - Atlas版本

2. **问题描述**
   - 错误现象
   - 复现步骤
   - 期望结果

3. **环境信息**
   - 配置文件内容
   - 错误日志
   - 系统资源使用情况

### 💬 技术支持

- **GitHub Issues**: https://github.com/JafeChang/Atlas/issues
- **文档站点**: https://github.com/JafeChang/Atlas/docs
- **讨论区**: https://github.com/JafeChang/Atlas/discussions

---

## 文档状态

- **当前版本**: v1.0.0
- **最后更新**: 2025-01-20
- **下次更新**: 根据用户反馈和功能更新
- **维护状态**: 活跃维护
- **质量评级**: ⭐⭐⭐⭐⭐

---

*本文档遵循 [Atlas 文档体系规范](../documentation-system.md)*