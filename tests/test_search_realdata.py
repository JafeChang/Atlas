"""T-205 真实数据流证据（硬规则 1）：对**真实归档数据**建索引并跑真实查询。

数据从哪来（**只读**）
----------------------

`data/` 里**没有**新结构的归档存储（`data/store/` 不存在），存在的只有旧系统的
534 份真实抓取产物：`data/raw/<频道>/*.json`，正文在 `raw_content` 字段
（SPEC §7.1 / §8.1 记录的那批"唯一有真实产物佐证"的数据）。因此本测试：

1. **只读**读取这些 JSON（`data/` 一个字节都不写，见测试内外的整树 sha256 对比）；
2. 把 `raw_content` 当作**原文**走 T-103 的 `ArchiveStore.put`，落进 `tmp_path` 下的
   临时存储根 → 得到真正的新结构归档记录（真实的 `raw_id` / `content_sha256` / `fetched_at`）；
3. 由 `ArchiveDocumentSource`（raw → T-104 归一化）建 T-205 索引；
4. 跑真实查询、删索引、重建、比对，并对第二个独立库文件重复一次。

**真实数字里有一个必须说明的落差**：534 个 JSON 文件里 474 个有可用正文，但旧系统
**没有去重**——同一篇文章被重复采集了 10 次（同 `source_url`、同正文、不同 UUID）。
新归档按内容寻址（`raw_id = f(channel, endpoint, content_sha256)`），因此这 474 次写入
收敛成 **65 条真实文档**。索引覆盖的正是这 65 条（`put` 是幂等的，见 T-103 契约）。

数据不在时跳过
--------------

`data/` **不进 git**（SPEC §8.1），所以干净 worktree 里没有它——此时本测试
`skip`（而不是失败），真实证据由主工作区的那次运行给出。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.search import (
    MAX_LIMIT,
    ArchiveDocumentSource,
    SearchQuery,
    SqliteSearchIndex,
    open_index,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_RAW = REPO_ROOT / "data" / "raw"
LEGACY_DB = REPO_ROOT / "data" / "atlas.db"

#: 真实查询：领域词（`machine learning` 在真实语料里可能为 0 命中，那也必须是"明确空结果"）。
REAL_QUERIES = (
    "model",
    "data",
    "artificial intelligence",
    "machine learning",
)

pytestmark = pytest.mark.skipif(
    not (LEGACY_RAW.is_dir() and any(LEGACY_RAW.rglob("*.json"))),
    reason="本地真实归档数据缺失（data/ 不进 git，见 SPEC §8.1）",
)


# --------------------------------------------------------------------------- #
# 读取真实数据（只读）
# --------------------------------------------------------------------------- #
def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


def _db_digest() -> str | None:
    return hashlib.sha256(LEGACY_DB.read_bytes()).hexdigest() if LEGACY_DB.is_file() else None


def _parse_moment(value: object, fallback: datetime) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    return fallback


def _load_legacy_documents() -> List[Tuple[str, str, bytes, datetime]]:
    """返回 `(channel_id, endpoint, content, fetched_at)`；正文为 UTF-8 字节。"""
    documents: List[Tuple[str, str, bytes, datetime]] = []
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
        endpoint = payload.get("source_url")
        if not isinstance(endpoint, str) or not endpoint:
            endpoint = f"legacy://{channel_id}/{path.name}"
        fetched_at = _parse_moment(
            payload.get("collected_at") or payload.get("stored_at"), BASE
        )
        documents.append((channel_id, endpoint, content.encode("utf-8"), fetched_at))
    return documents


def _seed_real_archive(root: Path) -> Tuple[ArchiveStore, Dict[str, str]]:
    """把真实正文写进临时存储根（**新结构**归档），返回 `(archive, 渠道→行业 映射)`。"""
    archive = open_archive(root)
    channels: Dict[str, str] = {}
    for channel_id, endpoint, content, fetched_at in _load_legacy_documents():
        record = RawRecord(
            raw_id=raw_id_for(channel_id, endpoint, content_sha256(content)),
            channel_id=channel_id,
            endpoint=endpoint,
            content_sha256=content_sha256(content),
            byte_length=len(content),
            fetched_at=fetched_at,
            http_status=200,
        )
        archive.put(record, content)
        channels[channel_id] = f"{channel_id}-industry"
    return archive, channels


def _rare_term(archive: ArchiveStore) -> str:
    """从**真实正文**里取一个罕见词（字典序最大、长度 ≥ 6 的纯字母词）。"""
    raw_id = archive.all_raw_ids()[0]
    words = archive.get_content(raw_id).decode("utf-8", "replace").split()
    candidates = sorted({word.strip(".,;:()[]{}\"'").lower() for word in words})
    long_words = [word for word in candidates if len(word) >= 6 and word.isalpha()]
    assert long_words, "取不到可用于验证的罕见词"
    return long_words[-1]


def _snapshot(index, queries) -> List[Tuple]:
    pages = []
    for query in queries:
        result = index.search(query)
        pages.append(
            tuple(
                (hit.raw_id, hit.score, hit.fts_score, hit.snippet, hit.fetched_at)
                for hit in result.items
            )
        )
    return pages


@pytest.fixture(scope="module")
def real_store(tmp_path_factory: pytest.TempPathFactory):
    """真实归档（内容寻址收敛后）只建一次；`data/` 只读。"""
    root = tmp_path_factory.mktemp("t205-real-store")
    archive, channels = _seed_real_archive(root)
    try:
        yield root, archive, channels
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 证据
# --------------------------------------------------------------------------- #
def test_real_archive_indexing_and_real_queries(real_store) -> None:
    """真实数据流：raw（真实抓取正文）→ 归一化 → FTS 索引 → 真实查询 → 删→重建。"""
    legacy_before = _tree_digest(LEGACY_RAW)
    db_before = _db_digest()

    root, archive, channels = real_store
    json_files = len(list(LEGACY_RAW.rglob("*.json")))
    distinct = len(archive.all_raw_ids())
    assert distinct >= 60, f"真实归档只有 {distinct} 条文档，数据可能已缺失"

    index = open_index(root)
    try:
        report = index.rebuild(
            ArchiveDocumentSource(archive).iter_documents(), industry_of=channels
        )
        assert report.document_count == distinct
        assert index.count() == report.document_count
        assert report.industry_source == "injected"
        assert len(report.corpus_sha256) == 64
        print(
            f"\n[T-205 真实数据] JSON 文件 {json_files} 个 → 归档 {distinct} 条文档"
            f"（旧系统未去重，按内容寻址收敛）→ 索引 {report.document_count} 篇"
            f"，空文本 {report.empty_text_count} 篇；corpus_sha256={report.corpus_sha256[:16]}…"
        )

        # 真实查询：命中数 + 排序（逐条打印，供报告引用真实数字）
        for text in REAL_QUERIES:
            page = index.search(SearchQuery(text=text, limit=5))
            big = index.search(SearchQuery(text=text, limit=MAX_LIMIT))
            assert page.total == big.total, "不同 limit 下的 total 必须一致"
            assert page.total >= len(page.items)
            keys = [(-hit.score, -hit.fetched_at.timestamp(), hit.raw_id) for hit in big.items]
            assert keys == sorted(keys), "真实结果必须按 (score desc, fetched_at desc, raw_id asc) 有序"
            print(
                f"[T-205 真实数据] 查询 {text!r}: 命中 {page.total} / 索引 {report.document_count}，"
                f"前 {len(page.items)} 条 = "
                + ", ".join(f"{hit.raw_id[4:14]}…(score={hit.score:.4g})" for hit in page.items)
            )

        # 「按 raw_id 取单条」
        sample_id = archive.all_raw_ids()[0]
        stored = index.get(sample_id)
        assert stored is not None and stored.text_length > 0
        print(f"[T-205 真实数据] get({sample_id[4:14]}…): text_length={stored.text_length}")

        # 行业筛选在真实渠道映射上闭合：各行业命中数之和 == 无筛选命中数（§2.5 C8 闭环）
        total = index.search(SearchQuery(text="data")).total
        per_industry = sum(
            index.search(SearchQuery(text="data", industries=(industry,))).total
            for industry in sorted(set(channels.values()))
        )
        assert per_industry == total, "行业筛选没有覆盖全部命中（C8 闭环断裂）"
        print(
            f"[T-205 真实数据] 行业筛选闭合：{len(set(channels.values()))} 个行业命中数之和 "
            f"{per_industry} == 无筛选 {total}"
        )

        # 逐渠道筛选也必须与总命中一致
        per_channel = sum(
            index.search(SearchQuery(text="data", channels=(channel,))).total
            for channel in sorted(channels)
        )
        assert per_channel == total

        # 罕见词（取自真实正文）必须能在它自己的文档里被检出
        rare = _rare_term(archive)
        rare_result = index.search(SearchQuery(text=rare, limit=MAX_LIMIT))
        assert rare_result.total >= 1, f"罕见词 {rare!r} 应至少命中 1 条"
        print(
            f"[T-205 真实数据] 罕见词 {rare!r}（取自真实正文）：命中 {rare_result.total}，"
            f"snippet={rare_result.items[0].snippet[:60]!r}"
        )

        queries = [SearchQuery(text=text) for text in REAL_QUERIES]
        queries.append(SearchQuery(text=rare, limit=MAX_LIMIT))
        queries.append(SearchQuery(text="data", order="recency"))
        before = _snapshot(index, queries)

        # 删索引 → 从 raw 全量重建 → 结果完全一致
        index.drop()
        assert index.is_built() is False
        assert index.count() == 0
        rebuilt = index.rebuild(
            ArchiveDocumentSource(archive).iter_documents(), industry_of=channels
        )
        after = _snapshot(index, queries)
        assert before == after, "重建前后检索结果不一致"
        assert rebuilt.corpus_sha256 == report.corpus_sha256
        assert rebuilt.document_count == report.document_count
        print(
            f"[T-205 真实数据] drop → rebuild：{len(queries)} 条查询的 "
            f"(raw_id, score, snippet, fetched_at) 序列逐条相同；corpus_sha256 相同"
        )

        # 第二个独立库文件：同样一致
        other = SqliteSearchIndex(root.parent / "other.db")
        try:
            other.rebuild(
                ArchiveDocumentSource(archive).iter_documents(), industry_of=channels
            )
            assert _snapshot(other, queries) == before
            assert other.meta()["corpus_sha256"] == report.corpus_sha256
        finally:
            other.close()
        print("[T-205 真实数据] 独立第二个库文件重建：结果与首个库逐条相同")
    finally:
        index.close()

    assert archive.verify() == []
    # 真实数据一个字节都没被改动（判据 A3）
    assert _tree_digest(LEGACY_RAW) == legacy_before
    assert _db_digest() == db_before


def test_real_corpus_is_not_tiny() -> None:
    """真实数据规模下限（防止"数据没了但测试还绿"）。"""
    documents = _load_legacy_documents()
    assert len(documents) >= 100, f"真实可索引文档只有 {len(documents)} 篇，数据可能已缺失"
    channels = {channel_id for channel_id, *_ in documents}
    assert len(channels) >= 3, f"真实频道只有 {len(channels)} 个：{sorted(channels)}"
    assert all(len(content) > 0 for *_, content, _ in documents)
    assert all(isinstance(moment, datetime) for *_, moment in documents)
