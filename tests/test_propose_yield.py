"""T-105 产出率（yield）的**判据**与"低产出率"的诊断复核。

背景（SPEC §2.17 的实测表）：真实 store 上 T-105 的产出率是 **4/24 ≈ 17%**，
未分类 20 行 = `timeout` 13 + `no_claim_extracted` 7。§2.17 当时把成因归到
"推理 token 吃掉输出预算 ⇒ 长单元失败"，并据此把 `BATCH_MAX_UNITS / BATCH_MAX_CHARS`
定为 4 / 6000，`CognitionConfig.max_output_tokens` 默认 **2048**（该默认值已在
2026-09-28 由本次诊断改成 **4096**，依据见下面的对照表）。本文件把那句话
**变成可证伪的判据**，并钉住诊断的结论。实测数字由
`tools/t105_yield_probe.py` 在真实数据上产出（诊断脚本，不是文档）。

判据（**判据 Y1–Y7**；T-105 原有的判据 1–15 见 `tests/_t105_criteria.py`）
=====================================================================

**判据 Y1 —— 样本必须是确定性纯函数，且缺单元要响亮失败。**
`tools/t105_yield_probe.select_sample()` 的输入是"全库单元 + 运行账"，输出是样本；
同一输入两次 → 样本逐字段相同；输入顺序不影响结果；运行账里出现**全库单元里没有的
unit_id** ⇒ `KeyError`（**不**静默少测几个单元 —— 那会让"产出率"这个分母无声地变）。
活对照：同一份合法输入必须**成功**返回非空样本。

**判据 Y2 —— 提高 `max_output_tokens` 不会让已跑过的单元重跑。**
`plan_digest` 只含 `(unit_digest, code_version, config_version, model_version,
label_space_version)`，而 `config_version` 是**静态常量** `cognition-config/1`
（`CognitionConfig.versions()`），**不是** `public_digest()`（后者才含
`max_output_tokens` / `timeout_seconds` / `reasoning_enabled`）。
⇒ 只改输出预算，`plan_digest` **不变** ⇒ `proposal_runs` 里那一行仍然命中，
产出率诊断**不能**拿"已跑过的单元"当样本。
活对照：改 `model_version` 时 `plan_digest` **必须**变（证明这套摘要是"对配置敏感"的，
不是恒等函数）。

**判据 Y3 —— 重试预算用尽的可重试降级，第二次运行不调用模型。**
真实库里 13 行 `timeout` 的 `retry_count = 2` 而生产默认 `max_retries = 2`
⇒ 它们在**任何** token / 批量配置下都会被跳过。
活对照：同一调用路径在重试预算**未**用尽时**必须**调用模型。

**判据 Y4 —— SPEC §2.17 的常量与代码一致。**
`BATCH_MAX_UNITS == 4`、`BATCH_MAX_CHARS == 6000`、`UNIT_TEXT_MAX_CHARS == 2000`、
`CognitionConfig().max_output_tokens == 4096`、`DEFAULT_TIMEOUT_SECONDS == 60.0`。
SPEC §2.17 写明这些数字是"实测标定，不是拍的"、改它们**必须重跑**
`tools/t105_real_evidence.py`；本测试只能保证"代码没被无声改动"，不能代替重跑。
⚠️ `max_output_tokens` 的默认值已由 **2048 改成 4096**（2026-09-28），
依据是下面的诊断表；**SPEC §2.17 的正文里还写着 2048，需要主代理更新**（本任务不改 SPEC）。

**判据 Y5 —— 测量脚本不能写真实 store。**
AST 扫描 `tools/t105_yield_probe.py`：每一处 `SqliteProposedStore(...)` 的
`db_path` 都不得指向 `REAL_DB`（`data/store/atlas.db`）。
活对照：扫描**必须**至少找到一处构造 —— 否则"没找到"会伪装成"通过"
（与判据 14 的 AST 扫描同一形状，硬规则 4 的"否定断言要有活对照"）。

**判据 Y6 —— 只读快照可重复（真实 store 缺 `data/` 时自跳过）。**
连续两次 `read_only_snapshot()` 逐字段相同；且 `data/store/raw` 的树摘要不变。

**判据 Y7 —— 配置的输出预算真的进了 HTTP 请求体。**
用本地 mock 服务器 + **真实边车进程**发起调用，断言发出去的请求体里
`max_completion_tokens` 等于 `CognitionConfig.max_output_tokens`。
为什么必须有这条：本项目的注释里**曾经**断言"这个字段没有被 adapter 送进边车"，
那是**假的**，并直接把 SPEC §2.17 的标定带偏了一轮（见 `classify.py` 的更正记录）。
注释会漂移，请求体不会。
活对照：两个**不同**的配置值必须给出两个不同的字段值（否则"字段在"可能只是硬编码）。

---

诊断结论（**由 `tools/t105_yield_probe.py` 的真实测量得出，不是推断**）
====================================================================

**只读复核**（真实 `data/store/atlas.db`，`mode=ro&immutable=1`，一行都没写）：

| 观测 | 数字 |
|---|---|
| 13 行 `timeout` 的调用 | `input_tokens = 0`、`output_tokens = 0`、`reasoning_tokens = NULL`、`elapsed_ms = 11367–11551`（13 行互差 **< 200 ms**），落在 **04:56:31–04:58:50** 这段连续 2.5 分钟里 ⇒ **传输层**失败（请求根本没到模型） |
| 7 行 `no_claim_extracted` 的调用 | `output_tokens = 49 / 96 / 120 / 129 / 232 / 796 …`，`reasoning_tokens` 占 79–96%，**内容部分恒约 17 token**（空 `claims` 数组的量级）⇒ 模型**答了**，只是没有可归属的取值 |
| 4 行 `classified` 的调用 | `output_tokens = 413 / 476 / 1099` |

**实验**（`tools/t105_yield_probe.py`，2026-09-28；25 个单元的**确定性**样本 =
真实库 20 个未分类单元 + 从未跑过的单元里按 `(len(text), unit_id)` 分位取的 5 个；
**每臂一个临时库**，因此运行账为空 ⇒ 这 20 个单元真的会重跑，不被 `planned_state` 跳过）：

| arm | 单元/批 | 重试 | 预算 | **单元产出率** | 调用 | 顶到上限的调用 | `empty_completion` | `no_claim_extracted` | reasoning/output | 墙钟 |
|---|---|---|---|---|---|---|---|---|---|---|
| `b1_tok2048` | 1 | 0 | 2048 | **10/25 = 40.0%** | 25 | **4** | 4 | 11 | 92.2% | 236.6 s |
| `b1_tok4096` | 1 | 0 | 4096 | **14/25 = 56.0%** | 25 | **0** | 0 | 11 | 88.6% | 301.8 s |
| `b1_tok8192` | 1 | 0 | 8192 | **15/25 = 60.0%** | 25 | 0 | 0 | 10 | 87.8% | 301.8 s |
| `b4_tok2048` | 4 | 0 | 2048 | **14/25 = 56.0%** | 14 | 2 | 6 | 5 | 86.0% | 192.9 s |
| `default_retry2`（SPEC 默认） | 4 | 2 | 2048 | **14/25 = 56.0%** | 23 | 4 | **0** | 11 | 91.0% | 292.4 s |

`b1_tok4096` 在独立的第二次运行里复现为 **14/25 = 56.0%**（同样那 11 个单元未分类）。

**标签空间诊断**（同一批 20 个未分类单元，单单元 / 4096 / 不重试，
标签空间从注册表的 4 个**加上** 4 个通用标签）：

| 标签空间 | 产出率 | 失败原因 |
|---|---|---|
| 4 个（`computer-vision / machine-learning / natural-language / statistical-learning`） | **9/20 = 45%** | 11 × `no_claim_extracted` |
| 8 个（+ `data-science / software-engineering / business-and-markets / other`） | **20/20 = 100%** | 无 |

那 11 个单元在 8 标签下拿到的取值是 **`software-engineering` / `data-science` /
`business-and-markets`** 这些**语义上确实成立**的标签，**`other` 一次都没被用过**
⇒ 失败原因不是"模型没话可说"，而是**当前 4 个标签里确实没有能装下它们的标签**。

⇒ 三条假设的判定：

- **(a) 输出预算 —— 成立，是原因之一。** 2048 下 **10/62** 次调用被截断
  （`output_tokens == reasoning_tokens == 2048`、`stopReason=length`、内容为 0
  ⇒ 降级 `empty_completion`）；4096 下 **0/50**；8192 下 **0/25**。
  单元产出率 **40% → 56%**。⇒ `CognitionConfig.max_output_tokens` 默认值
  **2048 → 4096**（取"实测零截断的最小值"；8192 只多 1/25 个单元，且 4096 复现为
  14/25，**不构成差异**）。
- **(b) 批量/字符 —— 不成立，常量不动。** 同样 2048 下 `b4_tok2048` 的产出率
  **不低于** `b1_tok2048`（14/25 vs 10/25），而调用数只有 14 次（vs 25）。
  批量确实会把一次截断放大成 4 个单元（2 次截断吃掉 6 个单元），
  但生产默认的**重试**把 4/4 截断全救回来了（`default_retry2` 最终 0 个
  `empty_completion`）。⇒ `BATCH_MAX_UNITS / BATCH_MAX_CHARS` **不改**。
- **(c) 瞬时失败 —— 对原来那 13 行成立，但已不是当前瓶颈。** 那一组是
  0 token + 11.4 s 恒定 + 2.5 分钟时间窗的**传输层**失败；本次 **108 次真实调用里
  0 次重现**（所有臂的 0-token 调用数都是 0）。
  把 13 行剔除后，真实库"真的到过模型"的产出率是 **4/11 ≈ 36%**，
  与 2048 下实测的 40% 同量级 ⇒ **原来的 17% 主要是瞬时的**。
- **(d) 其它 —— 现在最大的瓶颈是 `no_claim_extracted`（11/25 = 44%），
  根因是标签空间只有 4 个研究向标签。** 这不是 T-105 代码的缺陷：
  SPEC §2.5 / §2.9 的 C8 闭环**要求**候选标签只来自当前启用的行业配置。
  可执行的结论在**注册表配置**一侧（启用更多行业），而不是本模块。
  顺带一条有用的机制事实：`label_space_version` **在** `plan_digest` 里，
  因此"扩标签空间"这一次配置变更**会**给所有单元产生新计划
  ⇒ 那 20 行卡住的单元会随之被重跑（而只改 `max_output_tokens` **不会**，见判据 Y2）。

**残留风险（如实记下，不粉饰）**：4096 不是"永不再截断"——
在**加宽后的** 8 标签空间下仍观测到 **1/40** 次截断（另一次 20 个单元的运行是 0/20，
最大一次 output 3977 已经贴着 4096）。之所以仍取 4096：这个失败模式是
`empty_completion`，**可重试**，而实测的重试把 4/4 全部转成了分类。
若将来真的扩了标签空间，应当**重跑** `tools/t105_yield_probe.py` 再决定要不要抬到 8192。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from atlas.cognition import (  # noqa: E402
    BATCH_MAX_CHARS,
    BATCH_MAX_UNITS,
    UNIT_TEXT_MAX_CHARS,
    CallStatus,
    CognitionCallRecord,
    CognitionConfig,
    CognitionOutput,
    CognitionRequest,
    CognitionResult,
    CognitionUsage,
    DEFAULT_TIMEOUT_SECONDS,
    DegradeReason,
    ExtractedClaim,
    LabelSpace,
    ProposalPolicy,
    SqliteProposedStore,
    classify_document,
    label_space_version,
    plan_digest_for,
    propose_units,
)
from tests.test_cognition_support import (  # noqa: E402
    MockServer,
    mock_port,
    require_sidecar_installed,
)
from tools import t105_yield_probe  # noqa: E402

LABELS = ("computer-vision", "machine-learning", "natural-language", "statistical-learning")

FEED_BYTES = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Transformer scaling</title><link>https://example.com/a</link>
<description><![CDATA[<p>transformer scaling for language models</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
<item><title>Segmentation</title><link>https://example.com/b</link>
<description><![CDATA[<p>semantic image segmentation</p>]]></description>
<pubDate>Mon, 01 Jan 2024 00:00:00 +0000</pubDate></item>
</channel></rss>
"""


