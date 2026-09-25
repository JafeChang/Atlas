"""T-107 证据校验的判据（SPEC §2.2 / §2.3 / §4.2）。

判据逐条对应本文件里的测试：

1. quote 精确命中 → `VERIFIED`，且原文切片**经实体解码后**包含 quote 的首尾非空白字符
2. quote 含空白差异（换行 / 多空格）→ 仍能命中
3. quote 不存在 → `FAILED`，不产生锚点
4. 篡改检测：改掉声明的 anchor 后重算，得到的是**重算值**（不是采信传入值）
5. 坐标只能由确定性匹配产出：claim 带不了坐标，校验入口也没有坐标参数
6. 展示载荷（归一化区间 + 原文字节区间 + 高亮上下文）自洽
"""

from __future__ import annotations

import html as _html
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from atlas.contracts import (
    AnchorError,
    ConfirmedLabel,
    EvidenceAnchor,
    ProposedClaim,
    TaskVersions,
    UnverifiedEvidenceError,
    VerificationStatus,
    VersionError,
    build_anchor,
    content_sha256,
    raw_id_for,
)
from atlas.evidence import (
    EvidenceSpan,
    build_highlight,
    highlight_for_span,
    verify_claim,
    verify_quote,
)
from atlas.normalize import normalize

UTC_VERSIONS = TaskVersions(code_version="t107", config_version="v1", model_version="m1")

#: 仓库里的共享存储根（SPEC §2.10）。测试一律用 tmp_path，绝不往 data/ 写。
REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


RAW_TEXT = "Atlas 把证据锚定在原文上，坐标由确定性匹配算出，而不是让模型猜。"
RAW_BYTES = RAW_TEXT.encode("utf-8")
RAW_ID = raw_id_for("ch_test", "https://example.invalid/a", content_sha256(RAW_BYTES))
QUOTE = "证据锚定在原文上"


def make_claim(
    quote: str = QUOTE,
    *,
    version: int = 1,
    raw_id: str = RAW_ID,
    kind: str = "industry",
    value: str = "ai",
) -> ProposedClaim:
    claim = ProposedClaim.propose(
        raw_id=raw_id,
        kind=kind,
        value=value,
        quote=quote,
        confidence=0.87,
        versions=UTC_VERSIONS,
    )
    return claim if version == 0 else claim.with_version(version)


# --------------------------------------------------------------------------- #
# 判据 1：精确命中
# --------------------------------------------------------------------------- #


def test_exact_quote_is_verified_with_recomputed_anchor():
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")

    assert outcome.status is VerificationStatus.VERIFIED
    anchor = outcome.anchor
    assert anchor is not None
    # raw_sha256 是**当场算出的**，不是外部传入的
    assert anchor.raw_sha256 == content_sha256(RAW_BYTES)
    assert anchor.raw_id == RAW_ID

    verification = outcome.verification
    # 判据 1 的严格形式：SPEC §2.2「原文切片经实体解码后包含该区间的首尾非空白字符」
    window = verification.raw_text[anchor.char_start : anchor.char_end]
    assert QUOTE[0] in window and QUOTE[-1] in window
    # 纯文本场景下更强：切片**逐字符**等于 quote
    assert window == QUOTE
    # 派生区间（归一化）与真值区间（原文）同时给出且自洽
    derived = outcome.derived
    assert derived is not None and derived.normalized_start is not None
    assert derived.normalized_end is not None
    assert (
        verification.normalized_text[derived.normalized_start : derived.normalized_end] == QUOTE
    )
    assert (
        verify_quote(raw_id=RAW_ID, raw_bytes=RAW_BYTES, quote=QUOTE).normalized_text
        == verification.normalized_text
    )


