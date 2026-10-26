"""T-105 机器分类与提议：单元 → 一次调用 → 归属 → `proposed_claims`（SPEC §4.2 T-105）。

**调用策略：分批调用（batch prompting），不是一次一个单元**
=======================================================

这是本任务的核心设计问题，先摆实测事实，再给结论。

已知成本结构（SPEC §2.14 主代理实测 + 本任务在**真实数据**上的实测）：

| 项 | 实测 |
|---|---|
| 边车**每次调用**的进程启动开销（drvfs 上 import pi-ai） | **~4.5 s**（主代理）/ **~6.4 s**（本任务复测 `inspect`） |
| 一次端到端模型调用（单单元） | 墙钟 **9.7 s**（6 次实测均值），其中模型侧 8.5 s |
| 一次端到端模型调用（分批） | 模型侧 **6.0 s**/次 |
| 分类单元数（本轮复核真实 store） | 830 条目 + 65 文章 = **895** |

| 策略 | 调用次数（首轮） | 串行耗时外推 | 实测依据 |
|---|---|---|---|
| 一单元一次调用 | **895** | ≈ **144 min**（按实测 9.7 s/次） | 6 次实测：5/6 成功 |
| 分批（≤4 单元 / ≤6000 字符）**+ 有界重试** | **334** | ≈ **58 min** | 14 次实测：13 个单元 → 10 分类 / 3 未分类，quote 命中 21/21 = **100%** |

**结论：采用分批调用 + 有界重试。** 依据是三条实测事实：

1. **固定开销按调用次数计**：batch 4 把 895 次压到 334 次，
   单是边车启动就从 67 min 降到 25 min；
2. **批量不能无限大**：`deepseek-flash` 是推理型，实测一次调用的 reasoning token
   常达 1500–2200（占 `output` 的 **87–92%**），而单次调用的输出预算有限。
   ⚠️ **本注释原先写错了，2026-09-26 由主代理更正并实测**：原文称 T-003 的
   `max_output_tokens` **没有**被 adapter 送进边车——**这是假的**。
   实测链路：`adapter._build_job()` → `call.maxOutputTokens` → 边车
   `{...base, maxTokens: call.maxOutputTokens}` → SDK 以 **`max_completion_tokens`**
   发出（实测：配置 1234 → 请求体 `max_completion_tokens: 1234`）。
   生效预算是**配置值**（现为 **4096**）。
   ⚠️ **2048 太小，已由实测否定**（2026-09-28，`tools/t105_yield_probe.py`）：
   25 次真实单单元调用里 **4 次**被截断（`output_tokens == reasoning_tokens == 2048`、
   `stopReason=length` ⇒ 内容为 0、降级 `empty_completion`），产出率 **10/25 = 40%**；
   4096 下 **0 次截断**、**14/25 = 56%**。预算被吃掉的速度比原先估计得快。
   一批塞 5–10 个长单元时 `empty_completion` 明显增多；
3. **单单元调用也会失败**（实测 83% 成功），失败是**瞬时**的（同一份输入重跑常常成功）
   ⇒ 必须**有界重试**，且重试要把批次缩到单单元。
   ⚠️ 2026-09-28 补充实测：真实库里 13 行 `timeout` 的 `input_tokens`/`output_tokens`
   **都是 0** 且墙钟恒为 **11.4–11.5 s**（13 行互差 < 200 ms），出现在**连续 2.5 分钟**
   的时间窗里 —— 那是**传输层**失败（请求根本没到模型），不是输出预算问题；
   同一批单元在 `--write-store` 之后重跑（`tools/t105_yield_probe.py`，临时库）
   产出率 **40%（2048）/ 56%（4096）**，**一次 0-token 失败都没有**。
   即"瞬时"这条实测成立，而**当前**的瓶颈已不是它，是 `no_claim_extracted`。

**为什么批次按 `raw_id` 分组**（不是全局打包）
-------------------------------------------

`CognitionRequest.raw_id` 是一个单元/一次调用的**溯源锚**；跨 raw 混装会让一批
claim 的 `raw_id` 无从确定。而"同一 raw 内的单元同源"是事实，因此：

- **批次边界 = 单个 raw 内部的单元，按 `(char_start, unit_id)` 排序后切分**
  （`plan_batches`，纯函数、确定性、可复算）；
- 两条上限先到者生效：`BATCH_MAX_UNITS = 4`、`BATCH_MAX_CHARS = 6000`。

**分批最大的风险：claim 如何归属到具体单元 —— 以及"没返回"怎么记账**
-------------------------------------------------------------------

归属**不**依赖模型输出任何 ID（输出契约是冻结的 `{kind, value, quote, confidence}`，
加字段就是契约违例）。归属靠 SPEC §2.2 的**同一条分工**：

> 模型只给 `quote`（文字），系统做**确定性匹配**。

`_attribute()` 把每条 quote 匹配回单元文本，规则是：

1. 对每个单元，quote 必须落在**该单元文本**里（精确匹配，或经空白折叠后的匹配
   —— 与 `atlas.contracts.match_quote` 同一口径）；
2. 恰好命中**一个**单元 → 归属它；
3. 命中**零个**或**多个** → **不猜**：记为 `unattributed` 行 ——
   `status = unattributed` 的**独立记录**，带批次号与序号，能在库里查到。

**"某一条没返回"怎么记账**（不得静默丢）：批次里**每个**单元都要有一行结果：

| 情形 | 落库 |
|---|---|
| 单元被赋予 ≥1 条 quote | 每个标签一行 `classified` |
| 单元**一条 quote 都没有** | 一行 `unclassified` + `reason = no_claim_extracted` |
| 整批降级（模型不可用） | **每个**单元一行 `unclassified` + 该批的降级原因码 |
| 归属不了的 quote | 一行 `unattributed`（**不丢**，也不硬塞给某个单元） |
| 取值在标签空间之外的 claim | 一行 `out_of_space`（记下模型说了什么，**不计入**分类结果） |
| feed 里没有可分类内容 | 由 `classify` 判为 `skipped` + 理由码（不进本层） |

因此 `ProposalOutcome` 的对账恒等式可以**当场自检**：

```
attributed_claims + unattributed_claims == extracted_claims
units_run == classified_units + unclassified_units
```

这两条在 `ProposalOutcome.__post_init__` 里强制。**不满足就是编码错误，直接抛。**
（`out_of_space` 行不是 claim 也不是单元，它单独计数在 `rows_out_of_space` 里。）

**降级 = 未分类，绝不是猜测**（§2.14 决策四）
============================================

`CognitionResult.status == unclassified` 时本层**一条 claim 都不写**，只给每个单元
写一行 `unclassified` + `reason`（原因码来自 T-003 的闭集）。本层**没有任何**
关键词规则 / 启发式回退 —— 那正是 §2.14 决策四明令禁止的"偷偷顶上"。

**幂等**（§3）
==============

- **不重复调用模型**：`plan_digest = (unit_digest, code/config/model 版本, 标签空间版本)`
  在 `proposal_runs` 里查得到 ⇒ 该单元**跳过**，不发起调用（降级单元也一样跳过）；
- **不重复写行**：`claim_key` 只由输入决定，`output_digest` 只由输出决定；
  同一 `(claim_key, output_digest)` 已存在 ⇒ 无变化（版本不推进）；
- 输出变化 ⇒ 追加新版本，旧版本保留（§2.3 "Proposed 可覆写"）。

**依赖方向**（SPEC §4.0）
=========================

本模块**不 import** `atlas.registry`（不是 T-105 的上游）。标签空间是注入参数
（`LabelSpace`，与 T-106 的 `industry_of=` 同一形状）。`CognitionPort` 也用
`Protocol` **结构化**声明，因此本模块不 import `atlas.cognition.adapter`
（那是 T-003 的实现细节，本层只需要"端口形状"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from atlas.entries import sanitize_for_quote

from .classify import (
    BATCH_MAX_CHARS,
    BATCH_MAX_UNITS,
    LabelSpace,
    Unit,
    batch_key_for,
    classify_document,
    plan_batches,
)
from .store import (
    CLAIM_STATUS_CLASSIFIED,
    CLAIM_STATUS_OUT_OF_SPACE,
    CLAIM_STATUS_UNATTRIBUTED,
    CLAIM_STATUS_UNCLASSIFIED,
    KIND_INDUSTRY,
    ProposedClaimRow,
    ProposalRunRow,
    ProposedStoreError,
    SqliteProposedStore,
    batch_id_for,
    claim_key_for,
    label_space_version,
    output_digest_for,
    plan_digest_for,
)

__all__ = [
    "ATTRIBUTION_STATUS_ATTRIBUTED",
    "ATTRIBUTION_STATUS_MULTIPLE",
    "ATTRIBUTION_STATUS_NONE",
    "REASON_LABEL_OUT_OF_SPACE",
    "REASON_NO_CLAIM",
    "REASON_UNATTRIBUTED_QUOTE",
    "UNATTRIBUTED_UNIT_PREFIX",
    "AttributedClaim",
    "Attribution",
    "BatchOutcome",
    "BatchRequest",
    "CognitionPortLike",
    "OutcomeCounters",
    "ProposalOutcome",
    "ProposalPolicy",
    "UnattributedClaim",
    "attribute_claims",
    "build_batch_content",
    "build_batch_instruction",
    "build_batches",
    "propose_document",
    "propose_documents",
    "propose_units",
    "run_batch",
]

#: 原因码（本层自己的**闭集**，与 T-003 `DegradeReason` 的值域**刻意不重叠**，
#: 避免把"模型不可用"与"模型说了但没有可归属的输出"混成一类）。
REASON_NO_CLAIM = "no_claim_extracted"
REASON_UNATTRIBUTED_QUOTE = "unattributed_quote"
REASON_LABEL_OUT_OF_SPACE = "label_out_of_space"
REASON_RETRY_EXHAUSTED = "retry_exhausted"

#: **可重试**的降级原因（实测依据：`deepseek-flash` 是推理型模型，推理 token 会吃掉
#: 整个输出预算，于是同一份输入有时返回完整 JSON、有时返回空/被截断 —— 这是
#: **瞬时**失败，重试有意义）。见 `tools/t105_real_evidence.py` 的实测数字。
RETRYABLE_REASONS = frozenset(
    {
        "timeout",
        "empty_completion",
        "unparseable_output",
        "unreachable_model",
        "http_error",
        "sidecar_error",
    }
)

#: **不可重试**的降级原因：重试一万次也是同一个结果，重试只会烧钱。
PERMANENT_REASONS = frozenset({"model_deprecated"})

ATTRIBUTION_STATUS_ATTRIBUTED = "attributed"
ATTRIBUTION_STATUS_NONE = "none"
ATTRIBUTION_STATUS_MULTIPLE = "multiple"

#: 未归属行的单元标识前缀。**刻意不是** `ent_` / `art_`：
#: 它不是一个单元，而是一条"归不到任何单元"的记账 —— 混用前缀会让下游误以为它是单元。
UNATTRIBUTED_UNIT_PREFIX = "unattributed:"

#: 批次提示里的单元分隔标记。**极不可能**在真实文本里自然出现，
#: 因此 `assert` 检查"单元文本里不含它"是可以真的失败的判据（见 `build_batch_content`）。
UNIT_MARKER_RE = re.compile(r"^\[\[unit (\d+)\]\]$")


# --------------------------------------------------------------------------- #
# 端口形状（结构化，不 import T-003 的实现）
# --------------------------------------------------------------------------- #


class CognitionPortLike(Protocol):
    """`CognitionPort` 的**结构形状**（SPEC §2.14 决策二：端口是稳定接口）。

    本模块只依赖这个形状，不 import `atlas.cognition.adapter` —— 换 provider /
    换模型 / 换回直连 HTTP 都只换适配器，本层不动。
    """

    def extract(self, request: Any) -> Any:  # pragma: no cover - 协议声明
        ...


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProposalPolicy:
    """一次提议运行的**全部可调参数**（配置快照的一部分，SPEC §3）。

    为什么把它做成显式对象而不是散落的默认参数：批次大小**改变调用次数**，
    也**改变模型看到的输入**。因此它必须进配置指纹 —— 否则"同输入 + 同配置"
    这句话会因为批次策略的隐式默认值而失真。

    重试（§3 "可重试"）的参数：`max_retries` 是**每个单元**最多重试几次；
    `retry_max_units_per_call` 是重试轮里一批最多几个单元（默认 **1**）。

    为什么重试要**缩到单单元**（实测依据）：`deepseek-flash` 是推理型模型，一次调用的
    输出预算会被 reasoning token 吃掉；实测一批 5–10 个长单元时 `empty_completion`
    明显增多，而单单元调用的成功率高得多。因此"整批失败"之后继续用同样大的批次
    只是重复烧钱 —— 必须缩到最小粒度。

    `retry_shrink=True`（默认）让**每一轮**的上限继续减半
    （`units_for_attempt`），用于"多个单元同时失败但还没到单单元"的中间轮次。
    """

    max_units_per_call: int = BATCH_MAX_UNITS
    max_chars_per_call: int = BATCH_MAX_CHARS
    kind: str = KIND_INDUSTRY
    include_link: bool = True
    source: str = "t105-classify"
    max_retries: int = 2
    retry_max_units_per_call: int = 1
    retry_shrink: bool = True

    def __post_init__(self) -> None:
        if self.max_units_per_call <= 0:
            raise ProposedStoreError("max_units_per_call 必须为正")
        if self.max_chars_per_call <= 0:
            raise ProposedStoreError("max_chars_per_call 必须为正")
        if self.max_retries < 0:
            raise ProposedStoreError("max_retries 不得为负")
        if self.retry_max_units_per_call <= 0:
            raise ProposedStoreError("retry_max_units_per_call 必须为正")
        if not self.kind:
            raise ProposedStoreError("kind 不得为空")

    def units_for_attempt(self, retry_count: int) -> int:
        """第 `retry_count` 轮（0 = 首次）一批最多几个单元。

        重试轮**先按 `retry_max_units_per_call` 封顶**（默认 1：整批失败就拆到单单元），
        再按 `retry_shrink` 逐轮减半。两条规则都是"往上取小"，因此重试只会更细，
        不会更粗。
        """
        if retry_count <= 0:
            return self.max_units_per_call
        limit = min(self.max_units_per_call, self.retry_max_units_per_call)
        if self.retry_shrink:
            limit = max(1, limit // (2 ** (retry_count - 1)))
        return max(1, limit)

    def fingerprint(self) -> str:
        import hashlib

        payload = (
            f"{self.max_units_per_call}|{self.max_chars_per_call}|{self.kind}|"
            f"{int(self.include_link)}|{self.max_retries}|"
            f"{self.retry_max_units_per_call}|{int(self.retry_shrink)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_units_per_call": self.max_units_per_call,
            "max_chars_per_call": self.max_chars_per_call,
            "kind": self.kind,
            "include_link": self.include_link,
            "source": self.source,
            "max_retries": self.max_retries,
            "retry_max_units_per_call": self.retry_max_units_per_call,
            "retry_shrink": self.retry_shrink,
            "fingerprint": self.fingerprint(),
        }


# --------------------------------------------------------------------------- #
# 批次提示
# --------------------------------------------------------------------------- #


def build_batch_instruction(unit_count: int) -> str:
    """分批调用的**额外指令**（放在 `CognitionRequest.instruction` 里）。

    为什么走 `instruction` 而不是改 system prompt：T-003 的 `build_system_prompt`
    是**已交付、被测试钉住**的契约（标签空间、输出形状、"不要给坐标"都在那里）。
    T-105 **不改 T-003 的实现**，而是把"这次是多个单元"这件事作为**本次调用的指令**
    传进去 —— 这正是 `CognitionRequest.instruction` 这个字段存在的意义。

    指令文本**确定性**（只依赖单元数），因此同一批次的幂等键稳定。
    """
    if unit_count <= 0:
        raise ProposedStoreError("unit_count 必须为正")
    return (
        f"THIS CALL CONTAINS {unit_count} SEPARATE DOCUMENTS (units), not one document.\n"
        "The user message below is that many documents concatenated. Each document starts "
        "with its own line `[[unit N]]` (N = 1, 2, 3, ...). Those marker lines are "
        "delimiters added by the system, not document text: never quote them.\n"
        "Rules for this call:\n"
        f"- Return ONE claims array covering ALL {unit_count} units.\n"
        "- Every `quote` must be copied VERBATIM from the body of exactly one unit, and must "
        "be long enough to be unambiguous inside that unit.\n"
        "- If one unit deserves several different labels, emit one claim per label, each with "
        "its own verbatim quote from that unit.\n"
        "- If a unit does not match any allowed label, emit NO claim for it. Do not invent a "
        "claim to fill the gap.\n"
        "- Never merge text from two units into one quote."
    )


def build_batch_content(units: Sequence[Unit]) -> str:
    """把一批单元拼成**一段**外部内容（`CognitionRequest.external_content`）。

    格式（确定性、可复算）：

    ```
    [[unit 1]]
    <单元文本>

    [[unit 2]]
    <单元文本>
    ```

    **守卫**：单元文本里若出现形如 `[[unit N]]` 的行，会破坏"模型看到的边界"，
    因此这里**响亮失败**（而不是静默改写内容 —— 那会让 quote 无法逐字匹配）。
    """
    if not units:
        raise ProposedStoreError("批次不得为空")
    blocks: List[str] = []
    for position, unit in enumerate(units, start=1):
        for line in unit.text.split("\n"):
            if UNIT_MARKER_RE.match(line.strip()):
                raise ProposedStoreError(
                    f"单元 {unit.unit_id} 的文本里有形如 `[[unit N]]` 的行"
                    f"（{line.strip()!r}）：它会与系统的分隔标记冲突，"
                    "必须显式处理（改标记或跳过该单元），不得静默改写内容"
                )
        blocks.append(f"[[unit {position}]]\n{unit.text}")
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# 归属（确定性；不依赖模型输出 ID）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Attribution:
    """一条 quote 的归属结论。"""

    position: int
    unit_index: Optional[int]
    status: str
    matches: Tuple[int, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class AttributedClaim:
    """一条**已归属**的 claim（`unit_index` 非空）。"""

    unit_index: int
    unit: Unit
    order: int
    kind: str
    value: str
    quote: str
    confidence: float


@dataclass(frozen=True, slots=True)
class UnattributedClaim:
    """一条**归不到单元**的 claim（`status` 说明为什么）。

    它不是"被丢掉的"claim —— 它会作为一行 `unattributed` 记录落库。
    """

    position: int
    kind: str
    value: str
    quote: str
    confidence: float
    status: str
    detail: str


def _matches(text: str, quote: str) -> bool:
    """quote 是否**确定性地**落在这段文本里。

    两级，与 `atlas.contracts.match_quote` 同一口径（先精确、后空白折叠）：
    单元文本已经在 `reduce_text` 里折叠过空白，因此这里的折叠分支是给
    "模型在引文里加了换行"这类真实情况兜底 —— 它**不放大**范围，只是让
    空白差异不再造成人为的"未归属"。
    """
    candidate = quote.strip()
    if not candidate:
        return False
    if candidate in text:
        return True
    collapsed = " ".join(candidate.split())
    return bool(collapsed) and collapsed in text


def attribute_claims(
    units: Sequence[Unit], claims: Sequence[Any]
) -> Tuple[List[AttributedClaim], List[UnattributedClaim], Tuple[Attribution, ...]]:
    """把模型返回的 claim 归属到单元（**确定性**，见模块 docstring 的三条规则）。

    返回 `(已归属, 未归属, 全部结论)`。已归属按 `(unit_index, order)` 排序 ——
    与批次里的单元顺序、模型给出的顺序一致，因此可复现。
    """
    attributed: List[AttributedClaim] = []
    unattributed: List[UnattributedClaim] = []
    report: List[Attribution] = []

    for position, claim in enumerate(claims):
        quote = str(getattr(claim, "quote", "") or "")
        hits = tuple(
            index for index, unit in enumerate(units) if _matches(unit.text, quote)
        )
        if len(hits) == 1:
            index = hits[0]
            attributed.append(
                AttributedClaim(
                    unit_index=index,
                    unit=units[index],
                    order=position,
                    kind=str(getattr(claim, "kind", "") or ""),
                    value=str(getattr(claim, "value", "") or ""),
                    quote=quote,
                    confidence=float(getattr(claim, "confidence", 0.0) or 0.0),
                )
            )
            report.append(
                Attribution(
                    position=position,
                    unit_index=index,
                    status=ATTRIBUTION_STATUS_ATTRIBUTED,
                    matches=hits,
                    detail=f"命中唯一单元 [{index}]",
                )
            )
            continue

        status = ATTRIBUTION_STATUS_NONE if not hits else ATTRIBUTION_STATUS_MULTIPLE
        detail = (
            "quote 在这批单元里一处都找不到（逐字/折叠都不匹配）："
            "模型可能改写/翻译了引文 —— 不猜，记为未归属"
            if not hits
            else f"quote 同时命中 {len(hits)} 个单元 {list(hits)}：归属有歧义 —— "
            "不猜，记为未归属（硬规则：宁可记账，不可编造归属）"
        )
        unattributed.append(
            UnattributedClaim(
                position=position,
                kind=str(getattr(claim, "kind", "") or ""),
                value=str(getattr(claim, "value", "") or ""),
                quote=quote,
                confidence=float(getattr(claim, "confidence", 0.0) or 0.0),
                status=status,
                detail=detail,
            )
        )
        report.append(
            Attribution(
                position=position,
                unit_index=None,
                status=status,
                matches=hits,
                detail=detail,
            )
        )
    attributed.sort(key=lambda item: (item.unit_index, item.order))
    return attributed, unattributed, tuple(report)


# --------------------------------------------------------------------------- #
# 一次调用的产物
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """一个批次（**不发起调用**，只描述"要给模型什么"）。"""

    raw_id: str
    units: Tuple[Unit, ...]
    batch_id: str
    batch_index: int
    batch_count: int
    batch_key: str
    instruction: str
    external_content: str

    @property
    def unit_ids(self) -> Tuple[str, ...]:
        return tuple(unit.unit_id for unit in self.units)

    @property
    def char_count(self) -> int:
        return sum(len(unit.text) for unit in self.units)


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """一次真实调用的完整观测（**含降级**：降级也是一次真实调用）。"""

    request: BatchRequest
    status: str
    reason: Optional[str]
    detail: str
    provider: str
    model: str
    response_model: str
    credential_route: str
    code_version: str
    config_version: str
    model_version: str
    input_digest: str
    idempotency_key: str
    elapsed_ms: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: Optional[int]
    cost_total: Optional[float]
    tools_declared: int
    tool_calls: int
    extracted_claims: Tuple[Any, ...] = ()
    attributed: Tuple[AttributedClaim, ...] = ()
    unattributed: Tuple[UnattributedClaim, ...] = ()
    attribution: Tuple[Attribution, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == CLAIM_STATUS_CLASSIFIED

    @property
    def degraded(self) -> bool:
        return self.status == CLAIM_STATUS_UNCLASSIFIED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.request.batch_id,
            "batch_index": self.request.batch_index,
            "batch_count": self.request.batch_count,
            "raw_id": self.request.raw_id,
            "units": len(self.request.units),
            "chars": self.request.char_count,
            "status": self.status,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "response_model": self.response_model,
            "elapsed_ms": self.elapsed_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_total": self.cost_total,
            "extracted_claims": len(self.extracted_claims),
            "attributed_claims": len(self.attributed),
            "unattributed_claims": len(self.unattributed),
            "tools_declared": self.tools_declared,
            "tool_calls": self.tool_calls,
        }


@dataclass(frozen=True, slots=True)
class OutcomeCounters:
    """一次提议运行的**对账数字**（硬规则 1：完成要用数字证明）。"""

    units_seen: int = 0
    units_skipped_already_run: int = 0
    units_run: int = 0
    batches: int = 0
    retries: int = 0
    units_deferred_to_retry: int = 0
    units_retry_exhausted: int = 0
    calls_ok: int = 0
    calls_degraded: int = 0
    extracted_claims: int = 0
    attributed_claims: int = 0
    unattributed_claims: int = 0
    classified_units: int = 0
    unclassified_units: int = 0
    rows_written: int = 0
    rows_unchanged: int = 0
    rows_out_of_space: int = 0
    runs_written: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    elapsed_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """一次提议运行的结果：数字 + 批次观测 + 落盘的行。

    两条**对账恒等式**在构造期强制（不满足即抛 —— 这是编码错误，不是数据问题）：

    ```
    attributed_claims + unattributed_claims == extracted_claims
    units_run == classified_units + unclassified_units
    ```
    """

    raw_ids: Tuple[str, ...]
    label_space: LabelSpace
    policy: ProposalPolicy
    versions: Mapping[str, str]
    counters: OutcomeCounters
    batches: Tuple[BatchOutcome, ...] = ()
    claims: Tuple[ProposedClaimRow, ...] = ()
    skipped: Tuple[Tuple[str, str, str], ...] = ()
    runs: Tuple[ProposalRunRow, ...] = ()

    def __post_init__(self) -> None:
        counters = self.counters
        if counters.attributed_claims + counters.unattributed_claims != (
            counters.extracted_claims
        ):
            raise ProposedStoreError(
                "对账不成立：attributed "
                f"{counters.attributed_claims} + unattributed "
                f"{counters.unattributed_claims} != extracted "
                f"{counters.extracted_claims}"
            )
        if counters.units_run != counters.classified_units + counters.unclassified_units:
            raise ProposedStoreError(
                "对账不成立：units_run "
                f"{counters.units_run} != classified {counters.classified_units} + "
                f"unclassified {counters.unclassified_units}"
            )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_ids": list(self.raw_ids),
            "label_space": self.label_space.as_dict(),
            "policy": self.policy.as_dict(),
            "versions": dict(self.versions),
            "counters": self.counters.as_dict(),
            "batches": [batch.as_dict() for batch in self.batches],
            "skipped": [
                {"raw_id": raw_id, "reason": reason, "detail": detail}
                for raw_id, reason, detail in self.skipped
            ],
        }

    def summary(self) -> str:
        """人类可读的一行对账（运维输出；`render_report` 类静默失效的教训）。"""
        counters = self.counters
        return (
            f"单元 {counters.units_seen}（跳过已跑 {counters.units_skipped_already_run} / "
            f"实跑 {counters.units_run}）→ 批次 {counters.batches}"
            f"（ok {counters.calls_ok} / 降级 {counters.calls_degraded}）"
            f"｜claim 抽出 {counters.extracted_claims} = 归属 {counters.attributed_claims}"
            f" + 未归属 {counters.unattributed_claims}"
            f"｜单元分类 {counters.classified_units} / 未分类 {counters.unclassified_units}"
            f"｜落盘行 {counters.rows_written}（无变化 {counters.rows_unchanged}）"
            f"｜token in {counters.input_tokens} out {counters.output_tokens}"
            f"｜{counters.elapsed_ms} ms"
        )


# --------------------------------------------------------------------------- #
# 构建批次
# --------------------------------------------------------------------------- #


def build_batches(
    raw_id: str,
    units: Sequence[Unit],
    *,
    policy: ProposalPolicy,
) -> Tuple[BatchRequest, ...]:
    """把一个 raw 的单元切成批次请求（纯函数、确定性）。"""
    if not units:
        return ()
    groups = plan_batches(
        units,
        max_units=policy.max_units_per_call,
        max_chars=policy.max_chars_per_call,
    )
    requests: List[BatchRequest] = []
    for index, group in enumerate(groups):
        ids = tuple(unit.unit_id for unit in group)
        requests.append(
            BatchRequest(
                raw_id=raw_id,
                units=group,
                batch_id=batch_id_for(raw_id, ids),
                batch_index=index,
                batch_count=len(groups),
                batch_key=batch_key_for(raw_id, ids),
                instruction=build_batch_instruction(len(group)),
                external_content=build_batch_content(group),
            )
        )
    return tuple(requests)


# --------------------------------------------------------------------------- #
# 发起调用（唯一与 `CognitionPort` 接触的地方）
# --------------------------------------------------------------------------- #


def run_batch(
    request: BatchRequest,
    *,
    port: CognitionPortLike,
    label_space: LabelSpace,
    policy: ProposalPolicy,
) -> BatchOutcome:
    """对一批单元做**一次**认知层调用，并把结果归属到单元。

    端口返回 `unclassified` ⇒ 本函数**不抛**，而是产出一个降级 `BatchOutcome`
    （`attributed` 为空、`reason` 非空）。**降级是一等状态，不是异常**（§2.14 决策四）。

    端口抛异常 ⇒ **原样向上抛**（契约/环境问题响亮失败，硬规则 2）。
    """
    # 延迟 import：本模块只依赖端口的**形状**，真正构造请求对象时才需要 T-003 的类型。
    from .contracts import CognitionRequest

    cognition_request = CognitionRequest(
        raw_id=request.raw_id,
        external_content=request.external_content,
        instruction=request.instruction,
        candidate_labels=label_space.labels,
        kind=policy.kind,
    )
    result = port.extract(cognition_request)
    record = result.record

    base: Dict[str, Any] = {
        "request": request,
        "provider": str(getattr(record, "provider", "") or ""),
        "model": str(getattr(record, "model", "") or ""),
        "response_model": str(getattr(record, "response_model", "") or ""),
        "credential_route": str(getattr(record, "credential_route", "") or ""),
        "code_version": str(getattr(record, "code_version", "") or ""),
        "config_version": str(getattr(record, "config_version", "") or ""),
        "model_version": str(getattr(record, "model_version", "") or ""),
        "input_digest": str(getattr(record, "input_digest", "") or cognition_request.digest()),
        "idempotency_key": str(getattr(record, "idempotency_key", "") or ""),
        "elapsed_ms": int(getattr(record, "elapsed_ms", 0) or 0),
        "tools_declared": int(getattr(record, "tools_declared", 0) or 0),
        "tool_calls": int(getattr(record, "tool_calls", 0) or 0),
    }
    usage = getattr(record, "usage", None)
    base.update(
        {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
            "cost_total": getattr(usage, "cost_total", None),
        }
    )

    status = getattr(result, "status", None)
    is_unclassified = bool(getattr(result, "is_unclassified", False))
    if is_unclassified:
        reason = getattr(getattr(result, "reason", None), "value", None) or "unknown"
        return BatchOutcome(
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason=str(reason),
            detail=str(getattr(record, "detail", "") or ""),
            **base,
        )

    claims = tuple(getattr(result, "claims", ()) or ())
    attributed, unattributed, report = attribute_claims(request.units, claims)
    return BatchOutcome(
        status=CLAIM_STATUS_CLASSIFIED,
        reason=None,
        detail=str(getattr(record, "detail", "") or ""),
        extracted_claims=claims,
        attributed=tuple(attributed),
        unattributed=tuple(unattributed),
        attribution=report,
        **base,
    )


# --------------------------------------------------------------------------- #
# 提议主体
# --------------------------------------------------------------------------- #


def _versions_of(port: CognitionPortLike) -> Dict[str, str]:
    """从端口配置取版本三元组（SPEC §3）。端口没有配置就响亮失败。"""
    config = getattr(port, "config", None)
    if config is None:
        raise ProposedStoreError(
            "端口没有 `config`：无法取得 (code_version, config_version, model_version)，"
            "而 SPEC §3 要求每次产出都携带版本三元组 —— 响亮失败，不填默认值"
        )
    versions = config.versions()
    return {
        "code_version": str(versions["code_version"]),
        "config_version": str(versions["config_version"]),
        "model_version": str(versions["model_version"]),
    }


def _label_version(label_space: LabelSpace) -> str:
    return label_space_version(label_space)


def _is_retryable(reason: str) -> bool:
    """这个降级原因值不值得重试。

    实测依据（`tools/t105_real_evidence.py` / 本任务的调参探针）：`deepseek-flash`
    是**推理型**模型，推理 token 常常吃掉整个输出预算，于是同一份输入有时给出完整
    JSON、有时给出 `empty_completion` / `unparseable_output`。这类失败是**瞬时**的，
    重试有意义（判据 12 的实测成功率会打印出来）。

    反过来 `model_deprecated` 重试一万次也是 404 —— **不重试**，只记账（否则烧钱）。
    """
    if reason in PERMANENT_REASONS:
        return False
    return reason in RETRYABLE_REASONS


def propose_units(
    raw_id: str,
    units: Sequence[Unit],
    *,
    label_space: LabelSpace,
    port: CognitionPortLike,
    store: SqliteProposedStore,
    policy: Optional[ProposalPolicy] = None,
) -> ProposalOutcome:
    """一个 raw 的单元 → 批次调用 → 归属 → 写入 `proposed_claims`（一次跑完）。

    **不重复调用模型**（§3 幂等）：先按 `plan_digest` 查 `proposal_runs`。

    | 上次的状态 | 这次怎么办 |
    |---|---|
    | `classified` | **跳过**（已经分好了，不烧钱） |
    | `unclassified` 且原因**不可重试**（如模型下架） | **跳过**（重试也是同一个结果） |
    | `unclassified` 且原因**可重试**（超时 / 空输出 / 无法解析 / 不可达） | **重试**，直到 `policy.max_retries` 用完 |
    | 从未跑过 | 跑 |

    重试时批次上限**逐轮减半**直到 1（`policy.units_for_attempt`）—— 实测依据：
    一批塞得越多，单次调用的输出越容易被推理 token 占满而拿不到 JSON。
    """
    policy = policy or ProposalPolicy()
    versions = _versions_of(port)
    label_version = _label_version(label_space)
    rows: List[ProposedClaimRow] = []
    runs: List[ProposalRunRow] = []
    batches: List[BatchOutcome] = []
    counters = {
        "units_seen": len(units),
        "units_skipped_already_run": 0,
        "units_run": 0,
        "batches": 0,
        "retries": 0,
        "units_deferred_to_retry": 0,
        "units_retry_exhausted": 0,
        "calls_ok": 0,
        "calls_degraded": 0,
        "extracted_claims": 0,
        "attributed_claims": 0,
        "unattributed_claims": 0,
        "classified_units": 0,
        "unclassified_units": 0,
        "rows_written": 0,
        "rows_unchanged": 0,
        "rows_out_of_space": 0,
        "runs_written": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "elapsed_ms": 0,
    }

    plan_digests = {
        unit.unit_id: plan_digest_for(
            unit_digest=unit.unit_digest,
            code_version=versions["code_version"],
            config_version=versions["config_version"],
            model_version=versions["model_version"],
            label_space_version=label_version,
        )
        for unit in units
    }
    previous = store.planned_state(plan_digests)
    pending: List[Unit] = []
    retry_counts: Dict[str, int] = {}
    for unit in units:
        run = previous.get(unit.unit_id)
        if run is None:
            retry_counts[unit.unit_id] = 0
            pending.append(unit)
            continue
        if run.status == CLAIM_STATUS_CLASSIFIED:
            counters["units_skipped_already_run"] += 1  # 上次就成了
            continue
        reason = run.reason or ""
        attempt = int(run.retry_count) + 1
        if not _is_retryable(reason) or attempt > policy.max_retries:
            counters["units_skipped_already_run"] += 1  # 不可重试 / 重试用完
            continue
        retry_counts[unit.unit_id] = attempt
        pending.append(unit)

    # `units_run` 是"**这个单元**被跑过"的计数（重试不算第二次，否则对账恒等式
    # `units_run == classified + unclassified` 会被重试次数撑破）。批次与调用的计数
    # 记在 `batches` / `retries` 里，两者互不干扰。
    counted: set = set()

    # 轮次循环：0 = 首次，1..max_retries = 重试（批次上限逐轮收缩）。
    attempt_index = min((retry_counts.get(u.unit_id, 0) for u in pending), default=0)
    round_limit = policy.max_retries
    allow_wrapup = False
    guard = 0
    while pending and (allow_wrapup or attempt_index <= round_limit):
        guard += 1
        if guard > round_limit + 2:  # pragma: no cover - 防御：轮次循环必须有界
            raise ProposedStoreError(
                f"重试轮次超过上限（{round_limit}）：pending={len(pending)}"
                " —— 轮次循环的逻辑被改坏了（否则会无限重试并烧钱）"
            )
        batch_units = policy.units_for_attempt(attempt_index)
        failed: List[Unit] = []
        round_requests = build_batches(
            raw_id,
            pending,
            policy=ProposalPolicy(
                max_units_per_call=batch_units,
                max_chars_per_call=policy.max_chars_per_call,
                kind=policy.kind,
                include_link=policy.include_link,
                source=policy.source,
                max_retries=policy.max_retries,
                retry_max_units_per_call=policy.retry_max_units_per_call,
                retry_shrink=policy.retry_shrink,
            ),
        )
        for request in round_requests:
            outcome = run_batch(
                request, port=port, label_space=label_space, policy=policy
            )
            batches.append(outcome)
            counters["batches"] += 1
            for unit in request.units:
                if unit.unit_id not in counted:
                    counted.add(unit.unit_id)
                    counters["units_run"] += 1
            counters["extracted_claims"] += len(outcome.extracted_claims)
            counters["attributed_claims"] += len(outcome.attributed)
            counters["unattributed_claims"] += len(outcome.unattributed)
            counters["input_tokens"] += outcome.input_tokens
            counters["output_tokens"] += outcome.output_tokens
            counters["reasoning_tokens"] += int(outcome.reasoning_tokens or 0)
            counters["elapsed_ms"] += outcome.elapsed_ms
            if outcome.ok:
                counters["calls_ok"] += 1
            else:
                counters["calls_degraded"] += 1

            by_unit: Dict[int, List[AttributedClaim]] = {}
            for claim in outcome.attributed:
                by_unit.setdefault(claim.unit_index, []).append(claim)

            for index, unit in enumerate(request.units):
                plan_digest = plan_digests[unit.unit_id]
                attempt = retry_counts.get(unit.unit_id, 0)
                unit_claims = by_unit.get(index, [])
                classified_rows: List[ProposedClaimRow] = []
                audit_rows: List[ProposedClaimRow] = []
                for order, claim in enumerate(unit_claims):
                    row, is_classified = _claim_row(
                        unit=unit,
                        claim=claim,
                        order=order,
                        outcome=outcome,
                        policy=policy,
                        label_space=label_space,
                        versions=versions,
                        label_version=label_version,
                        plan_digest=plan_digest,
                        retry_count=attempt,
                    )
                    (classified_rows if is_classified else audit_rows).append(row)

                for row in classified_rows + audit_rows:
                    stored, written = store.record_claim(row)
                    rows.append(stored)
                    counters["rows_written" if written else "rows_unchanged"] += 1
                    if not row.is_classified:
                        counters["rows_out_of_space"] += 1

                if classified_rows:
                    counters["classified_units"] += 1
                    runs.append(
                        _run_row(
                            unit=unit,
                            plan_digest=plan_digest,
                            status=CLAIM_STATUS_CLASSIFIED,
                            reason=None,
                            outcome=outcome,
                            versions=versions,
                            label_version=label_version,
                            retry_count=attempt,
                        )
                    )
                    continue

                reason = (
                    REASON_LABEL_OUT_OF_SPACE
                    if audit_rows
                    else (outcome.reason or REASON_NO_CLAIM)
                )
                # 可重试的降级**先不落库**：等重试用完再落（否则每轮都会写一行
                # "未分类"，把版本链撑成一串中间态）。`out_of_space` 与
                # `no_claim_extracted` 是**确定性的结论**（模型答了但没得用），
                # 它们不重试，直接落库。
                if audit_rows or reason == REASON_NO_CLAIM:
                    retryable = False
                else:
                    retryable = _is_retryable(reason)
                if retryable and attempt < policy.max_retries:
                    counters["units_deferred_to_retry"] += 1
                    failed.append(unit)
                    continue

                if retryable and attempt >= policy.max_retries:
                    counters["units_retry_exhausted"] += 1
                counters["unclassified_units"] += 1
                if audit_rows:
                    detail = (
                        f"模型为该单元返回的 {len(audit_rows)} 条 claim 的取值**全部**"
                        f"落在当前标签空间之外（{list(label_space.labels)}）："
                        "已逐条记为 out_of_space 审计行，但**不计入**分类结果"
                        "（SPEC §2.9 的 C8 闭环：候选标签只来自当前启用的行业配置）"
                    )
                elif outcome.reason:
                    detail = (
                        f"本批次降级（{outcome.reason}）：{outcome.detail}"
                        + (
                            f"；已在 {attempt} 次重试后放弃（瞬时失败的预算用尽）"
                            if attempt
                            else ""
                        )
                    )
                else:
                    detail = (
                        f"模型在本次调用里没有为该单元返回任何可归属的 claim"
                        f"（批次 {outcome.request.batch_id} 第 {index + 1} 个单元，"
                        f"该批共抽出 {len(outcome.extracted_claims)} 条 claim）"
                    )
                row = _unclassified_row(
                    unit=unit,
                    reason=reason,
                    detail=detail,
                    outcome=outcome,
                    policy=policy,
                    label_space=label_space,
                    versions=versions,
                    label_version=label_version,
                    plan_digest=plan_digest,
                    retry_count=attempt,
                )
                stored, written = store.record_claim(row)
                rows.append(stored)
                counters["rows_written" if written else "rows_unchanged"] += 1
                runs.append(
                    _run_row(
                        unit=unit,
                        plan_digest=plan_digest,
                        status=CLAIM_STATUS_UNCLASSIFIED,
                        reason=reason,
                        outcome=outcome,
                        versions=versions,
                        label_version=label_version,
                        retry_count=attempt,
                    )
                )

            # 未归属的 claim：**独立记账**，绝不静默丢弃、也不硬塞给某个单元。
            for claim in outcome.unattributed:
                row = _unattributed_row(
                    request=request,
                    claim=claim,
                    outcome=outcome,
                    policy=policy,
                    versions=versions,
                    label_version=label_version,
                    plan_digest=plan_digests[request.units[0].unit_id],
                )
                stored, written = store.record_claim(row)
                rows.append(stored)
                counters["rows_written" if written else "rows_unchanged"] += 1

        if not failed:
            break
        counters["retries"] += 1
        # 可重试的降级**先不落库**：等重试用完再落（否则每轮都会写一行"未分类"，
        # 把版本链撑成一串中间态）。下面是轮次推进规则。
        #
        # ⚠️ 被推迟的单元**必须在这里把 `retry_count` 加一**，否则下一轮会沿用同一
        # 个 attempt（批次上限不收缩、重试预算也用不完）—— 实测踩过这个坑：
        # 整批失败后第二轮仍是"整批"，单单元重试从未发生。
        for unit in failed:
            retry_counts[unit.unit_id] = retry_counts[unit.unit_id] + 1
        more = [unit for unit in failed if retry_counts[unit.unit_id] <= round_limit]
        if not more:
            # 预算用尽：**不能**把剩下的单元丢在 pending 里（那就是静默丢东西）。
            # 进入最后一轮"收尾"，让它们在下面的耗尽分支里落库（带原因码）。
            attempt_index = round_limit
            allow_wrapup = True
        else:
            allow_wrapup = False
            attempt_index = min(retry_counts[unit.unit_id] for unit in more)
        pending = failed

    for run in runs:
        _stored_run, written = store.record_run(run)
        if written:
            counters["runs_written"] += 1

    return ProposalOutcome(
        raw_ids=(raw_id,),
        label_space=label_space,
        policy=policy,
        versions=versions,
        counters=OutcomeCounters(**counters),
        batches=tuple(batches),
        claims=tuple(rows),
        runs=tuple(runs),
    )


def propose_document(
    plan: Any,
    *,
    label_space: LabelSpace,
    port: CognitionPortLike,
    store: SqliteProposedStore,
    policy: Optional[ProposalPolicy] = None,
) -> ProposalOutcome:
    """`DocumentPlan` → 提议。跳过的 raw **原样记账**（带理由码），不进模型。"""
    policy = policy or ProposalPolicy()
    if plan.skipped:
        return ProposalOutcome(
            raw_ids=(plan.raw_id,),
            label_space=label_space,
            policy=policy,
            versions=_versions_of(port),
            counters=OutcomeCounters(),
            skipped=((plan.raw_id, str(plan.skip_reason.value), plan.detail),),
        )
    outcome = propose_units(
        plan.raw_id,
        plan.units,
        label_space=label_space,
        port=port,
        store=store,
        policy=policy,
    )
    return outcome


def propose_documents(
    jobs: Sequence[Tuple[Any, Any]],
    *,
    label_space: LabelSpace,
    port: CognitionPortLike,
    store: SqliteProposedStore,
    policy: Optional[ProposalPolicy] = None,
) -> ProposalOutcome:
    """多篇一起跑：`jobs = [(record, raw_bytes), ...]`（`record` 需有 `raw_id`）。

    分流在**这里**发生（`classify_document`），跳过的 raw 进 `skipped` 记账。
    返回值是把各篇合并后的**一个**总账（对账恒等式仍然成立）。
    """
    policy = policy or ProposalPolicy()
    versions = _versions_of(port)
    total = OutcomeCounters()
    all_batches: List[BatchOutcome] = []
    all_claims: List[ProposedClaimRow] = []
    all_runs: List[ProposalRunRow] = []
    skipped: List[Tuple[str, str, str]] = []
    raw_ids: List[str] = []

    acc: Dict[str, int] = {
        key: 0 for key in OutcomeCounters.__dataclass_fields__
    }

    for record, raw_bytes in jobs:
        raw_id = str(record.raw_id)
        raw_ids.append(raw_id)
        plan = classify_document(
            raw_bytes,
            raw_id=raw_id,
            channel_id=str(getattr(record, "channel_id", "") or ""),
            endpoint=str(getattr(record, "endpoint", "") or ""),
        )
        outcome = propose_document(
            plan, label_space=label_space, port=port, store=store, policy=policy
        )
        skipped.extend(outcome.skipped)
        all_batches.extend(outcome.batches)
        all_claims.extend(outcome.claims)
        all_runs.extend(outcome.runs)
        for key in acc:
            acc[key] += getattr(outcome.counters, key)

    total = OutcomeCounters(**acc)
    return ProposalOutcome(
        raw_ids=tuple(raw_ids),
        label_space=label_space,
        policy=policy,
        versions=versions,
        counters=total,
        batches=tuple(all_batches),
        claims=tuple(all_claims),
        skipped=tuple(skipped),
        runs=tuple(all_runs),
    )


# --------------------------------------------------------------------------- #
# 行构造（一处定义，避免两处漂移）
# --------------------------------------------------------------------------- #


def _row_common(
    *,
    unit: Unit,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
    retry_count: int = 0,
) -> Dict[str, Any]:
    return {
        "raw_id": unit.raw_id,
        "unit_id": unit.unit_id,
        "unit_kind": unit.kind,
        "unit_char_start": unit.char_start,
        "unit_char_end": unit.char_end,
        "entry_index": unit.entry_index,
        "title": unit.title,
        "kind": policy.kind,
        "plan_digest": plan_digest,
        "code_version": versions["code_version"],
        "config_version": versions["config_version"],
        "model_version": versions["model_version"],
        "label_space_version": label_version,
        "input_digest": outcome.input_digest,
        "batch_id": outcome.request.batch_id,
        "batch_position": _position_of(outcome.request.units, unit.unit_id),
        "batch_size": len(outcome.request.units),
        "retry_count": retry_count,
        "provider": outcome.provider,
        "model": outcome.model,
        "credential_route": outcome.credential_route,
        "source": policy.source,
    }


def _position_of(units: Sequence[Unit], unit_id: str) -> int:
    for index, unit in enumerate(units):
        if unit.unit_id == unit_id:
            return index
    raise ProposedStoreError(f"单元 {unit_id} 不在批次里：批次与单元列表不一致")


def _classified_row(
    *,
    unit: Unit,
    claim: AttributedClaim,
    order: int,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    label_space: LabelSpace,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
    retry_count: int = 0,
) -> ProposedClaimRow:
    if claim.value not in label_space.labels:
        # 标签空间来自配置（SPEC §2.5 / §2.9 闭环）。模型给出空间外的取值 ⇒
        # **不是**"猜"，而是模型违反了当前配置的标签空间：写成一条 `out_of_space`
        # 审计行（值留着可审计、**不计入**分类结果），而**不是**静默丢弃、也**不是**
        # 把它当分类结果写进去（那等于让闭环断掉）。
        return _out_of_space_row(
            unit=unit,
            claim=claim,
            outcome=outcome,
            policy=policy,
            label_space=label_space,
            versions=versions,
            label_version=label_version,
            plan_digest=plan_digest,
        )
    output_digest = output_digest_for(
        value=claim.value,
        quote=claim.quote,
        confidence=claim.confidence,
        status=CLAIM_STATUS_CLASSIFIED,
        reason=None,
    )
    return ProposedClaimRow(
        value=claim.value,
        quote=claim.quote,
        confidence=claim.confidence,
        status=CLAIM_STATUS_CLASSIFIED,
        reason=None,
        output_digest=output_digest,
        detail=(
            f"批次 {outcome.request.batch_id} 第 {claim.unit_index + 1} 个单元的第 "
            f"{order + 1} 条 claim（quote 逐字命中该单元文本）"
        ),
        **_row_common(
            unit=unit,
            outcome=outcome,
            policy=policy,
            versions=versions,
            label_version=label_version,
            plan_digest=plan_digest,
            retry_count=retry_count,
        ),
    )


def _claim_key_for_out_of_space(
    *,
    raw_id: str,
    unit_id: str,
    kind: str,
    value: str,
) -> str:
    """标签空间外审计行的行身份（与 `store.claim_key_for` 同一公式）。"""
    return claim_key_for(
        raw_id=raw_id,
        unit_id=unit_id,
        kind=kind,
        value=value,
        quote=None,
        status=CLAIM_STATUS_OUT_OF_SPACE,
        reason=REASON_LABEL_OUT_OF_SPACE,
    )


def _claim_row(
    *,
    unit: Unit,
    claim: AttributedClaim,
    order: int,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    label_space: LabelSpace,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
    retry_count: int = 0,
) -> Tuple[ProposedClaimRow, bool]:
    """一条已归属的 claim → 一行。返回 `(行, 是否是分类结果)`。

    `value` 不在注入的标签空间里 ⇒ 产出的是 `out_of_space` **审计行**
    （`False`）：它记下模型说了什么，但**不算**分类结果（SPEC §2.9 的 C8 闭环）。
    """
    if claim.value in label_space.labels:
        return (
            _classified_row(
                unit=unit,
                claim=claim,
                order=order,
                outcome=outcome,
                policy=policy,
                label_space=label_space,
                versions=versions,
                label_version=label_version,
                plan_digest=plan_digest,
                retry_count=retry_count,
            ),
            True,
        )
    return (
        _out_of_space_row(
            unit=unit,
            claim=claim,
            outcome=outcome,
            policy=policy,
            label_space=label_space,
            versions=versions,
            label_version=label_version,
            plan_digest=plan_digest,
            retry_count=retry_count,
        ),
        False,
    )


def _out_of_space_row(
    *,
    unit: Unit,
    claim: AttributedClaim,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    label_space: LabelSpace,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
    retry_count: int = 0,
) -> ProposedClaimRow:
    """标签空间外的 claim 的**审计行**（可审计，但**不是**分类结果）。

    为什么不是"直接拒绝/抛错"：SPEC §2.9 的闭环要求候选标签只来自配置，
    但模型偶尔会给出空间外的取值 —— 那既不该被静默丢弃（丢证据），
    也不该被当作分类结果写进去（闭环断掉）。因此单列一种状态：

    - `value` 留着（可审计"模型当时说了什么"）；
    - `quote` / `confidence` 为空（这条引用**没有**被本层当作证据）；
    - `status = out_of_space`，`reason = label_out_of_space`；
    - **不计入** `classified`，也不计入 `unattributed`（它是第三条账）。
    """
    return ProposedClaimRow(
        value=claim.value,
        quote=None,
        confidence=None,
        status=CLAIM_STATUS_OUT_OF_SPACE,
        reason=REASON_LABEL_OUT_OF_SPACE,
        detail=(
            f"模型给出的 value={claim.value!r} 不在注入的标签空间里"
            f"（{list(label_space.labels)}）：SPEC §2.5/§2.9 的 C8 闭环要求候选标签"
            "集合只来自当前启用的行业配置 —— 记为审计行，不计入分类结果。"
            f"原 quote={claim.quote[:200]!r}"
        ),
        output_digest=output_digest_for(
            value=claim.value,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_OUT_OF_SPACE,
            reason=REASON_LABEL_OUT_OF_SPACE,
        ),
        **_row_common(
            unit=unit,
            outcome=outcome,
            policy=policy,
            versions=versions,
            label_version=label_version,
            plan_digest=plan_digest,
            retry_count=retry_count,
        ),
    )


def _unclassified_row(
    *,
    unit: Unit,
    reason: str,
    detail: str,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    label_space: LabelSpace,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
    retry_count: int = 0,
) -> ProposedClaimRow:
    if not reason:
        raise ProposedStoreError("未分类行必须有理由（§2.14 决策四）")
    return ProposedClaimRow(
        value=None,
        quote=None,
        confidence=None,
        status=CLAIM_STATUS_UNCLASSIFIED,
        reason=reason,
        detail=detail,
        output_digest=output_digest_for(
            value=None,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNCLASSIFIED,
            reason=reason,
        ),
        **_row_common(
            unit=unit,
            outcome=outcome,
            policy=policy,
            versions=versions,
            label_version=label_version,
            plan_digest=plan_digest,
            retry_count=retry_count,
        ),
    )


def _unattributed_row(
    *,
    request: BatchRequest,
    claim: UnattributedClaim,
    outcome: BatchOutcome,
    policy: ProposalPolicy,
    versions: Mapping[str, str],
    label_version: str,
    plan_digest: str,
) -> ProposedClaimRow:
    """归不到单元的 claim 的记账行。

    它的 `unit_id` 是 `unattributed:<batch_id>:<序号>` —— **刻意不是** `ent_`/`art_`：
    它不是一个单元。`unit_char_start/end` 取 `[0, 1)` 只是为了满足"区间非空"的表约束，
    **不代表任何真实位置**；真正的位置信息在这条行里**根本不存在**（这正是"归不到"的
    意思）。它是一条**审计记录**，不是证据。
    """
    synthetic_unit_id = f"{UNATTRIBUTED_UNIT_PREFIX}{request.batch_id}:{claim.position}"
    return ProposedClaimRow(
        raw_id=request.raw_id,
        unit_id=synthetic_unit_id,
        unit_kind="unattributed",
        unit_char_start=0,
        unit_char_end=1,
        kind=policy.kind,
        value=None,
        quote=None,
        confidence=None,
        status=CLAIM_STATUS_UNATTRIBUTED,
        reason=REASON_UNATTRIBUTED_QUOTE,
        detail=(
            f"{claim.detail}；原 claim：kind={claim.kind!r} value={claim.value!r} "
            f"quote={claim.quote[:200]!r} confidence={claim.confidence}"
        ),
        output_digest=output_digest_for(
            value=None,
            quote=None,
            confidence=None,
            status=CLAIM_STATUS_UNATTRIBUTED,
            reason=f"{REASON_UNATTRIBUTED_QUOTE}:{claim.status}:{claim.position}",
        ),
        entry_index=None,
        title="",
        plan_digest=plan_digest,
        code_version=versions["code_version"],
        config_version=versions["config_version"],
        model_version=versions["model_version"],
        label_space_version=label_version,
        input_digest=outcome.input_digest,
        batch_id=request.batch_id,
        batch_position=claim.position,
        batch_size=len(request.units),
        provider=outcome.provider,
        model=outcome.model,
        credential_route=outcome.credential_route,
        source=policy.source,
    )


def _run_row(
    *,
    unit: Unit,
    plan_digest: str,
    status: str,
    reason: Optional[str],
    outcome: BatchOutcome,
    versions: Mapping[str, str],
    label_version: str,
    retry_count: int = 0,
) -> ProposalRunRow:
    return ProposalRunRow.make(
        unit_id=unit.unit_id,
        raw_id=unit.raw_id,
        plan_digest=plan_digest,
        status=status,
        reason=reason,
        code_version=versions["code_version"],
        config_version=versions["config_version"],
        model_version=versions["model_version"],
        label_space_version=label_version,
        batch_id=outcome.request.batch_id,
        batch_size=len(outcome.request.units),
        retry_count=retry_count,
        calls=1,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        reasoning_tokens=outcome.reasoning_tokens,
        elapsed_ms=outcome.elapsed_ms,
        credential_route=outcome.credential_route,
        model=outcome.model,
    )
