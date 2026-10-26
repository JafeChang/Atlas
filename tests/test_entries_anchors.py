"""T-130 判据 4：**条目 ID 不作锚点，但字符区间正是锚点**（与 T-206 的区别）。

这是本任务最容易被人误读的一条，因此先把边界写清楚：

| 量 | 性质 | 能不能当锚点 | 为什么 |
|---|---|---|---|
| `Entry.char_start` / `char_end` | 条目在**解码后原文**里的字符区间 —— SPEC §2.2 的真值形状 | ✅ **正是锚点** | 它不掺解析器版本、不掺字段提取规则；改规则时它**确实**指向别处 |
| `Entry.entry_id` | `ent_` + sha256(raw_id, raw_sha256, 解析器版本, 区间, 标题)[:32] | ❌ **永远不是** | 它掺了解析器版本与标题提取规则，换解析器会整体漂移 |

T-206 的结论是"分块**整体**不得作锚点"（`Chunk` 连字符区间都没有）。
T-130 **只禁 ID**，不禁区间 —— 照 `atlas.chunk` 已定的**三层代码级强制**做法办理：

1. **结构层**：`Entry` 里没有任何"锚点"字段；`as_anchor()` **永远抛**
   `EntryNotAnchorError`；`Entry` 与 `EvidenceAnchor` / `DerivedLocator` 类型不同，
   塞错地方 pydantic 直接 `ValidationError`。
2. **形状层**：`entry_id` 是 `ent_…`，过不了 `EvidenceAnchor.raw_sha256` 的
   `^[0-9a-f]{64}$`。
3. **机制层**：换解析器版本 → ID 全变、真值四元组不变。

**活对照**（硬规则 4）：每一层"拒绝"旁边都有一条**成功**的对照 ——
同一字符区间能造出合法 `EvidenceAnchor`，而 ID 不能。没有活对照的话，
签名不匹配 / 异常类型不对会伪装成"拒绝成功"。
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from atlas.contracts import (
    AnchorError,
    ConfirmedLabel,
    ContractError,
    ContractModel,
    DerivedLocator,
    EvidenceAnchor,
    VerificationStatus,
    build_anchor,
)
from atlas.entries import (
    ENTRY_ID_RE,
    Entry,
    EntryNotAnchorError,
    EntryParser,
    parse_entries,
)
from atlas.evidence import verify_quote
from atlas.normalize import normalize

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>锚点边界</title>
<item>
  <title>第一条：区间是锚点而 ID 不是</title>
  <link>https://example.invalid/1</link>
  <pubDate>Thu, 14 Aug 2025 06:31:20 +0000</pubDate>
  <description>证据锚点是 raw 的字符区间，不可变真值。条目 ID 是派生物。</description>
</item>
<item>
  <title>第二条：quote 由确定性匹配算出坐标</title>
  <link>https://example.invalid/2</link>
  <pubDate>Fri, 15 Aug 2025 07:00:00 +0000</pubDate>
  <description>抽取方只输出引用的文字，不输出坐标。</description>
</item>
</channel></rss>
"""
RSS_BYTES = RSS_FEED.encode("utf-8")
CONTENT_TYPE = "application/rss+xml"
RAW_ID = "raw_t130_anchors"

_ENTRY_FIELDS = {
    "entry_id",
    "raw_id",
    "raw_sha256",
    "index",
    "parser_version",
    "kind",
    "char_start",
    "char_end",
    "title",
    "link",
    "published_at",
    "entry_text",
    "problems",
}


class _AnchorHolder(ContractModel):
    """只为验证"塞错类型会被 pydantic 拒绝"的最小载体。"""

    anchor: EvidenceAnchor


def _entrieset():
    entrieset = parse_entries(RSS_BYTES, CONTENT_TYPE, raw_id=RAW_ID)
    assert entrieset.entry_count == 2, entrieset.problems
    return entrieset


