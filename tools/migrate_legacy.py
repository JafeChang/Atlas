#!/usr/bin/env python
"""T-131 组合根：把旧语料 `data/raw/` 逐篇导入 `data/store/raw/`，并打印对账表。

为什么这个脚本值得入库
----------------------

1. **它是 T-131 的唯一"真实数据流"入口**（硬规则 1）：`src/atlas/migrate/` 只提供机制，
   行业归属是**注入**的（§4.0 禁止 `atlas.migrate` import `atlas.registry`）。
   把"从注册表读渠道 → 构造映射 → 驱动导入"这条接线固定成一等公民，
   才不会退化成"某次手工跑过的命令"。
2. **它把幂等证明变成可复现的操作**：`--twice` 会连跑两次并打印
   `raw_records` / 目录数 / 字节数的前后对比 —— 这正是验收要的证据。
3. **它只写 `data/store/`**，绝不写 `data/raw/`；`--dry-run` 时连 store 都不写。

用法（在 WSL 内，仓库根目录）::

    ./.venv-new/bin/python tools/migrate_legacy.py --twice
    ./.venv-new/bin/python tools/migrate_legacy.py --dry-run
    ./.venv-new/bin/python tools/migrate_legacy.py --channel-map tools/legacy_channel_map.json

`--dry-run` 只做扫描与分类（不打开归档、不写任何字节），用来在导入前核对"会导入多少"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 让脚本在被直接执行时也能 import 到 src/（无需先 pip install -e）。
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from atlas.migrate import (  # noqa: E402
    DEFAULT_ARCHIVE_ROOT,
    DEFAULT_LEGACY_ROOT,
    REPO_ROOT,
    Migrate,
    MigrateOptions,
    build_channel_map,
    iter_legacy_files,
    load_channel_map_file,
    unmapped_names,
)
from atlas.migrate.source import classify_legacy, scan_legacy_files  # noqa: E402


def _registry_service(db_path: Path):
    """组合根**唯一**允许 import `atlas.registry` 的地方（§4.0 的接线规则）。

    为什么放在这里而不是包内：`atlas.migrate` 若 import `atlas.registry`，
    就违反了"跨包只允许指向 DAG 上游"（registry 不在 T-103 的上游），
    也会让导入包无法在只有归档层的场景里使用。
    """
    from atlas.registry import RegistryService, open_store

    return RegistryService(open_store(db_path, author="t131-migrate"))


def build_live_channel_map(db_path: Path, legacy_channel_names: List[str]):
    """由注册表构造映射；精确匹配不到的名字用**显式记录**的方式报告。

    返回 `(映射, 未匹配的旧频道名, 是否动用了低置信度前缀推导)`。
    """
    service = _registry_service(db_path)
    channels = service.list_channels()
    exact = build_channel_map(channels)
    missing = unmapped_names(exact, legacy_channel_names)
    if not missing:
        return exact, [], False
    # 低置信度回退：**显式**打开，并在输出里声明（不静默）。
    derived = build_channel_map(channels, derive_industry_prefixes=True)
    return derived, unmapped_names(derived, legacy_channel_names), True


def _dry_run(legacy_root: Path) -> int:
    """只扫描与分类：不打开归档、不写任何字节。"""
    counts: Dict[str, int] = {}
    per_channel: Dict[str, Tuple[int, int]] = {}
    for scanned in scan_legacy_files(legacy_root):
        if scanned.error is not None:
            key = f"FAIL:{scanned.error.reason}"
        else:
            skip = classify_legacy(scanned.payload or {})
            key = f"SKIP:{skip}" if skip else "DOC"
        counts[key] = counts.get(key, 0) + 1
        channel = scanned.file.channel
        docs, other = per_channel.get(channel, (0, 0))
        if key == "DOC":
            docs += 1
        else:
            other += 1
        per_channel[channel] = (docs, other)
    total = sum(counts.values())
    print(f"[dry-run] 旧语料根 {legacy_root}")
    print(f"[dry-run] 扫描 {total} 个 JSON")
    for key in sorted(counts):
        print(f"[dry-run]   {key:<28} {counts[key]}")
    print("[dry-run] 逐频道（文档 / 其它）：")
    for channel in sorted(per_channel):
        docs, other = per_channel[channel]
        print(f"[dry-run]   {channel:<20} {docs:>4} / {other:>4}")
    return 0


def _snapshot(store_root: Path) -> Dict[str, object]:
    """`data/store` 的可比较快照（**只读**：走 sqlite 只读连接 + 目录遍历）。"""
    import sqlite3

    db_path = store_root / "atlas.db"
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT COUNT(*) AS n FROM raw_records").fetchone()["n"]
        tables = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        triggers = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        ]
        raw_digest = connection.execute(
            "SELECT COALESCE(SUM(byte_length), 0) AS total FROM raw_records"
        ).fetchone()["total"]
    finally:
        connection.close()
    manifest = {}
    for path in sorted(store_root.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(store_root))] = path.stat().st_size
    return {
        "rows": rows,
        "tables": tables,
        "triggers": triggers,
        "declared_bytes": raw_digest,
        "manifest": manifest,
    }


def _print_delta(before: Dict[str, object], after: Dict[str, object], title: str) -> None:
    added = sorted(set(after["manifest"]) - set(before["manifest"]))
    removed = sorted(set(before["manifest"]) - set(after["manifest"]))
    changed = sorted(
        key
        for key in set(before["manifest"]) & set(after["manifest"])
        if before["manifest"][key] != after["manifest"][key]
    )
    print(f"\n== {title} ==")
    print(f"raw_records: {before['rows']} → {after['rows']}")
    print(f"声明字节数  : {before['declared_bytes']} → {after['declared_bytes']}")
    print(f"文件数      : {len(before['manifest'])} → {len(after['manifest'])}")
    print(f"新增文件    : {len(added)}（其中 content.bin 形态 {sum(1 for k in added if k.endswith('content.bin'))}）")
    print(f"删除文件    : {len(removed)}")
    print(f"被修改文件  : {len(changed)}（archive 目录内的记录文件应为 0）")
    if changed:
        print("  被修改: " + ", ".join(changed[:10]))
    if before["tables"] != after["tables"]:
        print(f"⚠️ 表集合变化：{before['tables']} → {after['tables']}")
    if before["triggers"] != after["triggers"]:
        print(f"⚠️ 触发器集合变化：{before['triggers']} → {after['triggers']}")


def _print_samples(report, archive) -> None:
    import hashlib

    print("\n== 抽查（旧记录 → 新 raw_id → 磁盘字节）==")
    for entry in report.entries[:3]:
        if not entry.raw_id:
            continue
        on_disk = archive.blobs.content_path(entry.raw_id).read_bytes()
        digest = hashlib.sha256(on_disk).hexdigest()
        print(
            f"  {entry.relative}\n"
            f"    source_url={entry.endpoint}\n"
            f"    channel={entry.channel} → 注册表渠道 {entry.registry_channel}\n"
            f"    moment={entry.moment_field}={entry.moment_value} → fetched_at={entry.fetched_at}\n"
            f"    raw_id={entry.raw_id}\n"
            f"    磁盘 sha256={digest[:16]}… 与报告一致：{digest == entry.content_sha256}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T-131 旧语料导入（逐篇 → 不可变归档）")
    parser.add_argument("--legacy-root", default=str(DEFAULT_LEGACY_ROOT))
    parser.add_argument("--store-root", default=str(DEFAULT_ARCHIVE_ROOT))
    parser.add_argument(
        "--db-path",
        default=None,
        help="注册表库文件（默认 <store-root>/atlas.db，与归档共用同一个库）",
    )
    parser.add_argument("--channel-map", default=None, help="显式映射 JSON（{旧频道名: 渠道 id}）")
    parser.add_argument("--twice", action="store_true", help="连跑两次以证明幂等")
    parser.add_argument("--dry-run", action="store_true", help="只扫描分类，不写任何字节")
    parser.add_argument("--strict", action="store_true", help="第一条失败即中止（异常里带对账表）")
    args = parser.parse_args(argv)

    legacy_root = Path(args.legacy_root)
    store_root = Path(args.store_root)

    if args.dry_run:
        return _dry_run(legacy_root)

    from atlas.archive import open_archive

    if not store_root.is_dir():
        print(f"归档根不存在：{store_root}（先用 T-120 的组合根落一次盘）", file=sys.stderr)
        return 2

    legacy_names = sorted({entry.channel for entry in iter_legacy_files(legacy_root)})
    if args.channel_map:
        channel_map = load_channel_map_file(Path(args.channel_map))
        missing = unmapped_names(channel_map, legacy_names)
        derived = False
    else:
        channel_map, missing, derived = build_live_channel_map(
            Path(args.db_path) if args.db_path else store_root / "atlas.db", legacy_names
        )

    print("== 接线 ==")
    print(f"旧语料根  : {legacy_root}")
    print(f"归档根    : {store_root}")
    print(f"旧频道目录: {legacy_names}")
    print(f"渠道映射  : {json.dumps({k: list(v) for k, v in sorted(channel_map.items())}, ensure_ascii=False)}")
    if derived:
        print("⚠️ 动用了**低置信度**前缀推导（精确匹配不到的旧名见下）")
    if missing:
        print(f"⚠️ 映射不到的旧频道名（这些文件会进失败栏）：{missing}")

    archive = open_archive(store_root)
    before = _snapshot(store_root)
    print(f"导入前 raw_records: {before['rows']}；目录文件数 {len(before['manifest'])}")

    try:
        report = Migrate(
            MigrateOptions(
                legacy_root=legacy_root,
                archive_root=store_root,
                archive=archive,
                channel_map=channel_map,
                strict=args.strict,
            )
        ).run()
        print()
        print(report.render())
        _print_samples(report, archive)
        print(f"\narchive.verify() 问题数: {len(archive.verify())}")
        after = _snapshot(store_root)
        _print_delta(before, after, "第一次运行")

        if args.twice:
            second = Migrate(
                MigrateOptions(
                    legacy_root=legacy_root,
                    archive_root=store_root,
                    archive=archive,
                    channel_map=channel_map,
                    strict=args.strict,
                )
            ).run()
            print("\n== 第二次运行（幂等证明）==")
            print(
                f"新建 {second.imported} / 收敛 {second.deduplicated} / 失败 {second.failed}"
                f" / 扫描 {second.scanned_files}"
            )
            print(f"raw_records: {second.records_before} → {second.records_after}")
            after2 = _snapshot(store_root)
            _print_delta(after, after2, "第二次运行")
            if second.records_after != second.records_before:
                print("❌ 第二次运行改变了 raw_records 行数：幂等被破坏")
                return 1
            if len(second.ledger_problems()) != 0:
                print(f"❌ 第二次运行对账问题：{second.ledger_problems()}")
                return 1
            print("✅ 第二次运行 0 新建、行数不变 —— 幂等成立")
    finally:
        archive.close()

    problems = report.ledger_problems()
    if problems:
        print(f"\n❌ 对账问题：{problems}", file=sys.stderr)
        return 1
    if report.failed:
        print(f"\n⚠️ 有 {report.failed} 条失败（原因：{report.failure_counts()}）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
