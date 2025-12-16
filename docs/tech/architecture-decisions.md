# Atlas 架构决策文档

> 基于 Q&A 的用户回答和技术判断，确定的系统架构决策和演进路径。

---

## 1. 分级架构演进策略

### 1.1 架构分级原则

基于用户的明确要求："从简单方案到复杂方案可迁移、易迁移，不要出现数据丢失的问题"，制定三级架构演进：

| 级别 | 日新增文档数 | 存储方案 | 数据库 | 检索 | 任务调度 | LLM 处理 |
|------|-------------|----------|---------|------|----------|----------|
| **MVP** | < 100 | JSON 文件 | SQLite | FTS5 | cron 直调 | 本地模型 |
| **Growth** | 100-1000 | JSON + MinIO | SQLite/PG | FTS5/OpenSearch | cron + 简单队列 | 本地+API |
| **Scale** | > 1000 | MinIO/S3 | PostgreSQL | OpenSearch | Prefect+Celery | 混合策略 |

### 1.2 数据迁移保证

```python
# 迁移路径设计
MIGRATION_PATHS = {
    "sqlite_to_postgres": {
        "tool": "pgloader",
        "data_integrity": "checksum_validation",
        "downtime": "near_zero"
    },
    "json_to_minio": {
        "tool": "custom_sync",
        "incremental": True,
        "fallback": "keep_json_backup"
    },
    "fts_to_opensearch": {
        "tool": "bulk_reindex",
        "parallel": True,
        "validation": "sample_comparison"
    }
}
```

---

## 2. MVP 架构设计

### 2.1 MVP 核心组件（奥卡姆剃刀原则）

```yaml
MVP_Components:
  Core_Stack:
    - Python 3.13 + uv
    - SQLite (数据库)
    - JSON 文件存储
    - cron 调度

  Data_Processing:
    - requests (HTTP 客户端)
    - beautifulsoup4 (HTML 解析)
    - SQLite FTS5 (全文检索)
    - 本地 LLM (优先)

  Utilities:
    - loguru (日志)
    - pydantic (数据验证)
    - click (CLI)

  Explicit_Excluded:
    - Redis (暂不需要)
    - Celery/Prefect (过度设计)
    - OpenSearch (SQLite FTS5 足够)
    - Docker (增加复杂度)
```

### 2.2 MVP 项目结构

```
atlas/
├── src/
│   ├── core/
│   │   ├── config.py          # 配置管理
│   │   ├── database.py        # SQLite 操作
│   │   └── storage.py         # JSON 文件存储
│   ├── collectors/
│   │   ├── base.py           # 基础采集器
│   │   └── rss.py            # RSS 采集器
│   ├── processors/
│   │   ├── parser.py         # 内容解析
│   │   └── dedup.py          # 去重逻辑
│   ├── llm/
│   │   └── local_client.py   # 本地 LLM 客户端
│   └── cli.py                # 命令行接口
├── data/                     # JSON 文件存储
│   ├── raw/
│   ├── processed/
│   └── indexes/
├── config/
│   └── sources.yaml          # 数据源配置
├── logs/
├── scripts/
│   └── cron_job.py           # cron 调度脚本
├── pyproject.toml
└── README.md
```

### 2.3 核心决策总结

**用户明确回答的问题**：
- ✅ **Q1.1**: 确认架构需要分级，支持从简单到复杂的迁移
- ✅ **Q1.2**: MVP 满足方法论核心即可，使用奥卡姆剃刀原则
- ✅ **Q2.1**: 初期使用本地模型，支持排队机制
- ✅ **Q2.2**: 本地模型处理不过来再接入 API
- ✅ **Q3.1**: 采集频率动态调整，初期每域名每5分钟不超过1次
- ✅ **Q3.2**: 初期不考虑反爬虫（本地服务）
- ✅ **Q4.1**: 本地资源足够
- ✅ **Q5.1**: 实施步骤后续手动调用 LLM 决策
- ✅ **Q5.2**: 成功标准 = 稳定运行一天 + 采集到有效信息
- ✅ **Q6.1-Q7.3**: 初期简化处理（经验不足，后续优化）
- ✅ **Q8.1-Q8.3**: 由我来决策技术细节

---

## 3. 技术选型决策（我来定）

### 3.1 Python 依赖管理策略

```toml
# pyproject.toml
[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "ruff>=0.1",
    "mypy>=1.8"
]

dependencies = [
    "requests>=2.31",
    "beautifulsoup4>=4.12",
    "pydantic>=2.5",
    "loguru>=0.7",
    "click>=8.1",
    "pyyaml>=6.0",
    "schedule>=1.2"
]
```

**决策理由**：
- **uv 约束**：使用 uv 管理依赖，禁用 pip install
- **最小依赖**：每个依赖都有明确用途，避免过度引入
- **版本锁定**：生产环境锁定版本，开发环境允许小版本更新

