"""T-205 索引层验收：FTS5 探测、可全量重建、排序确定性、查询健壮性、越界参数、线程安全。

判据全文见 `tests/test_search_query.py` 的模块 docstring（**先于实现写定**）。
本文件覆盖 A1 / A2 / A4 / A5 / A6 的行为部分，以及 A2 的版本不符补救路径。
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.search import (
    DOCS_TABLE,
    FTS_TABLE,
    INDEX_VERSION,
    INDUSTRY_SOURCE_INJECTED,
    INDUSTRY_SOURCE_NONE,
    META_TABLE,
    ArchiveDocumentSource,
    DocumentText,
    EmptyQueryError,
    Fts5UnavailableError,
    IndexVersionError,
    SearchIndexError,
    SearchQuery,
    SearchQueryError,
    SqliteSearchIndex,
    drop_search_index,
    open_index,
    probe_fts5,
)
from atlas.search import sqlite_index as sqlite_index_module

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa"


# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #
def make_record(
    channel_id: str,
    endpoint: str,
    content: bytes,
    *,
    fetched_at: datetime = BASE,
    http_status: int | None = 200,
) -> RawRecord:
    digest = content_sha256(content)
    return RawRecord(
        raw_id=raw_id_for(channel_id, endpoint, digest),
        channel_id=channel_id,
        endpoint=endpoint,
        content_sha256=digest,
        byte_length=len(content),
        fetched_at=fetched_at,
        http_status=http_status,
    )


def add_document(
    archive: ArchiveStore,
    channel_id: str,
    endpoint: str,
    body: str,
    *,
    fetched_at: datetime = BASE,
) -> RawRecord:
    content = body.encode("utf-8")
    return archive.put(make_record(channel_id, endpoint, content, fetched_at=fetched_at), content)


def make_document(
    channel_id: str,
    endpoint: str,
    body: str,
    *,
    fetched_at: datetime = BASE,
) -> DocumentText:
    content = body.encode("utf-8")
    return DocumentText(
        record=make_record(channel_id, endpoint, content, fetched_at=fetched_at),
        text=body,
    )


class Harness:
    """一个临时存储根 + 归档 + 索引（全部在 `tmp_path` 下，绝不碰仓库 `data/`）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.archive = open_archive(root)
        self.index = open_index(root)

    @property
    def db_path(self) -> Path:
        return self.root / "atlas.db"

    def add(self, channel_id: str, endpoint: str, body: str, **kwargs: object) -> RawRecord:
        return add_document(self.archive, channel_id, endpoint, body, **kwargs)  # type: ignore[arg-type]

    def rebuild(self, *, industry_of: object = None, documents: object = None):
        docs = (
            ArchiveDocumentSource(self.archive).iter_documents()
            if documents is None
            else documents
        )
        return self.index.rebuild(docs, industry_of=industry_of)  # type: ignore[arg-type]

    def close(self) -> None:
        self.index.close()
        self.archive.close()


@pytest.fixture()
def harness(tmp_path: Path):
    made = Harness(tmp_path / "store")
    try:
        yield made
    finally:
        made.close()


def full(result) -> list[tuple]:
    """命中的完整可比指纹：raw_id / score / fts_score / snippet / fetched_at。"""
    return [
        (hit.raw_id, hit.score, hit.fts_score, hit.snippet, hit.fetched_at) for hit in result.items
    ]


# --------------------------------------------------------------------------- #
# A1：FTS5 可用性
# --------------------------------------------------------------------------- #
def test_probe_fts5_passes_on_this_build() -> None:
    assert probe_fts5() is None


def test_probe_fts5_works_on_a_file_connection_without_polluting_it(tmp_path: Path) -> None:
    conn = sqlite3.connect(str(tmp_path / "probe.db"))
    try:
        probe_fts5(conn)
        leftovers = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%probe%'"
            ).fetchall()
        ]
        assert leftovers == []
    finally:
        conn.close()


class _FailingConnection:
    """假连接：只用来驱动探测的失败分支（不碰真实 sqlite）。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[str] = []
        self.closed = False

    def execute(self, sql: str, *args: object):
        self.calls.append(sql)
        raise self.error

    def close(self) -> None:
        self.closed = True


def test_probe_fts5_raises_loudly_when_module_is_missing() -> None:
    fake = _FailingConnection(sqlite3.OperationalError("no such module: fts5"))
    with pytest.raises(Fts5UnavailableError) as info:
        probe_fts5(fake)  # type: ignore[arg-type]
    message = str(info.value)
    assert "FTS5" in message and "LIKE" in message
    assert "no such module: fts5" in message
    # 未使用调用方的连接做关闭动作（own=False）
    assert fake.closed is False


def test_probe_fts5_does_not_mask_other_wiring_errors() -> None:
    """非 "no such module" 的 `OperationalError` 必须原样上抛（硬规则 2）。"""
    fake = _FailingConnection(sqlite3.OperationalError("database is locked"))
    with pytest.raises(sqlite3.OperationalError) as info:
        probe_fts5(fake)  # type: ignore[arg-type]
    assert "database is locked" in str(info.value)


def test_index_construction_probes_fts5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构造索引时**必须**先探测：探测失败要发生在建立任何东西之前。"""
    calls: list[int] = []
    real = sqlite_index_module.probe_fts5

    def spy() -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(sqlite_index_module, "probe_fts5", spy)
    index = SqliteSearchIndex(tmp_path / "atlas.db")
    index.close()
    assert calls == [1]


