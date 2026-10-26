"""T-206 判据 7.1 / 7.2：策略参数化 + 非法参数在构造期响亮失败。

"默认值不写进文档就等于没有默认值"（SPEC §2.12）—— 这里同时钉死默认值本身。
"""

from __future__ import annotations

import re

import pytest

from atlas.chunk import (
    DEFAULT_POLICY,
    DEFAULT_SEPARATORS,
    MIN_FILL_RATIO,
    SEPARATOR_KINDS,
    ChunkPolicy,
    ChunkPolicyError,
    SeparatorSpec,
    policy_from,
)

# ---------------------------------------------------------------------------
# 判据 7.1：参数化 + 显式默认值
# ---------------------------------------------------------------------------


def test_criterion7_1_defaults_are_in_code() -> None:
    assert DEFAULT_POLICY.version == "chunk-policy-v1"
    assert DEFAULT_POLICY.target_chars == 1000
    assert DEFAULT_POLICY.max_chars == 1600
    assert DEFAULT_POLICY.overlap_chars == 0
    assert DEFAULT_POLICY.separators is DEFAULT_SEPARATORS
    assert ChunkPolicy() == DEFAULT_POLICY


def test_criterion7_1_separator_priority_is_declared_and_ordered() -> None:
    """分隔符是有序的：段落 > 句末 > 换行 > 列表项标记。"""
    assert [s.name for s in DEFAULT_SEPARATORS] == [
        "paragraph",
        "sentence",
        "newline",
        "list-item",
    ]
    assert [s.kind for s in DEFAULT_SEPARATORS] == ["after", "after", "after", "before"]
    for spec in DEFAULT_SEPARATORS:
        assert spec.kind in SEPARATOR_KINDS
        assert spec.regex.pattern == spec.pattern


def test_criterion7_1_policy_is_frozen_and_hashable() -> None:
    policy = ChunkPolicy()
    with pytest.raises(Exception):
        policy.target_chars = 1  # type: ignore[misc]
    assert {policy, ChunkPolicy()} == {policy}


def test_criterion7_1_policy_from_overrides_only_known_fields() -> None:
    policy = policy_from(target_chars=50, max_chars=80)
    assert (policy.target_chars, policy.max_chars) == (50, 80)
    assert policy.version == DEFAULT_POLICY.version
    with pytest.raises(ChunkPolicyError):
        policy_from(unknown_field=1)  # type: ignore[call-arg]


def test_criterion7_1_min_fill_derives_from_target() -> None:
    assert ChunkPolicy(target_chars=1000).min_fill_chars == int(1000 * MIN_FILL_RATIO)
    assert ChunkPolicy(target_chars=1, max_chars=1).min_fill_chars == 1


# ---------------------------------------------------------------------------
# 判据 7.2：非法参数 → ChunkPolicyError（构造期，不是运行期）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"target_chars": 0}, "target 必须 > 0"),
        ({"target_chars": -10}, "target 不得为负"),
        ({"max_chars": 0}, "max 必须 > 0"),
        ({"max_chars": 10, "target_chars": 20}, "max < target"),
        ({"overlap_chars": -1}, "overlap 不得为负"),
        ({"target_chars": 100, "max_chars": 100, "overlap_chars": 100}, "overlap == target"),
        ({"target_chars": 100, "max_chars": 100, "overlap_chars": 101}, "overlap > target"),
        ({"target_chars": 100, "max_chars": 100, "overlap_chars": 1000}, "overlap 远大于 target"),
        ({"max_chars": 100, "target_chars": 200}, "max < target"),
        ({"version": ""}, "version 不得为空"),
        ({"target_chars": True}, "bool 不是 int"),
        ({"separators": ()}, "separators 不得为空"),
        ({"separators": "not-a-tuple"}, "separators 必须是 tuple"),
    ],
)
def test_criterion7_2_illegal_policy_parameters_fail_at_construction(
    kwargs: dict[str, object], reason: str
) -> None:
    with pytest.raises(ChunkPolicyError):
        ChunkPolicy(**kwargs)  # type: ignore[arg-type]


def test_criterion7_2_overlap_must_be_strictly_less_than_target() -> None:
    """`overlap_chars >= target_chars` 会让分块循环永不前进 —— 必须构造期拒绝。"""
    ChunkPolicy(target_chars=10, max_chars=10, overlap_chars=9)  # 合法
    with pytest.raises(ChunkPolicyError, match="死循环"):
        ChunkPolicy(target_chars=10, max_chars=10, overlap_chars=10)
    with pytest.raises(ChunkPolicyError):
        ChunkPolicy(target_chars=10, max_chars=10, overlap_chars=11)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "", "kind": "after", "pattern": r"\n"},
        {"name": "x", "kind": "nope", "pattern": r"\n"},
        {"name": "x", "kind": "after", "pattern": ""},
        {"name": "x", "kind": "after", "pattern": "("},
        {"name": "x", "kind": "after", "pattern": "*"},
        {"name": "x", "kind": "after", "pattern": "a*"},
        {"name": "x", "kind": "after", "pattern": "^"},
    ],
)
def test_criterion7_2_bad_separator_specs_raise(kwargs: dict[str, str]) -> None:
    """非法正则 / 未知 kind / 命中空串的 pattern 都在构造 `SeparatorSpec` 时就炸。"""
    with pytest.raises(ChunkPolicyError):
        SeparatorSpec(**kwargs)  # type: ignore[arg-type]


