"""T-206 判据 3：分块永不作人工产物锚点（SPEC §2.2 / §5 延后登记 #7）。

这是本任务最硬的一条：分块是**派生**量，把它当证据或标签的锚点必须
**响亮失败**，而不是被静默接受。测试分四层，逐层收紧：

1. `Chunk.as_anchor()` 永远抛 `ChunkNotAnchorError`；
2. `chunk_id`（`chk_…`）过不了 `EvidenceAnchor` 的字段校验（前缀 + 长度都不符）；
3. 把 `Chunk` 塞进需要 `EvidenceAnchor` / `DerivedLocator` 的字段 → pydantic 类型拒绝；
4. **机制证明**：换策略后同一段文字得到不同 `chunk_id` —— 这就是"分块 ID
   不能当锚点"的实证理由，而不是一句声明。
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from atlas.chunk import (
    Chunk,
    ChunkNotAnchorError,
    ChunkPolicy,
    chunk_normalized,
)
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
from atlas.evidence import verify_quote
from atlas.normalize import normalize

TEXT = (
    "第一段：证据锚点是 raw 的字符区间，不可变真值。\n\n"
    "第二段：分块是派生物，可重建，永不作人工产物锚点。\n\n"
    "第三段：quote 由确定性匹配算出坐标，PI 只输出文字。\n"
)
CONTENT = TEXT.encode("utf-8")
CONTENT_TYPE = "text/plain; charset=utf-8"
RAW_ID = "raw_t206"

_CHUNK_FIELDS = {
    "chunk_id",
    "raw_id",
    "index",
    "policy_version",
    "normalized_start",
    "normalized_end",
    "raw_start",
    "raw_end",
    "text",
}


def _chunks(policy: ChunkPolicy | None = None):
    normalized = normalize(CONTENT, CONTENT_TYPE)
    return normalized, chunk_normalized(normalized, raw_id=RAW_ID, policy=policy or ChunkPolicy())


class _AnchorHolder(ContractModel):
    """只为验证"塞错类型会被 pydantic 拒绝"的最小载体。"""

    anchor: EvidenceAnchor


# ---------------------------------------------------------------------------
# 第 1 层：as_anchor() 永远抛错
# ---------------------------------------------------------------------------


def test_criterion3_1_chunk_as_anchor_raises_loudly() -> None:
    _, chunkset = _chunks(ChunkPolicy(target_chars=40, max_chars=80))
    assert chunkset.chunks, "夹具应当产出分块"
    for item in chunkset.chunks:
        with pytest.raises(ChunkNotAnchorError):
            item.as_anchor()


def test_criterion3_1_chunk_not_anchor_error_is_a_contract_error() -> None:
    """错误类型必须挂在 T-002 的 `ContractError` 之下：契约违例只有一套语义。"""
    assert issubclass(ChunkNotAnchorError, ContractError)


def test_criterion3_3_chunk_carries_no_evidence_truth_fields() -> None:
    """`Chunk` 只携带派生定位 + 溯源用的 raw_id；**没有** raw_sha256 之类的真值字段。"""
    _, chunkset = _chunks()
    item = chunkset.chunks[0]
    assert not hasattr(item, "raw_sha256")
    assert not hasattr(item, "char_start")
    assert not hasattr(item, "char_end")
    assert {f.name for f in dataclasses.fields(item)} == _CHUNK_FIELDS


def test_criterion3_3_chunk_locator_is_a_derived_locator() -> None:
    """转换出的定位器是 T-002 的 `DerivedLocator`（明确标注为派生）。"""
    _, chunkset = _chunks()
    item = chunkset.chunks[0]
    locator = item.locator
    assert isinstance(locator, DerivedLocator)
    assert locator.block_id == item.chunk_id
    assert (locator.normalized_start, locator.normalized_end) == item.span


# ---------------------------------------------------------------------------
# 第 2 层：chunk_id 过不了 EvidenceAnchor 的字段校验
# ---------------------------------------------------------------------------


def test_criterion3_1_chunk_id_fails_evidence_anchor_sha_validation() -> None:
    """`chk_…` 既带前缀又不是 64 位十六进制 → 永远过不了 `raw_sha256` 校验。"""
    _, chunkset = _chunks()
    chunk_id = chunkset.chunks[0].chunk_id
    with pytest.raises(ValidationError):
        EvidenceAnchor(raw_id=RAW_ID, raw_sha256=chunk_id, char_start=0, char_end=5)
    with pytest.raises(ValidationError):
        EvidenceAnchor(raw_id=RAW_ID, raw_sha256=chunk_id.upper(), char_start=0, char_end=5)


def test_criterion3_1_chunk_object_rejected_where_anchor_expected() -> None:
    """把 `Chunk` 直接塞进要求 `EvidenceAnchor` / `DerivedLocator` 的字段 → 类型拒绝。"""
    _, chunkset = _chunks()
    item = chunkset.chunks[0]
    with pytest.raises(ValidationError):
        _AnchorHolder(anchor=item)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DerivedLocator(block_id=item, normalized_start=0, normalized_end=1)  # type: ignore[arg-type]


def test_criterion3_1_chunk_id_rejected_as_confirmed_label_anchor() -> None:
    """人工标签的锚点必须是 raw 字符区间；分块 ID 不是锚点。"""
    _, chunkset = _chunks()
    chunk_id = chunkset.chunks[0].chunk_id
    with pytest.raises(ValidationError):
        ConfirmedLabel(
            label_id="lbl_x",
            raw_id=RAW_ID,
            label_key="chunk_ref",
            label_value="1",
            actor="me",
            anchor={
                "raw_id": RAW_ID,
                "raw_sha256": chunk_id,
                "char_start": 0,
                "char_end": 3,
            },
        )
    # 正确做法：标签锚在 raw_id 上（SPEC §2.1 决策 1A）
    label = ConfirmedLabel.human(raw_id=RAW_ID, label_key="industry", label_value="ai", actor="me")
    assert label.raw_id == RAW_ID and label.anchor is None


def test_criterion3_1_empty_chunk_span_cannot_become_anchor() -> None:
    """即便把分块区间换算成 raw 区间，空区间也过不了 `EvidenceAnchor.create`。"""
    _, chunkset = _chunks()
    item = chunkset.chunks[0]
    with pytest.raises(AnchorError):
        EvidenceAnchor.create(
            raw_id=RAW_ID,
            raw_sha256="b" * 64,
            char_start=item.raw_start,
            char_end=item.raw_start,
        )


# ---------------------------------------------------------------------------
# 第 3 层：真值锚点必须由 quote 确定性重算（分块完全不参与）
# ---------------------------------------------------------------------------


def test_criterion3_3_truth_anchor_is_derived_from_quote_not_from_chunk() -> None:
    """真值锚点由 quote 在归一化文本里确定性匹配得到，再映射回原文区间。"""
    normalized, chunkset = _chunks(ChunkPolicy(target_chars=40, max_chars=80))
    quote = "确定性匹配算出坐标"
    status, anchor, derived = build_anchor(
        raw_id=RAW_ID,
        raw_sha256="b" * 64,
        normalized_text=normalized.text,
        quote=quote,
        to_raw_offset=normalized.to_raw_offset,
    )
    assert status is VerificationStatus.VERIFIED
    assert anchor is not None and derived is not None
    assert normalized.raw_slice(derived.normalized_start, derived.normalized_end).find("确定性") >= 0

    # 该 quote 确实落在某个分块内部 —— 但锚点**不依赖**那个分块
    containing = [
        c
        for c in chunkset.chunks
        if c.normalized_start <= derived.normalized_start < c.normalized_end
    ]
    assert containing, "quote 应当落在某个分块内"

    # 换策略重跑分块：分块集合完全不同（这里是 3 块 vs 2 块），
    # 但锚点**逐字段不变**（它锚在 raw 上，不锚在分块上）
    _, other = _chunks(ChunkPolicy(target_chars=10, max_chars=25))
    assert [c.chunk_id for c in other.chunks] != [c.chunk_id for c in chunkset.chunks]
    assert [c.span for c in other.chunks] != [c.span for c in chunkset.chunks]
    again_status, again_anchor, again_derived = build_anchor(
        raw_id=RAW_ID,
        raw_sha256="b" * 64,
        normalized_text=normalized.text,
        quote=quote,
        to_raw_offset=normalized.to_raw_offset,
    )
    assert (again_status, again_anchor, again_derived) == (status, anchor, derived)


def test_criterion3_3_evidence_layer_verifies_quote_independently_of_chunks() -> None:
    """T-107 的校验入口只看 `(raw_bytes, quote)`，分块完全不参与。"""
    outcome = verify_quote(raw_id=RAW_ID, raw_bytes=CONTENT, quote="不可变真值")
    assert outcome.status is VerificationStatus.VERIFIED
    assert outcome.anchor is not None
    assert outcome.anchor.raw_sha256 != "" and len(outcome.anchor.raw_sha256) == 64


# ---------------------------------------------------------------------------
# 第 4 层：机制证明 —— 换策略 → 换 ID → 以 chunk_id 为锚的产物必然漂移
# ---------------------------------------------------------------------------


def test_criterion3_2_same_text_gets_different_chunk_ids_under_different_policies() -> None:
    """同一段文字，两个只差 `target_chars` 的策略给出不同 `chunk_id`。

    这就是"分块 ID 不能作锚点"的机制：任何以分块 ID 为锚的人工产物，
    在换策略/换解析器后都会**静默指向另一段文字**（或直接消失），
    而真值锚点（raw 字符区间）不会。
    """
    normalized = normalize(CONTENT, CONTENT_TYPE)
    small = chunk_normalized(
        normalized, raw_id=RAW_ID, policy=ChunkPolicy(target_chars=20, max_chars=40)
    )
    large = chunk_normalized(
        normalized, raw_id=RAW_ID, policy=ChunkPolicy(target_chars=200, max_chars=400)
    )
    assert len(small.chunks) > len(large.chunks)
    assert {c.chunk_id for c in small.chunks}.isdisjoint({c.chunk_id for c in large.chunks})

    probe = normalized.text.index("分块是派生物")
    left = next(c for c in small.chunks if c.normalized_start <= probe < c.normalized_end)
    right = next(c for c in large.chunks if c.normalized_start <= probe < c.normalized_end)
    assert left.chunk_id != right.chunk_id
    assert left.span != right.span


def test_criterion3_2_chunk_record_is_frozen_against_forging() -> None:
    """冻结记录：不能就地改区间或 ID 来"固化"一个派生量。"""
    _, chunkset = _chunks()
    item = chunkset.chunks[0]
    assert isinstance(item, Chunk)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.normalized_start = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.chunk_id = "chk_forged"  # type: ignore[misc]