def test_in_memory_index_works_without_touching_the_filesystem() -> None:
    index = SqliteSearchIndex(":memory:")
    try:
        index.rebuild([make_document("ch-a", "https://x.invalid/1", "alpha beta")])
        assert index.search(SearchQuery(text="alpha")).total == 1
        index.drop()
        index.rebuild([make_document("ch-a", "https://x.invalid/2", "gamma")])
        assert index.search(SearchQuery(text="gamma")).total == 1
    finally:
        index.close()


# --------------------------------------------------------------------------- #
# A2：可全量重建
# --------------------------------------------------------------------------- #
def seed(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "Vector database retrieval with BM25 ranking.")
    harness.add("ch-a", "https://x.invalid/2", "BM25 is a ranking function used by search engines.")
    harness.add("ch-b", "https://x.invalid/3", "Gardening tips for spring planting.")
    harness.add("ch-b", "https://x.invalid/4", "A vector database stores embeddings for retrieval.")


def test_rebuild_records_index_identity(harness: Harness) -> None:
    seed(harness)
    report = harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "garden"})

    assert report.document_count == 4
    assert report.empty_text_count == 0
    assert report.index_version == INDEX_VERSION
    assert report.tokenizer == "unicode61 remove_diacritics 2"
    assert report.industry_source == INDUSTRY_SOURCE_INJECTED
    assert len(report.corpus_sha256) == 64

    meta = harness.index.meta()
    assert meta["index_version"] == INDEX_VERSION
    assert meta["tokenizer"] == "unicode61 remove_diacritics 2"
    assert meta["text_source"].startswith("atlas.normalize.normalize")
    assert meta["document_count"] == "4"
    assert meta["corpus_sha256"] == report.corpus_sha256
    assert meta["industry_source"] == INDUSTRY_SOURCE_INJECTED
    assert meta["index_built"] == "1"
    assert meta["built_at"] == report.built_at.isoformat()
    assert harness.index.is_built() is True
    assert harness.index.count() == 4


def test_corpus_sha256_is_stable_and_sensitive_to_the_corpus(harness: Harness) -> None:
    seed(harness)
    first = harness.rebuild()
    second = harness.rebuild()
    assert first.corpus_sha256 == second.corpus_sha256
    harness.add("ch-a", "https://x.invalid/9", "An extra document about vector search.")
    third = harness.rebuild()
    assert third.corpus_sha256 != first.corpus_sha256


QUERIES = (
    SearchQuery(text="bm25"),
    SearchQuery(text="bm25 ranking"),
    SearchQuery(text="vector database", limit=3),
    SearchQuery(text="retrieval", order="recency"),
)


def test_drop_then_rebuild_gives_identical_results(harness: Harness) -> None:
    """判据 A2 的核心：删索引后从 raw 重建，检索结果**完全一致**。"""
    seed(harness)
    first = harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "garden"})
    before = [full(harness.index.search(query)) for query in QUERIES]
    assert all(page for page in before)

    harness.index.drop()
    assert harness.index.is_built() is False
    assert harness.index.count() == 0

    second = harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "garden"})
    after = [full(harness.index.search(query)) for query in QUERIES]

    assert before == after
    assert first.corpus_sha256 == second.corpus_sha256
    assert first.document_count == second.document_count


def test_rebuild_is_idempotent_without_drop(harness: Harness) -> None:
    """重复 `rebuild()`（不先删）同样必须收敛到同一结果。"""
    seed(harness)
    harness.rebuild(industry_of={"ch-a": "ai"})
    before = [full(harness.index.search(query)) for query in QUERIES]
    harness.rebuild(industry_of={"ch-a": "ai"})
    after = [full(harness.index.search(query)) for query in QUERIES]
    assert before == after
    assert harness.index.count() == 4


def test_rebuild_in_a_second_database_is_identical(harness: Harness, tmp_path: Path) -> None:
    """两个不同库文件里独立重建：结果必须逐字段相同。"""
    seed(harness)
    harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "garden"})
    expected = [full(harness.index.search(query)) for query in QUERIES]

    other = SqliteSearchIndex(tmp_path / "other.db")
    try:
        other.rebuild(
            ArchiveDocumentSource(harness.archive).iter_documents(),
            industry_of={"ch-a": "ai", "ch-b": "garden"},
        )
        observed = [full(other.search(query)) for query in QUERIES]
        other_meta = other.meta()
    finally:
        other.close()
    assert expected == observed
    # `built_at` 是 observed（不参与身份），但语料摘要必须一致
    assert other_meta["corpus_sha256"] == harness.index.meta()["corpus_sha256"]


