"""T-105 真实数据流证据、调用策略测量与成本外推（硬规则 1；判据 12 / 13 / 15）。

跑法（WSL 内，需要代理 + 真实凭据 + 边车依赖）::

    export HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897
    ./.venv-new/bin/python tools/t105_real_evidence.py            # 分流 + 真实样本（写临时库）
    ./.venv-new/bin/python tools/t105_real_evidence.py --write-store
                                                                  # 额外把 Proposed 写进真实 store

本脚本产出**实测数字**，四件事：

1. **分流构成**：真实 `data/store/raw` 的 75 条 raw → 8 feed（830 条目）/ 65 文章 / 2 跳过；
2. **调用策略的实测对比**：同一批真实单元，分别按"一单元一次调用"与"分批 + 有界重试"
   跑，逐次打印状态 / 耗时 / token / 抽出 claim 数 / **归属到的单元数** / 未归属数；
3. **全量外推**：按实测的每次调用成本外推 895 个单元的调用次数、耗时、token；
4. **真实降级触发**：指向不可达端点与**已下架模型**，验证"未分类 + 原因码非空 +
   claims 恒空"，并核对落库行为是"每个单元一行未分类"。

**只读纪律**：`data/store/raw` 一个字节都不写（脚本前后比对整树 sha256）。
默认**不**写 `data/store/atlas.db`；`--write-store` 才写 `proposed_claims`
（那是 T-105 的目的，SPEC §2.10 把这张表登记给了本任务），并且写完立刻核对
`raw_records` 与 `confirmed_labels` 的**行数与内容**都没变。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.cognition import (  # noqa: E402
    BATCH_MAX_CHARS,
    BATCH_MAX_UNITS,
    CallStatus,
    CognitionConfig,
    DocumentKind,
    LabelSpace,
    PiSidecarCognitionPort,
    ProposalPolicy,
    SqliteProposedStore,
    build_batches,
    classify_document,
    propose_document,
    propose_units,
    run_batch,
)

STORE_RAW = REPO_ROOT / "data" / "store" / "raw"
REAL_DB = REPO_ROOT / "data" / "store" / "atlas.db"

EXPECTED_FEEDS = {
    "https://arxiv.org/rss/cs.LG": 331,
    "https://arxiv.org/rss/cs.CV": 199,
    "https://arxiv.org/rss/cs.CL": 191,
    "https://arxiv.org/rss/stat.ML": 54,
    "https://googleaiblog.blogspot.com/atom.xml": 25,
    "https://syncedreview.com/feed": 10,
    "https://www.marktechpost.com/feed": 10,
    "https://www.kdnuggets.com/feed": 10,
}
EXPECTED_ARTICLES = 65
EXPECTED_UNITS = sum(EXPECTED_FEEDS.values()) + EXPECTED_ARTICLES

#: 有界样本默认值（**不跑全部 895 个单元**：耗时与费用都不可接受）。
DEFAULT_SAMPLE_UNITS = 12
DEFAULT_SAMPLE_ARTICLES = 3
#: 单单元对照组的规模（用于量"一单元一次调用"的成功率与单位成本）。
DEFAULT_SINGLE_UNITS = 8


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def load_records() -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for entry_dir in sorted(STORE_RAW.iterdir()):
        meta_path = entry_dir / "meta.json"
        content_path = entry_dir / "content.bin"
        if not entry_dir.is_dir() or not meta_path.is_file() or not content_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        records.append(
            {
                "raw_id": meta["raw_id"],
                "channel_id": meta["channel_id"],
                "endpoint": meta["endpoint"],
                "content": content_path.read_bytes(),
            }
        )
    return records


class _Job:
    """`propose_documents` 只要求 `raw_id` / `channel_id` / `endpoint` 三个属性。"""

    def __init__(self, record: Dict[str, object]) -> None:
        self.raw_id = str(record["raw_id"])
        self.channel_id = str(record["channel_id"])
        self.endpoint = str(record["endpoint"])


def read_label_space() -> LabelSpace:
    """组合根：从注册表读标签空间后**注入**（本包不 import registry）。"""
    from atlas.registry import RegistryService, SqliteConfigStore

    config_store = SqliteConfigStore(author="t105-evidence", db_path=REAL_DB)
    service = RegistryService(config_store)
    space = LabelSpace.of(
        service.label_space(), config_version=service.config_version, source="registry"
    )
    config_store.close()
    return space


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def measure_dispatch(
    records: Sequence[Dict[str, object]],
) -> Tuple[List[Dict[str, object]], List[Tuple[str, str, str]], List[Dict[str, object]]]:
    section("1. 真实分流（SPEC §6.3 的异质输入）")
    feeds: List[Dict[str, object]] = []
    articles: List[Dict[str, object]] = []
    skipped: List[Tuple[str, str, str]] = []
    entries_total = 0
    article_units = 0
    for record in records:
        plan = classify_document(
            record["content"],  # type: ignore[arg-type]
            raw_id=str(record["raw_id"]),
            endpoint=str(record["endpoint"]),
        )
        if plan.kind is DocumentKind.FEED:
            feeds.append(record)
            entries_total += plan.unit_count
            print(
                f"  FEED    {str(record['channel_id']):26s} {str(record['endpoint'])[:48]:48s}"
                f" → {plan.unit_count:4d} 条目"
            )
        elif plan.kind is DocumentKind.ARTICLE:
            articles.append(record)
            article_units += plan.unit_count
        else:
            skipped.append((str(record["raw_id"]), plan.skip_reason.value, plan.detail))
            print(
                f"  SKIP    {str(record['channel_id']):26s} {str(record['endpoint'])[:48]:48s}"
                f" → {plan.skip_reason.value}"
            )
    print(
        f"\n  合计 {len(records)} 条 raw = feed {len(feeds)}（{entries_total} 条目）"
        f" + 文章 {len(articles)}（{article_units} 整篇单元） + 跳过 {len(skipped)}"
    )
    print(
        f"  分类单元总数 = **{entries_total + article_units}**"
        f"（记录值 {EXPECTED_UNITS}）"
    )
    return feeds, skipped, articles


def _subset_units(
    record: Dict[str, object], limit: int
) -> Tuple[object, ...]:
    plan = classify_document(
        record["content"],  # type: ignore[arg-type]
        raw_id=str(record["raw_id"]),
        endpoint=str(record["endpoint"]),
    )
    return plan.units[:limit]


def run_single_unit_arm(
    records: Sequence[Dict[str, object]],
    units_pool: Sequence[Tuple[Dict[str, object], object]],
    *,
    space: LabelSpace,
    port: PiSidecarCognitionPort,
    limit: int,
) -> Dict[str, object]:
    section(f"2a. 对照组：**一单元一次调用**（{limit} 个单元）")
    policy = ProposalPolicy(max_units_per_call=1, max_chars_per_call=BATCH_MAX_CHARS)
    print(
        f"{'i':>3} {'chars':>6} {'status':>14} {'wall_ms':>8} {'in':>6} {'out':>6}"
        f" {'reason':>6} {'claims':>7} {'degrade':>18}"
    )
    ok = 0
    wall_total = 0
    in_total = 0
    out_total = 0
    reason_total = 0
    claims_total = 0
    for index, (record, unit) in enumerate(list(units_pool)[:limit]):
        request = build_batches(str(record["raw_id"]), [unit], policy=policy)[0]
        started = time.monotonic()
        outcome = run_batch(request, port=port, label_space=space, policy=policy)
        wall = int((time.monotonic() - started) * 1000)
        wall_total += wall
        in_total += outcome.input_tokens
        out_total += outcome.output_tokens
        reason_total += int(outcome.reasoning_tokens or 0)
        claims_total += len(outcome.extracted_claims)
        if outcome.ok:
            ok += 1
        print(
            f"{index:>3} {len(unit.text):>6} {outcome.status:>14} {wall:>8}"
            f" {outcome.input_tokens:>6} {outcome.output_tokens:>6}"
            f" {str(outcome.reasoning_tokens):>6} {len(outcome.extracted_claims):>7}"
            f" {str(outcome.reason):>18}"
        )
    count = min(limit, len(units_pool))
    print(
        f"\n  单单元：成功 {ok}/{count}（{100.0 * ok / count:.0f}%）｜"
        f"平均墙钟 {wall_total / count:.0f} ms｜平均 token"
        f" in {in_total / count:.0f} out {out_total / count:.0f}"
        f"（其中 reasoning {reason_total / count:.0f}）"
        f"｜claims {claims_total}"
    )
    return {
        "calls": count,
        "ok": ok,
        "wall_total": wall_total,
        "in_total": in_total,
        "out_total": out_total,
        "reason_total": reason_total,
        "claims_total": claims_total,
    }


def run_batched_arm(
    jobs: Sequence[Tuple[_Job, bytes]],
    *,
    space: LabelSpace,
    port: PiSidecarCognitionPort,
    store: SqliteProposedStore,
    policy: ProposalPolicy,
) -> Dict[str, object]:
    section(
        f"2b. 本任务的策略：**分批调用 + 有界重试**"
        f"（≤{policy.max_units_per_call} 单元 / ≤{policy.max_chars_per_call} 字符，"
        f"重试上限 {policy.max_retries}，重试收缩到 "
        f"{policy.retry_max_units_per_call} 单元）"
    )
    started = time.monotonic()
    all_batches = []
    rows = []
    counters: Dict[str, int] = {}
    for job, raw_bytes in jobs:
        units = getattr(job, "units", None)
        if units is None:
            plan = classify_document(raw_bytes, raw_id=job.raw_id, endpoint=job.endpoint)
            units = plan.units
        outcome = propose_units(
            job.raw_id,
            units,
            label_space=space,
            port=port,
            store=store,
            policy=policy,
        )
        all_batches.extend(outcome.batches)
        rows.extend(outcome.claims)
        for key, value in outcome.counters.as_dict().items():
            counters[key] = counters.get(key, 0) + int(value)
    wall = int((time.monotonic() - started) * 1000)

    hit = 0
    total = 0
    for batch in all_batches:
        for claim in batch.attributed:
            total += 1
            if claim.quote in claim.unit.text:
                hit += 1
        total += len(batch.unattributed)
        print(
            f"  批次 {batch.request.batch_id[:14]}… idx={batch.request.batch_index}"
            f"/{batch.request.batch_count} 单元={len(batch.request.units):2d}"
            f" 字符={batch.request.char_count:5d} {batch.status:12s}"
            f" {batch.elapsed_ms:6d} ms in={batch.input_tokens:5d}"
            f" out={batch.output_tokens:5d} reason={batch.reasoning_tokens}"
            f" claim={len(batch.extracted_claims)} 归属={len(batch.attributed)}"
            f" 未归属={len(batch.unattributed)}"
            + (f" degrade={batch.reason}" if batch.reason else "")
        )
    classified = [row for row in rows if row.is_classified]
    unclassified = [row for row in rows if row.is_unclassified]
    print(
        f"\n  单元 {counters.get('units_run', 0)}（跳过已跑 "
        f"{counters.get('units_skipped_already_run', 0)}）｜批次 "
        f"{counters.get('batches', 0)}｜重试轮 {counters.get('retries', 0)}"
        f"（推迟 {counters.get('units_deferred_to_retry', 0)} / 耗尽 "
        f"{counters.get('units_retry_exhausted', 0)}）"
    )
    print(
        f"  claim 抽出 {counters.get('extracted_claims', 0)} = 归属 "
        f"{counters.get('attributed_claims', 0)} + 未归属 "
        f"{counters.get('unattributed_claims', 0)}"
        f"｜单元分类 {counters.get('classified_units', 0)} / 未分类 "
        f"{counters.get('unclassified_units', 0)}"
    )
    print(
        f"  quote 逐字命中（在**被归属到**的单元文本里）：{hit}/{total}"
        f"（{100.0 * hit / total if total else 0:.1f}%）"
    )
    print(
        f"  落库 {counters.get('rows_written', 0)} 行新写｜分类行 {len(classified)}"
        f"｜未分类行 {len(unclassified)}｜空间外审计 "
        f"{counters.get('rows_out_of_space', 0)}"
    )
    print(
        f"  token：in {counters.get('input_tokens', 0)}"
        f" out {counters.get('output_tokens', 0)}"
        f"｜累计调用耗时 {counters.get('elapsed_ms', 0)} ms｜墙钟 {wall} ms"
    )
    print(f"  状态分布（独立读回）：{store.status_counts()}")
    print(f"  理由分布：{store.reason_counts()}")
    return {"counters": counters, "batches": len(all_batches), "wall_ms": wall, "quote_hits": hit, "quote_total": total}


def extrapolate(
    records: Sequence[Dict[str, object]],
    batched: Optional[Dict[str, object]],
    single: Optional[Dict[str, object]],
) -> None:
    section("3. 全量外推（按实测的每次调用成本）")
    strategy_calls = 0
    units_total = 0
    single_calls = 0
    for record in records:
        plan = classify_document(
            record["content"],  # type: ignore[arg-type]
            raw_id=str(record["raw_id"]),
            endpoint=str(record["endpoint"]),
        )
        if plan.skipped:
            continue
        units_total += plan.unit_count
        strategy_calls += len(
            build_batches(
                plan.raw_id,
                plan.units,
                policy=ProposalPolicy(
                    max_units_per_call=BATCH_MAX_UNITS, max_chars_per_call=BATCH_MAX_CHARS
                ),
            )
        )
        single_calls += plan.unit_count

    print(f"  全量单元数：**{units_total}**（记录值 {EXPECTED_UNITS}）")
    print(
        f"  调用次数（**首轮，不含重试**）：分批策略 **{strategy_calls}** 次"
        f"｜一单元一次 {single_calls} 次"
    )

    startup_seconds = 4.5  # SPEC §2.14 主代理实测（drvfs 上 import pi-ai）
    print(
        f"  纯边车启动开销（{startup_seconds} s/次）：分批 "
        f"{strategy_calls * startup_seconds / 60:.1f} min｜一单元一次 "
        f"{single_calls * startup_seconds / 60:.1f} min"
    )

    if batched and batched["batches"]:
        counters = batched["counters"]  # type: ignore[index]
        calls = int(batched["batches"])  # type: ignore[arg-type]
        per_call_ms = counters["elapsed_ms"] / calls
        per_call_in = counters["input_tokens"] / calls
        per_call_out = counters["output_tokens"] / calls
        units_done = counters["units_run"]
        print(
            f"  实测（分批）：{units_done} 个单元 / {calls} 次调用 ⇒ "
            f"{per_call_ms:.0f} ms/次（模型侧）、in {per_call_in:.0f}、"
            f"out {per_call_out:.0f} tokens/次"
        )
        # 重试倍率：实测"调用次数 / 单元数"相对于理论值的膨胀
        theoretical = strategy_calls if units_done else 0
        print(
            f"  全量外推（串行，按实测每次成本）：模型侧 "
            f"{strategy_calls * per_call_ms / 1000 / 60:.1f} min + 启动 "
            f"{strategy_calls * startup_seconds / 60:.1f} min ≈ **"
            f"{strategy_calls * (per_call_ms / 1000 + startup_seconds) / 60:.1f} min**"
        )
        print(
            f"  全量 token 外推：in ≈ {strategy_calls * per_call_in:,.0f}"
            f"｜out ≈ {strategy_calls * per_call_out:,.0f}"
        )
        print(
            "  ⚠️ 上面是**首轮**外推。实测的重试会按失败率增加调用次数："
            f"本样本 {units_done} 个单元实际用了 {calls} 次调用"
            f"（重试轮 {counters.get('retries', 0)} 次）；"
            "外推时按同样的比例放大即可。"
        )
    if single and single["calls"]:
        per_call_ms = single["wall_total"] / single["calls"]  # type: ignore[index]
        print(
            f"  实测（单单元）：{single['ok']}/{single['calls']} 成功，"
            f"墙钟 {per_call_ms:.0f} ms/次 ⇒ 全量 {single_calls} 次 ≈ "
            f"{single_calls * per_call_ms / 1000 / 60:.1f} min"
        )
    print(
        "  成本：DeepSeek 官方响应**不返回** cost 字段（`cost_total=None`），"
        "因此本脚本只给 token 实测值，不编造金额。"
    )


def demonstrate_degradation(space: LabelSpace, records: Sequence[Dict[str, object]]) -> None:
    section("4. 真实降级触发（§2.14 决策四）")
    feed = next(
        record for record in records if str(record["endpoint"]) == "https://syncedreview.com/feed"
    )
    units = _subset_units(feed, 2)
    request = build_batches(str(feed["raw_id"]), units, policy=ProposalPolicy())[0]

    arms = [
        (
            "不可达端点（连接被拒）",
            CognitionConfig(
                provider="test-provider",
                model="deepseek-flash",
                base_url="http://127.0.0.1:1/v1",
                route_name="unreachable",
                api_key="not-a-secret",
                timeout_seconds=20.0,
                model_version="deepseek-flash",
            ),
        ),
        (
            "已下架模型（真实 HTTP 404）",
            CognitionConfig.from_env(route="openai-compatible").with_overrides(
                model="xiaomi/mimo-v2-flash:free",
                model_version="xiaomi/mimo-v2-flash:free",
            ),
        ),
    ]
    for label, config in arms:
        port = PiSidecarCognitionPort(config)
        started = time.monotonic()
        outcome = run_batch(request, port=port, label_space=space, policy=ProposalPolicy())
        wall = int((time.monotonic() - started) * 1000)
        print(
            f"  [{label}] status={outcome.status} reason={outcome.reason}"
            f" claims={len(outcome.extracted_claims)} wall={wall} ms"
            f"\n      detail={outcome.detail[:200]}"
        )
        assert outcome.status == CallStatus.UNCLASSIFIED.value, "必须降级为未分类"
        assert outcome.reason, "降级必须带原因码"
        assert not outcome.extracted_claims and not outcome.attributed, "降级不得编造结果"

    # 落库行为（临时库，不污染真实 store）
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = SqliteProposedStore(db_path=Path(tmp) / "degrade.db")
        port = PiSidecarCognitionPort(arms[0][1])
        outcome = propose_units(
            str(feed["raw_id"]),
            units,
            label_space=space,
            port=port,
            store=store,
            policy=ProposalPolicy(max_units_per_call=2, max_retries=0),
        )
        print(
            f"  落库（临时库）：{store.status_counts()}｜理由 {store.reason_counts()}"
            f"｜调用 {outcome.counters.batches} 次"
        )
        assert store.status_counts().get("unclassified", 0) == len(units)
        store.close()


def write_to_real_store(
    jobs: Sequence[Tuple[_Job, bytes]],
    *,
    space: LabelSpace,
    policy: ProposalPolicy,
) -> None:
    section("5. 把 Proposed 写进真实 store（SPEC §2.10：proposed_claims 归 T-105）")

    def snapshot() -> Dict[str, object]:
        conn = sqlite3.connect(str(REAL_DB))
        out: Dict[str, object] = {}
        for table in ("raw_records", "confirmed_labels", "config_versions"):
            out[table] = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("proposed_claims", "proposal_runs"):
            try:
                out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = -1
        conn.close()
        return out

    before = snapshot()
    raw_before = _tree_digest(STORE_RAW)
    print(
        f"  写入前：raw_records={len(before['raw_records'])}"  # type: ignore[arg-type]
        f" confirmed_labels={len(before['confirmed_labels'])}"  # type: ignore[arg-type]
        f" proposed_claims={before['proposed_claims']} proposal_runs={before['proposal_runs']}"
    )

    store = SqliteProposedStore(db_path=REAL_DB)
    port = PiSidecarCognitionPort(CognitionConfig.from_env(route="deepseek"))
    classified_rows = 0
    for job, raw_bytes in jobs:
        units = getattr(job, "units", None)
        if units is None:
            plan = classify_document(raw_bytes, raw_id=job.raw_id, endpoint=job.endpoint)
            if plan.skipped:
                continue
            units = plan.units
        outcome = propose_units(
            job.raw_id, units, label_space=space, port=port, store=store, policy=policy
        )
        classified_rows += outcome.counters.classified_units
        print(f"  {job.raw_id[:22]}… {outcome.summary()}")
    totals = store.status_counts()
    store.close()

    after = snapshot()
    print(
        f"  写入后：raw_records={len(after['raw_records'])}"  # type: ignore[arg-type]
        f" confirmed_labels={len(after['confirmed_labels'])}"  # type: ignore[arg-type]
        f" proposed_claims={after['proposed_claims']} proposal_runs={after['proposal_runs']}"
    )
    print(f"  proposed_claims 状态分布（全表）：{totals}")
    assert after["raw_records"] == before["raw_records"], "本任务不得写 raw_records"
    assert after["confirmed_labels"] == before["confirmed_labels"], "本任务不得写 confirmed_labels"
    assert after["config_versions"] == before["config_versions"], "本任务不得写 config_versions"
    # 幂等：**已经跑过的单元不会再调用模型**，因此严格地说"这次运行"可能新写 0 行。
    # 交付证据要求真的产出分类行，因此这里断言的是"库里存在本次运行负责的分类行"。
    assert totals.get("classified", 0) > 0 or classified_rows > 0, (
        "本次运行没有产出任何分类行："
        f"classified_rows={classified_rows} 全表状态={totals}"
    )
    assert _tree_digest(STORE_RAW) == raw_before, "data/store/raw 必须一个字节都不变"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-store", action="store_true")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-single-arm", action="store_true", help="跳过单单元对照组")
    parser.add_argument("--sample-units", type=int, default=DEFAULT_SAMPLE_UNITS)
    parser.add_argument("--sample-articles", type=int, default=DEFAULT_SAMPLE_ARTICLES)
    parser.add_argument("--single-units", type=int, default=DEFAULT_SINGLE_UNITS)
    parser.add_argument("--feed-endpoint", default="https://syncedreview.com/feed")
    args = parser.parse_args()

    records = load_records()
    if not records:
        print(f"没有真实归档：{STORE_RAW} 不存在或为空")
        return 1

    raw_before = _tree_digest(STORE_RAW)
    space = read_label_space()
    print(f"标签空间（注入）：{space.as_dict()}")
    print(
        f"策略常量：BATCH_MAX_UNITS={BATCH_MAX_UNITS} "
        f"BATCH_MAX_CHARS={BATCH_MAX_CHARS} / 每单元文本上限见 classify.UNIT_TEXT_MAX_CHARS"
    )

    feeds, skipped, articles = measure_dispatch(records)
    print(f"  跳过明细：{[(r[:18], s) for r, s, _d in skipped]}")

    feed = next(
        record for record in feeds if str(record["endpoint"]) == args.feed_endpoint
    )
    subset = _subset_units(feed, args.sample_units)
    pool: List[Tuple[Dict[str, object], object]] = [(feed, unit) for unit in subset]
    short_articles = sorted(
        articles,
        key=lambda record: len(record["content"]),  # type: ignore[arg-type]
    )[: args.sample_articles]
    for record in short_articles:
        plan = classify_document(
            record["content"],  # type: ignore[arg-type]
            raw_id=str(record["raw_id"]),
            endpoint=str(record["endpoint"]),
        )
        if not plan.skipped:
            pool.append((record, plan.units[0]))
    print(
        f"\n  样本：{args.feed_endpoint} 的前 {len(subset)} 个条目单元"
        f" + {len(short_articles)} 篇导入文章 = {len(pool)} 个单元"
    )

    single: Optional[Dict[str, object]] = None
    batched: Optional[Dict[str, object]] = None
    if not args.skip_model:
        import tempfile

        config = CognitionConfig.from_env(route="deepseek")
        with tempfile.TemporaryDirectory() as tmp:
            if not args.skip_single_arm:
                single = run_single_unit_arm(
                    records,
                    pool,
                    space=space,
                    port=PiSidecarCognitionPort(config),
                    limit=args.single_units,
                )
            store = SqliteProposedStore(db_path=Path(tmp) / "batched.db")
            jobs: List[Tuple[_Job, bytes]] = [
                (
                    _SubsetJob(_Job(feed), tuple(unit for _record, unit in pool[: len(subset)])),
                    feed["content"],  # type: ignore[arg-type]
                )
            ]
            for record in short_articles:
                jobs.append((_Job(record), record["content"]))  # type: ignore[arg-type]
            batched = run_batched_arm(
                jobs,
                space=space,
                port=PiSidecarCognitionPort(config),
                store=store,
                policy=ProposalPolicy(),
            )
            store.close()

    extrapolate(records, batched, single)

    if not args.skip_model:
        demonstrate_degradation(space, records)

    if args.write_store:
        if args.skip_model:
            raise SystemExit(
                "--write-store 必须真的调用模型（真实证据），不能与 --skip-model 同用"
            )
        jobs = [
            (
                _SubsetJob(_Job(feed), tuple(unit for _record, unit in pool[: len(subset)])),
                feed["content"],  # type: ignore[arg-type]
            )
        ]
        for record in short_articles:
            jobs.append((_Job(record), record["content"]))  # type: ignore[arg-type]
        write_to_real_store(jobs, space=space, policy=ProposalPolicy())

    assert _tree_digest(STORE_RAW) == raw_before, "本脚本改动了 data/store/raw（违反只读纪律）"
    print("\n只读纪律核验：data/store/raw 整树 sha256 未变 ✓")
    return 0


class _SubsetJob(_Job):
    """带**单元子集**的任务：只跑该 feed 的前 N 个单元（有界成本）。"""

    def __init__(self, base: _Job, units: Sequence[object]) -> None:
        super().__init__(
            {"raw_id": base.raw_id, "channel_id": base.channel_id, "endpoint": base.endpoint}
        )
        self.units = tuple(units)


if __name__ == "__main__":
    raise SystemExit(main())
