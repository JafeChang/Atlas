# 2025-01-17 环境变量配置增强

**日期**：2025-01-17
**开发者**：Claude Sonnet
**更新类型**：功能增强
**任务关联**：TASK-001 配置完善

---

## 📋 更新内容

### ✅ 新增本地环境变量支持

基于用户需求，增强了 Atlas 配置系统，现在支持本地环境变量配置：

#### 🏗️ 环境变量加载优先级

1. **系统环境变量** (最高优先级)
2. **项目根目录 `.env`** (全局默认配置)
3. **配置目录 `.env.{环境名}`** (环境特定配置)
4. **配置目录 `.env.local`** (本地覆盖配置)
5. **配置目录 `.env`** (默认配置)

#### 📁 创建的环境文件

1. **项目根目录 `.env`**
   - 全局环境变量设置
   - 基础配置：`ATLAS_ENV=development`
   - 数据目录和日志目录配置

2. **配置目录 `.env.development`**
   - 开发环境特定配置
   - 调试模式启用：`ATLAS_DEBUG=true`
   - 日志级别：`ATLAS_LOG_LEVEL=DEBUG`
   - 开发友好的数据库配置

3. **配置目录 `.env.local`**
   - 本地开发配置模板
   - 包含各类 LLM 配置示例
   - 提供详细的配置说明
   - 支持本地自定义覆盖

#### 🔧 技术实现

##### LocalEnvLoader 类
```python
class LocalEnvLoader:
    """本地环境变量加载器"""

    def load_env_files(self) -> None:
        """按优先级加载环境变量文件"""
        env_files = self._get_env_file_paths()

        for env_file in env_files:
            if env_file.exists():
                load_dotenv(env_file, override=True)
```

##### 环境自动检测
- 从系统环境变量 `ATLAS_ENV` 读取
- 从项目根目录 `.env` 文件读取
- 默认为 `development` 环境

##### 配置深度合并
- 支持环境特定 YAML 配置文件
- 深度合并配置，不覆盖基础配置
- 格式：`config.{env_name}.yaml`

---

## 🎯 新增功能特性

### 📊 环境信息查看
新增 CLI 命令显示环境配置信息：
```bash
atlas status
```

输出示例：
```
🔧 环境配置:
  当前环境: development
  加载的配置文件:
    - /path/to/.env
    - /path/to/config/.env.development
    - /path/to/config/.env
```

### 🔒 安全性保障
- `.env.local` 已添加到 `.gitignore`
- 敏感配置不会提交到版本控制
- 提供清晰的配置模板和示例

### 🚀 开发体验优化
- 支持不同环境的独立配置
- 本地开发配置与生产配置分离
- 便于团队协作和个人定制

---

## 📁 文件结构

```
atlas/
├── .env                          # 项目根目录全局配置
├── .gitignore                     # 更新：忽略本地配置文件
├── src/atlas/core/config.py       # 增强：本地环境变量支持
├── src/atlas/cli.py              # 增强：环境信息显示
└── config/
    ├── .env.development          # 开发环境配置
    └── .env.local                # 本地配置模板
```

---

## 🔧 配置示例

### 开发环境配置
```bash
# .env.development
ATLAS_DEBUG=true
ATLAS_LOG_LEVEL=DEBUG
ATLAS_DATABASE_ECHO=true
ATLAS_RATE_LIMIT_DELAY=60
```

### 本地 LLM 配置
```bash
# .env.local (用户自定义)
ATLAS_LLM_PROVIDER=local
ATLAS_LLM_MODEL=qwen2.5:7b
ATLAS_LLM_BASE_URL=http://localhost:11434
```

### OpenAI API 配置
```bash
# .env.local (用户自定义)
ATLAS_LLM_PROVIDER=openai
ATLAS_LLM_OPENAI_API_KEY=your-api-key
ATLAS_LLM_OPENAI_MODEL=gpt-4
```

---

## 🎯 使用指南

### 1. 环境切换
```python
# 指定环境加载配置
from atlas.core.config import get_config

config = get_config(env_name="production")
```

### 2. 本地配置
1. 复制 `config/.env.local` 为 `config/.env.local`
2. 根据本地环境修改配置
3. 重启应用，配置自动生效

### 3. 环境特定配置
1. 创建 `config/.env.{环境名}` 文件
2. 添加环境特定配置
3. 通过 `ATLAS_ENV=环境名` 启动

---

## 📈 优势特性

### ✅ 灵活性
- 支持多环境配置
- 本地配置与团队配置分离
- 配置优先级清晰

### ✅ 安全性
- 敏感信息本地化
- 版本控制友好
- 配置模板化

### ✅ 易用性
- 自动环境检测
- 详细的配置示例
- CLI 状态查看

### ✅ 可维护性
- 清晰的加载顺序
- 配置文件命名规范
- 完整的文档说明

---

## 💡 最佳实践建议

### 🎯 开发团队
1. **团队配置**: 使用 `.env.development` 存放开发环境共享配置
2. **个人配置**: 每个开发者使用 `.env.local` 存储个人配置
3. **生产配置**: 使用 `.env.production` 存放生产环境配置

### 🔒 安全考虑
1. **敏感信息**: 只在 `.env.local` 中存储 API 密钥
2. **版本控制**: `.env.local` 不要提交到版本控制
3. **配置备份**: 重要配置提供模板和说明

### 🚀 部署流程
1. **环境变量**: 通过环境变量或 `.env.{环境名}` 配置
2. **配置验证**: 使用 `atlas check` 验证配置
3. **配置监控**: 使用 `atlas status` 查看当前配置

---

## 🔮 后续规划

### 短期优化
- [ ] 配置验证和错误提示
- [ ] 配置热重载功能
- [ ] 更多环境配置模板

### 中期扩展
- [ ] 配置文件加密支持
- [ ] 远程配置服务集成
- [ ] 配置变更追踪

### 长期演进
- [ ] 可视化配置管理界面
- [ ] 配置版本管理
- [ ] 智能配置推荐

---

## 📝 配置文档

- **主配置**: `config/config.yaml`
- **数据源**: `config/sources.yaml`
- **环境配置**: `.env`, `.env.development`, `.env.local`

---

## 💭 备注

本次配置增强大幅提升了 Atlas 的环境适应性和开发体验。通过分层的环境变量加载机制，既保证了配置的灵活性，又维护了安全性。特别是 `.env.local` 的引入，使得开发者可以在不影响团队配置的情况下进行个性化配置。

配置系统现在完全支持从开发到生产的多环境需求，为 Atlas 的长期发展和团队协作奠定了坚实基础。

---

*配置更新遵循 [Atlas 文档体系规范](../documentation-system.md)*