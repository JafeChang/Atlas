"""T-105（机器分类与提议）**先定义后实现**的验收判据 —— 判据的唯一出处。

本文件是**辅助模块**（不是测试文件，刻意不用 `test_` 前缀；照 `tests/_migrate_fixtures.py`
的先例）。它只做一件事：把判据写下来，供本任务的各个测试文件 import 并在 docstring 里
引用。这样"判据"只有一份，测试与报告不会各说一套。

硬规则 3：**不新建进度文档 / 报告文档**。判据放在代码里（测试模块与这里），不进 `docs/`。

---

T-105 验收判据（**实现之前写定**）
================================

**判据 1 —— 标签空间来自配置，且忘记注入要响亮可见**
`LabelSpace` 是**注入参数**（形状照 T-106 的 `industry_of=`）。构造期拒绝空标签集合；
本包**不 import** `atlas.registry`（SPEC §4.0：registry 不是 T-105 的上游）。
"组合根忘记注入"必须**响亮失败**，绝不静默产出空标签。
模型给出标签空间之外的取值时：写入一条 `out_of_space` **审计行**（值留着可审计），
但**不计入**分类结果 —— 闭环不因模型的自由发挥而断掉。

**判据 2 —— PI 只输出 quote，不输出坐标**
`ExtractedClaim` 的字段集**恰好**是 `{kind, value, quote, confidence}`；
`proposed_claims` 表里**没有**任何"模型给出的坐标"列：`unit_char_start` / `unit_char_end`
来自**单元**（T-130 的条目区间 / 整篇 `[0, len)`），不是来自模型。

**判据 3 —— Proposed 可覆写 + 保留版本链**
同一身份（`claim_key`）产出不同内容 ⇒ 追加新版本（`version` 递增、`supersedes` 指向前一版），
旧版本**永不删除**；`UPDATE` / `DELETE` 被 SQL 触发器拒绝。

**判据 4 —— 未经校验的证据不得进入 Confirmed**
本任务**不写** `confirmed_labels`（全文件运行前后该表计数与内容不变，逐字节比对）。

**判据 5 —— 版本三元组**
每一行 `proposed_claims` 与每一条 `proposal_runs` 都携带
`(code_version, config_version, model_version)`，且三者非空。

**判据 6 —— 降级 = 未分类，绝不是猜测**
端口返回 `unclassified` 时：`claims` 恒空、`reason` **必非空**；
落库的每一条都是 `status = unclassified`（`CHECK` 强制"没有 value/quote 且必有 reason"）。
本层**没有**任何关键词规则 / 启发式顶替路径（用真实降级触发一次证明，见判据 13）。

**判据 7 —— 幂等 / 可重算 / 可重试**
- 同输入 + 同配置重跑 ⇒ claim 行**逐字段相同**，且 `rows_written == 0`、
  `rows_unchanged > 0`（明确的"无变化"）；
- 输出变化（例如 `confidence` 变了）⇒ 新版本（`version == 2`，`supersedes == 1`）；
- **已跑过的单元不得重复调用模型**：第二次运行端口调用次数为 0；
- **可重试**（§3）：瞬时失败（超时 / 空输出 / 无法解析 / 不可达）在预算内重试，
  且重试轮**把批次缩到更小**；不可重试的失败（模型下架）**一次都不重试**；
  重试预算用尽时**落库 + 记录"耗尽"**（不静默丢单元）。

**判据 8 —— 零新增 Python 依赖**
`pyproject.toml` / `uv.lock` 不变；新增代码只 import stdlib + 已声明的 pydantic
+ 本项目自己的包。

**判据 9 —— 输入是异质的，必须先分流（SPEC §6.3 的直接结论）**
| 输入 | 分流 | 单元 |
|---|---|---|
| 是 feed | `DocumentKind.FEED` | **条目**（走 `atlas.entries.parse_entries`） |
| 已是逐篇文章 | `DocumentKind.ARTICLE` | **整篇**（**不**喂 feed 解析器） |
| 既不是 feed 也没有可分类内容 | `DocumentKind.SKIPPED` | 0 个 + **非空理由码** |
`skipped` 必须带 `SkipReason` 与可读 `detail`（构造期强制）；`EntryParseError` **不**被吞掉
当作"0 条目"，而是作为"这个 raw 不是 feed"的证据记进 `detail`。

**判据 10 —— pytest 全绿且不破坏现有测试**
本任务的测试另起文件名（`test_classify_*.py` / `test_propose_*.py`），
**不改** `tests/test_cognition_*.py`（T-003 的）与 `tests/test_entries_*.py`（T-130 的）。

**判据 11 —— 不变量不退化**
Raw 只增不改 / Confirmed 只增不改 / Proposed 可覆写（保留版本链）/ 归一化可重建。
真实数据测试在**只读**前提下运行：`data/store/raw` 树的 sha256 前后相同。

**判据 12 —— 真实数据流证据（硬规则 1）**
在**真实** `data/store` 上跑一个有界子集，用**真实模型**跑通完整链路：
单元 → `CognitionPort.extract` → 解析 → `proposed_claims` → **独立读回**验证。
报告实测：单元数 / 调用次数 / token / 耗时 / 成本 / claim 数 / quote 命中率。
并在同一脚本里给出**全量外推**（按本任务的调用策略）。

**判据 13 —— 失败路径有真实触发证据（不用 mock 声称）**
故意指向不可达端点 / 已下架模型，验证：状态为 `unclassified`、`reason` 非空、
`claims` 为空、落库行为是"每个单元一行未分类"，**一条编造结果都没有**。

**判据 14 —— 跨包 import 规则（SPEC §4.0）**
`src/atlas/cognition/` 的 T-105 部分**不** import `atlas.registry` / `atlas.labels` /
`atlas.search` / `atlas.feed` / `atlas.compose` / `atlas.webui` / `atlas.chunk` /
`atlas.migrate` / `atlas.catalog` / `atlas.collect`；允许的上游是
`atlas.contracts`、`atlas.entries`（T-130）、`atlas.normalize`（T-104）与包内模块。
用 **AST 扫描**钉死（不是靠约定）。

**判据 15 —— 账要平（不得静默丢东西）**
`attributed_claims + unattributed_claims == extracted_claims`；
`units_run == classified_units + unclassified_units`；
批次里每个单元都要有一行结果；归不到单元的 claim 落 `unattributed` 审计行。
两条恒等式在 `ProposalOutcome` 构造期强制。

---

**哪些判据在干净 worktree 里会自动跳过**：判据 12 / 13 需要真实 `data/`（不进 git）、
边车依赖与凭据，因此**必须自跳过**（照 `tests/test_search_realdata.py` 的先例），
退出码仍须为 0。真实数字由主工作区那次运行给出。
"""