def test_index_version_mismatch_fails_loudly_and_is_recoverable(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    expected = full(harness.index.search(SearchQuery(text="bm25")))

    with harness.index.lock:
        harness.index.connection.execute(
            f"UPDATE {META_TABLE} SET value = ? WHERE key = 'index_version'",
            ("atlas.search.index/0",),
        )
    with pytest.raises(IndexVersionError) as info:
        harness.index.search(SearchQuery(text="bm25"))
    message = str(info.value)
    assert "索引版本不符" in message and "重建" in message
    # is_built 也如实反映"版本不可用"
    assert harness.index.is_built() is False
    # 读路径响亮失败，但重建路径仍可用（索引是可重建派生物）
    harness.index.drop()
    harness.rebuild()
    assert full(harness.index.search(SearchQuery(text="bm25"))) == expected


def test_schema_version_mismatch_fails_loudly_and_is_recoverable(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    harness.index.close()

    conn = sqlite3.connect(str(harness.db_path))
    conn.execute(f"UPDATE {META_TABLE} SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(IndexVersionError) as info:
        SqliteSearchIndex(harness.db_path)
    assert "schema 版本" in str(info.value)

    # 补救路径：删掉索引表（只删本域的），再开再建
    drop_search_index(harness.db_path)
    recovered = SqliteSearchIndex(harness.db_path)
    try:
        assert recovered.is_built() is False
        recovered.rebuild(ArchiveDocumentSource(harness.archive).iter_documents())
        assert recovered.search(SearchQuery(text="bm25")).total == 2
        # 归档仍完好（索引修复不得动上游）
        assert harness.archive.verify() == []
    finally:
        recovered.close()
        harness.archive.close()


def test_failed_rebuild_leaves_the_previous_index_intact(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重建失败必须回滚：**不留半成品索引**（硬规则 1 的对应面）。"""
    seed(harness)
    harness.rebuild()
    expected = full(harness.index.search(SearchQuery(text="bm25")))
    assert expected

    real = sqlite_index_module._document_row
    calls = {"n": 0}

    def flaky(doc_rowid: int, document: DocumentText, industry):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟重建中途失败")
        return real(doc_rowid, document, industry)

    monkeypatch.setattr(sqlite_index_module, "_document_row", flaky)
    with pytest.raises(RuntimeError):
        harness.rebuild()
    monkeypatch.undo()

    assert full(harness.index.search(SearchQuery(text="bm25"))) == expected
    assert harness.index.count() == 4
    assert harness.archive.verify() == []


def test_max_documents_cap_is_rejected_not_truncated(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite_index_module, "MAX_DOCUMENTS", 2)
    documents = [make_document("ch-a", f"https://x.invalid/{index}", "alpha") for index in range(3)]
    with pytest.raises(SearchIndexError) as info:
        harness.index.rebuild(documents)
    assert "上限" in str(info.value)


# --------------------------------------------------------------------------- #
# A2/A5：索引输入契约
# --------------------------------------------------------------------------- #
def test_duplicate_raw_id_with_same_text_is_idempotent(harness: Harness) -> None:
    document = make_document("ch-a", "https://x.invalid/dup", "alpha beta")
    report = harness.index.rebuild([document, document])
    assert report.document_count == 1


def test_duplicate_raw_id_with_different_text_is_rejected(harness: Harness) -> None:
    first = make_document("ch-a", "https://x.invalid/dup", "alpha beta")
    second = DocumentText(record=first.record, text="completely different text")
    with pytest.raises(SearchIndexError) as info:
        harness.index.rebuild([first, second])
    assert "不同的归一化文本" in str(info.value)


def test_non_document_input_is_rejected(harness: Harness) -> None:
    with pytest.raises(SearchIndexError):
        harness.index.rebuild(["not a document"])  # type: ignore[list-item]


def test_empty_text_document_is_indexed_and_reported(harness: Harness) -> None:
    """归一化后为空的文档仍进索引（覆盖率完整），但会被计数上报，不会被当成命中。"""
    empty = harness.add("ch-a", "https://x.invalid/empty", "")
    harness.add("ch-a", "https://x.invalid/1", "alpha")
    report = harness.rebuild()
    assert report.document_count == 2
    assert report.empty_text_count == 1
    assert harness.index.search(SearchQuery(text="alpha")).total == 1
    stored = harness.index.get(empty.raw_id)
    assert stored is not None
    assert stored.text == ""
    assert stored.text_length == 0


def test_industry_of_returning_non_string_is_rejected(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha")
    with pytest.raises(SearchIndexError) as info:
        harness.rebuild(industry_of=lambda channel_id: 42)
    assert "industry_of" in str(info.value)


# --------------------------------------------------------------------------- #
# A4：排序确定性
# --------------------------------------------------------------------------- #
def test_repeated_queries_return_identical_order(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    first = full(harness.index.search(SearchQuery(text="vector database retrieval", limit=10)))
    second = full(harness.index.search(SearchQuery(text="vector database retrieval", limit=10)))
    assert first == second
    assert first


def test_relevance_prefers_more_matches(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha beta gamma")
    harness.add("ch-a", "https://x.invalid/2", "alpha only here")
    harness.rebuild()
    result = harness.index.search(SearchQuery(text="alpha beta"))
    assert [hit.raw_id for hit in result.items] == [harness.archive.all_raw_ids()[0]]


def test_equal_scores_break_ties_by_fetched_at_then_raw_id(harness: Harness) -> None:
    """同分必须由次级键决定顺序（真并列：同长度、同词频的正文）。"""
    body = "alpha beta gamma delta"
    ids = {}
    for index, minutes in enumerate((0, 5, 5)):
        record = harness.add(
            "ch-a",
            f"https://x.invalid/{index}",
            body,
            fetched_at=BASE + timedelta(minutes=minutes),
        )
        ids[record.raw_id] = minutes
    harness.rebuild()

    result = harness.index.search(SearchQuery(text="alpha", limit=10))
    assert len(result.items) == 3
    scores = {round(hit.score, 12) for hit in result.items}
    assert len(scores) == 1, "正文相同 ⇒ BM25 分数应真并列，否则这条测试没有覆盖并列路径"
    observed = [hit.raw_id for hit in result.items]
    newest = [rid for rid, minutes in ids.items() if minutes == 5]
    oldest = [rid for rid, minutes in ids.items() if minutes == 0]
    assert observed[0:2] == sorted(newest)  # 同分同时刻 → raw_id 升序
    assert observed[2] == oldest[0]

    # 连续两次结果一致
    assert [hit.raw_id for hit in harness.index.search(SearchQuery(text="alpha", limit=10)).items] == observed


def test_recency_order_matches_feed_tie_break(harness: Harness) -> None:
    ids = []
    for index in range(5):
        record = harness.add(
            "ch-a",
            f"https://x.invalid/{index}",
            "alpha",
            fetched_at=BASE + timedelta(minutes=index),
        )
        ids.append(record.raw_id)
    harness.rebuild()
    result = harness.index.search(SearchQuery(text="alpha", order="recency", limit=10))
    # 时间降序（T-106 的排序键；raw_id 只在并列时兜底）
    assert [hit.raw_id for hit in result.items] == list(reversed(ids))
    assert result.sort_description()["keys"] == ["fetched_at desc", "raw_id asc"]


# --------------------------------------------------------------------------- #
# A5：查询健壮性（行为部分：绝不让 sqlite3 异常穿透）
# --------------------------------------------------------------------------- #
NASTY_INPUTS = (
    '"',
    '"""',
    "*",
    "**",
    "-",
    "--",
    "NOT",
    "OR",
    "AND",
    "NEAR",
    "NEAR(a b)",
    "^",
    "a^b",
    ":",
    "col:value",
    "(",
    ")",
    "()",
    "()()",
    "a AND",
    "AND a",
    "a OR",
    "a NOT b",
    '"unterminated',
    'a"b',
    "\\",
    "{}[]",
    "...",
    "!!!",
    "?",
    "%",
    "_",
    "0",
    "a" * 4096,
    "foo*bar",
    "c++",
    'x" OR "secret',
    "'; DROP TABLE search_documents; --",
    "1; DELETE FROM search_documents",
    "\u4e2d\u6587*",
    "\U0001f600",
)


@pytest.mark.parametrize("text", NASTY_INPUTS)
def test_nasty_input_never_leaks_sqlite_errors(harness: Harness, text: str) -> None:
    seed(harness)
    harness.rebuild()
    try:
        query = SearchQuery(text=text)
    except (EmptyQueryError, SearchQueryError):
        return  # 明确拒绝也是合规结果（判据 A5 允许"明确错误"）
    try:
        result = harness.index.search(query)
    except sqlite3.Error as exc:  # pragma: no cover - 出现即判据被破坏
        raise AssertionError(f"用户输入 {text!r} 让 sqlite3 异常穿透：{exc}") from exc
    assert isinstance(result.total, int)
    assert result.total >= 0
    # 索引仍完好：SQL 注入不可能生效
    assert harness.index.count() == 4
    assert harness.archive.verify() == []


def test_operator_text_searches_literally(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "a document mentioning NEAR and AND and OR")
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="NEAR")).total == 1
    assert harness.index.search(SearchQuery(text="AND")).total == 1
    assert harness.index.search(SearchQuery(text="OR")).total == 1


def test_injection_attempt_does_not_widen_results(harness: Harness) -> None:
    """`x" OR "secret` 必须被当成四个字面词做 AND，而不是"或"查询。"""
    harness.add("ch-a", "https://x.invalid/1", "the secret plan")
    harness.add("ch-a", "https://x.invalid/2", "x marks the spot")
    harness.rebuild()
    query = SearchQuery(text='x" OR "secret')
    assert query.terms() == ("x", "OR", "secret")
    result = harness.index.search(query)
    assert result.total == 0


def test_prefix_operator_is_not_honoured(harness: Harness) -> None:
    """有意不做前缀搜索：`data*` 切词后是字面词 `data`，不匹配 `database`。"""
    harness.add("ch-a", "https://x.invalid/1", "database systems")
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="data*")).total == 0
    assert harness.index.search(SearchQuery(text="database")).total == 1


