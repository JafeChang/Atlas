"""T-205 验收判据（**先于实现写定**，实现见 `src/atlas/search/`）

本文件与 `test_search_index.py` / `test_search_invariants.py` / `test_search_realdata.py`
共同构成 T-205 的验收。判据如下（每条都有对应测试）：

A1 **FTS5 可用性**：启动时必须探测当前 SQLite 是否编译 FTS5，并验证本项目使用的分词器
   配置被接受；不可用 → `Fts5UnavailableError`（明确异常 + 清晰信息）。
   **不得**静默降级成 `LIKE`/`GLOB` 假装是全文检索（静态钉死：包内非 docstring 字符串
   字面量里不得出现 `LIKE`/`GLOB`）。探测不得掩盖其它接线错误：非 "no such module" 的
   `OperationalError` 必须原样上抛。

A2 **可全量重建**：索引可删除，并可由 raw（经 T-104 归一化）全量重建；`drop()` 后
   `rebuild()`，同一批查询的命中 raw_id 顺序、条数、score、snippet 必须完全相同；
   在两个不同库文件里独立重建结果同样必须相同。索引身份落盘
   （`schema_version`/`index_version`/`tokenizer`/`text_source`/`document_count`/
   `corpus_sha256`/`industry_source`/`built_at`）；同一语料 → 同一 `corpus_sha256`；
   `index_version` 与代码不符时**读路径响亮失败**，并给出"删索引后重建"的补救路径。

A3 **只读投影**：索引层不得出现任何写入 raw / Proposed / Confirmed 的路径。
   静态：包内不得 import 其它任务的实现包、不得出现上游事实表名、写语句目标表必须
   以 `search_` 开头。行为：建索引 + 查询 + drop 前后，`raw_records` 行、raw 字节目录、
   归档 `verify()`、以及同库内其它域的表（哨兵行）必须完全一致；
   并用 SQLite authorizer 实测"索引执行的全部 SQL 只碰 `search_*` 表"。

A4 **排序确定性**：`relevance` = `(score desc, fetched_at desc, raw_id asc)`，
   `recency` = `(fetched_at desc, raw_id asc)`，`score = -bm25(...)`（越大越相关）；
   `raw_id` 唯一 ⇒ 全序。同一查询连续两次、以及重建前后顺序必须相同；
   分数并列时顺序必须由次级键决定（用同长度同词频的语料构造真并列）。

A5 **查询健壮性**：用户输入**永不**作为 FTS5 表达式使用——先切词、去重、逐词加引号、
   以 `AND` 连接；操作符（`"` `*` `-` `:` `NEAR` `AND` `OR` `NOT` `^` `( )`）只按字面词
   处理。任何用户输入都不得让 `sqlite3.Error` 穿透到调用方。切词后为空 → `EmptyQueryError`
   （明确错误，不静默全空）；合法但无命中 → `total=0` 的明确空结果（不是错误）。
   超长文本 / 词元过多 → 明确拒绝，不截断。

A6 **越界参数**：`limit ∈ [1, 200]`（默认 50），越界**拒绝不截断**（与 SPEC §2.13 的
   T-106 裁决一致）；`offset ≥ 0`；`snippet_tokens ∈ [1, 64]`；筛选值非空且 ≤ 200 个；
   `since`/`until` 必须带时区且 `since ≤ until`；未知 `order` 拒绝。
   分页不得重不漏。

A7 **零新依赖**：只用标准库与已提交的 `atlas.contracts` / `atlas.archive` /
   `atlas.normalize`（后两者只读借用）；`pyproject.toml` 不改。

A8 **门禁**：`pytest` 全绿（含既有 813 条）；提交后用独立 worktree 复核同一 commit。

本文件覆盖 A5 / A6 / A4 的纯逻辑部分（零 I/O）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from atlas.search import (
    DEFAULT_LIMIT,
    DEFAULT_SNIPPET_TOKENS,
    MAX_FILTER_VALUES,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_QUERY_TERMS,
    MAX_SNIPPET_TOKENS,
    ORDER_RECENCY,
    ORDER_RELEVANCE,
    EmptyQueryError,
    SearchHit,
    SearchQuery,
    SearchQueryError,
    apply_boost,
    canonical_utc_iso,
    match_expression,
    parse_canonical_utc,
    rank_hits,
    tokenize,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# A5：关键词 → 安全表达式
# --------------------------------------------------------------------------- #
def test_tokenize_splits_on_non_alphanumeric_and_drops_underscore() -> None:
    """切词规则必须与 unicode61 的分隔规则一致：非字母数字（含 `_`）都是分隔符。"""
    assert tokenize("foo-bar") == ("foo", "bar")
    assert tokenize("snake_case_name") == ("snake", "case", "name")
    assert tokenize("C++ 17") == ("C", "17")
    assert tokenize("混合 text 与中文") == ("混合", "text", "与中文")
    assert tokenize("café") == ("café",)


def test_tokenize_dedupes_preserving_order() -> None:
    assert tokenize("beta alpha beta") == ("beta", "alpha")


def test_match_expression_contains_only_literals_and_and() -> None:
    """表达式里只允许出现带引号的字面量与 `AND`（没有别的操作符）。"""
    expression = match_expression(("foo", "bar"))
    assert expression == '"foo" AND "bar"'
    assert SearchQuery(text="foo bar").match_expression() == expression
    assert SearchQuery(text="foo-bar").terms() == ("foo", "bar")


def test_quote_escaping_is_defensive() -> None:
    """切词规则不产出引号，但逃逸仍按 FTS5 规则双写（纵深防御）。"""
    assert match_expression(('a"b',)) == '"a""b"'


@pytest.mark.parametrize(
    "text,expected_terms",
    [
        ('"quoted"', ("quoted",)),
        ("*", ()),
        ("-", ()),
        (":", ()),
        ("^", ()),
        ("()", ()),
        ("NEAR", ("NEAR",)),
        ("AND", ("AND",)),
        ("OR", ("OR",)),
        ("NOT", ("NOT",)),
        ("a AND", ("a", "AND")),
        ("a OR b", ("a", "OR", "b")),
        ("NEAR(a b)", ("NEAR", "a", "b")),
        ("col:value", ("col", "value")),
        ("foo*bar", ("foo", "bar")),
        ("a+b-c", ("a", "b", "c")),
        ('x" OR "y', ("x", "OR", "y")),
        ("100%", ("100",)),
        ("\\", ()),
        ("[]{}", ()),
        ("...", ()),
    ],
)
def test_operator_input_becomes_literal_terms(text: str, expected_terms: tuple) -> None:
    """操作符**不具备语法意义**：只按字面词处理（判据 A5 的核心）。"""
    if not expected_terms:
        with pytest.raises(EmptyQueryError):
            SearchQuery(text=text)
        return
    query = SearchQuery(text=text)
    assert query.terms() == expected_terms
    for term in query.terms():
        assert f'"{term}"' in query.match_expression()
    # 表达式里除了引号、字母、空格与 AND 之外没有别的字符
    assert set(query.match_expression()) <= set('" AND') | set(
        "".join(expected_terms)
    )


# --------------------------------------------------------------------------- #
# A5：空查询必须明确报错
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["", "   ", "\t\n", "!!!", "***", "---", ":::()"])
def test_empty_query_is_explicit_error(text: str) -> None:
    with pytest.raises(EmptyQueryError) as info:
        SearchQuery(text=text)
    assert "全空" in str(info.value)


def test_empty_query_error_is_a_value_error_for_http_layers() -> None:
    """与 `atlas.feed.query.InvalidQueryError` 一致：HTTP 层"ValueError → 400"可直接复用。"""
    assert issubclass(EmptyQueryError, SearchQueryError)
    assert issubclass(SearchQueryError, ValueError)


def test_non_string_text_rejected() -> None:
    with pytest.raises(SearchQueryError):
        SearchQuery(text=b"bytes")  # type: ignore[arg-type]


def test_text_length_cap_is_rejected_not_truncated() -> None:
    long_text = "a" * (MAX_QUERY_LENGTH + 1)
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text=long_text)
    assert "长度" in str(info.value)
    # 边界内可用
    assert len(SearchQuery(text="a" * MAX_QUERY_LENGTH).text) == MAX_QUERY_LENGTH


def test_term_cap_is_rejected_not_truncated() -> None:
    text = " ".join(f"t{index}" for index in range(MAX_QUERY_TERMS + 1))
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text=text)
    assert "词元" in str(info.value)
    just_inside = " ".join(f"t{index}" for index in range(MAX_QUERY_TERMS))
    assert len(SearchQuery(text=just_inside).terms()) == MAX_QUERY_TERMS


def test_repeated_terms_do_not_inflate_the_term_count() -> None:
    """去重发生在计数之前：重复同一个词不算"词元过多"。"""
    assert SearchQuery(text=" ".join(["same"] * 500)).terms() == ("same",)


# --------------------------------------------------------------------------- #
# A6：越界参数一律拒绝
# --------------------------------------------------------------------------- #
def test_limit_defaults_and_bounds() -> None:
    assert SearchQuery(text="x").limit == DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 200
    assert SearchQuery(text="x", limit=1).limit == 1
    assert SearchQuery(text="x", limit=MAX_LIMIT).limit == MAX_LIMIT
    for bad in (0, -1, MAX_LIMIT + 1, 10_000):
        with pytest.raises(SearchQueryError) as info:
            SearchQuery(text="x", limit=bad)
        assert "limit" in str(info.value)


def test_limit_must_be_int_and_bool_is_rejected() -> None:
    for bad in ("10", 10.0, None, True):
        with pytest.raises(SearchQueryError):
            SearchQuery(text="x", limit=bad)  # type: ignore[arg-type]


def test_offset_bounds() -> None:
    assert SearchQuery(text="x", offset=0).offset == 0
    assert SearchQuery(text="x", offset=10_000).offset == 10_000
    for bad in (-1, "0", None, False):
        with pytest.raises(SearchQueryError):
            SearchQuery(text="x", offset=bad)  # type: ignore[arg-type]


def test_snippet_token_bounds() -> None:
    assert SearchQuery(text="x").snippet_tokens == DEFAULT_SNIPPET_TOKENS == 32
    assert MAX_SNIPPET_TOKENS == 64
    assert SearchQuery(text="x", snippet_tokens=MAX_SNIPPET_TOKENS).snippet_tokens == 64
    for bad in (0, -3, MAX_SNIPPET_TOKENS + 1):
        with pytest.raises(SearchQueryError):
            SearchQuery(text="x", snippet_tokens=bad)


def test_order_validation() -> None:
    assert SearchQuery(text="x").order == ORDER_RELEVANCE
    assert SearchQuery(text="x", order=ORDER_RECENCY).order == ORDER_RECENCY
    for bad in ("score", "RELEVANCE", "asc", "", None):
        with pytest.raises(SearchQueryError):
            SearchQuery(text="x", order=bad)  # type: ignore[arg-type]


def test_filter_values_are_cleaned_and_deduped() -> None:
    query = SearchQuery(text="x", channels=(" ch-1 ", "ch-1", "ch-2"))
    assert query.channels == ("ch-1", "ch-2")
    assert SearchQuery(text="x", industries=("ai",)).industries == ("ai",)
    assert SearchQuery(text="x", raw_ids=("raw_a",)).raw_ids == ("raw_a",)


def test_filter_values_reject_empty_and_non_string() -> None:
    for bad in ("", "   "):
        with pytest.raises(SearchQueryError) as info:
            SearchQuery(text="x", channels=(bad,))
        assert "空值" in str(info.value)
    with pytest.raises(SearchQueryError):
        SearchQuery(text="x", channels=(1,))  # type: ignore[arg-type]
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text="x", channels="ch-1")  # type: ignore[arg-type]
    assert "序列" in str(info.value)


def test_filter_value_count_is_rejected_not_truncated() -> None:
    values = tuple(f"ch-{index}" for index in range(MAX_FILTER_VALUES + 1))
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text="x", channels=values)
    assert str(MAX_FILTER_VALUES) in str(info.value)
    inside = tuple(f"ch-{index}" for index in range(MAX_FILTER_VALUES))
    assert len(SearchQuery(text="x", channels=inside).channels) == MAX_FILTER_VALUES


def test_time_bounds_require_timezone() -> None:
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text="x", since=datetime(2026, 1, 1))
    assert "时区" in str(info.value)
    with pytest.raises(SearchQueryError):
        SearchQuery(text="x", until=datetime(2026, 1, 1))
    with pytest.raises(SearchQueryError):
        SearchQuery(text="x", since="2026-01-01T00:00:00+00:00")  # type: ignore[arg-type]


def test_since_must_not_be_after_until() -> None:
    with pytest.raises(SearchQueryError) as info:
        SearchQuery(text="x", since=BASE + timedelta(days=1), until=BASE)
    assert "不得晚于" in str(info.value)
    SearchQuery(text="x", since=BASE, until=BASE)  # 相等合法


# --------------------------------------------------------------------------- #
# A4：时间键与排序键
# --------------------------------------------------------------------------- #
def test_canonical_utc_is_fixed_width_and_round_trips() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    text = canonical_utc_iso(moment)
    assert text == "2026-01-02T03:04:05.678901+00:00"
    assert parse_canonical_utc(text) == moment
    assert len(text) == len(canonical_utc_iso(datetime(2030, 12, 31, tzinfo=UTC)))


def test_canonical_utc_normalises_offsets_and_treats_naive_as_utc() -> None:
    """字符串比较必须等价于时间比较：先统一到 UTC 再定宽格式化。"""
    plus_two = timezone(timedelta(hours=2))
    assert canonical_utc_iso(datetime(2026, 1, 1, 2, 0, 0, tzinfo=plus_two)) == (
        "2026-01-01T00:00:00.000000+00:00"
    )
    assert canonical_utc_iso(datetime(2026, 1, 1, 0, 0, 0)) == (
        "2026-01-01T00:00:00.000000+00:00"
    )
    # 时间靠后的串在字典序上也靠后
    earlier = canonical_utc_iso(datetime(2026, 1, 1, tzinfo=UTC))
    later = canonical_utc_iso(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=UTC))
    assert earlier < later


def test_sort_description_is_the_contract() -> None:
    relevance = SearchQuery(text="x").sort_description()
    assert relevance["keys"] == ["score desc", "fetched_at desc", "raw_id asc"]
    assert relevance["score"] == "-bm25(search_documents_fts)"
    assert relevance["tie_break"] == "raw_id asc"

    recency = SearchQuery(text="x", order=ORDER_RECENCY).sort_description()
    assert recency["keys"] == ["fetched_at desc", "raw_id asc"]
    assert recency["score"] is None


# --------------------------------------------------------------------------- #
# A4：纯函数排序（boost 组合路径复用同一套键）
# --------------------------------------------------------------------------- #
def make_hit(
    raw_id: str,
    *,
    score: float = 1.0,
    fetched_at: datetime = BASE,
    snippet: str = "",
) -> SearchHit:
    return SearchHit(
        raw_id=raw_id,
        channel_id="ch",
        endpoint=f"https://example.invalid/{raw_id}",
        industry=None,
        content_sha256="0" * 64,
        byte_length=1,
        fetched_at=fetched_at,
        http_status=200,
        score=score,
        fts_score=score,
        snippet=snippet,
    )


def test_rank_hits_relevance_is_total_order() -> None:
    hits = [
        make_hit("raw_c", score=1.0, fetched_at=BASE),
        make_hit("raw_b", score=2.0, fetched_at=BASE),  # 同分不同时间
        make_hit("raw_a", score=2.0, fetched_at=BASE + timedelta(minutes=1)),
        make_hit("raw_d", score=2.0, fetched_at=BASE + timedelta(minutes=1)),
        make_hit("raw_e", score=9.0, fetched_at=BASE - timedelta(days=1)),
    ]
    query = SearchQuery(text="x")
    ordered = [hit.raw_id for hit in rank_hits(hits, query)]
    # score 降序 → fetched_at 降序 → raw_id 升序
    assert ordered == ["raw_e", "raw_a", "raw_d", "raw_b", "raw_c"]
    # 输入顺序不影响输出（全序，不依赖稳定排序的偶然性）
    assert [hit.raw_id for hit in rank_hits(list(reversed(hits)), query)] == ordered


def test_rank_hits_recency_ignores_score() -> None:
    hits = [
        make_hit("raw_b", score=1.0, fetched_at=BASE + timedelta(minutes=2)),
        make_hit("raw_a", score=99.0, fetched_at=BASE),
        make_hit("raw_c", score=50.0, fetched_at=BASE + timedelta(minutes=2)),
    ]
    ordered = [hit.raw_id for hit in rank_hits(hits, SearchQuery(text="x", order=ORDER_RECENCY))]
    assert ordered == ["raw_b", "raw_c", "raw_a"]


def test_apply_boost_adds_extra_score() -> None:
    hits = [make_hit("raw_a", score=1.5), make_hit("raw_b", score=2.0)]
    boosted = apply_boost(hits, lambda hit: 10.0 if hit.raw_id == "raw_a" else 0.0)
    assert [hit.score for hit in boosted] == [11.5, 2.0]
    # 原始命中不被就地修改
    assert [hit.score for hit in hits] == [1.5, 2.0]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_apply_boost_rejects_non_finite(bad: float) -> None:
    """NaN / inf 会让排序静默失去全序，必须响亮拒绝（判据 A4）。"""
    with pytest.raises(SearchQueryError) as info:
        apply_boost([make_hit("raw_a")], lambda hit: bad)
    assert "有限实数" in str(info.value)


@pytest.mark.parametrize("bad", ["1.0", None, True])
def test_apply_boost_rejects_non_numbers(bad: object) -> None:
    with pytest.raises(SearchQueryError):
        apply_boost([make_hit("raw_a")], lambda hit: bad)  # type: ignore[return-value]


def test_replacing_a_hit_keeps_frozenity() -> None:
    hit = make_hit("raw_a", score=1.0)
    other = replace(hit, score=2.0)
    assert other.score == 2.0 and hit.score == 1.0