# =========================================================================== #
# 第 1 层：结构上 as_anchor() 永远抛，且没有锚点字段
# =========================================================================== #


def test_criterion4_1_entry_as_anchor_always_raises() -> None:
    entrieset = _entrieset()
    for entry in entrieset.entries:
        with pytest.raises(EntryNotAnchorError) as excinfo:
            entry.as_anchor()
        assert entry.entry_id in str(excinfo.value)
        # 错误信息必须指出正确的做法（否则使用者只会绕开它）
        assert "Entry.anchor()" in str(excinfo.value)


def test_criterion4_1_not_anchor_error_is_a_contract_error() -> None:
    """错误类型挂在 T-002 的 `ContractError` 之下：契约违例只有一套语义。"""
    assert issubclass(EntryNotAnchorError, ContractError)


def test_criterion4_1_entry_fields_do_not_contain_a_raw_sha_of_its_own() -> None:
    """`Entry` 的字段清单是**钉死**的：`raw_sha256` 是它所依附的原文指纹（真值必需），
    而 `entry_id` 是它自己的派生标识。两者语义不同，不能混为一谈。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    assert {f.name for f in dataclasses.fields(entry)} == _ENTRY_FIELDS
    assert entry.raw_sha256 == entrieset.raw_sha256  # 真值：指向原文
    assert entry.entry_id != entry.raw_sha256  # 派生：不是真值
    assert not ENTRY_ID_RE.match(entry.raw_sha256)
    assert not entry.raw_sha256.startswith("ent_")


def test_criterion4_2_entry_id_shape_cannot_be_a_raw_sha256() -> None:
    """第 2 层：`ent_…` 过不了 `^[0-9a-f]{64}$`。"""
    entrieset = _entrieset()
    entry_id = entrieset.entries[0].entry_id
    assert ENTRY_ID_RE.match(entry_id)
    with pytest.raises(ValidationError):
        EvidenceAnchor(raw_id=RAW_ID, raw_sha256=entry_id, char_start=0, char_end=5)
    with pytest.raises(ValidationError):
        EvidenceAnchor(raw_id=RAW_ID, raw_sha256=entry_id[4:], char_start=0, char_end=5)


def test_criterion4_2_all_entry_ids_are_unique_and_shape_conformant() -> None:
    entrieset = _entrieset()
    ids = [e.entry_id for e in entrieset.entries]
    assert len(set(ids)) == len(ids)
    assert all(ENTRY_ID_RE.match(value) for value in ids)
    assert all(len(value) == 4 + 32 for value in ids)


def test_criterion4_3_entry_object_rejected_where_anchor_expected() -> None:
    """第 1 层（类型）：把 `Entry` 塞进要求 `EvidenceAnchor` / `DerivedLocator` 的字段。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    with pytest.raises(ValidationError):
        _AnchorHolder(anchor=entry)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DerivedLocator(block_id=entry)  # type: ignore[arg-type]
    # 活对照：同一条目的**锚点**可以放进同一字段
    assert _AnchorHolder(anchor=entry.anchor()).anchor == entry.anchor()