def _claim(value: str, quote: str, confidence: float = 0.9) -> ExtractedClaim:
    return ExtractedClaim(kind="industry", value=value, quote=quote, confidence=confidence)


class ScriptedPort:
    """哑端口：按脚本返回**真实契约对象**（不联网、不启动边车）。

    `script` 每项是 `(claims, reason)`：`reason is None` ⇒ `ok`，否则 ⇒
    `unclassified` + 真实 `DegradeReason`。它记录每次收到的请求，因此可以断言
    "到底调用了几次" —— 判据 Y3 的"不调用模型"与它的活对照都靠这个计数。
    """

    def __init__(
        self,
        script: Optional[Sequence[Tuple[Optional[Sequence[ExtractedClaim]], Any]]] = None,
        *,
        config: Optional[CognitionConfig] = None,
    ) -> None:
        self.config = config or CognitionConfig(
            provider="scripted",
            model="scripted-model",
            base_url="http://127.0.0.1:1/v1",
            route_name="scripted",
            api_key="not-a-secret",
            model_version="scripted-model",
        )
        self.script: List[Tuple[Optional[Sequence[ExtractedClaim]], Any]] = list(script or [])
        self.requests: List[Any] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def _record(self, request: Any, status: CallStatus, reason: Optional[DegradeReason]) -> CognitionCallRecord:
        return CognitionCallRecord(
            status=status,
            reason=reason,
            detail="scripted",
            provider=self.config.provider,
            model=self.config.model,
            response_model=self.config.model,
            credential_route=self.config.credential_route(),
            sidecar_code_version="sha256:" + "0" * 64,
            code_version=self.config.prompt_version,
            config_version=self.config.config_version,
            model_version=self.config.model_version,
            config_digest=self.config.public_digest(),
            idempotency_key="k" * 64,
            input_digest=request.digest(),
            elapsed_ms=1,
            usage=CognitionUsage(input_tokens=1, output_tokens=1, reasoning_tokens=0),
            parse_strategy="direct",
            degraded=status is CallStatus.UNCLASSIFIED,
            tool_calls=0,
            tools_declared=0,
            thinking_chars=0,
        )

    def extract(self, request: Any) -> CognitionResult:
        self.requests.append(request)
        if not self.script:
            return CognitionResult(
                record=self._record(request, CallStatus.OK, None),
                output=CognitionOutput(claims=[]),
            )
        claims, reason = self.script.pop(0)
        if reason is not None:
            return CognitionResult(
                record=self._record(request, CallStatus.UNCLASSIFIED, reason), output=None
            )
        return CognitionResult(
            record=self._record(request, CallStatus.OK, None),
            output=CognitionOutput(claims=list(claims or ())),
        )


