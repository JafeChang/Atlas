"""T-120 瘦命令行入口：`python -m atlas.compose ...`。

四条子命令，全部只做"调用组合根"这一件事：

| 命令 | 作用 | 需要联网？ |
|---|---|---|
| `plan` | 打印将要执行的 DAG 与本轮渠道（**干跑**），每个渠道附 `due` / `last_collected_at` | 否 |
| `run` | 真实跑一次流水线（采集 → 归档 → 归一化 → feed → 打标 → 证据校验） | 是（且需 `ATLAS_LIVE=1`） |
| `evidence` | **只做证据校验**（T-107）：不采集，把 `proposed_claims` 的分类行校验成锚点 | 否 |
| `register` | 往注册表里配一个行业 + 渠道 | 否 |

**真实抓取默认关闭**：`run` 要求环境变量 `ATLAS_LIVE=1`，否则以退出码 2 明确拒绝
（SPEC §2.12 的合规默认值是"不主动抓"，而不是"默默抓了"）。该模式下走的是
`atlas.collect` 的真实实现，因此 robots 检查与同域限速**照常生效**，不存在旁路。

**T-207 due-only 模式（默认关闭）**

`run --due-only` / `plan --due-only` 只考虑 `channel.interval_seconds` 已到期的渠道
（判定规则与系统 cron 示例见 `atlas.schedule` 的模块文档）。三种结局被显式区分：

| 情形 | `run --due-only` | `plan --due-only` |
|---|---|---|
| 一个可采集渠道都没有 | 退出码 **1**（配置问题：`NoSchedulableChannelError`），响亮失败 | 退出码 **0**，JSON + 一行"没有可采集渠道（配置问题）"——只读查询对任何状态都成立 |
| 有渠道但**都还没到期** | 退出码 **0**：**正常状态**，打印一行说明，什么都不做 | 退出码 **0**，`schedule.idle=true` |
| 有渠道到期 | 照常跑；节点失败仍是退出码 1 | 退出码 0，列出到期渠道 |

第二行是这里的重点：5 分钟一次的 cron 大多数轮次无事可做，把它算成失败会
每小时报 11 次假故障。但也**绝不**用"报成功"假装采过 —— 输出里明确写"没有到期的
渠道，本轮不采集"，而不是渲染一份空报告。第一行的两种退出码差异也是刻意的：
"要采集"是一个动作（没有渠道就没法做），"看一眼计划"是一个查询（空配置也该能看）。

失败语义：节点失败时 `TaskFailedError` 向上传播到本层，本层把它与
`partial_report`（失败节点 + 被阻塞的下游）打印到 stderr 并返回**非零退出码**；
本层没有任何"部分成功即 success"的路径。

**T-107 的证据校验（`evidence` 子命令）**

`evidence` 是**离线**的（不采集）：它按 `--raw-id` 收窄到指定原文，
把 T-105 的 `classified` 行按 quote 做确定性匹配并落进 `evidence_spans`。
三条边界：

1. **坐标只来自确定性匹配**，本层不接收任何坐标参数；
2. **校验失败的行不落库**，但**必须在报告里可见**（`render_evidence` / `render_evidence_report`
   会逐条打印），绝不"报成功然后把未验证的证据藏起来"；
3. `--read-only` 只校验不落库（`wrote_to_store=false`），用于复核既有结论。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence

from atlas.contracts import ContractError
from atlas.registry.schema import Channel, FetchSpec, FetchType, Industry
from atlas.runner import RunReport, TaskFailedError
from atlas.runner.runner import STATUS_FAILED, STATUS_SKIPPED
from atlas.schedule import ScheduleError

from .pipeline import NODE_EVIDENCE, LabelAssignment, NothingDueError, build_pipeline
from .tasks import PipelineError, parse_raw_ids

__all__ = [
    "build_parser",
    "main",
    "render_evidence",
    "render_evidence_report",
    "render_failure",
    "render_report",
    "render_schedule",
]

LIVE_ENV_VAR = "ATLAS_LIVE"


def _store_root_default() -> str:
    return os.environ.get("ATLAS_STORE_ROOT", "data/store")


def _parse_now(value: Optional[str]) -> Optional[datetime]:
    """`--now`：调度判定用的当前时刻（naive 按 UTC 解释，与全仓同一规则）。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise SystemExit("--now 不得为空字符串")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"--now 不是 ISO-8601 时间：{value!r}（{exc}）") from exc
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.compose",
        description=(
            "Atlas 组合根（T-120）：把采集 / 归档 / 归一化 / feed / 打标接成一条流水线。"
            "T-207 追加 --due-only：只采 interval_seconds 已到期的渠道。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--store-root",
            default=_store_root_default(),
            help="存储根（默认 %(default)s；测试请用临时目录）",
        )
        target.add_argument("--author", default="operator", help="配置变更的作者（审计用）")
        target.add_argument("--actor", default=None, help="打标判断的作者（默认同 --author）")

    def add_schedule_flags(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--due-only",
            action="store_true",
            help=(
                "T-207：只注册 interval_seconds 已到期的渠道（默认关闭 = 全部启用渠道）。"
                "一个都不到期时**不是错误**：打印说明并以退出码 0 结束"
            ),
        )
        target.add_argument(
            "--now",
            default=None,
            help="调度判定用的当前时刻（ISO-8601；naive 按 UTC 解释，默认取系统时钟）",
        )

    plan = sub.add_parser("plan", help="干跑：打印 DAG 与本轮渠道（含到期判定），不采集")
    add_common(plan)
    add_schedule_flags(plan)

    run = sub.add_parser("run", help="真实跑一次流水线（需 ATLAS_LIVE=1）")
    add_common(run)
    add_schedule_flags(run)
    run.add_argument(
        "--window",
        default=None,
        help="轮询窗口起点（ISO-8601，UTC）；同一窗口重跑 = 同一份输入（幂等）",
    )
    run.add_argument(
        "--label-channel",
        action="append",
        default=[],
        metavar="CHANNEL:KEY:VALUE",
        help="给某渠道本轮采集到的文档打标（可重复）",
    )
    run.add_argument(
        "--label-raw",
        action="append",
        default=[],
        metavar="RAW_ID:KEY:VALUE",
        help="给指定 raw_id 打标（可重复）",
    )
    run.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help="每个节点的重试次数（默认 %(default)s）",
    )
    run.add_argument(
        "--allow-partial",
        action="store_true",
        help=(
            "采集阶段个别渠道失败时继续（默认失败即中止）。"
            "开启后失败会进入报告的 observed.failures，不会被吞掉"
        ),
    )

    register = sub.add_parser("register", help="往注册表里配一个行业 + 渠道")
    add_common(register)
    register.add_argument("--industry-id", required=True)
    register.add_argument("--industry-name", default=None, help="默认同 --industry-id")
    register.add_argument("--channel-id", required=True)
    register.add_argument("--endpoint", required=True)
    register.add_argument(
        "--type",
        choices=[item.value for item in FetchType],
        default=FetchType.RSS.value,
    )
    register.add_argument("--interval-seconds", type=int, default=3600)
    register.add_argument("--rate-limit-seconds", type=int, default=None)
    register.add_argument("--tag", action="append", default=[])

    evidence = sub.add_parser(
        "evidence",
        help=(
            "T-107：把 proposed_claims 的分类行校验成确定性锚点并落进 evidence_spans"
            "（离线：不采集、不打网络）"
        ),
    )
    add_common(evidence)
    evidence.add_argument(
        "--raw-id",
        action="append",
        default=[],
        metavar="RAW_ID",
        help=(
            "只校验这些原文（可重复）；缺省 = 归档里的全部原文。"
            "指定的 raw_id 不在归档里会**响亮失败**（不静默取交集）"
        ),
    )
    evidence.add_argument(
        "--read-only",
        action="store_true",
        help="只校验不落库（报告里 wrote_to_store=false）；缺省 = 真的写 evidence_spans",
    )

    return parser


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def render_observed(name: str, observed: Any) -> List[str]:
    """把 `observed` 里的**失败与跳过**显式打出来。

    为什么必须打：`identity` 只含内容寻址字段（**不含失败**），而 `--allow-partial`
    对操作者的承诺是"个别渠道失败时继续，失败会进入 `observed.failures`，**不会被吞掉**"。
    只打印 `identity` 会让"13 个渠道里 3 个失败"看起来像"一切正常"。

    实测踩到过：一次真实运行报 `failed: 0`、feed 有 10 条，
    操作者完全看不出 openai-blog(403) / ai-techpark(robots 403) / venturebeat-ai(429) 三个源根本没采到。
    这是"静默失效"的典型形态（SPEC §7.3 失败模式 3），因此必须显式呈现。
    """
    if not isinstance(observed, Mapping):
        return []

    failures = list(observed.get("failures") or [])
    skipped = list(observed.get("skipped") or [])
    per_channel = list(observed.get("per_channel") or [])

    lines: List[str] = []
    if per_channel:
        collected = sum(1 for item in per_channel if item.get("status") == "collected")
        lines.append(
            f"  观察 {name}：渠道 {len(per_channel)}"
            f"（collected={collected} failed={len(failures)} skipped={len(skipped)}）"
        )
    for item in failures:
        lines.append(
            f"    [failed] {item.get('channel_id')}  "
            f"{item.get('kind')}: {item.get('reason')}"
        )
    for item in skipped:
        lines.append(
            f"    [skipped] {item.get('channel_id')}  {item.get('note')}"
        )
    return lines