def test_absent_term_returns_explicit_empty_result(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    result = harness.index.search(SearchQuery(text="nonexistentterm"))
    assert result.total == 0
    assert result.items == ()
    assert result.has_more is False
    assert result.next_offset is None


def test_snippet_highlights_the_matched_terms(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha beta gamma " + " ".join(["pad"] * 40))
    harness.rebuild()
    result = harness.index.search(SearchQuery(text="beta", limit=1))
    hit = result.items[0]
    assert "[beta]" in hit.snippet


def test_diacritics_are_folded_in_both_directions(harness: Harness) -> None:
    """`remove_diacritics 2`：带变音符号与不带变音符号的写法互相命中。"""
    harness.add("ch-a", "https://x.invalid/1", "a café serving naïve analysis")
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="cafe")).total == 1
    assert harness.index.search(SearchQuery(text="café")).total == 1
    assert harness.index.search(SearchQuery(text="naive")).total == 1
    assert harness.index.search(SearchQuery(text="naïve")).total == 1


def test_cjk_runs_are_segmented_so_han_queries_hit(harness: Harness) -> None:
    """T-205 修订：汉字逐字切分后，`中文` **能**命中 `中文分词测试与检索`。

    这条测试原来叫 `test_cjk_runs_are_not_segmented_by_unicode61`，把"查不到中文"这一
    **错误行为**钉死成期望（T-205 当时只能如实记录缺口）。缺口修复后它断言的是
    **正确行为**——不是删掉、也不是放松断言。完整判据见 `tests/test_search_cjk.py`（C1–C6）。
    """
    harness.add("ch-a", "https://x.invalid/1", "中文分词测试与检索 mixed english")
    # 汉字与拉丁**直接相邻**（没有空白）：这是同一个缺口的另一形态，也必须可查
    harness.add("ch-b", "https://x.invalid/2", "Transformer架构与GPT模型")
    harness.rebuild()
    # 整段连写照旧命中
    assert harness.index.search(SearchQuery(text="中文分词测试与检索")).total == 1
    # 修复点：子串汉字现在命中（原来是 0）
    assert harness.index.search(SearchQuery(text="中文")).total == 1
    assert harness.index.search(SearchQuery(text="分词")).total == 1
    assert harness.index.search(SearchQuery(text="中文分词")).total == 1
    assert harness.index.search(SearchQuery(text="测试与检索")).total == 1
    assert harness.index.search(SearchQuery(text="检索")).total == 1
    # 修复点二：跨"汉字↔拉丁"边界的子串也命中（原来是 0）
    assert harness.index.search(SearchQuery(text="架构")).total == 1
    assert harness.index.search(SearchQuery(text="模型")).total == 1
    assert harness.index.search(SearchQuery(text="Transformer")).total == 1
    assert harness.index.search(SearchQuery(text="GPT")).total == 1
    # 查询表达式是**短语**（逐字 AND 会带来大量假阳性）
    assert SearchQuery(text="中文分词").match_expression() == '"中 文 分 词"'
    assert SearchQuery(text="数据库abc").match_expression() == '"数 据 库 abc"'
    # 摘要不得露出切分器插入的分隔符
    snippet = harness.index.search(SearchQuery(text="中文", limit=1)).items[0].snippet
    assert "[中文]" in snippet and "中 文" not in snippet
    mixed = harness.index.search(SearchQuery(text="架构", limit=1)).items[0].snippet
    assert "Transformer[架构]" in mixed and "Transformer 架 构" not in mixed
    # 拉丁词仍按词元匹配，不受影响（前后缀行为不变）
    assert harness.index.search(SearchQuery(text="english")).total == 1
    assert harness.index.search(SearchQuery(text="eng")).total == 0
    # 顺序敏感：倒过来的串不命中（短语而非逐字 AND）
    assert harness.index.search(SearchQuery(text="分词中文")).total == 0


