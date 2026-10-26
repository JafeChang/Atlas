"""T-205 **修订**（汉字逐字切分）验收判据 —— **先于实现写定**。

背景：T-205 交付时如实记录的缺口已由用户裁决修复
--------------------------------------------------

> `unicode61` 分词器**不切分连续汉字**。查 `中文` 命不中 `中文分词测试`，
> 只有整段汉字连写才命中。（SPEC §2.15 的"已知缺口"）

裁决：**保留 `unicode61`，改为"汉字逐字切分后再索引"**（不用 FTS5 `trigram`）——
理由是索引膨胀小、英文 BM25 排序不变、零新依赖。
**第二轮裁决（切分契约 `/2`）**：缺口不止"连续汉字"，`Transformer架构` / `GPT模型` /
`向量数据库abc` 这种**汉字与拉丁直接相邻**的混写同样是正确性缺陷（实测 `架构`→0、
`模型`→0、`向量数据库`→0），因此把切分规则扩到"汉字↔字母/数字边界"。

判据 C1–C9（每条都有对应测试；实现见 `src/atlas/search/`）
----------------------------------------------------------

C1 **纯函数切分，且可逆**：`segment_cjk(text) -> str` 是纯函数（零 I/O），在两个
   **词元字符**之间的空白间隙里插入**一个** ASCII 空格（插在间隙末尾、紧贴后一个
   词元字符），其它位置逐字符不动。"需要插入"= 两侧都是词元字符（`str.isalnum()`，
   **不含** `_`，与查询侧 `[^\\W_]+` 等价）**且至少一侧是汉字**：

   - 汉字↔汉字：`中文` → `中 文`（逐字切分的本意）
   - 汉字↔字母/数字：`Transformer架构` → `Transformer 架 构`、`第3章` → `第 3 章`
   - 字母/数字↔汉字：`abc中文` → `abc 中 文`
   - 字母/数字↔字母/数字：**不插** ⇒ 纯英文是**恒等变换**
   - 任一侧是标点/符号/空白/`_`：**不插**（`unicode61` 本来就在那里断开）

   确切 Unicode 范围硬编码在 `atlas.search.cjk.CJK_RANGES`：
   U+3400–U+4DBF（扩展 A）、U+4E00–U+9FFF（统一表意）、U+F900–U+FAFF（兼容表意）、
   U+20000–U+2EBEF（扩展 B–F）、U+2F800–U+2FA1F（兼容表意补充）、U+30000–U+323AF
   （扩展 G–H）；**不含**假名（U+3040–U+30FF）与 CJK 标点/部首（它们本来就被
   `unicode61` 当分隔符，切分对它们无意义；假名属音节文字，另有裁决）。
   **往返性质**：任意文本 `desegment(segment_cjk(text)) == text`。
   边界：`segment_cjk("中 文") == "中  文"`（真实空格 + 插入空格 = 两个空格），
   `desegment("中  文") == "中 文"`（**只折叠一个**，不得把真实空格一起吃掉）；
   `desegment("中 文") == "中文"`（单个空格就是切分器插入的那个）。
   对切分像，`segment_cjk(desegment(x)) == x`（两侧互为逆）。
   判定"某个空格是不是插入的"只有**一个**出处：`is_inserted_space` 与
   `segment_cjk` 共用同一个 `_needs_separator`。

C1b **汉字↔拉丁边界必须可查**（第二轮裁决的核心）：`架构` 在 `Transformer架构` 中
   命中、`模型` 在 `GPT模型` 中命中、`向量数据库` 在 `向量数据库abc` 中命中；
   `第3章` / `BERT模型` 这类数字/字母混写同样可查。每条都是**真实断言**。

C2 **索引进独立列**：`search_documents` 增列 `text_index TEXT NOT NULL`，内容恒为
   `segment_cjk(text)`；FTS5 外部内容表的列名与内容表列名**一致**（SQLite 要求，
   否则报 `no such column: T.…`），且 MATCH 只走切分后的列。
   该列是**内部列**：`IndexedDocument` / `SearchHit` 上不存在它，`get()` 返回原始文本。

C3 **版本必须升 + 补救路径**：`SCHEMA_VERSION == 2`（物理列没变）、`INDEX_VERSION ==
   "atlas.search.index/3"`（切分规则变了 ⇒ 索引语义变了）、
   `SEGMENTATION_VERSION == "atlas.search.cjk/2"`；磁盘上的 v1/v2 索引在**构造/读取时
   响亮失败**（`IndexVersionError`，消息指向"删索引后重建"），且**不得**被静默当成空索引；
   `drop_search_index()` 后重建可用（否定性断言必须配**活对照**：同一路径对合法输入必须成功）。

C4 **查询侧同一套切分 + 连续汉字是短语（不是逐字 AND）**：
   `SearchQuery(text="中文分词测试").match_expression() == '"中 文 分 词 测 试"'`；
   `SearchQuery(text="数据库abc").match_expression() == '"数 据 库 abc"'`（边界切开后
   整段是**一个**短语，与文档侧切分一致）。
   **为什么不能逐字 AND**：`"人" AND "工" AND "智" AND "能"` 会命中"世界**人**民**工**
   作**智**慧**能**力"。用两条可执行对照证明（同一条 SQL 路径跑逐字 AND 表达式）：
   查 `中文分词` 短语只命中 `中文分词测试…`，逐字 AND 还命中 `分词中文顺序颠倒…`；
   查 `人工智能` 短语不命中 `世界人民工作智慧能力`，逐字 AND 命中。
   安全属性不得退化：用户输入**永不**作为 FTS5 表达式，词元仍由本层加引号，
   操作符仍只作字面词。

C5 **摘要/高亮绝不露出插入的分隔符**：摘要必须从**原始文本**生成（结构性：去掉高亮
   标记与省略号后，摘要的每一段都必须是原始文本的子串）；`中 文` / `分 词` /
   `Transformer 架 构` 这类被切开的形式**不得出现**，而 `[中文]` / `[Transformer架构]`
   这类高亮必须出现（活对照）。

C6 **英文行为完全不变**：同一英文语料 + 与 T-205 相同的英文查询，在"FTS5 直接建在
   `text` 上"的**对照索引**与修订后的索引之间，命中 `raw_id` 顺序、`score`、`fts_score`、
   `snippet` 必须逐条相同；且 `segment_cjk` 对纯 ASCII/拉丁文本是恒等变换。

C7 **零新增依赖**：只用标准库；`pyproject.toml` / `uv.lock` 不改；包内 import 白名单
   不扩充（`tests/test_search_invariants.py` 静态钉死）。

C8 **索引仍是只读投影、可全量重建**（T-205 的 A1–A6 判据不得退化）：`drop()` 后
   `rebuild()` 结果逐条相同；authorizer 仍只碰 `search_*` 表。

C9 **真实数据流证据**（硬规则 1）：真实语料 `data/raw/**/*.json` 的 CJK 文档数如实报数；
   真实中文查询给出命中数 + 排序 + 摘要；若真实语料无中文，则把一份真实中文文档走
   完整链路（raw 字节 → 归档 → 归一化 → 索引 → 查询）产生证据，并**明确区分**两类数字。
   见 `tests/test_search_realdata.py`。

本文件覆盖 C1/C1b–C6 的纯逻辑与索引行为部分；C7 / C8 见 `test_search_invariants.py` 与
`test_search_index.py`，C9 见 `test_search_realdata.py`。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.search import (
    DOCS_TABLE,
    FTS_TABLE,
    INDEX_VERSION,
    META_TABLE,
    SCHEMA_VERSION,
    SEGMENTATION_VERSION,
    TOKENIZER,
    ArchiveDocumentSource,
    DocumentText,
    HIGHLIGHT_CLOSE,
    HIGHLIGHT_OPEN,
    IndexVersionError,
    IndexedDocument,
    SearchHit,
    SearchQuery,
    SNIPPET_ELLIPSIS,
    SqliteSearchIndex,
    drop_search_index,
    open_index,
    phrase_groups,
    tokenize,
)
from atlas.search import query as query_module
from atlas.search.cjk import (
    CJK_RANGES,
    desegment,
    is_cjk,
    is_inserted_space,
    is_token_character,
    segment_cjk,
)
from atlas.search.snippet import (
    SNIPPET_SENTINEL_CLOSE,
    SNIPPET_SENTINEL_ELLIPSIS,
    SNIPPET_SENTINEL_OPEN,
    restore_snippet,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

#: 区分性判据（C4）用的真实形态语料：每篇都"含某些字"，但只有 doc-a 是连续串。
DOC_A = "中文分词测试与检索 mixed english"
DOC_B = "世界人民工作智慧能力"  # 人 / 工 / 智 / 能 四个字都有，但**不连续**
DOC_C = "分词中文顺序颠倒的样例"  # 中 / 文 / 分 / 词 四个字都有，但**顺序颠倒**

#: C1b 用的**混写**语料（汉字与拉丁/数字直接相邻，没有空白）。
MIXED_DOCS = (
    "Transformer架构与注意力机制",
    "GPT模型驱动的检索系统",
    "向量数据库abc的工程实践",
    "第3章 讲 BERT模型 的微调",
)


# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #
def make_record(
    channel_id: str, endpoint: str, content: bytes, *, fetched_at: datetime = BASE
) -> RawRecord:
    digest = content_sha256(content)
    return RawRecord(
        raw_id=raw_id_for(channel_id, endpoint, digest),
        channel_id=channel_id,
        endpoint=endpoint,
        content_sha256=digest,
        byte_length=len(content),
        fetched_at=fetched_at,
        http_status=200,
    )


class Harness:
    """临时存储根 + 归档 + 索引（全部在 `tmp_path` 下，绝不碰仓库 `data/`）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.archive: ArchiveStore = open_archive(root)
        self.index = open_index(root)

    @property
    def db_path(self) -> Path:
        return self.root / "atlas.db"

    def add(self, channel_id: str, endpoint: str, body: str, **kwargs: object) -> RawRecord:
        content = body.encode("utf-8")
        record = make_record(channel_id, endpoint, content, **kwargs)  # type: ignore[arg-type]
        return self.archive.put(record, content)

    def rebuild(self, **kwargs: object):
        return self.index.rebuild(
            ArchiveDocumentSource(self.archive).iter_documents(), **kwargs  # type: ignore[arg-type]
        )

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


