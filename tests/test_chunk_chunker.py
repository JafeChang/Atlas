"""T-206 判据 1 / 2 / 4 / 7：纯函数、可重建、偏移回环、边界与覆盖。

对应 `src/atlas/chunk/chunker.py`。所有断言都基于 `NormalizedText` 的**真实**
偏移映射与分块区间，不测"函数能跑"。
"""

from __future__ import annotations

import re

import pytest

from atlas.chunk import (
    ChunkError,
    ChunkPolicy,
    ChunkSet,
    chunk,
    chunk_normalized,
)
from atlas.normalize import normalize

HTML_SAMPLE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Atlas 分块</title></head>
<body>
<h1>证据锚点</h1>
<p>第一段：证据锚点是 raw 的字符区间，不可变真值。这一段有若干句。第二句在这里。</p>
<p>第二段：分块是派生物，可重建。永远不作人工产物锚点。</p>
<div>第三段：<br>换行之后还有内容。</div>
</body></html>
"""

PLAIN_SAMPLE = (
    "标题行\n\n"
    "第一段。这里有几句。第二句。第三句。\n\n"
    "第二段：\n- 列表项一\n- 列表项二\n\n"
    "结尾段落。\n"
)


def _normalize_bytes(payload: bytes, content_type: str = "text/plain; charset=utf-8"):
    return normalize(payload, content_type)


def _chunkset(
    payload: bytes,
    policy: ChunkPolicy | None = None,
    content_type: str = "text/plain; charset=utf-8",
    raw_id: str = "raw_unit",
) -> tuple[object, ChunkSet]:
    normalized = _normalize_bytes(payload, content_type)
    return normalized, chunk_normalized(
        normalized, raw_id=raw_id, policy=policy or ChunkPolicy()
    )


def _assert_no_gaps(text: str, chunkset: ChunkSet) -> None:
    assert chunkset.uncovered_non_whitespace(text) == []
    for index in range(1, len(chunkset.chunks)):
        previous = chunkset.chunks[index - 1]
        current = chunkset.chunks[index]
        assert current.normalized_start > previous.normalized_start
        assert current.normalized_end > current.normalized_start


# ---------------------------------------------------------------------------
# 判据 1：纯函数 / 逐字节可重现
# ---------------------------------------------------------------------------


def test_criterion1_1_two_calls_are_byte_identical() -> None:
    policy = ChunkPolicy(target_chars=40, max_chars=80, overlap_chars=7)
    _, first = _chunkset(HTML_SAMPLE.encode("utf-8"), policy, "text/html; charset=utf-8")
    _, second = _chunkset(HTML_SAMPLE.encode("utf-8"), policy, "text/html; charset=utf-8")
    assert first == second
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]
    assert [c.text for c in first.chunks] == [c.text for c in second.chunks]
    assert first.policy == second.policy


def test_criterion1_1_chunk_set_repr_is_stable() -> None:
    policy = ChunkPolicy(target_chars=30, max_chars=60)
    _, first = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    _, second = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    assert repr(first) == repr(second)


@pytest.mark.parametrize("module_name", ["errors", "ids", "policy", "chunker", "__init__"])
def test_criterion1_2_no_io_or_nondeterminism_imports(module_name: str) -> None:
    """源码扫描 import 语句：不含 I/O、时钟、随机、环境相关模块。

    （直接搜文本会命中文档字符串里那句"不引入 chardet"式的自述，
    因此只检查 import 行 —— 与 `test_normalize.py` 判据 6 的做法一致。）
    """
    import importlib

    module = importlib.import_module(f"atlas.chunk.{module_name}")
    source = open(module.__file__, encoding="utf-8").read()
    import_lines = [
        line for line in source.splitlines() if re.match(r"^\s*(import|from)\s", line)
    ]
    banned = (
        "random",
        "secrets",
        "time",
        "datetime",
        "os",
        "pathlib",
        "socket",
        "tempfile",
        "subprocess",
        "shutil",
        "uuid",
        "functools",
    )
    for line in import_lines:
        for token in banned:
            assert not re.search(rf"\b{token}\b", line), f"{module_name}: 不应 import {token}：{line.strip()}"


def test_criterion1_2_chunk_records_have_no_time_fields() -> None:
    import dataclasses

    _, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"))
    assert chunkset.chunks
    fields = {f.name for f in dataclasses.fields(chunkset.chunks[0])}
    assert not (fields & {"created_at", "produced_at", "fetched_at", "timestamp"})
    assert "created_at" not in {f.name for f in dataclasses.fields(chunkset)}


def test_criterion1_3_chunk_set_is_frozen() -> None:
    import dataclasses

    _, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunkset.chunks = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunkset.raw_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 判据 2：可重建
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8"),
        (PLAIN_SAMPLE.encode("utf-8"), "text/plain; charset=utf-8"),
        (PLAIN_SAMPLE.encode("utf-8"), ""),
    ],
)
def test_criterion2_1_rebuild_from_raw_bytes_is_identical(payload: bytes, content_type: str) -> None:
    """丢弃分块结果，从 raw 重新 normalize + chunk → 完全相同。"""
    policy = ChunkPolicy(target_chars=60, max_chars=120, overlap_chars=9)
    normalized, first = _chunkset(payload, policy, content_type)

    # 完全丢弃：不保留任何分块中间态，重新走一遍 normalize → chunk
    fresh_normalized = _normalize_bytes(payload, content_type)
    rebuilt = chunk_normalized(fresh_normalized, raw_id="raw_unit", policy=policy)

    assert rebuilt == first
    assert [c.chunk_id for c in rebuilt.chunks] == [c.chunk_id for c in first.chunks]
    # 文本与映射也必须逐字符相同
    assert fresh_normalized.text == normalized.text
    assert fresh_normalized.raw_text == normalized.raw_text


def test_criterion2_2_chunk_set_records_policy_and_version() -> None:
    policy = ChunkPolicy(version="chunk-policy-v7", target_chars=25, max_chars=50)
    _, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    assert chunkset.policy is policy
    assert chunkset.policy_version == "chunk-policy-v7"
    assert all(c.policy_version == "chunk-policy-v7" for c in chunkset.chunks)
    assert chunkset.policy_fingerprint


def test_criterion2_1_rebuild_under_different_raw_id_changes_only_ids() -> None:
    """`raw_id` 参与 ID，不参与切分：换 raw_id 区间不变、ID 全变。**

    这正说明分块 ID 是"内容 + 命名空间"的派生量，而不是事实。
    """
    payload = PLAIN_SAMPLE.encode("utf-8")
    _, left = _chunkset(payload, raw_id="raw_a")
    _, right = _chunkset(payload, raw_id="raw_b")
    assert [c.span for c in left.chunks] == [c.span for c in right.chunks]
    assert [c.chunk_id for c in left.chunks] != [c.chunk_id for c in right.chunks]


# ---------------------------------------------------------------------------
# 判据 4：偏移可回环
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "content_type"),
    [
        (HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8"),
        (PLAIN_SAMPLE.encode("utf-8"), "text/plain; charset=utf-8"),
        ("实体：AT&amp;T 与 &#39;引号&#39; 与 &lt;tag&gt;，还有 &mdash; 破折号。\n\n第二段。".encode("utf-8"), "text/html; charset=utf-8"),
    ],
)
def test_criterion4_text_and_raw_round_trip(payload: bytes, content_type: str) -> None:
    policy = ChunkPolicy(target_chars=45, max_chars=90, overlap_chars=5)
    normalized, chunkset = _chunkset(payload, policy, content_type)
    assert chunkset.chunks, "夹具应当产出分块"

    for item in chunkset.chunks:
        # 4.1 归一化切片与分块文本逐字符一致且非空
        assert normalized.text[item.normalized_start : item.normalized_end] == item.text
        assert item.text.strip()
        # 4.2 原文区间由段表映射得到，单调且不越界
        assert item.raw_start == normalized.to_raw_offset(item.normalized_start)
        assert item.raw_end == normalized.to_raw_offset(item.normalized_end)
        assert 0 <= item.raw_start < item.raw_end <= len(normalized.raw_text)
        # `raw_slice` 与记录的原文区间一致
        assert item.raw_slice(normalized) == normalized.raw_text[item.raw_start : item.raw_end]
        # 4.3 映射单调
        assert normalized.to_raw_offset.to_raw_range(item.normalized_start, item.normalized_end) == (
            item.raw_start,
            item.raw_end,
        )

    # 4.3 分块序列上两个起点都单调不减
    starts = [c.normalized_start for c in chunkset.chunks]
    raw_starts = [c.raw_start for c in chunkset.chunks]
    assert starts == sorted(starts)
    assert raw_starts == sorted(raw_starts)
    assert chunkset.verify_offsets(normalized) is True


def test_criterion4_2_mapping_is_monotonic_over_whole_table() -> None:
    normalized, _ = _chunkset(HTML_SAMPLE.encode("utf-8"), content_type="text/html; charset=utf-8")
    assert normalized.to_raw_offset.is_monotonic() is True


def test_criterion4_chunk_text_equals_its_own_slice_even_with_overlap() -> None:
    policy = ChunkPolicy(target_chars=30, max_chars=60, overlap_chars=12)
    normalized, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    assert any(
        chunkset.chunks[i].normalized_start < chunkset.chunks[i - 1].normalized_end
        for i in range(1, len(chunkset.chunks))
    ), "夹具应当产生重叠"
    for item in chunkset.chunks:
        assert normalized.text[item.normalized_start : item.normalized_end] == item.text


# ---------------------------------------------------------------------------
# 判据 7.3：边界条件
# ---------------------------------------------------------------------------


def test_criterion7_3_empty_text_yields_no_chunks() -> None:
    normalized, chunkset = _chunkset(b"", content_type="text/plain")
    assert normalized.text_length == 0
    assert chunkset.chunks == ()
    assert chunkset.covered_ranges == ()
    assert chunkset.verify_ids() is True


@pytest.mark.parametrize("payload", [b"   ", b"\n\n\n", b" \t \r\n \t ", b"\r\n\r\n"])
def test_criterion7_3_whitespace_only_yields_no_chunks(payload: bytes) -> None:
    _, chunkset = _chunkset(payload, content_type="text/plain")
    assert chunkset.chunks == ()


def test_criterion7_3_single_character() -> None:
    normalized, chunkset = _chunkset("A".encode("utf-8"), content_type="text/plain")
    assert len(chunkset.chunks) == 1
    assert chunkset.chunks[0].span == (0, 1)
    assert chunkset.chunks[0].text == "A"
    assert chunkset.chunks[0].raw_slice(normalized) == "A"


def test_criterion7_3_exact_target_length_is_one_chunk() -> None:
    policy = ChunkPolicy(target_chars=100, max_chars=100)
    payload = ("字" * 100).encode("utf-8")
    _, chunkset = _chunkset(payload, policy, content_type="text/plain")
    assert len(chunkset.chunks) == 1
    assert chunkset.chunks[0].span == (0, 100)


def test_criterion7_3_one_char_over_target_still_splits() -> None:
    """`target + 1`：不能出现"最后一个分块把整篇当尾巴吞掉"的退化。"""
    policy = ChunkPolicy(target_chars=100, max_chars=100)
    _, chunkset = _chunkset(("字" * 101).encode("utf-8"), policy, content_type="text/plain")
    assert len(chunkset.chunks) == 2
    assert chunkset.chunks[0].span == (0, 100)
    assert chunkset.chunks[1].span == (100, 101)


def test_criterion7_3_long_text_without_separators_hard_breaks_within_max() -> None:
    policy = ChunkPolicy(target_chars=120, max_chars=150)
    _, chunkset = _chunkset(("字" * 5000).encode("utf-8"), policy, content_type="text/plain")
    assert len(chunkset.chunks) > 30
    for item in chunkset.chunks:
        assert len(item.text) <= policy.max_chars


def test_criterion7_3_long_text_without_separators_does_not_cut_words() -> None:
    policy = ChunkPolicy(target_chars=200, max_chars=240)
    words = " ".join(f"word{index}" for index in range(600))
    normalized, chunkset = _chunkset(words.encode("utf-8"), policy, content_type="text/plain")
    assert len(chunkset.chunks) > 5
    original_tokens = words.split(" ")
    # 分块**按文档顺序**读出的 token 序列必须与原文完全一致：
    # 没有丢词、没有重复、没有被切断的词。
    # （分块之间允许存在纯空白空隙，因此比较 token 序列而不是逐字符相等。）
    seen: list[str] = []
    for item in chunkset.chunks:
        seen.extend(item.text.split())
    assert seen == original_tokens
    for item in chunkset.chunks:
        assert item.text == normalized.text[item.normalized_start : item.normalized_end]


def test_criterion7_3_overlap_is_a_lower_bound_and_respected() -> None:
    policy = ChunkPolicy(target_chars=40, max_chars=90, overlap_chars=15)
    payload = ("。".join(f"第{index}句内容" for index in range(60)) + "。").encode("utf-8")
    _, chunkset = _chunkset(payload, policy, content_type="text/plain")
    assert len(chunkset.chunks) >= 3
    for index in range(1, len(chunkset.chunks)):
        previous = chunkset.chunks[index - 1]
        current = chunkset.chunks[index]
        assert previous.normalized_end - current.normalized_start >= policy.overlap_chars


def test_criterion7_3_zero_overlap_produces_disjoint_chunks() -> None:
    policy = ChunkPolicy(target_chars=40, max_chars=90, overlap_chars=0)
    normalized, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    for index in range(1, len(chunkset.chunks)):
        previous = chunkset.chunks[index - 1]
        current = chunkset.chunks[index]
        assert current.normalized_start >= previous.normalized_end
        # 无重叠时分块只在空白处相接：跳过的空白不含非空白字符
        assert normalized.text[previous.normalized_end : current.normalized_start].strip() == ""


def test_criterion7_3_invalid_overlap_fails_at_construction_before_chunking() -> None:
    from atlas.chunk import ChunkPolicyError

    with pytest.raises(ChunkPolicyError):
        ChunkPolicy(target_chars=50, max_chars=100, overlap_chars=50)
    # 合法边界：overlap = target - 1 仍然能跑完（不死循环）
    policy = ChunkPolicy(target_chars=50, max_chars=100, overlap_chars=49)
    _, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    assert chunkset.chunks


def test_criterion7_3_chunk_normalized_rejects_wrong_types() -> None:
    normalized, _ = _chunkset(PLAIN_SAMPLE.encode("utf-8"))
    with pytest.raises(ChunkError):
        chunk_normalized("not normalized", raw_id="r")  # type: ignore[arg-type]
    with pytest.raises(ChunkError):
        chunk_normalized(normalized, raw_id="")
    with pytest.raises(ChunkError):
        chunk_normalized(normalized, raw_id="r", policy="not a policy")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 判据 7.4：字符覆盖完整（无空隙）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "content_type", "policy"),
    [
        (HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8", ChunkPolicy(target_chars=30, max_chars=60)),
        (HTML_SAMPLE.encode("utf-8"), "text/html; charset=utf-8", ChunkPolicy(target_chars=30, max_chars=60, overlap_chars=10)),
        (PLAIN_SAMPLE.encode("utf-8"), "text/plain; charset=utf-8", ChunkPolicy(target_chars=17, max_chars=40)),
        (PLAIN_SAMPLE.encode("utf-8"), "text/plain; charset=utf-8", ChunkPolicy(target_chars=500, max_chars=1000)),
        (PLAIN_SAMPLE.encode("utf-8"), "", ChunkPolicy(target_chars=13, max_chars=23)),
    ],
)
def test_criterion7_4_union_covers_all_non_whitespace(
    payload: bytes, content_type: str, policy: ChunkPolicy
) -> None:
    normalized, chunkset = _chunkset(payload, policy, content_type)
    assert chunkset.chunks, "夹具应当产出分块"
    _assert_no_gaps(normalized.text, chunkset)

    # 未覆盖的位置只允许是空白
    for index in chunkset.uncovered_positions(normalized.text):
        assert normalized.text[index].isspace(), (
            f"位置 {index}（{normalized.text[index]!r}）未被任何分块覆盖"
        )
    # 反过来：所有非空白字符都被覆盖
    covered = bytearray(len(normalized.text))
    for start, end in chunkset.covered_ranges:
        covered[start:end] = b"\x01" * (end - start)
    for index, char in enumerate(normalized.text):
        if not char.isspace():
            assert covered[index], f"非空白字符 {char!r}@{index} 未被覆盖"


def test_criterion7_4_chunk_set_rejects_gapped_construction() -> None:
    """`ChunkSet` 构造即校验：起点不严格递增（即"空隙"）必须抛错。"""
    from atlas.chunk import Chunk

    policy = ChunkPolicy()
    first = Chunk(
        chunk_id="chk_" + "a" * 32,
        raw_id="r",
        index=0,
        policy_version=policy.version,
        normalized_start=0,
        normalized_end=5,
        raw_start=0,
        raw_end=5,
        text="abcde",
    )
    second = Chunk(
        chunk_id="chk_" + "b" * 32,
        raw_id="r",
        index=1,
        policy_version=policy.version,
        normalized_start=7,
        normalized_end=10,
        raw_start=7,
        raw_end=10,
        text="fgh",
    )
    # 起点递增是合法的（空隙只允许是空白，由 `chunk_normalized` 用文本判定）
    chunkset = ChunkSet(chunks=(first, second), raw_id="r", policy=policy)
    assert chunkset.verify_ids() is False  # 手工拼的 ID 与内容不符

    # 起点不递增（倒序）→ 构造期响亮失败
    reversed_first = Chunk(
        chunk_id="chk_" + "c" * 32,
        raw_id="r",
        index=0,
        policy_version=policy.version,
        normalized_start=7,
        normalized_end=10,
        raw_start=7,
        raw_end=10,
        text="fgh",
    )
    reversed_second = Chunk(
        chunk_id="chk_" + "d" * 32,
        raw_id="r",
        index=1,
        policy_version=policy.version,
        normalized_start=0,
        normalized_end=5,
        raw_start=0,
        raw_end=5,
        text="abcde",
    )
    with pytest.raises(ChunkError, match="严格递增"):
        ChunkSet(chunks=(reversed_first, reversed_second), raw_id="r", policy=policy)
    with pytest.raises(ChunkError, match="index"):
        ChunkSet(chunks=(second,), raw_id="r", policy=policy)


def test_criterion7_4_chunk_rejects_text_length_mismatch() -> None:
    from atlas.chunk import Chunk

    with pytest.raises(ChunkError, match="长度"):
        Chunk(
            chunk_id="chk_" + "a" * 32,
            raw_id="r",
            index=0,
            policy_version="v",
            normalized_start=0,
            normalized_end=5,
            raw_start=0,
            raw_end=5,
            text="abcd",
        )


def test_criterion7_4_chunk_rejects_whitespace_only_text() -> None:
    from atlas.chunk import Chunk

    with pytest.raises(ChunkError, match="空白"):
        Chunk(
            chunk_id="chk_" + "a" * 32,
            raw_id="r",
            index=0,
            policy_version="v",
            normalized_start=0,
            normalized_end=3,
            raw_start=0,
            raw_end=3,
            text="   ",
        )


def test_criterion7_4_chunk_rejects_non_derived_id_prefix() -> None:
    from atlas.chunk import Chunk

    with pytest.raises(ChunkError, match="chk_"):
        Chunk(
            chunk_id="blk_0001",
            raw_id="r",
            index=0,
            policy_version="v",
            normalized_start=0,
            normalized_end=1,
            raw_start=0,
            raw_end=1,
            text="a",
        )


# ---------------------------------------------------------------------------
# 判据 7.5 / 7.6：可读边界优先 + 长度上界
# ---------------------------------------------------------------------------


def test_criterion7_5_boundaries_land_on_paragraph_breaks() -> None:
    """段落文本上，分块边界必须落在段末（`\\n\\n` 之后）。"""
    paragraphs = [f"第{index}段正文，长度足够构成一个分块。" for index in range(8)]
    text = "\n\n".join(paragraphs) + "\n"
    policy = ChunkPolicy(target_chars=len(paragraphs[0]) + 4, max_chars=len(paragraphs[0]) * 3)
    normalized, chunkset = _chunkset(text.encode("utf-8"), policy, "text/plain")
    assert len(chunkset.chunks) >= 2

    for item in chunkset.chunks[:-1]:
        assert item.text.endswith("\n\n"), f"分块未落在段末：{item.text[-6:]!r}"
    # 每个分块都恰好是整数个段落
    for item in chunkset.chunks:
        assert item.text.rstrip("\n").split("\n\n")[0].startswith("第")
    _assert_no_gaps(normalized.text, chunkset)


def test_criterion7_6_every_chunk_within_max_chars() -> None:
    policies = [
        ChunkPolicy(target_chars=10, max_chars=25),
        ChunkPolicy(target_chars=25, max_chars=25),
        ChunkPolicy(target_chars=40, max_chars=200, overlap_chars=5),
        ChunkPolicy(target_chars=7, max_chars=13),
    ]
    payloads = [
        PLAIN_SAMPLE.encode("utf-8"),
        HTML_SAMPLE.encode("utf-8"),
        ("超长无分隔符" * 300).encode("utf-8"),
    ]
    for policy in policies:
        for payload in payloads:
            content_type = (
                "text/html; charset=utf-8"
                if payload == HTML_SAMPLE.encode("utf-8")
                else "text/plain; charset=utf-8"
            )
            normalized, chunkset = _chunkset(payload, policy, content_type=content_type)
            for item in chunkset.chunks:
                assert len(item.text) <= policy.max_chars, (policy, item.span, len(item.text))
            _assert_no_gaps(normalized.text, chunkset)


def test_criterion7_5_hard_break_keeps_words_whole_for_space_separated_text() -> None:
    """无分隔符但有空格的文本：硬切也只在空白处切，不切断单词。"""
    policy = ChunkPolicy(target_chars=60, max_chars=100)
    text = " ".join(f"token{index}" for index in range(200))
    normalized, chunkset = _chunkset(text.encode("utf-8"), policy, content_type="text/plain")
    assert len(chunkset.chunks) > 3
    original_tokens = text.split(" ")
    # 分块按文档顺序读出的 token 序列与原文一致（没有被切断的词）
    seen: list[str] = []
    for item in chunkset.chunks:
        seen.extend(item.text.split())
    assert seen == original_tokens
    # 分块之间跳过的位置只允许是空白
    _assert_no_gaps(normalized.text, chunkset)


# ---------------------------------------------------------------------------
# 判据 7.7：不依赖 T-104 的派生块
# ---------------------------------------------------------------------------


def test_criterion7_7_chunking_ignores_normalized_blocks() -> None:
    """`normalize` 会附加派生块；把块换成垃圾也必须得到相同的分块。"""
    from atlas.normalize import derive_blocks
    from atlas.normalize.text import normalize as normalize_without_blocks

    payload = HTML_SAMPLE.encode("utf-8")
    normalized = normalize(payload, "text/html; charset=utf-8")
    assert normalized.blocks, "夹具应当带上派生块"

    with_blocks = chunk_normalized(normalized, raw_id="r", policy=ChunkPolicy(target_chars=30, max_chars=60))
    stripped = chunk_normalized(
        normalize_without_blocks(payload, "text/html; charset=utf-8"),
        raw_id="r",
        policy=ChunkPolicy(target_chars=30, max_chars=60),
    )
    polluted = chunk_normalized(
        normalized.with_blocks(()),  # 人为清空派生块
        raw_id="r",
        policy=ChunkPolicy(target_chars=30, max_chars=60),
    )
    assert with_blocks == stripped == polluted
    assert [c.chunk_id for c in with_blocks.chunks] == [c.chunk_id for c in polluted.chunks]


# ---------------------------------------------------------------------------
# 判据 1/2 的入口一致性：chunk() vs chunk_normalized()
# ---------------------------------------------------------------------------


def test_convenience_entry_matches_normalized_entry_exactly() -> None:
    policy = ChunkPolicy(target_chars=35, max_chars=70, overlap_chars=4)
    payload = HTML_SAMPLE.encode("utf-8")
    normalized, via_bytes = chunk(payload, "text/html; charset=utf-8", raw_id="r", policy=policy)
    via_text = chunk_normalized(normalized, raw_id="r", policy=policy)
    assert via_bytes == via_text


def test_chunk_set_reports_total_chars_with_overlap() -> None:
    policy = ChunkPolicy(target_chars=30, max_chars=60, overlap_chars=10)
    normalized, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"), policy)
    assert chunkset.total_chars == sum(len(c.text) for c in chunkset.chunks)
    if len(chunkset.chunks) > 1:
        assert chunkset.total_chars > normalized.text_length


def test_verify_ids_detects_tampering() -> None:
    from atlas.chunk import Chunk

    _, chunkset = _chunkset(PLAIN_SAMPLE.encode("utf-8"))
    good = chunkset.chunks[0]
    assert chunkset.verify_ids() is True
    tampered = Chunk(
        chunk_id=good.chunk_id,
        raw_id=good.raw_id,
        index=0,
        policy_version=good.policy_version,
        normalized_start=good.normalized_start,
        normalized_end=good.normalized_end,
        raw_start=good.raw_start,
        raw_end=good.raw_end,
        text=good.text,
    )
    assert tampered == good, "同内容重建应当得到相同记录"
    forged = ChunkSet(
        chunks=(
            Chunk(
                chunk_id=good.chunk_id,
                raw_id=good.raw_id,
                index=0,
                policy_version=good.policy_version,
                normalized_start=good.normalized_start,
                normalized_end=good.normalized_end,
                raw_start=good.raw_start,
                raw_end=good.raw_end,
                text=good.text,
            ),
        ),
        raw_id=good.raw_id,
        policy=chunkset.policy,
    )
    assert forged.verify_ids() is True
    # 记录 ID 但换成别的文本（区间相同）→ 独立重算必然发现不一致
    inconsistent = ChunkSet(
        chunks=(
            Chunk(
                chunk_id=good.chunk_id,
                raw_id=good.raw_id,
                index=0,
                policy_version=good.policy_version,
                normalized_start=good.normalized_start,
                normalized_end=good.normalized_end,
                raw_start=good.raw_start,
                raw_end=good.raw_end,
                text="x" * len(good.text),
            ),
        ),
        raw_id=good.raw_id,
        policy=chunkset.policy,
    )
    assert inconsistent.verify_ids() is False
