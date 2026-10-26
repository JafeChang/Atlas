"""T-206 判据 5：确定性 ID 与策略记录。

对应 `src/atlas/chunk/ids.py`：

1. `chunk_id` 与 T-002 的 ID 策略**同构**（前缀 + 32 位十六进制），且能独立重算；
2. ID 由 `(raw_id, 策略版本, 区间, 文本)` 共同决定 —— 改任何一项都换 ID；
3. 非法入参响亮失败，不返回编造结果。
"""

from __future__ import annotations

import re

import pytest

from atlas.chunk import (
    CHUNK_ID_PREFIX,
    ChunkError,
    chunk,
    chunk_id_for,
    chunk_normalized,
    policy_fingerprint,
)
from atlas.chunk.policy import ChunkPolicy, SeparatorSpec
from atlas.contracts.ids import raw_id_for, normalize_for_id
from atlas.normalize import normalize

# 与 T-002 `atlas.contracts.ids` 里 `raw_id_for` 完全同构：前缀 + 32 位十六进制。
_ID_SHAPE = re.compile(r"^[a-z]{3,4}_[0-9a-f]{32}$")

TEXT = (
    "第一段：分块是 raw 的纯函数，可重建。\n\n"
    "第二段：分块 ID 是派生量，永不作人工产物锚点。\n\n"
    "第三段：偏移映射必须能回环到原文。\n"
)


def _chunk_set(policy: ChunkPolicy | None = None, raw_id: str = "raw_test"):
    normalized = normalize(TEXT.encode("utf-8"), "text/plain; charset=utf-8")
    return normalized, chunk_normalized(
        normalized, raw_id=raw_id, policy=policy or ChunkPolicy()
    )


def _chunks(policy: ChunkPolicy | None = None, raw_id: str = "raw_test"):
    return _chunk_set(policy, raw_id)[1]


# ---------------------------------------------------------------------------
# 判据 5.1 / 5.2：形状与可独立重算
# ---------------------------------------------------------------------------


def test_criterion5_2_chunk_id_shape_matches_t002_id_policy() -> None:
    """`chk_` + 32 位十六进制：与 `raw_` / `clm_` / `lbl_` 同构。

    这条**同时**是判据 3.1 的结构性依据：`EvidenceAnchor.raw_sha256` 要求
    `^[0-9a-f]{64}$`，`chk_…` 既带前缀、长度也不对，因此永远不可能被当成
    证据真值锚点使用。
    """
    chunkset = _chunks()
    assert chunkset.chunks, "夹具应当产出分块"
    for item in chunkset.chunks:
        assert item.chunk_id.startswith(CHUNK_ID_PREFIX)
        assert _ID_SHAPE.match(item.chunk_id), item.chunk_id

    # 同构性用 T-002 的公开入口钉死：两者都恰好 36 字符（4 前缀 + 32 摘要）。
    reference = raw_id_for("channel", "https://example.com/feed", "a" * 64)
    assert len(reference) == len(chunkset.chunks[0].chunk_id) == 36


def test_criterion5_3_verify_ids_recomputes_independently() -> None:
    chunkset = _chunks()
    assert chunkset.verify_ids() is True
    for item in chunkset.chunks:
        expected = chunk_id_for(
            raw_id=chunkset.raw_id,
            policy_version=item.policy_version,
            normalized_start=item.normalized_start,
            normalized_end=item.normalized_end,
            text=item.text,
        )
        assert expected == item.chunk_id


def test_criterion5_1_id_is_content_addressed_not_position_only() -> None:
    """篡改任一分量都会改变 ID（内容寻址，不靠编号）。"""
    base = dict(
        raw_id="raw_test",
        policy_version="chunk-policy-v1",
        normalized_start=0,
        normalized_end=5,
        text="abcde",
    )
    reference = chunk_id_for(**base)
    assert chunk_id_for(**{**base, "raw_id": "raw_other"}) != reference
    assert chunk_id_for(**{**base, "policy_version": "chunk-policy-v2"}) != reference
    assert chunk_id_for(**{**base, "normalized_start": 1, "normalized_end": 6}) != reference
    assert chunk_id_for(**{**base, "text": "abcdf"}) != reference


def test_criterion5_1_id_ignores_whitespace_only_differences() -> None:
    """ID 用 T-002 的 `normalize_for_id` 口径：只压缩空白，不做语义清洗。"""
    left = chunk_id_for(
        raw_id="r", policy_version="v", normalized_start=0, normalized_end=5, text="a  b\nc"
    )
    right = chunk_id_for(
        raw_id="r", policy_version="v", normalized_start=0, normalized_end=5, text="a b c"
    )
    assert left == right
    assert normalize_for_id("a  b\nc") == "a b c"


def test_criterion5_1_same_input_same_id_across_calls() -> None:
    _, first = _chunk_set()
    _, second = _chunk_set()
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]