def test_criterion4_3_entry_id_rejected_as_confirmed_label_anchor() -> None:
    """人工标签的锚点必须是 raw 字符区间；条目 ID 不是。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    with pytest.raises(ValidationError):
        ConfirmedLabel(
            label_id="lbl_x",
            raw_id=RAW_ID,
            label_key="entry_ref",
            label_value="1",
            actor="me",
            anchor={
                "raw_id": RAW_ID,
                "raw_sha256": entry.entry_id,
                "char_start": 0,
                "char_end": 3,
            },
        )
    # 活对照 A：标签锚在 raw_id 上（SPEC §2.1 决策 1A）
    label = ConfirmedLabel.human(raw_id=RAW_ID, label_key="industry", label_value="ai", actor="me")
    assert label.raw_id == RAW_ID and label.anchor is None
    # 活对照 B：用**条目区间**构造的合法锚点可以进标签
    with_span = ConfirmedLabel(
        label_id="lbl_y",
        raw_id=RAW_ID,
        label_key="entry_span",
        label_value="1",
        actor="me",
        anchor=entry.anchor(),
    )
    assert with_span.anchor == entry.anchor()


# =========================================================================== #
# 第 0 层 + 活对照：字符区间**能**造出合法锚点
# =========================================================================== #


def test_criterion4_4_character_span_yields_a_valid_evidence_anchor() -> None:
    """活对照：`Entry.anchor()` 成功，且四元组与 SPEC §2.2 的真值形状一致。"""
    entrieset = _entrieset()
    for entry in entrieset.entries:
        anchor = entry.anchor()
        assert isinstance(anchor, EvidenceAnchor)
        assert (anchor.raw_id, anchor.raw_sha256, anchor.char_start, anchor.char_end) == (
            entry.raw_id,
            entry.raw_sha256,
            entry.char_start,
            entry.char_end,
        )
        assert anchor.raw_id == RAW_ID
        assert anchor.raw_sha256 == entrieset.raw_sha256
        assert anchor.length == entry.length > 0

    # 同一个 raw_sha256 / raw_id 必须与独立计算的锚点完全一致
    independent = EvidenceAnchor.create(
        raw_id=RAW_ID,
        raw_sha256=entrieset.raw_sha256,
        char_start=entrieset.entries[0].char_start,
        char_end=entrieset.entries[0].char_end,
    )
    assert independent == entrieset.entries[0].anchor()


def test_criterion4_4_as_anchors_returns_a_valid_anchor_for_every_entry() -> None:
    entrieset = _entrieset()
    anchors = entrieset.as_anchors()
    assert len(anchors) == entrieset.entry_count
    assert all(isinstance(a, EvidenceAnchor) for a in anchors)
    assert [a.char_start for a in anchors] == [e.char_start for e in entrieset.entries]


def test_criterion4_4_empty_or_inverted_span_cannot_become_an_anchor() -> None:
    """区间为空 / 反向时构造锚点必须失败（`AnchorError`，不是静默接受）。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    with pytest.raises(AnchorError):
        EvidenceAnchor.create(
            raw_id=RAW_ID,
            raw_sha256=entrieset.raw_sha256,
            char_start=entry.char_start,
            char_end=entry.char_start,
        )
    # 活对照：非空区间成功
    assert entry.anchor().length > 0
    # 条目本身也拒绝空区间（构造期）
    with pytest.raises(Exception):
        dataclasses.replace(entry, char_end=entry.char_start)