def render_evidence(observed: Any) -> List[str]:
    """把 T-107 证据校验的关键计数与**失败逐条**打出来。

    为什么必须单独渲染（这是本项目踩过的真实缺陷形态）：`identity` 里只有
    "哪些 claim 取得了锚点"，**不含**校验失败的行。若只打印 identity，
    "3 条 quote 在原文里找不到"会显示成"一切正常" —— 与 SPEC §7.3 失败模式 3
    （静默失效）完全同形。因此这里显式打印：

    - 计数：范围内的 raw / 分类行 / 已验证 / 校验失败 / **跳过的未分类行** /
      本轮新写入的 span / 已在库中的 span / 库中总行数；
    - 每一条**校验失败**（claim + raw + quote）；
    - `raws_without_claims`（归一化过但库里没有分类行的 raw）—— 它们不是错误，
      但"校验了 0 条"必须能一眼看出原因。
    """
    if not isinstance(observed, Mapping):
        return []
    if "claims_in_scope" not in observed and "verified" not in observed:
        return []

    lines: List[str] = [
        "  观察 evidence："
        f"raws_in_scope={observed.get('raws_in_scope')}"
        f" classified_claims={observed.get('classified_claims')}"
        f" verified={observed.get('verified')}"
        f" verification_failed={observed.get('verification_failed')}"
        f" skipped_unclassified={observed.get('skipped_unclassified')}"
        f" spans_written={observed.get('spans_written')}"
        f" spans_unchanged={observed.get('spans_unchanged')}"
        f" spans_in_store={observed.get('spans_in_store')}"
        f" wrote_to_store={observed.get('wrote_to_store')}"
    ]
    if str(observed.get("evidence_db")):
        lines.append(f"    证据库：{observed.get('evidence_db')}")

    failures = list(observed.get("failures") or [])
    if failures:
        lines.append(
            f"    ⚠️ {len(failures)} 条 claim 的 quote 在原文里**匹配不到**"
            "（状态 failed、**未落库**）："
        )
        for item in failures:
            lines.append(
                f"      [未验证] {item.get('claim_id')}@v{item.get('claim_version')} "
                f"raw={item.get('raw_id')} quote={item.get('quote')!r}"
            )

    without = list(observed.get("raws_without_claims") or [])
    if without:
        lines.append(
            f"    [无 claim] 本轮归一化过但 proposed_claims 里没有分类行的 raw（{len(without)}）："
            f"{without}"
        )
    if observed.get("classified_claims") == 0:
        lines.append(
            "    ⚠️ 本轮范围里没有任何 classified 行："
            "校验了 0 条，**没有**产生证据（这不是'全部通过'）"
        )
    return lines


