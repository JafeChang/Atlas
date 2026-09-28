"""T-105 产出率（yield）分离实验：**输出预算 / 批量大小 / 瞬时网络失败**，谁是原因。

跑法（WSL 内，需要代理 + 真实凭据 + 边车依赖）::

    export PATH="$HOME/.nvm/versions/node/v22.21.1/bin:$PATH"
    export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897
    ./.venv-new/bin/python tools/t105_yield_probe.py --sample-only      # 只打印样本，不调用模型
    ./.venv-new/bin/python tools/t105_yield_probe.py --json-out /tmp/t105yield.json

**要回答的问题**（SPEC §2.17 的常量号称"实测标定，不是拍的"；这里就是对它的复核）
-------------------------------------------------------------------------------

真实 store 的 T-105 产出率是 **4/24 ≈ 17%**（§2.17 实测表）。当初的假设是
"推理 token 吃掉输出预算 ⇒ 长单元失败（`empty_completion` / `unparseable_output` / `timeout`）
⇒ 抬高 `max_output_tokens` 能把产出率抬起来"。

本脚本把这个假设拆成**可分别观测**的三条，并给每条一个可证伪的判据：

| 假设 | 判据（必须在数字上成立才算成立） |
|---|---|
| (a) **输出预算** | 失败调用的 `output_tokens` 顶到 `max_output_tokens`（说明被截断），且抬到 4096/8192 后单元级产出率显著上升 |
| (b) **批量/字符上限** | 同一批单元在 `max_units_per_call=1` 与 `4` 之间的产出率差异显著（批量越大越差） |
| (c) **瞬时失败** | 失败调用的 `input_tokens == 0` 且 `output_tokens == 0`（请求根本没到模型）⇒ 与单元长度/预算无关，且同一单元重跑会成功 |

如果三条都不成立，诚实的结论就是"**当前的 17% 主要是瞬时的，产出率在现有配置下其实不低**"
—— 这是**有效结果**，不得为了"有改进"而回调参数。

**样本怎么选（确定性、可复算、不含随机）**
----------------------------------------

样本 = **两组**，全部由 `select_sample()` 这个**纯函数**决定
（2026-09-28 实跑时是 20 + 5 = **25 个单元**）：

1. **失败组 `FAIL`**：真实 `data/store/atlas.db` 的 `proposal_runs` 里
   `status='unclassified'` 的**全部** unit_id（2026-09-28 实为 **20 个** = 13 个
   `timeout` + 7 个 `no_claim_extracted` 所在的单元），按 unit_id 升序。
   它们正是"17% 产出率"那个分母的失败侧，必须在样本里 —— 否则测的是另一个问题。
   **每个 arm 用各自的临时库**，所以运行账里没有它们的记录
   ⇒ 它们会**真的重跑**（不会被 `planned_state` 跳过）。
2. **长度分层组 `FRESH`**：**从未跑过**（不在 `proposal_runs`）的单元按
   `(len(text), unit_id)` 升序后的**位置**取 `strata=5` 个：
   `index = round(q * (n - 1))`，`q ∈ {0, 0.25, 0.5, 0.75, 1}`。
   这一组的目的是让"长度"这个变量在**运行账不可能跳过**的单元上被覆盖
   （最短 / 中位 / 最长都进样本）。

样本规模是**有意的**：25 个单元 × 5 个 arm ≈ 110 次调用（每次约 6–24 s 墙钟），
够做出方向性判断，又不至于把预算烧在"再多测几个"上。每次调用都要付一次
**边车进程启动**（drvfs 实测 4.2–8.2 s，逐臂均值会打印出来）。

**实验臂（arm）**
---------------

| arm | max_units_per_call | max_retries | max_output_tokens | 测什么 |
|---|---|---|---|---|
| `b1_tok2048` | 1 | 0 | 2048 | 单单元 + **改之前**的默认预算（基线） |
| `b1_tok4096` | 1 | 0 | 4096 | 预算翻倍的**纯效应** |
| `b1_tok8192` | 1 | 0 | 8192 | 预算 4 倍的纯效应 |
| `b4_tok2048` | 4 | 0 | 2048 | **批量**的纯效应（对同一预算） |
| `default_retry2` | 4 | 2 | 2048 | **SPEC §2.17 的生产默认**（含重试）⇒ 真实产出率 |

`max_retries=0` 的臂是**单发测量**：一次调用一个批次，不再重试。这样"失败原因直方图"
反映的是**首次尝试**的行为，而不是重试之后的结果 —— 重试的效果单独由 `default_retry2` 体现。

**标签空间诊断臂**（`--diag-wide-labels`）
---------------------------------------

它**不是配置建议**，只是一个分离实验：把 4 个通用标签
（`DIAG_EXTRA_LABELS`）加进标签空间，看 `no_claim_extracted` 到底是
"模型没话可说"还是"当前标签里没有能装下这条内容的标签"。
**这一步是必须的** —— 因为 `no_claim_extracted` 与输出预算无关
（那些调用的 `output_tokens` 只有 30–800，离上限很远），
仅仅看"产出率没上去"无法区分成因。

**契约边界**：本脚本只**读** `data/store/atlas.db`（标签空间 + 运行账），
所有 `proposed_claims` / `proposal_runs` 都写进 `tempfile.TemporaryDirectory()` 下的临时库。
`--write-store` 这个开关**不存在**，也不可能被误开。跑完会核对：
真实库的 `proposed_claims` / `proposal_runs` / `config_versions` 行数与内容一字未变，
且 `data/store/raw` 整树 sha256 未变。

**2026-09-28 的实测结论（本脚本自己跑出来的，不是注释里的信念）**

- (a) **输出预算成立**：2048 下 **10/62** 次调用被截断
  （`output_tokens == reasoning_tokens == 上限`、`stopReason=length` ⇒ 内容为 0
  ⇒ `empty_completion`），单元产出率 **10/25 = 40%**；4096 下 **0/50** 次截断、
  **14/25 = 56%**（第二次独立运行复现 14/25）；8192 下 0/25、15/25。
  ⇒ `CognitionConfig.max_output_tokens` 默认值改成 **4096**。
- (b) **批量不动**：同样 2048 下 `b4_tok2048` 产出率 14/25、只用 14 次调用；
  批量会把一次截断放大成 4 个单元，但生产默认的重试把 4/4 截断都救回来了。
- (c) **瞬时失败不再复现**：本次 108 次调用里 0-token 的调用数是 **0**；
  真实库里那 13 行 `timeout` 是 0 token + 11.4 s 恒定 + 连续 2.5 分钟的时间窗，
  属于**传输层**失败。把它们剔除后，真实库"到过模型"的产出率是 4/11 ≈ 36%。
- (d) **真正的剩余瓶颈是标签空间**：11 个 `no_claim_extracted` 单元在
  4 标签下 9/20 分类、在 8 标签下 **20/20**，而且用的是 `software-engineering` /
  `data-science` / `business-and-markets` 这些**语义上确实成立**的标签，
  **`other` 一次都没用**。这是注册表配置的结论，不是本模块的缺陷。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.cognition import (  # noqa: E402
    BATCH_MAX_CHARS,
    BATCH_MAX_UNITS,
    UNIT_TEXT_MAX_CHARS,
    CognitionConfig,
    LabelSpace,
    PiSidecarCognitionPort,
    ProposalPolicy,
    SqliteProposedStore,
    classify_document,
    propose_units,
)

STORE_RAW = REPO_ROOT / "data" / "store" / "raw"
REAL_DB = REPO_ROOT / "data" / "store" / "atlas.db"

#: 长度分层组的取样点数（含两端）。
DEFAULT_STRATA = 5

#: **诊断用**的额外标签（`--diag-wide-labels`）。它们**不是**配置建议，只是用来区分
#: "模型没话可说"与"当前标签空间太窄、模型正确地不硬塞"这两种解释：
#: 只有 4 个标签（computer-vision / machine-learning / natural-language /
#: statistical-learning）时，一篇讲"用 Python 自动化重复劳动"的 kdnuggets 文章
#: 确实可能一条都不匹配 —— 那 **不是产出率问题，是标签空间宽度的性质**。
DIAG_EXTRA_LABELS: Tuple[str, ...] = (
    "data-science",
    "software-engineering",
    "business-and-markets",
    "other",
)

#: 实验臂：(名字, max_units_per_call, max_retries, max_output_tokens)
DEFAULT_ARMS: Tuple[Tuple[str, int, int, int], ...] = (
    ("b1_tok2048", 1, 0, 2048),
    ("b1_tok4096", 1, 0, 4096),
    ("b1_tok8192", 1, 0, 8192),
    ("b4_tok2048", 4, 0, 2048),
    ("default_retry2", BATCH_MAX_UNITS, 2, 2048),
)


# --------------------------------------------------------------------------- #
# 只读快照（"真实 store 一字未变"的判据）
# --------------------------------------------------------------------------- #


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def read_only_snapshot() -> Dict[str, Any]:
    """真实库中**本任务不得改动**的三张表 + 原始归档整树的指纹。"""
    uri = f"file:{REAL_DB}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {}
    for table in ("proposed_claims", "proposal_runs", "config_versions"):
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = (len(rows), hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest())
        except sqlite3.OperationalError as exc:  # pragma: no cover - 表不存在时如实报告
            out[table] = (-1, f"missing:{exc}")
    conn.close()
    out["raw_tree"] = _tree_digest(STORE_RAW)
    return out


# --------------------------------------------------------------------------- #
# 样本（纯函数：给定单元与运行账 → 样本；无 I/O、无随机、无时钟）
# --------------------------------------------------------------------------- #


def select_sample(
    units_by_id: Mapping[str, Any],
    run_rows: Sequence[Mapping[str, Any]],
    *,
    strata: int = DEFAULT_STRATA,
) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
    """返回 `(失败组, 长度分层组)`。**纯函数**，同一输入永远同一输出。

    失败组 = 运行账里 `status='unclassified'` 的 unit_id（升序）；
    长度分层组 = 从未跑过的单元按 `(len(text), unit_id)` 升序后按分位点取 `strata` 个。

    unit_id 在 `units_by_id` 里找不到 ⇒ **响亮失败**（`KeyError`）：那说明运行账与本轮
    分流结果不一致（分流规则变了 / 原文变了），不能悄悄少测几个单元。
    """
    if strata < 1:
        raise ValueError(f"strata 必须为正：{strata}")
    ran_ids = {str(row["unit_id"]) for row in run_rows}
    fail_ids = sorted(
        {str(row["unit_id"]) for row in run_rows if str(row["status"]) == "unclassified"}
    )
    fail = tuple(units_by_id[unit_id] for unit_id in fail_ids)

    fresh = sorted(
        (unit for unit_id, unit in units_by_id.items() if unit_id not in ran_ids),
        key=lambda unit: (len(unit.text), unit.unit_id),
    )
    picks: List[Any] = []
    seen: set = set()
    for index in range(strata):
        if not fresh:  # 全库都跑过了：分层组为空是**事实**，不是错误（`fail` 仍然有效）
            break
        q = index / (strata - 1) if strata > 1 else 0.0
        position = int(round(q * (len(fresh) - 1)))
        unit = fresh[position]
        if unit.unit_id not in seen:
            seen.add(unit.unit_id)
            picks.append(unit)
    return fail, tuple(picks)


def load_units() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """`data/store/raw` → `{unit_id: Unit}` 与 `{raw_id: (channel_id, endpoint, bytes)}`。"""
    units_by_id: Dict[str, Any] = {}
    raws: Dict[str, Any] = {}
    for entry_dir in sorted(STORE_RAW.iterdir()):
        meta_path = entry_dir / "meta.json"
        content_path = entry_dir / "content.bin"
        if not entry_dir.is_dir() or not meta_path.is_file() or not content_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw_id = str(meta["raw_id"])
        payload = content_path.read_bytes()
        raws[raw_id] = (str(meta["channel_id"]), str(meta["endpoint"]), payload)
        plan = classify_document(
            payload,
            raw_id=raw_id,
            channel_id=str(meta["channel_id"]),
            endpoint=str(meta["endpoint"]),
        )
        if plan.skipped:
            continue
        for unit in plan.units:
            if unit.unit_id in units_by_id:
                raise RuntimeError(
                    f"unit_id 撞车：{unit.unit_id} 同时属于两个 raw "
                    f"（{units_by_id[unit.unit_id].raw_id} / {unit.raw_id}）——"
                    "单元身份必须是全库唯一，否则样本无法复现"
                )
            units_by_id[unit.unit_id] = unit
    return units_by_id, raws


def read_run_rows() -> List[Dict[str, Any]]:
    """真实库的运行账（**只读**；用 immutable 打开，绝不写）。"""
    uri = f"file:{REAL_DB}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT unit_id, status, reason, retry_count FROM proposal_runs ORDER BY unit_id"
        ).fetchall()
    ]
    conn.close()
    return rows


def read_label_space() -> LabelSpace:
    """组合根：从注册表读标签空间后**注入**（本包不 import registry）。"""
    from atlas.registry import RegistryService, SqliteConfigStore

    config_store = SqliteConfigStore(author="t105-yield-probe", db_path=REAL_DB)
    service = RegistryService(config_store)
    space = LabelSpace.of(
        service.label_space(), config_version=service.config_version, source="registry"
    )
    config_store.close()
    return space


# --------------------------------------------------------------------------- #
# 计时端口（每次调用的墙钟 + 原始记录；**不改**端口语义）
# --------------------------------------------------------------------------- #


class TimedPort:
    """`CognitionPort` 的**透明包装**：记录每次调用的墙钟与 `CognitionCallRecord`。

    墙钟（本类）与模型侧 `elapsed_ms`（记录里）的差 = 边车进程启动 + 往返开销，
    这是"启动成本占大头"这条 SPEC §2.14 结论的直接观测量。
    """

    def __init__(self, inner: PiSidecarCognitionPort, log: List[Dict[str, Any]]) -> None:
        self._inner = inner
        self._log = log
        self.config = inner.config

    def extract(self, request: Any) -> Any:
        started = time.monotonic()
        result = self._inner.extract(request)
        wall_ms = int((time.monotonic() - started) * 1000)
        record = result.record
        usage = record.usage
        self._log.append(
            {
                # 绝对时刻：**瞬时失败**的判据之一是"同一段时间窗内成片失败"，
                # 没有时刻就无法把"网络抖动"与"模型行为"分开。
                "at": time.time(),
                "wall_ms": wall_ms,
                "status": str(getattr(result.status, "value", result.status)),
                "reason": (
                    str(getattr(record.reason, "value", record.reason))
                    if record.reason is not None
                    else None
                ),
                "model_elapsed_ms": int(record.elapsed_ms or 0),
                "input_tokens": int(usage.input_tokens or 0),
                "output_tokens": int(usage.output_tokens or 0),
                "reasoning_tokens": usage.reasoning_tokens,
                "thinking_chars": int(record.thinking_chars or 0),
                "claims": len(result.claims or ()) if not result.is_unclassified else 0,
                "detail": str(record.detail or "")[:200],
            }
        )
        return result


# --------------------------------------------------------------------------- #
# 一个 arm
# --------------------------------------------------------------------------- #


def run_arm(
    name: str,
    units: Sequence[Any],
    *,
    raws: Mapping[str, Any],
    space: LabelSpace,
    base_config: CognitionConfig,
    max_units: int,
    max_retries: int,
    max_output_tokens: int,
) -> Dict[str, Any]:
    """把样本按 raw 分组，逐 raw 跑 `propose_units`（生产的真实路径），写**临时库**。"""
    policy = ProposalPolicy(
        max_units_per_call=max_units,
        max_chars_per_call=BATCH_MAX_CHARS,
        max_retries=max_retries,
    )
    config = base_config.with_overrides(max_output_tokens=max_output_tokens)
    grouped: Dict[str, List[Any]] = {}
    for unit in units:
        grouped.setdefault(unit.raw_id, []).append(unit)

    calls: List[Dict[str, Any]] = []
    run_rows: List[Any] = []
    values_by_unit: Dict[str, List[str]] = {}
    with tempfile.TemporaryDirectory(prefix=f"t105yield-{name}-") as tmp:
        store = SqliteProposedStore(db_path=Path(tmp) / "probe.db")
        port = TimedPort(PiSidecarCognitionPort(config), calls)
        started = time.monotonic()
        for raw_id in sorted(grouped):
            outcome = propose_units(
                raw_id,
                grouped[raw_id],
                label_space=space,
                port=port,
                store=store,
                policy=policy,
            )
            run_rows.extend(outcome.runs)
            # 写进库的**分类行取值**（模型给了哪些标签）——诊断"标签空间是不是太窄"时，
            # 只知道"分类了"不够，还得知道它用了哪个标签。
            for claim in outcome.claims:
                if claim.is_classified and claim.value:
                    values_by_unit.setdefault(str(claim.unit_id), []).append(str(claim.value))
        wall_ms = int((time.monotonic() - started) * 1000)
        claim_status = store.status_counts()
        claim_reasons = store.reason_counts()
        store.close()

    unit_reasons: Dict[str, int] = {}
    unit_status: Dict[str, int] = {}
    retries_used = 0
    for row in run_rows:
        unit_status[str(row.status)] = unit_status.get(str(row.status), 0) + 1
        if row.reason:
            unit_reasons[str(row.reason)] = unit_reasons.get(str(row.reason), 0) + 1
        retries_used += int(row.retry_count or 0)

    input_tokens = sum(int(c["input_tokens"]) for c in calls)
    output_tokens = sum(int(c["output_tokens"]) for c in calls)
    reasoning_tokens = sum(int(c["reasoning_tokens"] or 0) for c in calls)
    return {
        "arm": name,
        "max_units_per_call": max_units,
        "max_chars_per_call": BATCH_MAX_CHARS,
        "max_retries": max_retries,
        "max_output_tokens": max_output_tokens,
        "units_attempted": len(units),
        "units_classified": unit_status.get("classified", 0),
        "units_unclassified": unit_status.get("unclassified", 0),
        "unit_status": unit_status,
        "unit_reasons": unit_reasons,
        "claim_status": claim_status,
        "claim_reasons": claim_reasons,
        "calls": len(calls),
        "calls_ok": sum(1 for c in calls if c["status"] == "classified"),
        "calls_degraded": sum(1 for c in calls if c["status"] != "classified"),
        "retries_used": retries_used,
        "wall_ms": wall_ms,
        "model_elapsed_ms": sum(int(c["model_elapsed_ms"]) for c in calls),
        "startup_overhead_ms": sum(
            int(c["wall_ms"]) - int(c["model_elapsed_ms"]) for c in calls
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_share_of_output": (
            round(reasoning_tokens / output_tokens, 4) if output_tokens else None
        ),
        "calls_at_budget_cap": sum(
            1 for c in calls if int(c["output_tokens"]) >= max_output_tokens
        ),
        "call_reasons": _histogram(
            c["reason"] for c in calls if c["reason"] is not None
        ),
        "calls_detail": calls,
        "unit_rows": [
            {
                "unit_id": str(row.unit_id),
                "status": str(row.status),
                "reason": row.reason,
                "retry_count": int(row.retry_count or 0),
                "batch_size": int(row.batch_size or 0),
                "calls": int(row.calls or 0),
                "input_tokens": int(row.input_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
                "reasoning_tokens": row.reasoning_tokens,
                "elapsed_ms": int(row.elapsed_ms or 0),
                "values": values_by_unit.get(str(row.unit_id), []),
            }
            for row in run_rows
        ],
    }


def _histogram(values: Iterable[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #


def section(title: str) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def report_sample(fail: Sequence[Any], fresh: Sequence[Any], units_by_id: Mapping[str, Any]) -> None:
    section("样本（确定性；`select_sample()` 是纯函数）")
    print(f"失败组 FAIL（真实库 proposal_runs 里 status=unclassified 的全部单元）：{len(fail)} 个")
    for unit in fail:
        print(f"  {unit.unit_id[:14]:16s} {len(unit.text):5d} 字符  {unit.kind:8s} {unit.title[:60]!r}")
    print(f"\n长度分层组 FRESH（从未跑过；按 (len(text), unit_id) 排序后取分位点）：{len(fresh)} 个")
    for unit in fresh:
        print(f"  {unit.unit_id[:14]:16s} {len(unit.text):5d} 字符  {unit.kind:8s} {unit.title[:60]!r}")
    print(
        f"\n全库单元 {len(units_by_id)} 个｜样本合计 {len(fail) + len(fresh)} 个"
        f"｜UNIT_TEXT_MAX_CHARS={UNIT_TEXT_MAX_CHARS}"
        f"｜BATCH_MAX_UNITS={BATCH_MAX_UNITS}｜BATCH_MAX_CHARS={BATCH_MAX_CHARS}"
    )


def report_arm(summary: Mapping[str, Any]) -> None:
    attempted = int(summary["units_attempted"])
    classified = int(summary["units_classified"])
    section(
        f"arm `{summary['arm']}`：units/call={summary['max_units_per_call']}"
        f" retries={summary['max_retries']} max_output_tokens={summary['max_output_tokens']}"
    )
    print(
        f"  单元 {attempted} → **产出率 {classified}/{attempted} "
        f"= {100.0 * classified / attempted if attempted else 0:.1f}%**"
        f"（未分类 {summary['units_unclassified']}）"
    )
    print(
        f"  调用 {summary['calls']} 次（ok {summary['calls_ok']} / 降级 "
        f"{summary['calls_degraded']}）｜重试轮累计 {summary['retries_used']}"
        f"｜顶到预算上限的调用 {summary['calls_at_budget_cap']} 次"
    )
    print(f"  单元级失败原因：{summary['unit_reasons']}")
    print(f"  调用级失败原因：{summary['call_reasons']}")
    degraded = [c for c in summary["calls_detail"] if c["status"] != "classified"]
    if degraded:
        print("  降级调用明细（时刻 / 原因 / token）：")
        for call in degraded:
            print(
                f"    {time.strftime('%H:%M:%S', time.localtime(call['at']))}"
                f" reason={str(call['reason']):20s} in={call['input_tokens']:5d}"
                f" out={call['output_tokens']:5d} reas={str(call['reasoning_tokens']):>5s}"
                f" wall={call['wall_ms']:6d}ms claims={call['claims']}"
                f" {call['detail'][:80]!r}"
            )
    print(
        f"  token：in {summary['input_tokens']} out {summary['output_tokens']}"
        f" 其中 reasoning {summary['reasoning_tokens']}"
        f"（占 output "
        f"{summary['reasoning_share_of_output'] if summary['reasoning_share_of_output'] is not None else 'n/a'}）"
    )
    print(
        f"  墙钟 {summary['wall_ms']} ms = 模型侧 {summary['model_elapsed_ms']} ms"
        f" + 启动/往返 {summary['startup_overhead_ms']} ms"
        f"（每次启动均值 "
        f"{summary['startup_overhead_ms'] / summary['calls'] if summary['calls'] else 0:.0f} ms）"
    )
    print(f"  落库状态（临时库）：{summary['claim_status']}｜理由 {summary['claim_reasons']}")
    print("  逐单元：")
    print(
        f"    {'unit_id':16s} {'status':14s} {'reason':22s} {'retry':>5s} {'bsz':>4s}"
        f" {'calls':>5s} {'in':>5s} {'out':>5s} {'reas':>5s} {'ms':>7s}  values"
    )
    for row in summary["unit_rows"]:
        print(
            f"    {str(row['unit_id'])[:14]:16s} {str(row['status']):14s}"
            f" {str(row['reason'] or '-'):22s} {row['retry_count']:>5d} {row['batch_size']:>4d}"
            f" {row['calls']:>5d} {row['input_tokens']:>5d} {row['output_tokens']:>5d}"
            f" {str(row['reasoning_tokens']):>5s} {row['elapsed_ms']:>7d}"
            f"  {row.get('values') or ''}"
        )


def report_verdict(summaries: Sequence[Mapping[str, Any]]) -> None:
    section("判据核对（三条假设各自的证伪判据，见模块 docstring）")
    by_name = {str(s["arm"]): s for s in summaries}
    base = by_name.get("b1_tok2048")

    # (a) 输出预算：失败调用是否顶到上限
    capped = {name: int(s["calls_at_budget_cap"]) for name, s in by_name.items()}
    print(f"(a) **输出预算**：各 arm 中 output_tokens 顶到 max_output_tokens 的调用数 = {capped}")
    if base is not None:
        print(
            f"    基线 b1_tok2048 的 calls={base['calls']}"
            f"｜out={base['output_tokens']}（reasoning {base['reasoning_tokens']}, "
            f"占 {base['reasoning_share_of_output']}）"
            f"｜单次调用 out 均值 "
            f"{base['output_tokens'] / base['calls'] if base['calls'] else 0:.0f}"
            f" / 预算 {base['max_output_tokens']}"
        )
    for name in ("b1_tok2048", "b1_tok4096", "b1_tok8192"):
        if name in by_name:
            s = by_name[name]
            print(
                f"    {name:12s} 产出率 {s['units_classified']}/{s['units_attempted']}"
                f"（{100.0 * int(s['units_classified']) / int(s['units_attempted']):.1f}%）"
                f"｜调用级降级 {s['calls_degraded']}/{s['calls']}"
                f"｜原因 {s['call_reasons']}"
            )

    # (b) 批量
    print()
    print("(b) **批量/字符上限**：")
    for name in ("b1_tok2048", "b4_tok2048"):
        if name in by_name:
            s = by_name[name]
            print(
                f"    {name:12s} 产出率 {s['units_classified']}/{s['units_attempted']}"
                f"（{100.0 * int(s['units_classified']) / int(s['units_attempted']):.1f}%）"
                f"｜调用 {s['calls']}｜原因 {s['unit_reasons']}"
            )

    # (c) 瞬时失败：0 token 的调用
    print()
    zero_token = {
        str(s["arm"]): sum(
            1
            for c in s["calls_detail"]
            if int(c["input_tokens"]) == 0 and int(c["output_tokens"]) == 0
        )
        for s in summaries
    }
    print(
        "(c) **瞬时失败**（`input_tokens == 0 且 output_tokens == 0` ⇒ 请求没到达模型）："
        f"{zero_token}"
    )
    if base is not None:
        zero = [c for c in base["calls_detail"] if not c["input_tokens"] and not c["output_tokens"]]
        print(f"    基线里这类调用的次数 = {len(zero)}")
        for call in zero[:12]:
            print(
                f"      {time.strftime('%H:%M:%S', time.localtime(call['at']))}"
                f" status={call['status']} reason={call['reason']}"
                f" wall={call['wall_ms']}ms detail={call['detail'][:110]!r}"
            )
    default = by_name.get("default_retry2")
    if default is not None:
        print(
            f"    **生产默认臂 default_retry2 的真实产出率 = "
            f"{default['units_classified']}/{default['units_attempted']} "
            f"= {100.0 * int(default['units_classified']) / int(default['units_attempted']):.1f}%**"
            f"（调用 {default['calls']} 次，重试累计 {default['retries_used']}）"
        )
    print(
        "\n注意：本脚本**只报实测**。三条假设哪条成立、要不要改 SPEC §2.17 的常量，"
        "由数字决定，不由预期决定。"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-only", action="store_true", help="只打印样本，不调用模型")
    parser.add_argument("--strata", type=int, default=DEFAULT_STRATA)
    parser.add_argument("--json-out", default="")
    parser.add_argument(
        "--arms",
        default="",
        help="只看这些 arm（逗号分隔的名字）；默认全部",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只跑样本的前 N 个单元（**只为冒烟测试接线**；正式测量不要用）",
    )
    parser.add_argument(
        "--diag-wide-labels",
        choices=("off", "fresh", "fail", "all"),
        default="off",
        help="诊断臂：把通用标签加进标签空间，看 `no_claim_extracted` 是否只是标签空间太窄"
        "（off / 只跑长度分层组 / 只跑失败组 / 全部）",
    )
    args = parser.parse_args()

    node = shutil.which("node")
    before = read_only_snapshot()
    units_by_id, raws = load_units()
    run_rows = read_run_rows()
    fail, fresh = select_sample(units_by_id, run_rows, strata=args.strata)
    sample = list(fail) + list(fresh)
    if args.limit:
        sample = sample[: args.limit]
        print(f"⚠️ --limit {args.limit}：只跑样本前 {len(sample)} 个单元（冒烟测试用）")

    print(
        f"真实库只读快照（跑前）：proposed_claims {before['proposed_claims'][0]} 行"
        f"｜proposal_runs {before['proposal_runs'][0]} 行"
        f"｜raw 整树 {before['raw_tree'][:16]}…"
    )
    report_sample(fail, fresh, units_by_id)
    if args.sample_only:
        print("\n--sample-only：不调用模型，退出。")
        return 0

    if node is None:
        print(
            "找不到 node：它只通过 nvm 提供，不在默认 PATH 上。"
            '请先 `export PATH="$HOME/.nvm/versions/node/v22.21.1/bin:$PATH"`。'
        )
        return 1

    space = read_label_space()
    base_config = CognitionConfig.from_env(route="deepseek", node_bin=node)
    print(
        f"配置：route={base_config.route_name} model={base_config.model}"
        f" base_url={base_config.base_url} timeout={base_config.timeout_seconds}s"
        f" reasoning_enabled={base_config.reasoning_enabled}"
        f"｜标签空间 {len(space.labels)} 个标签 @ {space.config_version}"
    )
    print(f"  **注入的标签（只读自注册表，本脚本不改）**：{list(space.labels)}")
    print(f"node={node}")

    if args.arms.strip() == "none":
        selected: set = {"__none__"}  # 只跑诊断臂时用（匹配不到任何 arm）
    else:
        selected = {name.strip() for name in args.arms.split(",") if name.strip()} or {
            name for name, _u, _r, _t in DEFAULT_ARMS
        }

    summaries: List[Dict[str, Any]] = []
    for name, max_units, max_retries, max_tokens in DEFAULT_ARMS:
        if name not in selected:
            continue
        summary = run_arm(
            name,
            sample,
            raws=raws,
            space=space,
            base_config=base_config,
            max_units=max_units,
            max_retries=max_retries,
            max_output_tokens=max_tokens,
        )
        summaries.append(summary)
        report_arm(summary)

    report_verdict(summaries)

    diagnostic: Optional[Dict[str, Any]] = None
    diag_units = {
        "off": [],
        "fresh": list(fresh),
        "fail": list(fail),
        "all": sample,
    }[args.diag_wide_labels]
    if diag_units:
        wide = LabelSpace.of(
            tuple(space.labels) + DIAG_EXTRA_LABELS,
            config_version=f"{space.config_version}+diag-wide",
            source="diagnostic",
        )
        diagnostic = run_arm(
            "diag_wide_labels",
            diag_units,
            raws=raws,
            space=wide,
            base_config=base_config,
            max_units=1,
            max_retries=0,
            max_output_tokens=4096,
        )
        section(
            "诊断臂 `diag_wide_labels`（**不是配置建议**，只为区分「没话可说」与「标签空间太窄」）"
        )
        print(
            f"  标签空间：{list(space.labels)} → {list(wide.labels)}"
            f"（{len(diag_units)} 个单元，单单元、4096 token、不重试）"
        )
        report_arm(diagnostic)
        base = next((s for s in summaries if s["arm"] == "b1_tok4096"), None)
        if base is not None:
            diag_ids = {unit.unit_id for unit in diag_units}
            base_by_id = {
                row["unit_id"]: row for row in base["unit_rows"] if row["unit_id"] in diag_ids
            }
            diag_by_id = {row["unit_id"]: row for row in diagnostic["unit_rows"]}
            print("  同一批单元在两个标签空间下的单元级结论：")
            print(f"    {'unit_id':16s} {'4 标签（4096 token）':32s} {'8 标签（4096 token）':32s} 8 标签下用了哪些标签")
            for unit in diag_units:
                left = base_by_id.get(unit.unit_id, {})
                right = diag_by_id.get(unit.unit_id, {})
                print(
                    f"    {unit.unit_id[:14]:16s}"
                    f" {str(left.get('status')) + '/' + str(left.get('reason') or '-'):32s}"
                    f" {str(right.get('status')) + '/' + str(right.get('reason') or '-'):32s}"
                    f" {right.get('values') or ''}"
                )

    after = read_only_snapshot()
    print("\n真实库只读快照（跑后）：")
    for key in ("proposed_claims", "proposal_runs", "config_versions", "raw_tree"):
        mark = "✓ 未变" if after[key] == before[key] else "✗ 变了！"
        print(f"  {key:18s} {mark}  {after[key]}")
    assert after["proposed_claims"] == before["proposed_claims"], "本脚本不得写 proposed_claims"
    assert after["proposal_runs"] == before["proposal_runs"], "本脚本不得写 proposal_runs"
    assert after["config_versions"] == before["config_versions"], "本脚本不得写 config_versions"
    assert after["raw_tree"] == before["raw_tree"], "data/store/raw 必须一个字节都不变"

    if args.json_out:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "node": node,
            "model": base_config.model,
            "route": base_config.route_name,
            "sample": {
                "fail": [unit.unit_id for unit in fail],
                "fresh": [unit.unit_id for unit in fresh],
                "strata": args.strata,
            },
            "arms": summaries,
            "diagnostic_wide_labels": diagnostic,
            "before": {k: list(v) for k, v in before.items()},
            "after": {k: list(v) for k, v in after.items()},
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON 报告已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
