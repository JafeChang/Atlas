"""证据高亮展示载荷（SPEC §4.2 T-107 → T-109）。

本模块是**纯派生物层**：输入是原始字节 + 已经过校验的引用，输出是"够渲染高亮"的载荷。

同时给出两套坐标，并且必须自洽（`HighlightPayload` 的模型校验器逐条钉死）：

| 坐标 | 用途 | 性质 |
|---|---|---|
| `char_start` / `char_end` | 在**原文**上定位（`raw_text`，含实体字面量） | 真值（SPEC §2.2） |
| `normalized_start` / `normalized_end` | 在**归一化文本**上定位 | 派生（可重建） |

展示路径**同样不采信存储的坐标**：`highlight_for_span()` 会按 quote 重算一遍，
与行里存的锚点对不上就抛 `EvidenceMismatchError`（宁可不显示，也不显示错的高亮）。
"""

from __future__ import annotations

import html as _html
from typing import Tuple

from pydantic import Field, model_validator

from atlas.contracts import ContractModel, EvidenceMismatchError, VerificationStatus

from .verify import EvidenceSpan, QuoteVerification, verify_quote

__all__ = [
    "DEFAULT_CONTEXT_CHARS",
    "HighlightPayload",
    "HighlightRegion",
    "build_highlight",
    "highlight_for_span",
]

#: 高亮区间前后各带多少个原文字符作为上下文（够 T-109 做"打开原文"的定位）。
DEFAULT_CONTEXT_CHARS = 80


class HighlightRegion(ContractModel):
    """一个高亮区间：原文区间 + 归一化区间 + 两侧的文本。"""

    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(gt=0)
    #: 原文切片：**含实体字面量**（`&mdash;`），与 `EvidenceAnchor` 的参照系一致
    raw_text: str = Field(min_length=1)
    #: `html.unescape(raw_text)`：T-109 直接渲染这个更直观
    decoded_text: str = Field(min_length=1)
    #: 归一化文本上的切片（命中区间的"可见文字"）
    normalized_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_region(self) -> "HighlightRegion":
        if self.char_end <= self.char_start:
            raise ValueError(f"原文区间非法：[{self.char_start}, {self.char_end})")
        if self.normalized_end <= self.normalized_start:
            raise ValueError(
                f"归一化区间非法：[{self.normalized_start}, {self.normalized_end})"
            )
        if _html.unescape(self.raw_text) != self.decoded_text:
            raise ValueError("decoded_text 必须是 raw_text 的实体解码结果")
        return self


class HighlightPayload(ContractModel):
    """渲染高亮所需的全部信息（自洽性由校验器强制，不靠调用方自觉）。"""

    raw_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: VerificationStatus
    quote: str = Field(min_length=1)
    raw_text: str
    normalized_text: str
    regions: Tuple[HighlightRegion, ...] = ()
    context_char_start: int = Field(default=0, ge=0)
    context_char_end: int = Field(default=0, ge=0)
    context_text: str = ""

    @model_validator(mode="after")
    def _check_payload(self) -> "HighlightPayload":
        if self.status is VerificationStatus.VERIFIED and not self.regions:
            raise ValueError("VERIFIED 载荷必须至少有一个高亮区间")
        if self.status is not VerificationStatus.VERIFIED and self.regions:
            raise ValueError("非 VERIFIED 载荷不得携带高亮区间")

        previous_end = -1
        for region in self.regions:
            if region.char_start < previous_end:
                raise ValueError("高亮区间必须有序且不重叠")
            previous_end = region.char_end
            if self.raw_text[region.char_start : region.char_end] != region.raw_text:
                raise ValueError("raw_text 与 char_start/char_end 不自洽")
            if (
                self.normalized_text[region.normalized_start : region.normalized_end]
                != region.normalized_text
            ):
                raise ValueError("normalized_text 与 normalized_start/end 不自洽")

        if self.context_char_end < self.context_char_start:
            raise ValueError("context 区间非法")
        if self.context_char_end > len(self.raw_text):
            raise ValueError("context 区间越过原文末尾")
        if self.raw_text[self.context_char_start : self.context_char_end] != self.context_text:
            raise ValueError("context_text 与 context 区间不自洽")

        if self.regions:
            covered = (self.regions[0].char_start, self.regions[-1].char_end)
            if self.context_char_start > covered[0] or self.context_char_end < covered[1]:
                raise ValueError("context 区间必须覆盖全部高亮区间")
        return self


