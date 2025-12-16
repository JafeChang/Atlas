---
version: "1.0.0"
last_updated: "2025-01-17"
updated_by: "Claude Sonnet"
document_type: "mutable"
category: "usage_guide"
status: "draft"
tags: ["installation", "setup", "quick-start"]
reviewer: "用户"
---

# Atlas 安装指南

> 指导用户快速安装和配置 Atlas 个人信息聚合系统

---

## 文档信息

- **创建时间**：2025-01-17
- **创建人**：Claude Sonnet
- **文档类型**：可变更文档
- **适用版本**：Atlas v0.1.0 (MVP)
- **阅读时长**：约 15 分钟

---

## 目录

- [系统要求](#系统要求)
- [安装步骤](#安装步骤)
- [配置说明](#配置说明)
- [验证安装](#验证安装)
- [常见问题](#常见问题)

---

## 系统要求

### 硬件要求

#### 最小配置
- **CPU**：4 核心
- **内存**：8GB RAM
- **存储**：256GB SSD
- **网络**：稳定的互联网连接

#### 推荐配置
- **CPU**：8 核心
- **内存**：16GB RAM
- **存储**：500GB SSD
- **网络**：高速互联网连接

### 软件要求

#### 操作系统
- **Linux**：Ubuntu 20.04+ / Fedora 38+
- **WSL**：Windows Subsystem for Linux 2
- **macOS**：12.0+ (通过 Docker)

#### 必需软件
- **Python**：3.13.x
- **uv**：最新版本
- **Git**：2.30+
- **SQLite**：3.45+

#### 可选软件
- **Ollama**：本地 LLM 支持
- **Docker**：容器化部署

---

## 安装步骤

### 步骤 1：环境准备

#### 1.1 安装 Python 3.13

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.13 python3.13-venv python3.13-dev

# Fedora
sudo dnf install python3.13 python3.13-pip python3.13-devel

# 验证安装
python3.13 --version
```

#### 1.2 安装 uv

```bash
# 使用官方安装脚本
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或者使用 pip
pip install uv

# 验证安装
uv --version
```

#### 1.3 克隆项目

```bash
# 克隆仓库
git clone https://github.com/your-username/atlas.git
cd atlas

# 检出正确的分支
git checkout feature/do_project
```

### 步骤 2：依赖安装

#### 2.1 创建虚拟环境

```bash
# 使用 uv 创建虚拟环境
uv venv

# 激活虚拟环境
source .venv/bin/activate  # Linux/macOS
# 或
.venv\Scripts\activate     # Windows
```

#### 2.2 安装项目依赖

```bash
# 安装所有依赖
uv pip install -e .

# 验证安装
uv pip list
```

### 步骤 3：配置设置

#### 3.1 创建配置文件

```bash
# 复制配置模板
cp config/config.yaml.example config/config.yaml

# 创建环境变量文件
cp .env.example .env
```

#### 3.2 编辑配置文件

```yaml
# config/config.yaml
database:
  path: "data/atlas.db"

storage:
  raw_dir: "data/raw"
  processed_dir: "data/processed"

collection:
  default_interval: 300  # 5分钟

llm:
  provider: "local"  # 或 "openai", "anthropic"
  model: "qwen2.5:7b"

logging:
  level: "INFO"
  dir: "logs"
```

```bash
# .env
ATLAS_ENV=development
ATLAS_LOG_LEVEL=INFO
ATLAS_DATA_DIR=./data
```

### 步骤 4：初始化数据库

```bash
# 运行数据库初始化
python -m atlas.core.database init

# 验证数据库
python -m atlas.core.database check
```

### 步骤 5：配置数据源

```bash
# 编辑数据源配置
nano config/sources.yaml
```

```yaml
# sources.yaml 示例
sources:
  - name: "example-news"
    type: "rss"
    url: "https://example.com/rss.xml"
    interval: 3600  # 1小时
    enabled: true

  - name: "tech-blog"
    type: "web"
    url: "https://tech-blog.com"
    interval: 1800  # 30分钟
    selectors:
      title: "h1.article-title"
      content: "div.article-content"
    enabled: true
```

---

## 配置说明

### 核心配置项

| 配置项 | 说明 | 默认值 | 必需 |
|--------|------|--------|------|
| `database.path` | SQLite 数据库路径 | `data/atlas.db` | ✅ |
| `storage.raw_dir` | 原始数据目录 | `data/raw` | ✅ |
| `storage.processed_dir` | 处理后数据目录 | `data/processed` | ✅ |
| `collection.default_interval` | 默认采集间隔 | 300 | ✅ |
| `llm.provider` | LLM 提供商 | `local` | ✅ |
| `logging.level` | 日志级别 | `INFO` | ✅ |

### LLM 配置

#### 本地 LLM (Ollama)
```yaml
llm:
  provider: "local"
  model: "qwen2.5:7b"
  base_url: "http://localhost:11434"
  timeout: 60
```

#### OpenAI API
```yaml
llm:
  provider: "openai"
  model: "gpt-4"
  api_key: "${OPENAI_API_KEY}"
  base_url: "https://api.openai.com/v1"
```

### 采集配置

```yaml
collection:
  user_agent: "Atlas/0.1.0 (Information Aggregation)"
  max_concurrent: 3
  retry_attempts: 3
  timeout: 30
```

---

## 验证安装

### 1. 运行健康检查

```bash
# 运行系统检查
python -m atlas.cli check

# 预期输出
✅ Database connection: OK
✅ Storage directories: OK
✅ Configuration: OK
✅ LLM connection: OK
```

### 2. 测试数据采集

```bash
# 运行测试采集
python -m atlas.cli collect --source example-news --dry-run

# 检查结果
python -m atlas.cli status
```

### 3. 验证 LLM 功能

```bash
# 测试 LLM 连接
python -m atlas.llm.local_client test

# 测试去重功能
python -m atlas.processors.dedup test
```

### 4. 查看 Web 界面（如果启用）

```bash
# 启动 Web 服务
python -m atlas.web serve

# 访问 http://localhost:8000
```

---

## 常见问题

### 安装问题

#### 问题 1：Python 版本不兼容
**错误**：`Python 3.13 is required`
**解决方案**：
```bash
# 安装 pyenv
curl https://pyenv.run | bash

# 安装 Python 3.13
pyenv install 3.13.0
pyenv global 3.13.0
```

#### 问题 2：uv 安装失败
**错误**：`command not found: uv`
**解决方案**：
```bash
# 手动安装
pip install uv

# 添加到 PATH
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

#### 问题 3：依赖安装失败
**错误**：`Failed building wheel for package`
**解决方案**：
```bash
# 安装系统依赖
sudo apt install build-essential python3-dev

# 使用国内镜像
uv pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple/
```

### 运行问题

#### 问题 4：数据库连接失败
**错误**：`sqlite3.OperationalError: unable to open database file`
**解决方案**：
```bash
# 创建数据目录
mkdir -p data
chmod 755 data

# 检查权限
ls -la data/
```

#### 问题 5：LLM 连接超时
**错误**：`Connection timeout to LLM service`
**解决方案**：
```bash
# 检查 Ollama 服务
ollama list

# 重启 Ollama
systemctl restart ollama

# 或者安装 Ollama
curl -fsSL https://ollama.ai/install.sh | sh
```

#### 问题 6：数据采集失败
**错误**：`HTTP 403 Forbidden`
**解决方案**：
1. 检查 User-Agent 设置
2. 降低采集频率
3. 验证目标网站可访问性

---

## 故障排除

### 日志查看

```bash
# 查看应用日志
tail -f logs/atlas.log

# 查看错误日志
tail -f logs/errors.log

# 查看采集日志
tail -f logs/collection.log
```

### 性能监控

```bash
# 检查资源使用
python -m atlas.cli monitor

# 数据库状态
python -m atlas.core.database stats
```

### 重置系统

```bash
# 清理数据（谨慎使用）
python -m atlas.cli reset --confirm

# 重新初始化
python -m atlas.core.database init --force
```

---

## 下一步

安装完成后，建议：

1. 阅读 [用户手册](user-manual.md)
2. 配置你的第一个数据源
3. 设置定时采集任务
4. 了解 [开发者指南](developer-guide.md)

---

## 获取帮助

- **文档**：查看 [完整文档](../README.md)
- **社区**：[GitHub Discussions](链接)
- **问题报告**：[GitHub Issues](链接)

---

## 变更日志

### v1.0.0 (2025-01-17)
**更新人**：Claude Sonnet

**新增功能**：
- 完整的安装流程
- 配置文件说明
- 验证步骤

**改进优化**：
- 故障排除指南
- 常见问题解答

**影响范围**：
- 新用户安装体验
- 系统部署流程

**注意事项**：
- 需要Python 3.13
- 建议使用uv管理依赖

---

## 文档状态

- **当前版本**：v1.0.0
- **下次更新计划**：根据用户反馈
- **维护状态**：活跃维护
- **质量评级**：⭐⭐⭐⭐⭐

---

*本文档遵循 [Atlas 文档体系规范](../documentation-system.md)*