def test_criterion7_2_duplicate_separator_names_rejected() -> None:
    duplicate = (
        SeparatorSpec(name="same", kind="after", pattern=r"\n"),
        SeparatorSpec(name="same", kind="before", pattern=r"(?=x)"),
    )
    with pytest.raises(ChunkPolicyError, match="唯一"):
        ChunkPolicy(separators=duplicate)


def test_criterion7_2_valid_custom_separator_accepted() -> None:
    custom = (SeparatorSpec(name="custom", kind="after", pattern=r";\s*"),)
    policy = ChunkPolicy(target_chars=10, max_chars=20, separators=custom)
    assert policy.separators == custom
    assert policy.break_positions("aa; bb; cc", 0, 10) == [4, 8]


def test_criterion7_2_invalid_search_window_fails_loudly() -> None:
    """入参按 `len(text)` 夹紧；夹紧后仍非法的窗口（`start >= target_end`）必须抛错。"""
    policy = ChunkPolicy(target_chars=10, max_chars=20)
    with pytest.raises(ChunkPolicyError):
        policy.find_break("abcdefghij", 5, 3, 8)
    with pytest.raises(ChunkPolicyError):
        policy.find_break("", 0, 5, 20)
    # `max_end` 超过文末会被夹紧，不抛错（调用方可以直接传 start + max_chars）
    assert policy.find_break("abcdefghij", 0, 5, 50)[0] == 10


# ---------------------------------------------------------------------------
# 判据 7.5 的机制部分：分隔符命中位置
# ---------------------------------------------------------------------------


def test_break_positions_respect_priority_and_kind() -> None:
    policy = ChunkPolicy(target_chars=1000, max_chars=2000)
    text = "para one\n\n- item a\n- item b\nline\n\nlast"
    positions = policy.break_positions(text, 0, len(text))
    assert positions == sorted(set(positions))
    assert all(0 < p <= len(text) for p in positions)
    # 段落断点在段落之后（`after` 语义）："para one\n\n" 之后 = 9
    assert 9 in positions
    # 列表项断点在标记之前（`before` 语义）：断点 = 标记前那个换行
    assert 10 in positions
    assert text[10] == "-"
    assert text[9] == "\n"
    # 命中位置全部落在请求区间内
    for lower, upper in [(0, 5), (11, 20), (25, 30)]:
        for position in policy.break_positions(text, lower, upper):
            assert lower <= position <= upper


def test_find_break_prefers_higher_priority() -> None:
    """目标窗口内存在段末时选段末（即使句末更靠近 `target_chars`）。"""
    text = "第一句很短。\n\n第二句也很短。第三句继续。第四句收尾。"
    policy = ChunkPolicy(target_chars=12, max_chars=80)
    end, hit = policy.find_break(text, 0, 12, len(text))
    assert hit == "paragraph"
    assert text[:end].endswith("\n\n")
    assert text[:end].rstrip() == "第一句很短。"


def test_find_break_extends_window_when_target_has_no_break() -> None:
    """目标窗口内没有分隔符 → 放大到硬上限后仍能找到可读边界（`min_fill` 仍生效）。"""
    # 段末在 16；target 窗口 [11, 15] 内够不到它，放大到 [11, 40] 后命中。
    text = "长句子一二三四五。\n\n短句。"
    assert text.index("\n\n") == 9, "夹具前提：段末紧跟在第一句后"
    policy = ChunkPolicy(target_chars=15, max_chars=40)
    end, hit = policy.find_break(text, 0, 15, len(text))
    assert hit == "paragraph"
    assert end == text.index("\n\n") + 2
    assert text[:end].rstrip() == "长句子一二三四五。"


def test_find_break_does_not_accept_break_before_min_fill() -> None:
    """紧挨起点的段末不算边界（否则会切出"。" 这样的碎块）。"""
    text = "。\n\n第二段：这是一段足够长的正文内容。"
    policy = ChunkPolicy(target_chars=30, max_chars=60)
    end, hit = policy.find_break(text, 0, 30, len(text))
    assert hit == "hard-break"
    assert end >= policy.min_fill_chars
    assert text[:end].strip()


def test_find_break_falls_back_to_sentence_when_no_paragraph() -> None:
    """没有段末时下降到句末（同一窗口内取最后一个句末）。"""
    text = "第一句很短。 第二句也很短。 第三句继续。 第四句收尾。"
    policy = ChunkPolicy(target_chars=18, max_chars=40)
    end, hit = policy.find_break(text, 0, 18, len(text))
    assert hit == "sentence"
    assert text[:end] == "第一句很短。 第二句也很短。 "
    assert text[: end - 1].endswith("。")


