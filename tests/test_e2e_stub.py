"""T-002 最小端到端桩：Raw → Proposed → Confirmed。

这是 SPEC §4.1 要求的完成判据第 2 条：三态不变量必须**实际跑通**，
而不是只把类型定义写出来。归档基线的 `entities` / `categories` 就是
"类型存在但没有一条路径真的写进去"，最终成为空壳。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.contracts import (
    ConfirmedLabel,
    ConfirmedStore,
    ImmutabilityError,
    ProposedClaim,
    ProposedStore,
    RawRecord,
    RawStore,
    TaskVersions,
    UnverifiedEvidenceError,
    VerificationStatus,
    build_anchor,
)

VERSIONS = TaskVersions(
    code_version="t002-contract@1",
    config_version="industries@0",
    model_version="pi-ai:stub",
)


def test_stub_raw_to_proposed_to_confirmed():
    raw_store, proposed_store, confirmed_store = RawStore(), ProposedStore(), ConfirmedStore()

    # ---- 1. Raw：只增不改 -------------------------------------------------
    content = "OpenAI 发布了新的推理模型，显著降低了延迟。".encode("utf-8")
    record = RawRecord.create(
        channel_id="openai-blog",
        endpoint="https://openai.com/blog/rss/",
        content=content,
        http_status=200,
    )
    raw_store.put(record, content)
    assert raw_store.get_content(record.raw_id) == content

    # 幂等：同内容重复抓取不产生第二条
    assert raw_store.put(record, content).raw_id == record.raw_id
    assert len(raw_store.all_raw_ids()) == 1

    # 不可改：字段冻结 + 封死 model_copy 后门
    with pytest.raises(ValidationError):
        record.endpoint = "https://evil.example/"  # type: ignore[misc]
    with pytest.raises(ImmutabilityError):
        record.model_copy(update={"endpoint": "https://evil.example/"})

    # ---- 2. Proposed：只带 quote，不带坐标 ---------------------------------
    normalized_text = content.decode("utf-8")
    quote = "显著降低了延迟"
    claim = ProposedClaim.propose(
        raw_id=record.raw_id,
        kind="industry",
        value="人工智能",
        quote=quote,
        confidence=0.82,
        versions=VERSIONS,
    )
    v1 = proposed_store.append(claim)
    assert v1.version == 1
    assert v1.verification_status is VerificationStatus.UNVERIFIED
    assert v1.anchor is None, "抽取阶段不得携带坐标"

    # ---- 3. 未校验的证据不得进入 Confirmed ---------------------------------
    with pytest.raises(UnverifiedEvidenceError):
        ConfirmedLabel.from_proposal(v1, actor="me")

    # ---- 4. 确定性校验：quote -> anchor ------------------------------------
    status, anchor, derived = build_anchor(
        raw_id=record.raw_id,
        raw_sha256=record.content_sha256,
        normalized_text=normalized_text,
        quote=quote,
        to_raw_offset=lambda i: i,  # stub：恒等映射（真实映射由 T-104 提供）
    )
    assert status is VerificationStatus.VERIFIED
    assert anchor is not None and anchor.length == len(quote)
    assert normalized_text[anchor.char_start : anchor.char_end] == quote

    # 覆写 = 追加新版本，旧版本保留
    v2 = proposed_store.append(v1.with_verification(status, anchor))
    assert v2.version == 2
    assert proposed_store.version_count(claim.claim_id) == 2
    assert proposed_store.history(claim.claim_id)[0].verification_status is (
        VerificationStatus.UNVERIFIED
    )
    assert proposed_store.current(claim.claim_id).version == 2  # type: ignore[union-attr]

    # ---- 5. 校验后可以进入 Confirmed --------------------------------------
    confirmed = ConfirmedLabel.from_proposal(v2, actor="me")
    confirmed_store.add(confirmed)
    assert confirmed.anchor == anchor
    assert confirmed_store.latest_value(record.raw_id, "industry") == "人工智能"

    # 重复提交同一判断 → 幂等
    confirmed_store.add(confirmed)
    assert confirmed_store.count() == 1

    # ---- 6. 人工直判（1A）：文档级，无需 AI 证据 ---------------------------
    human = ConfirmedLabel.human(
        raw_id=record.raw_id, label_key="valid", label_value="yes", actor="me"
    )
    confirmed_store.add(human)
    assert confirmed_store.latest_value(record.raw_id, "valid") == "yes"

    # 人工改判 → 新记录（只增不改）
    confirmed_store.add(
        ConfirmedLabel.human(
            raw_id=record.raw_id, label_key="valid", label_value="no", actor="me"
        )
    )
    assert confirmed_store.count() == 3
    assert confirmed_store.latest_value(record.raw_id, "valid") == "no"


def test_stub_failed_verification_never_reaches_confirmed():
    """quote 在原文中找不到时，必须停在 FAILED，且无法产生 Confirmed。"""
    raw_store, proposed_store = RawStore(), ProposedStore()
    content = "完全无关的一段文本。".encode("utf-8")
    record = RawRecord.create(channel_id="ch", endpoint="http://x", content=content)
    raw_store.put(record, content)

    claim = ProposedClaim.propose(
        raw_id=record.raw_id,
        kind="industry",
        value="人工智能",
        quote="这段引用并不存在于原文中",
        confidence=0.9,
        versions=VERSIONS,
    )
    status, anchor, derived = build_anchor(
        raw_id=record.raw_id,
        raw_sha256=record.content_sha256,
        normalized_text=content.decode("utf-8"),
        quote=claim.quote,
        to_raw_offset=lambda i: i,
    )
    assert status is VerificationStatus.FAILED
    assert anchor is None and derived is None

    failed = proposed_store.append(claim.with_verification(status, anchor))
    with pytest.raises(UnverifiedEvidenceError):
        ConfirmedLabel.from_proposal(failed, actor="me")
