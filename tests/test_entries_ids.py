"""T-130 判据 2 的 ID 层细化：`entry_id` 的确定性、形状与"版本必须参与"。

判据原文见 `tests/test_entries_parser.py` 的模块 docstring。

`entry_id` 的公式（与 T-002 的 `raw_` / `clm_` / `lbl_` 和 T-206 的 `chk_` 同构）：

    entry_id = "ent_" + sha256(raw_id, raw_sha256, parser_version,
                               char_start, char_end, normalized_title)[:32]

本文件逐字段证明**每一个输入都真的参与**：改任何一个，ID 必须变。
"某个字段其实没进 ID"是这类代码最隐蔽的缺陷 —— 它不会报错，只会让身份
在某个维度上悄悄不敏感。
"""

from __future__ import annotations

import hashlib

import pytest

from atlas.contracts.ids import normalize_for_id, raw_id_for
from atlas.entries import (
    ENTRY_ID_PREFIX,
    ENTRY_ID_RE,
    ENTRY_PARSER_VERSION,
    EntryError,
    entry_id_for,
    fingerprint,
)

RAW_ID = "raw_t130_ids"
RAW_SHA = "a" * 64
TITLE = "一篇标题"


def _call(**overrides):
    payload = {
        "raw_id": RAW_ID,
        "raw_sha256": RAW_SHA,
        "parser_version": ENTRY_PARSER_VERSION,
        "char_start": 10,
        "char_end": 20,
        "title": TITLE,
    }
    payload.update(overrides)
    return entry_id_for(**payload)


# =========================================================================== #
# 形状：与 T-002 的 ID 策略同构
# =========================================================================== #


def test_id_shape_is_prefix_plus_32_hex() -> None:
    value = _call()
    assert value.startswith(ENTRY_ID_PREFIX)
    assert ENTRY_ID_RE.match(value)
    assert len(value) == len(ENTRY_ID_PREFIX) + 32
    assert value[4:] == value[4:].lower()
    assert all(char in "0123456789abcdef" for char in value[4:])


def test_id_shape_matches_the_contract_module_digest_convention() -> None:
    """与 `atlas.contracts.ids.raw_id_for` **逐字节**同构（不靠文档承诺）。

    契约模块的摘要助手是私有的，因此本包就地实现了同一形状；这条测试用**公开**
    的 `raw_id_for` 重算一遍同一约定，钉死两份实现不会悄悄漂移。
    """
    channel, endpoint = "chan", "https://example.invalid/feed"
    contract_value = raw_id_for(channel, endpoint, RAW_SHA)
    manual = "raw_" + hashlib.sha256(
        b"\x1f".join(
            part.encode("utf-8") for part in (channel, endpoint, RAW_SHA)
        )
        + b"\x1f"
    ).hexdigest()[:32]
    assert contract_value == manual


def test_id_is_32_hex_of_the_same_digest_when_recomputed_by_hand() -> None:
    """按公式手算一遍，必须与函数输出逐字符相同（实现没有额外盐）。"""
    expected = ENTRY_ID_PREFIX + hashlib.sha256(
        b"\x1f".join(
            part.encode("utf-8")
            for part in (
                RAW_ID,
                RAW_SHA,
                ENTRY_PARSER_VERSION,
                "10",
                "20",
                normalize_for_id(TITLE),
            )
        )
        + b"\x1f"
    ).hexdigest()[:32]
    assert _call() == expected


def test_fingerprint_is_a_full_64_hex_digest() -> None:
    value = fingerprint((ENTRY_PARSER_VERSION, "rss"))
    assert len(value) == 64
    assert value == value.lower()
    assert value == fingerprint((ENTRY_PARSER_VERSION, "rss"))
    assert value != fingerprint((ENTRY_PARSER_VERSION, "atom"))


# =========================================================================== #
# 每一个输入都必须参与 ID
# =========================================================================== #


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_id", "raw_other"),
        ("raw_sha256", "b" * 64),
        ("parser_version", "entry-parser-v2"),
        ("char_start", 11),
        ("char_end", 21),
        ("title", "另一个标题"),
    ],
)
def test_every_input_field_changes_the_id(field: str, value: object) -> None:
    assert _call(**{field: value}) != _call(), f"改动 {field} 没有改变 entry_id"


def test_id_is_stable_across_repeated_calls() -> None:
    assert len({_call() for _ in range(20)}) == 1


def test_id_ignores_only_whitespace_differences_in_the_title() -> None:
    """标题只按 `normalize_for_id` 的口径（压缩空白）参与 —— 这是**有意**的稳定性。

    同一段文字不因换行 / 多空格差异产生两个 ID；但任何非空白差异都必须换 ID。
    """
    left = _call(title="标题   带   多空格")
    right = _call(title="标题 带 多空格")
    assert left == right
    assert _call(title="标题带多空格") != left


# =========================================================================== #
# 非法入参：响亮失败，不返回空串
# =========================================================================== #


@pytest.mark.parametrize(
    "overrides",
    [
        {"raw_id": ""},
        {"raw_id": None},
        {"raw_sha256": ""},
        {"raw_sha256": "A" * 64},  # 大写不是合法内容指纹
        {"raw_sha256": "a" * 63},
        {"raw_sha256": "g" * 64},
        {"raw_sha256": None},
        {"parser_version": ""},
        {"parser_version": None},
        {"char_start": -1},
        {"char_end": 5},  # end <= start
        {"char_start": 10, "char_end": 10},
        {"char_start": True},
        {"char_end": 20.5},
        {"title": ""},
        {"title": "   "},
        {"title": None},
    ],
)
def test_illegal_inputs_raise_entry_error(overrides: dict) -> None:
    with pytest.raises(EntryError):
        _call(**overrides)
    # 活对照：不带覆盖的调用成功（证明上面的失败确实来自非法入参，而不是签名错）
    assert ENTRY_ID_RE.match(_call())


def test_valid_input_still_succeeds_after_each_rejection() -> None:
    """逐条对照：每个非法入参被拒之后，同一调用路径对合法输入**成功**。"""
    legal = _call()
    for overrides in ({"raw_id": ""}, {"title": ""}, {"char_end": 1}):
        with pytest.raises(EntryError):
            _call(**overrides)
        assert _call() == legal


def test_fingerprint_rejects_empty_or_non_string_fields() -> None:
    with pytest.raises(EntryError):
        fingerprint(())
    with pytest.raises(EntryError):
        fingerprint(("ok", ""))
    with pytest.raises(EntryError):
        fingerprint(("ok", 1))  # type: ignore[arg-type]
    # 活对照：合法字段成功
    assert len(fingerprint(("ok", "fine"))) == 64
