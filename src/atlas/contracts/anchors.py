"""证据锚点契约（SPEC §2.2，方案 2C）。

- **真值**：`EvidenceAnchor` —— 锚定在**原始字节**的字符区间，不随解析器变化。
- **派生**：`DerivedLocator` —— 块 ID / 页码 / 归一化区间，可失效、可重建，
  **禁止**用作人工产物的依附对象。

关键分工（SPEC §2.2）：抽取方只产出 **quote（文字）**，坐标由**确定性匹配**算出。
匹配失败必须标记 `FAILED`，不得凭空造坐标，也不得进入 Confirmed。

坐标转换：归一化文本偏移 → 原文偏移，由 `to_raw_offset` 完成（T-104 提供真实映射）。
本模块把这条依赖**显式化为参数**，避免在契约层假装两者相同。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Callable, Optional, Tuple

from pydantic import Field, model_validator

from .base import ContractModel
from .errors import AnchorError

_WS = re.compile(r"\s+")


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"  # 已产出 quote，尚未做确定性匹配
    VERIFIED = "verified"      # 已由确定性匹配取得 anchor
    FAILED = "failed"          # 匹配失败：quote 在原文中找不到


class EvidenceAnchor(ContractModel):
    """真值锚点：不可变，锚定在原始字节区间。"""

    raw_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_range(self) -> "EvidenceAnchor":
        if self.char_end <= self.char_start:
            raise ValueError(f"char_end({self.char_end}) 必须大于 char_start({self.char_start})")
        return self

    @classmethod
    def create(
        cls, *, raw_id: str, raw_sha256: str, char_start: int, char_end: int
    ) -> "EvidenceAnchor":
        """工厂入口：空区间抛 `AnchorError`，而不是让调用方去解析 `ValidationError`。"""
        if char_end <= char_start:
            raise AnchorError(f"非法锚点区间: [{char_start}, {char_end})")
        return cls(
            raw_id=raw_id, raw_sha256=raw_sha256, char_start=char_start, char_end=char_end
        )

    @property
    def length(self) -> int:
        return self.char_end - self.char_start


class DerivedLocator(ContractModel):
    """派生物：可失效、可重建。**不得**作为人工产物的锚点。"""

    block_id: Optional[str] = None
    page_number: Optional[int] = Field(default=None, ge=1)
    normalized_start: Optional[int] = Field(default=None, ge=0)
    normalized_end: Optional[int] = Field(default=None, gt=0)


OffsetMap = Callable[[int], int]
"""归一化文本偏移 → 原文偏移 的映射（T-104 提供；stub 中可用恒等映射）。"""


def match_quote(normalized_text: str, quote: str) -> Optional[Tuple[int, int]]:
    """确定性匹配，返回 quote 在 `normalized_text` 中的 `[start, end)`。

    先精确匹配；失败后退化为「空白折叠」匹配（容忍换行/空格差异）。
    找不到返回 `None` —— 调用方必须据此标记 `FAILED`。
    """
    candidate = quote.strip()
    if not candidate:
        raise AnchorError("quote 不得为空")

    idx = normalized_text.find(candidate)
    if idx >= 0:
        return idx, idx + len(candidate)

    collapsed = _WS.sub(" ", candidate)
    if not collapsed:
        return None
    pattern = re.compile(r"\s+".join(re.escape(p) for p in collapsed.split(" ")))
    found = pattern.search(normalized_text)
    if found is None:
        return None
    return found.start(), found.end()


def build_anchor(
    *,
    raw_id: str,
    raw_sha256: str,
    normalized_text: str,
    quote: str,
    to_raw_offset: OffsetMap,
) -> Tuple[VerificationStatus, Optional[EvidenceAnchor], Optional[DerivedLocator]]:
    """由 quote 推导锚点。

    Returns:
        `(status, anchor, derived)`；只有 `VERIFIED` 时 `anchor` 非空。
    """
    span = match_quote(normalized_text, quote)
    if span is None:
        return VerificationStatus.FAILED, None, None

    norm_start, norm_end = span
    anchor = EvidenceAnchor.create(
        raw_id=raw_id,
        raw_sha256=raw_sha256,
        char_start=to_raw_offset(norm_start),
        char_end=to_raw_offset(norm_end),
    )
    derived = DerivedLocator(normalized_start=norm_start, normalized_end=norm_end)
    return VerificationStatus.VERIFIED, anchor, derived
