"""T-120 瘦命令行入口：`python -m atlas.compose ...`。

三条子命令，全部只做"调用组合根"这一件事：

| 命令 | 作用 | 需要联网？ |
|---|---|---|
| `plan` | 打印将要执行的 DAG 与本轮渠道（**干跑**） | 否 |
| `run` | 真实跑一次流水线（采集 → 归档 → 归一化 → feed → 打标） | 是（且需 `ATLAS_LIVE=1`） |
| `register` | 往注册表里配一个行业 + 渠道 | 否 |

**真实抓取默认关闭**：`run` 要求环境变量 `ATLAS_LIVE=1`，否则以退出码 2 明确拒绝
（SPEC §2.12 的合规默认值是"不主动抓"，而不是"默默抓了"）。该模式下走的是
`atlas.collect` 的真实实现，因此 robots 检查与同域限速**照常生效**，不存在旁路。

失败语义：节点失败时 `TaskFailedError` 向上传播到本层，本层把它与
`partial_report`（失败节点 + 被阻塞的下游）打印到 stderr 并返回**非零退出码**；
本层没有任何"部分成功即 success"的路径。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

from atlas.contracts import ContractError
from atlas.registry.schema import Channel, FetchSpec, FetchType, Industry
from atlas.runner import RunReport, TaskFailedError
from atlas.runner.runner import STATUS_FAILED, STATUS_SKIPPED

from .pipeline import LabelAssignment, build_pipeline
from .tasks import PipelineError

__all__ = ["build_parser", "main", "render_failure", "render_report"]

LIVE_ENV_VAR = "ATLAS_LIVE"


def _store_root_default() -> str:
    return os.environ.get("ATLAS_STORE_ROOT", "data/store")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.compose",
        description=(
            "Atlas 组合根（T-120）：把采集 / 归档 / 归一化 / feed / 打标接成一条流水线。"
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

    plan = sub.add_parser("plan", help="干跑：打印 DAG 与本轮渠道，不采集")
    add_common(plan)

    run = sub.add_parser("run", help="真实跑一次流水线（需 ATLAS_LIVE=1）")
    add_common(run)
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

    return parser


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


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
    with build_pipeline(store_root=args.store_root, actor=args.actor or args.author) as pipe:
        plan = pipe.plan()
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
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
        try:
            report = pipeline.run()
        except TaskFailedError as exc:
            print(render_failure(exc), file=sys.stderr)
            return 1
        print(render_report(report))
        if report.failed or report.blocked:
            # 兜底：执行器只有在真的失败时才抛异常；这里再确认一次，绝不"报了成功却又失败"。
            print("报告里存在失败/阻塞节点，本次运行不算成功", file=sys.stderr)
            return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handlers = {"plan": _cmd_plan, "run": _cmd_run, "register": _cmd_register}
    handler = handlers[args.command]
    try:
        return handler(args)
    except (PipelineError, ContractError) as exc:
        # 领域错误：如实报错并非零退出，不吞、不降级。
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - 由 __main__.py 覆盖
    raise SystemExit(main())
