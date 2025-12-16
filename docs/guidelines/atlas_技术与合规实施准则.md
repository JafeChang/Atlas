# Atlas 技术与合规实施准则

> 本文档用于沉淀 Atlas 项目的整体方法论、技术方案、LLM 接入策略、硬件与部署选择，以及合规与政策边界，作为**后续代码实现、架构演进和技术决策的长期准则**。

---

## 1. 项目定位与目标

### 1.1 项目名称
- **Atlas**

### 1.2 项目定位
Atlas 是一个跨行业信息聚合、整理与分析系统，核心目标是：

- 聚合各行业及公共政策的**公开信息**
- 将分散信息转化为**结构化、可追溯、可分析的资产**
- 支撑**产业趋势判断、投资方向研判与长期研究**

Atlas 不是新闻站、不是全文镜像站，而是：
> **一个以“判断”为目的的信息基础设施。**

### 1.3 首期重点行业
- 互联网
- 人工智能
- 半导体
- 脑机接口
- 具身机器人
- 社会法律与公共政策
- 农业
- 医疗
- 房地产

---

## 2. 总体方法论（Why & How）

### 2.1 方法论核心

```
公开信息
   ↓
系统化采集
   ↓
标准化整理
   ↓
LLM + 规则协同分析
   ↓
趋势与判断
```

### 2.2 三条底层原则
1. **证据链优先**：任何结论必须能回到原文
2. **分层可信度**：LLM 参与，但不直接决定最终事实
3. **可扩展性优先**：新增行业 ≠ 重构系统

---

## 3. 系统总体架构

### 3.1 架构分层

1. Source Registry（来源资产化）
2. Ingestion（采集层）
3. Raw Archive（原文归档）
4. Parse（解析与切块）
5. Normalize & Governance（标准化与治理）
6. LLM Cognition（认知层）
7. Index & Retrieval（检索层）
8. Analytics（分析层）
9. Serving（API / 管理后台）
10. Observability & Ops（监控与审计）

---

## 4. LLM 接入设计准则

### 4.1 LLM 的角色定位

- LLM 是 **效率放大器**，不是事实权威
- LLM 负责：
  - 理解
  - 抽取
  - 分类
  - 聚合
  - 摘要
- 不负责：
  - 原始事实存储
  - 最终结论裁定

### 4.2 数据分层（极其重要）

- **Raw**：原文与附件（不可变）
- **Proposed**：LLM 输出（建议态）
- **Confirmed**：规则/人工确认（可信态）

任何对外分析与结论，默认基于 Confirmed 层。

### 4.3 LLM 高 ROI 任务清单

优先级由高到低：
1. 结构化抽取（JSON + 引用证据）
2. 多标签分类（行业/主题/政策类型）
3. 事件聚合（Event Card）
4. 版本差异（政策修订 Diff）
5. 执行摘要（Evidence-backed）
6. RAG 问答（基于检索）

---

## 5. 技术选型（标准栈）

### 5.1 后端与调度
- Python + FastAPI
- Redis + Celery（任务队列）
- Prefect（流程编排）

### 5.2 数据与存储
- PostgreSQL 16 + pgvector
- OpenSearch（全文检索）
- MinIO / 云对象存储（原文与附件）

### 5.3 LLM 与语义能力
- LiteLLM（统一模型网关）
- LlamaIndex（RAG 框架）
- Langfuse（LLM 追踪与评估）
- Embedding / Rerank：小模型高频
- 抽取 / 差异：强模型低频

---

## 6. 硬件与部署策略

### 6.1 基本判断
- 日新增文档：**< 1K**
- 强模型：低频 API 调用
- 小模型：高频（embedding / rerank）
- 无原文保密诉求

### 6.2 推荐部署形态

**云上生产底座 +（可选）轻量推理节点**

- 云上：采集、解析、数据库、检索、对象存储、队列
- LLM 强模型：API
- 小模型：
  - 初期 API
  - 成本上升后再自建 GPU 节点

### 6.3 云实例建议（起步）

- 计算节点：4–8 vCPU / 16–32GB RAM
- 数据节点（Postgres + OpenSearch）：4–8 vCPU / 32GB RAM
- 对象存储：托管优先

---

## 7. 合规与政策边界（必须遵守）

### 7.1 爬取政府信息的合规原则

允许：
- 爬取**公开政府网站**的政策、公告、白皮书

禁止：
- 绕过权限
- 高频压测式抓取
- 爬取登录后或内部系统

技术要求：
- 遵守 robots.txt
- 严格限速
- 明确 User-Agent

### 7.2 数据跨境的现实判断

- Atlas 当前数据类型：
  - 公开信息
  - 非个人数据
  - 非重要/核心数据

→ **风险等级低，但需架构可拆**

### 7.3 架构性避险策略
- 数据分层（Raw / Analysis）
- 不混入私有或个人数据
- 设计上支持未来“国内/国际”拆分，但当前不提前复杂化

---

## 8. 实施顺序（工程准则）

### Phase 0：MVP
- 基础采集 + 存储 + 检索
- 不追求复杂分析

### Phase 1：LLM 核心能力
- 结构化抽取
- 分类
- Langfuse 上线

### Phase 2：认知增强
- 事件卡片
- 政策版本差异

### Phase 3：规模化
- 成本优化（embedding 自建）
- 分析自动化
- 多区域部署（如需要）

---

## 9. 项目技术价值观（写给未来的自己）

