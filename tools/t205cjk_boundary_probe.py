"""残余边界的**实测**取证：汉字与拉丁**直接相邻**（中间无空白）时，跨该边界的子串仍查不到。

契约（用户已定）是"只在两个相邻汉字之间的空白间隙插入空格，其它位置一律不动"，
因此 `数据库abc` 里的 `库` 与 `a` 之间不插空格 ⇒ `库abc` 是**一个** token ⇒
查询 `数据库`（短语 `"数 据 库"`）命中不了它。本脚本把这条边界跑出来，
用真实数字说明影响面，供是否扩展切分规则的裁决参考。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/mnt/c/Users/bestz/Documents/projects/Atlas")
sys.path.insert(0, str(REPO / "src"))

from atlas.archive import open_archive  # noqa: E402
from atlas.contracts import RawRecord  # noqa: E402
from atlas.contracts.ids import content_sha256, raw_id_for  # noqa: E402
from atlas.search import ArchiveDocumentSource, SearchQuery, open_index, segment_cjk  # noqa: E402

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

DOCS = (
    "Transformer架构与注意力机制",
    "GPT模型与向量数据库abc的工程实践",
    "数据库系统概论",
    "纯中文分词测试",
)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="t205cjk-boundary-"))
    archive = open_archive(root)
    for index, body in enumerate(DOCS):
        content = body.encode("utf-8")
        digest = content_sha256(content)
        archive.put(
            RawRecord(
                raw_id=raw_id_for("ch", f"https://example.invalid/{index}", digest),
                channel_id="ch",
                endpoint=f"https://example.invalid/{index}",
                content_sha256=digest,
                byte_length=len(content),
                fetched_at=BASE,
                http_status=200,
            ),
            content,
        )
    idx = open_index(root)
    idx.rebuild(ArchiveDocumentSource(archive).iter_documents())

    for body in DOCS:
        print(f"原文 {body!r}\n  → 切分 {segment_cjk(body)!r}")

    print()
    for query in ("架构", "注意力机制", "模型", "向量数据库", "数据库", "纯中文", "分词"):
        result = idx.search(SearchQuery(text=query, limit=10))
        hits = [idx.get(hit.raw_id).text for hit in result.items]
        print(f"查询 {query!r:>12} → 命中 {result.total}: {hits}")
    idx.close()
    archive.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
