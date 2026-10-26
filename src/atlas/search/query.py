"""T-205 检索查询模型：关键词 → 安全 FTS5 表达式、筛选、排序、分页（**纯逻辑，零 I/O**）。

本模块是"排序确定性"与"查询健壮性"两条判据（A4 / A5 / A6）的落点。

用户输入**永不**作为 FTS5 表达式（判据 A5）
-------------------------------------------

FTS5 的 `MATCH` 语法自带操作符（`"` 短语、`*` 前缀、`-`/`NOT` 排除、`NEAR`、
`:` 列限定、`^` 行首、`( )` 分组、`AND`/`OR`）。把用户输入直接拼进 `MATCH`
有三种坏结果：语法错误穿透到调用方（500）、语义被劫持（用户以为在搜索，
实际在构造查询）、以及"某些输入静默全空"。

本模块的做法是**先切词、再去重、再逐组加双引号、最后以 `AND` 连接**：

    用户输入  C++ build "quoted" -NEAR*  →  词元 («C», «build», «quoted», «NEAR»)
                                         →  "C" AND "build" AND "quoted" AND "NEAR"

操作符因此**不具备语法意义**，只按字面词处理；引号由本模块生成，
且词元内不可能含引号（切词规则只产出 `[^\\W_]+`），逃逸是纵深防御。
切词后为空（空串 / 纯空白 / 纯操作符）→ `EmptyQueryError`（明确错误，
不是静默全空）。

汉字：同一套切分 + **短语**，不是逐字 `AND`（T-205 修订）
-------------------------------------------------------

索引侧的汉字是**逐字切开**后再索引的（`atlas.search.cjk.segment_cjk`），
因此查询侧必须用同一套切分。但"逐字切分"不能直接变成"逐字 `AND`"：

    "人" AND "工" AND "智" AND "能"   会命中"世界**人**民**工**作**智**慧**能**力"

连续汉字必须变成 FTS5 **短语**（短语只匹配**连续出现**的汉字串）：

    中文分词测试  →  "中 文 分 词 测 试"

分组规则（`phrase_groups`）：把查询里**连续的汉字**归为一个短语组，其它按
切词规则切成单字词元组；组间 `AND`。用户在汉字之间打了空白就是两个组
（`中文 分词` → `"中 文" AND "分 词"`），因为那本来就是两个词。

**仍然有意不做**的用户级操作符语义：引号短语（`"a b"`）、前缀（`a*`）、排除（`-a`）、
`NEAR`。它们需要"解析用户意图"，而本任务是"用户输入即文本"。汉字短语是**切分规则的
产物**（同一个切分函数在两侧的使用），不是"解析用户写的引号"。

切词规则与索引侧的分词器对齐
----------------------------

索引用 FTS5 `unicode61 remove_diacritics 2`。该分词器把"非字母数字"（含 `_`）
当分隔符，并对字母做变音符号折叠。本模块的切词用 `[^\\W_]+`（Unicode 感知、
排除下划线）近似同一分隔规则，于是"切出来的词"正好是"索引里的词"：
`foo-bar` → `"foo" AND "bar"` 而不是 `"foo-bar"` 这样的短语。
汉字部分先过 `segment_cjk`（与索引侧**同一个函数**），于是汉字也满足
"切出来的词正好是索引里的词"。

排序键与全序（判据 A4）
----------------------

| `order` | 排序键 |
|---|---|
| `relevance`（默认） | `score desc, fetched_at desc, raw_id asc` |
| `recency` | `fetched_at desc, raw_id asc` |

`score = -bm25(...)`（FTS5 的 `bm25()` 越相关越负，取负后**越大越相关**）。
`raw_id` 唯一 ⇒ 排序键是全序 ⇒ 同一查询两次、以及重建前后，顺序必须逐条相同。
`fetched_at` 并列时进一步由 `raw_id asc` 决定，与 T-106 feed 的并列处理同构。

时间键：索引内一律存**定宽 UTC ISO 串**
----------------------------------------

`fetched_at` 在索引里存成 `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`（`canonical_utc_iso`）。
定宽 + 同一偏移 ⇒ 字符串比较即时间比较，可以直接在 SQL 里做范围筛选与排序，
并且**不依赖 Python 侧的解析**。naive 时间按 UTC 解释（与 T-106 `_ensure_aware`
同一条写死的规则，不做"看情况"处理）。

越界参数一律拒绝（判据 A6）
--------------------------

`limit ∈ [1, 200]`（默认 50）与 SPEC §2.13 对 T-106 的裁决一致：**拒绝不截断**。
`offset ≥ 0`、`snippet_tokens ∈ [1, 64]`、筛选值非空且 ≤ 200 个、
查询词 ≤ 64 个、查询文本 ≤ 4096 字符——全部显式拒绝，不做静默截断。

> ⚠️ `MAX_QUERY_TERMS = 64` 计的是**词元**；汉字逐字切分之后一个汉字就是一个词元，
> 因此**中文查询的上限是 64 个汉字**（超出仍然**拒绝**而不是截断）。这是本修订对
> 契约的直接影响，已在交付报告里单独列出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Sequence, Tuple, Union

from .cjk import is_inserted_space, segment_cjk
from .errors import EmptyQueryError, SearchQueryError

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_SNIPPET_TOKENS",
    "HIGHLIGHT_CLOSE",
    "HIGHLIGHT_OPEN",
    "MAX_FILTER_VALUES",
    "MAX_LIMIT",
    "MAX_QUERY_LENGTH",
    "MAX_QUERY_TERMS",
    "MAX_SNIPPET_TOKENS",
    "ORDER_RECENCY",
    "ORDER_RELEVANCE",
    "ORDERS",
    "SNIPPET_ELLIPSIS",
    "Boost",
    "Group",
    "SearchHit",
    "SearchQuery",
    "SearchResult",
    "canonical_utc_iso",
    "match_expression",
    "parse_canonical_utc",
    "phrase_groups",
    "rank_hits",
    "tokenize",
]

# --------------------------------------------------------------------------- #
# 契约值（对外可观测，改动需按 SPEC 的契约变更流程走）
# --------------------------------------------------------------------------- #

#: 默认页大小（与 T-106 的裁决一致）。
DEFAULT_LIMIT = 50
#: 页大小上限：越界**拒绝**而不是截断（SPEC §2.13）。
MAX_LIMIT = 200
#: 单个筛选维度允许的取值个数上限（超出拒绝）。
MAX_FILTER_VALUES = 200
#: 查询文本长度上限（字符；超出拒绝，不截断）。
MAX_QUERY_LENGTH = 4096
#: 查询词元个数上限（超出拒绝）。深度过大的 `AND` 树会撞 SQLite 的表达式深度上限，
#: 因此这条既是可用性约束，也是"不让 sqlite3 原始异常穿透"的防线。
MAX_QUERY_TERMS = 64
#: 摘要片段默认词元数。
DEFAULT_SNIPPET_TOKENS = 32
#: 摘要片段词元数上限。
MAX_SNIPPET_TOKENS = 64

#: 高亮标记（固定契约值：调用方按这两个常量渲染，不猜）。
HIGHLIGHT_OPEN = "["
HIGHLIGHT_CLOSE = "]"
#: 摘要片段被截断时的省略号。
SNIPPET_ELLIPSIS = "…"

ORDER_RELEVANCE = "relevance"
ORDER_RECENCY = "recency"
ORDERS = (ORDER_RELEVANCE, ORDER_RECENCY)

#: 排序键描述（`sort_description()` 与测试共用同一份定义）。
RELEVANCE_KEYS = ("score desc", "fetched_at desc", "raw_id asc")
RECENCY_KEYS = ("fetched_at desc", "raw_id asc")

#: 切词规则：Unicode 字母数字序列，**排除下划线**（unicode61 把 `_` 当分隔符）。
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: `boost`：第二个打分源。返回**附加分**（与 FTS 分数相加）。
Boost = Callable[["SearchHit"], float]

#: 一个"查询组"：由若干词元组成。长度 1 的组等价于普通词元；长度 > 1 的组是
#: **连续汉字**，会拼成 FTS5 短语（只匹配连续出现的汉字串）。
Group = Tuple[str, ...]


# --------------------------------------------------------------------------- #
# 关键词 → 安全表达式
# --------------------------------------------------------------------------- #
def phrase_groups(text: str) -> Tuple[Group, ...]:
    """把查询切成有序的组（去重保序）：连续汉字一组，其它词元各自一组。

    实现**复用索引侧同一个切分函数** `segment_cjk`：先把查询按同样的规则切分，
    再判定"相邻两个词元之间是不是只有切分器插入的那一个空格"——是则属于同一个
    汉字短语。两侧因此共用同一套"连续汉字"定义（`atlas.search.cjk.is_inserted_space`
    是唯一出处），不会出现"索引切了、查询没切"或反过来的情况。
    """
    if not isinstance(text, str):
        raise SearchQueryError("text", f"必须是 str（收到 {type(text).__name__}）")
    segmented = segment_cjk(text)
    groups: List[Group] = []
    current: List[str] = []
    previous_end = -1
    for match in _TERM_RE.finditer(segmented):
        term = match.group(0)
        contiguous_han = (
            bool(current)
            and match.start() - previous_end == 1
            and is_inserted_space(segmented, match.start() - 1)
        )
        if contiguous_han:
            current.append(term)
        else:
            if current:
                groups.append(tuple(current))
            current = [term]
        previous_end = match.end()
    if current:
        groups.append(tuple(current))

    deduped: List[Group] = []
    for group in groups:
        if group not in deduped:
            deduped.append(group)
    return tuple(deduped)


def tokenize(text: str) -> Tuple[str, ...]:
    """把用户输入切成词元（去重保序）。切不出词元时返回空元组。

    汉字逐字切分之后，**一个汉字就是一个词元**（与索引侧一致）。
    """
    seen: List[str] = []
    for group in phrase_groups(text):
        for term in group:
            if term not in seen:
                seen.append(term)
    return tuple(seen)


def _quote(term: str) -> str:
    """把词元包成 FTS5 字符串字面量；内部引号按 FTS5 规则双写逃逸。"""
    return '"' + term.replace('"', '""') + '"'


def match_expression(groups: Sequence[Union[str, Sequence[str]]]) -> str:
    """把查询组拼成**只含字符串字面量与 `AND`** 的 FTS5 表达式。

    - 长度为 1 的组（或直接给一个 `str`）→ `"term"`；
    - 长度 > 1 的组（连续汉字）→ `"中 文 分 词"`，即 FTS5 **短语**，
      只匹配连续出现的汉字串（逐字 `AND` 会命中"人民工作智慧能力"这类假阳性）。

    引号一律由本函数生成；组内词元里的引号按 FTS5 规则双写逃逸（纵深防御）。
    """
    parts: List[str] = []
    for group in groups:
        terms = (group,) if isinstance(group, str) else tuple(group)
        if not terms:
            raise SearchQueryError("terms", "查询组不得为空")
        parts.append(_quote(" ".join(terms)))
    return " AND ".join(parts)


# --------------------------------------------------------------------------- #
# 时间键
# --------------------------------------------------------------------------- #
def canonical_utc_iso(moment: datetime) -> str:
    """索引内的定宽 UTC 时间键。naive 按 UTC 解释（与 T-106 同一条规则）。"""
    if not isinstance(moment, datetime):
        raise SearchQueryError("time", f"必须是 datetime（收到 {type(moment).__name__}）")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def parse_canonical_utc(text: str) -> datetime:
    """`canonical_utc_iso` 的逆运算（读回来的永远是 aware UTC）。"""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f+00:00").replace(
        tzinfo=timezone.utc
    )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchQuery:
    """一次检索的**全部**输入。构造即校验：非法组合直接抛错（判据 A5 / A6）。"""

    text: str
    channels: Tuple[str, ...] = ()
    industries: Tuple[str, ...] = ()
    raw_ids: Tuple[str, ...] = ()
    since: datetime | None = None
    until: datetime | None = None
    order: str = ORDER_RELEVANCE
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    snippet_tokens: int = DEFAULT_SNIPPET_TOKENS

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise SearchQueryError("text", f"必须是 str（收到 {type(self.text).__name__}）")
        if len(self.text) > MAX_QUERY_LENGTH:
            raise SearchQueryError(
                "text", f"长度不得超过 {MAX_QUERY_LENGTH} 字符（收到 {len(self.text)}）"
            )
        terms = tokenize(self.text)
        if not terms:
            raise EmptyQueryError(
                "查询文本切词后为空（空串 / 纯空白 / 纯操作符）；"
                "拒绝返回全空结果，因为那与'没有命中'无法区分"
            )
        if len(terms) > MAX_QUERY_TERMS:
            raise SearchQueryError(
                "text", f"词元不得超过 {MAX_QUERY_TERMS} 个（收到 {len(terms)}）"
            )

        # 规范化筛选值（去空白、去重保序）。frozen dataclass 用 object.__setattr__ 落值，
        # 使实例**永远处于已校验状态**，而不是把清洁工作推给调用方。
        object.__setattr__(self, "channels", _clean_values(self.channels, "channel"))
        object.__setattr__(self, "industries", _clean_values(self.industries, "industry"))
        object.__setattr__(self, "raw_ids", _clean_values(self.raw_ids, "raw_id"))

        if self.order not in ORDERS:
            raise SearchQueryError(
                "order", f"只接受 {'/'.join(ORDERS)}（收到 {self.order!r}）"
            )
        _require_int(self.limit, "limit", minimum=1, maximum=MAX_LIMIT)
        _require_int(self.offset, "offset", minimum=0, maximum=None)
        _require_int(
            self.snippet_tokens,
            "snippet_tokens",
            minimum=1,
            maximum=MAX_SNIPPET_TOKENS,
        )
        for name, moment in (("since", self.since), ("until", self.until)):
            if moment is None:
                continue
            if not isinstance(moment, datetime):
                raise SearchQueryError(
                    name, f"必须是 datetime（收到 {type(moment).__name__}）"
                )
            if moment.tzinfo is None:
                raise SearchQueryError(
                    name,
                    "必须带时区偏移（例如 2026-01-01T00:00:00+00:00），"
                    "naive 时间含义不明，拒绝猜测",
                )
        if self.since is not None and self.until is not None and self.since > self.until:
            raise SearchQueryError("since", "不得晚于 until")

    # ------------------------------------------------------------------
    def terms(self) -> Tuple[str, ...]:
        """切出的词元（与构造期校验使用同一函数，结果确定）。"""
        return tokenize(self.text)

    def groups(self) -> Tuple[Group, ...]:
        """切出的查询组（连续汉字各成一组短语；组间 `AND`）。"""
        return phrase_groups(self.text)

    def match_expression(self) -> str:
        """实际下发给 FTS5 的 `MATCH` 表达式（只含字面量、短语与 `AND`）。"""
        return match_expression(self.groups())

    def sort_description(self) -> Dict[str, Any]:
        """排序契约的可审计描述（与实现同一个出处）。"""
        keys = RELEVANCE_KEYS if self.order == ORDER_RELEVANCE else RECENCY_KEYS
        return {
            "order": self.order,
            "keys": list(keys),
            "score": "-bm25(search_documents_fts)" if self.order == ORDER_RELEVANCE else None,
            "tie_break": "raw_id asc",
        }

    def filters_description(self) -> Dict[str, Any]:
        return {
            "channels": list(self.channels),
            "industries": list(self.industries),
            "raw_ids": list(self.raw_ids),
            "since": canonical_utc_iso(self.since) if self.since else None,
            "until": canonical_utc_iso(self.until) if self.until else None,
        }


def _clean_values(values: Iterable[str], parameter: str) -> Tuple[str, ...]:
    if isinstance(values, str):
        raise SearchQueryError(parameter, "必须是字符串序列，而不是单个字符串")
    cleaned: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise SearchQueryError(parameter, f"取值必须是 str（收到 {type(raw).__name__}）")
        text = raw.strip()
        if not text:
            raise SearchQueryError(parameter, "不允许空值")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_FILTER_VALUES:
        raise SearchQueryError(
            parameter, f"取值不得超过 {MAX_FILTER_VALUES} 个（收到 {len(cleaned)}）"
        )
    return tuple(cleaned)


def _require_int(value: Any, parameter: str, *, minimum: int, maximum: int | None) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SearchQueryError(parameter, f"必须是整数（收到 {value!r}）")
    if value < minimum:
        raise SearchQueryError(parameter, f"必须 ≥ {minimum}（收到 {value}）")
    if maximum is not None and value > maximum:
        raise SearchQueryError(parameter, f"必须 ≤ {maximum}（收到 {value}）")


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchHit:
    """一条命中（**只读投影**，不是事实本体）。"""

    raw_id: str
    channel_id: str
    endpoint: str
    industry: str | None
    content_sha256: str
    byte_length: int
    fetched_at: datetime
    http_status: int | None
    #: 最终分数：`fts_score`（+ 注入的 boost 附加分）。**越大越相关**。
    score: float
    #: FTS5 BM25 分量（`-bm25(...)`），单独留出以便调用方自行组合打分。
    fts_score: float
    #: 摘要片段，命中词元被 `HIGHLIGHT_OPEN` / `HIGHLIGHT_CLOSE` 包裹。
    snippet: str


@dataclass(frozen=True)
class SearchResult:
    """一页结果 + 分页与排序元数据。`total` 是**筛选后**的命中总数。"""

    query: SearchQuery
    items: Tuple[SearchHit, ...]
    total: int
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None
    #: 实际下发给 FTS5 的表达式（可审计"用户输入是如何被中性的"）。
    expression: str
    #: 是否应用了 boost（第二个打分源）。
    boosted: bool = False
    #: boost 模式下实际参与重排的候选条数（非 boost 模式等于本页条数）。
    candidates: int = 0

    def sort_description(self) -> Dict[str, Any]:
        description = self.query.sort_description()
        description["boosted"] = self.boosted
        return description


# --------------------------------------------------------------------------- #
# 排序（纯函数：boost 组合在 Python 侧复用同一套键）
# --------------------------------------------------------------------------- #
def _relevance_key(hit: SearchHit) -> Tuple[float, float, str]:
    return (-hit.score, -hit.fetched_at.timestamp(), hit.raw_id)


def _recency_key(hit: SearchHit) -> Tuple[float, str]:
    return (-hit.fetched_at.timestamp(), hit.raw_id)


def rank_hits(hits: Sequence[SearchHit], query: SearchQuery) -> Tuple[SearchHit, ...]:
    """按 `query` 的排序契约重排命中（全序，与 SQL 侧同一套键）。

    用于 `boost` 路径：先在 SQL 里按 FTS 分数取候选，再在这里用可组合的分数重排。
    """
    key = _relevance_key if query.order == ORDER_RELEVANCE else _recency_key
    return tuple(sorted(hits, key=key))


def apply_boost(hits: Sequence[SearchHit], boost: Boost) -> Tuple[SearchHit, ...]:
    """把 `boost` 的附加分加到 `score` 上，返回新的命中元组。

    boost 返回值必须是**有限实数**：NaN / inf 会让排序静默失去全序，
    因此这里响亮拒绝（判据 A4：确定性优先于宽容）。
    """
    boosted: list[SearchHit] = []
    for hit in hits:
        extra = boost(hit)
        if isinstance(extra, bool) or not isinstance(extra, (int, float)):
            raise SearchQueryError(
                "boost", f"必须返回实数（收到 {type(extra).__name__}）"
            )
        value = float(extra)
        if value != value or value in (float("inf"), float("-inf")):
            raise SearchQueryError("boost", f"必须返回有限实数（收到 {extra!r}）")
        boosted.append(replace(hit, score=hit.score + value))
    return tuple(boosted)
