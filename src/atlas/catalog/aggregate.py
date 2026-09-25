"""T-111 目录聚合：把外部现成清单变成注册表里的渠道（SPEC §2.7 / §2.8 / §2.9）。

SPEC §2.7 把"目录层"定为**做"种子 + 聚合管道"，不做全集**，理由写在 §2.8：
*目录没有"完成"状态，只有持续保鲜*。本模块只提供**聚合管道**，不内置任何渠道清单。

数据流
------

```
外部 OPML（文本 或 URL）
   │  atlas.registry.opml.parse_opml   ← 唯一的 OPML 解析器，本模块不另写一个
   ▼
OpmlDocument（已识别的 channels/industries + 需要补默认值的外部 legacy outline + skipped）
   │  归一化：外部条目按调用方**显式给出**的默认值补齐，再走 registry 自己那条构造路径
   ▼
候选渠道 ChannelCandidate（每条都已经过 atlas.registry.schema 校验）
   │  清单内去重：id / 归一化 endpoint **双键**
   ▼
AggregationResult（候选 + 逐条被拒绝的候选 + 冲突报告 + 解析期跳过项）
   │  plan_merge：与注册表现有记录对账（同样双键）
   ▼
MergePlan（新增 + 幂等已存在 + 冲突）
   │  apply_merge：只经 atlas.registry.service 的批量提交入口
   ▼
注册表新版本
```

四条硬约束（对应任务书）
------------------------

1. **不自己写 OPML 解析**：解析一律 `atlas.registry.opml.parse_opml`。
   外部 outline 的字段映射（`type="rss"` → `FetchType.RSS`、`xmlUrl` → endpoint、
   由 endpoint 派生 id、`interval_seconds` 等）**也不在本模块重写**：外部条目被
   回填成一份单条目的 Atlas OPML 再交给 `parse_opml`，走的完全是注册表自己
   那条构造 + 校验路径（`Channel(**payload)`，SPEC §2.9「导入必须走同一套校验，
   不得有旁路」）。
2. **不静默丢候选**：归一化或校验失败的条目进 `rejected`，逐条带 index + 标题 +
   endpoint + 原因；解析期被跳过的 outline 原样进 `skipped`。两者都在结果对象里，
   调用方拿得到，不是日志里的一句话。
3. **不静默覆盖**：清单内与注册表内一律**双键**去重（规范化 endpoint、id），
   冲突时**保留已有记录**并把冲突写进 `conflicts` / `conflicts`（MergePlan），
   绝不改写既有渠道。要改必须显式调 `RegistryService.update_channel()`。
4. **抓取清单本身也合规**：`fetch_listing()` 复用 `atlas.collect` 的 robots 检查与
   同域限速，robots 不允许、或拿不到 robots 规则时**拒绝抓取**（保守，见
   `atlas.collect.robots`）；限速等待超预算抛 `ThrottleTimeout`，不抢跑。

本模块只依赖可依赖的既有包：`atlas.collect.*`、`atlas.registry.{opml,schema,service,
versioning}`、`atlas.contracts`。零新增依赖（stdlib + 已声明依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
)
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from atlas.collect.fetch import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    Fetcher,
    FetchFailure,
    FetchRequest,
    fetch_once,
)
from atlas.collect.robots import RobotsCache, RobotsDecision, RobotsOutcome
from atlas.collect.throttle import DomainThrottle, domain_of
from atlas.contracts import ContractError, InvalidContractStateError
from atlas.registry.opml import (
    ATLAS_ATTR_PREFIX,
    OpmlOutline,
    parse_opml,
)
from atlas.registry.schema import (
    Channel,
    Industry,
    validate_endpoint,
    validate_registry,
)
from atlas.registry.service import RegistryService
from atlas.registry.versioning import ChangeKind, ConfigVersion, RegistryMutation

__all__ = [
    "DEFAULT_LISTING_USER_AGENT",
    "CandidateOrigin",
    "DuplicateKind",
    "MergeConflictKind",
    "CatalogError",
    "AggregationError",
    "ListingRobotsDenied",
    "ListingFetchFailed",
    "ListingDocument",
    "ChannelCandidate",
    "RejectedCandidate",
    "CandidateConflict",
    "AggregationResult",
    "MergeConflict",
    "MergePlan",
    "AggregationOutcome",
    "normalize_endpoint",
    "fetch_listing",
    "aggregate_listing",
    "plan_merge",
    "apply_merge",
    "aggregate_registry",
    "aggregate_registry_from_url",
]

#: 抓取外部清单用的 UA：本系统自有 UA（SPEC §7.1：不伪装浏览器）。
DEFAULT_LISTING_USER_AGENT = DEFAULT_USER_AGENT

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _attr(name: str) -> str:
    """Atlas OPML 属性名（前缀来自 `atlas.registry.opml`，本模块不另立约定）。"""
    return f"{ATLAS_ATTR_PREFIX}{name}"


# --- 错误 ---------------------------------------------------------------------


class CatalogError(Exception):
    """T-111 目录聚合的错误基类。

    刻意**不继承 `ValueError`**：与 `atlas.contracts.errors` 同一考虑 —— 契约违例
    必须原样穿透，不被 pydantic 包装成通用的 `ValidationError`。
    """


class AggregationError(CatalogError):
    """清单整体不可用（解析失败等文档级问题）。

    注意与"逐条被拒绝的候选"的区别：那类问题在 `AggregationResult.rejected` 里，
    本异常只用于**整份清单都读不下去**的情形。
    """


class ListingRobotsDenied(CatalogError):
    """robots 检查未通过，因此**没有**发起抓取（合规底线）。"""

    def __init__(self, url: str, decision: RobotsDecision) -> None:
        self.url = url
        self.decision = decision
        super().__init__(
            f"robots 检查拒绝抓取清单 {url!r}：{decision.outcome.value}：{decision.reason}"
        )


class ListingFetchFailed(CatalogError):
    """清单抓取失败（结构化失败原样带出，不揉成一句"失败"）。"""

    def __init__(self, url: str, failure: FetchFailure) -> None:
        self.url = url
        self.failure = failure
        super().__init__(
            f"抓取清单 {url!r} 失败（{failure.kind.value}）：{failure.message}"
        )


# --- endpoint 规范化 ----------------------------------------------------------


def normalize_endpoint(url: str) -> str:
    """把 endpoint 归一化成去重键（scheme/host 小写、去默认端口、去 fragment）。

    先经 `registry.schema.validate_endpoint()` —— 去重键的**准入**与配置校验同源，
    不在这里另立一套"什么算合法 URL"。

    归一化只做不会改变资源身份的部分：保留 path 与 query（`?feed=rss` 是不同资源），
    只把空 path 视作 `/`（RFC 语义下二者同一资源）、丢弃 fragment。
    """
    validate_endpoint(url)
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


# --- 合规抓取外部清单 ---------------------------------------------------------


@dataclass(frozen=True)
class ListingDocument:
    """一次合规抓取到的外部清单文本 + 抓取元数据。"""

    url: str
    text: str
    status_code: int
    robots_outcome: RobotsOutcome
    robots_url: str
    waited_seconds: float
    fetched_at: datetime
    #: 解码时被替换掉的字符数。0 表示严格 UTF-8 解码成功。
    #: 非 0 不是"失败"（OPML 结构是 ASCII，受损的只会是标题文本），但必须可观测，
    #: 不允许悄悄替换掉再当没事发生。
    replacement_chars: int = 0


def _listing_min_interval(throttle: DomainThrottle) -> float:
    """抓清单时该域的最小请求间隔 = 限速层的全局下限（默认 1 req/sec per domain）。

    刻意**不**用 `channel.interval_seconds`：那是轮询周期（下限 60s，SPEC §2.12），
    把它当请求间隔会让一次聚合卡一分钟。同域若同时有渠道采集，`DomainThrottle`
    取最严的那个间隔，因此这里取全局下限不会放宽任何已有承诺。
    """
    return float(throttle.global_min_interval)


def _decode_listing(content: bytes) -> Tuple[str, int]:
    """UTF-8 解码（容忍 BOM）；受损字节用替换字符承接并**计数上报**。"""
    try:
        return content.decode("utf-8-sig"), 0
    except UnicodeDecodeError:
        text = content.decode("utf-8-sig", errors="replace")
        return text, text.count("\ufffd")


def fetch_listing(
    url: str,
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    user_agent: str = DEFAULT_LISTING_USER_AGENT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    wall_clock: Callable[[], datetime] = _utcnow,
) -> ListingDocument:
    """合规地抓取一份外部 OPML 清单（robots → 限速 → fetch）。

    `robots` 与 `throttle` 都是**必填**参数：本函数不提供"默认放行"的实现，
    避免任何调用路径绕过合规层（与 `atlas.collect` 的执行器同一纪律）。
    本函数不重试 —— 重试策略若需要，由调用方用 `atlas.collect.RetryPolicy` 自行组合。
    """
    if not user_agent or not user_agent.strip():
        raise InvalidContractStateError(
            "抓取外部清单必须声明非空 user_agent（合规底线，SPEC §7.1）"
        )

    decision = robots.check(url, user_agent=user_agent)
    if not decision.allowed:
        raise ListingRobotsDenied(url, decision)

    throttle_decision = throttle.reserve(domain_of(url), _listing_min_interval(throttle))

    outcome = fetch_once(
        fetcher,
        FetchRequest(url=url, user_agent=user_agent, timeout_seconds=timeout_seconds),
    )
    if not outcome.ok:
        assert outcome.failure is not None  # FetchOutcome 保证二者恰有其一
        raise ListingFetchFailed(url, outcome.failure)

    result = outcome.result
    assert result is not None
    text, replaced = _decode_listing(result.content)
    return ListingDocument(
        url=url,
        text=text,
        status_code=result.status_code,
        robots_outcome=decision.outcome,
        robots_url=decision.robots_url,
        waited_seconds=throttle_decision.waited_seconds,
        fetched_at=wall_clock(),
        replacement_chars=replaced,
    )


# --- 候选与报告 ---------------------------------------------------------------


class CandidateOrigin(str, Enum):
    """候选渠道的来源形态。"""

    #: 清单里已带 `atlas-*` 字段（即另一份 Atlas 目录导出）。
    ATLAS = "atlas"
    #: 通用 OPML 条目（`xmlUrl` + `type`），需要按调用方给的默认值补齐。
    EXTERNAL = "external"


@dataclass(frozen=True)
class ChannelCandidate:
    """一条**已通过校验**的候选渠道 + 它在清单里的位置与来源。"""

    index: int
    origin: CandidateOrigin
    title: str
    channel: Channel
    source: str


@dataclass(frozen=True)
class RejectedCandidate:
    """被拒绝的候选：**逐条**给出原因，不静默丢弃。"""

    index: int
    title: str
    endpoint: Optional[str]
    reason: str


class DuplicateKind(str, Enum):
    """清单内部去重冲突的三种形态。"""

    #: 同 id 且同 endpoint：同一条目出现多次。
    IDENTICAL = "identical"
    #: 同 id、不同 endpoint：id 撞车。
    SAME_ID_DIFFERENT_ENDPOINT = "same_id_different_endpoint"
    #: 同 endpoint、不同 id：同一资源被起了两个 id。
    SAME_ENDPOINT_DIFFERENT_ID = "same_endpoint_different_id"


@dataclass(frozen=True)
class CandidateConflict:
    """清单内部去重冲突：保留先出现的，报告被丢弃的。"""

    kind: DuplicateKind
    key: str
    kept_channel_id: str
    dropped_channel_id: str
    reason: str


@dataclass(frozen=True)
class AggregationResult:
    """一份清单的聚合结果（候选 + 被拒绝 + 冲突 + 解析期跳过项）。"""

    source: str
    title: Optional[str]
    candidates: Tuple[ChannelCandidate, ...]
    rejected: Tuple[RejectedCandidate, ...]
    conflicts: Tuple[CandidateConflict, ...]
    #: `parse_opml` 在解析期跳过的 outline（文本, 原因），原样带出。
    skipped: Tuple[Tuple[str, str], ...]
    #: 清单里声明的行业（信息用途；`plan_merge(create_missing_industries=True)`
    #: 会用它们补齐候选渠道依赖的行业）。
    industries: Tuple[Industry, ...]

    @property
    def channel_ids(self) -> Tuple[str, ...]:
        return tuple(c.channel.id for c in self.candidates)

    @property
    def total_seen(self) -> int:
        return len(self.candidates) + len(self.rejected)

    def summary(self) -> str:
        return (
            f"清单 {self.source!r}：候选 {len(self.candidates)} 条、"
            f"被拒 {len(self.rejected)} 条、清单内冲突 {len(self.conflicts)} 条、"
            f"解析跳过 {len(self.skipped)} 条"
        )


@dataclass(frozen=True)
class MergeConflict:
    """候选与**注册表**现有记录之间的冲突：保留注册表记录，报告被拒候选。"""

    kind: "MergeConflictKind"
    key: str
    kept: str
    incoming: str
    reason: str


class MergeConflictKind(str, Enum):
    """对账冲突的原因分类。"""

    #: 注册表已有同 id 渠道且内容不同（不覆盖）。
    ID = "id"
    #: 注册表已有同 endpoint 渠道（endpoint 双键去重）。
    ENDPOINT = "endpoint"
    #: 候选的 industry_id 在注册表里不存在，且不允许从清单补建。
    INDUSTRY_MISSING = "industry_missing"
    #: 跨对象校验（`validate_registry`）拒绝。
    INVALID = "invalid"


@dataclass(frozen=True)
class MergePlan:
    """与注册表对账的结论：要建什么、哪些已存在、哪些被拒。"""

    source: str
    create_industries: Tuple[Industry, ...]
    create_channels: Tuple[Channel, ...]
    existing: Tuple[str, ...]
    conflicts: Tuple[MergeConflict, ...]

    @property
    def is_empty(self) -> bool:
        return not self.create_industries and not self.create_channels

    def summary(self) -> str:
        return (
            f"清单 {self.source!r} 对账：新增行业 {len(self.create_industries)} 个、"
            f"新增渠道 {len(self.create_channels)} 条、"
            f"已存在（幂等）{len(self.existing)} 条、冲突被拒 {len(self.conflicts)} 条"
        )


@dataclass(frozen=True)
class AggregationOutcome:
    """端到端聚合结果：清单 → 候选 → 对账 → 落注册表。"""

    result: AggregationResult
    plan: MergePlan
    #: `None` 表示明确的"无变化"（没有任何新记录要写）。
    version: Optional[ConfigVersion]

    def summary(self) -> str:
        tail = (
            "无变化（没有新记录）"
            if self.version is None
            else f"注册表已提交 v{self.version.version}"
        )
        return f"{self.result.summary()}；{self.plan.summary()}；{tail}"


# --- 归一化 -------------------------------------------------------------------


def _legacy_to_channel(
    outline: OpmlOutline,
    *,
    default_industry_id: Optional[str],
    default_interval_seconds: Optional[int],
) -> Channel:
    """把通用 OPML 条目归一化成 `Channel`。

    做法是**回填成单条目的 Atlas OPML 再交给 `parse_opml`**：`type` 解析、
    `xmlUrl` → endpoint、由 endpoint 派生 id、`interval_seconds`、`fetch_spec`、
    以及 `Channel(...)` 的字段校验，全部由注册表自己那条路径完成。
    本模块因此没有第二套字段映射，也就没有"旁路校验"可谈。

    默认值缺失时**不猜**：`industry_id` / `interval_seconds` 必须由调用方显式给出，
    否则抛错由上层逐条记入 `rejected`（SPEC §2.9「不得静默降级」）。
    """
    attributes: Dict[str, Any] = {str(k): str(v) for k, v in outline.attributes.items()}
    attributes[_attr("object")] = "channel"
    if default_industry_id is not None:
        attributes.setdefault(_attr("industry-id"), default_industry_id)
    if default_interval_seconds is not None:
        attributes.setdefault(_attr("interval-seconds"), str(default_interval_seconds))

    root = ET.Element("opml", {"version": "2.0"})
    body = ET.SubElement(root, "body")
    ET.SubElement(body, "outline", attributes)
    document = parse_opml(ET.tostring(root, encoding="unicode"))
    if len(document.channels) != 1:
        raise InvalidContractStateError(
            f"外部 outline {outline.text!r} 归一化后得到 {len(document.channels)} 条渠道"
            "（期望恰好 1 条）"
        )
    return document.channels[0]


def _endpoint_of(outline: OpmlOutline) -> Optional[str]:
    """从外部 outline 里尽力取出 endpoint（**只用于报告**，不参与校验）。"""
    for key in (_attr("endpoint"), "xmlUrl", "url"):
        value = outline.attributes.get(key)
        if value:
            return str(value)
    return None


def aggregate_listing(
    text: str,
    *,
    source: str = "<inline>",
    default_industry_id: Optional[str] = None,
    default_interval_seconds: Optional[int] = None,
) -> AggregationResult:
    """解析 + 归一化 + 清单内去重，得到候选集合。

    - 解析一律走 `atlas.registry.opml.parse_opml`；**文档级**解析失败抛
      `AggregationError`（整份清单读不下去，无法逐条归因）。
    - 逐条问题（缺默认值、协议不在枚举内、id 非法……）进 `rejected`。
    - 清单内去重按 **endpoint（规范化后）+ id** 双键，先到先得，冲突进 `conflicts`。
    """
    try:
        document = parse_opml(text)
    except ContractError as exc:
        raise AggregationError(
            f"清单 {source!r} 解析失败（{type(exc).__name__}）：{exc}"
        ) from exc

    candidates: List[ChannelCandidate] = []
    rejected: List[RejectedCandidate] = []
    conflicts: List[CandidateConflict] = []
    by_id: Dict[str, ChannelCandidate] = {}
    by_endpoint: Dict[str, ChannelCandidate] = {}

    def accept(
        index: int, origin: CandidateOrigin, title: str, channel: Channel
    ) -> None:
        key = normalize_endpoint(channel.endpoint)
        same_id = by_id.get(channel.id)
        same_endpoint = by_endpoint.get(key)

        if same_id is not None and same_id is same_endpoint:
            conflicts.append(
                CandidateConflict(
                    kind=DuplicateKind.IDENTICAL,
                    key=channel.id,
                    kept_channel_id=same_id.channel.id,
                    dropped_channel_id=channel.id,
                    reason=(
                        f"清单内重复条目（同 id 且同 endpoint；保留首次出现的 "
                        f"{same_id.channel.id!r}）"
                    ),
                )
            )
            return
        if same_id is not None:
            conflicts.append(
                CandidateConflict(
                    kind=DuplicateKind.SAME_ID_DIFFERENT_ENDPOINT,
                    key=channel.id,
                    kept_channel_id=same_id.channel.id,
                    dropped_channel_id=channel.id,
                    reason=(
                        f"清单内 id 撞车：{channel.id!r} 已指向 "
                        f"{same_id.channel.endpoint!r}，本条指向 {channel.endpoint!r}；"
                        "保留首次出现，不覆盖"
                    ),
                )
            )
            return
        if same_endpoint is not None:
            conflicts.append(
                CandidateConflict(
                    kind=DuplicateKind.SAME_ENDPOINT_DIFFERENT_ID,
                    key=key,
                    kept_channel_id=same_endpoint.channel.id,
                    dropped_channel_id=channel.id,
                    reason=(
                        f"清单内 endpoint 重复：{key!r} 已由 "
                        f"{same_endpoint.channel.id!r} 占用，本条 id={channel.id!r}；"
                        "保留首次出现，不覆盖"
                    ),
                )
            )
            return

        candidate = ChannelCandidate(
            index=index, origin=origin, title=title, channel=channel, source=source
        )
        candidates.append(candidate)
        by_id[channel.id] = candidate
        by_endpoint[key] = candidate

    index = 0
    for channel in document.channels:
        accept(index, CandidateOrigin.ATLAS, channel.id, channel)
        index += 1

    for outline in document.legacy:
        try:
            channel = _legacy_to_channel(
                outline,
                default_industry_id=default_industry_id,
                default_interval_seconds=default_interval_seconds,
            )
        except ContractError as exc:
            rejected.append(
                RejectedCandidate(
                    index=index,
                    title=outline.text,
                    endpoint=_endpoint_of(outline),
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            index += 1
            continue
        accept(
            index,
            CandidateOrigin.EXTERNAL,
            outline.text or channel.id,
            channel,
        )
        index += 1

    return AggregationResult(
        source=source,
        title=document.title,
        candidates=tuple(candidates),
        rejected=tuple(rejected),
        conflicts=tuple(conflicts),
        skipped=tuple(document.skipped),
        industries=tuple(document.industries),
    )


# --- 与注册表对账 -------------------------------------------------------------


def plan_merge(
    result: AggregationResult,
    *,
    existing_industries: Iterable[Industry],
    existing_channels: Iterable[Channel],
    create_missing_industries: bool = False,
) -> MergePlan:
    """把候选集合与注册表现有记录对账，产出一个**不覆盖任何既有记录**的计划。

    去重键与清单内一致：**规范化 endpoint + id**。冲突一律保留注册表记录并写入
    `conflicts`（含原因），既不更新也不删除。

    跨对象规则（`industry_id` 必须存在、id 唯一）**不在这里重新实现**：每个候选都拿
    真实的 `validate_registry()` 跑一遍（候选快照 = 现有行业 + 已接受候选 + 本条），
    因此失败原因就是校验器自己的话，且能逐条归因 —— 没有"平行规则"可以跟它跑偏。

    `create_missing_industries=True` 时，候选依赖、且**清单自己声明过**的行业会一并
    纳入计划（用于往返导入一份 Atlas 目录导出）；清单里也没声明的行业仍然是拒绝理由。
    """
    industries: Dict[str, Industry] = {i.id: i for i in existing_industries}
    channels: Dict[str, Channel] = {c.id: c for c in existing_channels}
    declared: Dict[str, Industry] = {i.id: i for i in result.industries}

    endpoints: Dict[str, str] = {}
    for channel in sorted(channels.values(), key=lambda c: c.id):
        endpoints.setdefault(normalize_endpoint(channel.endpoint), channel.id)

    create_industries: List[Industry] = []
    wanted_industries: Dict[str, Industry] = {}
    create_channels: List[Channel] = []
    existing: List[str] = []
    conflicts: List[MergeConflict] = []

    def snapshot_channels(extra: Channel) -> Tuple[Channel, ...]:
        return tuple(
            sorted([*channels.values(), *create_channels, extra], key=lambda c: c.id)
        )

    for candidate in result.candidates:
        channel = candidate.channel

        current = channels.get(channel.id)
        if current is not None:
            if current.payload() == channel.payload():
                existing.append(channel.id)
            else:
                conflicts.append(
                    MergeConflict(
                        kind=MergeConflictKind.ID,
                        key=channel.id,
                        kept=current.id,
                        incoming=channel.id,
                        reason=(
                            f"注册表已有 id={channel.id!r} 的渠道且内容不同；"
                            "保留注册表记录，不覆盖（如需改请显式调用 "
                            "RegistryService.update_channel）"
                        ),
                    )
                )
            continue

        key = normalize_endpoint(channel.endpoint)
        holder = endpoints.get(key)
        if holder is not None:
            conflicts.append(
                MergeConflict(
                    kind=MergeConflictKind.ENDPOINT,
                    key=key,
                    kept=holder,
                    incoming=channel.id,
                    reason=(
                        f"注册表已有同 endpoint 渠道 {holder!r}；endpoint 双键去重保留"
                        f"既有记录，不新增 {channel.id!r}"
                    ),
                )
            )
            continue

        if channel.industry_id not in industries:
            declaration = declared.get(channel.industry_id)
            if declaration is None:
                conflicts.append(
                    MergeConflict(
                        kind=MergeConflictKind.INDUSTRY_MISSING,
                        key=channel.industry_id,
                        kept="",
                        incoming=channel.id,
                        reason=(
                            f"渠道 {channel.id!r} 的 industry_id="
                            f"{channel.industry_id!r} 既不在注册表里，也不在本清单里"
                        ),
                    )
                )
                continue
            if not create_missing_industries:
                conflicts.append(
                    MergeConflict(
                        kind=MergeConflictKind.INDUSTRY_MISSING,
                        key=channel.industry_id,
                        kept="",
                        incoming=channel.id,
                        reason=(
                            f"渠道 {channel.id!r} 的 industry_id="
                            f"{channel.industry_id!r} 不在注册表里；清单里虽有该行业声明，"
                            "但未允许从清单补建行业（create_missing_industries=False）"
                        ),
                    )
                )
                continue
            industries[channel.industry_id] = declaration
            if declaration.id not in wanted_industries:
                wanted_industries[declaration.id] = declaration
                create_industries.append(declaration)

        try:
            validate_registry(
                tuple(sorted(industries.values(), key=lambda i: i.id)),
                snapshot_channels(channel),
            )
        except ContractError as exc:
            # 回滚本条对 industries 的临时写入，避免失败候选污染后续判断。
            rolled_back = wanted_industries.pop(channel.industry_id, None)
            if rolled_back is not None:
                del industries[channel.industry_id]
                create_industries.remove(rolled_back)
            conflicts.append(
                MergeConflict(
                    kind=MergeConflictKind.INVALID,
                    key=channel.id,
                    kept="",
                    incoming=channel.id,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        create_channels.append(channel)
        endpoints[key] = channel.id

    return MergePlan(
        source=result.source,
        create_industries=tuple(create_industries),
        create_channels=tuple(create_channels),
        existing=tuple(existing),
        conflicts=tuple(conflicts),
    )


def _merge_note(plan: MergePlan, note: Optional[str], *, prefix: str) -> str:
    parts = [note] if note else []
    parts.append(prefix)
    if plan.existing:
        parts.append(f"幂等跳过 {len(plan.existing)} 条已存在且相同的渠道")
    if plan.conflicts:
        rendered = "; ".join(f"{c.incoming}: {c.reason}" for c in plan.conflicts)
        parts.append(f"冲突被拒 {len(plan.conflicts)} 条（{rendered}）")
    return "；".join(parts)


def apply_merge(
    service: RegistryService,
    plan: MergePlan,
    *,
    author: Optional[str] = None,
    note: Optional[str] = None,
    prefix: str = "T-111 目录聚合",
) -> Optional[ConfigVersion]:
    """把对账计划落进注册表（一次提交、一个版本）。

    只走 `RegistryService.commit_raw()` —— 校验、版本化、审计都在注册表现有实现里，
    本模块**不写任何注册表 SQL**。计划为空时返回 `None`，表示明确的"无变化"
    （不构造空提交：`commit()` 会拒绝无变化的提交，这是正确的）。
    """
    mutations: List[RegistryMutation] = [
        RegistryMutation(
            kind="create", object_kind="industry", payload=industry.payload()
        )
        for industry in plan.create_industries
    ]
    mutations.extend(
        RegistryMutation(kind="create", object_kind="channel", payload=channel.payload())
        for channel in plan.create_channels
    )
    if not mutations:
        return None
    return service.commit_raw(
        mutations,
        author=author,
        kind=ChangeKind.IMPORT,
        note=_merge_note(plan, note, prefix=prefix),
    )


def aggregate_registry(
    service: RegistryService,
    text: str,
    *,
    source: str = "<inline>",
    default_industry_id: Optional[str] = None,
    default_interval_seconds: Optional[int] = None,
    create_missing_industries: bool = False,
    author: Optional[str] = None,
    note: Optional[str] = None,
) -> AggregationOutcome:
    """端到端：清单文本 → 候选 → 与注册表对账 → 提交。

    非破坏性：只新增，绝不 update / delete 既有记录（SPEC §2.4 精神与任务书
    「保留已有记录并报告冲突，不得静默覆盖」）。
    """
    result = aggregate_listing(
        text,
        source=source,
        default_industry_id=default_industry_id,
        default_interval_seconds=default_interval_seconds,
    )
    snapshot = service.snapshot
    plan = plan_merge(
        result,
        existing_industries=snapshot.industries,
        existing_channels=snapshot.channels,
        create_missing_industries=create_missing_industries,
    )
    version = apply_merge(service, plan, author=author, note=note)
    return AggregationOutcome(result=result, plan=plan, version=version)


def aggregate_registry_from_url(
    service: RegistryService,
    url: str,
    *,
    fetcher: Fetcher,
    robots: RobotsCache,
    throttle: DomainThrottle,
    user_agent: str = DEFAULT_LISTING_USER_AGENT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    default_industry_id: Optional[str] = None,
    default_interval_seconds: Optional[int] = None,
    create_missing_industries: bool = False,
    author: Optional[str] = None,
    note: Optional[str] = None,
    wall_clock: Callable[[], datetime] = _utcnow,
) -> AggregationOutcome:
    """端到端（URL 输入）：`fetch_listing` 合规抓取 → 与 `aggregate_registry` 同一条管道。

    `fetcher` / `robots` / `throttle` 都是**必填**参数：抓外部清单也要走合规层，
    没有"默认放行"的捷径。
    """
    document = fetch_listing(
        url,
        fetcher=fetcher,
        robots=robots,
        throttle=throttle,
        user_agent=user_agent,
        timeout_seconds=timeout_seconds,
        wall_clock=wall_clock,
    )
    return aggregate_registry(
        service,
        document.text,
        source=document.url,
        default_industry_id=default_industry_id,
        default_interval_seconds=default_interval_seconds,
        create_missing_industries=create_missing_industries,
        author=author,
        note=note,
    )
