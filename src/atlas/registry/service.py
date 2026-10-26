"""注册表服务层：CRUD + 列表查询 + 变更审计（T-101）。

这一层是 T-106（feed）与配置前端的**唯一入口**，职责是：

- 把 `SqliteConfigStore` 的"提交语义"翻译成对象级的 CRUD（行业 / 渠道）
- 提供列表查询与按启用状态过滤（T-106 feed 与配置前端都要用）
- 提供审计查询：**谁在何时改了什么**（直接来自版本链的 diff，不是另存一份日志）

三条纪律：

1. **没有任何写入旁路**：所有变更都构造成 `RegistryMutation` 后交给
   `ConfigStore.commit()`，因此单对象与跨对象校验（`schema.py`）一律照跑。
2. **不静默返回**：查不到就抛 `atlas.contracts.NotFoundError`，
   非法状态由领域异常抛错，绝不返回 `None` 或空对象冒充成功。
3. **不硬编码行业**：本模块里没有任何行业名/行业枚举 —— 行业全部来自配置数据
   （SPEC §2.9 闭环约束）。预置渠道目录见 `catalog.py`。
"""

from __future__ import annotations

from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from pydantic import Field

from atlas.contracts import ContractModel, InvalidContractStateError, NotFoundError

from .persistence import SqliteConfigStore
from .schema import Channel, FetchType, Industry
from .versioning import (
    ChangeKind,
    ConfigSnapshot,
    ConfigVersion,
    DiffEntry,
    RegistryMutation,
)

if TYPE_CHECKING:  # 避免 catalog 与 service 相互导入
    from .catalog import CatalogSelection, PrebuiltCatalog

__all__ = [
    "AuditEntry",
    "ChannelView",
    "IndustryView",
    "RegistryService",
    "index_by_id",
]


class AuditEntry(ContractModel):
    """一条审计记录：某次提交里对**某个对象**做过什么。

    由 `ConfigVersion.diff` 投影而来，所以审计链与版本链是**同一份事实**，
    不存在"审计日志和实际变更对不上"的可能。
    """

    version: int = Field(ge=1)
    author: str = Field(min_length=1)
    created_at: datetime
    kind: ChangeKind
    object_kind: str
    object_id: str
    change: ChangeKind
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    note: Optional[str] = None
    restored_from: Optional[int] = None

    def summary(self) -> str:
        """一行人类可读描述（前端与日志都用它，避免各自拼字符串）。"""
        label = "行业" if self.object_kind == "industry" else "渠道"
        action = {
            ChangeKind.CREATE: "新增",
            ChangeKind.UPDATE: "修改",
            ChangeKind.DELETE: "删除",
        }.get(self.change, self.change.value)
        tail = ""
        if self.change is ChangeKind.UPDATE and self.before and self.after:
            fields = sorted(
                key
                for key in set(self.before) | set(self.after)
                if self.before.get(key) != self.after.get(key)
            )
            tail = f"（字段：{', '.join(fields)}）"
        return f"v{self.version} {self.author} {action}{label} {self.object_id}{tail}"


class IndustryView(ContractModel):
    """行业记录 + 它在版本链里的生命周期。"""

    record: Industry
    created_version: int = Field(ge=1)
    created_at: datetime
    updated_version: int = Field(ge=1)
    updated_at: datetime

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def enabled(self) -> bool:
        return self.record.enabled


class ChannelView(ContractModel):
    """渠道记录 + 它在版本链里的生命周期。"""

    record: Channel
    created_version: int = Field(ge=1)
    created_at: datetime
    updated_version: int = Field(ge=1)
    updated_at: datetime

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def industry_id(self) -> str:
        return self.record.industry_id

    @property
    def enabled(self) -> bool:
        return self.record.enabled


def index_by_id(records: Iterable[Any]) -> Dict[str, Any]:
    """按 `.id` 建索引；重复 id 响亮失败（不静默后者覆盖前者）。"""
    out: Dict[str, Any] = {}
    for record in records:
        if record.id in out:
            raise InvalidContractStateError(f"id 重复：{record.id!r}")
        out[record.id] = record
    return out