def seed_cjk(harness: Harness) -> Dict[str, str]:
    """写入 C4 的三篇区分性语料，返回 `标签 -> raw_id`。"""
    records = {
        "a": harness.add("ch-a", "https://x.invalid/a", DOC_A),
        "b": harness.add("ch-b", "https://x.invalid/b", DOC_B),
        "c": harness.add("ch-c", "https://x.invalid/c", DOC_C),
    }
    return {label: record.raw_id for label, record in records.items()}


# =========================================================================== #
# C1：纯函数切分与可逆性
# =========================================================================== #
def test_segment_cjk_puts_exactly_one_space_between_han_characters() -> None:
    assert segment_cjk("中文分词测试") == "中 文 分 词 测 试"
    assert segment_cjk("中") == "中"
    assert segment_cjk("") == ""
    # 长串汉字：每个字之间一个空格，首尾不加
    assert segment_cjk("人工智能") == "人 工 智 能"
    assert not segment_cjk("人工智能").startswith(" ")
    assert not segment_cjk("人工智能").endswith(" ")


def test_segment_cjk_splits_the_han_latin_boundary() -> None:
    """C1/C1b：汉字与拉丁/数字**直接相邻**（无空白）时也必须切开，两个方向都要。"""
    assert segment_cjk("Transformer架构") == "Transformer 架 构"
    assert segment_cjk("架构Transformer") == "架 构 Transformer"
    assert segment_cjk("GPT模型") == "GPT 模 型"
    assert segment_cjk("模型GPT") == "模 型 GPT"
    assert segment_cjk("向量数据库abc") == "向 量 数 据 库 abc"
    assert segment_cjk("第3章") == "第 3 章"
    assert segment_cjk("5G技术") == "5G 技 术"
    assert segment_cjk("v2版本") == "v2 版 本"
    assert segment_cjk("A中B文C") == "A 中 B 文 C"
    # 标点/符号/下划线**不是**词元字符 ⇒ 不插（unicode61 本来就在那里断开）
    assert segment_cjk("中-文") == "中-文"
    assert segment_cjk("中_文") == "中_文"
    assert segment_cjk("中。文") == "中。文"
    assert segment_cjk("中/文") == "中/文"
    assert segment_cjk("中+文") == "中+文"
    # 间隙里已有空白时同样插入（可逆性所需），数字/字母两侧一致
    assert segment_cjk("中 abc") == "中  abc"
    assert segment_cjk("abc 中") == "abc  中"
    assert segment_cjk("第 3 章") == "第  3  章"


def test_segment_cjk_is_the_identity_on_pure_latin_and_digits() -> None:
    """C6 的结构性保证：没有汉字 ⇒ 逐字节不变（英文索引内容因此完全不变）。"""
    for text in (
        "vector database retrieval with BM25 ranking.",
        "C++ 17 café naïve 42%",
        "foo-bar_baz/qux+v2",
        "  leading and trailing  ",
        "multi   space\tand\nnewline",
        "ABCdef123",
        "[]{}()<>!?",
    ):
        assert segment_cjk(text) == text, f"{text!r} 不是恒等变换"
        assert desegment(text) == text


