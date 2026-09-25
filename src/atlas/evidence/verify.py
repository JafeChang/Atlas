"""证据校验：quote → **确定性**锚点（SPEC §2.2 方案 2C / §4.2 T-107）。

本模块只做一件事，并且做到底：**把机器产出的 quote 变成经过校验的原文锚点**。

坐标从哪里来（这是整个项目的原则，SPEC §2.2）
------------------------------------------------

```
ProposedClaim（只有 quote，没有坐标）
        │
        ▼  atlas.normalize.normalize(raw_bytes, content_type)   ← 纯函数，可重建
NormalizedText.text / .to_raw_offset
        │
        ▼  atlas.contracts.build_anchor(..., to_raw_offset=nt.to_raw_offset)
(status, EvidenceAnchor, DerivedLocator)
```

三条不可协商的规则，代码层逐条落实：

1. **本模块没有任何"接收坐标"的入口。** 公开 API 只接受 `quote: str` 与 `raw_bytes`；
   `raw_sha256` 由 `content_sha256(raw_bytes)` **当场算出**，不作为参数接受。
   `ProposedClaim` 的 `extra="forbid"` 已经让抽取器塞不进 `char_start=`，这里再保证
   本模块也**读不到**坐标——唯一被读到的坐标是调用方传入的 `declared_anchor`
   （即 claim 自己声明的锚点），而它**只用于比对**，**绝不**作为返回值。
2. **匹配失败 = `FAILED`，且不产生锚点。** 不存在"大概位置"这种降级：
   `FAILED` 时 `anchor is None`、`derived is None`。
3. **重算优先于采信。** `verify_claim()` 永远按 quote 重算一遍；若 claim 声明的锚点
   与重算结果不一致，**响亮失败**（`EvidenceMismatchError`），而不是把声明的坐标当真。
   这是判据 4「篡改检测」的实现：改掉记录里的 anchor 再重算，得到的仍是重算值。

关于「端点对齐」（SPEC §2.2 的实体澄清）
----------------------------------------

`atlas.normalize` 对 HTML 实体各成一个原子（`&mdash;` → `—`，原子原文区间长度 = 实体字面量
长度）。因此当归一化区间**恰好结束在一个实体字符上**时，`to_raw_offset(norm_end)` 会停在
实体字面量**内部**（`&mdas`），这样的锚点无法代表证据。本模块用一个**纯函数、只看段表**
的端点对齐把它补到段边界：`norm_end == seg.norm_end and seg.raw_length > seg.length` 时，
`char_end` 取 `seg.raw_end`。除此之外**一律原样保留 `build_anchor` 的输出**
（测试 `test_plain_quote_anchor_is_exactly_build_anchor` 钉死这一点）。

对齐之后仍要用 SPEC §2.2 的判据自检：
「原文切片**经实体解码后**包含该区间的首尾非空白字符」——不满足则抛 `AnchorError`
（宁可响亮失败，也不返回一个代表不了证据的锚点）。
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from pydantic import Field, model_validator

from atlas.contracts import (
    AnchorError,
    ContractModel,
    DerivedLocator,
    EvidenceAnchor,
    EvidenceMismatchError,
    ProposedClaim,
    VerificationStatus,
    VersionError,
    build_anchor,
    content_sha256,
)
from atlas.normalize import NormalizedText, Segment, normalize

__all__ = [
    "EvidenceSpan",
    "QuoteVerification",
    "VerificationOutcome",
    "verify_claim",
    "verify_quote",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_bytes(raw_bytes: bytes) -> bytes:
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise TypeError(f"raw_bytes 必须是 bytes-like，得到 {type(raw_bytes).__name__}")
    return bytes(raw_bytes)


def _edge_chars(quote: str) -> Tuple[str, str]:
    """quote 的**首尾非空白字符**（SPEC §2.2 判据的对象）。

    `match_quote` 内部用 `quote.strip()`，这里用同一口径；空白 quote 是非法证据，
    响亮失败而不是当成"匹配不到"。
    """
    stripped = quote.strip()
    if not stripped:
        raise AnchorError("quote 不得为空白：无法作为证据")
    return stripped[0], stripped[-1]


def _segment_containing(segments: Sequence[Segment], normalized_offset: int) -> Optional[Segment]:
    for segment in segments:
        if segment.norm_start <= normalized_offset < segment.norm_end:
            return segment
    return None


def _aligned_raw_end(
    normalized: NormalizedText, *, norm_end: int, raw_end: int
) -> int:
    """把落在"原文字符数 > 归一化字符数"的段内部的末端对齐到段边界。

    只处理段末（`norm_end == segment.norm_end`）：这种段就是 HTML 实体字面量
    （或折叠的空白串）；`to_raw_offset(norm_end)` 此时指向段内偏移，会切在实体中间。
    段内偏移（quote 是长段的真前缀）**不做任何扩展**——那里的映射本来就是逐字符精确的。
    """
    segment = _segment_containing(normalized.segments, norm_end - 1)
    if segment is None:
        return raw_end
    if norm_end == segment.norm_end and segment.raw_length > segment.length:
        return max(raw_end, segment.raw_end)
    return raw_end


def _assert_anchor_represents_quote(
    normalized: NormalizedText, anchor: EvidenceAnchor, quote: str
) -> None:
    """SPEC §2.2 判据：原文切片**经实体解码后**包含 quote 的首尾非空白字符。"""
    first, last = _edge_chars(quote)
    window = _html.unescape(normalized.raw_text[anchor.char_start : anchor.char_end])
    if first not in window or last not in window:
        raise AnchorError(
            "锚点无法代表证据（SPEC §2.2）："
            f"原文切片 {normalized.raw_text[anchor.char_start:anchor.char_end]!r}"
            f"（解码后 {window!r}）既不包含 quote 的首字符 {first!r}、"
            f"也不包含尾字符 {last!r}（quote={quote!r}）"
        )


# --------------------------------------------------------------------------- #
# 一次确定性匹配的产物
# --------------------------------------------------------------------------- #


class QuoteVerification(ContractModel):
    """**只**由 `(raw_bytes, content_type, raw_id, quote)` 算出的匹配结果。

    它携带归一化文本与原文本身，因此下游（`display`）不必再归一化一次，
    也就不会出现"展示用的是另一份归一化结果"这种分叉。

    `declared_anchor` 是调用方声明的锚点（`ProposedClaim.anchor`），**仅用于比对**：
    `anchor` 字段永远是本次重算的结果。
    """

    raw_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: str = Field(min_length=1)
    status: VerificationStatus
    anchor: Optional[EvidenceAnchor] = None
    derived: Optional[DerivedLocator] = None
    normalized_text: str
    raw_text: str
    declared_anchor: Optional[EvidenceAnchor] = None

    @model_validator(mode="after")
    def _check_status(self) -> "QuoteVerification":
        if self.status is VerificationStatus.VERIFIED:
            if self.anchor is None or self.derived is None:
                raise ValueError("VERIFIED 必须携带重算出的 anchor 与 derived")
        elif self.anchor is not None or self.derived is not None:
            raise ValueError("非 VERIFIED 状态不得携带 anchor/derived（避免伪造坐标）")
        return self

    @property
    def declared_anchor_matches(self) -> bool:
        """声明的锚点是否与重算结果一致；未声明时为 `False`（"没有可比对的声明"）。"""
        return self.declared_anchor is not None and self.declared_anchor == self.anchor

    @property
    def normalized_start(self) -> Optional[int]:
        return None if self.derived is None else self.derived.normalized_start

    @property
    def normalized_end(self) -> Optional[int]:
        return None if self.derived is None else self.derived.normalized_end

    @property
    def raw_slice(self) -> str:
        """锚点对应的原文切片（`FAILED` 时为空串）。"""
        if self.anchor is None:
            return ""
        return self.raw_text[self.anchor.char_start : self.anchor.char_end]


# --------------------------------------------------------------------------- #
# 校验入口
# --------------------------------------------------------------------------- #


def verify_quote(
    *,
    raw_id: str,
    raw_bytes: bytes,
    quote: str,
    content_type: str = "",
    declared_anchor: Optional[EvidenceAnchor] = None,
) -> QuoteVerification:
    """quote → 锚点（**唯一**的坐标来源是这里的确定性匹配）。

    Args:
        raw_id: 锚点绑定的原文标识（由调用方按 `atlas.contracts.ids.raw_id_for` 取得）。
        raw_bytes: 原始字节；归一化与 `raw_sha256` 都从这里算出。
        quote: 抽取方给出的引用文字。**没有**坐标参数。
        content_type: `Content-Type`（决定 HTML / 纯文本路径）。
        declared_anchor: 待比对的声明锚点；只用于 `declared_anchor_matches`。

    Returns:
        `QuoteVerification`：`VERIFIED` 时携带重算出的 `anchor` + `derived`，
        否则为 `FAILED` 且两者皆 `None`。
    """
    if not raw_id:
        raise ValueError("raw_id 不得为空：锚点必须绑定到具体原文")
    payload = _as_bytes(raw_bytes)
    digest = content_sha256(payload)
    if declared_anchor is not None and declared_anchor.raw_id != raw_id:
        raise EvidenceMismatchError(
            f"声明锚点绑定的 raw_id={declared_anchor.raw_id!r} 与本次校验的 {raw_id!r} 不一致"
        )

    normalized = normalize(payload, content_type)
    status, anchor, derived = build_anchor(
        raw_id=raw_id,
        raw_sha256=digest,
        normalized_text=normalized.text,
        quote=quote,
        to_raw_offset=normalized.to_raw_offset,
    )

    if status is VerificationStatus.VERIFIED:
        assert anchor is not None and derived is not None  # build_anchor 的契约
        assert derived.normalized_end is not None
        aligned_end = _aligned_raw_end(
            normalized, norm_end=derived.normalized_end, raw_end=anchor.char_end
        )
        if aligned_end != anchor.char_end:
            # 端点对齐是**纯函数**：输入只有段表与归一化区间，没有任何外部坐标。
            anchor = EvidenceAnchor.create(
                raw_id=anchor.raw_id,
                raw_sha256=anchor.raw_sha256,
                char_start=anchor.char_start,
                char_end=aligned_end,
            )
        _assert_anchor_represents_quote(normalized, anchor, quote)

    return QuoteVerification(
        raw_id=raw_id,
        raw_sha256=digest,
        quote=quote,
        status=status,
        anchor=anchor,
        derived=derived,
        normalized_text=normalized.text,
        raw_text=normalized.raw_text,
        declared_anchor=declared_anchor,
    )


class VerificationOutcome(ContractModel):
    """一次 **claim 级**校验的产物：claim 身份 + 匹配结果。

    `claim_version` 是幂等键的一半（另一半是 `claim_id`）：同一 claim 的同一版本
    重复校验必须幂等，新版本是新记录（SPEC §3「幂等 / 可重算」）。
    """

    claim_id: str = Field(min_length=1)
    claim_version: int = Field(ge=1)
    verification: QuoteVerification

    @property
    def status(self) -> VerificationStatus:
        return self.verification.status

    @property
    def anchor(self) -> Optional[EvidenceAnchor]:
        return self.verification.anchor

    @property
    def derived(self) -> Optional[DerivedLocator]:
        return self.verification.derived

    @property
    def quote(self) -> str:
        return self.verification.quote

    @property
    def raw_id(self) -> str:
        return self.verification.raw_id

    @property
    def raw_sha256(self) -> str:
        return self.verification.raw_sha256

    def apply_to(self, claim: ProposedClaim) -> ProposedClaim:
        """把校验结果落回 claim（产出**新实例**）—— 通往 T-108 Confirmed 的唯一入口。

        只有校验后的 claim 才能构造 `ConfirmedLabel.from_proposal`（SPEC §2.3）。
        """
        if claim.claim_id != self.claim_id or claim.version != self.claim_version:
            raise EvidenceMismatchError(
                f"校验结果属于 {self.claim_id}@v{self.claim_version}，"
                f"不能套用到 {claim.claim_id}@v{claim.version}"
            )
        return claim.with_verification(self.status, self.anchor)


def verify_claim(
    claim: ProposedClaim,
    raw_bytes: bytes,
    *,
    content_type: str = "",
) -> VerificationOutcome:
    """把 `ProposedClaim` 的 quote 校验成锚点。

    **不接受任何坐标输入**：`claim.anchor` 只用于比对。若它存在且与重算结果不一致
    （被篡改 / 过期 / 来自另一份原文），抛 `EvidenceMismatchError` —— 绝不采信。
    claim 尚未入 store（`version == 0`）时抛 `VersionError`：证据必须挂在某个已定版本上，
    否则"同一 claim 同一版本幂等"这个键就不成立。
    """
    if claim.version < 1:
        raise VersionError(
            f"claim {claim.claim_id} 的 version={claim.version}，尚未入 store；"
            "证据记录以 (claim_id, version) 为幂等键，必须先取得版本号"
        )
    verification = verify_quote(
        raw_id=claim.raw_id,
        raw_bytes=raw_bytes,
        quote=claim.quote,
        content_type=content_type,
        declared_anchor=claim.anchor,
    )
    if claim.anchor is not None and not verification.declared_anchor_matches:
        raise EvidenceMismatchError(
            f"claim {claim.claim_id} 声明的锚点 "
            f"[{claim.anchor.char_start}, {claim.anchor.char_end}) 与按 quote 重算出的 "
            f"[{verification.anchor.char_start if verification.anchor else None}, "
            f"{verification.anchor.char_end if verification.anchor else None}) 不一致："
            "坐标只能由确定性匹配产出（SPEC §2.2），拒绝采信声明的坐标"
        )
    return VerificationOutcome(
        claim_id=claim.claim_id,
        claim_version=claim.version,
        verification=verification,
    )


# --------------------------------------------------------------------------- #
# 可持久化的证据记录
# --------------------------------------------------------------------------- #


class EvidenceSpan(ContractModel):
    """`evidence_spans` 表的一行（SPEC §2.10，表归属 T-107）。

    真值仍是 `EvidenceAnchor` 的**原文**字符区间；`normalized_*` 是**派生**区间
    （可重建，只为了让 T-109 能在归一化文本上渲染高亮）。两者必须自洽：
    校验时由同一次 `to_raw_offset` 映射产出。
    """

    claim_id: str = Field(min_length=1)
    claim_version: int = Field(ge=1)
    raw_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: str = Field(min_length=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)
    normalized_start: int = Field(ge=0)
    normalized_end: int = Field(gt=0)
    verified_at: datetime

    @model_validator(mode="after")
    def _check_ranges(self) -> "EvidenceSpan":
        if self.char_end <= self.char_start:
            raise ValueError(
                f"原文区间非法：[{self.char_start}, {self.char_end})"
            )
        if self.normalized_end <= self.normalized_start:
            raise ValueError(
                f"归一化区间非法：[{self.normalized_start}, {self.normalized_end})"
            )
        return self

    @property
    def key(self) -> Tuple[str, int]:
        return (self.claim_id, self.claim_version)

    @property
    def anchor(self) -> EvidenceAnchor:
        return EvidenceAnchor.create(
            raw_id=self.raw_id,
            raw_sha256=self.raw_sha256,
            char_start=self.char_start,
            char_end=self.char_end,
        )

    def identity_fields(self) -> Tuple[object, ...]:
        """幂等比对用的字段（**不含** `verified_at`：重跑时刻不同不代表记录不同）。"""
        return (
            self.raw_id,
            self.raw_sha256,
            self.quote,
            self.char_start,
            self.char_end,
            self.normalized_start,
            self.normalized_end,
        )

    @classmethod
    def from_outcome(
        cls, outcome: VerificationOutcome, *, verified_at: Optional[datetime] = None
    ) -> "EvidenceSpan":
        """只有 `VERIFIED` 才能成为证据记录；其余状态抛 `AnchorError`。

        调用方（store）必须在**写入之前**调用它，从而"校验失败 → 不留半成品行"
        是结构性的，而不是靠写库路径上的 if 兜住。
        """
        if outcome.status is not VerificationStatus.VERIFIED or outcome.anchor is None:
            raise AnchorError(
                f"claim {outcome.claim_id}@v{outcome.claim_version} 的校验状态为 "
                f"{outcome.status.value}：未取得锚点的证据**不得**写入 evidence_spans"
            )
        derived = outcome.derived
        assert derived is not None and derived.normalized_start is not None
        assert derived.normalized_end is not None
        return cls(
            claim_id=outcome.claim_id,
            claim_version=outcome.claim_version,
            raw_id=outcome.raw_id,
            raw_sha256=outcome.raw_sha256,
            quote=outcome.quote,
            char_start=outcome.anchor.char_start,
            char_end=outcome.anchor.char_end,
            normalized_start=derived.normalized_start,
            normalized_end=derived.normalized_end,
            verified_at=verified_at or _utcnow(),
        )
