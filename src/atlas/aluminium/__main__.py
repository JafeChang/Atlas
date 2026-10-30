"""python -m atlas.aluminium：导入、受控验证、结构化提取与导出。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections import Counter
from pathlib import Path

from .sources import configure_rss_source, import_sources, load_manifest, probe_sources
from .store import AluminiumStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="海外电解铝产能监测")
    parser.add_argument(
        "--store-root", default=os.environ.get("ATLAS_STORE_ROOT", "data/store")
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("file")
    imp = sub.add_parser("import")
    imp.add_argument("file")
    probe = sub.add_parser("probe")
    probe.add_argument("--enable-verified", action="store_true")
    probe.add_argument("--report", required=True)
    probe.add_argument("--content-dir")
    probe.add_argument(
        "--source-id", action="append", help="只验证指定目录信源，可重复传入"
    )
    configure = sub.add_parser(
        "configure-rss", help="为 RSS 目录配置具体公开订阅，默认禁用"
    )
    configure.add_argument("source_id")
    configure.add_argument("feed_url")
    configure.add_argument(
        "--browser",
        action="store_true",
        help="真实桌面 Chromium 读取；需安装 browser 可选依赖及 Xvfb",
    )
    sub.add_parser("status")
    process = sub.add_parser("process")
    process.add_argument(
        "--extract", action="store_true", help="显式调用已配置 LLM；默认只整理归档候选"
    )
    process.add_argument(
        "--fetch-articles", action="store_true", help="经既有 robots/限流读取候选详情"
    )
    process.add_argument("--limit", type=int, default=30)
    process.add_argument("--retry-errors", action="store_true")
    process.add_argument("--route", default="opencode-go")
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    root = Path(args.store_root)
    db_path = root / "atlas.db"
    if args.command == "validate":
        manifest = load_manifest(args.file)
        print(
            json.dumps(
                {
                    "count": len(manifest.sources),
                    "ingestion": dict(Counter(s.mode for s in manifest.sources)),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command != "import" and not db_path.is_file():
        parser.error("数据库不存在，请先执行 import")
    if args.command == "probe" and os.environ.get("ATLAS_LIVE") != "1":
        parser.error("真实连通性验证需显式设置 ATLAS_LIVE=1")
    if args.command == "process" and args.limit < 1:
        parser.error("limit 必须为正数")
    # 与既有 cron 共用锁；配置变更期间不能让旧快照覆盖新配置。
    lock_path = Path(".tmp_atlas/collection.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        if args.command in ("import", "probe", "configure-rss"):
            fcntl.flock(lock, fcntl.LOCK_EX)
        if args.command == "process":
            # 可由持有 collection.lock 的 cron 调用；提取单独互斥。
            process_lock = (root / ".aluminium-process.lock").open("a")
            try:
                fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                process_lock.close()
                print("上一轮电解铝处理仍在运行，本轮跳过。")
                return 0
        else:
            process_lock = None
        if args.command == "import":
            result = import_sources(args.file, db_path)
        elif args.command == "configure-rss":
            result = configure_rss_source(
                db_path, args.source_id, args.feed_url, browser=args.browser
            )
        elif args.command == "probe":
            from atlas.compose.tasks import ComposeDependencies

            dependencies = ComposeDependencies.real(root)
            report = probe_sources(
                db_path,
                dependencies,
                enable_verified=args.enable_verified,
                content_dir=args.content_dir,
                source_ids=args.source_id,
                on_result=lambda r: print(
                    json.dumps(r, ensure_ascii=False), flush=True
                ),
            )
            result = {
                "tested": len(report),
                "collectible": sum(r["collectible"] for r in report),
                "sources": report,
            }
            output = Path(args.report)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "process":
            if args.fetch_articles and os.environ.get("ATLAS_LIVE") != "1":
                parser.error("真实读取详情需 ATLAS_LIVE=1")
            from .processing import process_archive

            result = process_archive(
                root,
                extract=args.extract,
                fetch_articles=args.fetch_articles,
                limit=args.limit,
                retry_errors=args.retry_errors,
                route=args.route,
            )
        else:
            with AluminiumStore(db_path, read_only=True) as store:
                result = (
                    store.status() if args.command == "status" else store.intelligence()
                )
            if args.command == "export":
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                result = {"observations": len(result), "output": str(output)}
        if process_lock:
            process_lock.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