def test_search_text_is_the_normalized_text_not_the_raw_bytes(harness: Harness) -> None:
    """索引的是 T-104 的归一化文本：HTML 标签与 `<script>` 内容都不可检索。"""
    harness.add(
        "ch-a",
        "https://x.invalid/1",
        "<html><head><style>styletoken</style></head>"
        "<body><script>scriptoken</script><p>bodytoken</p></body></html>",
    )
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="bodytoken")).total == 1
    assert harness.index.search(SearchQuery(text="scriptoken")).total == 0
    assert harness.index.search(SearchQuery(text="styletoken")).total == 0
    assert harness.index.search(SearchQuery(text="p")).total == 0


def test_search_before_rebuild_fails_loudly(harness: Harness) -> None:
    with pytest.raises(SearchIndexError) as info:
        harness.index.search(SearchQuery(text="alpha"))
    assert "未构建" in str(info.value)


def test_get_before_rebuild_fails_loudly(harness: Harness) -> None:
    with pytest.raises(SearchIndexError):
        harness.index.get("raw_whatever")


def test_search_rejects_non_query(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    with pytest.raises(SearchQueryError):
        harness.index.search("alpha")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# A6：筛选维度与分页
# --------------------------------------------------------------------------- #
def test_channel_and_industry_and_raw_id_filters(harness: Harness) -> None:
    first = harness.add("ch-a", "https://x.invalid/1", "alpha from a")
    second = harness.add("ch-b", "https://x.invalid/2", "alpha from b")
    harness.add("ch-c", "https://x.invalid/3", "alpha from c")
    harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "ai", "ch-c": "garden"})

    assert harness.index.search(SearchQuery(text="alpha")).total == 3
    assert harness.index.search(SearchQuery(text="alpha", channels=("ch-a",))).total == 1
    assert harness.index.search(SearchQuery(text="alpha", channels=("ch-a", "ch-b"))).total == 2
    assert harness.index.search(SearchQuery(text="alpha", industries=("ai",))).total == 2
    assert harness.index.search(SearchQuery(text="alpha", industries=("garden",))).total == 1
    assert harness.index.search(SearchQuery(text="alpha", industries=("nope",))).total == 0
    single = harness.index.search(SearchQuery(text="alpha", raw_ids=(first.raw_id,)))
    assert [hit.raw_id for hit in single.items] == [first.raw_id]
    assert single.total == 1
    both = harness.index.search(SearchQuery(text="alpha", raw_ids=(first.raw_id, second.raw_id)))
    assert both.total == 2


