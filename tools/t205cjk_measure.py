"""T-205 修订评估：真实语料上的索引体积与英文查询回归（可对**任意版本**的 src 跑）。

用法（WSL）：
    # 修订版（主工作区 src）
    ./.venv-new/bin/python tools/t205cjk_measure.py --out /tmp/t205cjk-new.json
    # 基线版（T-205 提交 6d001b4 的独立 worktree）
    PYTHONPATH=/tmp/atlas-verify-t205cjk-baseline/src \
      ./.venv-new/bin/python tools/t205cjk_measure.py --out /tmp/t205cjk-old.json

只读 `data/raw`，只写临时目录与 `--out`。输出包含：
- 索引库文件字节数（含 FTS5 影子表）；
- 真实语料（65 篇，全英文）的英文查询 `(raw_id, score, fts_score, snippet)` 序列；
- 版本信息（INDEX_VERSION / SCHEMA_VERSION / 索引里 text_index 列是否存在）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RAW = REPO_ROOT / "data" / "raw"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: 与 `tests/test_search_realdata.py` 相同的英文查询（真实语料回归网）。
QUERIES = ("model", "data", "artificial intelligence", "machine learning", "retrieval", "bm25")


def load_legacy() -> List[tuple]:
    out: List[tuple] = []
    for path in sorted(LEGACY_RAW.rglob("*.json")):
        try:
            payload = json.loads(path.read_bytes().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        content = payload.get("raw_content")
        if not isinstance(content, str) or not content.strip():
            continue
        channel_id = path.parent.name
        endpoint = payload.get("source_url") or f"legacy://{channel_id}/{path.name}"
        out.append((channel_id, endpoint, content.encode("utf-8")))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    import atlas.search as search_mod
    from atlas.archive import open_archive
    from atlas.contracts import RawRecord
    from atlas.contracts.ids import content_sha256, raw_id_for
    from atlas.search import ArchiveDocumentSource, SearchQuery, open_index

    def version_of(name: str) -> Any:
        return getattr(search_mod, name, None)

    root = Path(tempfile.mkdtemp(prefix="t205cjk-measure-"))
    archive = open_archive(root)
    rows = load_legacy()
    channels: Dict[str, str] = {}
    for channel_id, endpoint, content in rows:
        digest = content_sha256(content)
        archive.put(
            RawRecord(
                raw_id=raw_id_for(channel_id, endpoint, digest),
                channel_id=channel_id,
                endpoint=endpoint,
                content_sha256=digest,
                byte_length=len(content),
                fetched_at=BASE,
                http_status=200,
            ),
            content,
        )
        channels[channel_id] = f"{channel_id}-industry"

    index = open_index(root)
    pages_before_index = index.connection.execute("PRAGMA page_count").fetchone()[0]
    report = index.rebuild(ArchiveDocumentSource(archive).iter_documents(), industry_of=channels)
    page_size = index.connection.execute("PRAGMA page_size").fetchone()[0]
    pages_after_index = index.connection.execute("PRAGMA page_count").fetchone()[0]
    index_pages = pages_after_index - pages_before_index

    # 若本构建带 dbstat，给出按对象分组的精确字节数（没有就跳过，不假装有）
    per_object: Dict[str, int] = {}
    try:
        for name, size in index.connection.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY name"
        ).fetchall():
            per_object[str(name)] = int(size or 0)
    except Exception:  # noqa: BLE001 - dbstat 是可选编译项
        per_object = {}

    db_path = root / "atlas.db"
    sizes = {"total_bytes": 0, "by_suffix": {}}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        sizes["total_bytes"] += size
        suffix = path.name.split(".", 1)[1] if "." in path.name else path.name
        sizes["by_suffix"][suffix] = sizes["by_suffix"].get(suffix, 0) + size

    columns: Dict[str, List[str]] = {}
    for table in ("search_documents", "search_documents_fts", "search_meta"):
        try:
            columns[table] = [
                row[1] for row in index.connection.execute(f"PRAGMA table_info({table})")
            ]
        except Exception as exc:  # noqa: BLE001 - 只为报告
            columns[table] = [f"<{type(exc).__name__}: {exc}>"]

    # page / freelist 级别的体积（比文件字节更能说明"索引本体"多大）
    page_stats = {
        "page_size": index.connection.execute("PRAGMA page_size").fetchone()[0],
        "page_count": index.connection.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": index.connection.execute("PRAGMA freelist_count").fetchone()[0],
    }

    results: Dict[str, List[List[Any]]] = {}
    for text in QUERIES:
        result = index.search(SearchQuery(text=text, limit=200))
        results[text] = [
            [hit.raw_id, repr(hit.score), hit.snippet] for hit in result.items
        ]

    text_bytes = index.connection.execute(
        "SELECT SUM(LENGTH(text)) AS n FROM search_documents"
    ).fetchone()["n"]
    index_bytes = (
        index.connection.execute(
            "SELECT SUM(LENGTH(text_index)) AS n FROM search_documents"
        ).fetchone()["n"]
        if "text_index" in columns.get("search_documents", [])
        else None
    )
    index.close()
    archive.close()

    payload = {
        "label": args.label,
        "src": str(Path(sys.modules["atlas"].__file__).resolve().parents[2]),
        "index_version": version_of("INDEX_VERSION"),
        "schema_version": version_of("SCHEMA_VERSION"),
        "segmentation": version_of("SEGMENTATION"),
        "documents": report.document_count,
        "corpus_sha256": report.corpus_sha256,
        "db_bytes": sizes,
        "page_stats": page_stats,
        "page_size": page_size,
        "pages_before_index": pages_before_index,
        "pages_after_index": pages_after_index,
        "index_pages": index_pages,
        "index_bytes": index_pages * page_size,
        "per_object_bytes": per_object,
        "columns": columns,
        "sum_length_text": text_bytes,
        "sum_length_text_index": index_bytes,
        "queries": results,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")

    print(f"[{args.label}] src={payload['src']}")
    print(f"[{args.label}] index_version={payload['index_version']} schema={payload['schema_version']}")
    print(f"[{args.label}] documents={report.document_count} corpus_sha256={report.corpus_sha256[:16]}…")
    print(f"[{args.label}] db total bytes={sizes['total_bytes']} by_suffix={sizes['by_suffix']}")
    print(f"[{args.label}] page_size={page_stats['page_size']} page_count={page_stats['page_count']} freelist={page_stats['freelist_count']}")
    print(
        f"[{args.label}] 索引本体：{pages_before_index} → {pages_after_index} 页"
        f"（{index_pages} 页 = {index_pages * page_size} 字节）"
    )
    if per_object:
        for name in sorted(per_object):
            print(f"[{args.label}]   object {name}: {per_object[name]} 字节")
    print(f"[{args.label}] SUM(LENGTH(text))={text_bytes} SUM(LENGTH(text_index))={index_bytes}")
    for text in QUERIES:
        print(f"[{args.label}] query {text!r}: hits={len(results[text])}")
    print(f"[{args.label}] written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