class _Unit:
    """`select_sample` 只用到 `unit_id` / `text` 两个属性。"""

    def __init__(self, unit_id: str, text: str) -> None:
        self.unit_id = unit_id
        self.text = text


@pytest.fixture()
def space() -> LabelSpace:
    return LabelSpace.of(LABELS, config_version="cfg/1")


# =========================================================================== #
# 判据 Y1：样本是确定性纯函数（可复跑的前提）
# =========================================================================== #


def test_select_sample_is_deterministic_and_order_insensitive() -> None:
    """**判据 Y1**：同输入 → 同样本；输入顺序不影响结果。"""
    units = {
        f"ent_{index:032x}": _Unit(f"ent_{index:032x}", "x" * (10 + index))
        for index in range(10)
    }
    run_rows = [
        {"unit_id": "ent_" + "0" * 31 + "3", "status": "unclassified", "reason": "timeout"},
        {"unit_id": "ent_" + "0" * 31 + "7", "status": "classified", "reason": None},
    ]

    first = t105_yield_probe.select_sample(units, run_rows, strata=3)
    second = t105_yield_probe.select_sample(dict(reversed(list(units.items()))), list(reversed(run_rows)), strata=3)

    assert [unit.unit_id for unit in first[0]] == [unit.unit_id for unit in second[0]]
    assert [unit.unit_id for unit in first[1]] == [unit.unit_id for unit in second[1]]
    # 失败组 = 运行账里 status=unclassified 的单元（升序）；分层组来自从未跑过的单元
    assert [unit.unit_id for unit in first[0]] == ["ent_" + "0" * 31 + "3"]
    assert len(first[1]) == 3
    assert all(unit.unit_id != "ent_" + "0" * 31 + "7" for unit in first[1]), (
        "已跑过的单元（无论成功失败）都不得进入『从未跑过』的分层组；"
        "失败组是**另一组**，它按设计来自运行账"
    )
    assert len(first[1]) + len(first[0]) == 4
    # 分层组必须跨越长度区间（最短、中位、最长都在里面）
    lengths = [len(unit.text) for unit in first[1]]
    assert lengths == sorted(lengths)
    assert lengths[0] == 10 and lengths[-1] == 19, f"分层没跨到两端：{lengths}"