def test_plain_quote_anchor_is_exactly_build_anchor():
    """普通 quote 不做任何端点扩展：锚点必须**逐字段等于** `build_anchor` 的输出。

    这条钉死"本模块只负责校验，不负责发明坐标"——端点对齐只在实体字面量内部发生。
    """
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")
    normalized = normalize(RAW_BYTES, "text/plain")
    status, expected, derived = build_anchor(
        raw_id=RAW_ID,
        raw_sha256=content_sha256(RAW_BYTES),
        normalized_text=normalized.text,
        quote=QUOTE,
        to_raw_offset=normalized.to_raw_offset,
    )
    assert status is VerificationStatus.VERIFIED
    assert outcome.anchor == expected
    assert outcome.derived == derived


# --------------------------------------------------------------------------- #
# 判据 2：空白差异
# --------------------------------------------------------------------------- #


def test_whitespace_differences_still_match():
    raw = "第一段  有多个连续空格\n\n第二段 结束".encode("utf-8")
    raw_id = raw_id_for("ch_test", "https://example.invalid/ws", content_sha256(raw))
    quote = "有多个连续空格 第二段"  # 原文里是 "\n\n"，quote 里是单个空格

    outcome = verify_claim(make_claim(quote, raw_id=raw_id), raw, content_type="text/plain")

    assert outcome.status is VerificationStatus.VERIFIED
    anchor = outcome.anchor
    assert anchor is not None
    window = outcome.verification.raw_text[anchor.char_start : anchor.char_end]
    stripped = quote.strip()
    assert stripped[0] in window and stripped[-1] in window
    # 匹配区间确实跨越了原文的换行
    assert "\n\n" in window


def test_multiline_quote_matches_single_line_raw():
    raw = b"Alpha Beta Gamma Delta"
    raw_id = raw_id_for("ch_test", "https://example.invalid/ml", content_sha256(raw))
    outcome = verify_claim(
        make_claim("Beta\nGamma", raw_id=raw_id), raw, content_type="text/plain"
    )
    assert outcome.status is VerificationStatus.VERIFIED
    assert outcome.anchor is not None
    assert outcome.verification.raw_text[outcome.anchor.char_start : outcome.anchor.char_end] == (
        "Beta Gamma"
    )


# --------------------------------------------------------------------------- #
# 判据 3：匹配失败 → FAILED，且没有锚点
# --------------------------------------------------------------------------- #


def test_missing_quote_fails_without_anchor():
    outcome = verify_claim(
        make_claim("这句话在原文里根本不存在"), RAW_BYTES, content_type="text/plain"
    )

    assert outcome.status is VerificationStatus.FAILED
    assert outcome.anchor is None
    assert outcome.derived is None
    assert outcome.verification.raw_slice == ""
    # 失败的校验不得产出可持久化的证据行（结构性保证，而不是写库路径上的 if）
    with pytest.raises(AnchorError):
        EvidenceSpan.from_outcome(outcome)


def test_blank_quote_is_a_loud_failure_not_a_failed_status():
    raw = b"some raw text"
    raw_id = raw_id_for("ch_test", "https://example.invalid/blank", content_sha256(raw))
    claim = ProposedClaim(
        claim_id="clm_blank",
        raw_id=raw_id,
        kind="industry",
        value="ai",
        quote="   ",
        confidence=0.5,
        versions=UTC_VERSIONS,
        version=1,
    )
    with pytest.raises(AnchorError):
        verify_claim(claim, raw, content_type="text/plain")


# --------------------------------------------------------------------------- #
# 判据 4：篡改检测（坐标是重算的，不是采信的）
# --------------------------------------------------------------------------- #


