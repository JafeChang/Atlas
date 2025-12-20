---
version: "1.0.0"
last_updated: "2025-01-20"
updated_by: "Claude Sonnet"
document_type: "mutable"
category: "configuration_guide"
status: "active"
tags: ["configuration", "atlas", "env", "llm", "security"]
reviewer: "用户"
---

# Atlas 配置管理指南

> Atlas 系统配置管理和环境变量使用指南

---

## 文档信息

- **创建时间**：2025-01-20
- **文档类型**：可变更文档
- **系统版本**：Atlas v0.1.0 MVP
- **配置层级**：开发/生产环境分离
- **安全级别**：敏感配置分离

---

## 📖 目录

- [配置文件架构](#配置文件架构)
- [环境配置文件](#环境配置文件)
- [配置加载优先级](#配置加载优先级)
- [LLM配置管理](#llm配置管理)
- [生产环境配置](#生产环境配置)
- [安全最佳实践](#安全最佳实践)
- [故障排除](#故障排除)

---

## 配置文件架构

Atlas 采用多层级配置架构，确保敏感信息安全并支持不同环境的配置需求：

```
Atlas配置架构
├── .env                    # 基础非敏感配置 (Git追踪)
├── .env.local              # 本地敏感配置 (Git忽略) ⭐
├── .env.development        # 开发环境配置 (Git忽略)
├── .env.test               # 测试环境配置 (Git忽略)
├── .env.production         # 生产环境配置 (Git忽略)
├── config/config.yaml      # 主配置文件
├── config/sources.yaml     # 数据源配置
└── config/.env             # 配置目录环境变量 (Git忽略)
```

---

## 环境配置文件

### 📁 .env (基础配置)

**用途**: 包含非敏感的默认配置，会被Git追踪
**范围**: 所有开发者和部署环境共享的基础设置

```bash
# Atlas 项目基础环境配置文件
# 仅包含非敏感的默认配置，敏感配置请使用 .env.local

# 基础环境配置
ATLAS_ENV=development
ATLAS_DEBUG=false
ATLAS_LOG_LEVEL=INFO

# 数据存储路径配置
ATLAS_DATA_DIR=./data
ATLAS_CONFIG_DIR=./config
ATLAS_LOG_DIR=./logs
```

### 🔒 .env.local (本地敏感配置) ⭐

**用途**: 包含所有敏感配置，**不会被Git追踪**
**范围**: 仅限当前开发者本地环境
**关键**: 必须在`.gitignore`中，防止敏感信息泄露

```bash
# Atlas 本地开发环境敏感配置
# 此文件包含敏感信息，不会被Git追踪
# 请根据实际情况修改配置

# LLM 配置 - 敏感信息
ATLAS_LLM_PROVIDER=local
ATLAS_LLM_MODEL=qwen2.5:7b
ATLAS_LLM_BASE_URL=http://localhost:11434
ATLAS_OPENAI_API_KEY=your-openai-api-key-here
ATLAS_ANTHROPIC_API_KEY=your-anthropic-api-key-here

# 代理配置 (如果需要)
# HTTP_PROXY=http://proxy.example.com:8080
# HTTPS_PROXY=http://proxy.example.com:8080

# 开发配置
ATLAS_DEBUG=true
ATLAS_RELOAD=true
```

### 🏗️ 环境特定配置文件

#### 开发环境 (.env.development)
```bash
# 开发环境特定配置
ATLAS_ENV=development
ATLAS_DEBUG=true
ATLAS_LOG_LEVEL=DEBUG
ATLAS_RELOAD=true
ATLAS_MONITORING_ENABLED=false
```

#### 测试环境 (.env.test)
```bash
# 测试环境配置
ATLAS_ENV=test
ATLAS_DEBUG=false
ATLAS_LOG_LEVEL=WARNING
ATLAS_DATABASE_URL=sqlite:///:memory:
ATLAS_LLM_PROVIDER=mock  # 使用模拟LLM
```

#### 生产环境 (.env.production)
```bash
# 生产环境配置
ATLAS_ENV=production
ATLAS_DEBUG=false
ATLAS_LOG_LEVEL=INFO
ATLAS_MONITORING_ENABLED=true
ATLAS_RATE_LIMIT_DELAY=1000
```

---

## 配置加载优先级

Atlas 按以下优先级加载配置 (高优先级覆盖低优先级):

1. **环境变量** (最高优先级)
2. **.env.local** (本地敏感配置)
3. **.env.{environment}** (环境特定配置)
4. **.env** (基础配置)
5. **config.yaml** (默认配置)

```python
# Atlas 配置加载顺序
1. os.environ (系统环境变量)
2. load_dotenv('.env.local', override=True)
3. load_dotenv('.env.production', override=True)  # 如果是生产环境
4. load_dotenv('.env', override=False)
5. config.yaml (默认值)
```

---

## LLM配置管理

### 🔒 敏感配置原则

LLM配置包含敏感信息，**必须**放在`.env.local`中：

```bash
# ✅ 正确：放在 .env.local
ATLAS_LLM_PROVIDER=local
ATLAS_LLM_MODEL=qwen2.5:7b
ATLAS_LLM_BASE_URL=http://localhost:11434
ATLAS_OPENAI_API_KEY=sk-...
ATLAS_ANTHROPIC_API_KEY=sk-ant-...

# ❌ 错误：不要放在 .env 或 config.yaml 中
# 这些文件会被Git追踪，会导致敏感信息泄露
```

### 🏗️ 不同LLM提供商配置

#### 本地LLM (Ollama)
```bash
# .env.local
ATLAS_LLM_PROVIDER=local
ATLAS_LLM_MODEL=qwen2.5:7b
ATLAS_LLM_BASE_URL=http://localhost:11434
ATLAS_LLM_TIMEOUT=60
ATLAS_LLM_MAX_WORKERS=1
```

#### OpenAI
```bash
# .env.local
ATLAS_LLM_PROVIDER=openai
ATLAS_OPENAI_API_KEY=sk-proj-...
ATLAS_OPENAI_BASE_URL=https://api.openai.com/v1
ATLAS_OPENAI_MODEL=gpt-4
```

#### Anthropic Claude
```bash
# .env.local
ATLAS_LLM_PROVIDER=anthropic
ATLAS_ANTHROPIC_API_KEY=sk-ant-...
ATLAS_ANTHROPIC_MODEL=claude-3-sonnet-20240229
```

### 🔄 配置切换示例

```python
# 快速切换LLM配置的开发技巧
import os
from dotenv import load_dotenv

# 切换到OpenAI
os.environ['ATLAS_LLM_PROVIDER'] = 'openai'
os.environ['ATLAS_OPENAI_API_KEY'] = 'your-key-here'

# 切换到本地模型
os.environ['ATLAS_LLM_PROVIDER'] = 'local'
os.environ['ATLAS_LLM_MODEL'] = 'llama2:13b'
```

---

## 生产环境配置

### 🔧 生产环境配置步骤

1. **创建生产环境配置文件**
```bash
# 复制模板
cp .env.example .env.production
```

2. **配置生产环境变量**
```bash
# .env.production
ATLAS_ENV=production
ATLAS_DEBUG=false
ATLAS_LOG_LEVEL=INFO

# 生产LLM配置
ATLAS_LLM_PROVIDER=openai  # 或其他稳定提供商
ATLAS_OPENAI_API_KEY=sk-proj-...

# 安全配置
ATLAS_CORS_ORIGINS=["https://yourdomain.com"]
ATLAS_MAX_REQUEST_SIZE=5242880  # 5MB

# 性能配置
ATLAS_MAX_CONCURRENT_REQUESTS=10
ATLAS_RATE_LIMIT_DELAY=1000
```

3. **部署时加载正确配置**
```bash
# 部署脚本示例
export ATLAS_ENV=production
export ATLAS_LLM_PROVIDER=openai
export ATLAS_OPENAI_API_KEY=${OPENAI_API_KEY}

# 或使用配置文件
source .env.production
python -m atlas init
```

### 🚀 Docker生产配置

```dockerfile
# Dockerfile
FROM python:3.13-slim

# 创建非root用户
RUN groupadd -r atlas && useradd -r -g atlas atlas

# 复制应用代码
COPY . /app
WORKDIR /app

# 安装依赖
RUN pip install uv && uv sync

# 设置权限
RUN chown -R atlas:atlas /app
USER atlas

# 默认使用生产环境
ENV ATLAS_ENV=production

CMD ["python", "-m", "atlas", "init"]
```

```yaml
# docker-compose.yml
services:
  atlas:
    build: .
    environment:
      - ATLAS_ENV=production
      - ATLAS_LLM_PROVIDER=openai
      - ATLAS_OPENAI_API_KEY=${OPENAI_API_KEY}
      - ATLAS_LLM_MODEL=gpt-4
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
```

---

## 安全最佳实践

### 🔒 敏感信息保护

1. **永远不要提交敏感配置**
```bash
# ✅ .gitignore 已正确配置
.env.local
.env.production
.env.development
*.key
*.pem
secrets/
```

2. **使用环境变量模板**
```bash
# .env.example - 提供配置模板，不包含真实密钥
ATLAS_OPENAI_API_KEY=your-openai-api-key-here
ATLAS_ANTHROPIC_API_KEY=your-anthropic-api-key-here
```

3. **生产环境使用密钥管理**
```bash
# 使用环境变量或密钥管理服务
export ATLAS_OPENAI_API_KEY=$(aws secretsmanager get-secret-value --secret-id atlas-openai-key --query SecretString --output text)
```

### 🛡️ 配置验证

```python
# config_validator.py - 配置验证脚本
import os
from dotenv import load_dotenv

def validate_sensitive_config():
    """验证敏感配置是否安全"""

    # 检查是否有示例密钥
    api_keys = [
        'ATLAS_OPENAI_API_KEY',
        'ATLAS_ANTHROPIC_API_KEY'
    ]

    for key in api_keys:
        value = os.getenv(key, '')
        if 'your-' in value or 'example' in value:
            print(f"⚠️ 警告: {key} 可能包含示例值，请设置真实密钥")
        elif not value:
            print(f"ℹ️ 信息: {key} 未设置")
        else:
            print(f"✅ {key} 已正确设置")

def validate_llm_config():
    """验证LLM配置完整性"""

    provider = os.getenv('ATLAS_LLM_PROVIDER')

    if provider == 'openai':
        if not os.getenv('ATLAS_OPENAI_API_KEY'):
            print("❌ 错误: OpenAI提供商需要API密钥")
        else:
            print("✅ OpenAI配置完整")

    elif provider == 'anthropic':
        if not os.getenv('ATLAS_ANTHROPIC_API_KEY'):
            print("❌ 错误: Anthropic提供商需要API密钥")
        else:
            print("✅ Anthropic配置完整")

    elif provider == 'local':
        base_url = os.getenv('ATLAS_LLM_BASE_URL', 'http://localhost:11434')
        print(f"✅ 本地LLM配置: {base_url}")

if __name__ == "__main__":
    load_dotenv('.env.local', override=True)
    validate_sensitive_config()
    validate_llm_config()
```

---

## 故障排除

### ❓ 常见配置问题

#### 问题1: 配置未生效
```bash
# 诊断步骤
1. 检查配置文件是否存在
   ls -la .env*

2. 检查环境变量加载
   python -c "import os; print('ATLAS_LLM_PROVIDER:', os.getenv('ATLAS_LLM_PROVIDER'))"

3. 验证加载顺序
   python -c "from dotenv import load_dotenv; load_dotenv('.env.local'); print('Loaded .env.local')"
```

#### 问题2: 敏感信息被Git追踪
```bash
# 解决方案
1. 立即从历史记录移除敏感信息
   git filter-branch --force --index-filter 'git rm --cached --ignore-unmatch .env.local' --prune-empty --tag-name-filter cat -- --all

2. 确保文件在.gitignore中
   grep ".env.local" .gitignore

3. 提交修复
   git add .gitignore
   git commit -m "Remove sensitive config from version control"
```

#### 问题3: 生产环境配置错误
```bash
# 诊断脚本
python -c "
import os
from dotenv import load_dotenv

load_dotenv('.env.production')

print('生产环境配置诊断:')
print(f'环境: {os.getenv(\"ATLAS_ENV\")}')
print(f'调试模式: {os.getenv(\"ATLAS_DEBUG\")}')
print(f'LLM提供商: {os.getenv(\"ATLAS_LLM_PROVIDER\")}')
print(f'API密钥已设置: {\"ATLAS_OPENAI_API_KEY\" in os.environ}')
"
```

### 🔧 配置重置

```bash
# 重置到默认配置
python -c "
from atlas.core.config import get_config
config = get_config()
print('默认配置路径:', config.config_dir)
print('数据目录:', config.data_dir)
"
```

---

## 📚 相关资源

### 📖 相关文档
- [用户手册](user-manual.md)
- [部署指南](deployment-guide.md)
- [故障排除指南](troubleshooting.md)

### 🔧 配置脚本
- [配置验证脚本](../../scripts/validate_config.py)
- [环境设置脚本](../../scripts/setup_env.py)

---

## 文档状态

- **当前版本**: v1.0.0
- **最后更新**: 2025-01-20
- **安全级别**: 高 (敏感配置分离)
- **维护状态**: 活跃维护
- **质量评级**: ⭐⭐⭐⭐⭐

---

*本文档遵循 [Atlas 文档体系规范](../documentation-system.md)*