def test_select_sample_fails_loudly_on_unknown_unit_id() -> None:
    """**判据 Y1 的否定断言 + 活对照**：运行账里的 unit_id 不在全库单元里 ⇒ 响亮失败。

    活对照（同一调用路径、合法输入）必须**成功** —— 否则 `TypeError` / 签名不匹配
    会伪装成"拒绝成功"（硬规则 4）。
    """
    units = {"ent_" + "0" * 32: _Unit("ent_" + "0" * 32, "hello")}
    good = [{"unit_id": "ent_" + "0" * 32, "status": "unclassified", "reason": "timeout"}]
    fail, fresh = t105_yield_probe.select_sample(units, good, strata=1)
    assert [unit.unit_id for unit in fail] == ["ent_" + "0" * 32]
    assert fresh == ()

    bad = [{"unit_id": "ent_" + "f" * 32, "status": "unclassified", "reason": "timeout"}]
    with pytest.raises(KeyError):
        t105_yield_probe.select_sample(units, bad, strata=1)


# =========================================================================== #
# 判据 Y2：输出预算不进计划摘要 ⇒ 提高预算不会让旧结果重跑
# =========================================================================== #


def _digest_for(config: CognitionConfig, unit: Any, space: LabelSpace) -> str:
    versions = config.versions()
    return plan_digest_for(
        unit_digest=unit.unit_digest,
        code_version=versions["code_version"],
        config_version=versions["config_version"],
        model_version=versions["model_version"],
        label_space_version=label_space_version(space),
    )