def test_tampered_anchor_is_rejected_and_recomputation_wins():
    claim = make_claim()
    outcome = verify_claim(claim, RAW_BYTES, content_type="text/plain")
    real = outcome.anchor
    assert real is not None

    tampered = EvidenceAnchor.create(
        raw_id=real.raw_id,
        raw_sha256=real.raw_sha256,
        char_start=real.char_start + 1,
        char_end=real.char_end + 1,
    )
    assert tampered != real

    # 1) 声明了被篡改的坐标 → 响亮失败，绝不采信
    tampered_claim = claim.with_verification(VerificationStatus.VERIFIED, tampered)
    with pytest.raises(UnverifiedEvidenceError) as excinfo:
        verify_claim(tampered_claim, RAW_BYTES, content_type="text/plain")
    assert "不一致" in str(excinfo.value)

    # 2) 重算值仍然是**真值**，与传入的声明无关
    recomputed = verify_quote(
        raw_id=RAW_ID,
        raw_bytes=RAW_BYTES,
        quote=QUOTE,
        declared_anchor=tampered,
    )
    assert recomputed.anchor == real
    assert recomputed.anchor != tampered
    assert recomputed.declared_anchor_matches is False

    # 3) 声明未篡改时，比对通过
    honest = verify_quote(
        raw_id=RAW_ID, raw_bytes=RAW_BYTES, quote=QUOTE, declared_anchor=real
    )
    assert honest.declared_anchor_matches is True


def test_declared_anchor_with_wrong_raw_sha_is_rejected():
    claim = make_claim()
    real = verify_claim(claim, RAW_BYTES, content_type="text/plain").anchor
    assert real is not None

    other_raw = b"completely different bytes"
    other_sha = content_sha256(other_raw)
    forged = EvidenceAnchor.create(
        raw_id=real.raw_id,
        raw_sha256=other_sha,
        char_start=real.char_start,
        char_end=real.char_end,
    )
    forged_claim = claim.with_verification(VerificationStatus.VERIFIED, forged)
    with pytest.raises(UnverifiedEvidenceError):
        verify_claim(forged_claim, RAW_BYTES, content_type="text/plain")


def test_declared_anchor_for_another_raw_is_rejected():
    claim = make_claim()
    real = verify_claim(claim, RAW_BYTES, content_type="text/plain").anchor
    assert real is not None
    foreign = EvidenceAnchor.create(
        raw_id="raw_somewhere_else",
        raw_sha256=real.raw_sha256,
        char_start=real.char_start,
        char_end=real.char_end,
    )
    with pytest.raises(UnverifiedEvidenceError):
        verify_quote(
            raw_id=RAW_ID, raw_bytes=RAW_BYTES, quote=QUOTE, declared_anchor=foreign
        )


# --------------------------------------------------------------------------- #
# 判据 5：坐标只能由确定性匹配产出
# --------------------------------------------------------------------------- #


def test_proposed_claim_cannot_carry_coordinates():
    payload = {
        "claim_id": "clm_x",
        "raw_id": RAW_ID,
        "kind": "industry",
        "value": "ai",
        "quote": QUOTE,
        "confidence": 0.5,
        "versions": UTC_VERSIONS,
        "version": 1,
    }
    for key in ("char_start", "char_end", "anchor", "normalized_start"):
        with pytest.raises(ValidationError):
            ProposedClaim(**payload, **{key: 3})

    # 非 VERIFIED 状态不得携带 anchor（模型层再挡一次伪造坐标）
    with pytest.raises(ValidationError):
        ProposedClaim(
            **payload,
            anchor=EvidenceAnchor.create(
                raw_id=RAW_ID,
                raw_sha256=content_sha256(RAW_BYTES),
                char_start=0,
                char_end=5,
            ),
        )


def test_verification_entrypoints_take_no_coordinates():
    quote_params = set(inspect.signature(verify_quote).parameters)
    assert quote_params == {
        "raw_id",
        "raw_bytes",
        "quote",
        "content_type",
        "declared_anchor",
    }
    # 唯一与坐标有关的入参只用于**比对**，不参与计算
    assert set(inspect.signature(verify_claim).parameters) == {
        "claim",
        "raw_bytes",
        "content_type",
    }


def test_raw_sha256_is_computed_never_accepted():
    other = "Atlas 把证据锚定在原文上，坐标由确定性匹配算出，而不是让模型猜。".encode()
    raw_id = raw_id_for("ch_test", "https://example.invalid/a", content_sha256(other))
    base = verify_quote(raw_id=raw_id, raw_bytes=RAW_BYTES, quote=QUOTE)
    changed = verify_quote(raw_id=raw_id, raw_bytes=other + b"!", quote=QUOTE)
    assert base.raw_sha256 == content_sha256(RAW_BYTES)
    assert changed.raw_sha256 == content_sha256(other + b"!")
    assert base.raw_sha256 != changed.raw_sha256


