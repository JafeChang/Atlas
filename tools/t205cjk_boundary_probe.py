"""汉字↔拉丁边界切分的**实测取证**（切分契约 `/2`）。

`Transformer架构`、`GPT模型`、`向量数据库abc` 这种混写在中文技术文本里是常态。
切分规则扩到"两个词元字符之间只要有一侧是汉字就插分隔符"之后，跨该边界的汉字子串
必须可查。扩展前的实测是 `架构`→0、`模型`→0、`向量数据库`→0（见交付报告 §8），
本脚本用同一批语料跑出扩展后的正确行为，并顺带核对往返性质。

只写临时目录，不碰 `data/`。
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
from atlas.search import (  # noqa: E402
    ArchiveDocumentSource,
    SearchQuery,
    desegment,
    open_index,
    segment_cjk,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)

DOCS = (
    "Transformer架构与注意力机制",
    "GPT模型与向量数据库abc的工程实践",
    "数据库系统概论",
    "纯中文分词测试",
    "第3章 BERT模型 的微调",
)

#: `查询 -> 期望命中数`（正确行为；扩展前 `架构` / `模型` / `向量数据库` 都是 0）
EXPECTED = {
    "架构": 1,
    "注意力机制": 1,
    "模型": 2,
    "向量数据库": 1,
    "数据库": 2,
    "纯中文": 1,
    "分词": 1,
    "第3章": 1,
    "BERT": 1,
    "第": 1,
    "章": 1,
}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="t205cjk-boundary-"))
    archive = open_archive(root)
    for position, body in enumerate(DOCS):
        content = body.encode("utf-8")
        digest = content_sha256(content)
        archive.put(
            RawRecord(
                raw_id=raw_id_for("ch", f"https://example.invalid/{position}", digest),
                channel_id="ch",
                endpoint=f"https://example.invalid/{position}",
                content_sha256=digest,
                byte_length=len(content),
                fetched_at=BASE,
                http_status=200,
            ),
            content,
        )
    index = open_index(root)
    index.rebuild(ArchiveDocumentSource(archive).iter_documents())

    for body in DOCS:
        segmented = segment_cjk(body)
        print(f"原文 {body!r}\n  → 切分 {segmented!r}\n  → 还原 {desegment(segmented)!r}")
        assert desegment(segmented) == body, "往返性质被破坏"

    failures = []
    print()
    for query, expected in EXPECTED.items():
        result = index.search(SearchQuery(text=query, limit=10))
        hits = [index.get(hit.raw_id).text for hit in result.items]
        ok = result.total == expected
        if not ok:
            failures.append(query)
        print(
            f"查询 {query!r:>12} → 命中 {result.total}（期望 {expected}）"
            f"{'' if ok else '  <<< 不符'}  expression={result.expression!r}"
        )
        print(f"{'':>18}命中文档：{hits}")

    print(f"\n不符的查询：{failures}")
    index.close()
    archive.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