# ---------------------------------------------------------------------------
# 判据 2.3：策略版本变化必须改变 ID
# ---------------------------------------------------------------------------


def test_criterion2_3_different_policy_version_changes_ids_but_keeps_spans() -> None:
    """同一文本 + 同一参数，只改 `version` → ID 全变，边界不变。

    这正是"分块 ID 不能作锚点"的机制证明：换策略版本后，同一段文字得到
    不同的 ID，任何以分块 ID 为锚的人工产物都会静默漂移。
    """
    v1 = _chunks(ChunkPolicy(version="chunk-policy-v1", target_chars=40, max_chars=80))
    v2 = _chunks(ChunkPolicy(version="chunk-policy-v2", target_chars=40, max_chars=80))
    assert [c.span for c in v1.chunks] == [c.span for c in v2.chunks], "参数相同，边界应当相同"
    assert [c.chunk_id for c in v1.chunks] != [c.chunk_id for c in v2.chunks]


def test_criterion2_3_different_parameters_change_ids_and_spans() -> None:
    small = _chunks(ChunkPolicy(target_chars=40, max_chars=80))
    large = _chunks(ChunkPolicy(target_chars=200, max_chars=400))
    assert [c.chunk_id for c in small.chunks] != [c.chunk_id for c in large.chunks]
    assert [c.span for c in small.chunks] != [c.span for c in large.chunks]


def test_criterion5_3_policy_fingerprint_is_stable_and_distinguishing() -> None:
    left = ChunkPolicy(target_chars=100, max_chars=200, overlap_chars=0)
    right = ChunkPolicy(target_chars=100, max_chars=200, overlap_chars=5)
    assert policy_fingerprint(left.snapshot_fields()) == policy_fingerprint(left.snapshot_fields())
    assert policy_fingerprint(left.snapshot_fields()) != policy_fingerprint(right.snapshot_fields())
    assert len(policy_fingerprint(left.snapshot_fields())) == 64


def test_criterion5_3_chunk_set_exposes_policy_snapshot_and_fingerprint() -> None:
    policy = ChunkPolicy(target_chars=50, max_chars=90, overlap_chars=7, version="chunk-policy-v9")
    chunkset = _chunks(policy)
    assert chunkset.policy is policy
    assert chunkset.policy_version == "chunk-policy-v9"
    assert chunkset.policy_fingerprint == policy_fingerprint(policy.snapshot_fields())


# ---------------------------------------------------------------------------
# 非法入参：响亮失败
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"raw_id": ""},
        {"policy_version": ""},
        {"normalized_start": -1},
        {"normalized_end": 0},
        {"text": "   "},
    ],
)
def test_criterion5_1_invalid_id_inputs_fail_loudly(overrides: dict[str, object]) -> None:
    base: dict[str, object] = dict(
        raw_id="r",
        policy_version="v",
        normalized_start=0,
        normalized_end=3,
        text="abc",
    )
    with pytest.raises(ChunkError):
        chunk_id_for(**{**base, **overrides})  # type: ignore[arg-type]


def test_criterion5_1_policy_fingerprint_rejects_empty_fields() -> None:
    """空字段会让摘要歧义（"a" + "" 与 "a"）→ 必须响亮失败，而不是算出一个坏摘要。"""
    with pytest.raises(ChunkError):
        policy_fingerprint(("a", ""))
    with pytest.raises(ChunkError):
        policy_fingerprint(())
    with pytest.raises(ChunkError):
        policy_fingerprint(("a", 1))  # type: ignore[arg-type]


def test_criterion5_1_separator_patterns_cannot_contain_delimiter_ambiguity() -> None:
    """分隔符 pattern 里带 `|` 是合法的（正则），且策略指纹必须区分不同 pattern。"""
    left = ChunkPolicy(separators=(SeparatorSpec(name="a", kind="after", pattern=r"\n+"),))
    right = ChunkPolicy(separators=(SeparatorSpec(name="a", kind="after", pattern=r"\n\n"),))
    assert policy_fingerprint(left.snapshot_fields()) != policy_fingerprint(right.snapshot_fields())


# ---------------------------------------------------------------------------
# 与 chunk 入口的一致性
# ---------------------------------------------------------------------------


def test_ids_from_convenience_entry_match_chunk_normalized() -> None:
    normalized = normalize(TEXT.encode("utf-8"), "text/plain; charset=utf-8")
    policy = ChunkPolicy(target_chars=60, max_chars=120)
    direct = chunk_normalized(normalized, raw_id="raw_x", policy=policy)
    _, via_bytes = chunk(
        TEXT.encode("utf-8"), "text/plain; charset=utf-8", raw_id="raw_x", policy=policy
    )
    assert [c.chunk_id for c in direct.chunks] == [c.chunk_id for c in via_bytes.chunks]
