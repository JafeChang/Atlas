"""T-002 契约不变量测试。

每一条测试对应 SPEC 里一条**必须被代码强制**的规则；不是"文档约定"。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.contracts import (
    AnchorError,
    AtlasTask,
    ConfirmedLabel,
    ConfirmedStore,
    EvidenceAnchor,
    IdError,
    IdError,
    ImmutabilityError,
    NotFoundError,
    ProposedClaim,
    ProposedStore,
    RawRecord,
    RawStore,
    Snapshot,
    TaskContractError,
    TaskVersions,
    UnverifiedEvidenceError,
    VerificationStatus,
    build_anchor,
    match_quote,
)

V1 = TaskVersions(code_version="c1", config_version="g1", model_version="m1")
V2 = TaskVersions(code_version="c2", config_version="g1", model_version="m1")
HEX64 = "a" * 64


# --------------------------------------------------------------------------- #
# 锚点
# --------------------------------------------------------------------------- #


def test_anchor_rejects_empty_range():
    with pytest.raises(AnchorError):
        EvidenceAnchor.create(raw_id="r", raw_sha256=HEX64, char_start=50, char_end=50)
    with pytest.raises(AnchorError):
        EvidenceAnchor.create(raw_id="r", raw_sha256=HEX64, char_start=50, char_end=10)


def test_anchor_requires_64_hex():
    with pytest.raises(ValidationError):
        EvidenceAnchor(raw_id="r", raw_sha256="short", char_start=0, char_end=1)


def test_anchor_is_frozen():
    anchor = EvidenceAnchor.create(raw_id="r", raw_sha256=HEX64, char_start=0, char_end=5)
    with pytest.raises(ValidationError):
        anchor.char_end = 99  # type: ignore[misc]
    assert anchor.length == 5


def test_match_quote_exact_and_whitespace_tolerant():
    text = "第一段。\n\nOpenAI   发布了新模型，显著降低了延迟。"
    assert match_quote(text, "显著降低了延迟") == (text.index("显著"), text.index("延迟") + 2)
    # 空白差异应仍能匹配
    assert match_quote(text, "OpenAI 发布了新模型") is not None


def test_match_quote_returns_none_when_absent():
    assert match_quote("无关文本", "不存在的引用") is None


def test_match_quote_rejects_empty_quote():
    with pytest.raises(AnchorError):
        match_quote("文本", "   ")


# --------------------------------------------------------------------------- #
# ContractModel：封死 pydantic 的 frozen 后门
# --------------------------------------------------------------------------- #


def test_model_copy_update_is_blocked():
    anchor = EvidenceAnchor.create(raw_id="r", raw_sha256=HEX64, char_start=0, char_end=5)
    with pytest.raises(ImmutabilityError):
        anchor.model_copy(update={"char_end": 10})


def test_extra_fields_are_forbidden():
    """抽取器不得把坐标塞进 Proposed —— 类型层就构造不出来。"""
    with pytest.raises(ValidationError):
        ProposedClaim(
            claim_id="c",
            raw_id="r",
            kind="industry",
            value="AI",
            quote="q",
            confidence=0.5,
            versions=V1,
            char_start=3,  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------------- #
# Raw：只增不改
# --------------------------------------------------------------------------- #


def test_raw_put_is_idempotent_for_identical_content():
    store = RawStore()
    content = b"hello"
    rec = RawRecord.create(channel_id="ch", endpoint="http://x", content=content)
    first = store.put(rec, content)
    second = store.put(rec, content)
    assert first.raw_id == second.raw_id
    assert len(store.all_raw_ids()) == 1


def test_raw_put_rejects_content_hash_mismatch():
    store = RawStore()
    rec = RawRecord.create(channel_id="ch", endpoint="http://x", content=b"hello")
    with pytest.raises(IdError):
        store.put(rec, b"tampered")


def test_raw_store_refuses_same_id_with_different_content():
    store = RawStore()
    a = b"aaa"
    rec_a = RawRecord.create(channel_id="ch", endpoint="http://x", content=a)
    store.put(rec_a, a)

    b = b"bbb"
    rec_b = RawRecord.create(channel_id="ch", endpoint="http://x", content=b)
    forged = RawRecord(
        raw_id=rec_a.raw_id,  # 伪造：挂到已存在的 raw_id 上
        channel_id="ch",
        endpoint="http://x",
        content_sha256=rec_b.content_sha256,
        byte_length=len(b),
        fetched_at=rec_b.fetched_at,
    )
    with pytest.raises(ImmutabilityError):
        store.put(forged, b)


def test_raw_store_exposes_no_mutation_api():
    for forbidden in ("update", "delete", "remove", "overwrite", "set"):
        assert not hasattr(RawStore, forbidden), f"RawStore 不应提供 {forbidden}"


def test_raw_get_missing_raises_not_found():
    with pytest.raises(NotFoundError):
        RawStore().get("raw_missing")


# --------------------------------------------------------------------------- #
# Proposed：可覆写 = 追加新版本
# --------------------------------------------------------------------------- #


def _propose(raw_id: str = "raw_x", quote: str = "引用文字") -> ProposedClaim:
    return ProposedClaim.propose(
        raw_id=raw_id, kind="industry", value="人工智能", quote=quote, confidence=0.7, versions=V1
    )


def test_proposed_version_chain_keeps_history():
    store = ProposedStore()
    claim = _propose()
    v1 = store.append(claim)
    v2 = store.append(claim)
    assert (v1.version, v2.version) == (1, 2)
    assert store.version_count(claim.claim_id) == 2
    assert store.current(claim.claim_id).version == 2  # type: ignore[union-attr]
    assert store.history(claim.claim_id)[0].version == 1


def test_proposed_claim_id_is_deterministic():
    assert _propose().claim_id == _propose().claim_id
    assert _propose().claim_id != _propose(quote="另一段引用").claim_id


def test_unverified_claim_cannot_carry_anchor():
    anchor = EvidenceAnchor.create(raw_id="raw_x", raw_sha256=HEX64, char_start=0, char_end=3)
    with pytest.raises(ValidationError):
        ProposedClaim(
            claim_id="c",
            raw_id="raw_x",
            kind="industry",
            value="AI",
            quote="q",
            confidence=0.5,
            versions=V1,
            verification_status=VerificationStatus.UNVERIFIED,
            anchor=anchor,
        )


def test_with_verification_returns_new_instance():
    claim = _propose()
    anchor = EvidenceAnchor.create(raw_id="raw_x", raw_sha256=HEX64, char_start=0, char_end=3)
    verified = claim.with_verification(VerificationStatus.VERIFIED, anchor)
    assert claim.verification_status is VerificationStatus.UNVERIFIED
    assert verified.verification_status is VerificationStatus.VERIFIED
    assert verified.anchor == anchor


# --------------------------------------------------------------------------- #
# Confirmed：只增不改 + 未校验证据不得进入
# --------------------------------------------------------------------------- #


def test_confirmed_from_unverified_proposal_is_refused():
    claim = ProposedStore().append(_propose())
    with pytest.raises(UnverifiedEvidenceError):
        ConfirmedLabel.from_proposal(claim, actor="me")


def test_confirmed_from_verified_proposal_is_allowed():
    raw_id = "raw_x"
    claim = _propose(raw_id=raw_id)
    anchor = EvidenceAnchor.create(raw_id=raw_id, raw_sha256=HEX64, char_start=2, char_end=6)
    verified = claim.with_verification(VerificationStatus.VERIFIED, anchor)
    label = ConfirmedLabel.from_proposal(verified, actor="me")
    assert label.anchor == anchor
    assert label.from_claim_id == verified.claim_id


def test_human_label_needs_no_evidence_1a():
    """1A：人工判断是文档级的，不要求 AI 证据。"""
    label = ConfirmedLabel.human(
        raw_id="raw_x", label_key="valid", label_value="yes", actor="me"
    )
    assert label.anchor is None and label.from_claim_id is None


def test_confirmed_store_is_append_only_and_idempotent():
    store = ConfirmedStore()
    label = ConfirmedLabel.human(
        raw_id="raw_x", label_key="valid", label_value="yes", actor="me"
    )
    store.add(label)
    store.add(label)  # 重复提交同一判断 → 幂等
    assert store.count() == 1
    for forbidden in ("update", "delete", "remove", "overwrite"):
        assert not hasattr(ConfirmedStore, forbidden), f"ConfirmedStore 不应提供 {forbidden}"


def test_different_label_value_creates_new_record():
    store = ConfirmedStore()
    store.add(ConfirmedLabel.human(raw_id="r", label_key="valid", label_value="yes", actor="me"))
    store.add(ConfirmedLabel.human(raw_id="r", label_key="valid", label_value="no", actor="me"))
    assert store.count() == 2


# --------------------------------------------------------------------------- #
# ID 策略
# --------------------------------------------------------------------------- #


def test_ids_reject_empty_input():
    from atlas.contracts import claim_id_for, raw_id_for

    with pytest.raises(IdError):
        raw_id_for("", "http://x", HEX64)
    with pytest.raises(IdError):
        claim_id_for("raw_x", "industry", "   ")


def test_raw_id_is_content_addressed():
    a = RawRecord.create(channel_id="ch", endpoint="http://x", content=b"same")
    b = RawRecord.create(channel_id="ch", endpoint="http://x", content=b"same")
    c = RawRecord.create(channel_id="ch", endpoint="http://x", content=b"other")
    assert a.raw_id == b.raw_id
    assert a.raw_id != c.raw_id


# --------------------------------------------------------------------------- #
# 任务规范
# --------------------------------------------------------------------------- #


class _EchoTask(AtlasTask):
    name = "echo"

    def run(self, inputs, config):
        return {"echo": inputs.payload}


class _UnnamedTask(AtlasTask):
    def run(self, inputs, config):
        return {}


def test_task_output_carries_versions_and_idempotency_key():
    task = _EchoTask(V1)
    inputs = Snapshot(payload={"a": 1})
    config = Snapshot(payload={"b": 2})
    out1 = task.execute(inputs, config)
    out2 = task.execute(inputs, config)
    assert out1.idempotency_key == out2.idempotency_key
    assert out1.versions == V1
    assert out1.task_name == "echo"


def test_task_key_changes_with_code_version():
    inputs, config = Snapshot(payload={"a": 1}), Snapshot(payload={})
    assert _EchoTask(V1).idempotency_key(inputs, config) != _EchoTask(V2).idempotency_key(
        inputs, config
    )


def test_task_key_is_key_order_insensitive():
    a = Snapshot(payload={"x": 1, "y": 2})
    b = Snapshot(payload={"y": 2, "x": 1})
    assert a.digest() == b.digest()


def test_task_requires_name():
    with pytest.raises(TaskContractError):
        _UnnamedTask(V1).execute(Snapshot(), Snapshot())


def test_task_propagates_run_errors():
    class _Boom(AtlasTask):
        name = "boom"

        def run(self, inputs, config):
            raise NotImplementedError("尚未实现")

    with pytest.raises(NotImplementedError):
        _Boom(V1).execute(Snapshot(), Snapshot())