def test_industry_filter_fails_loudly_when_mapping_was_not_injected(harness: Harness) -> None:
    """§2.5 警告的"能跑但闭环断开"必须响亮失败，而不是静默全空。"""
    seed(harness)
    report = harness.rebuild()
    assert report.industry_source == INDUSTRY_SOURCE_NONE
    assert harness.index.meta()["industry_source"] == INDUSTRY_SOURCE_NONE

    with pytest.raises(SearchIndexError) as info:
        harness.index.search(SearchQuery(text="bm25", industries=("ai",)))
    assert "industry_of" in str(info.value)
    # 不做行业筛选时照常可用
    assert harness.index.search(SearchQuery(text="bm25")).total == 2


def test_unmapped_channel_has_no_industry_and_never_matches(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha")
    unmapped = harness.add("ch-z", "https://x.invalid/2", "alpha")
    harness.rebuild(industry_of={"ch-a": "ai"})  # ch-z 未映射
    filtered = harness.index.search(SearchQuery(text="alpha", industries=("ai",)))
    assert filtered.total == 1
    assert harness.index.search(SearchQuery(text="alpha")).total == 2
    assert harness.index.get(unmapped.raw_id).industry is None
    assert harness.index.get(unmapped.raw_id).raw_id == unmapped.raw_id


def test_time_range_filter(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha", fetched_at=BASE)
    harness.add("ch-a", "https://x.invalid/2", "alpha", fetched_at=BASE + timedelta(days=1))
    harness.add("ch-a", "https://x.invalid/3", "alpha", fetched_at=BASE + timedelta(days=2))
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="alpha")).total == 3
    assert harness.index.search(SearchQuery(text="alpha", since=BASE + timedelta(days=1))).total == 2
    assert harness.index.search(SearchQuery(text="alpha", until=BASE + timedelta(days=1))).total == 2
    window = SearchQuery(
        text="alpha", since=BASE + timedelta(days=1), until=BASE + timedelta(days=1)
    )
    assert harness.index.search(window).total == 1


