"""显式生成、续跑正式库的中文译题与简介：python -m atlas.briefs_cli。"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import fcntl
import json
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import requests

from atlas.archive import (
    ArticleContentStore,
    open_archive,
    read_article_body,
    read_article_contents,
)
from atlas.cognition import BriefClient, BriefStore, CognitionConfig, read_articles
from atlas.cognition.admission import read_admissions
from atlas.collect.article import (
    CLEANER_VERSION,
    ArticleReader,
    ArticleReadError,
    read_source,
)
from atlas.webapp import ArchiveEntriesAccess


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="读取原文，在正式 atlas.db 写入中文译题及 300 字以内简介"
    )
    parser.add_argument("--store-root", default="data/store")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--fetch-workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--route", default="opencode-go")
    parser.add_argument("--model")
    parser.add_argument("--report-dir")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-unavailable", action="store_true")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument(
        "--failure-cooldown",
        type=int,
        default=0,
        help="自动运行失败后的最短重试间隔（秒）",
    )
    parser.add_argument(
        "--source-key", action="append", help="只处理指定来源键，可重复传入"
    )
    parser.add_argument(
        "--refresh-originals",
        action="store_true",
        help="重新提取原文；内容相同的已完成简介不重复调用",
    )
    args = parser.parse_args(argv)
    if (
        args.failure_cooldown < 0
        or not 1 <= args.workers <= 4
        or not 1 <= args.batch_size <= 8
        or not 1 <= args.fetch_workers <= 8
        or (args.limit is not None and args.limit < 1)
    ):
        parser.error("模型并发 1–4，抓取并发 1–8，批次 1–8，limit 必须为正")
    root = Path(args.store_root)
    db_path = root / "atlas.db"
    if not db_path.is_file():
        parser.error("正式库不存在，请先采集归档")
    lock = (root / ".briefs.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("上一轮译题和简介仍在生成，本轮跳过。", flush=True)
        return 0
    try:
        return _run(args, root, db_path, parser)
    finally:
        lock.close()


def _run(args, root, db_path, parser):
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive)
        records = sorted(
            (archive.get(rid) for rid in archive.all_raw_ids()),
            key=lambda r: (r.fetched_at, r.raw_id),
            reverse=True,
        )
        sources = {}
        for record in records:
            for source in access.brief_sources(record.raw_id).values():
                sources.setdefault(source.key, source)
    admission = read_admissions(db_path)
    sources = {k: s for k, s in sources.items() if admission.allows(k)}
    existing = read_articles(db_path)
    retained_bodies = read_article_contents(db_path)
    if args.source_key and set(args.source_key) - sources.keys():
        parser.error("指定的来源键不存在")
    pending = [
        s
        for s in sources.values()
        if s.key not in existing
        or (args.refresh_originals and existing[s.key].body_source == "original")
        or (
            existing[s.key].status != "ready"
            and (
                existing[s.key].status != "unavailable"
                or args.retry_unavailable
                or (
                    s.url in retained_bodies
                    and retained_bodies[s.url].status == "ready"
                )
            )
        )
    ]
    if args.source_key:
        pending = [s for s in pending if s.key in args.source_key]
    if args.limit:
        pending = pending[: args.limit]
    report_dir = (
        Path(args.report_dir)
        if args.report_dir
        else Path(".tmp_atlas/feed-articles")
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    retry_path = report_dir / "retry-after.json"
    if (
        args.failure_cooldown
        and retry_path.exists()
        and time.time() < json.loads(retry_path.read_text())["until"]
    ):
        print("上一轮生成失败，冷却期内不重复调用模型。", flush=True)
        return 0
    counts = Counter(
        total_sources=len(sources),
        already_ready=sum(r.status == "ready" for r in existing.values()),
        selected=len(pending),
        generated=0,
        failed=0,
        unavailable=0,
    )
    started = time.monotonic()
    config = None

    def progress(state):
        report = {
            "state": state,
            **dict(counts),
            "remaining": len(pending)
            - counts["generated"]
            - counts["failed"]
            - counts["unavailable"],
            "reused_ready": counts["reused_ready"],
            "model": config.model if config else None,
            "seconds": round(time.monotonic() - started, 1),
        }
        report["remaining"] -= counts["reused_ready"]
        stage = report_dir / "progress.tmp"
        stage.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        stage.replace(report_dir / "progress.json")
        print(json.dumps(report, ensure_ascii=False), flush=True)

    if not pending:
        retry_path.unlink(missing_ok=True)
        progress("complete")
        return 0
    store = BriefStore(db_path)
    body_store = None
    try:
        store.ensure_sources(pending)
        needs_fetch = {}
        prepared = {}
        for source in pending:
            if source.url in retained_bodies and not args.refresh_originals:
                saved = read_article_body(db_path, source.url)
                if saved:
                    store.record_body(
                        source, body=saved.cleaned_text, final_url=saved.resolved_url
                    )
                    prepared[source.key] = replace(
                        source, body=saved.cleaned_text, kind="original"
                    )
                    continue
            row = existing.get(source.key)
            if (
                row
                and row.article_text
                and row.body_source == "original"
                and not args.refresh_originals
            ):
                candidate = replace(source, body=row.article_text, kind="original")
                if candidate.has_body:
                    prepared[source.key] = candidate
                    continue
            saved_state = retained_bodies.get(source.url)
            if (
                not source.has_body
                and saved_state
                and saved_state.status == "unavailable"
                and not args.retry_unavailable
                and not args.refresh_originals
            ):
                store.record_body(source, body="", error=saved_state.failure_detail)
                counts["unavailable"] += 1
                continue
            if source.has_body:
                prepared[source.key] = source
            else:
                needs_fetch.setdefault(source.url, []).append(source)
        local = threading.local()

        def read(url):
            if not hasattr(local, "reader"):
                local.reader = ArticleReader()
            try:
                return read_source(local.reader, url), ""
            except ArticleReadError as error:
                return None, error

        progress("reading_originals")
        with futures.ThreadPoolExecutor(max_workers=args.fetch_workers) as pool:
            iterator = iter(needs_fetch)
            running = {
                pool.submit(read, url): url
                for url in (next(iterator, None) for _ in range(args.fetch_workers))
                if url is not None
            }
            while running:
                done, _ = futures.wait(
                    running, timeout=10, return_when=futures.FIRST_COMPLETED
                )
                for future in done:
                    url = running.pop(future)
                    result, error = future.result()
                    if body_store is None:
                        body_store = ArticleContentStore(db_path)
                    if result:
                        body_store.record(
                            source_url=url,
                            resolved_url=result.url,
                            status="ready",
                            content_kind=result.content_kind,
                            cleaned_text=result.text,
                            cleaner_version=result.cleaner_version,
                            truncated=result.truncated,
                            raw_content=result.raw_content,
                            content_type=result.content_type,
                            http_status=200,
                        )
                    else:
                        failure = error.failure
                        body_store.record(
                            source_url=url,
                            resolved_url=error.url or url,
                            status="unavailable",
                            cleaner_version=CLEANER_VERSION,
                            failure_code=failure.code,
                            failure_category=failure.category,
                            failure_label=failure.label,
                            failure_detail=str(error),
                            retryable=failure.retryable,
                            http_status=failure.http_status,
                            raw_content=error.raw_content,
                            content_type=error.content_type,
                        )
                    for source in needs_fetch[url]:
                        old = existing.get(source.key)
                        if (
                            result
                            and old
                            and old.status == "ready"
                            and old.article_text == result.text
                        ):
                            counts["reused_ready"] += 1
                            continue
                        store.record_body(
                            source,
                            body=result.text if result else "",
                            error=str(error) if error else "",
                            final_url=result.url if result else "",
                        )
                        if result:
                            prepared[source.key] = replace(
                                source, body=result.text, kind="original"
                            )
                            counts["originals_read"] += 1
                        else:
                            counts["unavailable"] += 1
                            with (report_dir / "unavailable.jsonl").open("a") as log:
                                log.write(
                                    json.dumps(
                                        {
                                            "key": source.key,
                                            "url": url,
                                            "error": str(error),
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                )
                    following = next(iterator, None)
                    if following is not None:
                        running[pool.submit(read, following)] = following
                progress("reading_originals")
        if args.fetch_only:
            progress("originals_saved")
            return 0
        candidates = [prepared[s.key] for s in pending if s.key in prepared]
        config = (
            CognitionConfig.from_env(
                route=args.route, **({"model": args.model} if args.model else {})
            )
            if candidates
            else None
        )
        groups, current, chars = [], [], 0
        for source in candidates:
            size = min(12000, len(source.body)) + len(source.title)
            if current and (len(current) >= args.batch_size or chars + size > 24000):
                groups.append(current)
                current, chars = [], 0
            current.append(source)
            chars += size
        if current:
            groups.append(current)
        (report_dir / "plan.json").write_text(
            json.dumps(
                {"keys": [s.key for s in candidates], "batches": len(groups)},
                ensure_ascii=False,
            )
        )

        def generate(group):
            for attempt in range(3):
                try:
                    return BriefClient(config).generate(group)
                except RuntimeError as error:
                    if attempt < 2 and any(
                        f"HTTP {code}" in str(error)
                        for code in (429, 500, 502, 503, 504)
                    ):
                        time.sleep(attempt + 1)
                        continue
                    raise
                except requests.RequestException:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
                except (ValueError, KeyError, IndexError) as error:
                    # 结构不合格时缩小输入范围；不修补 JSON，也不保存错配结果。
                    # 最深只到单篇，单篇仍受三次尝试上限约束。
                    if len(group) > 1:
                        middle = len(group) // 2
                        left, left_usage = generate(group[:middle])
                        right, right_usage = generate(group[middle:])
                        usage = {
                            name: left_usage.get(name, 0) + right_usage.get(name, 0)
                            for name in ("prompt_tokens", "completion_tokens")
                        }
                        usage["_requests_completed"] = left_usage.get(
                            "_requests_completed", 1
                        ) + right_usage.get("_requests_completed", 1)
                        usage["_split_groups"] = (
                            1
                            + left_usage.get("_split_groups", 0)
                            + right_usage.get("_split_groups", 0)
                        )
                        return {**left, **right}, usage
                    if attempt == 2:
                        raise ValueError(f"译题/简介校验失败：{error}") from error
                    time.sleep(attempt + 1)

        aborted = False
        progress("generating")
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            iterator = iter(groups)
            running = {
                pool.submit(generate, g): g
                for g in (next(iterator, None) for _ in range(args.workers))
                if g
            }
            while running:
                done, _ = futures.wait(
                    running, timeout=10, return_when=futures.FIRST_COMPLETED
                )
                for future in done:
                    group = running.pop(future)
                    try:
                        texts, usage = future.result()
                        store.record(group, texts, model=config.model)
                        counts["generated"] += len(group)
                        counts["requests_completed"] += usage.get(
                            "_requests_completed", 1
                        )
                        counts["split_groups"] += usage.get("_split_groups", 0)
                        counts["input_tokens"] += usage.get("prompt_tokens", 0)
                        counts["output_tokens"] += usage.get("completion_tokens", 0)
                        with (report_dir / "calls.jsonl").open("a") as log:
                            log.write(
                                json.dumps(
                                    {"keys": [s.key for s in group], "usage": usage},
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                    except Exception as error:
                        counts["failed"] += len(group)
                        with (report_dir / "errors.jsonl").open("a") as log:
                            log.write(
                                json.dumps(
                                    {
                                        "keys": [s.key for s in group],
                                        "error": str(error),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        aborted = True
                if not aborted:
                    for _ in done:
                        group = next(iterator, None)
                        if group:
                            running[pool.submit(generate, group)] = group
                progress("stopping" if aborted else "generating")
        progress("failed" if aborted else "complete")
        if aborted and args.failure_cooldown:
            retry_path.write_text(
                json.dumps({"until": time.time() + args.failure_cooldown})
            )
        elif not aborted:
            retry_path.unlink(missing_ok=True)
        return 1 if aborted else 0
    finally:
        if body_store is not None:
            body_store.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