def test_claim_without_store_version_is_rejected():
    with pytest.raises(VersionError):
        verify_claim(make_claim(version=0), RAW_BYTES, content_type="text/plain")


# --------------------------------------------------------------------------- #
# 实体场景（SPEC §2.2 的澄清：原文切片是实体字面量）
# --------------------------------------------------------------------------- #


def test_html_entity_quote_decodes_from_raw_slice():
    raw = "<p>Atlas &mdash; 证据锚定 &amp; 高亮</p>".encode("utf-8")
    raw_id = raw_id_for("ch_test", "https://example.invalid/html", content_sha256(raw))
    quote = "证据锚定 & 高亮"

    outcome = verify_claim(make_claim(quote, raw_id=raw_id), raw, content_type="text/html")
    assert outcome.status is VerificationStatus.VERIFIED
    anchor = outcome.anchor
    assert anchor is not None
    window = outcome.verification.raw_text[anchor.char_start : anchor.char_end]
    # 真值切片里是**实体的字面量**，不是解码后的字符
    assert "&amp;" in window
    # 判据按 SPEC §2.2 的精确表述：经实体解码后包含首尾非空白字符
    decoded = _html.unescape(window)
    assert quote[0] in decoded and quote[-1] in decoded


def test_entity_at_quote_end_aligns_to_entity_boundary():
    """归一化区间结束在实体字符上时，`to_raw_offset` 会停在实体字面量内部。

    本模块用**只看段表**的纯函数把端点对齐到段边界，使锚点能完整覆盖证据。
    """
    raw = "<p>Atlas &mdash;</p>".encode("utf-8")
    raw_id = raw_id_for("ch_test", "https://example.invalid/entity-end", content_sha256(raw))
    quote = "Atlas —"

    normalized = normalize(raw, "text/html")
    assert normalized.text == "Atlas —"
    status, naive, _derived = build_anchor(
        raw_id=raw_id,
        raw_sha256=content_sha256(raw),
        normalized_text=normalized.text,
        quote=quote,
        to_raw_offset=normalized.to_raw_offset,
    )
    assert status is VerificationStatus.VERIFIED
    naive_slice = normalized.raw_text[naive.char_start : naive.char_end]
    assert naive_slice == "Atlas &"  # 逐字符映射把实体字面量切在了第一个字符上
    assert naive.char_end == naive.char_start + len("Atlas &")

    outcome = verify_claim(make_claim(quote, raw_id=raw_id), raw, content_type="text/html")
    assert outcome.status is VerificationStatus.VERIFIED
    anchor = outcome.anchor
    assert anchor is not None
    assert anchor.char_start == naive.char_start
    assert anchor.char_end > naive.char_end
    window = outcome.verification.raw_text[anchor.char_start : anchor.char_end]
    assert window == "Atlas &mdash;"
    # 端点对齐后仍必须满足判据 1（按 SPEC §2.2：切片**经实体解码后**包含首尾字符）
    decoded = _html.unescape(window)
    assert decoded == quote
    assert quote[0] in decoded and quote[-1] in decoded


# --------------------------------------------------------------------------- #
# 与 T-108 的衔接（SPEC §2.3：未经校验的证据不得进入 Confirmed）
# --------------------------------------------------------------------------- #


def test_verified_claim_enables_confirmed_while_unverified_does_not():
    claim = make_claim()
    with pytest.raises(UnverifiedEvidenceError):
        ConfirmedLabel.from_proposal(claim, actor="reviewer")

    outcome = verify_claim(claim, RAW_BYTES, content_type="text/plain")
    verified = outcome.apply_to(claim)
    assert verified.verification_status is VerificationStatus.VERIFIED
    assert verified.anchor == outcome.anchor
    assert verified.claim_id == claim.claim_id and verified.version == claim.version

    label = ConfirmedLabel.from_proposal(verified, actor="reviewer")
    assert label.anchor == outcome.anchor
    assert label.from_claim_id == claim.claim_id