class RegistryService:
    """行业 / 渠道配置的服务层。

    持有 `SqliteConfigStore`；每次写入后刷新本地审计索引，读操作不做额外 SQL。
    """

    def __init__(self, store: SqliteConfigStore) -> None:
        self._store = store
        self._refresh_index()

    # ------------------------------------------------------------------
    # 依赖暴露（T-102 / T-106 需要）
    # ------------------------------------------------------------------
    @property
    def store(self) -> SqliteConfigStore:
        return self._store

    @property
    def config_version(self) -> str:
        """SPEC §3：采集任务必须记录它所用配置的 `config_version`。"""
        return self._store.current.config_version

    @property
    def current_version(self) -> int:
        return self._store.current_version

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self._store.current

    def label_space(self) -> Tuple[str, ...]:
        """AI 分类的候选标签集合 —— 唯一来源是当前启用行业配置（SPEC §2.5）。"""
        return self._store.label_space()

    # ------------------------------------------------------------------
    # 查：单个对象
    # ------------------------------------------------------------------
    def require_industry(self, industry_id: str) -> Industry:
        for record in self._store.current.industries:
            if record.id == industry_id:
                return record
        raise NotFoundError(
            f"行业 {industry_id!r} 不存在（当前 v{self.current_version}；"
            f"现有行业：{[i.id for i in self._store.current.industries]}）"
        )

    def require_channel(self, channel_id: str) -> Channel:
        for record in self._store.current.channels:
            if record.id == channel_id:
                return record
        raise NotFoundError(
            f"渠道 {channel_id!r} 不存在（当前 v{self.current_version}；"
            f"现有渠道：{[c.id for c in self._store.current.channels]}）"
        )

    def industry_view(self, industry_id: str) -> IndustryView:
        return self._views_industries()[industry_id]

    def channel_view(self, channel_id: str) -> ChannelView:
        return self._views_channels()[channel_id]

    # ------------------------------------------------------------------
    # 查：列表与过滤
    # ------------------------------------------------------------------
    def list_industries(
        self,
        *,
        enabled: Optional[bool] = None,
        parent_id: Optional[str] = None,
        include_children: bool = True,
    ) -> Tuple[Industry, ...]:
        """按启用状态 / 父行业列行业。

        `parent_id` 给出时默认连同**所有后代**一起返回（层级是配置出来的，
        不是枚举出来的）；`include_children=False` 则只返回直接子级。
        """
        records = list(self._store.current.industries)
        if parent_id is not None:
            self.require_industry(parent_id)
            if include_children:
                wanted = self._descendants(parent_id)
            else:
                wanted = {i.id for i in records if i.parent_id == parent_id}
            records = [i for i in records if i.id in wanted]
        if enabled is not None:
            records = [i for i in records if i.enabled is enabled]
        return tuple(records)

    def list_channels(
        self,
        *,
        industry_id: Optional[str] = None,
        enabled: Optional[bool] = None,
        tag: Optional[str] = None,
        fetch_type: Optional[FetchType] = None,
    ) -> Tuple[Channel, ...]:
        """按行业 / 启用状态 / 标签 / 抓取方式过滤渠道。

        所有过滤条件同时给出时是 **AND** 语义；`industry_id` 不存在时抛
        `NotFoundError`（而不是返回空列表假装"这个行业没有渠道"）。
        """
        if industry_id is not None:
            self.require_industry(industry_id)
        records = list(self._store.current.channels)
        if industry_id is not None:
            records = [c for c in records if c.industry_id == industry_id]
        if enabled is not None:
            records = [c for c in records if c.enabled is enabled]
        if tag is not None:
            records = [c for c in records if tag in c.tags]
        if fetch_type is not None:
            wanted_type = (
                fetch_type if isinstance(fetch_type, FetchType) else FetchType(fetch_type)
            )
            records = [c for c in records if c.type is wanted_type]
        return tuple(records)

    def list_industry_views(self, **kwargs: Any) -> Tuple[IndustryView, ...]:
        views = self._views_industries()
        return tuple(views[i.id] for i in self.list_industries(**kwargs))

    def list_channel_views(self, **kwargs: Any) -> Tuple[ChannelView, ...]:
        views = self._views_channels()
        return tuple(views[c.id] for c in self.list_channels(**kwargs))

    def fetchable_channels(self) -> Tuple[Channel, ...]:
        """T-102 的输入：当前启用、且其行业也启用的渠道（停用行业不再采集）。"""
        enabled_industries = {i.id for i in self._store.current.enabled_industries()}
        return tuple(
            c
            for c in self._store.current.channels
            if c.enabled and c.industry_id in enabled_industries
        )

    def channels_per_industry(self) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        """每个行业下的渠道 id（T-111 目录保鲜与前端分组都要用）。

        用元组而不是 dict，保证是契约记录、顺序稳定、可比较。
        """
        grouped: Dict[str, List[str]] = {i.id: [] for i in self._store.current.industries}
        for channel in self._store.current.channels:
            grouped.setdefault(channel.industry_id, []).append(channel.id)
        return tuple(
            (industry_id, tuple(sorted(ids))) for industry_id, ids in sorted(grouped.items())
        )

    # ------------------------------------------------------------------
    # 写：行业
    # ------------------------------------------------------------------
    def create_industry(
        self,
        industry: Industry,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        record = self._store.create_industry(industry, author=author, note=note)
        self._refresh_index()
        return record

    def update_industry(
        self,
        industry_id: str,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
        **fields: Any,
    ) -> ConfigVersion:
        """按字段更新行业。

        `id` 不参与更新：契约记录不可变且 `model_copy(update=...)` 被封死，
        因此这里**显式构造新记录**。`id` 一旦被标签引用就不允许改（SPEC §2.9 规则 1），
        需要换 id 必须显式 delete + create。
        """
        existing = self.require_industry(industry_id)
        payload = self._merged_payload(
            existing.payload(), fields, what=f"行业 {industry_id!r}"
        )
        record = self._store.update_industry(Industry(**payload), author=author, note=note)
        self._refresh_index()
        return record

    def delete_industry(
        self,
        industry_id: str,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        self.require_industry(industry_id)
        record = self._store.delete_industry(industry_id, author=author, note=note)
        self._refresh_index()
        return record

    # ------------------------------------------------------------------
    # 写：渠道
    # ------------------------------------------------------------------
    def create_channel(
        self,
        channel: Channel,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        record = self._store.create_channel(channel, author=author, note=note)
        self._refresh_index()
        return record

    def update_channel(
        self,
        channel_id: str,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
        **fields: Any,
    ) -> ConfigVersion:
        """按字段更新渠道（同样显式构造新记录，不走 `model_copy(update=...)`）。"""
        existing = self.require_channel(channel_id)
        payload = self._merged_payload(
            existing.payload(), fields, what=f"渠道 {channel_id!r}"
        )
        record = self._store.update_channel(Channel(**payload), author=author, note=note)
        self._refresh_index()
        return record

    def set_channel_enabled(
        self,
        channel_id: str,
        enabled: bool,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        """启停渠道（配置前端的开关）；走同一条 update 路径，无旁路。"""
        return self.update_channel(
            channel_id, enabled=enabled, author=author, note=note
        )

    def set_industry_enabled(
        self,
        industry_id: str,
        enabled: bool,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        """启停行业。停用后不再采集（`fetchable_channels()` 会排除其渠道），
        历史数据保留；被标签引用的行业仍然存在，因此停用是安全的。"""
        return self.update_industry(
            industry_id, enabled=enabled, author=author, note=note
        )

    def delete_channel(
        self,
        channel_id: str,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        self.require_channel(channel_id)
        record = self._store.delete_channel(channel_id, author=author, note=note)
        self._refresh_index()
        return record

    # ------------------------------------------------------------------
    # 写：批量（一次提交一个版本）
    # ------------------------------------------------------------------
    def commit_raw(
        self,
        mutations: Sequence[RegistryMutation],
        *,
        author: Optional[str] = None,
        kind: Optional[ChangeKind] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        """批量提交入口（供 OPML 导入与选配复用）。

        仍然只经 `ConfigStore.commit()`：校验与版本化都在那里，本方法不绕过任何一步。
        `kind` 缺省为 `BATCH` —— 走这个入口的就是"一次提交里可能混合多种操作"，
        不应被推断成单一对象种类的 CREATE/UPDATE/DELETE。
        """
        record = self._store.commit(
            mutations, author=author, kind=kind or ChangeKind.BATCH, note=note
        )
        self._refresh_index()
        return record

    def rollback(
        self,
        version: int,
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ConfigVersion:
        record = self._store.rollback(version, author=author, note=note)
        self._refresh_index()
        return record

    # ------------------------------------------------------------------
    # 选配：从预置目录勾选 + 覆盖默认值（SPEC §2.9）
    # ------------------------------------------------------------------
    def apply_catalog(
        self,
        catalog: "PrebuiltCatalog",
        selections: Sequence["CatalogSelection"],
        *,
        author: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Optional[ConfigVersion]:
        """把一组勾选落到配置里（一次提交，一个版本）。

        规则（不静默覆盖用户已有的配置）：

        - 勾选依赖的行业不存在 → 自动按目录模板建（行业也来自目录数据）
        - 渠道不存在 → 按"模板默认值 + 覆盖"创建
        - 渠道已存在且内容一致 → 幂等跳过
        - 渠道已存在且内容**不同** → 显式报错，请调用方用 `update_channel()` 明确覆盖
          （目录不是"每次勾选都重置用户改动"的开关）
        - 全部都已存在且一致 → 返回 `None`，表示明确的"无变化"

        所有记录都由 `catalog.instantiate_*` 构造（末尾经 `Channel()` / `Industry()`
        校验），再交给 `commit()` 做跨对象校验 —— 与手工 CRUD 完全同一条路径。
        """
        from .catalog import CatalogSelection, PrebuiltCatalog  # 局部导入避免环

        if not selections:
            raise InvalidContractStateError("选配为空：没有任何勾选")

        mutations: List[RegistryMutation] = []
        unchanged: List[str] = []
        required = catalog.required_industries(selections)
        wanted_industries = {record.id for record in required}
        existing_industries = {i.id: i for i in self._store.current.industries}
        for industry in required:
            if industry.id in existing_industries:
                continue
            mutations.append(
                RegistryMutation(
                    kind="create", object_kind="industry", payload=industry.payload()
                )
            )

        existing_channels = {c.id: c for c in self._store.current.channels}
        for selection in selections:
            channel = catalog.instantiate(selection)
            if channel.industry_id not in wanted_industries and (
                channel.industry_id not in existing_industries
            ):
                raise InvalidContractStateError(
                    f"勾选 {selection.template_id!r} 指向的行业 {channel.industry_id!r} "
                    "既不在目录里也不在配置里"
                )
            existing = existing_channels.get(channel.id)
            if existing is None:
                mutations.append(
                    RegistryMutation(
                        kind="create", object_kind="channel", payload=channel.payload()
                    )
                )
                continue
            if existing.payload() == channel.payload():
                unchanged.append(channel.id)
                continue
            raise InvalidContractStateError(
                f"渠道 {channel.id!r} 已存在且与目录模板不一致；"
                "勾选不会覆盖你已有的配置，请显式调用 update_channel()"
            )

        if not mutations:
            return None

        summary = [note] if note else []
        summary.append(f"从目录 {catalog.name!r} 选配 {len(selections)} 条")
        if unchanged:
            summary.append(f"幂等跳过 {len(unchanged)} 条已存在且相同的渠道")
        return self.commit_raw(
            mutations,
            author=author,
            kind=ChangeKind.BATCH,
            note="；".join(summary),
        )

    # ------------------------------------------------------------------
    # 审计：谁在何时改了什么
    # ------------------------------------------------------------------
    def audit_log(self) -> Tuple[AuditEntry, ...]:
        """完整审计链，按 (版本号, 对象) 升序。"""
        return self._audit

    def audit_for(self, object_id: str) -> Tuple[AuditEntry, ...]:
        """某个对象（行业或渠道）的全部变更历史。

        对象既不在当前配置、也没有任何历史变更时抛 `NotFoundError`，
        不返回空元组冒充"没有变更"。
        """
        entries = tuple(e for e in self._audit if e.object_id == object_id)
        if entries:
            return entries
        known = {i.id for i in self._store.current.industries} | {
            c.id for c in self._store.current.channels
        }
        if object_id in known:
            # 理论上不可达（在当前配置里就一定有 CREATE 记录）；响亮失败而不是沉默
            raise InvalidContractStateError(
                f"{object_id!r} 当前存在，但审计链里查不到它的任何记录："
                "版本链与当前配置不一致"
            )
        raise NotFoundError(
            f"{object_id!r} 既不在当前配置 v{self.current_version}，"
            "也没有任何历史变更记录"
        )

    def audit_by(self, author: str, *, since: Optional[datetime] = None) -> Tuple[AuditEntry, ...]:
        """某位作者做过的变更（可按时间下界过滤）。"""
        wanted = author.strip()
        if not wanted:
            raise InvalidContractStateError("审计查询必须给出非空作者")
        return tuple(
            e
            for e in self._audit
            if e.author == wanted and (since is None or e.created_at >= since)
        )

    def audit_between(self, start_version: int, end_version: int) -> Tuple[AuditEntry, ...]:
        """某个版本区间的变更（含两端）。"""
        if start_version > end_version:
            raise InvalidContractStateError(
                f"版本区间非法：start={start_version} > end={end_version}"
            )
        return tuple(
            e for e in self._audit if start_version <= e.version <= end_version
        )

    def diff(self, from_version: int, to_version: int) -> Tuple[DiffEntry, ...]:
        """两个版本的差异（重放 diff 汇总，不改动任何历史）。"""
        source = self._store.snapshot(from_version)
        target = self._store.snapshot(to_version)
        before_ind = index_by_id(source.industries)
        after_ind = index_by_id(target.industries)
        before_ch = index_by_id(source.channels)
        after_ch = index_by_id(target.channels)
        entries: List[DiffEntry] = []
        entries.extend(_diff_map("industry", before_ind, after_ind))
        entries.extend(_diff_map("channel", before_ch, after_ch))
        return tuple(entries)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _merged_payload(
        current: Dict[str, Any], changes: Dict[str, Any], *, what: str
    ) -> Dict[str, Any]:
        """把"改哪些字段"合并成**完整的新记录字段**。

        显式构造新记录（不是补丁）：契约模型冻结，且 `model_copy(update=...)` 被封死。
        未知字段不静默忽略 —— 交给模型层 `extra="forbid"` 响亮拒绝。
        """
        forbidden = {"id", "object_id"}
        touched = forbidden & set(changes)
        if touched:
            raise InvalidContractStateError(
                f"{what} 不允许通过更新改动 {sorted(touched)}；"
                "契约记录不可变，需要换 id 请显式 delete + create"
            )
        unsupported = set(changes) - set(current) - {"author", "note"}
        if unsupported:
            raise InvalidContractStateError(
                f"{what} 不认识的字段 {sorted(unsupported)}；"
                f"可更新字段：{sorted(set(current))}"
            )
        merged = dict(current)
        for key, value in changes.items():
            if key in ("author", "note"):
                continue
            merged[key] = _normalize_change(key, value)
        return merged

    def _refresh_index(self) -> None:
        """重算审计链与生命周期视图（写入后调用，读操作零额外 SQL）。"""
        history = self._store.history()
        audit: List[AuditEntry] = []
        for version in history:
            for entry in version.diff:
                audit.append(
                    AuditEntry(
                        version=version.version,
                        author=version.author,
                        created_at=version.created_at,
                        kind=version.kind,
                        object_kind=entry.object_kind,
                        object_id=entry.object_id,
                        change=entry.change,
                        before=entry.before,
                        after=entry.after,
                        note=version.note,
                        restored_from=version.restored_from,
                    )
                )
        self._audit = tuple(audit)

        industry_life = _lifecycle("industry", history)
        channel_life = _lifecycle("channel", history)
        self._industry_views = {
            record.id: IndustryView(
                record=record,
                created_version=industry_life[record.id][0][0],
                created_at=industry_life[record.id][0][1],
                updated_version=industry_life[record.id][1][0],
                updated_at=industry_life[record.id][1][1],
            )
            for record in self._store.current.industries
        }
        self._channel_views = {
            record.id: ChannelView(
                record=record,
                created_version=channel_life[record.id][0][0],
                created_at=channel_life[record.id][0][1],
                updated_version=channel_life[record.id][1][0],
                updated_at=channel_life[record.id][1][1],
            )
            for record in self._store.current.channels
        }

    def _views_industries(self) -> Dict[str, IndustryView]:
        return self._industry_views

    def _views_channels(self) -> Dict[str, ChannelView]:
        return self._channel_views

    def _descendants(self, root_id: str) -> set[str]:
        """层级后代（含自身）。层级由配置数据给出，代码里没有行业清单。"""
        records = self._store.current.industries
        children: Dict[str, List[str]] = {}
        for record in records:
            if record.parent_id is not None:
                children.setdefault(record.parent_id, []).append(record.id)
        found: set[str] = set()
        stack = [root_id]
        while stack:
            cursor = stack.pop()
            if cursor in found:
                continue
            found.add(cursor)
            stack.extend(children.get(cursor, ()))
        return found


def _normalize_change(key: str, value: Any) -> Any:
    """把前端友好的输入归一化成模型能接受的值（不做校验，校验仍归 schema）。"""
    if key in ("tags", "keywords") and isinstance(value, list):
        return tuple(value)
    return value


def _lifecycle(
    object_kind: str, history: Sequence[ConfigVersion]
) -> Dict[str, Tuple[Tuple[int, datetime], Tuple[int, datetime]]]:
    """从版本链推出每个对象的 (创建版本, 创建时间) 与 (最后更新版本, 最后更新时间)。"""
    out: Dict[str, Tuple[Tuple[int, datetime], Tuple[int, datetime]]] = {}
    for version in history:
        for entry in version.diff:
            if entry.object_kind != object_kind:
                continue
            if entry.change is ChangeKind.CREATE:
                out[entry.object_id] = (
                    (version.version, version.created_at),
                    (version.version, version.created_at),
                )
            elif entry.object_id in out:
                created, _ = out[entry.object_id]
                out[entry.object_id] = (created, (version.version, version.created_at))
            else:
                # 中途出现的对象（理论上不该有）：以本次变更同时作为创建与更新
                out[entry.object_id] = (
                    (version.version, version.created_at),
                    (version.version, version.created_at),
                )
    return out


def _diff_map(
    object_kind: str,
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> List[DiffEntry]:
    entries: List[DiffEntry] = []
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
