"""条目解析器版本与 ID：内容寻址、确定性、可独立重算（SPEC §2.2 / §3，T-130）。

**与 T-002 / T-206 的 ID 策略同构，不另发明一套。** `atlas.contracts.ids` 已确立形状：

    raw_id   = "raw_" + sha256(channel_id, endpoint, content_sha256)[:32]
    claim_id = "clm_" + sha256(raw_id, kind, normalized_quote)[:32]
    label_id = "lbl_" + sha256(raw_id, label_key, label_value, actor)[:32]
    chunk_id = "chk_" + sha256(raw_id, policy_version, start, end, text)[:32]

本模块加同一形状的一条：

    entry_id = "ent_" + sha256(raw_id, raw_sha256, parser_version,
                               char_start, char_end, normalized_title)[:32]

四个字段各自为什么必须在 ID 里
------------------------------

| 字段 | 不放进 ID 的后果 |
|---|---|
| `raw_id` | 两份 feed 里位置相同的条目会撞 ID |
| `raw_sha256` | **`raw_id` 相同、字节不同**时（理论上不该发生，但 Raw 只增不改只由存储层强制）条目身份会错误地保持稳定 |
| `parser_version` | **换了解析器却拿到同一批 ID** —— 派生量看似稳定，实际已经漂移。这正是本任务要求"版本变化必须改变条目 ID"的原因 |
| `char_start` / `char_end` | 条目顺序/位置变化后身份漂移 |
| `normalized_title` | 同区间换标题（字段提取规则变化）时身份不变，掩盖解析漂移 |

**前缀 `ent_` 是结构性的**：`EvidenceAnchor.raw_sha256` 要求 `^[0-9a-f]{64}$`，
而 `ent_…` 既带前缀、长度也不是 64 —— 因此把条目 ID 当证据真值使用会在**类型校验层**
立刻失败，而不是被静默接受。

摘要实现为什么是"就地重复"的
--------------------------

`atlas.contracts.ids._sha256_hex` 是 T-002 模块的**私有**函数，而本任务被明确要求
不得修改其它任务的包，因此不能给它加公开的 `entry_id_for`。这里按同一约定
（`\\x1f` 分段分隔符 + utf-8 + sha256 + 取前 32 位十六进制）就地实现同一形状，
并由 `tests/test_entries_ids.py` 用契约模块里**公开**的 `raw_id_for` 钉死"形状一致"。
（与 T-206 `atlas.chunk.ids` 的记录相同：`atlas.contracts.ids` 需要一个公开的
`digest_id(prefix, *parts)`；本任务未擅自新增，以免与主代理的统一收口冲突。）
"""

from __future__ import annotations

import hashlib
import re

from atlas.contracts.ids import normalize_for_id

from .errors import EntryError

__all__ = [
    "ENTRY_ID_PREFIX",
    "ENTRY_PARSER_VERSION",
    "ENTRY_ID_RE",
    "entry_id_for",
    "fingerprint",
]

#: 条目 ID 的固定前缀（与 `raw_` / `clm_` / `lbl_` / `chk_` 并列，互不混淆）。
ENTRY_ID_PREFIX = "ent_"

#: 当前解析器版本。**改动解析行为必须同时改它**，否则条目 ID 不会变。
ENTRY_PARSER_VERSION = "entry-parser-v1"

#: 合法条目 ID 的形状（活对照：`EvidenceAnchor.raw_sha256` 要求 `^[0-9a-f]{64}$`）。
ENTRY_ID_RE = re.compile(r"^ent_[0-9a-f]{32}$")

#: 摘要的字段分隔符（与 T-002 / T-206 的 `\x1f` 同一约定）。
_SEP = b"\x1f"


def _digest_hex(*parts: str) -> str:
    """带分隔符的稳定摘要，避免拼接歧义（`"ab"+"c"` 与 `"a"+"bc"`）。

    与 `atlas.contracts.ids._sha256_hex` 逐字节等价（同一 `\\x1f` 约定、
    同一 utf-8 编码、同一 sha256）。
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(_SEP)
    return digest.hexdigest()


def fingerprint(parts: "tuple[str, ...] | list[str]") -> str:
    """由解析器的有序字段算出**完整**摘要（不截断），用于"解析器快照是否一致"的自检。

    与 `entry_id` 的区别：ID 用前缀 + 32 位截断（与其它领域 ID 同构），
    指纹保留 64 位，用来断言"重建时用的解析器与首次完全一致"。
    """
    if not parts:
        raise EntryError("解析器字段不得为空")
    for index, part in enumerate(parts):
        if not isinstance(part, str):
            raise EntryError(f"解析器字段 #{index} 必须是 str，得到 {type(part).__name__}")
        if not part:
            raise EntryError(f"解析器字段 #{index} 不得为空串")
    return _digest_hex(*parts)


def _check_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise EntryError(f"{label} 必须是 str，得到 {type(value).__name__}")
    return value


def _check_offset(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EntryError(f"{label} 必须是 int，得到 {type(value).__name__}")
    if value < 0:
        raise EntryError(f"{label} 不得为负：{value}")
    return value


def entry_id_for(
    *,
    raw_id: str,
    raw_sha256: str,
    parser_version: str,
    char_start: int,
    char_end: int,
    title: str,
) -> str:
    """`entry_id` = f(raw 身份, 解析器版本, 字符区间, 标题) —— 确定性、可独立重算。

    - 只改解析器版本 → 换 ID（这是**要求**：见模块 docstring）
    - 只改原文 sha256 → 换 ID
    - 只改区间或标题 → 换 ID（内容寻址，不靠"第几条"这种位置编号）
    - 相同五元组 → 永远同一个 ID（幂等）

    非法入参抛 `EntryError`（不返回空串、不静默纠正）。
    """
    if not isinstance(raw_id, str) or not raw_id:
        raise EntryError("raw_id 不得为空：条目必须能溯源到不可变原文")
    if not isinstance(parser_version, str) or not parser_version:
        raise EntryError(
            "parser_version 不得为空：ID 必须携带解析器版本，否则换解析器会撞 ID"
        )
    raw_sha256 = _check_text(raw_sha256, "raw_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise EntryError(
            f"raw_sha256 必须是 64 位小写十六进制（内容指纹），得到 {raw_sha256!r}"
        )
    start = _check_offset(char_start, "char_start")
    end = _check_offset(char_end, "char_end")
    if end <= start:
        raise EntryError(f"非法条目区间：[{start}, {end})")
    title = _check_text(title, "title")
    canonical = normalize_for_id(title)
    if not canonical:
        raise EntryError("条目标题为空：不得为无标题条目生成 ID")

    return ENTRY_ID_PREFIX + _digest_hex(
        raw_id,
        raw_sha256,
        parser_version,
        str(start),
        str(end),
        canonical,
    )[:32]
