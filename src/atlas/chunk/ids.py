"""分块 ID：内容寻址、确定性、可独立重算（SPEC §2.2 / §5 #7，T-206）。

**与 T-002 的 ID 策略同构，不另发明一套。** `atlas.contracts.ids` 已确立形状：

    raw_id   = "raw_" + sha256(channel_id, endpoint, content_sha256)[:32]
    claim_id = "clm_" + sha256(raw_id, kind, normalized_quote)[:32]
    label_id = "lbl_" + sha256(raw_id, label_key, label_value, actor)[:32]

本模块加同一形状的一条：

    chunk_id = "chk_" + sha256(raw_id, policy_version, start, end, normalized_text)[:32]

前缀各不相同（`chk_`），这一点是**结构性**的：`EvidenceAnchor.raw_sha256` 要求
`^[0-9a-f]{64}$`，而 `chk_…` 既带前缀、长度也不是 64 —— 因此把分块 ID 当作
证据真值锚点使用会在**类型校验层**立刻失败，而不是被静默接受。

关于文本参与 ID 的归一化口径
---------------------------
复用 `atlas.contracts.ids.normalize_for_id`（仅压缩空白、不做语义清洗）。
它只保证"同一段文字不因空白差异产生两个 ID"，**不是** SPEC §2.2 的归一化文本层
（那由 T-104 负责，可重建且带偏移映射）。

为什么摘要实现是"重复"的（而不是 import `atlas.contracts.ids._sha256_hex`）
--------------------------------------------------------------------------
`_sha256_hex` 是 T-002 模块的**私有**函数，而本任务被明确要求"不要修改
`src/atlas/normalize/` 或任何其它任务的包"，因此不能给它加公开的 `chunk_id_for`。
这里按**同一约定**（`\x1f` 分段分隔符 + utf-8 + sha256 + 取前 32 位十六进制）
就地实现同一形状，并由 `tests/test_chunk_ids.py` 用契约模块里**公开**的
`raw_id_for` 逐字节钉死"形状一致"，避免两份实现悄悄漂移。

（已作为"不属于本任务的缺陷"上报：`atlas.contracts.ids` 的摘要助手是私有的，
后续需要一个公开的 `digest_id(prefix, *parts)`。）
"""

from __future__ import annotations

import hashlib

from atlas.contracts.ids import normalize_for_id

from .errors import ChunkError

__all__ = [
    "CHUNK_ID_PREFIX",
    "chunk_id_for",
    "policy_fingerprint",
]

#: 分块 ID 的固定前缀（与 `raw_` / `clm_` / `lbl_` 并列，互不混淆）。
CHUNK_ID_PREFIX = "chk_"

#: 摘要的字段分隔符（与 T-002 的 `\x1f` 同一约定）。
_SEP = b"\x1f"


def _digest_hex(*parts: str) -> str:
    """带分隔符的稳定摘要，避免拼接歧义（`"ab"+"c"` 与 `"a"+"bc"`）。

    与 T-002 `atlas.contracts.ids._sha256_hex` **逐字节等价**（同一 `\\x1f` 约定、
    同一 utf-8 编码、同一 sha256 截断）。
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


def chunk_id_for(
    *,
    raw_id: str,
    policy_version: str,
    normalized_start: int,
    normalized_end: int,
    text: str,
) -> str:
    """`chunk_id` = f(raw, 策略版本, 区间, 文本) —— 内容寻址、确定性、可独立重算。

    - 只改策略版本 → 换 ID（换策略后旧 ID 不会"碰巧"复用）
    - 只改文本或区间 → 换 ID（内容寻址，不靠位置编号）
    - 相同 `(raw, 版本, 区间, 文本)` → 永远同一个 ID（幂等）

    非法入参抛 `ChunkError`（不返回空串、不静默纠正）。
    """
    if not raw_id:
        raise ChunkError("raw_id 不得为空：分块必须能溯源到不可变原文")
    if not policy_version:
        raise ChunkError("policy_version 不得为空：ID 必须携带策略版本，否则换策略会撞 ID")
    if isinstance(normalized_start, bool) or not isinstance(normalized_start, int):
        raise ChunkError(f"normalized_start 必须是 int，得到 {type(normalized_start).__name__}")
    if isinstance(normalized_end, bool) or not isinstance(normalized_end, int):
        raise ChunkError(f"normalized_end 必须是 int，得到 {type(normalized_end).__name__}")
    if normalized_start < 0:
        raise ChunkError(f"normalized_start 不得为负：{normalized_start}")
    if normalized_end <= normalized_start:
        raise ChunkError(f"非法分块区间：[{normalized_start}, {normalized_end})")
    canonical = normalize_for_id(text)
    if not canonical:
        raise ChunkError("分块文本为空：不得为空白内容生成 ID")

    return CHUNK_ID_PREFIX + _digest_hex(
        raw_id,
        policy_version,
        str(normalized_start),
        str(normalized_end),
        canonical,
    )[:32]


def policy_fingerprint(parts: "tuple[str, ...] | list[str]") -> str:
    """由策略的有序字段算出**完整**摘要（不截断），用于"策略快照是否一致"的自检。

    与 `chunk_id` 的区别：ID 用前缀 + 32 位截断（与其它领域 ID 同构），
    指纹保留 64 位，用于断言"重建时用的策略与首次完全一致"。

    字段用 `\\x1f`（控制字符）分隔，因此正则里的 `|`、`:` 等字符不会造成歧义
    ——分隔符本身不可能出现在任何策略字段中。
    """
    if not parts:
        raise ChunkError("策略字段不得为空")
    for index, part in enumerate(parts):
        if not isinstance(part, str):
            raise ChunkError(f"策略字段 #{index} 必须是 str，得到 {type(part).__name__}")
        if not part:
            raise ChunkError(f"策略字段 #{index} 不得为空串")
    return _digest_hex(*parts)
