"""ID 与版本策略（SPEC §4.1 T-002）。

设计取舍：**确定性（内容寻址）标识**，使重跑幂等。

- `raw_id`   = f(channel_id, endpoint, content_sha256)
  同一渠道同一端点、内容相同 → 同一条 Raw（幂等，不产生重复）；
  内容变化 → 新的 raw_id（旧记录不被覆盖）。
- `claim_id` = f(raw_id, kind, quote)
  同一份原文、同一模型、同一 quote 再次抽取 → 同一个 claim_id，只增加 version，
  不产生重复条目（幂等）。
- `label_id` = f(raw_id, label_key, label_value, actor)
  同一个人重复提交同一判断 → 幂等；改判断 → 新的 label_id（Confirmed 只增不改）。

非法入参一律抛 `IdError`（不返回空串、不静默纠正）。
"""

from __future__ import annotations

import hashlib
import re

from .errors import IdError

_SEP = b"\x1f"
_WS = re.compile(r"\s+")


def _sha256_hex(*parts: str) -> str:
    """带分隔符的稳定摘要，避免拼接歧义（"ab"+"c" 与 "a"+"bc"）。"""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


def normalize_for_id(text: str) -> str:
    """ID 计算用的稳定性归一化：仅压缩空白，不做语义清洗。

    注意：这与 SPEC §2.2 的「归一化文本层」不是一回事——后者由 T-104 负责，
    可重建且带偏移映射。这里只保证同一段文字不因空白差异产生两个 ID。
    """
    return _WS.sub(" ", text).strip()


def content_sha256(data: bytes) -> str:
    """原文内容的指纹（`raw_sha256` 的来源）。"""
    if not isinstance(data, (bytes, bytearray)):
        raise IdError(f"content 必须是 bytes，收到 {type(data).__name__}")
    return hashlib.sha256(bytes(data)).hexdigest()


def raw_id_for(channel_id: str, endpoint: str, content_hash: str) -> str:
    if not channel_id or not endpoint or not content_hash:
        raise IdError("channel_id / endpoint / content_sha256 均不得为空")
    return "raw_" + _sha256_hex(channel_id, endpoint, content_hash)[:32]


def claim_id_for(raw_id: str, kind: str, quote: str) -> str:
    if not raw_id or not kind:
        raise IdError("raw_id / kind 不得为空")
    normalized = normalize_for_id(quote)
    if not normalized:
        raise IdError("quote 不得为空：Proposed 必须携带证据")
    return "clm_" + _sha256_hex(raw_id, kind, normalized)[:32]


def label_id_for(raw_id: str, label_key: str, label_value: str, actor: str) -> str:
    if not raw_id or not label_key or not actor:
        raise IdError("raw_id / label_key / actor 不得为空")
    return "lbl_" + _sha256_hex(raw_id, label_key, label_value, actor)[:32]