### 3.2 数据库选型决策

**选择**：SQLite 3.45+ (初期) → PostgreSQL 16 (成长期)

**决策理由**：
```python
DATABASE_DECISION = {
    "sqlite_reasons": [
        "零配置，开箱即用",
        "文件存储，便于备份",
        "FTS5 全文检索足够 MVP",
        "Python 标准库支持",
        "迁移路径清晰"
    ],
    "postgres_triggers": [
        "文档数 > 10000",
        "并发访问需求",
        "复杂查询需求",
        "向量搜索需求"
    ]
}
```

### 3.3 任务调度设计

**选择**：cron + Python 脚本 + 简单状态管理

```python
# scripts/cron_job.py
import subprocess
import time
from pathlib import Path

def run_with_lock(script_path: str, lock_file: str):
    """简单锁机制防止重复执行"""
    lock = Path(lock_file)
    if lock.exists():
        print(f"Task {script_path} already running")
        return

    try:
        lock.write_text(str(time.time()))
        subprocess.run(["python", script_path], check=True)
    finally:
        lock.unlink(missing_ok=True)

# crontab 示例
# */10 * * * * cd /path/to/atlas && python scripts/cron_job.py
```

**决策理由**：
- **简单可靠**：cron 是系统级标准工具
- **易于调试**：直接运行 Python 脚本
- **状态透明**：文件锁状态清晰可见
- **低资源占用**：无需额外进程

---

## 4. 数据采集具体策略

### 4.1 频率控制实现

```python
class RateLimiter:
    def __init__(self, domain_interval: int = 300):  # 5分钟
        self.domain_interval = domain_interval
        self.last_access = {}

    def can_access(self, domain: str) -> bool:
        now = time.time()
        last = self.last_access.get(domain, 0)

        if now - last >= self.domain_interval:
            self.last_access[domain] = now
            return True
        return False

# 使用示例
limiter = RateLimiter()
if limiter.can_access("example.com"):
    # 执行采集
    pass
else:
    # 跳过，记录日志
    pass
```

### 4.2 User-Agent 策略

```python
USER_AGENT = "Atlas/0.1.0 (Information Aggregation System; contact: your-email@example.com)"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}
```

---

## 5. 本地 LLM 集成方案

### 5.1 本地模型选择

**推荐**：Ollama + Qwen2.5-7B-Instruct

**理由**：
- 7B 参数平衡性能和资源需求
- 中文支持优秀
- API 兼容 OpenAI 格式
- 支持流式输出

### 5.2 队列机制设计

```python
import queue
import threading
from typing import Dict, Any

class LLMQueue:
    def __init__(self, max_workers: int = 1):
        self.queue = queue.Queue()
        self.max_workers = max_workers
        self.workers = []
        self.start_workers()

    def enqueue(self, task: Dict[str, Any]) -> str:
        """任务入队，返回任务 ID"""
        task_id = str(uuid.uuid4())
        task["id"] = task_id
        self.queue.put(task)
        return task_id

    def process_task(self, task: Dict[str, Any]):
        """处理单个任务"""
        try:
            # 调用本地 LLM
            result = self.call_local_llm(task["prompt"])
            # 保存结果
            self.save_result(task["id"], result)
        except Exception as e:
            self.log_error(task["id"], e)
```

### 5.3 降速机制

```python
class AdaptiveCollector:
    def __init__(self, llm_queue: LLMQueue):
        self.llm_queue = llm_queue
        self.base_interval = 600  # 10分钟基础间隔
        self.current_interval = self.base_interval

    def adjust_speed(self):
        """根据队列长度调整采集速度"""
        queue_size = self.llm_queue.qsize()

        if queue_size > 50:  # 队列过长，降速
            self.current_interval = min(self.current_interval * 1.5, 3600)
        elif queue_size < 10:  # 队列较短，提速
            self.current_interval = max(self.current_interval * 0.8, 60)
```

---

## 6. 数据治理简化实现

### 6.1 LLM 去重策略

```python
class LLMDeduplicator:
    def __init__(self, llm_client):
        self.llm_client = llm_client
        self.prompt_template = """
        判断以下两个文档是否重复内容：

        文档A标题：{title_a}
        文档A内容：{content_a[:500]}...

        文档B标题：{title_b}
        文档B内容：{content_b[:500]}...

        请回答：
        1. 是否重复（是/否）
        2. 重复程度（0-100%）
        3. 判断理由

        格式：JSON
        """

    async def is_duplicate(self, doc_a: dict, doc_b: dict) -> dict:
        prompt = self.prompt_template.format(
            title_a=doc_a["title"],
            content_a=doc_a["content"],
            title_b=doc_b["title"],
            content_b=doc_b["content"]
        )

        result = await self.llm_client.generate(prompt)
        return json.loads(result)
```

