"""Reproducible, read-only-corpus CPU sampling for multilingual-e5-small.

Run prepare/lexical with Atlas's Python (PYTHONPATH=src), tokenize/bench with
an isolated uv environment containing numpy, onnxruntime and tokenizers.
All generated files, including the experimental FTS database, go under --out.
No production index, configuration or dependency file is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import resource
import sqlite3
import statistics
import time
from collections import Counter
from pathlib import Path

MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
TOPICS = (
    (
        "gas",
        "norsk-hydro-update-on-alunorte-gas-supply-situation.html",
        "巴西氧化铝厂天然气供应变化",
        "natural gas supply changes at a Brazilian alumina refinery",
    ),
    (
        "ramp",
        "norsk-hydro-alunorte-ramps-up-alumina-production.html",
        "氧化铝工厂提高产量",
        "an alumina refinery increasing its production",
    ),
    (
        "wind",
        "hydro-to-receive-eur-28-million-compensation-following-wind-farm-sale.html",
        "出售风电场后收到补偿款",
        "compensation payment following the sale of a wind farm",
    ),
    (
        "output",
        "international-aluminium.org/statistics/primary-aluminium-production/",
        "全球原铝产量统计",
        "worldwide primary aluminium production statistics",
    ),
    (
        "router",
        "github.com/ADCDS/xiaomi-ax3000t-cabmeshd-disclosure",
        "小米路由器硬编码密钥导致未登录接管管理员",
        "unauthenticated router admin takeover using a hardcoded key",
    ),
    (
        "law",
        "lexifina.com/blog/git-for-the-us-code",
        "像管理代码一样追踪美国法律的修改",
        "tracking changes to United States legislation with version control",
    ),
    (
        "dna",
        "hugging-face-carbon-x-14b-dna-language-model/",
        "能同时理解DNA序列与人类语言的开放模型",
        "an open model connecting DNA sequences and natural language",
    ),
    (
        "notes",
        "github.com/SabitaKumariDev/LectureInk-V1",
        "把视频教程整理成开源学习笔记",
        "open source study notes generated from video lessons",
    ),
    (
        "kv",
        "arxiv.org/abs/2607.12550",
        "压缩大模型KV缓存并尽量不损失精度",
        "compressing language model key value caches with minimal accuracy loss",
    ),
    (
        "memory",
        "arxiv.org/abs/2610.26433",
        "提前预测智能体记忆检索失败",
        "forecasting retrieval failures in an AI agent's memory",
    ),
    (
        "entropy",
        "arxiv.org/abs/2509.26114",
        "强化学习里裁剪上下限怎样改变输出随机性",
        "how upper and lower clipping limits affect entropy during reinforcement learning",
    ),
    (
        "gpu",
        "arxiv.org/abs/2610.35529",
        "单张消费级显卡如何运行长时程世界模型",
        "running long horizon world models on a single consumer graphics card",
    ),
    (
        "campus",
        "princeton-to-provide-chatgpt-accounts-all-students-faculty-staff",
        "普林斯顿向全校师生提供聊天机器人账号",
        "university wide AI chatbot accounts at Princeton",
    ),
    (
        "recall",
        "arxiv.org/abs/2610.34507",
        "记忆规模变大对主动回忆有什么代价",
        "the cost of increasing memory size for proactive recall",
    ),
)


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def rss():
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:", "VmSwap:", "Threads:")):
            key, value = line.split(":", 1)
            fields[key] = int(value.strip().split()[0])
    return fields


def prepare(args):
    from atlas.cognition.admission import read_admissions

    path = Path(args.db).resolve()
    admission = read_admissions(path)
    # A real read-only connection; do not open any domain's writing store.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        latest = {}
        for url, rowid, status, chars in db.execute(
            "SELECT source_url,id,status,body_chars FROM article_contents "
            "INDEXED BY idx_article_contents_readview ORDER BY source_url,id"
        ):
            if status == "ready":
                latest[url] = (rowid, chars)
        eligible = {}
        for key, title, title_zh, url, summary, status in db.execute(
            "SELECT source_key,original_title,title_zh,source_url,summary_zh,status "
            "FROM feed_articles ORDER BY source_key"
        ):
            if not admission.allows(key) or url not in latest or url in eligible:
                continue
            eligible[url] = {
                "source_key": key,
                "title": title,
                "title_zh": title_zh,
                "url": url,
                "summary_zh": summary,
                "brief_status": status,
                "body_id": latest[url][0],
                "body_chars": latest[url][1],
            }
        queries, selected = [], {}
        for topic, suffix, zh, en in TOPICS:
            matches = [d for u, d in eligible.items() if suffix in u]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected one eligible article for {topic}: {len(matches)}"
                )
            doc = matches[0]
            selected[doc["url"]] = {**doc, "selection": "fixed-reference"}
            for language, text in (("zh", zh), ("en", en)):
                queries.append(
                    {
                        "id": f"{topic}-{language}",
                        "topic": topic,
                        "language": language,
                        "text": text,
                        "reference": doc["url"],
                    }
                )
        # Include nearby industrial topics as distractors, then fill by stable hash.
        # This is an enriched diagnostic sample, not a random population accuracy test.
        ordered = sorted(
            eligible.values(), key=lambda d: digest("atlas-e5-sample-v1:" + d["url"])
        )
        for doc in ordered:
            if "/no/" not in doc["url"] and any(
                term in (doc["title"] + " " + doc["url"]).lower()
                for term in ("aluminium", "aluminum", "alunorte", "norsk hydro")
            ):
                selected.setdefault(
                    doc["url"], {**doc, "selection": "industrial-distractor"}
                )
        for doc in ordered:
            if len(selected) >= args.sample_size:
                break
            if "/no/" not in doc["url"] and 1000 <= doc["body_chars"] <= 200_000:
                selected.setdefault(
                    doc["url"], {**doc, "selection": "stable-hash-fill"}
                )
        docs, seen = [], {}
        for doc in selected.values():
            (body,) = db.execute(
                "SELECT cleaned_text FROM article_contents WHERE id=?",
                (doc["body_id"],),
            ).fetchone()
            fingerprint = digest(body)
            if fingerprint in seen:
                raise ValueError(
                    f"Duplicate body: {doc['url']} and {seen[fingerprint]}"
                )
            seen[fingerprint] = doc["url"]
            docs.append({**doc, "body": body, "body_sha256": fingerprint})
    result = {
        "version": "atlas.e5-sample/1",
        "model_revision": MODEL_REVISION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "db": str(path),
        "admission_policy": admission.policy,
        "eligible_docs": len(eligible),
        "eligible_body_chars": sum(d["body_chars"] for d in eligible.values()),
        "eligible_native_zh_titles": sum(
            sum("\u3400" <= c <= "\u9fff" for c in d["title"]) > 2
            for d in eligible.values()
        ),
        "sample_size": len(docs),
        "sample_body_chars": sum(len(d["body"]) for d in docs),
        "selection_counts": dict(Counter(d["selection"] for d in docs)),
        "limitation": "Hand-authored paraphrases with one known reference each, fixed before inference. Not exhaustive relevance judgments or population recall.",
        "documents": docs,
        "queries": queries,
    }
    write_json(args.out / "corpus.json", result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("documents", "queries")}
        ),
        flush=True,
    )


def tokenize(args):
    from tokenizers import Tokenizer

    corpus = read_json(args.out / "corpus.json")
    tokenizer = Tokenizer.from_file(str(args.model / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    passages, original_tokens = [], 0
    started = time.perf_counter()
    for doc_id, doc in enumerate(corpus["documents"]):
        body_tokens = tokenizer.encode(doc["body"], add_special_tokens=False)
        original_tokens += len(body_tokens.ids)
        title = doc["title"]
        title_tokens = tokenizer.encode(title, add_special_tokens=False)
        if len(title_tokens.ids) > 64:
            title = title[: title_tokens.offsets[63][1]]
        offsets = body_tokens.offsets
        # Keep the final full window rather than creating a tiny reference/URL
        # tail which can become a misleading nearest neighbour after max pooling.
        final_start = max(0, len(offsets) - 384)
        starts = list(range(0, final_start + 1, 320))
        if starts[-1] != final_start:
            starts.append(final_start)
        if len(starts) > args.chunks_per_doc:
            starts = [
                starts[round(i * (len(starts) - 1) / (args.chunks_per_doc - 1))]
                for i in range(args.chunks_per_doc)
            ]
        for start in starts:
            end = min(start + 384, len(offsets))
            char_start, char_end = offsets[start][0], offsets[end - 1][1]
            text = f"passage: {title}\n{doc['body'][char_start:char_end]}"
            length = len(tokenizer.encode(text).ids)
            if length > 512:
                raise ValueError(f"Passage unexpectedly exceeds model limit: {length}")
            passages.append(
                {
                    "scope": "original",
                    "doc_id": doc_id,
                    "url": doc["url"],
                    "text": text,
                    "char_start": char_start,
                    "char_end": char_end,
                    "tokens": length,
                    "body_tokens": end - start,
                }
            )
        if doc["brief_status"] == "ready" and doc["title_zh"] and doc["summary_zh"]:
            text = f"passage: {doc['title_zh']}\n{doc['summary_zh']}"
            length = len(tokenizer.encode(text).ids)
            if length > 512:
                raise ValueError(
                    "Chinese summary exceeds limit; explicit chunking required"
                )
            passages.append(
                {
                    "scope": "chinese_generated_summary",
                    "doc_id": doc_id,
                    "url": doc["url"],
                    "text": text,
                    "tokens": length,
                }
            )
    result = {
        "corpus_sha256": digest(json.dumps(corpus, sort_keys=True, ensure_ascii=False)),
        "model_revision": MODEL_REVISION,
        "passages": passages,
        "document_metadata": [
            {k: v for k, v in d.items() if k != "body"} for d in corpus["documents"]
        ],
        "queries": corpus["queries"],
        "full_sample_body_tokens": original_tokens,
        "full_sample_body_chars": corpus["sample_body_chars"],
        "eligible_body_chars": corpus["eligible_body_chars"],
        "eligible_docs": corpus["eligible_docs"],
        "tokenize_wall_seconds": time.perf_counter() - started,
        "chunking": {
            "algorithm": "even-full-windows/2",
            "body_window_tokens": 384,
            "stride_tokens": 320,
            "max_title_tokens": 64,
            "sampled_windows_per_doc": args.chunks_per_doc,
            "selection": "evenly spaced including first and last; not whole-body coverage",
        },
    }
    write_json(args.out / "passages.json", result)
    print(
        json.dumps(
            {
                "passages": len(passages),
                "scopes": dict(Counter(p["scope"] for p in passages)),
                "full_sample_body_tokens": original_tokens,
                "tokenize_seconds": result["tokenize_wall_seconds"],
            }
        ),
        flush=True,
    )


def lexical(args):
    from atlas.search import SearchQuery, segment_cjk

    data = read_json(args.out / "passages.json")
    # In-memory FTS is deliberately independent of the production search tables.
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE VIRTUAL TABLE p USING fts5(text,scope UNINDEXED,doc_id UNINDEXED,tokenize='unicode61 remove_diacritics 2')"
    )
    db.executemany(
        "INSERT INTO p VALUES(?,?,?)",
        [(segment_cjk(p["text"]), p["scope"], p["doc_id"]) for p in data["passages"]],
    )
    results = {}
    for scope in ("original", "chinese_generated_summary"):
        for q in data["queries"]:
            expression = SearchQuery(text=q["text"]).match_expression()
            # Also measure permissive OR recall; AND is Atlas's existing semantics.
            for mode, expr in (
                ("and", expression),
                ("or", expression.replace(" AND ", " OR ")),
            ):
                rows = db.execute(
                    "SELECT doc_id,bm25(p) FROM p WHERE p MATCH ? AND scope=? ORDER BY bm25(p),doc_id",
                    (expr, scope),
                ).fetchall()
                ordered = list(dict.fromkeys(int(row[0]) for row in rows))
                results[f"{scope}:{q['id']}:{mode}"] = ordered
    write_json(args.out / "lexical.json", results)
    db.close()
    print(
        "BM25 AND/OR baseline complete, using Atlas query/CJK functions and identical passages.",
        flush=True,
    )


def bench(args):
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    def distribution(values):
        return {
            "p50_ms": float(np.percentile(values, 50)) * 1000,
            "p95_ms": float(np.percentile(values, 95)) * 1000,
            "min_ms": min(values) * 1000,
            "max_ms": max(values) * 1000,
        }

    data = read_json(args.out / "passages.json")
    lexical_results = read_json(args.out / "lexical.json")
    tokenizer = Tokenizer.from_file(str(args.model / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
    before_load = rss()
    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    started = time.perf_counter()
    session = ort.InferenceSession(
        str(args.model / "onnx/model.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    load_seconds = time.perf_counter() - started
    after_load = rss()
    input_names = {i.name for i in session.get_inputs()}

    def encode(texts):
        encoded = tokenizer.encode_batch(texts)
        if any(e.overflowing for e in encoded):
            raise ValueError(
                "Unexpected truncation: benchmark input must fit within 512 tokens"
            )
        ids = np.asarray([e.ids for e in encoded], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encoded], dtype=np.int64)
        values = {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": np.zeros_like(ids),
        }
        output = session.run(
            None, {k: v for k, v in values.items() if k in input_names}
        )[0]
        if output.ndim != 3 or output.shape[-1] != 384:
            raise ValueError(f"Unexpected official ONNX output shape: {output.shape}")
        weights = mask.astype(np.float32)
        masked = output * weights[:, :, None]
        vectors = masked.sum(axis=1) / weights.sum(axis=1)[:, None]
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        if not np.isfinite(vectors).all():
            raise ValueError("Non-finite embeddings")
        return vectors

    cold_started = time.perf_counter()
    encode(["query: 如何检索中英文文章"])
    first_query_seconds = time.perf_counter() - cold_started
    encode([p["text"] for p in data["passages"][: args.batch_size]])
    probe = data["passages"][0]["text"]
    shorter = "passage: 检索测试"
    single = encode([shorter])[0]
    paired = encode([probe, shorter])[1]
    batch_padding_max_error = float(np.max(np.abs(single - paired)))
    if not np.allclose(single, paired, atol=5e-5, rtol=5e-5):
        raise ValueError(f"Padding changes embedding: {batch_padding_max_error}")
    vectors, batches = [], []
    cpu_started = time.process_time()
    encoding_started = time.perf_counter()
    for start in range(0, len(data["passages"]), args.batch_size):
        batch = data["passages"][start : start + args.batch_size]
        at = time.perf_counter()
        vectors.append(encode([p["text"] for p in batch]))
        batches.append(
            {
                "count": len(batch),
                "tokens": sum(p["tokens"] for p in batch),
                "padded_tokens": max(p["tokens"] for p in batch) * len(batch),
                "seconds": time.perf_counter() - at,
            }
        )
        if start % (args.batch_size * 8) == 0:
            print(
                json.dumps(
                    {
                        "threads": args.threads,
                        "encoded": start + len(batch),
                        "total": len(data["passages"]),
                        "elapsed_s": round(time.perf_counter() - encoding_started, 2),
                        "rss": rss(),
                    }
                ),
                flush=True,
            )
    encoding_seconds = time.perf_counter() - encoding_started
    encoding_cpu_seconds = time.process_time() - cpu_started
    matrix = np.concatenate(vectors)
    if not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5):
        raise ValueError("Document embeddings are not unit normalized")
    after_encoding = rss()
    np.save(args.out / f"vectors-t{args.threads}.npy", matrix)
    scope_indices = {
        scope: np.array(
            [i for i, p in enumerate(data["passages"]) if p["scope"] == scope]
        )
        for scope in ("original", "chinese_generated_summary")
    }
    results, query_times, retrieval_times = [], [], []
    query_vectors = []
    for q in data["queries"]:
        timings = []
        for _ in range(3):
            at = time.perf_counter()
            qv = encode(["query: " + q["text"]])[0]
            timings.append(time.perf_counter() - at)
        query_times.extend(timings)
        query_vectors.append(qv)
        for scope, indices in scope_indices.items():
            at = time.perf_counter()
            scores = matrix[indices] @ qv
            by_doc = {}
            for i, score in zip(indices, scores, strict=True):
                doc_id = data["passages"][int(i)]["doc_id"]
                by_doc[doc_id] = max(by_doc.get(doc_id, -math.inf), float(score))
            ranked = sorted(by_doc, key=lambda i: (-by_doc[i], i))
            retrieval_times.append(time.perf_counter() - at)
            ranks = {
                kind: lexical_results[f"{scope}:{q['id']}:{kind}"]
                for kind in ("and", "or")
            }
            fused = {}
            for source in (ranked, ranks["or"]):
                for rank, i in enumerate(source, 1):
                    fused[i] = fused.get(i, 0) + 1 / (60 + rank)
            ranks.update(
                dense=ranked, hybrid=sorted(fused, key=lambda i: (-fused[i], i))
            )
            reference = next(
                i
                for i, d in enumerate(data["document_metadata"])
                if d["url"] == q["reference"]
            )
            positions = {
                k: (v.index(reference) + 1 if reference in v else None)
                for k, v in ranks.items()
            }
            results.append(
                {
                    "query": q,
                    "scope": scope,
                    "reference_ranks": positions,
                    "query_ms": statistics.median(timings) * 1000,
                    "top5": [
                        {
                            "title": data["document_metadata"][i]["title"],
                            "url": data["document_metadata"][i]["url"],
                            "score": by_doc[i],
                        }
                        for i in ranked[:5]
                    ],
                }
            )
    quality = {}
    for scope in scope_indices:
        for language in ("zh", "en"):
            group = [
                r
                for r in results
                if r["scope"] == scope and r["query"]["language"] == language
            ]
            for kind in ("and", "or", "dense", "hybrid"):
                ranks = [r["reference_ranks"][kind] for r in group]
                quality[f"{scope}:{language}:{kind}"] = {
                    "queries": len(ranks),
                    "success_at_1": sum(r == 1 for r in ranks),
                    "success_at_5": sum(r is not None and r <= 5 for r in ranks),
                    "success_at_10": sum(r is not None and r <= 10 for r in ranks),
                    "mrr": sum(1 / r if r else 0 for r in ranks) / len(ranks),
                }
    scale_times = {}
    for count in (10_000, 100_000):
        at = time.perf_counter()
        enlarged = np.tile(matrix, (math.ceil(count / len(matrix)), 1))[:count].copy()
        seconds = []
        for qv in query_vectors[:10]:
            at = time.perf_counter()
            scores = enlarged @ qv
            best = np.argpartition(scores, -10)[-10:]
            assert len(best) == 10
            seconds.append(time.perf_counter() - at)
        scale_times[str(count)] = dict(
            **distribution(seconds),
            matrix_bytes=enlarged.nbytes,
            rss=rss(),
            note="Replicated sample vectors for compute/memory scaling only; not retrieval quality.",
        )
        del enlarged
    total_tokens = sum(p["tokens"] for p in data["passages"])
    rate = total_tokens / encoding_seconds
    projected_body_tokens = (
        data["eligible_body_chars"]
        * data["full_sample_body_tokens"]
        / data["full_sample_body_chars"]
    )
    original = [p for p in data["passages"] if p["scope"] == "original"]
    prefix_tokens = statistics.mean(p["tokens"] - p["body_tokens"] for p in original)
    projected_encoded_tokens = projected_body_tokens / 320 * (384 + prefix_tokens)
    result = {
        "model": "intfloat/multilingual-e5-small",
        "revision": MODEL_REVISION,
        "provider": session.get_providers(),
        "dtype": "float32",
        "threads": args.threads,
        "batch_size": args.batch_size,
        "python": platform.python_version(),
        "packages": {
            n: importlib.metadata.version(n)
            for n in ("numpy", "onnxruntime", "tokenizers")
        },
        "cpu": next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "load_seconds": load_seconds,
        "first_query_ms": first_query_seconds * 1000,
        "batch_padding_max_error": batch_padding_max_error,
        "memory_before_load": before_load,
        "memory_after_load": after_load,
        "memory_after_encoding": after_encoding,
        "memory_final": rss(),
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "documents": len(data["document_metadata"]),
        "passages": len(matrix),
        "tokens": total_tokens,
        "encoding_wall_seconds": encoding_seconds,
        "encoding_cpu_seconds": encoding_cpu_seconds,
        "tokens_per_second": rate,
        "passages_per_second": len(matrix) / encoding_seconds,
        "query_latency": distribution(query_times),
        "sample_retrieval_latency": distribution(retrieval_times),
        "scale_retrieval_latency": scale_times,
        "vector_bytes": matrix.nbytes,
        "quality": quality,
        "results": results,
        "batches": batches,
        "projection": {
            "eligible_docs": data["eligible_docs"],
            "eligible_body_chars": data["eligible_body_chars"],
            "estimated_body_tokens": projected_body_tokens,
            "estimated_encoded_tokens": projected_encoded_tokens,
            "estimated_full_index_hours": projected_encoded_tokens / rate / 3600,
            "limitation": "Extrapolation from enriched sample and steady-state throughput, excluding cold DB I/O, queueing, failures, and index persistence; not a production timing guarantee.",
        },
        "corpus_sha256": data["corpus_sha256"],
        "load_average": os.getloadavg(),
    }
    write_json(args.out / f"result-t{args.threads}.json", result)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("results", "batches")},
            ensure_ascii=False,
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "tokenize", "lexical", "bench"))
    parser.add_argument("--db", default="data/store/atlas.db")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--sample-size", type=int, default=128)
    parser.add_argument("--chunks-per-doc", type=int, default=3)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    if (
        args.sample_size < len(TOPICS)
        or args.chunks_per_doc < 2
        or args.threads < 1
        or args.batch_size < 1
    ):
        parser.error("invalid sample size, chunk count, threads or batch size")
    if args.phase in ("tokenize", "bench") and args.model is None:
        parser.error("--model is required")
    args.out.mkdir(parents=True, exist_ok=True)
    globals()[args.phase](args)


if __name__ == "__main__":
    main()