def test_output_budget_is_not_part_of_the_plan_digest(space) -> None:
    """**判据 Y2**：改 `max_output_tokens` / `timeout_seconds` ⇒ `plan_digest` **不变**。

    这正是"提高预算**不会**重跑已失败单元"的机制：`proposal_runs` 的行按
    `(unit_id, plan_digest)` 命中，摘要没变 ⇒ 跳过。
    **活对照**：改 `model_version` 时摘要**必须**变（否则摘要就是个恒等函数，
    上面那条"不变"什么也没证明）。
    """
    plan = classify_document(FEED_BYTES, raw_id="raw_feed")
    unit = plan.units[0]
    base = CognitionConfig(
        provider="scripted",
        model="scripted-model",
        base_url="http://127.0.0.1:1/v1",
        route_name="scripted",
        api_key="not-a-secret",
        model_version="scripted-model",
    )

    digest_2048 = _digest_for(base.with_overrides(max_output_tokens=2048), unit, space)
    digest_8192 = _digest_for(base.with_overrides(max_output_tokens=8192), unit, space)
    digest_timeout = _digest_for(base.with_overrides(timeout_seconds=120.0), unit, space)
    digest_reasoning = _digest_for(base.with_overrides(reasoning_enabled=True), unit, space)
    assert digest_8192 == digest_2048
    assert digest_timeout == digest_2048
    assert digest_reasoning == digest_2048

    # 活对照：换模型 ⇒ 摘要必须变（配置指纹是活的）
    digest_model = _digest_for(base.with_overrides(model_version="other-model"), unit, space)
    assert digest_model != digest_2048, "换 model_version 都没让 plan_digest 变，摘要不可信"

    # 两个摘要的**来源**确实不同：public_digest 对预算敏感，plan_digest 的输入不敏感
    assert base.with_overrides(max_output_tokens=8192).public_digest() != base.public_digest()
    print(
        f"\n[Y2] plan_digest(2048)={digest_2048[:16]}… "
        f"plan_digest(8192)={digest_8192[:16]}… 相同 ⇒ 提高输出预算不产生新计划"
    )