from __future__ import annotations

__all__ = ["CRITERIA", "CRITERION_TITLES"]


#: 判据全文（唯一出处）。测试模块的 docstring 引用这里的编号，不各自抄一遍。
CRITERIA: dict[int, str] = {
    1: "标签空间来自配置（注入），忘记注入响亮失败；空间外取值记 out_of_space 审计行且不计入分类",
    2: "PI 只输出 quote，不输出坐标（claim 字段集恰好 4 个；表里没有模型给出的坐标列）",
    3: "Proposed 可覆写 + 保留版本链（新版本 supersedes 旧版本；UPDATE/DELETE 被触发器拒绝）",
    4: "未经校验的证据不得进入 Confirmed（本任务一行都不写 confirmed_labels）",
    5: "版本三元组 (code_version, config_version, model_version) 每次产出都携带且非空",
    6: "降级 = 未分类，绝不是猜测（claims 恒空 + reason 必非空 + CHECK 强制）",
    7: "幂等 / 可重算（重跑无变化；输出变化 → 新版本；已跑过的单元不再调用模型）",
    8: "零新增 Python 依赖",
    9: "异质输入先分流：feed→条目 / 文章→整篇 / 其它→跳过并给理由",
    10: "pytest 全绿且不破坏现有测试（测试另起文件名）",
    11: "不变量不退化；真实数据只读",
    12: "真实数据流证据（真实 store 的有界子集 + 真实模型 + 独立读回 + 全量外推）",
    13: "失败路径有真实触发证据（不可达端点 / 已下架模型 → 未分类且 reason 非空）",
    14: "跨包 import 只在允许的上游（AST 扫描钉死）",
    15: "账要平：attributed + unattributed == extracted；units_run == classified + unclassified",
}

#: 供 `pytest -k` 与报告引用的短标题。
CRITERION_TITLES: dict[int, str] = {
    number: text.split("（")[0] for number, text in CRITERIA.items()
}