def test_failed_outcome_applies_as_failed_claim():
    claim = make_claim("原文里没有的句子")
    outcome = verify_claim(claim, RAW_BYTES, content_type="text/plain")
    failed = outcome.apply_to(claim)
    assert failed.verification_status is VerificationStatus.FAILED
    assert failed.anchor is None
    with pytest.raises(UnverifiedEvidenceError):
        ConfirmedLabel.from_proposal(failed, actor="reviewer")


def test_apply_to_rejects_foreign_claim():
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")
    with pytest.raises(UnverifiedEvidenceError):
        outcome.apply_to(make_claim(version=2))


# --------------------------------------------------------------------------- #
# 判据 6：展示载荷
# --------------------------------------------------------------------------- #


def test_highlight_payload_is_self_consistent():
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")
    payload = build_highlight(outcome.verification, context_chars=5)

    assert payload.status is VerificationStatus.VERIFIED
    assert len(payload.regions) == 1
    region = payload.regions[0]
    anchor = outcome.anchor
    assert anchor is not None
    assert (region.char_start, region.char_end) == (anchor.char_start, anchor.char_end)
    # 原文切片由 payload 自己持有的文本重新切出 —— 自洽（模型校验器也钉了一遍）
    assert region.raw_text == payload.raw_text[region.char_start : region.char_end]
    assert region.normalized_text == payload.normalized_text[
        region.normalized_start : region.normalized_end
    ]
    assert region.normalized_text == QUOTE
    assert region.decoded_text == QUOTE
    # 上下文窗口覆盖高亮区间，且自身自洽
    assert payload.context_char_start <= region.char_start
    assert payload.context_char_end >= region.char_end
    assert payload.context_text == payload.raw_text[
        payload.context_char_start : payload.context_char_end
    ]
    assert region.raw_text in payload.context_text


def test_highlight_payload_for_failed_verification_has_no_regions():
    outcome = verify_claim(make_claim("不存在"), RAW_BYTES, content_type="text/plain")
    payload = build_highlight(outcome.verification)

    assert payload.status is VerificationStatus.FAILED
    assert payload.regions == ()
    # 仍然给出原文与归一化文本：T-109 需要展示"未验证"而不是空白
    assert payload.raw_text == RAW_TEXT
    assert payload.normalized_text == outcome.verification.normalized_text
    assert payload.quote == "不存在"


def test_highlight_for_span_rederives_and_detects_tampering():
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")
    span = EvidenceSpan.from_outcome(outcome)
    assert (span.char_start, span.char_end) == (
        outcome.anchor.char_start,
        outcome.anchor.char_end,
    )

    payload = highlight_for_span(span, RAW_BYTES, content_type="text/plain")
    assert payload.regions[0].raw_text == QUOTE
    assert payload.raw_id == RAW_ID and payload.raw_sha256 == span.raw_sha256

    # 篡改后的 span 在展示路径上同样被拒绝（不展示错误的高亮）
    tampered = EvidenceSpan(
        claim_id=span.claim_id,
        claim_version=span.claim_version,
        raw_id=span.raw_id,
        raw_sha256=span.raw_sha256,
        quote=span.quote,
        char_start=span.char_start + 2,
        char_end=span.char_end + 2,
        normalized_start=span.normalized_start,
        normalized_end=span.normalized_end,
        verified_at=span.verified_at,
    )
    with pytest.raises(UnverifiedEvidenceError):
        highlight_for_span(tampered, RAW_BYTES, content_type="text/plain")


def test_highlight_context_is_clamped_to_raw_bounds():
    outcome = verify_claim(make_claim(), RAW_BYTES, content_type="text/plain")
    payload = build_highlight(outcome.verification, context_chars=10000)
    assert payload.context_char_start == 0
    assert payload.context_char_end == len(RAW_TEXT)
    assert payload.context_text == RAW_TEXT
