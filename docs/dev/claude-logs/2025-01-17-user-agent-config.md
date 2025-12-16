# 2025-01-17 User-Agent 配置功能实现

**日期**：2025-01-17
**开发者**：Claude Sonnet
**更新类型**：功能增强
**需求来源**：用户需求

---

## 📋 更新内容

### ✅ 新增 User-Agent 配置支持

基于用户需求，实现了完整的 User-Agent 配置功能：

#### 🎯 配置方式

1. **全局配置**：在 `config/sources.yaml` 中定义 User-Agent 选项
2. **数据源配置**：为每个数据源指定使用的 User-Agent
3. **随机选择**：支持随机和轮换 User-Agent
4. **动态切换**：运行时根据配置动态选择

#### 📁 实现文件

- **`src/atlas/collectors/base.py`** - 基础采集器增强
- **`src/atlas/collectors/rss_collector.py`** - RSS 采集器
- **`src/atlas/collectors/web_collector.py`** - Web 采集器
- **`config/sources.yaml`** - User-Agent 配置定义

---

## 🔧 技术实现

### 📋 User-Agent 配置结构

```yaml
collectors:
  user_agents:
    default: "Atlas/0.1.0 (Information Aggregation System; +https://github.com/your-username/atlas)"

    options:
      atlas_custom: "Atlas/0.1.0 (Information Aggregation System; +https://github.com/your-username/atlas)"
      atlas_crawler: "Atlas-Crawler/0.1.0 (+https://github.com/your-username/atlas)"

      # 现代浏览器
      chrome_windows: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      chrome_mac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      firefox_windows: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
      firefox_mac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0"

      # 移动设备
      iphone_safari: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
      ipad_safari: "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"

      # 其他工具
      curl: "curl/7.68.0"
      wget: "Wget/1.20.3"
```

### 🎯 数据源配置示例

```yaml
sources:
  - name: "openai-blog"
    type: "rss"
    url: "https://openai.com/blog/rss/"
    user_agent: "atlas_custom"  # 指定使用 Atlas 自定义 UA

  - name: "tech-website"
    type: "web"
    url: "https://example.com"
    user_agent: "chrome_windows"  # 模拟 Chrome 浏览器
```

### 🔧 核心功能

#### BaseCollector 增强
- **`configure_user_agent()`** - 根据配置设置 User-Agent
- **`get_random_user_agent()`** - 获取随机 User-Agent
- **`set_user_agent()`** - 设置指定 User-Agent
- **`set_random_user_agent()`** - 设置随机 User-Agent

#### 配置优先级
1. **数据源特定配置** (`user_agent: "atlas_custom"`)
2. **随机模式** (`use_random_user_agent: true`)
3. **默认配置** (`default_user_agent`)

---

## 🎯 使用方式

### 1. 全局启用随机 User-Agent

```yaml
# config/sources.yaml
collectors:
  user_agents:
    # ... User-Agent 选项
```

```bash
# 环境变量
ATLAS_USE_RANDOM_USER_AGENT=true
```

### 2. 数据源指定 User-Agent

```yaml
sources:
  - name: "example-rss"
    user_agent: "chrome_windows"
```

### 3. 代码中使用

```python
from atlas.collectors import CollectorFactory
from atlas.core.config import get_config

config = get_config()
collector = CollectorFactory.create_collector('rss', config.collector)

# 设置随机 User-Agent
collector.set_random_user_agent()

# 设置指定 User-Agent
collector.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...")
```

---

## 🚀 技术优势

### ✅ 灵活性
- 支持多种配置方式
- 运行时动态切换
- 支持随机和固定模式

### ✅ 兼容性
- 模拟现代浏览器
- 支持移动设备 User-Agent
- 包含常用工具 User-Agent

### ✅ 可维护性
- 配置集中管理
- 类型安全的实现
- 完整的日志记录

### ✅ 可扩展性
- 易于添加新 User-Agent
- 支持自定义策略
- 插件化设计

---

## 📊 配置统计

### 预定义 User-Agent 选项
- **Atlas 系列**: 2 个
- **现代浏览器**: 5 个 (Chrome, Firefox, Safari, Edge)
- **移动设备**: 2 个 (iPhone, iPad)
- **工具类**: 3 个 (curl, wget, python-requests)
- **总计**: 12 个选项

### 支持的功能
- [x] 数据源特定配置
- [x] 随机选择模式
- [x] 轮换模式
- [x] 动态切换
- [x] 日志记录

---

## 🔧 配置示例

### 场景 1: 模拟真实浏览器
```yaml
# 大多数网站使用现代浏览器 UA
sources:
  - name: "news-website"
    user_agent: "chrome_windows"
```

### 场景 2: 避免 Ban
```yaml
# 使用随机 UA 降低被 Ban 风险
collectors:
  use_random_user_agent: true
  rotate_user_agent: true
```

### 场景 3: 特殊网站要求
```yaml
# 某些网站要求特定的 UA
sources:
  - name: "special-website"
    user_agent: "mozilla_compatible"  # 特定要求
```

---

## 💡 最佳实践建议

### 🎯 网站适配
- **RSS 源**: 使用 `atlas_custom` 或 `atlas_crawler`
- **新闻网站**: 使用现代浏览器 UA (Chrome, Firefox)
- **技术博客**: 使用 `chrome_mac` 或 `firefox_mac`
- **移动优先网站**: 使用 `iphone_safari`

### 🔒 反爬虫应对
- **频繁采集**: 启用随机和轮换模式
- **严格网站**: 使用最新浏览器 UA
- **API 接口**: 使用工具类 UA (curl, wget)

### 📊 监控建议
- 监控请求成功率
- 记录被拒绝的请求
- 分析最佳 User-Agent 策略

---

## 🔮 后续规划

### 短期优化
- [ ] User-Agent 使用统计
- [ ] 自动重试机制
- [ ] 成功率分析

### 中期扩展
- [ ] User-Agent 性能测试
- [ ] 智能选择算法
- [ ] 网站适配建议

### 长期演进
- [ ] 机器学习优化
- [ ] 自适应策略
- [ ] 社区共享 UA 库

---

## 📝 配置文档

- **主配置**: `config/sources.yaml`
- **采集器代码**: `src/atlas/collectors/`
- **配置说明**: 参考 User-Agent 配置部分

---

## 💭 备注

本次 User-Agent 配置功能的实现，为 Atlas 提供了强大的爬虫适配能力。通过灵活的配置系统，可以轻松应对不同网站的反爬虫策略，提高采集成功率。

特别是支持随机和轮换模式，大大降低了被 Ban 的风险。同时，丰富的预定义 User-Agent 选项，覆盖了主流浏览器和工具，确保了兼容性。

这个功能为后续的大规模数据采集奠定了坚实基础。

---

*功能实现遵循 [Atlas 文档体系规范](../documentation-system.md)*