def test_segment_cjk_touches_nothing_outside_han_runs() -> None:
    """契约："其它位置一律不动"——只有词元字符之间、且至少一侧是汉字时才插。"""
    for text in (
        "vector database retrieval",
        "C++ 17 café naïve",
        "混合 text 与中文",
        "检索 retrieval 系统",
        "「中文」：测试，完毕。",
        "テキスト と 漢字",  # 假名不在范围内（见 C1 的说明）
        "emoji 🙂 与文字",
        "多   空格\t与\n换行",
        "Transformer架构 v2 与 GPT模型",
    ):
        segmented = segment_cjk(text)
        # 不会出现两个连续汉字，也不会出现"汉字紧贴字母/数字"
        for index in range(len(segmented) - 1):
            first, second = segmented[index], segmented[index + 1]
            if not (is_token_character(first) and is_token_character(second)):
                continue
            assert not (is_cjk(first) or is_cjk(second)), (
                f"{text!r} 切分后仍有未切开的汉字词元边界：{segmented!r}"
            )
        # 去掉插入的空格必须得到原文（可逆性的结构性表述）
        assert desegment(segmented) == text


def test_is_token_character_matches_the_query_side_term_regex() -> None:
    """`is_token_character` 必须与查询侧切词正则 `[^\\W_]+` 逐字符等价（否则两侧口径分家）。"""
    samples = (
        "a", "Z", "0", "9", "_", "-", "+", ".", " ", "\t", "中", "文", "あ", "テ",
        "🙂", "é", "٣", "Ⅷ", "①", "\U00020000", "\uf900", "。", "，", "％", "＿",
    )
    for character in samples:
        assert is_token_character(character) == bool(
            query_module._TERM_RE.fullmatch(character)
        ), f"{character!r} 的判定与查询侧不一致"
    # 穷举 BMP 中一批码点，确保不是靠样例凑出来的
    mismatches = [
        code
        for code in range(0x0000, 0x3000)
        if is_token_character(chr(code)) != bool(query_module._TERM_RE.fullmatch(chr(code)))
    ]
    assert mismatches == [], f"U+{mismatches[0]:04X} 起有 {len(mismatches)} 个码点判定不一致"


def test_segment_cjk_range_boundaries_are_exact() -> None:
    """六个区段的首尾码点必须在内，紧邻的区段外码点必须在外。"""
    assert CJK_RANGES == (
        (0x3400, 0x4DBF),
        (0x4E00, 0x9FFF),
        (0xF900, 0xFAFF),
        (0x20000, 0x2EBEF),
        (0x2F800, 0x2FA1F),
        (0x30000, 0x323AF),
    )
    inside = (
        0x3400,
        0x4DBF,
        0x4E00,
        0x9FFF,
        0xF900,
        0xFAFF,
        0x20000,
        0x2EBEF,
        0x2F800,
        0x2FA1F,
        0x30000,
        0x323AF,
    )
    outside = (
        0x33FF,
        0x4DC0,
        0x4DFF,
        0xA000,  # 彝文音节，紧邻统一表意之后
        0xF8FF,  # 私用区
        0xFB00,  # 拉丁连字
        0x1FFFF,
        0x2EBF0,
        0x2F7FF,
        0x2FA20,
        0x2FFFF,
        0x323B0,
        0x3042,  # 平假名 あ
        0x30C6,  # 片假名 テ
        0x3002,  # 中文句号
        0x2F00,  # 康熙部首
    )
    for code in inside:
        assert is_cjk(chr(code)), f"U+{code:04X} 应当在范围内"
    for code in outside:
        assert not is_cjk(chr(code)), f"U+{code:04X} 不应当在范围内"


ROUND_TRIP_TEXTS = (
    "",
    "vector database retrieval with BM25 ranking.",
    "中文",
    "中文分词测试与检索",
    "中 文",  # 真实空格：切分后是两个空格
    "中  文",  # 两个真实空格：切分后是三个
    "中文 分词",
    "中\n文",
    "中\t文",
    "中 \t\n 文",
    "中。文",
    "「中文」测试。",
    "中文abc",
    "abc中文",
    "a 中 文 b",
    "🙂中🙂文🙂",
    "\U00020000\U00020001",  # 扩展 B
    "\ue000中\ue000文",  # 私用区不影响
    "汉" * 50,
    ("汉字 abc " * 20).strip(),
)


@pytest.mark.parametrize("text", ROUND_TRIP_TEXTS)
def test_desegment_is_the_exact_inverse_of_segment_cjk(text: str) -> None:
    """往返性质（C1 的硬要求）：真实空格必须被保留。"""
    segmented = segment_cjk(text)
    assert desegment(segmented) == text


def test_round_trip_holds_for_long_generated_mixtures() -> None:
    """组合式覆盖：空白/汉字/拉丁/数字/标点的各种拼接都必须往返。"""
    alphabet = ("中", "文", "a", "1", "A", " ", "  ", "\n", "\t", "。", "-", "_", "🙂", " 中文 ")
    texts = {""}
    for first in alphabet:
        for second in alphabet:
            texts.add(first + second)
            for third in alphabet:
                texts.add(first + second + third)
    for text in sorted(texts):
        assert desegment(segment_cjk(text)) == text, f"往返失败：{text!r}"


def test_single_space_between_han_characters_becomes_two_and_is_not_over_collapsed() -> None:
    """C1 的边界：原本就在两个汉字之间的**一个**真实空格，切分后变成两个空格；
    逆映射只许折叠**一个**，否则真实空格被吃掉（这正是"可逆性"为什么要求插入空格）。"""
    assert segment_cjk("中 文") == "中  文"
    assert desegment("中  文") == "中 文"
    assert desegment("中 文") == "中文"
    # 反过来：切分与逆切分在像上互为逆
    for text in ROUND_TRIP_TEXTS:
        segmented = segment_cjk(text)
        assert segment_cjk(desegment(segmented)) == segmented


def test_is_inserted_space_is_the_single_source_of_truth_for_the_inverse() -> None:
    """逆映射的判定只有一个出处（内部一致，不会出现两套规则打架）。"""
    segmented = segment_cjk("中 文 与 分词")
    inserted = [
        index
        for index in range(len(segmented))
        if is_inserted_space(segmented, index)
    ]
    assert [segmented[index] for index in inserted] == [" ", " ", " ", " "]
    # 真实空格（"中"与"文"之间那两个空格里的**前**一个）没有被判成插入的
    real_space = segmented.index("  文")
    assert segmented[real_space] == " " and real_space not in inserted
    # 非空格位置永远不是插入的
    assert not any(is_inserted_space(segmented, i) for i, ch in enumerate(segmented) if ch != " ")