# =========================================================================== #
# 判据 Y3：重试预算用尽 ⇒ 第二次运行不调用模型（活对照：未用尽时必须调用）
# =========================================================================== #


def test_exhausted_retry_budget_skips_the_unit_without_calling_the_model(
    tmp_path, space
) -> None:
    """**判据 Y3**：`max_retries=0` 跑出一次 `timeout` 之后，再跑一次**不调用模型**。

    这是"真实库里 13 行 `timeout`（`retry_count=2`，生产 `max_retries=2`）在任何
    token / 批量配置下都会被跳过"这条结论的机制证明。
    **活对照**：同一调用路径在预算**未**用尽时（`max_retries=2`）必须真的调用模型。
    """
    plan = classify_document(FEED_BYTES, raw_id="raw_feed")
    unit = plan.units[0]
    store = SqliteProposedStore(db_path=tmp_path / "ledger.db")

    # 第一次：预算 0 ⇒ 一次调用，降级为 timeout，落一行 retry_count=0 的未分类运行账
    first_port = ScriptedPort([(None, DegradeReason.TIMEOUT)])
    first = propose_units(
        "raw_feed",
        [unit],
        label_space=space,
        port=first_port,
        store=store,
        policy=ProposalPolicy(max_units_per_call=1, max_retries=0),
    )
    assert first_port.call_count == 1
    assert first.counters.unclassified_units == 1
    assert first.counters.units_retry_exhausted == 1

    # 第二次：同样的预算 ⇒ 运行账命中且预算用尽 ⇒ **一次模型都不调**
    second_port = ScriptedPort([([_claim("machine-learning", "transformer scaling")], None)])
    second = propose_units(
        "raw_feed",
        [unit],
        label_space=space,
        port=second_port,
        store=store,
        policy=ProposalPolicy(max_units_per_call=1, max_retries=0),
    )
    assert second_port.call_count == 0, "预算已用尽的可重试降级不得再调用模型"
    assert second.counters.units_skipped_already_run == 1
    assert second.counters.units_run == 0

    # 活对照：预算**未**用尽（max_retries=2）⇒ 同一路径必须真的调用模型
    third_port = ScriptedPort([([_claim("machine-learning", "transformer scaling")], None)])
    third = propose_units(
        "raw_feed",
        [unit],
        label_space=space,
        port=third_port,
        store=store,
        policy=ProposalPolicy(max_units_per_call=1, max_retries=2),
    )
    assert third_port.call_count == 1, "预算没用完时必须真调用（否则上面的『不调用』毫无意义）"
    assert third.counters.classified_units == 1
    store.close()
    print(
        "\n[Y3] 预算用尽 → 调用 0 次；预算未用尽 → 调用 1 次并分类成功（活对照成立）"
    )


# =========================================================================== #
# 判据 Y4：SPEC §2.17 的常量与代码一致
# =========================================================================== #


def test_spec_217_measured_constants_match_the_code() -> None:
    """**判据 Y4**：§2.17 登记的四个常量 + 超时与代码一致（改动必须重跑证据脚本）。"""
    assert BATCH_MAX_UNITS == 4
    assert BATCH_MAX_CHARS == 6000
    assert UNIT_TEXT_MAX_CHARS == 2000
    # 2048 → 4096：实测标定值（见模块 docstring 的诊断表；SPEC §2.17 正文待主代理更新）
    assert CognitionConfig().max_output_tokens == 4096
    assert DEFAULT_TIMEOUT_SECONDS == 60.0
    policy = ProposalPolicy()
    assert policy.max_units_per_call == BATCH_MAX_UNITS
    assert policy.max_chars_per_call == BATCH_MAX_CHARS
    assert policy.max_retries == 2
    assert policy.retry_max_units_per_call == 1
    assert policy.retry_shrink is True
    assert policy.units_for_attempt(0) == BATCH_MAX_UNITS
    assert policy.units_for_attempt(1) == 1
    assert policy.units_for_attempt(2) == 1


# =========================================================================== #
# 判据 Y5：测量脚本不能写真实 store（AST 扫描 + 活对照）
# =========================================================================== #


