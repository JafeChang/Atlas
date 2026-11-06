"""Local, rebuildable Chinese-brief indexes; never writes the source database.

Build/query: python -m atlas.brief_search_cli build|query|status
The optional ``semantic`` dependency group supplies CPU ONNX inference. Reading
status and keyword queries does not import the model runtime. The web integration
reuses these formats through ``atlas.brief_search``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import resource
import sqlite3
import struct
import tempfile
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from atlas.cognition.admission import read_admissions
from atlas.cognition.briefs import BRIEF_VERSION
from atlas.search import SearchQuery, segment_cjk

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_SHA256 = "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
DIMENSIONS = 384
INDEX_VERSION = "atlas.brief-search/1"
DEFAULT_MODEL = Path("data/models/multilingual-e5-small") / REVISION
DEFAULT_INDEX = Path("data/store/brief-search.sqlite")
PROTOCOL_KEYS = (
    "model",
    "model_revision",
    "model_sha256",
    "tokenizer_sha256",
    "dimensions",
    "dtype",
    "normalization",
    "pooling",
    "document_prefix",
    "query_prefix",
)


def model_metadata(model_dir):
    """Read the pinned encoding protocol without starting an inference session."""
    manifest = json.loads((Path(model_dir) / "manifest.json").read_text())
    if (
        manifest.get("model") != MODEL
        or manifest.get("revision") != REVISION
        or manifest["files"]["onnx/model.onnx"]["sha256"] != MODEL_SHA256
    ):
        raise ValueError("本地模型版本不符")
    return {
        "model": MODEL,
        "model_revision": REVISION,
        "model_sha256": MODEL_SHA256,
        "tokenizer_sha256": manifest["files"]["tokenizer.json"]["sha256"],
        "dimensions": DIMENSIONS,
        "dtype": "little-endian-float32",
        "normalization": "L2",
        "pooling": "attention-mask-mean",
        "document_prefix": "passage: ",
        "query_prefix": "query: ",
    }


def utc_now():
    return datetime.now(UTC).isoformat()


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def restrict_half_cpu():
    """Idempotently use at most half the host's physical cores, including SMT."""
    cores = {}
    for entry in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"):
        cpu = int(entry.name[3:])
        if (entry / "online").exists() and (
            entry / "online"
        ).read_text().strip() != "1":
            continue
        topology = entry / "topology"
        key = (
            (topology / "physical_package_id").read_text().strip(),
            (topology / "core_id").read_text().strip(),
        )
        cores.setdefault(key, []).append(cpu)
    allowed = os.sched_getaffinity(0)
    available = [key for key in sorted(cores) if allowed.intersection(cores[key])]
    chosen = available[: max(1, len(cores) // 2)]
    cpus = sorted(cpu for key in chosen for cpu in cores[key] if cpu in allowed)
    if len(cores) == 1:
        cpus = cpus[: max(1, len(cpus) // 2)]
    if not cpus:
        raise RuntimeError("无法确定半 CPU 限制")
    os.sched_setaffinity(0, cpus)
    return cpus


@dataclass(frozen=True)
class Brief:
    source_key: str
    source_url: str
    title_zh: str
    summary_zh: str
    updated_at: str

    @property
    def model_text(self):
        return "passage: " + self.title_zh + "\n" + self.summary_zh

    @property
    def text_sha256(self):
        return hashlib.sha256(self.model_text.encode()).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    documents: tuple[Brief, ...]
    admission_policy: str
    captured_at: str

    @property
    def sha256(self):
        return fingerprint(
            {
                "policy": self.admission_policy,
                "documents": [asdict(d) for d in self.documents],
            }
        )


def load_snapshot(source):
    """Only reads existing title/summary fields; never opens a writing store."""
    admission = read_admissions(source)
    db = readonly(source)
    try:
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT source_key,source_url,title_zh,summary_zh,updated_at FROM feed_articles "
            "WHERE policy=? AND status='ready' ORDER BY source_key",
            (BRIEF_VERSION,),
        ).fetchall()
        documents = tuple(
            Brief(*row)
            for row in rows
            if admission.allows(row["source_key"])
            and row["title_zh"].strip()
            and row["summary_zh"].strip()
        )
        return Snapshot(documents, admission.policy, utc_now())
    finally:
        db.close()


class LocalEncoder:
    def __init__(self, model_dir=DEFAULT_MODEL):
        self.cpus = restrict_half_cpu()
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        model_dir = Path(model_dir)
        manifest = json.loads((model_dir / "manifest.json").read_text())
        if manifest.get("model") != MODEL or manifest.get("revision") != REVISION:
            raise ValueError("本地模型版本不符")
        for name in ("tokenizer.json", "onnx/model.onnx"):
            if file_sha256(model_dir / name) != manifest["files"][name]["sha256"]:
                raise ValueError(f"本地模型文件校验失败：{name}")
        if manifest["files"]["onnx/model.onnx"]["sha256"] != MODEL_SHA256:
            raise ValueError("ONNX 不是指定版本的官方模型")
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = len(self.cpus)
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(model_dir / "onnx/model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.inputs = {value.name for value in self.session.get_inputs()}
        self.metadata = {
            "model": MODEL,
            "model_revision": REVISION,
            "model_sha256": MODEL_SHA256,
            "tokenizer_sha256": manifest["files"]["tokenizer.json"]["sha256"],
            "dimensions": DIMENSIONS,
            "dtype": "little-endian-float32",
            "normalization": "L2",
            "pooling": "attention-mask-mean",
            "document_prefix": "passage: ",
            "query_prefix": "query: ",
            "runtime": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "onnxruntime", "tokenizers")
            },
        }

    def encode(self, texts):
        np = self.np
        encoded = self.tokenizer.encode_batch(texts)
        ids = np.asarray([e.ids for e in encoded], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encoded], dtype=np.int64)
        if ids.shape[1] > 512:
            raise ValueError("输入超过模型 512 token 上限；拒绝截断")
        values = {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": np.zeros_like(ids),
        }
        output = self.session.run(
            None, {k: v for k, v in values.items() if k in self.inputs}
        )[0]
        if output.ndim != 3 or output.shape[-1] != DIMENSIONS:
            raise ValueError("模型输出维度不符")
        weights = mask.astype(np.float32)
        vectors = (output * weights[:, :, None]).sum(axis=1) / weights.sum(axis=1)[
            :, None
        ]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not np.isfinite(vectors).all() or (norms <= 0).any():
            raise ValueError("模型返回无效向量")
        vectors = (vectors / norms).astype("<f4")
        return [vector.tobytes() for vector in vectors]


def validate_vector(blob):
    if len(blob) != DIMENSIONS * 4:
        raise ValueError("向量长度不符")
    values = struct.unpack(f"<{DIMENSIONS}f", blob)
    if (
        not all(math.isfinite(v) for v in values)
        or abs(sum(v * v for v in values) - 1) > 1e-4
    ):
        raise ValueError("向量不是有限的单位向量")


def index_metadata(path):
    db = readonly(path)
    try:
        meta = {
            row[0]: json.loads(row[1])
            for row in db.execute("SELECT key,value FROM brief_search_meta")
        }
        if meta.get("index_version") != INDEX_VERSION:
            raise ValueError("简介索引版本不符，请重建")
        count = db.execute("SELECT count(*) FROM brief_search_documents").fetchone()[0]
        if count != meta["document_count"]:
            raise ValueError("简介索引计数不符")
        return {**meta, "index_bytes": Path(path).stat().st_size}
    finally:
        db.close()


def write_index(path, snapshot, cache, metadata):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE brief_search_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE brief_search_documents(
                id INTEGER PRIMARY KEY,source_key TEXT UNIQUE NOT NULL,source_url TEXT NOT NULL,
                title_zh TEXT NOT NULL,summary_zh TEXT NOT NULL,updated_at TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,title_index TEXT NOT NULL,summary_index TEXT NOT NULL,
                embedding BLOB NOT NULL CHECK(length(embedding)=1536));
            CREATE VIRTUAL TABLE brief_search_fts USING fts5(
                title_index,summary_index,content='brief_search_documents',content_rowid='id',
                tokenize='unicode61 remove_diacritics 2');
        """)
        for rowid, doc in enumerate(snapshot.documents, 1):
            digest, vector = cache[doc.source_key]
            if digest != doc.text_sha256:
                raise ValueError("向量与译题/简介内容不一致")
            validate_vector(vector)
            db.execute(
                "INSERT INTO brief_search_documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rowid,
                    doc.source_key,
                    doc.source_url,
                    doc.title_zh,
                    doc.summary_zh,
                    doc.updated_at,
                    digest,
                    segment_cjk(doc.title_zh),
                    segment_cjk(doc.summary_zh),
                    vector,
                ),
            )
        db.execute("INSERT INTO brief_search_fts(brief_search_fts) VALUES('rebuild')")
        db.execute(
            "INSERT INTO brief_search_fts(brief_search_fts) VALUES('integrity-check')"
        )
        db.executemany(
            "INSERT INTO brief_search_meta VALUES(?,?)",
            [
                (key, json.dumps(value, ensure_ascii=False))
                for key, value in metadata.items()
            ],
        )
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("索引 SQLite 完整性验证失败")


def build_index(
    source,
    destination=DEFAULT_INDEX,
    *,
    model_dir=DEFAULT_MODEL,
    batch_size=8,
    encoder=None,
    progress=None,
    incremental=False,
):
    if not 1 <= batch_size <= 64:
        raise ValueError("batch_size 必须为 1–64")
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("索引不能覆盖来源数据库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started, cpu_started = time.perf_counter(), time.process_time()
    with destination.with_suffix(".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        protocol = (
            encoder.metadata if encoder is not None else model_metadata(model_dir)
        )
        cache, encoded_count, encoded_keys = {}, 0, set()
        snapshot = load_snapshot(source)
        previous = None
        if incremental and destination.exists():
            previous = index_metadata(destination)
            if any(previous.get(key) != protocol.get(key) for key in PROTOCOL_KEYS):
                raise ValueError("增量更新的模型协议与索引不符，请显式全量 build")
            with closing(readonly(destination)) as db:
                for row in db.execute(
                    "SELECT source_key,text_sha256,embedding FROM brief_search_documents"
                ):
                    validate_vector(row["embedding"])
                    cache[row["source_key"]] = (row["text_sha256"], row["embedding"])
            if previous.get("source_snapshot_sha256") == snapshot.sha256:
                return {
                    **previous,
                    "encoded_count": 0,
                    "reused_count": len(snapshot.documents),
                    "incremental": True,
                    "updated": False,
                    "elapsed_seconds": time.perf_counter() - started,
                    "path": str(destination),
                }
        for revision in range(3):
            if not snapshot.documents and previous is None:
                raise ValueError("没有已通过审核的中文译题和简介")
            pending = [
                d
                for d in snapshot.documents
                if d.source_key not in cache or cache[d.source_key][0] != d.text_sha256
            ]
            if pending and encoder is None:
                encoder = LocalEncoder(model_dir)
                if any(
                    encoder.metadata.get(key) != protocol.get(key)
                    for key in PROTOCOL_KEYS
                ):
                    raise ValueError("编码模型协议发生变化；未发布索引")
            if progress:
                progress(
                    {
                        "state": "encoding",
                        "pass": revision + 1,
                        "documents": len(snapshot.documents),
                        "pending": len(pending),
                        "cpus": encoder.cpus if encoder is not None else [],
                    }
                )
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                vectors = encoder.encode([doc.model_text for doc in batch])
                for doc, vector in zip(batch, vectors, strict=True):
                    validate_vector(vector)
                    cache[doc.source_key] = (doc.text_sha256, vector)
                    encoded_keys.add(doc.source_key)
                encoded_count += len(batch)
                if progress and (
                    start % 128 == 0 or start + len(batch) == len(pending)
                ):
                    progress(
                        {
                            "state": "encoding",
                            "completed": start + len(batch),
                            "total": len(pending),
                            "elapsed_seconds": round(time.perf_counter() - started, 1),
                        }
                    )
            current = load_snapshot(source)
            if current.sha256 == snapshot.sha256:
                break
            snapshot = current
        else:
            raise RuntimeError("建库期间来源持续变化；未发布，请重试")
        metadata = {
            **(encoder.metadata if encoder is not None else protocol),
            "index_version": INDEX_VERSION,
            "source_db": str(source),
            "source_policy": BRIEF_VERSION,
            "admission_policy": snapshot.admission_policy,
            "source_snapshot_sha256": snapshot.sha256,
            "snapshot_at": snapshot.captured_at,
            "built_at": utc_now(),
            "document_count": len(snapshot.documents),
            "fields": ["title_zh", "summary_zh"],
            "cpu_affinity": encoder.cpus if encoder is not None else [],
            "batch_size": batch_size,
            "encoded_count": encoded_count,
            "reused_count": sum(
                d.source_key not in encoded_keys for d in snapshot.documents
            ),
            "incremental": incremental,
            "cpu_seconds": time.process_time() - cpu_started,
        }
        fd, stage = tempfile.mkstemp(
            prefix=".brief-search-", suffix=".sqlite", dir=destination.parent
        )
        os.close(fd)
        try:
            write_index(stage, snapshot, cache, metadata)
            index_metadata(stage)
            with open(stage, "rb") as handle:
                os.fsync(handle.fileno())
            os.replace(stage, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(stage).unlink(missing_ok=True)
        return {
            **index_metadata(destination),
            "elapsed_seconds": time.perf_counter() - started,
            "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "index_sha256": file_sha256(destination),
            "path": str(destination),
            "updated": True,
        }


def query_index(
    path,
    text,
    *,
    mode="semantic",
    limit=10,
    encoder=None,
    model_dir=DEFAULT_MODEL,
    allowed_keys=None,
):
    if mode not in ("semantic", "keyword") or not 1 <= limit <= 200:
        raise ValueError("无效查询模式或 limit（1–200）")
    query = SearchQuery(text=text)
    meta = index_metadata(path)
    db = readonly(path)
    try:
        if mode == "keyword":
            rows = db.execute(
                "SELECT d.*,-bm25(brief_search_fts,8.0,1.0) score FROM brief_search_fts JOIN brief_search_documents d ON d.id=brief_search_fts.rowid WHERE brief_search_fts MATCH ? ORDER BY bm25(brief_search_fts,8.0,1.0),d.source_key",
                (query.match_expression(),),
            ).fetchall()
            ranked = [(dict(r), r["score"]) for r in rows]
        else:
            encoder = encoder or LocalEncoder(model_dir)
            for key in (
                "model",
                "model_revision",
                "model_sha256",
                "tokenizer_sha256",
                "dimensions",
                "pooling",
                "normalization",
            ):
                if encoder.metadata.get(key) != meta.get(key):
                    raise ValueError(f"查询模型与索引不符：{key}")
            import numpy as np

            rows = db.execute(
                "SELECT * FROM brief_search_documents ORDER BY source_key"
            ).fetchall()
            matrix = np.vstack(
                [np.frombuffer(row["embedding"], dtype="<f4") for row in rows]
            )
            vector = np.frombuffer(encoder.encode(["query: " + text])[0], dtype="<f4")
            scores = matrix @ vector
            ranked = [
                (dict(rows[int(i)]), float(scores[i]))
                for i in np.argsort(-scores, kind="stable")
            ]
        hits = []
        for row, score in ranked:
            if allowed_keys is not None and row["source_key"] not in allowed_keys:
                continue
            hits.append(
                {
                    key: row[key]
                    for key in ("source_key", "source_url", "title_zh", "summary_zh")
                }
                | {"score": score}
            )
            if len(hits) == limit:
                break
        return hits
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "update", "query", "status"))
    parser.add_argument("text", nargs="?")
    parser.add_argument("--source", type=Path, default=Path("data/store/atlas.db"))
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--mode", choices=("semantic", "keyword"), default="semantic")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    restrict_half_cpu()
    if args.command == "query" and not args.text:
        parser.error("query 需要查询文本")
    if args.command != "query" and args.text:
        parser.error("仅 query 接受查询文本")
    try:
        if args.command in ("build", "update"):
            result = build_index(
                args.source,
                args.index,
                model_dir=args.model_dir,
                batch_size=args.batch_size,
                incremental=args.command == "update",
                progress=lambda value: print(
                    json.dumps(value, ensure_ascii=False), flush=True
                ),
            )
        elif args.command == "query":
            allowed = {d.source_key for d in load_snapshot(args.source).documents}
            result = query_index(
                args.index,
                args.text,
                mode=args.mode,
                limit=args.limit,
                model_dir=args.model_dir,
                allowed_keys=allowed,
            )
        else:
            result = index_metadata(args.index)
    except (ValueError, OSError, RuntimeError, sqlite3.DatabaseError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