def test_find_break_takes_last_hit_of_chosen_priority() -> None:
    """同一优先级内取窗口内**最后**一个命中（这一块尽量长）。"""
    text = "aa xx\n\nbb yy\n\ncc zz\n\ndd ww\n\nee vv\n\nff"
    policy = ChunkPolicy(target_chars=14, max_chars=40)
    end, hit = policy.find_break(text, 0, 14, len(text))
    assert hit == "paragraph"
    # `[7, 14]` 内的段落断点是 "bb yy" 之后（14）→ 取它
    assert end == 14
    assert text[:end] == "aa xx\n\nbb yy\n\n"


def test_find_break_falls_back_to_hard_break_at_whitespace() -> None:
    """目标窗口内无分隔符、窗口末尾是空白 → 退到最后一个非空白（不切断单词）。"""
    policy = ChunkPolicy(target_chars=120, max_chars=140)
    text = ("lorem ipsum dolor sit amet " * 10).strip()
    end, hit = policy.find_break(text, 0, 120, 140)
    assert hit == "hard-break"
    assert not text[end - 1].isspace()
    assert end == len(text) or text[end].isspace()


def test_find_break_does_not_split_tokens_at_hard_break() -> None:
    """硬切断点只落在 token 边界：断点两侧不会出现"同一个 token 的两半"。"""
    import re

    policy = ChunkPolicy(target_chars=10, max_chars=20)
    text = "alpha beta gamma delta epsilon"
    end, hit = policy.find_break(text, 0, 10, 20)
    assert hit == "hard-break"
    # 断点两边至少一侧是空白（即不在 token 内部切）
    assert text[end - 1].isspace() or text[end].isspace()
    tokens = re.findall(r"\S+", text)
    for token in tokens:
        for match in re.finditer(re.escape(token), text):
            if match.start() < end < match.end():
                raise AssertionError(f"断点 {end} 切断了 token {token!r}")


def test_find_break_hard_cuts_inside_unbreakable_token_when_no_whitespace_in_window() -> None:
    """窗口内没有任何空白（单个超长词）→ 只能在 `max_end` 处切。"""
    policy = ChunkPolicy(target_chars=10, max_chars=20)
    text = "A" * 100
    end, hit = policy.find_break(text, 0, 10, 20)
    assert (end, hit) == (20, "hard-break")


def test_find_break_hard_cuts_inside_unbreakable_token() -> None:
    """单个超长 token（无空白、无分隔符）→ 只能在 `max_chars` 处硬切。"""
    policy = ChunkPolicy(target_chars=10, max_chars=20)
    text = "A" * 100
    end, hit = policy.find_break(text, 0, 10, 20)
    assert (end, hit) == (20, "hard-break")


def test_find_break_never_exceeds_hard_limit() -> None:
    """返回值契约：断点恒在 `(start, max_end]` 内（本块长度受硬上限约束）。"""
    policy = ChunkPolicy(target_chars=7, max_chars=13)
    samples = [
        "alpha beta gamma delta",
        "a\n\n b\n c d e f g h i j",
        "x" * 50,
        "one two  three   four    five",
        "中文没有空格但是有句号。第二句来了。第三句。",
    ]
    for text in samples:
        for start in range(len(text)):
            max_end = min(start + policy.max_chars, len(text))
            target_end = min(start + policy.target_chars, max_end)
            if max_end <= start or target_end <= start:
                continue
            end, _hit = policy.find_break(text, start, target_end, max_end)
            assert start < end <= max_end, (text, start, end, max_end)
            assert end == len(text) or text[start:end].strip(), (text, start, end)


def test_snap_start_keeps_position_inside_bounds() -> None:
    policy = ChunkPolicy(target_chars=20, max_chars=40, overlap_chars=5)
    text = "word " * 20
    for start in range(0, 20):
        for end in range(start + 1, min(start + 40, len(text)) + 1):
            snapped = policy.snap_start(text, start + 1, end, end - policy.overlap_chars)
            assert start < snapped <= end


def test_snap_start_aligns_to_break_when_falling_inside_whitespace() -> None:
    """重叠回退落进分隔符里时，起点应对齐到 `[lower, position]` 内最后一个断点。"""
    policy = ChunkPolicy(target_chars=20, max_chars=40, overlap_chars=5)
    text = "aaa\n\nbbb ccc ddd"
    # candidate=4 → text[4]="\n"（空白）→ 区间 [1,4] 内最后的断点是换行之后（4）
    assert policy.snap_start(text, 1, 5, 4) == 4
    # candidate=2 → text[2]="a"（非空白）→ 原样返回，不做对齐
    assert policy.snap_start(text, 1, 5, 2) == 2
    # candidate=0 → 夹到 lower=1
    assert policy.snap_start(text, 1, 5, 0) == 1


def test_separator_regex_compiles_once_and_is_reused() -> None:
    spec = SeparatorSpec(name="x", kind="after", pattern=r"\n+")
    assert isinstance(spec.regex, re.Pattern)
    assert spec.regex is spec.regex
