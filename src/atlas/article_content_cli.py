"""留存清洗正文与原始响应，分类失败原因；不调用模型。"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import fcntl
import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit

from atlas.archive import (
    ArticleContentStore,
    enable_wal,
    open_archive,
    read_article_contents,
)
from atlas.cognition import BriefStore
from atlas.cognition.admission import read_admissions
from atlas.collect.article import (
    CLEANER_VERSION,
    ArticleReader,
    ArticleReadError,
    OriginThrottle,
    clean_document,
    clean_paragraphs,
    is_paper,
    read_source,
)
from atlas.collect.article_failure import classify_article_failure
from atlas.collect.network_policy import SharedNetworkPolicy
from atlas.webapp import ArchiveEntriesAccess


def seed_existing(store, db_path):
    existing = read_article_contents(db_path)
    with sqlite3.connect(db_path, timeout=30) as db:
        rows = db.execute(
            "SELECT source_url,article_text,body_source,status,error,resolved_url,updated_at FROM feed_articles"
        ).fetchall()
    chosen = {}
    rank = {"original": 3, "excerpt": 2, "abstract": 1, "title": 0}
    for row in rows:
        url, text, kind, status, error, _, _ = row
        if not url or url in existing:
            continue
        score = (bool(text), rank.get(kind, 0), len(text))
        if url not in chosen or score > chosen[url][0]:
            chosen[url] = (score, row)
    for _, row in chosen.values():
        url, text, kind, status, error, resolved, fetched = row
        if text:
            content_kind = (
                "article"
                if kind == "original"
                else "excerpt"
                if kind == "excerpt"
                else "abstract"
            )
            store.record(
                source_url=url,
                resolved_url=resolved or url,
                status="ready" if kind == "original" else "partial",
                content_kind=content_kind,
                cleaned_text=clean_paragraphs(text),
                cleaner_version="legacy-summary-input-v1",
                truncated=kind == "excerpt" and len(text) >= 12000,
                fetched_at=fetched,
            )
        elif status == "unavailable":
            failure = classify_article_failure(error, url)
            store.record(
                source_url=url,
                resolved_url=resolved or url,
                status="unavailable",
                cleaner_version="legacy-summary-input-v1",
                failure_code=failure.code,
                failure_category=failure.category,
                failure_label=failure.label,
                failure_detail=failure.detail,
                retryable=failure.retryable,
                http_status=failure.http_status,
                fetched_at=fetched,
            )


def _source_recency(record, entry):
    published = getattr(entry, "published_at", None)
    if published:
        try:
            value = datetime.fromisoformat(published)
            return value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp()
        except ValueError:
            pass
    return record.fetched_at.timestamp()


def _round_robin_urls(urls, paper_channels=None):
    """按输入优先级轮换站点；新闻和论文各自都能进入有上限的批次。"""
    paper_channels = paper_channels or {}
    groups = []
    for paper in (False, True):
        buckets = defaultdict(deque)
        for url in urls:
            if is_paper(url) == paper:
                host = urlsplit(url).hostname
                key = (host, paper_channels.get(url, "")) if paper else host
                buckets[key].append(url)
        ordered = []
        while buckets:
            for host in list(buckets):
                ordered.append(buckets[host].popleft())
                if not buckets[host]:
                    del buckets[host]
        groups.append(ordered)
    return [url for pair in zip_longest(*groups) for url in pair if url is not None]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="清洗留存正文（含论文全文）并归类失败；零模型调用"
    )
    parser.add_argument("--store-root", default="data/store")
    parser.add_argument("--report-dir", default=".tmp_atlas/article-content")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--scope", choices=["all", "news", "papers"], default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--retry-transient", action="store_true")
    parser.add_argument("--retry-unavailable", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--reclean", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8 or (args.limit is not None and args.limit < 1):
        parser.error("并发为 1–8，limit 必须为正")
    root = Path(args.store_root)
    network_policy = SharedNetworkPolicy(root)
    db_path = root / "atlas.db"
    if not db_path.is_file():
        parser.error("正式库不存在")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    lock = (
        root
        / (
            ".article-contents-reclean.lock"
            if args.reclean
            else ".article-contents.lock"
        )
    ).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("同范围正文任务正在运行")
    enable_wal(db_path)
    with open_archive(root) as archive:
        access = ArchiveEntriesAccess(archive)
        sources, recency, paper_channels = {}, {}, {}
        for rid in archive.all_raw_ids():
            record = archive.get(rid)
            entries = {(e.char_start, e.char_end): e for e in access.entries_of(record).entries}
            for position, source in access.brief_sources(rid).items():
                sources[source.key] = source
                timestamp = _source_recency(record, entries.get(position))
                recency[source.url] = max(recency.get(source.url, 0), timestamp)
                if is_paper(source.url) and record.channel_id.startswith("arxiv-"):
                    paper_channels.setdefault(source.url, record.channel_id)
    admission = read_admissions(db_path)
    sources = {k: s for k, s in sources.items() if admission.allows(k)}
    brief_store = BriefStore(db_path)
    try:
        brief_store.ensure_sources(list(sources.values()))
    finally:
        brief_store.close()
    store = ArticleContentStore(db_path)
    try:
        with store.batch():
            seed_existing(store, db_path)
        existing = read_article_contents(db_path)
        urls = list(dict.fromkeys(s.url for s in sources.values()))
        urls = [
            url
            for url in urls
            if args.scope == "all" or is_paper(url) == (args.scope == "papers")
        ]
        now = time.time()
        host_blocks_path = report_dir / "host-blocks.json"
        host_blocks = (
            json.loads(host_blocks_path.read_text())
            if host_blocks_path.exists()
            else {}
        )
        pending = []
        deferred = 0
        for url in urls:
            row = existing.get(url)
            if args.seed_only:
                continue
            if args.reclean:
                if (
                    row
                    and row.status == "ready"
                    and row.raw_sha256
                    and (
                        row.cleaner_version != CLEANER_VERSION
                        or (is_paper(url) and row.content_kind != "paper")
                    )
                ):
                    pending.append(url)
                continue
            if not args.refresh and row:
                if (
                    row.status == "ready"
                    and row.cleaner_version == CLEANER_VERSION
                    and (not is_paper(url) or row.content_kind == "paper")
                ):
                    continue
                if row.status == "unavailable":
                    age = now - datetime.fromisoformat(row.fetched_at).timestamp()
                    if not args.retry_unavailable and not (row.failure_code == "site_deferred" and now >= network_policy.ready_at(url)) and not (
                        args.retry_transient and row.retryable and age >= 3600
                    ):
                        continue
            host = urlsplit(url).hostname or ""
            if host_blocks.get(host, 0) > now:
                deferred += 1
            else:
                pending.append(url)
        pending.sort(key=lambda url: recency.get(url, 0), reverse=True)
        ordered = _round_robin_urls(pending, paper_channels)
        pending = ordered[: args.limit] if args.limit else ordered
        counts = Counter(
            total_urls=len(urls),
            selected=len(pending),
            completed=0,
            ready=0,
            unavailable=0,
            deferred=deferred,
        )
        started = time.monotonic()
        last_url = ""

        def progress(state):
            data = {
                "state": state,
                **dict(counts),
                "remaining": len(pending) - counts["completed"],
                "seconds": round(time.monotonic() - started, 1),
                "last_url": last_url,
                "model_calls": 0,
            }
            stage = report_dir / "progress.tmp"
            stage.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            stage.replace(report_dir / "progress.json")
            print(json.dumps(data, ensure_ascii=False), flush=True)

        throttle = OriginThrottle(shared=network_policy)
        local = threading.local()
        network_failures = Counter()

        def fetch(url):
            if not hasattr(local, "reader"):
                local.reader = ArticleReader(throttle=throttle)
            try:
                return read_source(local.reader, url), None
            except ArticleReadError as error:
                return None, error

        def save(url, result, error, *, fetched_at=None):
            nonlocal last_url
            last_url = url
            if result:
                store.record(
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
                    fetched_at=fetched_at,
                )
                counts["ready"] += 1
            else:
                f = error.failure
                store.record(
                    source_url=url,
                    resolved_url=error.url or url,
                    status="unavailable",
                    cleaner_version=CLEANER_VERSION,
                    failure_code=f.code,
                    failure_category=f.category,
                    failure_label=f.label,
                    failure_detail=f.detail,
                    retryable=f.retryable,
                    http_status=f.http_status,
                    raw_content=error.raw_content,
                    content_type=error.content_type,
                )
                counts["unavailable"] += 1
            counts["completed"] += 1
            with (report_dir / "results.jsonl").open("a") as log:
                log.write(
                    json.dumps(
                        {
                            "url": url,
                            "status": "ready" if result else "unavailable",
                            "failure_code": error.failure.code if error else "",
                            "body_chars": len(result.text) if result else 0,
                            "time": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

        progress("running")
        if args.reclean:
            for url in pending:
                row = existing[url]
                raw, content_type = store.raw(row.id)
                try:
                    cleaned = clean_document(raw, content_type)
                    if len(cleaned.text) < 80:
                        raise ArticleReadError(
                            "原文缺少足够的可读正文",
                            url=url,
                            raw_content=raw,
                            content_type=content_type,
                        )
                    from atlas.collect.article import ArticleContent

                    save(
                        url,
                        ArticleContent(
                            cleaned.text,
                            row.resolved_url,
                            raw,
                            content_type,
                            "paper" if is_paper(url) else cleaned.content_kind,
                            CLEANER_VERSION,
                            cleaned.truncated,
                        ),
                        None,
                        fetched_at=row.fetched_at,
                    )
                except ArticleReadError as error:
                    save(url, None, error)
                progress("running")
        else:
            iterator = iter(pending)

            def next_available():
                for following in iterator:
                    if (
                        host_blocks.get(urlsplit(following).hostname or "", 0)
                        > time.time()
                    ):
                        counts["deferred"] += 1
                        continue
                    return following
                return None

            with futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                running = {
                    pool.submit(fetch, url): url
                    for url in (next_available() for _ in range(args.workers))
                    if url is not None
                }
                while running:
                    done, _ = futures.wait(
                        running, timeout=10, return_when=futures.FIRST_COMPLETED
                    )
                    for future in done:
                        url = running.pop(future)
                        result, error = future.result()
                        save(url, result, error)
                        host = urlsplit(url).hostname or ""
                        if error and error.failure.code == "site_deferred":
                            host_blocks[host] = max(time.time() + 1, network_policy.ready_at(url))
                            host_blocks_path.write_text(json.dumps(host_blocks))
                        elif error and error.failure.retryable:
                            network_failures[host] += 1
                            if network_failures[host] >= 5:
                                host_blocks[host] = time.time() + 3600
                                host_blocks_path.write_text(json.dumps(host_blocks))
                        else:
                            network_failures[host] = 0
                    for _ in done:
                        following = next_available()
                        if following is not None:
                            running[pool.submit(fetch, following)] = following
                    progress("running")
        progress("retry_later" if counts["deferred"] else "complete")
        return 75 if counts["deferred"] else 0
    finally:
        store.close()
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