def _region(verification: QuoteVerification) -> HighlightRegion:
    anchor = verification.anchor
    derived = verification.derived
    assert anchor is not None and derived is not None  # VERIFIED 的契约
    assert derived.normalized_start is not None and derived.normalized_end is not None
    raw_slice = verification.raw_text[anchor.char_start : anchor.char_end]
    return HighlightRegion(
        char_start=anchor.char_start,
        char_end=anchor.char_end,
        normalized_start=derived.normalized_start,
        normalized_end=derived.normalized_end,
        raw_text=raw_slice,
        decoded_text=_html.unescape(raw_slice),
        normalized_text=verification.normalized_text[
            derived.normalized_start : derived.normalized_end
        ],
    )


def build_highlight(
    verification: QuoteVerification, *, context_chars: int = DEFAULT_CONTEXT_CHARS
) -> HighlightPayload:
    """校验结果 → 展示载荷。

    `FAILED` 也返回载荷（`regions` 为空、上下文为空窗口）：T-109 需要把"未验证"
    明确展示出来，而不是拿到一个空白或异常。
    """
    if context_chars < 0:
        raise ValueError(f"context_chars 不得为负：{context_chars}")

    regions = (_region(verification),) if verification.status is VerificationStatus.VERIFIED else ()

    if regions:
        low = regions[0].char_start
        high = regions[-1].char_end
        context_start = max(0, low - context_chars)
        context_end = min(len(verification.raw_text), high + context_chars)
    else:
        context_start = context_end = 0

    return HighlightPayload(
        raw_id=verification.raw_id,
        raw_sha256=verification.raw_sha256,
        status=verification.status,
        quote=verification.quote,
        raw_text=verification.raw_text,
        normalized_text=verification.normalized_text,
        regions=regions,
        context_char_start=context_start,
        context_char_end=context_end,
        context_text=verification.raw_text[context_start:context_end],
    )


def highlight_for_span(
    span: EvidenceSpan, raw_bytes: bytes, *, content_type: str = ""
) -> HighlightPayload:
    """持久化的证据行 + 原文 → 展示载荷。

    重算一遍再比对：存储的坐标只是**索引**，不是事实来源（SPEC §2.2）。
    对不上就响亮失败，绝不渲染一个与原文不符的高亮。
    """
    verification = verify_quote(
        raw_id=span.raw_id,
        raw_bytes=raw_bytes,
        quote=span.quote,
        content_type=content_type,
    )
    if verification.status is not VerificationStatus.VERIFIED or verification.anchor is None:
        raise EvidenceMismatchError(
            f"claim {span.claim_id}@v{span.claim_version} 的 quote 在给定原文中已匹配不到，"
            "拒绝渲染高亮"
        )
    if verification.raw_sha256 != span.raw_sha256:
        raise EvidenceMismatchError(
            f"记录的 raw_sha256={span.raw_sha256[:12]}… 与给定原文 "
            f"{verification.raw_sha256[:12]}… 不一致：原文已被替换"
        )
    stored = span.anchor
    if verification.anchor != stored:
        raise EvidenceMismatchError(
            f"evidence_spans 记录的锚点 [{stored.char_start}, {stored.char_end}) 与按 quote "
            f"重算出的 [{verification.anchor.char_start}, {verification.anchor.char_end}) "
            "不一致：坐标只能由确定性匹配产出（SPEC §2.2）"
        )
    derived = verification.derived
    assert derived is not None
    if (derived.normalized_start, derived.normalized_end) != (
        span.normalized_start,
        span.normalized_end,
    ):
        raise EvidenceMismatchError(
            "evidence_spans 记录的归一化区间与重算结果不一致（派生数据已漂移）"
        )
    return build_highlight(verification)