def test_yield_probe_never_constructs_a_store_on_the_real_database() -> None:
    """**判据 Y5**：`tools/t105_yield_probe.py` 的每处 `SqliteProposedStore(...)`
    都不得把 `db_path` 指向 `REAL_DB`。

    **活对照**：扫描必须至少找到一处构造 —— 没有的话，"没找到危险构造"只是因为
    什么都没找到（与判据 14 的 AST 扫描同一形状）。
    """
    source_path = REPO_ROOT / "tools" / "t105_yield_probe.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    sources = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SqliteProposedStore"
    ]
    assert sources, "扫描没找到任何 SqliteProposedStore 构造：判据空转，不是通过"
    for rendered in sources:
        assert "REAL_DB" not in rendered, f"有构造指向真实库：{rendered}"
        assert "tmp" in rendered or "TemporaryDirectory" in rendered, (
            f"构造不在临时目录里：{rendered}"
        )

    # 也没有"写真实库"这个逃生口：argparse 里不存在 --write-store（AST 扫描，不看注释文字）
    flags = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert flags, "扫描没找到任何 add_argument：判据空转，不是通过"
    assert "--write-store" not in flags, f"测量脚本不该有写真实库的开关：{flags}"
    assert "--sample-only" in flags, f"扫描到的开关不像本脚本：{flags}"
    print(
        f"\n[Y5] AST 扫描：{len(sources)} 处 SqliteProposedStore 构造全部落在临时目录；"
        f"开关 {sorted(flags)} 中没有写真实库的逃生口"
    )


# =========================================================================== #
# 判据 Y7：配置的输出预算**真的**进了 HTTP 请求体
# =========================================================================== #


def test_configured_output_budget_reaches_the_http_request_body() -> None:
    """**判据 Y7**：`CognitionConfig.max_output_tokens` 真的以 `max_completion_tokens`
    出现在发给 provider 的 HTTP 请求体里。

    为什么必须钉死：本项目的注释里**曾经**断言"这个字段没有被 adapter 送进边车"，
    那是**假的**，并直接把 SPEC §2.17 的标定带偏了一轮（见 `classify.py` 的更正记录）。
    注释会漂移，请求体不会 —— 这里用**本地 mock 服务器 + 真实边车进程**直接读请求体。
    **活对照**：两个不同的配置值必须给出两个不同的字段值（否则"字段在"可能只是硬编码）。
    """
    require_sidecar_installed()
    observed: Dict[int, Any] = {}
    with MockServer() as server:
        for tokens in (1234, 4321):
            server.mock.requests.clear()
            port = mock_port(server, max_output_tokens=tokens)
            result = port.extract(
                CognitionRequest(
                    raw_id="raw_budget",
                    external_content="A short note about gradient boosted trees.",
                    candidate_labels=LABELS,
                    kind="industry",
                )
            )
            assert result.status is CallStatus.OK, result.record.detail
            observed[tokens] = server.mock.last_request().get("max_completion_tokens")
    assert observed == {1234: 1234, 4321: 4321}, (
        f"输出预算没有如实进入请求体：{observed}"
    )
    print(f"\n[Y7] 请求体里的 max_completion_tokens 跟随配置：{observed}")


# =========================================================================== #
# 判据 Y6：只读快照可重复（缺真实数据时自跳过）
# =========================================================================== #

REAL_DB = REPO_ROOT / "data" / "store" / "atlas.db"
STORE_RAW = REPO_ROOT / "data" / "store" / "raw"

requires_real_data = pytest.mark.skipif(
    not (REAL_DB.is_file() and STORE_RAW.is_dir()),
    reason="本地真实 store 缺失（data/ 不进 git，见 SPEC §8.1）→ 自跳过",
)


@requires_real_data
def test_read_only_snapshot_is_repeatable() -> None:
    """**判据 Y6**：连续两次只读快照逐字段相同；raw 整树摘要不变。"""
    first = t105_yield_probe.read_only_snapshot()
    second = t105_yield_probe.read_only_snapshot()
    assert first == second
    assert first["raw_tree"] == second["raw_tree"]
    assert first["proposed_claims"][0] >= 0
    print(
        f"\n[Y6] 真实库只读快照可重复：proposed_claims={first['proposed_claims'][0]} 行"
        f"、proposal_runs={first['proposal_runs'][0]} 行、raw 树={first['raw_tree'][:16]}…"
    )