def test_time_filter_handles_offsets_and_naive_records(harness: Harness) -> None:
    """存储层把时间统一成定宽 UTC 串，因此混合偏移不会排错序、筛错范围。

    `RawRecord.fetched_at` 契约上不禁止 naive 时间（T-103 直接 `isoformat()` 落库），
    而 naive 串与带偏移的串**字符串不可比**——索引层因此统一规范化。
    """
    plus_two = timezone(timedelta(hours=2))
    harness.add("ch-a", "https://x.invalid/1", "alpha", fetched_at=BASE)
    harness.add("ch-a", "https://x.invalid/2", "alpha", fetched_at=BASE + timedelta(hours=1))
    naive = harness.add(
        "ch-a", "https://x.invalid/3", "alpha", fetched_at=BASE.replace(tzinfo=None)
    )
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="alpha")).total == 3

    # naive 记录按 UTC 解释（与 T-106 feed 同一条规则）
    stored = harness.index.get(naive.raw_id)
    assert stored is not None
    assert stored.fetched_at == BASE
    assert stored.fetched_at.tzinfo is not None

    # 用 +02:00 表达同一个绝对时刻，范围筛选结果必须一致
    same_instant = SearchQuery(text="alpha", since=BASE.astimezone(plus_two))
    assert harness.index.search(same_instant).total == 3
    cutoff = SearchQuery(text="alpha", since=(BASE + timedelta(minutes=30)).astimezone(plus_two))
    assert harness.index.search(cutoff).total == 1

    # 时间排序也必须是"按时刻"而不是"按字符串"
    ordered_ids = [
        hit.raw_id
        for hit in harness.index.search(
            SearchQuery(text="alpha", order="recency", limit=10)
        ).items
    ]
    # BASE+1h 最新排第一；两个 BASE 的记录并列，由 raw_id 兜底
    assert ordered_ids[0] != naive.raw_id
    assert naive.raw_id in ordered_ids[-2:]


def test_paging_is_gapless_and_non_overlapping(harness: Harness) -> None:
    for index in range(25):
        harness.add(
            "ch-a",
            f"https://x.invalid/{index}",
            f"alpha document number {index}",
            fetched_at=BASE + timedelta(minutes=index),
        )
    harness.rebuild()
    full_result = harness.index.search(SearchQuery(text="alpha", limit=200))
    assert full_result.total == 25

    seen: list[str] = []
    offset = 0
    while True:
        page = harness.index.search(SearchQuery(text="alpha", limit=7, offset=offset))
        seen.extend(hit.raw_id for hit in page.items)
        if not page.has_more:
            assert page.next_offset is None
            break
        assert page.next_offset == offset + len(page.items)
        offset = page.next_offset
    assert seen == [hit.raw_id for hit in full_result.items]
    assert len(set(seen)) == 25


