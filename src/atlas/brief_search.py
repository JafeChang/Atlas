"""Read-only search over saved briefs, with one bounded, half-CPU worker."""

import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from atlas.brief_search_cli import (
    DEFAULT_MODEL,
    INDEX_VERSION,
    LocalEncoder,
    readonly,
    restrict_half_cpu,
    validate_vector,
)
from atlas.search import match_expression, phrase_groups


class BriefSearch:
    """Keep local inference and vectors off HTTP threads; never write an index.

    Cached scores contain no user state. Admission/personal/channel filters are
    applied before the semantic candidate window, by passing eligible keys.
    """

    def __init__(self, path, *, model_dir=DEFAULT_MODEL, encoder_factory=None):
        self.path = Path(path)
        self.model_dir = model_dir
        self.encoder_factory = encoder_factory
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brief-search")
        self._slots = threading.BoundedSemaphore(3)
        self._signature = None
        self._db = None
        self._encoder = None
        self._matrix = None
        self._cache = OrderedDict()

    def search(self, text, mode, allowed_keys):
        if mode not in ("hybrid", "keyword", "semantic"):
            raise ValueError("未知搜索模式")
        if not self._slots.acquire(blocking=False):
            raise RuntimeError("搜索繁忙，请稍后重试")
        future = self._worker.submit(self._search, text, mode, frozenset(allowed_keys))
        # A timed-out HTTP request must not release the queue slot early.
        future.add_done_callback(lambda _: self._slots.release())
        return future.result(timeout=30)

    def _load(self):
        restrict_half_cpu()
        stat = self.path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if signature == self._signature:
            return
        db = readonly(self.path)
        try:
            meta = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM brief_search_meta")}
            if meta.get("index_version") != INDEX_VERSION:
                raise ValueError("搜索索引版本不符")
            rows = db.execute("SELECT source_key,embedding FROM brief_search_documents ORDER BY source_key").fetchall()
            if len(rows) != meta["document_count"]:
                raise ValueError("搜索索引计数不符")
            for row in rows:
                validate_vector(row["embedding"])
            if self.path.stat().st_ino != stat.st_ino:
                db.close()
                return self._load()
        except BaseException:
            db.close()
            raise
        if self._db is not None:
            self._db.close()
        self._db, self._meta, self._rows = db, meta, rows
        self._signature, self._matrix = signature, None
        self._cache.clear()

    def _scores(self, text, mode):
        cache_key = (text, mode)
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]
        keyword, semantic = {}, {}
        if mode != "semantic":
            expression = match_expression(phrase_groups(text))
            if expression:
                matches = self._db.execute(
                    "SELECT d.source_key FROM brief_search_fts JOIN brief_search_documents d "
                    "ON d.id=brief_search_fts.rowid WHERE brief_search_fts MATCH ? "
                    "ORDER BY bm25(brief_search_fts,8.0,1.0),d.source_key", (expression,),
                )
                keyword = {row[0]: rank for rank, row in enumerate(matches)}
        if mode != "keyword" and self._rows:
            if self._encoder is None:
                self._encoder = (self.encoder_factory() if self.encoder_factory else LocalEncoder(self.model_dir))
            for key in ("model", "model_revision", "model_sha256", "tokenizer_sha256", "dimensions", "pooling", "normalization"):
                if self._encoder.metadata.get(key) != self._meta.get(key):
                    raise ValueError("查询模型与搜索索引不符：" + key)
            import numpy as np

            if self._matrix is None:
                self._matrix = np.vstack([np.frombuffer(row["embedding"], dtype="<f4") for row in self._rows])
            vector = np.frombuffer(self._encoder.encode(["query: " + text])[0], dtype="<f4")
            scores = self._matrix @ vector
            semantic = {row["source_key"]: float(score) for row, score in zip(self._rows, scores, strict=True)}
        value = (keyword, semantic)
        self._cache[cache_key] = value
        if len(self._cache) > 64:
            self._cache.popitem(last=False)
        return value

    def _search(self, text, mode, allowed_keys):
        self._load()
        keyword, semantic = self._scores(text, mode)
        lexical = {key: rank for key, rank in keyword.items() if key in allowed_keys}
        if mode == "keyword":
            return sorted(lexical, key=lexical.get)
        related = sorted((key for key in semantic if key in allowed_keys), key=lambda key: (-semantic[key], key))
        # Similarity is not a probability. This is an initial candidate window,
        # not an assertion that every returned article answers the query.
        top = semantic[related[0]] if related else 1.0
        candidates = {key for key in related[:100] if semantic[key] >= max(0.78, top - 0.06)}
        if mode == "semantic":
            return [key for key in related if key in candidates]
        candidates.update(lexical)
        # Only literal AND/phrase matches receive a lexical boost. Broad OR
        # recall of common English words hurt cross-language ranking in trials.
        def score(key):
            boost = 0.08 + 0.02 / (1 + lexical[key] / 10) if key in lexical else 0
            return semantic.get(key, 0) + boost
        return sorted(candidates, key=lambda key: (-score(key), key))

    def close(self):
        def release():
            if self._db is not None:
                self._db.close()
        self._worker.submit(release).result(timeout=30)
        self._worker.shutdown(wait=True)
