"""Measure offline cleaning of frozen real responses, without touching live stores.

The experiment directory contains inventory.json, sample.json and input/<id>.gz.
Use Atlas's existing Python environment with PYTHONPATH=src. Restrict the whole
process tree to half of the physical cores (including their SMT siblings).
This candidate is an experimental main-body extractor, not a production change.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import importlib
import json
import multiprocessing
import os
import resource
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

VERSION = "atlas.index-clean-sample/1"


def json_read(path):
    return json.loads(Path(path).read_text())


def json_write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def half_cpu_set():
    cores = defaultdict(list)
    for cpu in sorted(os.sched_getaffinity(0)):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        key = (
            (topology / "physical_package_id").read_text().strip(),
            (topology / "core_id").read_text().strip(),
        )
        cores[key].append(cpu)
    selected = sorted(cores)[: max(1, len(cores) // 2)]
    return [cpu for key in selected for cpu in cores[key]]


def candidate_clean(raw, mime):
    from bs4 import BeautifulSoup

    from atlas.collect.article import CleanedDocument, clean_document, clean_paragraphs

    if raw.startswith(b"%PDF") or "application/pdf" in mime:
        return clean_document(raw, mime)
    soup = BeautifulSoup(raw, "lxml")
    for node in soup.select(
        "script,style,noscript,nav,header,footer,aside,form,svg,iframe,"
        '[hidden],[aria-hidden="true"],.related-posts,.related-articles,'
        ".related-content,.recommended-posts,.newsletter,.subscription-prompt,"
        ".cookie-banner,.cookie-consent,#cookie-banner,#onetrust-banner-sdk,"
        ".share-buttons,.social-share,.author-bio,.advertisement,.ad-container,"
        ".ltx_page_header,.ltx_page_footer,.ltx_TOC"
    ):
        node.decompose()

    def score(node):
        text = node.get_text(" ", strip=True)
        links = sum(len(a.get_text(" ", strip=True)) for a in node.select("a"))
        return max(0, len(text) - links)

    # A specific body takes precedence over a large surrounding main container.
    root = None
    for selector in (
        '[itemprop="articleBody"],#main-body-container,.article-body,.article-content,'
        ".entry-content,.post-content,.post-body,.story-body,.markdown-body,.readme,.ltx_document",
        'article,main,[role="main"],#main-content,#content',
    ):
        candidates = soup.select(selector)
        if candidates:
            root = max(candidates, key=score)
            if score(root) > 0:
                break
            root = None
    if root is None:
        paragraphs = soup.select("p,pre,.comment,.blob-code")
        text = "\n".join(p.get_text(" ", strip=True) for p in paragraphs if score(p))
        if len(text) < 80 and ("text/plain" in mime or b"<" not in raw):
            text = soup.get_text(" ", strip=True)
    else:
        for pre in list(root.select("pre")):
            pre.replace_with("\n\n```\n" + pre.get_text().strip("\n") + "\n```\n\n")
        for br in root.select("br"):
            br.replace_with("\n")
        for block in list(
            root.select("p,div,section,li,h1,h2,h3,h4,h5,h6,pre,blockquote,tr")
        ):
            block.insert_before("\n\n")
            block.insert_after("\n\n")
        text = root.get_text(" ", strip=False)
    text = clean_paragraphs(text)
    return CleanedDocument(text[:2_000_000], "article", len(text) > 2_000_000)


def clean_one(task):
    root, row = task
    root = Path(root)
    started = time.perf_counter()
    cpu = time.process_time()
    compressed = (root / "input" / f"{row['id']}.gz").read_bytes()
    raw = gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
        raise ValueError(f"Raw response hash mismatch for {row['id']}")
    decode_cpu = time.process_time() - cpu
    cpu = time.process_time()
    cleaned = candidate_clean(raw, row["mime"])
    clean_cpu = time.process_time() - cpu
    text = cleaned.text.encode()
    cpu = time.process_time()
    compact = gzip.compress(text, mtime=0)
    text_compress_cpu = time.process_time() - cpu
    # Existing ArticleContentStore.record recompresses raw bytes into each snapshot.
    cpu = time.process_time()
    recompressed = gzip.compress(raw, mtime=0)
    raw_recompress_cpu = time.process_time() - cpu
    (root / "output" / f"{row['id']}.txt").write_bytes(text)
    result = {
        **row,
        "pid": os.getpid(),
        "raw_bytes": len(raw),
        "new_chars": len(cleaned.text),
        "new_utf8_bytes": len(text),
        "new_text_gzip_bytes": len(compact),
        "recompressed_raw_bytes": len(recompressed),
        "decode_cpu_s": decode_cpu,
        "clean_cpu_s": clean_cpu,
        "text_compress_cpu_s": text_compress_cpu,
        "raw_recompress_cpu_s": raw_recompress_cpu,
        "elapsed_wall_s": time.perf_counter() - started,
        "worker_hwm_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "truncated": cleaned.truncated,
        "too_short": len(cleaned.text) < 80,
    }
    if "norsk-hydro-alunorte-ramps-up-alumina-production.html" in row["url"]:
        result["body_control"] = {
            "production_number_preserved": "100,000 to 120,000 tonnes" in cleaned.text,
            "recommended_reading_removed": "Recommended Reading" not in cleaned.text,
            "related_news_removed": "Bunzl Canada" not in cleaned.text,
        }
    return result


def warm_worker():
    # Library import cost is a one-off startup cost, not a per-document cost.
    for name in ("bs4", "atlas.collect.article", "pypdf"):
        importlib.import_module(name)


def process_tree(pid):
    pids, pending = [], [pid]
    while pending:
        p = pending.pop()
        pids.append(p)
        try:
            pending.extend(
                int(n) for n in Path(f"/proc/{p}/task/{p}/children").read_text().split()
            )
        except FileNotFoundError:
            pass  # A worker can exit between discovery and inspection.
    return pids


def memory_snapshot(pid):
    total = Counter()
    for p in process_tree(pid):
        try:
            status = Path(f"/proc/{p}/status").read_text()
            for line in status.splitlines():
                if line.startswith(("VmRSS:", "VmSwap:")):
                    key, value = line.split(":", 1)
                    total[key] += int(value.strip().split()[0])
            for line in Path(f"/proc/{p}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    total["Pss"] += int(line.split()[1])
            total["processes"] += 1
        except (FileNotFoundError, ProcessLookupError):
            pass
    return dict(total)


def write_sample_database(root, rows, snapshots):
    path = root / ("snapshot-storage.sqlite" if snapshots else "derived-storage.sqlite")
    if path.exists():
        raise FileExistsError(f"Use a fresh experiment directory: {path}")
    started = time.perf_counter()
    peak_wal = 0
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE texts(id INTEGER PRIMARY KEY,source_id INTEGER,url TEXT,cleaner_version TEXT,text TEXT,sha TEXT,raw_gzip BLOB)"
        )
        for number, r in enumerate(rows, 1):
            text = (root / "output" / f"{r['id']}.txt").read_text()
            blob = (
                (root / "input" / f"{r['id']}.gz").read_bytes() if snapshots else None
            )
            db.execute(
                "INSERT INTO texts VALUES(?,?,?,?,?,?,?)",
                (
                    number,
                    r["id"],
                    r["url"],
                    VERSION,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    blob,
                ),
            )
            if number % 16 == 0:
                db.commit()
                wal = Path(str(path) + "-wal")
                peak_wal = max(peak_wal, wal.stat().st_size if wal.exists() else 0)
        db.commit()
        wal = Path(str(path) + "-wal")
        peak_wal = max(peak_wal, wal.stat().st_size if wal.exists() else 0)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "file_bytes": path.stat().st_size,
        "peak_wal_bytes": peak_wal,
        "write_wall_s": time.perf_counter() - started,
    }


def byte_estimates(group, population):
    # Total original character counts are known exactly. Ratio estimation avoids
    # mistaking a sample's unusually long/short documents for a storage change.
    denominator = sum(row["old_chars"] for row in group)
    return {
        key: sum(row[key] for row in group) / denominator * population["old_chars"]
        for key in ("new_utf8_bytes", "new_text_gzip_bytes", "old_utf8_bytes")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    cpus = half_cpu_set()
    if not 1 <= args.workers <= len(cpus):
        parser.error("Workers must fit the half-machine CPU set")
    os.sched_setaffinity(0, cpus)
    sample = json_read(args.out / "sample.json")
    (args.out / "output").mkdir(exist_ok=True)
    telemetry = {"baseline": memory_snapshot(os.getpid()), "peak": {}, "samples": 0}
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            snap = memory_snapshot(os.getpid())
            for key, value in snap.items():
                telemetry["peak"][key] = max(telemetry["peak"].get(key, 0), value)
            telemetry["samples"] += 1
            stop.wait(0.2)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    results = []
    started = time.perf_counter()
    print(
        json.dumps(
            {
                "cpu_affinity": cpus,
                "workers": args.workers,
                "samples": len(sample["sample"]),
            }
        ),
        flush=True,
    )
    try:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=args.workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=warm_worker,
        ) as pool:
            jobs = {
                pool.submit(clean_one, (str(args.out), row)): row
                for row in sample["sample"]
            }
            pending = set(jobs)
            last_notice = time.monotonic()
            while pending:
                done, pending = concurrent.futures.wait(
                    pending, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for job in done:
                    result = job.result()  # Unexpected errors must fail visibly.
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "done": len(results),
                                "id": result["id"],
                                "stratum": result["stratum"],
                                "seconds": round(result["elapsed_wall_s"], 3),
                                "rss_worker_mib": round(
                                    result["worker_hwm_kib"] / 1024, 1
                                ),
                            }
                        ),
                        flush=True,
                    )
                if time.monotonic() - last_notice > 25:
                    print(
                        json.dumps(
                            {
                                "remaining": len(pending),
                                "wall_s": round(time.perf_counter() - started, 1),
                                "memory": memory_snapshot(os.getpid()),
                            }
                        ),
                        flush=True,
                    )
                    last_notice = time.monotonic()
    finally:
        stop.set()
        watcher.join()
    sample_wall = time.perf_counter() - started
    json_write(args.out / "measurements.json", results)
    storage_derived = write_sample_database(args.out, results, False)
    storage_snapshot = write_sample_database(args.out, results, True)
    fields = (
        "decode_cpu_s",
        "clean_cpu_s",
        "raw_recompress_cpu_s",
        "text_compress_cpu_s",
        "new_utf8_bytes",
        "new_text_gzip_bytes",
        "old_utf8_bytes",
    )
    projection = Counter()
    breakdown = {}
    for stratum, population in sample["strata"].items():
        group = [
            r
            for r in results
            if r["stratum"] == stratum and r["selection"] == "stratified"
        ]
        if not group:
            raise ValueError(f"No random sample for stratum {stratum}")
        estimates = {
            key: statistics.mean(r[key] for r in group) * population["count"]
            for key in fields
        }
        estimates.update(byte_estimates(group, population))
        projection.update(estimates)
        breakdown[stratum] = {
            "population": population["count"],
            "sample": len(group),
            "estimates": estimates,
        }
    # SQLite page overhead measured on the same output, not the source file size.
    overhead_derived = storage_derived["file_bytes"] / sum(
        r["new_utf8_bytes"] for r in results
    )
    overhead_snapshot = storage_snapshot["file_bytes"] / sum(
        r["new_utf8_bytes"] + r["gzip_bytes"] for r in results
    )
    clean_cpu = projection["decode_cpu_s"] + projection["clean_cpu_s"]
    derived_disk = projection["new_utf8_bytes"] * overhead_derived
    snapshot_disk = (
        projection["new_utf8_bytes"] + sample["inventory"]["raw_gzip_bytes"]
    ) * overhead_snapshot
    summary = {
        "version": VERSION,
        "cpu_affinity": cpus,
        "logical_cpus_total": os.cpu_count(),
        "workers": args.workers,
        "sample_docs": len(results),
        "sample_wall_s": sample_wall,
        "sample_cpu_s": sum(
            r["decode_cpu_s"]
            + r["clean_cpu_s"]
            + r["text_compress_cpu_s"]
            + r["raw_recompress_cpu_s"]
            for r in results
        ),
        "inventory": sample["inventory"],
        "telemetry": telemetry,
        "per_worker_hwm_kib": {
            str(pid): max(r["worker_hwm_kib"] for r in results if r["pid"] == pid)
            for pid in {r["pid"] for r in results}
        },
        "storage_sample": {"derived": storage_derived, "snapshot": storage_snapshot},
        "projection": {
            **projection,
            "cleaning_at_half_cpu_seconds": clean_cpu / len(cpus),
            "including_raw_recompression_at_half_cpu_seconds": (
                clean_cpu + projection["raw_recompress_cpu_s"]
            )
            / len(cpus),
            "derived_sqlite_bytes": derived_disk,
            "append_snapshot_sqlite_bytes": snapshot_disk,
        },
        "strata": breakdown,
        "storage_estimator": "Within-stratum UTF8/gzip bytes per known original character, multiplied by exact population character counts; SQLite page overhead measured on sample databases.",
        "too_short_count": sum(r["too_short"] for r in results),
        "body_controls": [r["body_control"] for r in results if "body_control" in r],
        "largest_tasks": sorted(
            results, key=lambda r: r["worker_hwm_kib"], reverse=True
        )[:5],
        "limitations": [
            "Stratified means are estimates, not a full-library execution.",
            "Stress/control samples excluded from population estimates.",
            "CPU projection assumes four logical workers remain busy on the selected two physical cores; disk, idle time, contention and write transactions add wall time.",
            "Candidate body selection is not a complete relevance/retention audit.",
            "No model inference, remote fetches or production writes.",
            "Memory PSS/RSS peaks are sampled every 0.2s; per-worker HWM also recorded.",
            "Inventory/export I/O and sample SQLite writes are separate from worker cleaning time.",
        ],
    }
    json_write(args.out / "summary.json", summary)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("strata", "largest_tasks")},
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