def test_segment_cjk_rejects_non_string() -> None:
    for bad in (b"bytes", None, 42):
        with pytest.raises(Exception) as info:
            segment_cjk(bad)  # type: ignore[arg-type]
        assert "str" in str(info.value)
        with pytest.raises(Exception):
            desegment(bad)  # type: ignore[arg-type]


# =========================================================================== #
# C2：索引进独立列
# =========================================================================== #
def test_documents_table_has_a_separate_index_column_and_fts_points_at_it(
    harness: Harness,
) -> None:
    seed_cjk(harness)
    harness.rebuild()

    columns = {
        row[1]: row for row in harness.index.connection.execute(f"PRAGMA table_info({DOCS_TABLE})")
    }
    assert "text_index" in columns, f"{DOCS_TABLE} 缺少 text_index 列"
    assert columns["text_index"][3] == 1, "text_index 必须是 NOT NULL"
    assert "text" in columns and columns["text"][3] == 1

    fts_columns = [
        row[1]
        for row in harness.index.connection.execute(f"PRAGMA table_info({FTS_TABLE})")
        if not row[1].startswith("rank")
    ]
    assert fts_columns == ["text_index"], (
        f"FTS5 外部内容表的列名必须与内容表列名一致（否则 SQLite 报 no such column），"
        f"实际 {fts_columns}"
    )


def test_text_index_is_exactly_the_segmented_text(harness: Harness) -> None:
    seed_cjk(harness)
    harness.add("ch-d", "https://x.invalid/en", "vector database retrieval")
    harness.rebuild()

    rows = harness.index.connection.execute(
        f"SELECT raw_id, text, text_index FROM {DOCS_TABLE} ORDER BY raw_id"
    ).fetchall()
    assert len(rows) == 4
    for row in rows:
        assert row["text_index"] == segment_cjk(row["text"]), (
            f"{row['raw_id']} 的 text_index 不是切分后的文本"
        )
    by_text = {row["text"]: row["text_index"] for row in rows}
    assert by_text[DOC_A] == "中 文 分 词 测 试 与 检 索  mixed english"
    assert by_text["vector database retrieval"] == "vector database retrieval"


def test_match_only_goes_through_the_index_column(harness: Harness) -> None:
    seed_cjk(harness)
    harness.rebuild()
    with harness.index.lock:
        by_index = harness.index.connection.execute(
            f"SELECT COUNT(*) AS n FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", ('"中 文"',)
        ).fetchone()["n"]
        whole_run = harness.index.connection.execute(
            f"SELECT COUNT(*) AS n FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", ('"中文"',)
        ).fetchone()["n"]
    # 切分后的列能被逐字短语命中：doc-a（中文分词…）与 doc-c（分词中文顺序颠倒…）
    assert by_index == 2, "切分后的列必须能被逐字短语命中"
    assert whole_run == 0, "未切分的连写不再是一个词元（否则说明索引没走 text_index）"


def test_text_index_never_leaks_through_any_public_return_path(harness: Harness) -> None:
    """C2/C5：`text_index` 是内部列——返回类型上根本没有它。"""
    assert "text_index" not in {field.name for field in dataclasses.fields(IndexedDocument)}
    assert "text_index" not in {field.name for field in dataclasses.fields(SearchHit)}

    record = harness.add("ch-a", "https://x.invalid/a", DOC_A)
    harness.rebuild()
    document = harness.index.get(record.raw_id)
    assert document is not None
    assert document.text == DOC_A, "get() 必须返回**原始**归一化文本"
    assert not hasattr(document, "text_index")

    hit = harness.index.search(SearchQuery(text="中文", limit=1)).items[0]
    assert not hasattr(hit, "text_index")
    assert "text_index" not in repr(document) and "text_index" not in repr(hit)
    assert "text_index" not in document.__dict__ and "text_index" not in hit.__dict__


# =========================================================================== #
# C3：版本升级与补救路径
# =========================================================================== #
def test_schema_and_index_versions_are_bumped() -> None:
    assert SCHEMA_VERSION == 2, "物理列没变（切分规则变化不动 schema）"
    assert INDEX_VERSION == "atlas.search.index/3", "切分规则变了 ⇒ 索引语义版本必须升"
    assert SEGMENTATION_VERSION == "atlas.search.cjk/2"


_LEGACY_V1_DDL = f"""
CREATE TABLE {META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE {DOCS_TABLE} (
    doc_rowid      INTEGER PRIMARY KEY,
    raw_id         TEXT NOT NULL UNIQUE,
    channel_id     TEXT NOT NULL,
    endpoint       TEXT NOT NULL,
    industry       TEXT,
    content_sha256 TEXT NOT NULL,
    byte_length    INTEGER NOT NULL,
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER,
    text           TEXT NOT NULL,
    text_sha256    TEXT NOT NULL,
    text_length    INTEGER NOT NULL
);
CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(
    text,
    content='{DOCS_TABLE}',
    content_rowid='doc_rowid',
    tokenize="{TOKENIZER}"
);
"""