- 不为“看起来智能”牺牲可审计性
- 不为短期便利牺牲长期可扩展性
- 不让 LLM 成为黑箱裁判
- 永远保留回到原文的能力

> Atlas 的目标不是预测未来，
> 而是**减少判断未来时的信息不对称与认知噪音**。

---

（本文档将随着 Atlas 的演进持续修订）



---

## 10. 系统架构图（逻辑视图）

> 说明：该架构图为 Atlas 的**逻辑视图**（Logical Architecture）。用于统一模块边界与数据流；物理部署（云/本地/混合）可在此基础上做映射。

```mermaid
flowchart TB
  %% =====================
  %% Atlas Logical Architecture
  %% =====================

  subgraph SRC[Source Layer  信息源]
    S1[政府/监管官网
公告/法规/征求意见]
    S2[行业协会/标准组织
白皮书/标准/统计]
    S3[高校/科研/智库
论文/报告/会议]
    S4[企业披露
财报/公告/博客]
    S5[权威媒体/专业媒体
新闻/深度]
  end

  subgraph REG[Source Registry  来源资产化]
    R1[(sources)]
    R2[(watchlists
keywords/topics)]
    R3[采集策略
频率/限速/方式]
  end

  subgraph ING[Ingestion  采集层]
    I1[RSS/Atom 连接器]
    I2[API 连接器]
    I3[Scrapy 爬虫]
    I4[Playwright 动态抓取
(仅必要时)]
  end

  subgraph QUE[Task & Orchestration  任务与编排]
    Q1[Prefect
流程编排]
    Q2[Celery Workers
任务队列执行]
    Q3[(Redis
Queue/Cache)]
  end

  subgraph RAW[Raw Archive  原文归档层]
    O1[(Object Storage
S3/MinIO)]
    D1[(raw_documents)]
  end

  subgraph PARSE[Parse  解析与切块]
    P1[HTML 正文抽取
trafilatura/readability]
    P2[PDF/多格式解析
pdfplumber/Tika]
    P3[(parsed_chunks)]
  end

  subgraph GOV[Normalize & Governance  标准化与治理]
    N1[(normalized_records)]
    N2[去重/指纹
SHA256/SimHash]
    N3[规则校验
字段/状态/版本]
    N4[(entities
entity_links)]
    N5[(quality_flags)]
  end

  subgraph LLM[LLM Cognition  认知层（任务化）]
    L0[LLM Gateway
LiteLLM]
    L1[结构化抽取
JSON + Evidence]
    L2[多标签分类
行业/主题/政策类型]
    L3[事件聚合
Event Cards]
    L4[版本差异
Diff]
    L5[证据支撑摘要
Exec Summary]
    L6[(llm_runs
审计/成本/延迟)]
    L7[(proposed_*
建议态结果)]
  end

  subgraph IDX[Index & Retrieval  检索层]
    E1[(OpenSearch
全文索引)]
    V1[(pgvector
embeddings)]
    RR[Re-rank
(bge-reranker 可选)]
  end

  subgraph ANA[Analytics  分析层]
    A1[指标计算
SQL/pandas]
    A2[趋势/时间线
按行业/国家/实体]
    A3[模型化分析模板
PEST/五力/SWOT]
    A4[(analysis_outputs)]
  end

  subgraph SVC[Serving  服务层]
    API[FastAPI
Search/Docs/Events/QA]
    UI[管理后台/检索台
(后续实现)]
  end

  subgraph OBS[Observability  可观测与运维]
    M1[Prometheus/Grafana]
    M2[Sentry]
    M3[Langfuse
LLM 追踪]
    M4[日志
OpenSearch/Loki]
  end

  %% ====== Flows ======
  S1-->REG
  S2-->REG
  S3-->REG
  S4-->REG
  S5-->REG

  REG-->ING
  ING-->QUE

  QUE-->RAW
  RAW-->PARSE
  PARSE-->GOV

  GOV-->LLM
  LLM-->GOV

  GOV-->IDX
  PARSE-->IDX
  LLM-->IDX

  IDX-->SVC
  GOV-->ANA
  LLM-->ANA
  ANA-->SVC

  QUE-->OBS
  RAW-->OBS
  PARSE-->OBS
  GOV-->OBS
  LLM-->OBS
  IDX-->OBS
  ANA-->OBS
  SVC-->OBS

  %% ====== Storage Links ======
  ING-->D1
  RAW-->O1
  PARSE-->P3
  GOV-->N1
  LLM-->L6
  LLM-->L7
  IDX-->E1
  IDX-->V1

```

### 10.1 关键模块边界与职责（必须遵守）

- **Ingestion 不做判断**：只负责稳定采集与落 Raw。
- **Parse 可重复**：解析与切块输出必须可通过 Raw 重新生成。
- **Governance 是“口径中枢”**：字段标准、去重、版本链与质量告警在此收敛。
- **LLM 输出默认 Proposed**：必须带 evidence；不直接覆盖 Confirmed 字段。
- **Index/ Retrieval 与事实层解耦**：OpenSearch/向量索引可重建，不作为事实唯一来源。
- **Observability 是生产必需品**：采集/解析/LLM 成本与失败率必须可观测。

### 10.2 物理部署映射（建议）

- **云上生产底座**：Postgres、OpenSearch、对象存储、队列与编排、API、监控。
- **LLM 强模型**：优先 API（低频抽取/差异/摘要）。
- **小模型（embedding/rerank）**：初期 API；成本上升后可加独立轻推理节点（16GB 显存级别）。

