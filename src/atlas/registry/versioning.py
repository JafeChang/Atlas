"""配置版本化与回滚（SPEC §2.9「版本化与回滚」）。

三条不可退让的性质：

1. **每次变更产生新版本**，记录 `author` / 时间 / `diff`；历史版本**只读**。
2. **回滚不重写历史**：回滚本身也是一次新的提交（新版本号 + 完整 diff），
   指向被恢复的源版本（`restored_from`）。这样审计链永远线性可读。
3. **所有写入路径收敛到 `commit()`**：前端 CRUD、批量应用、OPML 导入、回滚，
   都先构造候选快照，再跑 `schema.validate_registry()`，通过后才落版本。
   因此"保存时拒绝"是结构性的，而不是各调用点自觉。

跨对象规则（`industry_id` 必须存在、`id` 唯一、被标签引用的行业不得删除）
全部由 `validate_registry()` 在**每次提交前**对候选快照强制执行。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from pydantic import Field, field_validator, model_validator

from atlas.contracts import (
    ContractModel,
    IdError,
    InvalidContractStateError,
    NotFoundError,
    VersionError,
)

from .schema import Channel, Industry, validate_registry

__all__ = [
    "ChangeKind",
    "DiffEntry",
    "RegistryMutation",
    "ConfigSnapshot",
    "ConfigVersion",
    "ConfigStore",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChangeKind(str, Enum):
    """变更类型。`BATCH` 用于一次提交里混有多种操作，`IMPORT` 标记 OPML 导入。"""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    BATCH = "batch"
    IMPORT = "import"
    ROLLBACK = "rollback"


ObjectKind = Literal["industry", "channel"]


class DiffEntry(ContractModel):
    """单条对象的变更记录：`before` 为空表示新增，`after` 为空表示删除。"""

    object_kind: ObjectKind
    object_id: str
    change: ChangeKind
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def _check_shape(self) -> "DiffEntry":
        if self.change is ChangeKind.CREATE:
            if self.before is not None or self.after is None:
                raise InvalidContractStateError("create 变更必须只有 after")
        elif self.change is ChangeKind.DELETE:
            if self.after is not None or self.before is None:
                raise InvalidContractStateError("delete 变更必须只有 before")
        elif self.change is ChangeKind.UPDATE:
            if self.before is None or self.after is None:
                raise InvalidContractStateError("update 变更必须同时有 before 与 after")
            if self.before == self.after:
                raise InvalidContractStateError(
                    f"update 变更 {self.object_id!r} 的 before 与 after 相同：无变化不入版本"
                )
        else:  # pragma: no cover - 其余 kind 只出现在 ConfigVersion 上
            raise InvalidContractStateError(
                f"DiffEntry.change 只能是 create/update/delete，收到 {self.change.value!r}"
            )
        return self


class RegistryMutation(ContractModel):
    """一次待提交的对象级变更。

    `payload` 是**完整的新记录字段**（不是补丁）：契约记录冻结且禁止
    `model_copy(update=...)`，因此变更必须显式构造新记录（SPEC 与 `ContractModel` 的约定）。
    """

    kind: Literal["create", "update", "delete"]
    object_kind: ObjectKind
    object_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_shape(self) -> "RegistryMutation":
        if self.kind == "create":
            if "id" not in self.payload:
                raise InvalidContractStateError("create 变更的 payload 必须含 id")
            if self.object_id is not None and self.object_id != self.payload["id"]:
                raise IdError(
                    f"create 变更的 object_id={self.object_id!r} 与 payload.id="
                    f"{self.payload['id']!r} 不一致"
                )
        else:
            if not self.object_id:
                raise InvalidContractStateError(f"{self.kind} 变更必须给出 object_id")
            if self.payload.get("id", self.object_id) != self.object_id:
                raise IdError(
                    f"{self.kind} 变更不得改动 id：payload.id="
                    f"{self.payload.get('id')!r} ≠ object_id={self.object_id!r}"
                )
        return self


class ConfigSnapshot(ContractModel):
    """某个版本号下的完整配置。不可变，且与版本号绑定。"""

    version: int = Field(ge=1)
    industries: Tuple[Industry, ...] = ()
    channels: Tuple[Channel, ...] = ()

    @field_validator("industries", "channels", mode="before")
    @classmethod
    def _coerce(cls, value: Any) -> Any:
        return tuple(value)

    @model_validator(mode="after")
    def _check_sorted_and_valid(self) -> "ConfigSnapshot":
        if tuple(sorted(i.id for i in self.industries)) != tuple(
            i.id for i in self.industries
        ):
            raise InvalidContractStateError("industries 必须按 id 升序存放（保证 digest 稳定）")
        if tuple(sorted(c.id for c in self.channels)) != tuple(
            c.id for c in self.channels
        ):
            raise InvalidContractStateError("channels 必须按 id 升序存放（保证 digest 稳定）")
        validate_registry(self.industries, self.channels)
        return self

    def digest(self) -> str:
        """规范化 JSON 的 sha256：键序无关，内容相同则摘要相同。"""
        canonical = json.dumps(
            {
                "industries": [i.payload() for i in self.industries],
                "channels": [c.payload() for c in self.channels],
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def config_version(self) -> str:
        """采集任务记录用的配置版本串（SPEC §3 的 `config_version`）。"""
        return f"cfg-v{self.version:04d}-{self.digest()[:12]}"

    def industry(self, industry_id: str) -> Industry:
        for ind in self.industries:
            if ind.id == industry_id:
                return ind
        raise NotFoundError(f"行业 {industry_id!r} 不存在（版本 v{self.version}）")

    def channel(self, channel_id: str) -> Channel:
        for ch in self.channels:
            if ch.id == channel_id:
                return ch
        raise NotFoundError(f"渠道 {channel_id!r} 不存在（版本 v{self.version}）")

    def enabled_industries(self) -> Tuple[Industry, ...]:
        """AI 分类的候选标签空间来源（SPEC §2.9 闭环约束：从当前启用配置读取）。"""
        return tuple(i for i in self.industries if i.enabled)

    def enabled_channels(self) -> Tuple[Channel, ...]:
        return tuple(c for c in self.channels if c.enabled)

    def label_space(self) -> Tuple[str, ...]:
        """启用行业的 id 列表 —— 唯一的标签空间定义，代码里没有行业枚举。"""
        return tuple(i.id for i in self.enabled_industries())


class ConfigVersion(ContractModel):
    """一次配置变更的版本记录。"""

    version: int = Field(ge=1)
    author: str = Field(min_length=1)
    created_at: datetime
    kind: ChangeKind
    parent_version: Optional[int] = None
    restored_from: Optional[int] = None
    diff: Tuple[DiffEntry, ...] = ()
    snapshot_digest: str = Field(min_length=1)
    note: Optional[str] = None

    @field_validator("diff", mode="before")
    @classmethod
    def _coerce_diff(cls, value: Any) -> Any:
        return tuple(value)

    @model_validator(mode="after")
    def _check_kind_shape(self) -> "ConfigVersion":
        if self.kind is ChangeKind.ROLLBACK:
            if self.restored_from is None:
                raise VersionError("rollback 版本必须记录 restored_from")
        elif self.restored_from is not None:
            raise VersionError(
                f"只有 rollback 版本可以带 restored_from，收到 kind={self.kind.value}"
            )
        return self

    def changed_object_ids(self) -> Tuple[str, ...]:
        return tuple(sorted({d.object_id for d in self.diff}))


class ConfigStore:
    """配置仓储：唯一写入入口 + 版本链。

    线程/进程安全不在 T-004 范围内（单用户、每天一次的使用频率，SPEC §1.5）；
    持久化落地见 T-101。本类只保证**内存中的语义正确性**与**可审计的版本链**。
    """

    def __init__(
        self,
        *,
        author: str,
        industries: Iterable[Industry] = (),
        channels: Iterable[Channel] = (),
        label_references: Optional[Callable[[], Iterable[str]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not author or not author.strip():
            raise InvalidContractStateError("ConfigStore 必须给出非空的初始作者")
        self._author = author.strip()
        self._label_references = label_references
        self._clock = clock or _utcnow

        industry_tuple = tuple(sorted(industries, key=lambda i: i.id))
        channel_tuple = tuple(sorted(channels, key=lambda c: c.id))
        validate_registry(
            industry_tuple, channel_tuple, referenced_ids=self._referenced_ids()
        )
        genesis = ConfigSnapshot(
            version=1, industries=industry_tuple, channels=channel_tuple
        )
        diff = tuple(
            [
                DiffEntry(
                    object_kind="industry",
                    object_id=i.id,
                    change=ChangeKind.CREATE,
                    after=i.payload(),
                )
                for i in industry_tuple
            ]
            + [
                DiffEntry(
                    object_kind="channel",
                    object_id=c.id,
                    change=ChangeKind.CREATE,
                    after=c.payload(),
                )
                for c in channel_tuple
            ]
        )
        self._versions: list[ConfigVersion] = [
            ConfigVersion(
                version=1,
                author=self._author,
                created_at=self._clock(),
                kind=ChangeKind.CREATE,
                parent_version=None,
                diff=diff,
                snapshot_digest=genesis.digest(),
                note="genesis",
            )
        ]
        self._snapshots: Dict[int, ConfigSnapshot] = {1: genesis}

    # --- 读 -----------------------------------------------------------------

    @property
    def current(self) -> ConfigSnapshot:
        return self._snapshots[self._versions[-1].version]

    @property
    def current_version(self) -> int:
        return self.current.version

    @property
    def versions(self) -> Tuple[ConfigVersion, ...]:
        return tuple(self._versions)

    def version(self, number: int) -> ConfigVersion:
        for record in self._versions:
            if record.version == number:
                return record
        raise NotFoundError(f"配置版本 v{number} 不存在（现有版本：{[v.version for v in self._versions]}）")

    def snapshot(self, number: int) -> ConfigSnapshot:
        self.version(number)  # 不存在则抛 NotFoundError
        return self._snapshots[number]

    def history(self) -> Tuple[ConfigVersion, ...]:
        """按版本号升序的完整审计链。"""
        return tuple(sorted(self._versions, key=lambda v: v.version))

    def label_space(self) -> Tuple[str, ...]:
        """当前启用行业 id —— SPEC §2.5 闭环里"AI 分类的标签空间"。"""
        return self.current.label_space()

    # --- 写：唯一入口 --------------------------------------------------------

    def commit(
        self,
        mutations: Sequence[RegistryMutation],
        *,
        author: Optional[str] = None,
        kind: Optional[ChangeKind] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        """应用一批变更并产生新版本。

        流程固定为：构造候选快照 → `validate_registry()` → 计算 diff → 追加版本。
        任何一步失败都抛领域异常，**不产生半成品版本**（SPEC §3「失败不留半成品」）。
        """
        actor = (author or self._author).strip()
        if not actor:
            raise InvalidContractStateError("提交必须给出非空作者")

        mutation_list = list(mutations)
        if not mutation_list:
            raise InvalidContractStateError("提交为空：没有任何变更")

        base = self.current
        industries: Dict[str, Industry] = {i.id: i for i in base.industries}
        channels: Dict[str, Channel] = {c.id: c for c in base.channels}

        referenced = self._referenced_ids()
        diff: list[DiffEntry] = []

        for mutation in mutation_list:
            if mutation.object_kind == "industry":
                self._apply_industry(mutation, industries, channels, referenced, diff)
            else:
                self._apply_channel(mutation, channels, diff)

        candidate = ConfigSnapshot(
            version=base.version + 1,
            industries=tuple(sorted(industries.values(), key=lambda i: i.id)),
            channels=tuple(sorted(channels.values(), key=lambda c: c.id)),
        )
        validate_registry(
            candidate.industries, candidate.channels, referenced_ids=referenced
        )

        if not diff:
            raise InvalidContractStateError("配置无变化，不产生新版本（SPEC §3：明确的『无变化』）")

        resolved_kind = kind or self._infer_kind(mutation_list)
        record = ConfigVersion(
            version=candidate.version,
            author=actor,
            created_at=self._clock(),
            kind=resolved_kind,
            parent_version=base.version,
            diff=tuple(diff),
            snapshot_digest=candidate.digest(),
            note=note,
        )
        self._versions.append(record)
        self._snapshots[record.version] = candidate
        return record

    def rollback(
        self,
        target_version: int,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        """回滚到任一历史版本。

        回滚**不删除**任何后续版本，而是把目标版本的配置作为一次新的提交追加到链尾。
        被标签引用的行业在回滚后仍须存在，否则 `validate_registry()` 拒绝。
        """
        target_record = self.version(target_version)
        if target_version == self.current_version:
            raise VersionError(
                f"回滚目标 v{target_version} 即当前版本，无需回滚"
            )
        target_snapshot = self._snapshots[target_version]
        base = self.current

        before_ind: Dict[str, Industry] = {i.id: i for i in base.industries}
        after_ind: Dict[str, Industry] = {i.id: i for i in target_snapshot.industries}
        before_ch: Dict[str, Channel] = {c.id: c for c in base.channels}
        after_ch: Dict[str, Channel] = {c.id: c for c in target_snapshot.channels}

        diff = list(_diff_map("industry", before_ind, after_ind))
        diff.extend(_diff_map("channel", before_ch, after_ch))

        candidate = ConfigSnapshot(
            version=base.version + 1,
            industries=target_snapshot.industries,
            channels=target_snapshot.channels,
        )
        validate_registry(
            candidate.industries,
            candidate.channels,
            referenced_ids=self._referenced_ids(),
        )

        if not diff:
            raise InvalidContractStateError(
                f"回滚到 v{target_version} 无内容变化（目标配置与当前一致）"
            )

        actor = (author or self._author).strip()
        if not actor:
            raise InvalidContractStateError("回滚必须给出非空作者")

        record = ConfigVersion(
            version=candidate.version,
            author=actor,
            created_at=self._clock(),
            kind=ChangeKind.ROLLBACK,
            parent_version=base.version,
            restored_from=target_record.version,
            diff=tuple(diff),
            snapshot_digest=candidate.digest(),
            note=note,
        )
        self._versions.append(record)
        self._snapshots[record.version] = candidate
        return record

    # --- 便利方法（同样走 commit，无旁路）-----------------------------------

    def create_industry(self, industry: Industry, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="create", object_kind="industry", payload=industry.payload())],
            **kwargs,
        )

    def update_industry(self, industry: Industry, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="industry",
                    object_id=industry.id,
                    payload=industry.payload(),
                )
            ],
            **kwargs,
        )

    def delete_industry(self, industry_id: str, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="delete", object_kind="industry", object_id=industry_id)],
            **kwargs,
        )

    def create_channel(self, channel: Channel, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="create", object_kind="channel", payload=channel.payload())],
            **kwargs,
        )

    def update_channel(self, channel: Channel, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [
                RegistryMutation(
                    kind="update",
                    object_kind="channel",
                    object_id=channel.id,
                    payload=channel.payload(),
                )
            ],
            **kwargs,
        )

    def delete_channel(self, channel_id: str, **kwargs: Any) -> ConfigVersion:
        return self.commit(
            [RegistryMutation(kind="delete", object_kind="channel", object_id=channel_id)],
            **kwargs,
        )

    # --- 内部 ---------------------------------------------------------------

    def _referenced_ids(self) -> Tuple[str, ...]:
        if self._label_references is None:
            return ()
        return tuple(self._label_references())

    @staticmethod
    def _infer_kind(mutations: Sequence[RegistryMutation]) -> ChangeKind:
        kinds = {m.kind for m in mutations}
        if len(kinds) == 1:
            return ChangeKind(next(iter(kinds)))
        return ChangeKind.BATCH

    @staticmethod
    def _apply_industry(
        mutation: RegistryMutation,
        industries: Dict[str, Industry],
        channels: Dict[str, Channel],
        referenced: Tuple[str, ...],
        diff: list[DiffEntry],
    ) -> None:
        if mutation.kind == "create":
            record = Industry(**mutation.payload)
            if record.id in industries:
                raise InvalidContractStateError(f"行业 id 已存在：{record.id!r}")
            if record.id in channels:
                raise InvalidContractStateError(
                    f"id={record.id!r} 已被渠道占用，不能同时作为行业"
                )
            industries[record.id] = record
            diff.append(
                DiffEntry(
                    object_kind="industry",
                    object_id=record.id,
                    change=ChangeKind.CREATE,
                    after=record.payload(),
                )
            )
            return

        assert mutation.object_id is not None  # RegistryMutation 已保证
        object_id = mutation.object_id
        existing = industries.get(object_id)
        if existing is None:
            raise NotFoundError(f"行业 {object_id!r} 不存在，无法 {mutation.kind}")

        if mutation.kind == "update":
            record = Industry(**mutation.payload)
            if record.id != object_id:
                extra = (
                    "（该 id 已被标签引用，SPEC §2.9 规则 1 禁止改 id）"
                    if object_id in referenced
                    else ""
                )
                raise IdError(
                    f"行业 id 不可修改：{object_id!r} → {record.id!r}；"
                    f"如需换 id 请显式 delete + create{extra}"
                )
            if record.payload() == existing.payload():
                raise InvalidContractStateError(f"行业 {object_id!r} 无字段变化")
            industries[object_id] = record
            diff.append(
                DiffEntry(
                    object_kind="industry",
                    object_id=object_id,
                    change=ChangeKind.UPDATE,
                    before=existing.payload(),
                    after=record.payload(),
                )
            )
            return

        # delete
        if object_id in referenced:
            raise InvalidContractStateError(
                f"行业 {object_id!r} 已被标签引用，不得删除（SPEC §2.9 校验规则 1）"
            )
        children = sorted(i.id for i in industries.values() if i.parent_id == object_id)
        if children:
            raise InvalidContractStateError(
                f"行业 {object_id!r} 仍有子行业 {children}，不得删除"
            )
        attached = sorted(c.id for c in channels.values() if c.industry_id == object_id)
        if attached:
            raise InvalidContractStateError(
                f"行业 {object_id!r} 仍被渠道 {attached} 引用，不得删除"
            )
        del industries[object_id]
        diff.append(
            DiffEntry(
                object_kind="industry",
                object_id=object_id,
                change=ChangeKind.DELETE,
                before=existing.payload(),
            )
        )

    @staticmethod
    def _apply_channel(
        mutation: RegistryMutation,
        channels: Dict[str, Channel],
        diff: list[DiffEntry],
    ) -> None:
        if mutation.kind == "create":
            record = Channel(**mutation.payload)
            if record.id in channels:
                raise InvalidContractStateError(f"渠道 id 已存在：{record.id!r}")
            channels[record.id] = record
            diff.append(
                DiffEntry(
                    object_kind="channel",
                    object_id=record.id,
                    change=ChangeKind.CREATE,
                    after=record.payload(),
                )
            )
            return

        assert mutation.object_id is not None
        object_id = mutation.object_id
        existing = channels.get(object_id)
        if existing is None:
            raise NotFoundError(f"渠道 {object_id!r} 不存在，无法 {mutation.kind}")

        if mutation.kind == "update":
            record = Channel(**mutation.payload)
            if record.id != object_id:
                raise IdError(
                    f"渠道 id 不可修改：{object_id!r} → {record.id!r}；"
                    "如需换 id 请显式 delete + create"
                )
            if record.payload() == existing.payload():
                raise InvalidContractStateError(f"渠道 {object_id!r} 无字段变化")
            channels[object_id] = record
            diff.append(
                DiffEntry(
                    object_kind="channel",
                    object_id=object_id,
                    change=ChangeKind.UPDATE,
                    before=existing.payload(),
                    after=record.payload(),
                )
            )
            return

        del channels[object_id]
        diff.append(
            DiffEntry(
                object_kind="channel",
                object_id=object_id,
                change=ChangeKind.DELETE,
                before=existing.payload(),
            )
        )


def _diff_map(
    object_kind: ObjectKind,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for object_id in sorted(set(before) | set(after)):
        old = before.get(object_id)
        new = after.get(object_id)
        if old is None and new is not None:
            entries.append(
                DiffEntry(
                    object_kind=object_kind,
                    object_id=object_id,
                    change=ChangeKind.CREATE,
                    after=new.payload(),
                )
            )
        elif old is not None and new is None:
            entries.append(
                DiffEntry(
                    object_kind=object_kind,
                    object_id=object_id,
                    change=ChangeKind.DELETE,
                    before=old.payload(),
                )
            )
        elif old is not None and new is not None and old.payload() != new.payload():
            entries.append(
                DiffEntry(
                    object_kind=object_kind,
                    object_id=object_id,
                    change=ChangeKind.UPDATE,
                    before=old.payload(),
                    after=new.payload(),
                )
            )
    return entries