def _write_legacy_v1_index(db_path: Path) -> None:
    """磁盘上真实存在的**旧版本**索引（T-205 交付形态）：schema_version=1，无 text_index。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_LEGACY_V1_DDL)
        conn.execute(
            f"INSERT INTO {META_TABLE}(key, value) VALUES ('schema_version', '1')"
        )
        for key, value in (
            ("index_version", "atlas.search.index/1"),
            ("tokenizer", TOKENIZER),
            ("text_source", "atlas.normalize.normalize -> NormalizedText.text"),
            ("document_count", "1"),
            ("empty_text_count", "0"),
            ("corpus_sha256", "0" * 64),
            ("industry_source", "none"),
            ("built_at", "2026-01-01T00:00:00+00:00"),
            ("index_built", "1"),
        ):
            conn.execute(
                f"INSERT INTO {META_TABLE}(key, value) VALUES (?, ?)", (key, value)
            )
        conn.execute(
            f"INSERT INTO {DOCS_TABLE}(doc_rowid, raw_id, channel_id, endpoint, industry,"
            " content_sha256, byte_length, fetched_at, http_status, text, text_sha256,"
            " text_length) VALUES (1, 'raw_legacy', 'ch-a', 'https://x.invalid/1', NULL,"
            f" '{'0' * 64}', 3, '2026-01-01T00:00:00.000000+00:00', 200,"
            " '中文分词测试', '0' * 64, 12)"
        )
        conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')")
        conn.commit()
    finally:
        conn.close()


def test_legacy_v1_index_fails_loudly_and_is_recoverable(tmp_path: Path) -> None:
    """旧索引必须**响亮失败**，并给出"删索引后重建"的补救路径（判据 C3）。"""
    legacy_path = tmp_path / "legacy" / "atlas.db"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    _write_legacy_v1_index(legacy_path)

    # 对照：这确实是一份 v1 形态的库（没有 text_index 列），不是"随便损坏的文件"
    conn = sqlite3.connect(str(legacy_path))
    try:
        legacy_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({DOCS_TABLE})")}
        legacy_version = conn.execute(
            f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "text_index" not in legacy_columns
    assert legacy_version == "1"

    with pytest.raises(IndexVersionError) as info:
        SqliteSearchIndex(legacy_path)
    message = str(info.value)
    assert "schema" in message and "重建" in message
    assert str(SCHEMA_VERSION) in message

    # 活对照：同一构造路径对**新版本**库必须成功
    healthy = open_index(tmp_path / "healthy")
    healthy.close()

    # 补救路径：删索引（只删本域的表）→ 重开 → 重建 → 可查
    drop_search_index(legacy_path)
    recovered = SqliteSearchIndex(legacy_path)
    try:
        assert recovered.is_built() is False
        assert recovered.count() == 0
        recovered.rebuild(
            [
                DocumentText(
                    record=make_record("ch-a", "https://x.invalid/a", DOC_A.encode("utf-8")),
                    text=DOC_A,
                )
            ]
        )
        assert recovered.search(SearchQuery(text="中文分词")).total == 1
        assert recovered.meta()["schema_version"] == str(SCHEMA_VERSION)
        assert recovered.meta()["index_version"] == INDEX_VERSION
    finally:
        recovered.close()


@pytest.mark.parametrize(
    "stale_version",
    ["atlas.search.index/1", "atlas.search.index/2"],
)
def test_stale_index_version_is_not_silently_an_empty_index(
    harness: Harness, stale_version: str
) -> None:
    """版本不符必须响亮失败——不得伪装成"没有命中"（判据 C3 的否定性断言 + 活对照）。

    `/1` = T-205 原始形态；`/2` = 上一轮修订（只切汉字↔汉字）——两者现在都必须弃用重建，
    因为切分规则又变了、`text_index` 的内容不同（物理 schema 相同，所以只有
    `index_version` 能识别出来）。
    """
    seed_cjk(harness)
    harness.rebuild()
    # 活对照：版本正确时确实有命中
    assert harness.index.search(SearchQuery(text="中文分词")).total == 1

    with harness.index.lock:
        harness.index.connection.execute(
            f"UPDATE {META_TABLE} SET value = ? WHERE key = 'index_version'",
            (stale_version,),
        )
    with pytest.raises(IndexVersionError) as info:
        harness.index.search(SearchQuery(text="中文分词"))
    message = str(info.value)
    assert "索引版本不符" in message and "重建" in message
    assert harness.index.is_built() is False
    assert harness.index.meta()["index_version"] == stale_version

    # 活对照之二：重建之后恢复可用（索引是可重建派生物）
    harness.index.drop()
    harness.rebuild()
    assert harness.index.search(SearchQuery(text="中文分词")).total == 1
    assert harness.index.meta()["index_version"] == INDEX_VERSION


# =========================================================================== #
# C4：查询侧同一套切分；连续汉字 → 短语
# =========================================================================== #
def test_han_run_becomes_one_phrase_in_the_match_expression() -> None:
    query = SearchQuery(text="中文分词测试")
    assert query.match_expression() == '"中 文 分 词 测 试"'
    assert query.terms() == ("中", "文", "分", "词", "测", "试")
    assert phrase_groups("中文分词测试") == (("中", "文", "分", "词", "测", "试"),)


def test_han_runs_separated_by_real_whitespace_are_separate_phrases() -> None:
    assert SearchQuery(text="中文 分词").match_expression() == '"中 文" AND "分 词"'
    assert SearchQuery(text="中文\n分词").match_expression() == '"中 文" AND "分 词"'
    assert SearchQuery(text="分词中文顺序颠倒").match_expression() == '"分 词 中 文 顺 序 颠 倒"'


def test_han_and_latin_are_combined_with_and() -> None:
    assert (
        SearchQuery(text="vector 数据库 retrieval").match_expression()
        == '"vector" AND "数 据 库" AND "retrieval"'
    )
    # 汉字与拉丁直接相邻 ⇒ 同一段未切开的输入 ⇒ **一个**短语（与文档侧切分一致）
    assert SearchQuery(text="数据库abc").match_expression() == '"数 据 库 abc"'
    assert SearchQuery(text="Transformer架构").match_expression() == '"Transformer 架 构"'
    assert SearchQuery(text="GPT模型").match_expression() == '"GPT 模 型"'
    assert SearchQuery(text="BM25 排序").match_expression() == '"BM25" AND "排 序"'
    # 用户打了空白就是两个组（`BM25` 与 `排 序` 之间是真实空白）


def test_tokenize_still_dedupes_and_undercuts_the_term_cap() -> None:
    assert tokenize("foo-bar") == ("foo", "bar")
    assert tokenize("beta alpha beta") == ("beta", "alpha")
    assert tokenize("中文分词") == ("中", "文", "分", "词")
    assert tokenize("混合 text 与中文") == ("混", "合", "text", "与", "中", "文")
    assert tokenize(" ".join(["same"] * 500)) == ("same",)


def test_phrase_groups_agree_with_segment_cjk_runs() -> None:
    """两侧必须用同一套"连续片段"定义（单一出处，见 `atlas.search.cjk`）。"""
    with_phrases = 0
    for text in (
        "中文分词测试",
        "中文 分词",
        "中 文",
        "vector 数据库 retrieval",
        "数据库abc",
        "abc中文",
        "Transformer架构",
        "第3章",
        "中\n文\t之 间",
        "「中文」测试。",
        "abc",
        "汉字" * 3 + " " + "汉字",
    ):
        groups = phrase_groups(text)
        flat = tuple(term for group in groups for term in group)
        # `tokenize` 是对展开后的词元**再去重**（判据 A6 的词元计数口径），
        # 因此不变式是"去重后一致"，而不是逐位置一致。
        deduped: List[str] = []
        for term in flat:
            if term not in deduped:
                deduped.append(term)
        assert tuple(deduped) == tokenize(text), f"{text!r}: 分组展开与 tokenize 不一致"
        # 每个多成员组必须是"用户输入里未被切开的连续片段"：
        # 把它拼回去再过一次切分，必须正好等于组内以空格连接的形式
        for group in groups:
            if len(group) > 1:
                with_phrases += 1
                assert segment_cjk("".join(group)) == " ".join(group), (
                    f"{text!r}: 短语组 {group!r} 不是连续片段"
                )
    # 活对照：样例里确实产生了短语组（否则上面的循环是空转）
    assert with_phrases >= 3
    # 用户打了空白就是两个组（不是一条短语）
    assert phrase_groups("中 文") == (("中",), ("文",))
    assert phrase_groups("中文分词") == (("中", "文", "分", "词"),)
    # 汉字与拉丁直接相邻是**同一段未切开的输入** ⇒ 一个组（字母+汉字同组）
    assert phrase_groups("Transformer架构") == (("Transformer", "架", "构"),)
    assert phrase_groups("数据库abc") == (("数", "据", "库", "abc"),)
    assert phrase_groups("第3章") == (("第", "3", "章"),)
    # 标点不是词元字符 ⇒ 天然拆组
    assert phrase_groups("中-文") == (("中",), ("文",))


def test_expression_contains_only_quoted_literals_and_and_for_cjk_input() -> None:
    """C4 的安全属性：用户输入永不作为 FTS5 表达式。"""
    for text in ('中文" OR "测试', "中文*", "中 NEAR 文", "-中文", "中:文", "中文)", "(中文"):
        query = SearchQuery(text=text)
        expression = query.match_expression()
        assert set(expression) <= set('" AND') | set("".join(query.terms())), expression
        for term in query.terms():
            assert term in expression
        # 操作符只作字面词：表达式里除了引号/空格/AND 与词元本身没有别的字符
        assert expression.count('"') % 2 == 0


def test_phrase_query_only_matches_contiguous_han(harness: Harness) -> None:
    """C4 的主判据：连续汉字按短语匹配（顺序敏感），不按逐字 AND。"""
    ids = seed_cjk(harness)
    harness.rebuild()

    # 活对照：doc-a 的整串切分确实进了索引
    assert harness.index.search(SearchQuery(text=DOC_A)).total == 1

    contiguous = harness.index.search(SearchQuery(text="中文分词"))
    assert contiguous.total == 1
    assert [hit.raw_id for hit in contiguous.items] == [ids["a"]]
    assert "[中文分词]" in contiguous.items[0].snippet

    # 顺序敏感：颠倒顺序的 doc-c 不命中
    assert harness.index.search(SearchQuery(text="分词中文")).total == 1
    assert [hit.raw_id for hit in harness.index.search(SearchQuery(text="分词中文")).items] == [
        ids["c"]
    ]

    # doc-b 四个字都有，但不相邻 ⇒ 不命中
    assert harness.index.search(SearchQuery(text="人工智能")).total == 0
    # 命中 doc-b 自己的连续串（证明它确实在索引里，测试不是空转）
    assert harness.index.search(SearchQuery(text="世界人民")).total == 1
    assert harness.index.search(SearchQuery(text="智慧能力")).total == 1


def test_per_character_and_is_a_false_positive_machine(harness: Harness) -> None:
    """C4 的可执行证明：同一条 FTS 上跑**逐字 AND**，出现短语不会出现的假阳性。

    这条测试是"为什么不能用逐字 AND"的证据，不是注释。
    """
    ids = seed_cjk(harness)
    harness.rebuild()

    def and_hits(terms: str) -> List[str]:
        expression = " AND ".join(f'"{term}"' for term in terms.split())
        with harness.index.lock:
            rows = harness.index.connection.execute(
                f"SELECT d.raw_id AS raw_id FROM {FTS_TABLE} JOIN {DOCS_TABLE} AS d "
                f"ON d.doc_rowid = {FTS_TABLE}.rowid "
                f"WHERE {FTS_TABLE} MATCH ? ORDER BY d.raw_id",
                (expression,),
            ).fetchall()
        return [row["raw_id"] for row in rows]

    def phrase_hits(phrase: str) -> List[str]:
        expression = '"' + " ".join(phrase) + '"'
        with harness.index.lock:
            rows = harness.index.connection.execute(
                f"SELECT d.raw_id AS raw_id FROM {FTS_TABLE} JOIN {DOCS_TABLE} AS d "
                f"ON d.doc_rowid = {FTS_TABLE}.rowid "
                f"WHERE {FTS_TABLE} MATCH ? ORDER BY d.raw_id",
                (expression,),
            ).fetchall()
        return [row["raw_id"] for row in rows]

    # 1) 中文分词：逐字 AND 把"顺序颠倒"的 doc-c 当成命中（假阳性）
    assert phrase_hits("中文分词") == [ids["a"]]
    assert and_hits("中 文 分 词") == sorted([ids["a"], ids["c"]])

    # 2) 人工智能：doc-b 只是**碰巧**含这四个字 ⇒ 逐字 AND 命中，短语不命中
    assert phrase_hits("人工智能") == []
    assert and_hits("人 工 智 能") == [ids["b"]]

    # 生产路径与短语一致（不是"测试自己构造了另一条路径"）
    assert [hit.raw_id for hit in harness.index.search(SearchQuery(text="中文分词")).items] == [
        ids["a"]
    ]
    assert harness.index.search(SearchQuery(text="人工智能")).total == 0


def test_single_han_character_query_still_works(harness: Harness) -> None:
    """单个汉字也是合法查询（短语长度为 1 时退化成普通词元查询）。"""
    ids = seed_cjk(harness)
    harness.rebuild()
    assert [hit.raw_id for hit in harness.index.search(SearchQuery(text="人")).items] == [ids["b"]]
    assert {hit.raw_id for hit in harness.index.search(SearchQuery(text="文")).items} == {
        ids["a"],
        ids["c"],
    }
    assert harness.index.search(SearchQuery(text="文")).total == 2


# =========================================================================== #
# C1b：汉字↔拉丁/数字边界必须可查（第二轮裁决的核心，真实断言不是"应该可以"）
# =========================================================================== #
def seed_mixed(harness: Harness) -> Dict[str, str]:
    return {
        body: harness.add("ch-mix", f"https://x.invalid/mix-{position}", body).raw_id
        for position, body in enumerate(MIXED_DOCS)
    }


@pytest.mark.parametrize(
    "query,expected_docs",
    [
        ("架构", ("Transformer架构与注意力机制",)),
        ("注意力机制", ("Transformer架构与注意力机制",)),
        ("模型", ("GPT模型驱动的检索系统", "第3章 讲 BERT模型 的微调")),
        ("GPT", ("GPT模型驱动的检索系统",)),
        ("向量数据库", ("向量数据库abc的工程实践",)),
        ("工程实践", ("向量数据库abc的工程实践",)),
        ("abc", ("向量数据库abc的工程实践",)),
        ("第3章", ("第3章 讲 BERT模型 的微调",)),
        ("BERT", ("第3章 讲 BERT模型 的微调",)),
        ("微调", ("第3章 讲 BERT模型 的微调",)),
    ],
)
def test_han_latin_boundary_is_searchable(
    harness: Harness, query: str, expected_docs: Tuple[str, ...]
) -> None:
    """`架构` 必须命中 `Transformer架构`、`模型` 必须命中 `GPT模型`…（C1b）。

    这些是**扩展前实测为 0 命中**的查询（`tools/t205cjk_boundary_probe.py` 记录了前后对照）。
    """
    seed_mixed(harness)
    harness.rebuild()
    result = harness.index.search(SearchQuery(text=query, limit=10))
    hits = {harness.index.get(hit.raw_id).text for hit in result.items}
    assert hits == set(expected_docs), f"{query!r} 的命中集合不符：{hits}"
    # 活对照：两个集合都非空，因此上面的相等不是"两边都空"凑出来的
    assert result.total == len(expected_docs) >= 1


def test_han_latin_boundary_is_really_split_in_the_index(harness: Harness) -> None:
    """负向对照：索引里**不再存在**"汉字紧贴拉丁"的单一 token。"""
    seed_mixed(harness)
    harness.rebuild()
    with harness.index.lock:
        def count(expression: str) -> int:
            return harness.index.connection.execute(
                f"SELECT COUNT(*) AS n FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
                (expression,),
            ).fetchone()["n"]

        assert count('"Transformer架构"') == 0, "未切开的连写不该还能命中"
        assert count('"Transformer 架 构"') == 1
        assert count('"GPT模型"') == 0
        assert count('"GPT 模 型"') == 1
        assert count('"向量数据库abc"') == 0
        assert count('"向 量 数 据 库 abc"') == 1


def test_snippet_reconstructs_the_contiguous_mixed_text(harness: Harness) -> None:
    """摘要里汉字与拉丁重新贴回原文形态（不得露出 `Transformer 架 构`）。"""
    record = harness.add("ch-mix", "https://x.invalid/mix", "Transformer架构与注意力机制")
    harness.rebuild()
    hit = harness.index.search(SearchQuery(text="架构", limit=1)).items[0]
    assert hit.raw_id == record.raw_id
    assert "Transformer[架构]" in hit.snippet, hit.snippet
    assert "Transformer 架 构" not in hit.snippet
    assert "架 构" not in hit.snippet


# =========================================================================== #
# C5：摘要/高亮不泄漏插入的分隔符
# =========================================================================== #
def _plain_snippet(snippet: str) -> str:
    return snippet.replace(HIGHLIGHT_OPEN, "").replace(HIGHLIGHT_CLOSE, "")


def test_snippet_never_exposes_an_inserted_separator(harness: Harness) -> None:
    record = harness.add("ch-a", "https://x.invalid/a", DOC_A)
    harness.rebuild()

    for text, highlighted in (
        ("中文", "[中文]"),
        ("分词", "[分词]"),
        ("中文分词", "[中文分词]"),
        ("测试", "[测试]"),
        ("检索", "[检索]"),
        ("english", "[english]"),
    ):
        hit = harness.index.search(SearchQuery(text=text, limit=1)).items[0]
        assert hit.raw_id == record.raw_id
        assert highlighted in hit.snippet, f"{text!r} 的高亮缺失：{hit.snippet!r}"
        assert "中 文" not in hit.snippet
        assert "分 词" not in hit.snippet
        assert "检 索" not in hit.snippet
        assert " ".join("中文分词测试与检索") not in hit.snippet


@pytest.mark.parametrize("snippet_tokens", [1, 2, 3, 5, 32, 64])
def test_snippet_pieces_are_substrings_of_the_original_text(
    harness: Harness, snippet_tokens: int
) -> None:
    """结构性保证（C5）：去掉高亮标记与省略号后，摘要的每一段都必须是**原文**的子串。

    这条比"不出现空格"更强：任何插入的分隔符都会让某一段不再是原文的子串。
    """
    record = harness.add("ch-a", "https://x.invalid/a", DOC_A)
    harness.add("ch-a", "https://x.invalid/2", "短 中文 文本 与检索" * 3)
    harness.rebuild()

    for text in ("中文", "分词", "检索", "english", "测试"):
        result = harness.index.search(SearchQuery(text=text, limit=5, snippet_tokens=snippet_tokens))
        assert result.total >= 1, text
        for hit in result.items:
            original = harness.index.get(hit.raw_id).text
            for piece in _plain_snippet(hit.snippet).split(SNIPPET_ELLIPSIS):
                if not piece:
                    continue
                assert piece in original, (
                    f"{text!r} 的摘要片段不是**该文档**原文的子串："
                    f"{piece!r}（原文 {original!r}）"
                )
    assert harness.index.get(record.raw_id) is not None


def test_snippet_keeps_literal_brackets_that_belong_to_the_text(harness: Harness) -> None:
    """高亮标记是 `[` / `]`，正文里也可能有字面方括号——两者都必须**原样保留**。

    这条同时说明了为什么"结构性断言"不能用"去掉方括号"来做（真实语料里就有
    `List[Example]` 这种正文）。
    """
    record = harness.add("ch-c", "https://x.invalid/brackets", "代码 [Example] 与 grid[data] 检索")
    harness.rebuild()
    hit = harness.index.search(SearchQuery(text="检索", limit=1)).items[0]
    assert hit.raw_id == record.raw_id
    assert hit.snippet == "代码 [Example] 与 grid[data] [检索]"


def test_restore_snippet_is_the_identity_for_text_without_han() -> None:
    """英文（无汉字）上，还原函数是恒等变换——这正是 C6 的结构性保证。"""
    open_mark, close_mark, dots = SNIPPET_SENTINEL_OPEN, SNIPPET_SENTINEL_CLOSE, SNIPPET_SENTINEL_ELLIPSIS
    assert (
        restore_snippet(
            f"alpha {open_mark}beta{close_mark} gamma",
            indexed_text="alpha beta gamma",
            original_text="alpha beta gamma",
        )
        == "alpha [beta] gamma"
    )
    assert (
        restore_snippet(
            f"{dots}gamma {open_mark}beta{close_mark}",
            indexed_text="gamma beta",
            original_text="gamma beta",
        )
        == "…gamma [beta]"
    )


def test_restore_snippet_removes_only_the_inserted_separators() -> None:
    """直接钉住还原规则：只删切分器插入的那一个空格，真实空格一个不少。"""
    open_mark, close_mark = SNIPPET_SENTINEL_OPEN, SNIPPET_SENTINEL_CLOSE
    original = "中 文 分词"
    segmented = segment_cjk(original)
    assert segmented == "中  文  分 词"
    rendered = f"{open_mark}中  文{close_mark}  分 词"
    assert (
        restore_snippet(rendered, indexed_text=segmented, original_text=original)
        == "[中 文] 分词"
    )


def test_restore_snippet_rejects_an_inconsistent_mapping() -> None:
    """接线错误必须响亮失败，不得静默返回可能泄漏分隔符的串（硬规则 2）。"""
    from atlas.search.errors import SearchIndexError

    with pytest.raises(SearchIndexError) as info:
        # indexed_text 里根本没有 body ⇒ 映射不可能成立
        restore_snippet("[中 文]", indexed_text="完全无关", original_text="中文")
    assert "摘要" in str(info.value)


# =========================================================================== #
# C6：英文行为完全不变（对照索引）
# =========================================================================== #
ENGLISH_DOCS = (
    ("ch-a", "https://x.invalid/1", "Vector database retrieval with BM25 ranking."),
    ("ch-a", "https://x.invalid/2", "BM25 is a ranking function used by search engines."),
    ("ch-b", "https://x.invalid/3", "Gardening tips for spring planting."),
    ("ch-b", "https://x.invalid/4", "A vector database stores embeddings for retrieval."),
    ("ch-c", "https://x.invalid/5", "café naïve analysis with 17 tokens and C++ code"),
    # 正文里带字面方括号：确认"摘要还原"不会把正文的 `[` / `]` 当成标记处理
    ("ch-d", "https://x.invalid/6", "Lists [Example] and grid[data] with retrieval tokens"),
    ("ch-d", "https://x.invalid/7", "A B C café"),
)
ENGLISH_QUERIES = (
    SearchQuery(text="bm25"),
    SearchQuery(text="bm25 ranking"),
    SearchQuery(text="vector database", limit=3),
    SearchQuery(text="retrieval", order="recency"),
    SearchQuery(text="cafe"),
    SearchQuery(text="naïve"),
    SearchQuery(text="database retrieval", snippet_tokens=4),
    SearchQuery(text="grid[data]"),
    SearchQuery(text="alpha"),
)


def _control_query(conn: sqlite3.Connection, query: SearchQuery) -> List[Tuple[str, float, str]]:
    """对照索引上的同一套排序键（T-205 原形态：FTS5 直接建在 `text` 上）。"""
    order_by = (
        "-bm25(ctrl_fts) DESC, ctrl_docs.fetched_at DESC, ctrl_docs.raw_id ASC"
        if query.order == "relevance"
        else "ctrl_docs.fetched_at DESC, ctrl_docs.raw_id ASC"
    )
    rows = conn.execute(
        "SELECT ctrl_docs.raw_id AS raw_id, -bm25(ctrl_fts) AS score, "
        "snippet(ctrl_fts, 0, '[', ']', '…', ?) AS snippet "
        "FROM ctrl_fts JOIN ctrl_docs ON ctrl_docs.doc_rowid = ctrl_fts.rowid "
        f"WHERE ctrl_fts MATCH ? ORDER BY {order_by} LIMIT ?",
        (query.snippet_tokens, query.match_expression(), query.offset + query.limit),
    ).fetchall()
    return [(row["raw_id"], float(row["score"]), row["snippet"]) for row in rows]


def test_english_results_and_snippets_are_byte_identical_to_the_unsegmented_control(
    harness: Harness,
) -> None:
    """C6 的核心：与 T-205 同形态的对照索引逐条对比（命中顺序 / 分数 / 摘要）。"""
    for channel_id, endpoint, body in ENGLISH_DOCS:
        harness.add(channel_id, endpoint, body)
    harness.rebuild()
    assert harness.index.count() == len(ENGLISH_DOCS)

    control = sqlite3.connect(str(harness.root / "control.db"))
    control.row_factory = sqlite3.Row
    try:
        control.executescript(
            f"""
            CREATE TABLE ctrl_docs (
                doc_rowid INTEGER PRIMARY KEY, raw_id TEXT NOT NULL UNIQUE,
                fetched_at TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE ctrl_fts USING fts5(
                text, content='ctrl_docs', content_rowid='doc_rowid', tokenize="{TOKENIZER}"
            );
            """
        )
        with harness.index.lock:
            rows = harness.index.connection.execute(
                f"SELECT raw_id, text, fetched_at FROM {DOCS_TABLE} ORDER BY raw_id"
            ).fetchall()
        for index, row in enumerate(rows, start=1):
            control.execute(
                "INSERT INTO ctrl_docs(doc_rowid, raw_id, fetched_at, text) VALUES (?, ?, ?, ?)",
                (index, row["raw_id"], row["fetched_at"], row["text"]),
            )
        control.execute("INSERT INTO ctrl_fts(ctrl_fts) VALUES ('rebuild')")
        control.commit()

        checked = 0
        for query in ENGLISH_QUERIES:
            observed = [
                (hit.raw_id, hit.score, hit.snippet)
                for hit in harness.index.search(query).items
            ]
            expected = _control_query(control, query)
            assert observed == expected, f"英文查询 {query.text!r} 出现回归"
            checked += 1
        assert checked == len(ENGLISH_QUERIES)
    finally:
        control.close()


def test_empty_control_is_not_vacuous(harness: Harness) -> None:
    """活对照：上面的英文查询在对照与修订索引里都**真的有命中**（否则对比是空转）。"""
    for channel_id, endpoint, body in ENGLISH_DOCS:
        harness.add(channel_id, endpoint, body)
    harness.rebuild()
    totals = {query.text: harness.index.search(query).total for query in ENGLISH_QUERIES}
    assert totals["bm25"] == 2
    assert totals["retrieval"] == 3
    assert totals["cafe"] == 2  # café 与 café 两处（remove_diacritics 2 双向折叠）
    assert totals["grid[data]"] == 1
    assert totals["alpha"] == 0


def test_segment_cjk_is_the_identity_on_english_text() -> None:
    """结构性保证：没有汉字的文本，切分是恒等变换 ⇒ 索引内容逐字节不变。"""
    for _channel, _endpoint, body in ENGLISH_DOCS:
        assert segment_cjk(body) == body
        assert desegment(body) == body
