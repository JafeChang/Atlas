"""T-207 真实调度探针：对**真实注册表 + 真实归档库**算一遍"这一轮该采哪些渠道"。

为什么是 `tools/`（而不是一次性脚本）
-------------------------------------

这个脚本回答一个**下个月还会再问一次**的问题：

> 现在这套配置（13 个渠道 / interval 1800 / 3600 / 7200，`raw_records` 里 N 条记录）
> 在某个时刻，due-only 的一轮到底会去试哪些渠道？

它**只读** `data/store/atlas.db`（`file:...?mode=ro`），不写一个字节、不抓一次网络，
因此在任何一天都可以重跑并得到与当天状态一致的答案。`tests/test_schedule_realdata.py`
用的是同一套 API 与口径；本脚本的价值是"脱离 pytest 直接给人看数字"，
并且把两层数字并排放在一起——**调度到的渠道 ≠ 真的会发请求的渠道**。

用法::

    # 用真实时钟
    ./.venv-new/bin/python tools/t207_schedule_probe.py

    # 指定时刻（可复现的取证）
    ./.venv-new/bin/python tools/t207_schedule_probe.py --now 2026-09-26T02:00:00+00:00

    # 按轮询窗口推进，看"哪些轮次会去注册渠道"
    ./.venv-new/bin/python tools/t207_schedule_probe.py \\
        --now 2026-09-26T00:00:00+00:00 --rounds 6 --step-minutes 30
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.registry import RegistryService, open_store as open_registry  # noqa: E402
from atlas.schedule import evaluate_schedule, open_last_collection_source  # noqa: E402

DEFAULT_STORE_ROOT = REPO_ROOT / "data" / "store"


def _parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _load(store_root: Path):
    """真实注册表 + 真实只读状态源。库缺失即**响亮失败**（不假装"什么都没采过"）。"""
    db_path = store_root / "atlas.db"
    if not db_path.is_file():
        raise SystemExit(
            f"真实存储库不存在：{db_path}\n"
            "（`data/` 不进 git；请在有数据的机器上跑，或先用 compose run 产生归档）"
        )
    store = open_registry(db_path, author="t207-probe")
    try:
        service = RegistryService(store)
        channels = service.fetchable_channels()
        config_version = service.config_version
    finally:
        store.close()
    source = open_last_collection_source(store_root)
    return channels, source.last_collected_at(), config_version


def _due_at(channels, last_collected, moment: datetime):
    try:
        return evaluate_schedule(channels, last_collected, moment)
    except Exception as exc:  # 响亮失败：探针不吞异常
        raise SystemExit(f"{type(exc).__name__}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-207 真实调度探针（只读）")
    parser.add_argument("--store-root", default=str(DEFAULT_STORE_ROOT))
    parser.add_argument("--now", default=None, help="判定时刻（ISO-8601；默认现在）")
    parser.add_argument(
        "--rounds", type=int, default=1, help="连续判定多少轮（默认 1）"
    )
    parser.add_argument(
        "--step-minutes", type=float, default=30.0, help="每轮推进多少分钟（默认 30）"
    )
    args = parser.parse_args(argv)

    channels, last_collected, config_version = _load(Path(args.store_root))
    print(f"[T-207 真实] 注册表 config_version={config_version}，可采集渠道 {len(channels)} 个")
    print(f"[T-207 真实] raw_records 里有采集记录的渠道 {len(last_collected)} 个")

    intervals = Counter(channel.interval_seconds for channel in channels)
    print(
        "[T-207 真实] interval_seconds 分布："
        + "，".join(f"{value}s×{count}" for value, count in sorted(intervals.items()))
    )
    missing = sorted(channel.id for channel in channels if channel.id not in last_collected)
    print(f"[T-207 真实] 从未采集（⇒ 必然到期）的渠道 {len(missing)} 个：{missing}")

    moment = _parse_now(args.now)
    for round_index in range(max(args.rounds, 1)):
        decision = _due_at(channels, last_collected, moment)
        print(
            f"[T-207 真实] 第 {round_index + 1} 轮 now={decision.now.isoformat()} → "
            f"到期 {len(decision.due_ids)}/{decision.enabled_channels}：{list(decision.due_ids)}"
        )
        for item in decision.schedules:
            last = (
                item.last_collected_at.isoformat() if item.last_collected_at else "（从未采集）"
            )
            mark = "到期  " if item.due else "未到期"
            print(
                f"[T-207 真实]   {mark} {item.channel_id:<26} interval={item.interval_seconds:>5}s "
                f"last={last} next_due={item.next_due_at.isoformat()}"
            )
        moment = moment + timedelta(minutes=args.step_minutes)

    print(
        "[T-207 真实] 提醒：以上是**调度层**（该试哪些渠道）。真抓与否还取决于运行器的幂等"
        "（同渠道载荷 + 同窗口 id ⇒ 跳过，见 atlas.schedule 模块文档）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
