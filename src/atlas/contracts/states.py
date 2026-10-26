"""三态契约（SPEC §2.3 / §2.4 / §2.10）。

| 层 | 不可变性 | 谁产出 |
|---|---|---|
| Raw | **只增不改** | 采集 |
| 归一化 | 可重建（纯函数） | 解析（T-104，不在本模块） |
| Proposed | **可覆写**（保留版本痕迹） | 机器 |
| Confirmed | **只增不改** | 人工 |

关于「可覆写」的实现方式：**不是**改已有实例，而是**显式构造新版本记录**，
旧版本保留在版本链中。`ContractModel` 已封死 `model_copy(update=...)` 后门，
因此本模块所有版本推进都走显式构造（见 `ProposedClaim.with_version`）。

关键不变量：**Confirmed 只能由「证据已校验」的 Proposed 派生**。
但 1A 决定人工可以直接判断"这条有效"，无需 AI 证据。因此提供两个入口，
语义不同、要求不同：

- `ConfirmedLabel.human(...)`        人工直判，不要求证据；
  **文档级**（`anchor=None`，1A）与**条目级**（带该条目的字符区间，2C）两种粒度
- `ConfirmedLabel.from_proposal(...)` 确认 AI 提议，**必须** VERIFIED + anchor

Store 为契约级实现（内存版）。T-103 用文件系统 + stdlib `sqlite3` 实现同一契约，
Confirmed 的禁改由 SQL 触发器强制（§2.10）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import Field, model_validator

from .anchors import EvidenceAnchor, VerificationStatus
from .base import ContractModel
from .errors import (
    AnchorError,
    IdError,
    ImmutabilityError,
    InvalidContractStateError,
    NotFoundError,
    UnverifiedEvidenceError,
    VersionError,
)
from .ids import claim_id_for, content_sha256, label_id_for, raw_id_for
from .tasks import TaskVersions


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Raw —— 只增不改
# --------------------------------------------------------------------------- #


class RawRecord(ContractModel):
    """原文元数据。内容本身由 blob 存储（T-103 落盘）。

    `entry_kind`（T-109 补完时新增，**可选、不参与 `raw_id`**）
    ------------------------------------------------------------

    SPEC §6.3 裁决 B 把 raw 分成三类：**feed-Raw 是容器**（它的派生条目才是条目），
    **article-Raw 本身就是条目**，无内容的既不是容器也不是条目。
    这个分类必须能被消费者机器可判，否则 feed 前端只能靠"试着解析一下"来猜。

    | 值 | 含义 |
    |---|---|
    | `None`（默认） | 未标定 ⇒ **不当作容器**：整篇原文就是一个条目 |
    | `"feed"` | 容器：内容是一份 feed，条目由 `atlas.entries` 派生 |

    为什么用 `Optional[str]` 而不是 `bool`：将来可能出现第三种容器形态
    （例如 JSON API 响应），枚举比布尔好扩展；而**默认 `None` 必须解释成
    "不是容器"**——反过来会让所有既有记录（默认 `None`）突然从 feed 里消失，
    那是静默的数据消失，不是分类。

    为什么**不参与 `raw_id`**：`raw_id = f(channel_id, endpoint, content_sha256)`
    是内容寻址的。把分类掺进 id 会让"同一条原文按不同 `entry_kind` 写入"
    变成两条 Raw，而 Raw 只增不改 ⇒ 改分类等于新建 Raw。分类因此是
    **元数据**，不是身份的一部分。
    """

    raw_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    fetched_at: datetime
    http_status: Optional[int] = None
    entry_kind: Optional[str] = None

    @classmethod
    def create(
        cls,
        *,
        channel_id: str,
        endpoint: str,
        content: bytes,
        fetched_at: Optional[datetime] = None,
        http_status: Optional[int] = None,
        entry_kind: Optional[str] = None,
    ) -> "RawRecord":
        digest = content_sha256(content)
        return cls(
            raw_id=raw_id_for(channel_id, endpoint, digest),
            channel_id=channel_id,
            endpoint=endpoint,
            content_sha256=digest,
            byte_length=len(content),
            fetched_at=fetched_at or _utcnow(),
            http_status=http_status,
            entry_kind=entry_kind,
        )


class RawStore:
    """Raw 存储契约：只有 put/get，**没有** update/delete。"""

    def __init__(self) -> None:
        self._records: Dict[str, RawRecord] = {}
        self._blobs: Dict[str, bytes] = {}

    def put(self, record: RawRecord, content: bytes) -> RawRecord:
        actual = content_sha256(content)
        if actual != record.content_sha256:
            raise IdError(
                f"内容指纹与标识不一致：声明 {record.content_sha256[:12]}… 实际 {actual[:12]}…"
            )
        existing = self._records.get(record.raw_id)
        if existing is not None:
            if existing.content_sha256 == record.content_sha256:
                return existing  # 幂等：同内容重复抓取不是错误
            raise ImmutabilityError(f"raw_id 已存在且内容不同：{record.raw_id}")
        self._records[record.raw_id] = record
        self._blobs[record.raw_id] = content
        return record

    def get(self, raw_id: str) -> RawRecord:
        try:
            return self._records[raw_id]
        except KeyError as exc:
            raise NotFoundError(f"raw_id 不存在：{raw_id}") from exc

    def get_content(self, raw_id: str) -> bytes:
        self.get(raw_id)
        return self._blobs[raw_id]

    def all_raw_ids(self) -> List[str]:
        return sorted(self._records)


# --------------------------------------------------------------------------- #
# Proposed —— 可覆写（= 追加新版本）
# --------------------------------------------------------------------------- #


class ProposedClaim(ContractModel):
    """机器产出。**只携带 quote，不携带坐标**（坐标由校验阶段产生）。

    `extra="forbid"`（继承自 `ContractModel`）在这里是实质约束：
    抽取器若试图塞 `char_start=` 之类字段，在类型层就构造不出来。
    """

    claim_id: str = Field(min_length=1)
    raw_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)          # 例如 "industry" / "topic"
    value: str = Field(min_length=1)         # 必须来自当前行业配置（§2.9）
    quote: str = Field(min_length=1)         # 证据：文字，不是坐标
    confidence: float = Field(ge=0.0, le=1.0)
    version: int = Field(default=0, ge=0)    # 0 = 尚未入 store
    versions: TaskVersions
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    anchor: Optional[EvidenceAnchor] = None
    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _check_evidence(self) -> "ProposedClaim":
        if self.verification_status is VerificationStatus.VERIFIED and self.anchor is None:
            raise ValueError("VERIFIED 状态必须携带 anchor")
        if self.verification_status is not VerificationStatus.VERIFIED and self.anchor is not None:
            raise ValueError("非 VERIFIED 状态不得携带 anchor（避免伪造坐标）")
        return self

    @classmethod
    def propose(
        cls,
        *,
        raw_id: str,
        kind: str,
        value: str,
        quote: str,
        confidence: float,
        versions: TaskVersions,
    ) -> "ProposedClaim":
        return cls(
            claim_id=claim_id_for(raw_id, kind, quote),
            raw_id=raw_id,
            kind=kind,
            value=value,
            quote=quote,
            confidence=confidence,
            versions=versions,
        )

    def _rebuild(self, **changes: object) -> "ProposedClaim":
        """显式构造新记录（唯一合法的"覆写"方式）。"""
        fields = {
            "claim_id": self.claim_id,
            "raw_id": self.raw_id,
            "kind": self.kind,
            "value": self.value,
            "quote": self.quote,
            "confidence": self.confidence,
            "version": self.version,
            "versions": self.versions,
            "verification_status": self.verification_status,
            "anchor": self.anchor,
            "created_at": _utcnow(),
        }
        fields.update(changes)
        return ProposedClaim(**fields)  # type: ignore[arg-type]

    def with_verification(
        self, status: VerificationStatus, anchor: Optional[EvidenceAnchor]
    ) -> "ProposedClaim":
        """产出校验后的**新实例**（原实例不变）。"""
        if status is VerificationStatus.VERIFIED and anchor is None:
            raise AnchorError("VERIFIED 必须携带 anchor")
        if status is not VerificationStatus.VERIFIED and anchor is not None:
            raise InvalidContractStateError("非 VERIFIED 状态不得携带 anchor")
        return self._rebuild(verification_status=status, anchor=anchor)

    def with_version(self, version: int) -> "ProposedClaim":
        if version < 1:
            raise VersionError(f"version 必须 ≥ 1，收到 {version}")
        return self._rebuild(version=version)


class ProposedStore:
    """Proposed 存储契约：覆写 = 追加新版本，旧版本保留。"""

    def __init__(self) -> None:
        self._versions: Dict[str, List[ProposedClaim]] = {}

    def append(self, claim: ProposedClaim) -> ProposedClaim:
        history = self._versions.setdefault(claim.claim_id, [])
        stored = claim.with_version(len(history) + 1)
        history.append(stored)
        return stored

    def current(self, claim_id: str) -> Optional[ProposedClaim]:
        history = self._versions.get(claim_id)
        return history[-1] if history else None

    def history(self, claim_id: str) -> List[ProposedClaim]:
        return list(self._versions.get(claim_id, []))

    def by_raw(self, raw_id: str) -> List[ProposedClaim]:
        return [h[-1] for h in self._versions.values() if h and h[-1].raw_id == raw_id]

    def version_count(self, claim_id: str) -> int:
        return len(self._versions.get(claim_id, []))


# --------------------------------------------------------------------------- #
# Confirmed —— 只增不改
# --------------------------------------------------------------------------- #


class ConfirmedLabel(ContractModel):
    """人工产出。**文档级（1A）+ 条目级（2C 字符区间）**，只增不改。

    两种粒度，同一个内存形状（SPEC §2.1 按 §6.3 裁决 B 更新后的表述）：

    | 粒度 | 入口 | `anchor` | 依附对象 |
    |---|---|---|---|
    | **文档级**（1A 人工直判） | `human(..., anchor=None)` | `None` | 整份 `raw_id` |
    | **条目级**（2C 字符区间） | `human(..., anchor=<该条目区间>)` | 非空 | `raw_id` 上的 `[char_start, char_end)` |

    为什么条目级只要一个可选参数就够：存储层的四个锚点列**早就在读写**
    （`labels/sqlite_store.py`，含 `CHECK (anchor_raw_id IS NULL OR anchor_raw_id = raw_id)`
    与"要么全空要么全有"），`Entry.anchor()` 也已经产出合法 `EvidenceAnchor`。
    缺的只有内存契约这一环。

    **`anchor` 必须与 `label.raw_id` 锚在同一份原文上**：否则一条标签会有两个依附对象
    （文档级 vs 条目级）。`human()` 在工厂层直接拦（`AnchorError`），存储层另有对称的
    SQL `CHECK` 与 `AnchorError` —— 两条入口都拦得住。**字段级直接构造**（例如
    `ConfirmedLabel(...)` / 导出的回流）故意不拦：那是 T-108 已登记的有意单向差异
    （存储层 ⊇ 契约，见 SPEC §2.13 与 `tests/test_labels_store.py`）。

    **`label_id` 刻意不含 `anchor`**（详见 `atlas.contracts.ids.label_id_for`）：
    `label_id` 的内容寻址口径在本项目里是**跨模块一致的**——`labels/sqlite_store.py`
    的 `_assert_label_id` 与 `labels/export.py` 的 `verify_label_id` 都按
    `(raw_id, label_key, label_value, actor)` 重算。把它改成含 `anchor` 会让
    **已落库的标签无法再导出/回流**（Confirmed 只增不改，改不回去）。
    代价是"同一文档上同一个人的同维度同取值、锚在不同区间"会撞同一个 `label_id`；
    因此这类撞击必须**显式可见**（前端拒绝静默丢弃），而不是靠 id 隐式区分。
    """

    label_id: str = Field(min_length=1)
    raw_id: str = Field(min_length=1)
    label_key: str = Field(min_length=1)
    label_value: str
    actor: str = Field(min_length=1)
    from_claim_id: Optional[str] = None
    anchor: Optional[EvidenceAnchor] = None
    created_at: datetime = Field(default_factory=_utcnow)

    @classmethod
    def human(
        cls,
        *,
        raw_id: str,
        label_key: str,
        label_value: str,
        actor: str,
        anchor: Optional[EvidenceAnchor] = None,
    ) -> "ConfirmedLabel":
        """人工直判，不要求 AI 证据。

        - `anchor=None`（默认）⇒ **文档级**判断，行为与 1A 完全一致；
        - `anchor=<条目区间>` ⇒ **条目级**判断：锚定 `raw_id` 上的
          `(raw_sha256, char_start, char_end)`，即 SPEC §2.2 的真值形状。
          条目区间请用 `atlas.entries.Entry.anchor()` 产出，
          **绝不要**把 `entry_id` 塞进来（ID 是派生量，换解析器会漂移）。

        Raises:
            AnchorError: `anchor.raw_id` 与 `raw_id` 不同 —— 一条标签只能有一个
                依附对象（文档级**或**它的一个字符区间）。存储层的 SQL `CHECK`
                与 `AnchorError` 会做同一件事；这里在**进存储层之前**就拦，
                让错误离调用点更近（`test_labels_store.py` 钉死的"存储层更严"那条
                指的是**字段级构造**路径，不是本工厂路径）。
        """
        if anchor is not None and anchor.raw_id != raw_id:
            raise AnchorError(
                f"证据锚点锚在 {anchor.raw_id}，而标签锚在 {raw_id}："
                "人工标签只锚 raw_id（文档级）或它的一个字符区间（条目级），"
                "不得锚到别的原文 / 解析产物 / 块 ID / 归一化偏移（SPEC §2.1）"
            )
        return cls(
            label_id=label_id_for(raw_id, label_key, label_value, actor),
            raw_id=raw_id,
            label_key=label_key,
            label_value=label_value,
            actor=actor,
            anchor=anchor,
        )

    @classmethod
    def from_proposal(
        cls, claim: ProposedClaim, *, actor: str, label_value: Optional[str] = None
    ) -> "ConfirmedLabel":
        """确认 AI 提议：**必须**证据已校验，否则响亮失败（SPEC §2.3）。"""
        if claim.verification_status is not VerificationStatus.VERIFIED or claim.anchor is None:
            raise UnverifiedEvidenceError(
                f"claim {claim.claim_id} 的证据状态为 {claim.verification_status.value}，"
                "未经确定性校验的证据不得进入 Confirmed"
            )
        value = claim.value if label_value is None else label_value
        return cls(
            label_id=label_id_for(claim.raw_id, claim.kind, value, actor),
            raw_id=claim.raw_id,
            label_key=claim.kind,
            label_value=value,
            actor=actor,
            from_claim_id=claim.claim_id,
            anchor=claim.anchor,
        )


class ConfirmedStore:
    """Confirmed 存储契约：只有 add/查询，**没有** update/delete。"""

    def __init__(self) -> None:
        self._labels: Dict[str, ConfirmedLabel] = {}

    def add(self, label: ConfirmedLabel) -> ConfirmedLabel:
        if label.label_id in self._labels:
            return self._labels[label.label_id]  # 幂等：同一人重复提交同一判断
        self._labels[label.label_id] = label
        return label

    def all_for(self, raw_id: str) -> List[ConfirmedLabel]:
        return [lbl for lbl in self._labels.values() if lbl.raw_id == raw_id]

    def latest_value(self, raw_id: str, label_key: str) -> Optional[str]:
        matches = [lbl for lbl in self.all_for(raw_id) if lbl.label_key == label_key]
        if not matches:
            return None
        return max(matches, key=lambda lbl: lbl.created_at).label_value

    def count(self) -> int:
        return len(self._labels)