def test_criterion4_4_truth_fields_is_the_anchor_shape() -> None:
    """`truth_fields` 四元组可直接喂给 `EvidenceAnchor`（结构上就是同一形状）。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    raw_id, raw_sha256, char_start, char_end = entry.truth_fields
    rebuilt = EvidenceAnchor.create(
        raw_id=raw_id, raw_sha256=raw_sha256, char_start=char_start, char_end=char_end
    )
    assert rebuilt == entry.anchor()


# =========================================================================== #
# 第 3 层：机制证明 —— ID 漂移，区间不漂移
# =========================================================================== #


def test_criterion4_5_id_drifts_when_the_parser_changes_but_span_does_not() -> None:
    """换解析器版本：ID 全变（派生量漂移），真值四元组逐字段不变。

    这就是"ID 不能当锚点、区间可以"的**实证理由**：任何以 `entry_id` 为锚的
    人工产物，在换解析器后都会静默指向另一处（或消失）；以字符区间为锚的不会。
    """
    baseline = _entrieset()
    other = EntryParser(version="entry-parser-v2-testonly").parse(
        RSS_BYTES, CONTENT_TYPE, raw_id=RAW_ID
    )
    assert other.entry_count == baseline.entry_count  # 活对照
    assert [e.entry_id for e in other.entries] != [e.entry_id for e in baseline.entries]
    assert [e.anchor() for e in other.entries] == [e.anchor() for e in baseline.entries]
    assert [e.truth_fields for e in other.entries] == [
        e.truth_fields for e in baseline.entries
    ]


def test_criterion4_5_title_extraction_change_also_drifts_the_id() -> None:
    """同区间、不同标题 → 不同 ID：ID 掺了字段提取结果，因此不是纯位置量。"""
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    renamed = dataclasses.replace(entry, title="换了个标题")
    assert renamed.recompute_id() != entry.entry_id
    # 但真值四元组完全相同 —— 区间锚点不受标题提取影响
    assert renamed.truth_fields == entry.truth_fields
    assert renamed.anchor() == entry.anchor()


def test_criterion4_5_frozen_records_cannot_be_forged_in_place() -> None:
    entrieset = _entrieset()
    entry = entrieset.entries[0]
    assert isinstance(entry, Entry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.char_start = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.entry_id = "ent_forged"  # type: ignore[misc]
    # `ContractModel` 的 `model_copy(update=...)` 后门与 dataclass 无关，
    # 但 `Entry.anchor()` 返回的是冻结契约记录，同样不能被就地改
    with pytest.raises(Exception):
        entry.anchor().char_start = 1  # type: ignore[misc]


# =========================================================================== #
# 与 T-107 / T-104 的打通：quote 的锚点落在条目区间里
# =========================================================================== #


def test_entry_span_contains_the_quote_anchor_from_deterministic_matching() -> None:
    """`build_anchor` 由 quote 算出的锚点必须落在**包含该 quote 的条目**区间内。

    这是"某条证据属于哪个条目"的判定方式 —— 纯整数比较，不需要任何新机制。
    注意 quote 取自**条目正文里的普通文字**（不含 XML 标记），因此它在归一化文本
    里能精确匹配；坐标由确定性匹配产出（SPEC §2.2）。
    """
    entrieset = _entrieset()
    normalized = normalize(RSS_BYTES, CONTENT_TYPE)
    quote = "证据锚点是 raw 的字符区间"
    status, anchor, derived = build_anchor(
        raw_id=RAW_ID,
        raw_sha256=entrieset.raw_sha256,
        normalized_text=normalized.text,
        quote=quote,
        to_raw_offset=normalized.to_raw_offset,
    )
    assert status is VerificationStatus.VERIFIED, "夹具的 quote 应当能匹配"
    assert anchor is not None and derived is not None

    containing = [
        entry
        for entry in entrieset.entries
        if entry.char_start <= anchor.char_start and anchor.char_end <= entry.char_end
    ]
    assert len(containing) == 1, (
        f"quote 应当恰好落在一个条目内，实际 {len(containing)} 个；"
        f"anchor=[{anchor.char_start},{anchor.char_end}) "
        f"entries={[e.span for e in entrieset.entries]}"
    )
    entry = containing[0]
    assert entry.title.startswith("第一条")

    # 活对照：条目**区间**确实包含那段文字（去标记后逐字符可查）
    plain = entry.content_slice(entrieset.feed_text)
    assert quote in plain


def test_quote_anchor_does_not_depend_on_the_entry_layer_at_all() -> None:
    """T-107 的校验入口只看 `(raw_bytes, quote)` —— 条目层完全不参与。"""
    outcome = verify_quote(
        raw_id=RAW_ID, raw_bytes=RSS_BYTES, quote="不输出坐标", content_type=CONTENT_TYPE
    )
    assert outcome.status is VerificationStatus.VERIFIED
    assert outcome.anchor is not None
    assert len(outcome.anchor.raw_sha256) == 64

    entrieset = _entrieset()
    containing = [
        entry
        for entry in entrieset.entries
        if entry.char_start <= outcome.anchor.char_start
        and outcome.anchor.char_end <= entry.char_end
    ]
    assert len(containing) == 1
    assert containing[0].title.startswith("第二条")
