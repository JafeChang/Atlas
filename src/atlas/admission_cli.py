"""采集后的入站审核；先审核，后进入简介、全文补抓和待标注队列。"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import fcntl
import json
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
from atlas.cognition import CognitionConfig, read_articles
from atlas.cognition.admission import (
    AdmissionClient,
    AdmissionStore,
    Review,
    input_hash,
    read_admissions,
    review_input,
)
from atlas.collect.article import (
    CLEANER_VERSION,
    ArticleReader,
    ArticleReadError,
    read_source,
)
from atlas.collect.network_policy import SharedNetworkPolicy
from atlas.webapp import ArchiveEntriesAccess


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI 入站审核并保留拦截理由与证据")
    parser.add_argument("--store-root", default="data/store")
    parser.add_argument("--report-dir", default="data/store/admission-job")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-fetches", type=int, default=4, help="每轮最多补读的原文数")
    parser.add_argument("--enable", action="store_true", help="开启正式队列的审核门禁")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="修复后立即重试模型审核错误，不重审已通过/拦截项",
    )
    parser.add_argument("--source-key", action="append")
    args = parser.parse_args(argv)
    root = Path(args.store_root)
    db_path = root / "atlas.db"
    if (
        not db_path.is_file()
        or not 1 <= args.workers <= 4
        or args.max_fetches < 1
        or (args.limit is not None and args.limit < 1)
    ):
        parser.error("正式库必须存在；并发 1–4，limit 和 max-fetches 为正")
    with (root / ".admission.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("上一轮入站审核仍在运行，本轮跳过。", flush=True)
            return 0
        return run(args, root, db_path, parser)


def run(args, root, db_path, parser):
    store = AdmissionStore(db_path)
    try:
        state = read_admissions(db_path)
        if args.enable:
            store.enable()
        report_dir = Path(args.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        retry_path = report_dir / "retry-after.json"
        if (
            not args.retry_errors
            and retry_path.exists()
            and time.time() < json.loads(retry_path.read_text())["until"]
        ):
            print("入站审核模型冷却中，待审条目保持隔离。", flush=True)
            return 1
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
        if args.source_key and set(args.source_key) - sources.keys():
            parser.error("指定的来源键不存在")
        briefs = read_articles(db_path)
        bodies = read_article_contents(db_path)
        network_policy = SharedNetworkPolicy(root)
        pending = []
        skipped = 0
        for source in sources.values():
            if args.source_key and source.key not in args.source_key:
                continue
            previous = state.reviews.get(source.key)
            if previous and previous.decision != "pending" and not args.refresh:
                skipped += 1
                continue
            article = briefs.get(source.key)
            if article and article.article_text and article.body_source == "original":
                source = replace(source, body=article.article_text, kind="original")
            elif source.url in bodies and (
                not source.has_body
                or (previous and previous.reason_code == "insufficient_content")
            ):
                body = read_article_body(db_path, source.url)
                if body:
                    source = replace(source, body=body.cleaned_text, kind="original")
            if (
                previous
                and not args.refresh
                and previous.input_hash == input_hash(source)
                and not (
                    previous.reason_code == "insufficient_content"
                    and source.kind != "original"
                )
            ):
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(previous.reviewed_at)
                ).total_seconds()
                retry = (
                    previous.reason_code == "review_error"
                    and (age >= 900 or args.retry_errors)
                ) or (
                    previous.reason_code == "source_unavailable"
                    and (age >= 3600 or (
                        source.url in bodies
                        and bodies[source.url].failure_code == "site_deferred"
                        and time.time() >= network_policy.ready_at(source.url)
                    ))
                    and (source.url not in bodies or bodies[source.url].retryable)
                )
                if not retry:
                    skipped += 1
                    continue
            pending.append(source)
        # 已到期的站点限速重试优先，避免被新订阅的大量历史文章长期挤出批次。
        pending.sort(
            key=lambda source: not (
                source.url in bodies
                and bodies[source.url].failure_code == "site_deferred"
                and time.time() >= network_policy.ready_at(source.url)
            )
        )
        if args.limit:
            pending = pending[: args.limit]
        counts = Counter(
            total_sources=len(sources),
            selected=len(pending),
            reused=skipped,
            accepted=0,
            rejected=0,
            pending=0,
            failed=0,
            requests=0,
            fetch_deferred=0,
        )
        started = time.monotonic()

        def progress(stage):
            data = {
                "state": stage,
                **dict(counts),
                "policy": state.policy,
                "seconds": round(time.monotonic() - started, 1),
            }
            path = report_dir / "progress.tmp"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            path.replace(report_dir / "progress.json")
            print(json.dumps(data, ensure_ascii=False), flush=True)

        if not pending:
            progress("complete")
            return 0
        progress("preparing")
        candidates = []
        reader = ArticleReader(shared=network_policy)
        body_store = None
        fetched = 0
        try:
            for source in pending:
                previous = state.reviews.get(source.key)
                if not source.has_body or (
                    previous
                    and previous.reason_code == "insufficient_content"
                    and source.kind != "original"
                ):
                    known = bodies.get(source.url)
                    retry_known = (
                        previous
                        and known
                        and known.retryable
                        and (known.failure_code == "site_deferred" and time.time() >= network_policy.ready_at(source.url) or (
                            datetime.now(UTC)
                            - datetime.fromisoformat(previous.reviewed_at)
                        ).total_seconds()
                        >= 3600)
                    )
                    if (
                        known
                        and known.status == "unavailable"
                        and not retry_known
                        and not args.refresh
                    ):
                        store.record(
                            source,
                            Review(
                                "pending",
                                "source_unavailable",
                                "原文获取失败：" + known.failure_label,
                            ),
                            policy=state.policy,
                        )
                        counts["pending"] += 1
                        continue
                    if fetched >= args.max_fetches:
                        counts["fetch_deferred"] += 1
                        continue
                    fetched += 1
                    if body_store is None:
                        body_store = ArticleContentStore(db_path)
                    try:
                        result = read_source(reader, source.url)
                    except ArticleReadError as error:
                        failure = error.failure
                        body_store.record(
                            source_url=source.url,
                            resolved_url=error.url or source.url,
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
                        store.record(
                            source,
                            Review(
                                "pending",
                                "source_unavailable",
                                "原文获取失败：" + failure.label,
                            ),
                            policy=state.policy,
                        )
                        counts["pending"] += 1
                        progress("preparing")
                        continue
                    body_store.record(
                        source_url=source.url,
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
                    source = replace(source, body=result.text, kind="original")
                if source.has_body:
                    candidates.append(source)
                else:
                    store.record(
                        source,
                        Review(
                            "pending", "insufficient_content", "没有足够正文可供审核"
                        ),
                        policy=state.policy,
                    )
                    counts["pending"] += 1
        finally:
            if body_store:
                body_store.close()
        groups, current, size = [], [], 0
        for source in candidates:
            chars = min(len(source.body), 6000) + len(source.title)
            if current and (len(current) >= 8 or size + chars > 28000):
                groups.append(current)
                current, size = [], 0
            current.append(source)
            size += chars
        if current:
            groups.append(current)
        config = CognitionConfig.from_env(route="opencode-go") if candidates else None

        def review(group):
            try:
                for attempt in range(3):
                    try:
                        return AdmissionClient(config).review(group, state.scope)
                    except requests.RequestException:
                        if attempt == 2:
                            raise
                        time.sleep(attempt + 1)
                    except RuntimeError as error:
                        if attempt == 2 or not any(
                            f"HTTP {code}" in str(error)
                            for code in (429, 500, 502, 503, 504)
                        ):
                            raise
                        time.sleep(attempt + 1)
            except (ValueError, KeyError, IndexError, TypeError) as error:
                if len(group) > 1:
                    middle = len(group) // 2
                    left, left_usage = review(group[:middle])
                    right, right_usage = review(group[middle:])
                    usage = {
                        name: left_usage.get(name, 0) + right_usage.get(name, 0)
                        for name in (
                            "prompt_tokens",
                            "completion_tokens",
                            "_invalid_items",
                        )
                    }
                    usage["_requests"] = (
                        1
                        + left_usage.get("_requests", 1)
                        + right_usage.get("_requests", 1)
                    )
                    return {**left, **right}, usage
                with (report_dir / "errors.jsonl").open("a") as log:
                    log.write(
                        json.dumps(
                            {
                                "keys": [group[0].key],
                                "error": str(error),
                                "kind": "invalid_response",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                # 不修补错配响应；单条结构错误留在待审区，下轮冷却后重试。
                return {
                    group[0].key: Review(
                        "pending", "review_error", "模型审核响应不合格，稍后重试"
                    )
                }, {"_invalid_items": 1}

        aborted = False
        progress("reviewing")
        with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            iterator = iter(groups)
            running = {
                pool.submit(review, group): group
                for group in (next(iterator, None) for _ in range(args.workers))
                if group
            }
            while running:
                done, _ = futures.wait(
                    running, timeout=10, return_when=futures.FIRST_COMPLETED
                )
                for future in done:
                    group = running.pop(future)
                    try:
                        results, usage = future.result()
                        if set(results) != {s.key for s in group}:
                            raise ValueError("审核响应与输入不对应")
                        # 先全部验证，再写；任何不合格响应都不能局部放行。
                        for source in group:
                            result = results[source.key]
                            if (
                                result.evidence
                                and result.evidence not in review_input(source)["body"]
                            ):
                                raise ValueError("审核证据不匹配")
                            if result.decision == "rejected" and not result.evidence:
                                raise ValueError("缺少拦截证据")
                        for source in group:
                            result = results[source.key]
                            store.record(
                                source, result, policy=state.policy, model=config.model
                            )
                            counts[result.decision] += 1
                        counts["requests"] += usage.get("_requests", 1)
                        counts["failed"] += usage.get("_invalid_items", 0)
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
                    except (
                        requests.RequestException,
                        RuntimeError,
                        ValueError,
                        KeyError,
                        IndexError,
                        TypeError,
                    ) as error:
                        counts["failed"] += len(group)
                        for source in group:
                            store.record(
                                source,
                                Review(
                                    "pending", "review_error", "模型审核失败，稍后重试"
                                ),
                                policy=state.policy,
                            )
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
                            running[pool.submit(review, group)] = group
                progress("stopping" if aborted else "reviewing")
        if aborted:
            retry_path.write_text(json.dumps({"until": time.time() + 900}))
        else:
            retry_path.unlink(missing_ok=True)
        progress("failed" if aborted else "complete")
        return int(aborted)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