def render_report(report: RunReport) -> str:
    lines: List[str] = ["执行汇总（拓扑序）："]
    for name in report.order:
        result = report.result(name)
        suffix = ""
        if result.status == STATUS_SKIPPED:
            suffix = f" ← {result.reason}"
        elif result.status == STATUS_FAILED:
            suffix = f" ← {result.error}"
        lines.append(f"  [{result.status}] {name}{suffix}")
    counts = {
        "succeeded": len(report.succeeded),
        "skipped": len(report.skipped),
        "failed": len(report.failed),
        "blocked": len(report.blocked),
    }
    lines.append(f"计数：{json.dumps(counts, ensure_ascii=False, sort_keys=True)}")
    for name in report.order:
        result = report.result(name)
        if result.output is None:
            continue
        identity = result.output.artifacts.get("identity", {})
        lines.append(f"产物 {name}：{json.dumps(identity, ensure_ascii=False, sort_keys=True)}")
        observed = result.output.artifacts.get("observed")
        lines.extend(render_observed(name, observed))
        if name == NODE_EVIDENCE:
            # T-107 的失败与跳过**不在** identity 里，必须单独渲染（见 render_evidence）。
            lines.extend(render_evidence(observed))
    return "\n".join(lines)


def render_schedule(decision: Any) -> str:
    """把一轮调度判定渲染成人能读的几行（CLI 的 `plan --due-only` / 空闲轮次用）。

    必须显式给出"哪些到期、哪些没到期、上次什么时候采的"：只打一句"无到期渠道"
    会让操作者无法判断是**真的没到期**还是**状态读不到**（后者会是响亮失败，
    但把两者摆在一起才看得出区别）。`enabled_channels == 0` 时明确说"没有渠道"，
    而不是说"没有到期的渠道"——两者退出码不同，不能混。
    """
    lines: List[str] = [
        f"调度判定（now={decision.now.isoformat()}，可采集渠道 {decision.enabled_channels} 个）："
    ]
    if decision.enabled_channels == 0:
        lines.append("注册表里一个可采集渠道都没有（配置问题，不是空闲轮次）")
        return "\n".join(lines)
    for item in decision.schedules:
        last = item.last_collected_at.isoformat() if item.last_collected_at else "（从未采集）"
        mark = "到期" if item.due else "未到期"
        lines.append(
            f"  [{mark}] {item.channel_id}  interval={item.interval_seconds}s  "
            f"last_collected_at={last}  next_due_at={item.next_due_at.isoformat()}"
        )
    lines.append(f"到期渠道（{len(decision.due_ids)}）：{list(decision.due_ids)}")
    if decision.is_idle:
        lines.append(
            "没有到期的渠道，本轮不采集（正常状态，不是错误；退出码 0）"
        )
    return "\n".join(lines)