### 6.2 简化数据质量检查

```python
def basic_quality_check(document: dict) -> dict:
    """基础质量检查"""
    issues = []

    # 长度检查
    if len(document["content"]) < 100:
        issues.append("内容过短")

    # 标题检查
    if not document["title"].strip():
        issues.append("标题为空")

    # 内容重复检查
    if document["content"].count(document["content"][:100]) > 2:
        issues.append("内容重复")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "score": max(0, 100 - len(issues) * 20)
    }
```

---

## 7. 错误处理策略

### 7.1 错误日志设计

```python
import json
from datetime import datetime
from pathlib import Path

class StructuredLogger:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    def log_error(self, error_type: str, details: dict):
        """结构化错误日志"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "type": error_type,
            "details": details
        }

        log_file = self.log_dir / f"errors_{datetime.now().strftime('%Y%m%d')}.json"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
```

### 7.2 数据源失效处理

```python
class SourceHealthMonitor:
    def __init__(self):
        self.failure_counts = {}
        self.max_failures = 3

    def record_failure(self, source_id: str):
        """记录失败次数"""
        self.failure_counts[source_id] = self.failure_counts.get(source_id, 0) + 1

        if self.failure_counts[source_id] >= self.max_failures:
            self.disable_source(source_id)

    def is_healthy(self, source_id: str) -> bool:
        """检查数据源是否健康"""
        return self.failure_counts.get(source_id, 0) < self.max_failures
```

---

## 8. MVP 实施清单

### 8.1 第一阶段：核心功能（1-2 周）

- [ ] 基础项目结构搭建
- [ ] SQLite 数据库初始化
- [ ] JSON 文件存储系统
- [ ] 基础 HTTP 采集器
- [ ] 简单内容解析器
- [ ] cron 调度脚本
- [ ] 基础错误日志

### 8.2 第二阶段：LLM 集成（1 周）

- [ ] 本地 LLM 客户端
- [ ] 任务队列机制
- [ ] LLM 去重功能
- [ ] 降速控制机制

### 8.3 第三阶段：验证与优化（1 周）

- [ ] 端到端测试
- [ ] 性能监控
- [ ] 数据质量检查
- [ ] 稳定性测试

### 8.4 验收标准

✅ **稳定运行**：连续 24 小时无崩溃
✅ **有效采集**：成功采集到用户认可的有效信息
✅ **去重效果**：重复内容识别准确率 > 80%
✅ **资源控制**：内存使用 < 2GB，CPU 使用 < 50%

---

## 9. 风险控制措施

### 9.1 技术风险

| 风险 | 缓解措施 | 应急方案 |
|------|----------|----------|
| 本地 LLM 性能不足 | 队列机制 + 降速 | 云端 API 降级 |
| SQLite 性能瓶颈 | 定期维护 + 查询优化 | 迁移到 PostgreSQL |
| 数据采集失败 | 重试机制 + 源健康监控 | 手动干预 + 源替换 |

### 9.2 运维风险

| 风险 | 监控指标 | 告警阈值 |
|------|----------|----------|
| 磁盘空间不足 | 使用率 | > 80% |
| 内存泄漏 | 内存使用 | 持续增长 |
| 任务卡死 | 执行时间 | > 30分钟 |

---

## 10. 长期演进规划

### 10.1 架构升级触发条件

```python
UPGRADE_TRIGGERS = {
    "database": {
        "condition": "document_count > 50000",
        "action": "migrate_to_postgres",
        "timeline": "3-6 months"
    },
    "search": {
        "condition": "search_latency > 2s",
        "action": "deploy_opensearch",
        "timeline": "6-12 months"
    },
    "queue": {
        "condition": "queue_size > 1000",
        "action": "implement_celery",
        "timeline": "6-9 months"
    }
}
```

### 10.2 技术债务管理

**定期重构**：每季度一次代码审查和优化
**依赖更新**：每月检查依赖安全更新
**性能调优**：根据监控数据持续优化
**文档维护**：每功能模块完成后更新文档

---

## 11. 总结

**核心决策**：
1. **分级演进**：MVP → Growth → Scale，确保数据可迁移
2. **奥卡姆剃刀**：MVP 只包含核心功能，避免过度设计
3. **本地优先**：本地 LLM + 简单队列，支持降速控制
4. **渐进完善**：错误处理、数据治理等逐步完善
5. **用户驱动**：稳定运行 + 有效信息采集为验收标准

**成功关键**：
- 保持简单，快速迭代
- 重视数据安全和可迁移性
- 建立良好的监控和日志习惯
- 根据实际使用情况调整架构

这个架构设计平衡了简洁性和可扩展性，为 Atlas 的长期发展奠定了坚实基础。