def test_paging_beyond_the_end_returns_empty_page(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    page = harness.index.search(SearchQuery(text="bm25", limit=5, offset=500))
    assert page.items == ()
    assert page.total == 2
    assert page.has_more is False
    assert page.next_offset is None


# --------------------------------------------------------------------------- #
# get(raw_id)：按 raw_id 取单条
# --------------------------------------------------------------------------- #
def test_get_returns_the_indexed_projection(harness: Harness) -> None:
    record = harness.add("ch-a", "https://x.invalid/1", "alpha beta")
    harness.rebuild(industry_of={"ch-a": "ai"})
    document = harness.index.get(record.raw_id)
    assert document is not None
    assert document.raw_id == record.raw_id
    assert document.channel_id == "ch-a"
    assert document.industry == "ai"
    assert document.text == "alpha beta"
    assert document.text_length == len("alpha beta")
    assert document.content_sha256 == record.content_sha256
    assert document.fetched_at == record.fetched_at
    assert document.http_status == 200
    assert len(document.text_sha256) == 64


def test_get_returns_none_for_unknown_raw_id(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    assert harness.index.get("raw_does_not_exist") is None


def test_get_rejects_empty_raw_id(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    with pytest.raises(SearchQueryError):
        harness.index.get("")


# --------------------------------------------------------------------------- #
# 打分可组合（为 T-201 留的位置）：boost
# --------------------------------------------------------------------------- #
def test_boost_can_promote_a_document_outside_the_top_window(harness: Harness) -> None:
    """boost 拿到**全量候选**，因此能把排在窗口之外的文档提到第一位。"""
    for index in range(6):
        harness.add(
            "ch-a",
            f"https://x.invalid/{index}",
            "alpha " + " ".join(["filler"] * (index + 1)),
            fetched_at=BASE + timedelta(minutes=index),
        )
    # 这一篇正文极长 ⇒ BM25 分数最低，按 FTS 排序一定落在最后
    harness.add(
        "ch-b",
        "https://x.invalid/boost",
        "alpha " + " ".join(["noise"] * 200),
        fetched_at=BASE,
    )
    harness.rebuild(industry_of={"ch-a": "ai", "ch-b": "boosted"})
    boost_target = harness.index.search(
        SearchQuery(text="alpha", industries=("boosted",), limit=1)
    ).items[0].raw_id

    baseline = harness.index.search(SearchQuery(text="alpha", limit=1))
    assert baseline.items[0].raw_id != boost_target
    worst = harness.index.search(SearchQuery(text="alpha", limit=100)).items[-1]
    assert worst.raw_id == boost_target

    boosted = harness.index.search(
        SearchQuery(text="alpha", limit=1),
        boost=lambda hit: 1000.0 if hit.raw_id == boost_target else 0.0,
    )
    assert boosted.boosted is True
    assert boosted.items[0].raw_id == boost_target
    assert boosted.candidates == 7  # 全量候选，不是只重排本页
    assert boosted.total == 7  # total 不受排序方式影响


def test_zero_boost_reproduces_the_pure_sql_order(harness: Harness) -> None:
    """boost 恒为 0 时必须与不 boost 完全同序——两条排序路径共用同一套键。"""
    for index in range(8):
        harness.add(
            "ch-a",
            f"https://x.invalid/{index}",
            "alpha " + " ".join(["filler"] * (index + 1)),
            fetched_at=BASE + timedelta(minutes=index),
        )
    harness.rebuild()
    plain = harness.index.search(SearchQuery(text="alpha", limit=8))
    boosted = harness.index.search(SearchQuery(text="alpha", limit=8), boost=lambda hit: 0.0)
    assert [hit.raw_id for hit in plain.items] == [hit.raw_id for hit in boosted.items]
    assert [hit.score for hit in plain.items] == [hit.score for hit in boosted.items]


def test_boost_is_composable_with_the_fts_score(harness: Harness) -> None:
    """boost 拿到 `hit.score`（FTS 分量），因此调用方可做任意线性组合。"""
    harness.add("ch-a", "https://x.invalid/1", "alpha alpha alpha")
    harness.add("ch-a", "https://x.invalid/2", "alpha")
    harness.rebuild()
    halved = harness.index.search(
        SearchQuery(text="alpha", limit=2), boost=lambda hit: hit.score * 0.5
    )
    plain = harness.index.search(SearchQuery(text="alpha", limit=2))
    assert halved.items[0].score == pytest.approx(plain.items[0].score * 1.5)
    for hit in halved.items:
        assert hit.fts_score * 1.5 == pytest.approx(hit.score)


def test_boost_recency_order_also_applies(harness: Harness) -> None:
    harness.add("ch-a", "https://x.invalid/1", "alpha", fetched_at=BASE)
    harness.add("ch-a", "https://x.invalid/2", "alpha", fetched_at=BASE + timedelta(days=1))
    harness.rebuild()
    result = harness.index.search(
        SearchQuery(text="alpha", order="recency", limit=5), boost=lambda hit: 0.0
    )
    times = [hit.fetched_at for hit in result.items]
    assert times == sorted(times, reverse=True)


# --------------------------------------------------------------------------- #
# A3（线程部分）：单个实例跨线程可用
# --------------------------------------------------------------------------- #
def test_single_instance_is_usable_from_many_threads(harness: Harness) -> None:
    """`ThreadingHTTPServer` 形态：主线程建实例，别的线程并发查询（T-103 的坑不再重演）。"""
    for index in range(20):
        harness.add("ch-a", f"https://x.invalid/{index}", f"alpha document {index}")
    harness.rebuild()
    reference = full(harness.index.search(SearchQuery(text="alpha", limit=20)))

    barrier = threading.Barrier(8)
    problems: list[str] = []

    def reader() -> None:
        barrier.wait()
        for _ in range(10):
            observed = full(harness.index.search(SearchQuery(text="alpha", limit=20)))
            if observed != reference:
                problems.append("并发查询结果与单线程不一致")
            if harness.index.get(harness.archive.all_raw_ids()[0]) is None:
                problems.append("并发 get 返回 None")
            if harness.index.count() != 20:
                problems.append("并发 count 数目不对")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reader) for _ in range(8)]
        for future in futures:
            future.result()
    assert problems == []


def test_rebuild_from_another_thread_works(harness: Harness) -> None:
    seed(harness)
    harness.rebuild()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            harness.index.rebuild(ArchiveDocumentSource(harness.archive).iter_documents())
            harness.index.search(SearchQuery(text="bm25"))
        except BaseException as exc:  # noqa: BLE001 - 要断言"什么都不抛"
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == [], f"跨线程重建/查询抛错：{errors!r}"
    assert harness.index.count() == 4


def test_connection_is_opened_without_thread_affinity(harness: Harness) -> None:
    """直接证明 `check_same_thread=False` + busy_timeout 已生效（缺陷的最直接形态）。"""
    assert harness.index.connection.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    errors: list[BaseException] = []

    def query() -> None:
        try:
            harness.index.connection.execute(f"SELECT COUNT(*) FROM {DOCS_TABLE}").fetchone()
            harness.index.connection.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=query)
    thread.start()
    thread.join()
    assert errors == []