def render_failure(error: TaskFailedError) -> str:
    lines = [
        "流水线失败：",
        f"  失败节点：{error.task_name}（{error.attempts} 次尝试均未成功）",
        f"  原因：{type(error.last_error).__name__}: {error.last_error}",
    ]
    partial = error.partial_report
    if partial is not None:
        lines.append("  已完成：")
        for name in partial.order:
            result = partial.result(name)
            lines.append(f"    [{result.status}] {name}")
        blocked = [item.task_name for item in partial.blocked]
        if blocked:
            lines.append(f"  因失败被阻塞（未执行）：{blocked}")
        lines.append(
            f"  没有得到任何成功产物：{[item.task_name for item in partial.succeeded] or '无'}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def _parse_assignment(text: str, *, kind: str, actor: str) -> LabelAssignment:
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise SystemExit(
            f"--label-{kind} 需要 CHANNEL:KEY:VALUE 或 RAW_ID:KEY:VALUE 形式，收到 {text!r}"
        )
    target, key, value = (part.strip() for part in parts)
    if kind == "channel":
        return LabelAssignment(
            channel_id=target, label_key=key, label_value=value, actor=actor
        )
    return LabelAssignment(raw_id=target, label_key=key, label_value=value, actor=actor)


def _cmd_plan(args: argparse.Namespace) -> int:
    now = _parse_now(args.now)
    with build_pipeline(store_root=args.store_root, actor=args.actor or args.author) as pipe:
        plan = pipe.plan(due_only=args.due_only, now=now)
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        if args.due_only:
            # 人读的一行行判定走 stderr，stdout 保持"纯 JSON"（脚本可直接 json.loads）。
            print(render_schedule(pipe.due_decision(now=now)), file=sys.stderr)
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    with build_pipeline(store_root=args.store_root, actor=args.actor or args.author) as pipe:
        service = pipe.registry
        service.create_industry(
            Industry(
                id=args.industry_id,
                name=args.industry_name or args.industry_id,
                enabled=True,
            ),
            author=args.author,
            note="CLI register",
        )
        service.create_channel(
            Channel(
                id=args.channel_id,
                industry_id=args.industry_id,
                type=FetchType(args.type),
                endpoint=args.endpoint,
                fetch_spec=FetchSpec(type=FetchType(args.type)),
                interval_seconds=args.interval_seconds,
                rate_limit_seconds=args.rate_limit_seconds,
                enabled=True,
                tags=tuple(args.tag),
            ),
            author=args.author,
            note="CLI register",
        )
        plan = pipe.plan()
    print(json.dumps(plan["channels"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if os.environ.get(LIVE_ENV_VAR) != "1":
        print(
            f"默认关闭真实采集：设置环境变量 {LIVE_ENV_VAR}=1 后才允许对真实渠道发起请求"
            "（SPEC §2.12 的合规默认值）。\n"
            "想先看看将要执行什么，可以运行：python -m atlas.compose plan",
            file=sys.stderr,
        )
        return 2

    actor = args.actor or args.author
    now = _parse_now(args.now)
    assignments = [
        _parse_assignment(text, kind="channel", actor=actor)
        for text in args.label_channel
    ] + [_parse_assignment(text, kind="raw", actor=actor) for text in args.label_raw]

    pipeline = build_pipeline(
        store_root=args.store_root,
        actor=actor,
        window=args.window,
        label_assignments=assignments,
        on_channel_failure="report" if args.allow_partial else "fail",
        max_retries=args.max_retries,
    )
    with pipeline:
        if args.due_only:
            decision = pipeline.due_decision(now=now)
            if decision.enabled_channels == 0:
                # 与 `run --due-only` 同一条纪律：**一个可采集渠道都没有**是配置问题
                # （退出码 1），绝不能因为 due-only 而退化成"正常空闲"（退出码 0）。
                print(render_schedule(decision))
                print(
                    "注册表里没有可采集的渠道（启用渠道 × 启用行业 为空）："
                    "这是配置问题，不是'这一轮没有到期的渠道'；"
                    "先配置渠道（python -m atlas.compose register ...）",
                    file=sys.stderr,
                )
                return 1
            if decision.is_idle:
                # 正常状态，退出码 0：5 分钟一次的 cron 大多数轮次走这里。
                print(render_schedule(decision))
                return 0
            print(render_schedule(decision))
        try:
            report = pipeline.run(due_only=args.due_only, now=now)
        except NothingDueError as exc:
            # 判定与执行之间状态变了（例如另一个进程刚采完）：仍然是"没有工作"，
            # 仍然退出码 0，但如实说明是哪一种"没有工作"。
            print(render_schedule(exc.decision), file=sys.stderr)
            return 0
        except TaskFailedError as exc:
            print(render_failure(exc), file=sys.stderr)
            return 1
        print(render_report(report))
        if report.failed or report.blocked:
            # 兜底：执行器只有在真的失败时才抛异常；这里再确认一次，绝不"报了成功却又失败"。
            print("报告里存在失败/阻塞节点，本次运行不算成功", file=sys.stderr)
            return 1
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    """T-107：把 `proposed_claims` 的分类行校验成锚点（**离线**，不采集、不打网络）。

    走的不是 `TaskRunner`，而是组合根给出的**同一份**输入快照 + 同一个阶段实例：
    证据校验的输入是"归档里已有的字节 + 库里已有的 claim"，重跑采集既不必要、
    也会让这条命令变成"必须有网才能用一次"。代价是失去执行器的重试与执行记录，
    因此这里显式打印阶段自己算出的幂等键（见下），让"同输入同输出"仍然可核对。

    本命令**不要求** `ATLAS_LIVE=1`：它一个网络请求都不发。
    """
    pipeline = build_pipeline(
        store_root=args.store_root,
        actor=args.actor or args.author,
        raw_ids=parse_raw_ids(args.raw_id),
        evidence_read_only=args.read_only,
    )
    with pipeline:
        stage, inputs, config = pipeline.collect_evidence_input()
        output = stage.execute(inputs, config)
        print(render_evidence_report(output))
        if not output.artifacts["observed"]["wrote_to_store"] and not args.read_only:
            # 不可能状态：只有 --read-only 才允许不落库。
            print("证据未落库但未指定 --read-only：这是接线错误", file=sys.stderr)
            return 1
    return 0


def render_evidence_report(output: Any) -> str:
    """渲染一次离线证据校验的产物（计数 + 失败逐条 + 幂等键）。"""
    lines: List[str] = [
        f"证据校验（T-107，离线）：任务 {output.task_name}",
        f"  幂等键：{output.idempotency_key}",
        "产物 identity："
        + json.dumps(output.artifacts.get("identity", {}), ensure_ascii=False, sort_keys=True),
    ]
    lines.extend(render_evidence(output.artifacts.get("observed")))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handlers = {
        "plan": _cmd_plan,
        "run": _cmd_run,
        "register": _cmd_register,
        "evidence": _cmd_evidence,
    }
    handler = handlers[args.command]
    try:
        return handler(args)
    except (PipelineError, ScheduleError, ContractError) as exc:
        # 领域错误：如实报错并非零退出，不吞、不降级。
        # 注意 `NothingDueError` 是 `PipelineError` 的子类，但**不会**走到这里：
        # "没有到期的渠道"由上面的 `is_idle` 分支 / 专门的 `except` 处理成退出码 0。
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 由 __main__.py 覆盖
    raise SystemExit(main())
