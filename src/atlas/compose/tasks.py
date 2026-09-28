"""T-120 各阶段任务：每一阶段都调用**已提交包的真实 API**。

| 节点 | 调用的已提交 API | 产出（`identity` 部分） |
|---|---|---|
| `collect` | `atlas.collect.collect_channels`（内部走 robots + 限速 + 有界重试） | `RawRecord` + 原始字节 |
| `archive` | `atlas.archive.ArchiveStore.put` / `get_content` | 落库的 `RawRecord`（不可变） |
| `normalize` | `atlas.normalize.normalize` | 归一化文本 + 偏移映射 + 派生块（可重建缓存） |
| `feed` | `atlas.feed.ArchiveFeedSource` + `run_query` | 只读投影（**不产生事实**） |
| `label` | `atlas.labels.LabelStore.add` + `ConfirmedLabel.human` | 人工 `ConfirmedLabel`（只增不改） |
| `classify` | `atlas.cognition.classify_document` + `propose_units`（→ `proposed_claims`） | T-105 的分类行 / 降级行（只增不改，保留版本链） |
| `evidence` | `atlas.evidence.verify_claim` + `SqliteEvidenceStore.record`（→ T-105 的 `proposed_claims`） | 证据锚点 `evidence_spans`（只增不改） |

三条实现纪律
------------

1. **产物分两半**：`identity`（内容寻址、与时钟无关）与 `observed`（耗时 / 尝试次数 /
   时间戳等易变值）。只有 `identity` 会进入下游节点的输入快照，幂等键因此稳定；
   易变值只影响报告，不影响"要不要重跑"。
2. **不吞异常**：本模块的 `except` 只用于给异常补上下文，且必定重抛。
   需要"响亮失败"的地方显式 `raise`（采集失败、归一化文本为空、feed 查不到刚落库的条目……）。
3. **不重实现**：本模块不含抓取 / 解析 / 打标逻辑，只做"取上游产物 → 调已提交 API → 报产物"。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from atlas.archive import ArchiveStore
from atlas.cognition import (
    ClassificationError,
    LabelSpace,
    OutcomeCounters,
    ProposalPolicy,
    ProposedStoreError,
    classify_document,
    label_space_version,
    propose_units,
)
from atlas.collect import (
    DomainThrottle,
    Fetcher,
    ResultStatus,
    RetryPolicy,
    RobotsCache,
    UrllibFetcher,
    collect_channels,
)
from atlas.collect.fetch import DEFAULT_TIMEOUT_SECONDS
from atlas.contracts import (
    AtlasTask,
    ConfirmedLabel,
    ContractError,
    ProposedClaim,
    RawRecord,
    Snapshot,
    TaskVersions,
    VerificationStatus,
    build_anchor,
    content_sha256,
)
from atlas.evidence import SqliteEvidenceStore, verify_claim
from atlas.feed import MAX_LIMIT, ArchiveFeedSource, FeedQuery, run_query
from atlas.labels import LabelStore
from atlas.normalize import NormalizedText, normalize
from atlas.registry.schema import Channel

__all__ = [
    "COMPOSE_CODE_VERSION",
    "DISPATCH_CONTENT_TYPE",
    "MAP_SCHEMA",
    "ArchiveStage",
    "ClaimVerificationRequest",
    "ClassifyStage",
    "ClassifyStageError",
    "CollectStage",
    "CollectionFailedError",
    "ComposeDependencies",
    "EvidenceStage",
    "EvidenceStageError",
    "FeedStage",
    "LabelStage",
    "NormalizeStage",
    "NormalizeStageError",
    "PipelineError",
    "StageInputError",
    "StageLabelError",
    "atomic_write",
    "build_evidence_anchor",
    "claim_verification_requests",
    "classify_archived_document",
    "classify_plan_for_content",
    "classify_plan_projection",
    "content_type_conflict",
    "decode_content",
    "encode_content",
    "label_space_from_snapshot",
    "make_artifacts",
    "parse_raw_ids",
    "parse_window",
    "policy_from_snapshot",
    "proposed_claim_from_snapshot",
    "recompute_normalized_text",
    "sha256_text",
    "upstream_identity",
    "utc_hour_window",
]

#: 组合根自身的代码版本，进入每个任务的版本三元组（SPEC §3）。
COMPOSE_CODE_VERSION = "t120-compose-1"

#: `.map` 派生物的格式标识（由本包写入，可重建）。
MAP_SCHEMA = "atlas.normalize.map/1"


# --------------------------------------------------------------------------- #
# 异常：失败必须响亮可见
# --------------------------------------------------------------------------- #


class PipelineError(ContractError):
    """组合根层面的接线 / 输入错误。向上传播，不吞。"""


class StageInputError(PipelineError):
    """阶段收到的输入快照不完整或与存储不一致（接线错误，绝不"尽力继续"）。"""


class CollectionFailedError(PipelineError):
    """采集阶段有渠道失败，且策略是失败即中止。

    携带结构化的失败列表（渠道 / 类型 / 原因 / 尝试次数），因此"哪个渠道为什么失败"
    不必去翻日志。
    """

    def __init__(self, failures: Sequence[Mapping[str, Any]]) -> None:
        self.failures: Tuple[Dict[str, Any], ...] = tuple(dict(item) for item in failures)
        rendered = "；".join(
            f"{item.get('channel_id')}@{item.get('endpoint')} "
            f"[{item.get('kind')}] {item.get('reason')}（尝试 {item.get('attempts')} 次）"
            for item in self.failures
        )
        super().__init__(
            f"{len(self.failures)} 个渠道采集失败，策略=失败即中止（不产出部分结果）：{rendered}"
        )


class NormalizeStageError(PipelineError):
    """归一化产物不可用（例如文本为空）——下游无法基于它产出任何真实内容。"""


class StageLabelError(PipelineError):
    """打标阶段的输入不合法（例如解析不出目标文档）。"""


class EvidenceStageError(PipelineError):
    """证据校验阶段的输入 / 不变量不成立。

    用它而不是 `AssertionError` / 静默继续：锚点越出单元区间、claim 行版本号
    小于 1、上游缺字段，这些**都必须响亮失败**，因为它们意味着"写进
    `evidence_spans` 的坐标不是这条 claim 的证据"。
    """


class ClassifyStageError(PipelineError):
    """分类阶段（T-105）的输入 / 不变量不成立。

    用它而不是 `AssertionError` / 静默继续，覆盖四种**必须响亮失败**的情形：

    1. 快照里没有布尔开关 `enabled` —— 分不清"关掉了模型调用"与"本该调用却漏了"；
    2. 被启用却没有认知层端口 —— 绝不假装分类成功；
    3. 组合根注入的标签空间为空 / 指纹不符 —— 绝不产出空标签的分类结果（SPEC §2.5）；
    4. **单元投影与快照不一致** —— 说明"进幂等键的输入"与"真的送去分类的单元"
       不是同一份，那会让"同输入同配置 → 同输出"这句话失真。
    """


# --------------------------------------------------------------------------- #
# 依赖注入（默认装配 = 真实实现）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComposeDependencies:
    """流水线的全部外部依赖。默认 `real()` 是真实实现；测试注入假 fetcher。

    之所以每一层都做成可注入的，是因为 T-102 已经把它们做成了可注入的：
    组合根**不去绕过**合规层，只是把默认实现按 SPEC §2.12 拼起来。
    """

    fetcher: Fetcher
    robots: RobotsCache
    throttle: DomainThrottle
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    user_agent_options: Optional[Mapping[str, str]] = None

    @classmethod
    def real(cls) -> "ComposeDependencies":
        """真实装配：`urllib` fetcher + 真 robots 检查 + 真同域限速（SPEC §2.12）。"""
        fetcher = UrllibFetcher()
        return cls(
            fetcher=fetcher,
            robots=RobotsCache.from_fetcher(fetcher),
            throttle=DomainThrottle(),
        )


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def encode_content(content: bytes) -> str:
    """原始字节的 base64 表示（进入快照；JSON 安全且确定性）。"""
    return base64.b64encode(content).decode("ascii")


def decode_content(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, data: bytes) -> bool:
    """原子写文件；内容未变则不落盘（返回 `False`）。

    派生物是**可重建缓存**（SPEC §2.10）：重算不得产生半截文件，也不该无谓改动 mtime。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return False
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return True


def make_artifacts(
    identity: Mapping[str, Any], observed: Mapping[str, Any]
) -> Dict[str, Any]:
    """任务产物的标准形状：`identity`（进下游快照）+ `observed`（只进报告）。"""
    return {"identity": dict(identity), "observed": dict(observed)}


def utc_hour_window(now: Optional[datetime] = None) -> Tuple[str, datetime]:
    """默认轮询窗口：UTC 整点小时桶。返回 `(窗口 id, 窗口起点)`。

    采集是个**周期动作**：把"这一轮采集"的参数固化成 (渠道配置, 窗口)，
    同一窗口内重跑就是同一份输入 → 幂等；跨窗口才重新抓取（SPEC §3）。
    """
    moment = (now or _utcnow()).astimezone(timezone.utc)
    start = moment.replace(minute=0, second=0, microsecond=0)
    return start.isoformat(), start


def parse_window(value: Optional[str]) -> Tuple[str, datetime]:
    """把窗口字符串解析成 `(规范化 id, 起点 datetime)`；缺省 = 当前 UTC 小时桶。

    naive 时间一律按 UTC 解释（与 `atlas.feed` 的同名规则一致，不做猜测）。
    """
    if value is None:
        return utc_hour_window()
    text = value.strip()
    if not text:
        raise StageInputError("窗口 id 不得为空字符串")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise StageInputError(f"窗口 id 不是 ISO-8601 时间：{value!r}（{exc}）") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    start = parsed.astimezone(timezone.utc)
    return start.isoformat(), start


def upstream_identity(inputs: Snapshot, node: str) -> Dict[str, Any]:
    """取上游节点的内容寻址产物；缺失即**响亮失败**（绝不用空输入继续）。"""
    upstream = inputs.payload.get("upstream")
    if not isinstance(upstream, Mapping) or node not in upstream:
        raise StageInputError(
            f"任务输入缺少上游节点 {node!r} 的产物（现有："
            f"{sorted(upstream) if isinstance(upstream, Mapping) else upstream!r}）；"
            "依赖边声明了却拿不到产物属于接线错误，拒绝用空输入继续"
        )
    identity = upstream[node].get("identity") if isinstance(upstream[node], Mapping) else None
    if not isinstance(identity, Mapping):
        raise StageInputError(f"上游节点 {node!r} 的产物缺少 identity 块：{upstream[node]!r}")
    return dict(identity)


def _upstream_records(inputs: Snapshot, node: str, *, what: str) -> List[Dict[str, Any]]:
    identity = upstream_identity(inputs, node)
    records = identity.get("records")
    if not isinstance(records, list) or not records:
        raise StageInputError(f"上游节点 {node!r} 没有产出任何{what}：{identity!r}")
    return [dict(item) for item in records]


# --------------------------------------------------------------------------- #
# 阶段 1：采集（T-102）
# --------------------------------------------------------------------------- #


class CollectStage(AtlasTask):
    """按渠道配置采集，产出 `RawRecord` + 原始字节。

    输入快照（根节点，由组合根提供）::

        {"channels": [Channel.payload(), ...], "window": {"id": ..., "started_at": ...}}

    固化 `wall_clock` 为窗口起点后，`RawRecord.fetched_at` 与窗口一一对应：
    **同窗口 + 同内容 → 同一条 Raw**，幂等键才有意义（否则每次重跑都会因为
    秒级时间戳不同而"看起来是新输入"）。
    """

    name = "collect"

    def __init__(
        self,
        versions: TaskVersions,
        *,
        dependencies: ComposeDependencies,
        on_channel_failure: str = "fail",
    ) -> None:
        super().__init__(versions)
        if on_channel_failure not in ("fail", "report"):
            raise StageInputError(
                f"on_channel_failure 只接受 'fail' / 'report'，收到 {on_channel_failure!r}"
            )
        self._deps = dependencies
        self.on_channel_failure = on_channel_failure

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        channels = [
            Channel.model_validate(payload) for payload in inputs.payload["channels"]
        ]
        window = dict(inputs.payload["window"])
        _window_id, started = parse_window(window["started_at"])

        results = collect_channels(
            channels,
            fetcher=self._deps.fetcher,
            robots=self._deps.robots,
            throttle=self._deps.throttle,
            retry=self._deps.retry,
            sleeper=self._deps.sleeper,
            clock=self._deps.clock,
            wall_clock=lambda: started,
            timeout_seconds=self._deps.timeout_seconds,
            user_agent_options=self._deps.user_agent_options,
        )

        records: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        per_channel: List[Dict[str, Any]] = []

        for result in results:
            per_channel.append(
                {
                    "channel_id": result.channel_id,
                    "endpoint": result.endpoint,
                    "status": result.status.value,
                    "attempts": result.attempts,
                    "waited_seconds": result.waited_seconds,
                }
            )
            if result.status is ResultStatus.COLLECTED:
                raw, content = result.unwrap()
                records.append(
                    {
                        "raw": raw.model_dump(mode="json"),
                        "content_b64": encode_content(content),
                    }
                )
            elif result.status is ResultStatus.FAILED:
                assert result.failure is not None  # CollectionResult 构造时已强制
                failures.append(result.failure.as_dict())
            else:
                skipped.append(
                    {
                        "channel_id": result.channel_id,
                        "endpoint": result.endpoint,
                        "note": result.note,
                    }
                )

        if failures and self.on_channel_failure == "fail":
            raise CollectionFailedError(failures)
        if not records:
            raise CollectionFailedError(
                failures
                or [
                    {
                        "channel_id": item["channel_id"],
                        "endpoint": item["endpoint"],
                        "kind": "no_content",
                        "reason": f"没有渠道产出内容（{item.get('note') or '无说明'}）",
                        "attempts": 0,
                    }
                    for item in skipped
                ]
            )

        # identity 只含内容寻址字段（窗口已体现在 fetched_at 上），与耗时无关。
        identity = {"records": records, "window": {"id": window["id"], "started_at": window["started_at"]}}
        observed = {
            "window": window,
            "per_channel": per_channel,
            "failures": failures,
            "skipped": skipped,
            "collected_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)


# --------------------------------------------------------------------------- #
# 阶段 2：归档（T-103）
# --------------------------------------------------------------------------- #


class ArchiveStage(AtlasTask):
    """把采集产物写进不可变归档，并**读回校验**。

    读回校验不是多余的：它证明"落库的记录指向的字节真的在盘上、指纹真的对得上"，
    这正是 SPEC 硬规则 1 要的"可核对的产物"，而不是"有文件"。
    """

    name = "archive"

    def __init__(self, versions: TaskVersions, *, archive: ArchiveStore) -> None:
        super().__init__(versions)
        self._archive = archive

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        items = _upstream_records(inputs, "collect", what="采集结果")
        before = set(self._archive.all_raw_ids())

        stored_payloads: List[Dict[str, Any]] = []
        per_record: List[Dict[str, Any]] = []
        for item in items:
            record = RawRecord.model_validate(item["raw"])
            try:
                content = decode_content(item["content_b64"])
            except Exception as exc:  # 补上下文后重抛：内容编码错误是接线错误
                raise StageInputError(
                    f"raw_id={record.raw_id} 的 content_b64 无法解码：{exc}"
                ) from exc
            actual = content_sha256(content)
            if actual != record.content_sha256:
                raise StageInputError(
                    f"raw_id={record.raw_id} 声明指纹 {record.content_sha256[:12]}… "
                    f"实际 {actual[:12]}…（采集与归档之间的输入被篡改）"
                )

            stored = self._archive.put(record, content)
            if stored.raw_id != record.raw_id:
                raise StageInputError(
                    f"归档返回了不同的 raw_id：{stored.raw_id!r} != {record.raw_id!r}"
                )

            on_disk = self._archive.get_content(stored.raw_id)
            on_disk_digest = content_sha256(on_disk)
            if on_disk_digest != stored.content_sha256 or len(on_disk) != stored.byte_length:
                raise StageInputError(
                    f"raw_id={stored.raw_id} 归档字节与元数据不符："
                    f"盘上 {on_disk_digest[:12]}…/{len(on_disk)}B，"
                    f"元数据 {stored.content_sha256[:12]}…/{stored.byte_length}B"
                )

            stored_payloads.append(stored.model_dump(mode="json"))
            per_record.append(
                {
                    "raw_id": stored.raw_id,
                    "content_path": str(self._archive.blobs.content_path(stored.raw_id)),
                    "meta_path": str(self._archive.blobs.meta_path(stored.raw_id)),
                    "sha256_on_disk": on_disk_digest,
                    "byte_length_on_disk": len(on_disk),
                    "newly_written": stored.raw_id not in before,
                }
            )

        identity = {"records": stored_payloads}
        observed = {
            "per_record": per_record,
            "newly_written": sum(1 for item in per_record if item["newly_written"]),
            "total_archived": len(self._archive.all_raw_ids()),
            "archived_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)


# --------------------------------------------------------------------------- #
# 阶段 3：归一化（T-104）
# --------------------------------------------------------------------------- #


class NormalizeStage(AtlasTask):
    """由**归档里的字节**重算归一化文本 + 偏移映射，并写可重建缓存（SPEC §2.10）。

    归一化是 raw 的纯函数：本阶段永远从 `archive.get_content(raw_id)` 出发，
    不接受任何"别人算好的文本"，因此"可重建"是结构性成立的，而不是靠约定。

    关于"契约类型如何在阶段间传递"：`NormalizedText` **不能**进快照 —— 它的
    `to_raw_offset` 是 `SegmentTable`（`OffsetMap` = 可调用对象），序列化会失真。
    因此本阶段按 SPEC §2.10 落两份**可重建缓存**（`<raw_id>.txt` + `<raw_id>.map`），
    并把下游需要的紧凑事实（文本指纹 / 长度 / 编码 / 段表规模 / 派生块）放进产物：
    任何消费者都能由 raw 重新算出同样的文本（`recompute_normalized_text()`）。
    """

    name = "normalize"

    def __init__(
        self,
        versions: TaskVersions,
        *,
        archive: ArchiveStore,
        normalized_dir: Path,
        require_nonempty_text: bool = True,
    ) -> None:
        super().__init__(versions)
        self._archive = archive
        self._dir = Path(normalized_dir)
        self.require_nonempty_text = require_nonempty_text

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        items = _upstream_records(inputs, "archive", what="归档记录")

        facts: List[Dict[str, Any]] = []
        observed_records: List[Dict[str, Any]] = []
        for item in items:
            raw_id = item["raw_id"]
            content = self._archive.get_content(raw_id)
            digest = content_sha256(content)
            if digest != item["content_sha256"]:
                raise StageInputError(
                    f"raw_id={raw_id} 盘上字节指纹 {digest[:12]}… 与归档元数据 "
                    f"{item['content_sha256'][:12]}… 不符"
                )

            normalized_text = normalize(content)
            if self.require_nonempty_text and not normalized_text.text.strip():
                raise NormalizeStageError(
                    f"raw_id={raw_id} 归一化后文本为空（{len(normalized_text.text)} 字符）；"
                    "空文本无法支撑 feed / 证据锚点，拒绝把它当成成功的归一化产物"
                )
            self._assert_offsets_usable(normalized_text)

            text_sha256 = sha256_text(normalized_text.text)
            text_path = self._dir / f"{raw_id}.txt"
            map_path = self._dir / f"{raw_id}.map"
            wrote_text = atomic_write(text_path, normalized_text.text.encode("utf-8"))
            wrote_map = atomic_write(map_path, self._map_bytes(raw_id, normalized_text))

            facts.append(
                {
                    "raw_id": raw_id,
                    "channel_id": item["channel_id"],
                    "endpoint": item["endpoint"],
                    "content_sha256": item["content_sha256"],
                    "text_sha256": text_sha256,
                    "text_length": len(normalized_text.text),
                    "raw_text_length": len(normalized_text.raw_text),
                    "encoding": normalized_text.encoding,
                    "content_type": normalized_text.content_type,
                    "source_kind": normalized_text.source_kind,
                    "segment_count": len(normalized_text.segments),
                    "block_count": len(normalized_text.blocks),
                    # 派生块（`atlas.contracts.DerivedLocator`）：可重建、会失效，
                    # 因此只作为下游可见的派生信息传递，**绝不**用作人工锚点（SPEC §2.2）。
                    "blocks": [
                        block.model_dump(mode="json") for block in normalized_text.blocks
                    ],
                    "map_schema": MAP_SCHEMA,
                }
            )
            observed_records.append(
                {
                    "raw_id": raw_id,
                    "text_path": str(text_path),
                    "map_path": str(map_path),
                    "wrote_text": wrote_text,
                    "wrote_map": wrote_map,
                }
            )

        identity = {"records": facts}
        observed = {
            "per_record": observed_records,
            "normalized_dir": str(self._dir),
            "normalized_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)

    # ------------------------------------------------------------------
    @staticmethod
    def _assert_offsets_usable(normalized_text: NormalizedText) -> None:
        """偏移映射必须可用：落在原文范围内且单调不减。

        抽查而不是全量：全量会很慢，而段表的构造期校验（`SegmentTable`）已经保证
        结构合法；这里再做一次端到端的边界抽查，确保"映射真的能用"。
        """
        total = len(normalized_text.text)
        raw_total = len(normalized_text.raw_text)
        step = max(1, total // 64)
        previous = -1
        for index in list(range(0, max(total, 1), step)) + [max(total - 1, 0)]:
            mapped = normalized_text.to_raw_offset(index)
            if not 0 <= mapped <= raw_total:
                raise NormalizeStageError(
                    f"归一化偏移 {index} 映射到原文 {mapped}，越出原文长度 {raw_total}"
                )
            if mapped < previous:
                raise NormalizeStageError(
                    f"偏移映射非单调：偏移 {index} 映射到 {mapped} < 上一个 {previous}"
                )
            previous = mapped

    @staticmethod
    def _map_bytes(raw_id: str, normalized_text: NormalizedText) -> bytes:
        payload = {
            "schema": MAP_SCHEMA,
            "raw_id": raw_id,
            "encoding": normalized_text.encoding,
            "content_type": normalized_text.content_type,
            "source_kind": normalized_text.source_kind,
            "text_length": len(normalized_text.text),
            "raw_text_length": len(normalized_text.raw_text),
            "segments": [
                {
                    "norm_start": segment.norm_start,
                    "raw_start": segment.raw_start,
                    "length": segment.length,
                    "raw_length": segment.raw_length,
                }
                for segment in normalized_text.segments
            ],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# 阶段 4：feed 查询投影（T-106，只读）
# --------------------------------------------------------------------------- #


class FeedStage(AtlasTask):
    """证明刚落库的条目**真的可被 feed 查到**。

    `atlas.feed` 只做只读投影，本阶段因此不产生任何事实：它建 `ArchiveFeedSource`
    （注入**来自注册表**的 渠道→行业 映射，这是 SPEC §2.5 的 C8 闭环接口），
    用 `run_query` 分页拉完当前渠道，并断言本轮的每条 raw 都在结果里。
    查不到就是接线错误 —— 响亮失败，绝不"少几条也报成功"。
    """

    name = "feed"

    def __init__(
        self,
        versions: TaskVersions,
        *,
        archive: ArchiveStore,
        industry_of: Mapping[str, str],
    ) -> None:
        super().__init__(versions)
        self._archive = archive
        self._industry_of = dict(industry_of)

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        records = _upstream_records(inputs, "normalize", what="归一化记录")
        channels = tuple(sorted({item["channel_id"] for item in records}))
        expected = {item["raw_id"] for item in records}

        source = ArchiveFeedSource(self._archive, industry_of=self._industry_of)
        items: List[Any] = []
        offset = 0
        while True:
            page = run_query(
                source, FeedQuery(channels=channels, limit=MAX_LIMIT, offset=offset)
            )
            items.extend(page.items)
            if not page.has_more:
                break
            if page.next_offset is None or page.next_offset <= offset:
                raise PipelineError(
                    f"feed 分页不安全：offset={offset} 的 next_offset={page.next_offset!r}"
                    "没有前进，继续翻页会死循环"
                )
            offset = page.next_offset

        found = {item.raw_id for item in items}
        missing = sorted(expected - found)
        if missing:
            raise PipelineError(
                f"feed 查询查不到本轮刚落库的 raw_id：{missing}；"
                f"渠道={list(channels)}（只读投影与归档层不一致 = 接线错误）"
            )
        unmapped = sorted(
            {
                item.channel_id
                for item in items
                if item.raw_id in expected and item.industry is None
            }
        )
        if unmapped:
            raise PipelineError(
                f"渠道 {unmapped} 在 feed 里没有行业归属（industry=None）："
                "SPEC §2.5 要求 渠道→行业 从注册表注入，缺失会让按行业筛选静默失效"
            )

        identity = {
            "items": [
                {
                    "raw_id": item.raw_id,
                    "channel_id": item.channel_id,
                    "industry": item.industry,
                    "content_sha256": item.content_sha256,
                    "byte_length": item.byte_length,
                    "fetched_at": item.fetched_at.isoformat(),
                    "http_status": item.http_status,
                }
                for item in items
            ]
        }
        observed = {
            "query_channels": list(channels),
            "total": len(items),
            "industries": sorted({item.industry for item in items if item.industry}),
            "queried_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)


# --------------------------------------------------------------------------- #
# 阶段 5：人工打标（T-108）
# --------------------------------------------------------------------------- #


class LabelStage(AtlasTask):
    """把**人工决定**写进 Confirmed 层（只增不改）。

    本阶段不发明标签：`assignments` 由组合根从调用方给出的
    `LabelAssignment`（人给的判断）解析成具体的 `raw_id` 后放进输入快照。
    阶段只负责：确认该 raw 真的在归档里 → `ConfirmedLabel.human(...)` → `LabelStore.add`。
    """

    name = "label"

    def __init__(
        self, versions: TaskVersions, *, archive: ArchiveStore, labels: LabelStore
    ) -> None:
        super().__init__(versions)
        self._archive = archive
        self._labels = labels

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        assignments = inputs.payload.get("assignments", [])
        if not isinstance(assignments, list):
            raise StageLabelError(
                f"label 节点的输入快照里 assignments 必须是列表，收到 {type(assignments).__name__}"
            )

        labels: List[Dict[str, Any]] = []
        added: List[str] = []
        for item in assignments:
            raw_id = item["raw_id"]
            # 归档里真的存在：标签锚在 raw_id 上（SPEC §2.1），锚不到就不许写。
            try:
                self._archive.get(raw_id)
            except ContractError as exc:
                raise StageLabelError(
                    f"标签目标 raw_id={raw_id} 不在归档里，拒绝写入（{exc}）"
                ) from exc

            label = ConfirmedLabel.human(
                raw_id=raw_id,
                label_key=item["label_key"],
                label_value=item["label_value"],
                actor=item["actor"],
            )
            existed = self._labels.has(label.label_id)
            stored = self._labels.add(label)
            if stored.label_id != label.label_id:
                raise StageLabelError(
                    f"打标存储返回了不同的 label_id：{stored.label_id!r} != {label.label_id!r}"
                )
            if not existed:
                added.append(label.label_id)
            labels.append(
                {
                    "label_id": stored.label_id,
                    "raw_id": stored.raw_id,
                    "label_key": stored.label_key,
                    "label_value": stored.label_value,
                    "actor": stored.actor,
                    "created_at": stored.created_at.isoformat(),
                }
            )

        labels.sort(key=lambda item: item["label_id"])
        identity = {
            "labels": labels,
            "assignments": [
                {
                    "raw_id": item["raw_id"],
                    "label_key": item["label_key"],
                    "label_value": item["label_value"],
                    "actor": item["actor"],
                }
                for item in labels
            ],
        }
        observed = {
            "added": added,
            "already_present": [item["label_id"] for item in labels if item["label_id"] not in added],
            "total_in_store": self._labels.count(),
            "labeled_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)


# --------------------------------------------------------------------------- #
# 阶段 6：证据校验与落库（T-107）
# --------------------------------------------------------------------------- #


def parse_raw_ids(values: Optional[Sequence[str]]) -> tuple[str, ...]:
    """把 `--raw-id` 的取值整理成**去重且有序**的元组（空值响亮失败）。

    这份列表会（经组合根）进入 `evidence` 节点的输入快照 ⇒ 进入幂等键。
    因此它必须确定性：顺序由命令行给出者决定会被原样保留，重复项去掉。
    """
    seen: Dict[str, None] = {}
    for raw in values or ():
        text = raw.strip()
        if not text:
            raise EvidenceStageError("--raw-id 不得为空字符串")
        seen.setdefault(text, None)
    return tuple(seen)


def claim_verification_requests(payload: Any) -> List[Dict[str, Any]]:
    """校验并规范化 `evidence` 节点输入快照里的 `claims` 列表。

    组合根负责把 `proposed_claims` 里的 `classified` 行**投影**成这份列表。
    这里做的是**形状校验**：缺字段 / 类型不对都属于接线错误，响亮失败，
    绝不用 `.get(...) or 0` 之类的方式把它蒙混成一条"看起来能校验"的 claim。
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise EvidenceStageError(
            f"evidence 节点的输入快照里 claims 必须是列表，收到 {type(payload).__name__}"
        )
    requests: List[Dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            raise EvidenceStageError(f"claims[{index}] 必须是映射，收到 {type(item).__name__}")
        missing = [
            key
            for key in (
                "claim_id",
                "claim_version",
                "raw_id",
                "quote",
                "kind",
                "value",
                "confidence",
                "unit_char_start",
                "unit_char_end",
                "code_version",
                "config_version",
                "model_version",
            )
            if item.get(key) is None
        ]
        if missing:
            raise EvidenceStageError(f"claims[{index}] 缺少字段 {missing}：{dict(item)!r}")
        version = item["claim_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise EvidenceStageError(
                f"claims[{index}]（claim_id={item['claim_id']!r}）的 claim_version={version!r}，"
                "必须 ≥ 1：证据记录以 (claim_id, claim_version) 为幂等键，"
                "版本号小于 1 意味着这条 claim 还没有入 store"
            )
        requests.append(dict(item))
    return requests


def proposed_claim_from_snapshot(item: Mapping[str, Any]) -> ProposedClaim:
    """把组合根投影的**一行分类 claim** 还原成 T-002 的契约对象。

    **为什么不直接调 `ProposedClaimRow.as_proposed_claim()`**：阶段里没有那一行
    （`proposed_claims` 是 T-105 的表，本节点的快照里只有组合根投影出来的字段）。
    为了不让"契约对象"出现两份实现，这里的映射**逐个字段对齐**
    `ProposedClaimRow.as_proposed_claim()`，并有测试
    （`test_evidence_stage_contract_claim_matches_t105_bridge`）拿真实 store 的行
    做**逐字段比对**钉死两者一致。

    **版本号不做任何兜底**：`claim_version < 1` 已经在
    `claim_verification_requests()` 里响亮失败。刻意**不**照抄
    `as_proposed_claim()` 的 `version=max(self.version, 1)` —— 那会在"行还没入 store"
    时**编造**一个版本号，而 `(claim_id, claim_version)` 正是证据的幂等键。
    """
    return ProposedClaim(
        claim_id=str(item["claim_id"]),
        raw_id=str(item["raw_id"]),
        kind=str(item["kind"]),
        value=str(item["value"]),
        quote=str(item["quote"]),
        confidence=float(item["confidence"]),
        version=int(item["claim_version"]),
        versions=TaskVersions(
            code_version=str(item["code_version"]),
            config_version=str(item["config_version"]),
            model_version=str(item["model_version"]),
        ),
    )


class EvidenceStage(AtlasTask):
    """把 T-105 的 `classified` 产出校验成**确定性锚点**并落进 `evidence_spans`（T-107）。

    依赖与数据流（SPEC §4.5 的边 `T-104→T-107`、`T-105→T-107`）::

        normalize ──▶ evidence ◀── proposed_claims（T-105 的行，由组合根注入快照）

    三条纪律，逐条对应 SPEC §2.2 的冻结契约：

    1. **坐标只能来自确定性匹配**：本阶段把 raw **字节**（从归档读回）与 claim 的
       `quote` 交给 `atlas.evidence.verify_claim`。`ProposedClaim` 的 `extra="forbid"`
       与本阶段都不接受任何来自模型 / 来自快照的坐标。
    2. **失败 = `FAILED` + 不落库**：`SqliteEvidenceStore.record()` 对非 `VERIFIED`
       的结果**一行都不写**（明确返回 `None`），本阶段把它计进 `verification_failed`
       与 `failures`（**不吞**）。
    3. **锚点必须落在 claim 记录的真值区间内**：`unit_char_start` / `unit_char_end`
       是 T-105 的单元区间（来自 T-130 的条目 / 整篇），若确定性匹配算出的锚点
       越出它，说明"这条 quote 的证据不在它自己的单元里" ⇒ **响亮失败**，
       绝不把一条越界的坐标写进证据表。

    **未分类行必须被显式跳过并计数**（`skipped_unclassified`）：它们没有 `quote`，
    契约里也不存在"未分类的 claim"（`as_proposed_claim()` 对它们抛错）。
    组合根因此**只投影分类行**，本阶段另外如实报出"跳过了多少行、为什么"。

    幂等（SPEC §3）：输入快照 = 上游归一化产物 + 组合根注入的 claims 投影，
    两者都是内容寻址的 ⇒ 同输入同配置 ⇒ 幂等跳过；有新 claim / 新版本 /
    新归一化 ⇒ 幂等键变了 ⇒ 重跑。**这一点必须由组合根保证**（见
    `Pipeline._evidence_claims`）：若 claims 投影不进快照，新 claim 永远等不到校验。
    """

    name = "evidence"

    def __init__(
        self,
        versions: TaskVersions,
        *,
        archive: ArchiveStore,
        evidence: SqliteEvidenceStore,
        verify_only: bool = False,
    ) -> None:
        super().__init__(versions)
        self._archive = archive
        self._evidence = evidence
        self.verify_only = verify_only

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        records = _upstream_records(inputs, "normalize", what="归一化记录")
        requests = claim_verification_requests(inputs.payload.get("claims"))

        claim_raw_ids: Dict[str, None] = {}
        for item in requests:
            claim_raw_ids.setdefault(str(item["raw_id"]), None)
        scoped_raws = {str(item["raw_id"]) for item in records}
        out_of_scope = sorted(set(claim_raw_ids) - scoped_raws)
        if out_of_scope:
            raise EvidenceStageError(
                f"evidence 节点的输入里含本轮归一化产物之外的 raw_id：{out_of_scope}；"
                "锚点必须锚在本次真的读过字节的原文上（接线错误，拒绝越界校验）"
            )

        verified: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        spans_written = 0
        spans_unchanged = 0
        per_raw: Dict[str, Dict[str, int]] = {
            str(item["raw_id"]): {
                "classified_claims": 0,
                "verified": 0,
                "failed": 0,
                "spans_written": 0,
                "spans_unchanged": 0,
            }
            for item in records
        }

        for item in requests:
            claim_id = str(item["claim_id"])
            claim_version = int(item["claim_version"])
            raw_id = str(item["raw_id"])
            quote = str(item["quote"])
            unit_start = int(item["unit_char_start"])
            unit_end = int(item["unit_char_end"])

            # 契约对象由快照字段**逐字段**还原（与 `as_proposed_claim()` 对齐，有测试钉死）。
            # 防"版本号被静默编造"：`ProposedClaimRow.as_proposed_claim()` 会把
            # `version=0` 抬成 1（`max(self.version, 1)`）；本路径拒绝为未入 store 的行
            # 编造版本号（那会让 `(claim_id, claim_version)` 这个幂等键失真）。
            claim = proposed_claim_from_snapshot(item)

            content_type = self._content_type_for(records, raw_id)
            raw_bytes = self._archive.get_content(raw_id)
            record = self._archive.get(raw_id)
            if record.content_sha256 != content_sha256(raw_bytes):
                raise EvidenceStageError(
                    f"raw_id={raw_id} 归档字节指纹与元数据不符："
                    f"{content_sha256(raw_bytes)[:12]}… != {record.content_sha256[:12]}…"
                )
            # 顺序是**契约的一部分**：
            # 1) 先做确定性校验（纯函数，不落任何东西）；
            # 2) 再判锚点是否落在它自己的单元区间内 —— 越界就抛，**此时还没有写任何行**；
            # 3) 最后才落库。
            # 反过来写（先 `record()` 再判越界）会在库里留下一行越界坐标 ——
            # `evidence_spans` 是 append-only，那一行**永远删不掉**。
            outcome = verify_claim(claim, raw_bytes, content_type=content_type)
            entry = {
                "claim_id": claim_id,
                "claim_version": claim_version,
                "raw_id": raw_id,
                "quote": quote,
                "status": outcome.status.value,
                "char_start": None,
                "char_end": None,
                "unit_char_start": unit_start,
                "unit_char_end": unit_end,
            }
            if outcome.status is VerificationStatus.VERIFIED:
                anchor = outcome.anchor
                assert anchor is not None  # VERIFIED 的契约
                if not (unit_start <= anchor.char_start < anchor.char_end <= unit_end):
                    raise EvidenceStageError(
                        f"claim {claim_id}@v{claim_version} 的确定性锚点 "
                        f"[{anchor.char_start}, {anchor.char_end}) 越出它自己的单元区间 "
                        f"[{unit_start}, {unit_end})：证据不在它的单元里，拒绝写进 evidence_spans"
                    )
                entry["char_start"] = anchor.char_start
                entry["char_end"] = anchor.char_end
                verified.append(entry)
                per_raw[raw_id]["verified"] += 1
                if self.verify_only:
                    # 只校验不落库：一条都不写，也不谎称写过了。
                    spans_unchanged += 1
                    per_raw[raw_id]["spans_unchanged"] += 1
                else:
                    existed = self._evidence.span_for(claim_id, claim_version)
                    if existed is None:
                        spans_written += 1
                        per_raw[raw_id]["spans_written"] += 1
                    else:
                        spans_unchanged += 1
                        per_raw[raw_id]["spans_unchanged"] += 1
                    self._evidence.record(outcome)
            else:
                failures.append(entry)
                per_raw[raw_id]["failed"] += 1
            per_raw[raw_id]["classified_claims"] += 1

        verified.sort(key=lambda item: (item["raw_id"], item["claim_id"], item["claim_version"]))
        failures.sort(key=lambda item: (item["raw_id"], item["claim_id"], item["claim_version"]))

        unclassified_rows = int(inputs.payload.get("unclassified_rows") or 0)
        if unclassified_rows < 0:
            raise EvidenceStageError(
                f"unclassified_rows 不得为负：{unclassified_rows}（组合根注入的计数）"
            )

        identity = {
            "raws": sorted(scoped_raws),
            "claims_in_scope": len(requests),
            # 只含**内容寻址**字段：锚点是 quote + 原文的纯函数（SPEC §2.2），
            # `verified_at` 这类时刻不进来（重跑时刻不同不代表证据不同）。
            "verified": verified,
        }
        observed = {
            "raws_in_scope": len(scoped_raws),
            "claims_in_scope": len(requests),
            "classified_claims": len(requests),
            "verified": len(verified),
            "verification_failed": len(failures),
            "skipped_unclassified": unclassified_rows,
            "raws_without_claims": sorted(
                raw for raw, counts in per_raw.items() if counts["classified_claims"] == 0
            ),
            "spans_written": spans_written,
            "spans_unchanged": spans_unchanged,
            "spans_in_store": self._evidence.count(),
            "wrote_to_store": not self.verify_only,
            "per_raw": per_raw,
            "failures": failures,
            "verified_claims": verified,
            "evidence_db": str(self._evidence.db_path),
            "verified_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)

    @staticmethod
    def _content_type_for(records: Sequence[Mapping[str, Any]], raw_id: str) -> str:
        for item in records:
            if str(item["raw_id"]) == raw_id:
                return str(item.get("content_type") or "")
        raise EvidenceStageError(
            f"上游归一化产物里没有 raw_id={raw_id!r} 的 content_type："
            "证据校验必须用与归一化同一份 Content-Type"
        )


# --------------------------------------------------------------------------- #
# 阶段 7：机器分类与提议（T-105）
# --------------------------------------------------------------------------- #


#: 送进 T-105 分流的 Content-Type：**刻意留空**。
#:
#: 真实数据上的实测（`data/store` 的 75 条 raw，2026-09-26 快照）：
#:
#: | 送进去的 Content-Type | 分流结论 | 单元数 |
#: |---|---|---|
#: | `""`（让 T-130 自己嗅探**字节**） | 8 feed（830 条目）+ 65 文章 + 2 跳过 | **895** |
#: | `normalize()` 嗅探出来的那个（`text/html`） | 6 feed + 67 文章 + 2 跳过 | **877** |
#:
#: 差的 18 个单元来自 **2 份良构 RSS**（syncedreview / marktechpost，字节以
#: `<?xml version="1.0"?><rss version="2.0"` 开头）被 `normalize()` 嗅探成 `text/html`，
#: 而 T-130 的 `_reject_non_xml` 对 `mime == "text/html"` **一律拒绝** ⇒ 整份 feed 被
#: 当成"不是 feed"，再被 T-105 判成**一篇文章**。也就是说：把 T-104 的 Content-Type
#: 喂进分流会**静默丢掉 2% 的语料**（SPEC §2.17 登记的 895 个单元变成 877）。
#:
#: 因此生产路径**一律传空**（`classify_plan_for_content`），并把"T-104 记的那个
#: Content-Type 会不会改变结论"作为**冲突报告**显式呈现（`content_type_conflict`），
#: 而不是在两个答案里悄悄选一个。
DISPATCH_CONTENT_TYPE = ""


def classify_plan_for_content(
    content: bytes,
    *,
    raw_id: str,
    channel_id: str = "",
    endpoint: str = "",
) -> Any:
    """由**归档字节**做一次 T-105 的分流（`DocumentPlan`；纯计算、不落盘、不调模型）。

    Content-Type 留空（见 `DISPATCH_CONTENT_TYPE`）：让 T-130 自己按**字节**判定，
    这是 SPEC §2.17 / `tests/test_classify_realdata.py` / `tools/t105_real_evidence.py`
    实测并登记的那条路径（895 个单元）。

    为什么这条读字节的路径**不在**阶段里各写一份：`Pipeline._classify_input()` 要用
    同一份实现算出"进幂等键的单元投影"，阶段要用它算出"真的送去分类的单元"。
    两处各写一份就会漂移 —— 而这条链上任何漂移都表现为"幂等键说没变、实际变了"。
    """
    return classify_document(
        content,
        raw_id=raw_id,
        content_type=DISPATCH_CONTENT_TYPE,
        channel_id=channel_id,
        endpoint=endpoint,
    )


def classify_archived_document(
    archive: ArchiveStore,
    raw_id: str,
    *,
    channel_id: str = "",
    endpoint: str = "",
) -> Any:
    """`classify_plan_for_content` 的归档入口（自己读字节）。"""
    return classify_plan_for_content(
        archive.get_content(raw_id),
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=endpoint,
    )


def content_type_conflict(
    content: bytes,
    *,
    raw_id: str,
    content_type: str,
    channel_id: str = "",
    endpoint: str = "",
) -> Optional[Dict[str, Any]]:
    """T-104 记下来的 Content-Type **会不会改变分流结论**？会就如实报出来。

    这是"两条产品对同一份字节给出两个答案"的**显式呈现**，不是把差异藏起来：

    - `dispatched`：生产路径真的用的结论（不喂 Content-Type）；
    - `if_content_type_used`：把 T-104 嗅探出的 Content-Type 喂进去会得到的结论。

    实测（真实 store）：其中 **2 份良构 RSS** 的 `normalize()` Content-Type 是
    `text/html` ⇒ 喂进去会把整份 feed 判成**一篇文章**（895 → 877 个单元）。
    这条冲突必须可见 —— 它是 T-104 嗅探与 T-130 判据之间的真实缝隙（SPEC §7.3
    失败模式 3 的形态：数据被记录了但没有被呈现，等于没记录）。
    """
    if not content_type:
        return None
    dispatched = classify_plan_for_content(
        content, raw_id=raw_id, channel_id=channel_id, endpoint=endpoint
    )
    alternative = classify_document(
        content,
        raw_id=raw_id,
        content_type=content_type,
        channel_id=channel_id,
        endpoint=endpoint,
    )
    left = (dispatched.kind.value, dispatched.unit_count, _skip_of(dispatched))
    right = (alternative.kind.value, alternative.unit_count, _skip_of(alternative))
    if left == right:
        return None
    return {
        "raw_id": raw_id,
        "normalize_content_type": content_type,
        "dispatched": {
            "kind": left[0],
            "unit_count": left[1],
            "skip_reason": left[2],
        },
        "if_content_type_used": {
            "kind": right[0],
            "unit_count": right[1],
            "skip_reason": right[2],
        },
        "note": (
            "T-104 记录下来的 Content-Type 与 T-130 按字节的判定不一致；"
            "生产路径用**字节**判定（不喂 Content-Type），差异在此如实呈现"
        ),
    }


def _skip_of(plan: Any) -> Optional[str]:
    return plan.skip_reason.value if plan.skip_reason else None


def classify_plan_projection(plan: Any) -> Dict[str, Any]:
    """`DocumentPlan` 的**紧凑内容寻址投影**（进 `classify` 节点的输入快照）。

    **刻意不含单元文本**（895 个单元 × 最多 2000 字符会把执行记录吹成几 MB），
    只留"有哪些单元、它们的文本指纹是什么"：

    ```
    {"raw_id", "kind", "unit_count", "units_digest", "char_count", "skip_reason"}
    ```

    为什么必须进快照（这是 T-107 那条"claims 必须进快照"的**同一形态**）：
    T-105 的幂等键 `plan_digest` 含 `unit_digest`（喂给模型的文本的指纹）。
    若 `classify` 节点自己的输入快照里没有单元指纹的投影，那么"分流 / 归约规则变了、
    单元文本跟着变"就**不会改变节点自己的幂等键** ⇒ 节点被幂等跳过 ⇒
    新单元永远等不到分类。把投影放进快照，"换规则 / 换内容 ⇒ 重跑"就是结构性成立的。

    阶段侧会**重算一次同样的投影并逐字段比对**（不一致即 `ClassifyStageError`），
    因此"进幂等键的输入"与"真的送去分类的单元"不可能不是同一份。
    """
    digest = hashlib.sha256()
    for unit in plan.units:
        digest.update(unit.unit_id.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(unit.unit_digest.encode("utf-8"))
        digest.update(b"\x1f")
    return {
        "raw_id": str(plan.raw_id),
        "kind": plan.kind.value,
        "unit_count": int(plan.unit_count),
        "units_digest": digest.hexdigest(),
        "char_count": int(plan.total_chars),
        "skip_reason": plan.skip_reason.value if plan.skip_reason else None,
    }


def label_space_from_snapshot(payload: Any) -> LabelSpace:
    """把组合根注入的标签空间从**快照**还原成 `LabelSpace`（SPEC §2.5 的 C8 闭环）。

    为什么从快照还原，而不是让阶段自己去读注册表：`classify` 节点的幂等键 = 输入快照
    的摘要，而标签空间**决定送进模型的候选标签**（也因此决定产出）。从快照还原 ⇒
    "进幂等键的那份标签空间"与"真的用的那份"**物理上就是同一份**，不可能漂移。
    还原后还比对指纹，快照被改坏会**响亮失败**，不会静默换一套标签。

    标签为空时 `LabelSpace` 构造期就抛（见 `atlas.cognition.classify`），本函数把它
    重抛成本层的 `ClassifyStageError`：**绝不产出空标签的分类结果**。
    """
    if not isinstance(payload, Mapping):
        raise ClassifyStageError(
            f"classify 节点的输入快照里 label_space 必须是映射，收到 {type(payload).__name__}"
        )
    labels = payload.get("labels")
    if not isinstance(labels, list):
        raise ClassifyStageError(
            "classify 节点的输入快照里 label_space.labels 必须是列表"
            f"（组合根从注册表读取后注入），收到 {labels!r}"
        )
    try:
        space = LabelSpace.of(
            labels,
            config_version=str(payload.get("config_version") or ""),
            source=str(payload.get("source") or "injected"),
        )
    except ClassificationError as exc:
        raise ClassifyStageError(
            f"标签空间不可用：{exc}；SPEC §2.5 的 C8 闭环要求候选标签集合来自"
            "**当前启用的行业配置**，绝不静默产出空标签的分类结果"
        ) from exc
    declared = payload.get("fingerprint")
    if declared and space.fingerprint != declared:
        raise ClassifyStageError(
            f"标签空间指纹不符：快照声明 {declared}，按 labels 重算为 {space.fingerprint}；"
            "快照与内容不一致，拒绝继续（那会让'同输入同配置'失真）"
        )
    return space


def policy_from_snapshot(payload: Any) -> ProposalPolicy:
    """把 T-105 的批次 / 重试策略从**快照**还原（理由同 `label_space_from_snapshot`）。

    批次策略**改变模型看到的输入**（一次调用里塞几个单元、给多少字符），因此
    SPEC §2.17 把它计入配置指纹。它也进 `classify` 节点的输入快照，于是"改策略 ⇒
    幂等键变 ⇒ 重跑"是结构性成立的，而不是靠调用方记得改版本号。
    """
    if not isinstance(payload, Mapping):
        raise ClassifyStageError(
            f"classify 节点的输入快照里 policy 必须是映射，收到 {type(payload).__name__}"
        )
    values = {key: value for key, value in payload.items() if key != "fingerprint"}
    try:
        policy = ProposalPolicy(**values)  # type: ignore[arg-type]
    except (TypeError, ProposedStoreError) as exc:
        raise ClassifyStageError(
            f"批次策略无法从快照还原：{type(exc).__name__}: {exc}（快照={dict(payload)!r}）"
        ) from exc
    declared = payload.get("fingerprint")
    if declared and policy.fingerprint() != declared:
        raise ClassifyStageError(
            f"批次策略指纹不符：快照声明 {declared}，按内容重算为 {policy.fingerprint()}"
        )
    return policy


class ClassifyStage(AtlasTask):
    """跑 T-105 的提议步骤：raw → 分类单元 → 批次调用模型 → `proposed_claims`。

    依赖与数据流（SPEC §4.5 的边 `T-104→T-105`、`T-003→T-105`）::

        normalize ──▶ classify ──▶（写 proposed_claims）──▶ evidence（同一轮里校验它）

    **为什么这个节点必须存在**（本任务要闭合的缺口）：在此之前 `atlas.compose`
    对 `atlas.cognition` 零引用 —— 流水线能采集 / 归档 / 归一化 / 打标 / 校验证据，
    却**永远不会产出 `proposed_claims` 行**，于是 `evidence` 在任何新 store 上都只能
    报 `classified_claims=0`。T-105 的能力只存在于测试与 `tools/` 脚本里，
    这正是 SPEC §6.6「任务完成了，但系统不可用」的形态。

    **模型调用默认关闭**（成本：SPEC §2.17 记录边车每次调用 4.2–8.2 s 启动开销 +
    真实 token；895 个单元的语料 ≈ 334 次调用 ≈ 64 min）。开关由**组合根**写进输入快照
    （`enabled`），因此：

    - 关闭时本节点照常出现在 DAG 与报告里，但**明确记账**"没有调用模型、没有产出"，
      绝不静默空转；
    - 关闭 → 启用，快照变化 ⇒ 幂等键变化 ⇒ 节点真的会重跑（不会被上一次的
      "关闭态"执行记录跳过）。

    **降级不是异常，但必须可见**（SPEC §2.14 决策四）：模型不可用 ⇒ T-105 逐单元写
    `unclassified` 行 —— 本阶段把每个降级批次（原因码 + 受影响单元 + 耗时）放进
    `observed.degraded_calls`，由 `atlas.compose.cli.render_classify()` 逐条打印。
    只打印计数会让"整批超时"看起来像"一切正常"（§7.3 失败模式 3）。

    **幂等**（SPEC §3）分两层，且第二层才是真正省钱的那层：

    1. 节点层：输入快照（归一化产物 + 标签空间 + 策略 + 单元投影）不变 ⇒ 幂等跳过；
    2. T-105 层：`proposal_runs` 的 `plan_digest` 命中 ⇒ **该单元不再调用模型**，
       计进 `units_skipped_already_run`。

    第二层有一个**必须如实呈现**的细节（SPEC §2.17 已记录）：`max_output_tokens`
    **不在** `plan_digest` 里（`config_version` 是静态常量），所以一个已经跑过、
    可重试原因耗尽 `max_retries` 的单元会**被跳过而不是重试**。本阶段因此把
    `units_skipped_already_run` 如实报出，并在"全部单元都被跳过"时显式说明原因，
    绝不把它伪装成一次"没有输入所以无事可做"的空跑。
    """

    name = "classify"

    def __init__(
        self,
        versions: TaskVersions,
        *,
        archive: ArchiveStore,
        proposed: Any,
        port: Optional[Any] = None,
        port_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(versions)
        self._archive = archive
        self._proposed = proposed
        self._port = port
        self._port_factory = port_factory

    # ------------------------------------------------------------------
    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        payload = inputs.payload
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ClassifyStageError(
                "classify 节点的输入快照缺少布尔开关 `enabled`（组合根写入）："
                "没有它就分不清'模型调用被显式关掉'与'本该调用却漏了接线'，"
                f"拒绝在不明确的状态下继续（快照键：{sorted(payload)}）"
            )
        if not enabled:
            return self._disabled_artifacts(str(payload.get("reason") or "model_calls_disabled"))

        space = label_space_from_snapshot(payload.get("label_space"))
        policy = policy_from_snapshot(payload.get("policy"))
        scoped, declared, by_raw = self._classify_scope(inputs)
        port = self._resolve_port()

        classified: List[Dict[str, Any]] = []
        unclassified_rows = 0
        unclassified_reasons: Dict[str, int] = {}
        raws_classified: Dict[str, int] = {}
        degraded: List[Dict[str, Any]] = []
        raws_skipped: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        raw_plans: List[Dict[str, Any]] = []
        counters: Dict[str, int] = {
            key: 0 for key in OutcomeCounters.__dataclass_fields__
        }

        for raw_id in scoped:
            item = by_raw[raw_id]
            content = self._archive.get_content(raw_id)
            # 归档字节 vs 上游归一化记录的指纹：不一致就是"分类的不是归一化过的那份字节"，
            # 与 `EvidenceStage` 同一条纪律，响亮失败。
            declared_sha = str(item.get("content_sha256") or "")
            actual_sha = content_sha256(content)
            if declared_sha and declared_sha != actual_sha:
                raise ClassifyStageError(
                    f"raw_id={raw_id} 归档字节指纹 {actual_sha[:12]}… 与上游归一化记录 "
                    f"{declared_sha[:12]}… 不符（归档被改动）"
                )
            plan = classify_plan_for_content(
                content,
                raw_id=raw_id,
                channel_id=str(item.get("channel_id") or ""),
                endpoint=str(item.get("endpoint") or ""),
            )
            projection = classify_plan_projection(plan)
            if projection != declared[raw_id]:
                raise ClassifyStageError(
                    f"raw_id={raw_id} 的单元投影与输入快照不一致：本次算出 {projection}，"
                    f"快照声明 {declared[raw_id]}；这说明'进幂等键的输入'与"
                    "'真的送去分类的单元'不是同一份 —— 拒绝继续（那会让幂等失效）"
                )
            conflict = content_type_conflict(
                content,
                raw_id=raw_id,
                content_type=str(item.get("content_type") or ""),
                channel_id=str(item.get("channel_id") or ""),
                endpoint=str(item.get("endpoint") or ""),
            )
            if conflict is not None:
                conflicts.append(conflict)
            if plan.skipped:
                # 分流层明确跳过（HTML / JSON / 空 feed / 空内容）：带理由码记账，
                # **不进模型**，也不是"分类失败"。理由码是 T-105 的闭集（SkipReason）。
                raws_skipped.append(
                    {
                        "raw_id": raw_id,
                        "skip_reason": plan.skip_reason.value,
                        "detail": plan.detail,
                    }
                )
                raw_plans.append(projection)
                continue

            outcome = propose_units(
                raw_id,
                plan.units,
                label_space=space,
                port=port,
                store=self._proposed,
                policy=policy,
            )
            for key, value in outcome.counters.as_dict().items():
                counters[key] = counters.get(key, 0) + int(value)
            for row in outcome.claims:
                if row.is_classified:
                    classified.append(
                        {
                            "claim_key": row.claim_key,
                            "version": row.version,
                            "raw_id": row.raw_id,
                            "unit_id": row.unit_id,
                            "value": row.value,
                        }
                    )
                    raws_classified[row.raw_id] = raws_classified.get(row.raw_id, 0) + 1
                elif row.is_unclassified:
                    unclassified_rows += 1
                    key = str(row.reason or "unknown")
                    unclassified_reasons[key] = unclassified_reasons.get(key, 0) + 1
            for batch in outcome.batches:
                if batch.ok:
                    continue
                degraded.append(
                    {
                        "batch_id": batch.request.batch_id,
                        "raw_id": batch.request.raw_id,
                        "unit_ids": list(batch.request.unit_ids),
                        "reason": batch.reason,
                        "detail": batch.detail,
                        "elapsed_ms": batch.elapsed_ms,
                        "input_tokens": batch.input_tokens,
                        "output_tokens": batch.output_tokens,
                    }
                )
            raw_plans.append(projection)

        classified.sort(key=lambda item: (item["raw_id"], item["unit_id"], item["claim_key"]))
        degraded.sort(key=lambda item: (item["raw_id"], item["batch_id"]))
        raws_skipped.sort(key=lambda item: item["raw_id"])
        conflicts.sort(key=lambda item: item["raw_id"])

        identity = {
            "enabled": True,
            "raws": raw_plans,
            "classified": classified,
            "unclassified_rows": unclassified_rows,
            "label_space_version": label_space_version(space),
        }
        observed: Dict[str, Any] = {
            "enabled": True,
            "raws_in_scope": len(scoped),
            "raws_with_units": len(scoped) - len(raws_skipped),
            "raws_skipped": raws_skipped,
            "content_type_conflicts": conflicts,
            "dispatch_content_type": DISPATCH_CONTENT_TYPE,
            "classified_by_raw": dict(sorted(raws_classified.items())),
            "units_seen": counters["units_seen"],
            "units_run": counters["units_run"],
            "units_classified": counters["classified_units"],
            "units_unclassified": counters["unclassified_units"],
            "units_skipped_already_run": counters["units_skipped_already_run"],
            "units_deferred_to_retry": counters["units_deferred_to_retry"],
            "units_retry_exhausted": counters["units_retry_exhausted"],
            "batches": counters["batches"],
            "retries": counters["retries"],
            "calls_ok": counters["calls_ok"],
            "calls_degraded": counters["calls_degraded"],
            "extracted_claims": counters["extracted_claims"],
            "attributed_claims": counters["attributed_claims"],
            "unattributed_claims": counters["unattributed_claims"],
            "rows_written": counters["rows_written"],
            "rows_unchanged": counters["rows_unchanged"],
            "rows_out_of_space": counters["rows_out_of_space"],
            "unclassified_rows": unclassified_rows,
            "unclassified_reasons": dict(sorted(unclassified_reasons.items())),
            "input_tokens": counters["input_tokens"],
            "output_tokens": counters["output_tokens"],
            "reasoning_tokens": counters["reasoning_tokens"],
            "elapsed_ms": counters["elapsed_ms"],
            "degraded_calls": degraded,
            "label_space": space.as_dict(),
            "policy_fingerprint": policy.fingerprint(),
            "policy": policy.as_dict(),
            "claims_in_store": self._proposed.claim_count(),
            "runs_in_store": self._proposed.run_count(),
            "proposed_db": str(self._proposed.db_path),
            "classified_at": _utcnow().isoformat(),
        }
        return make_artifacts(identity, observed)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _disabled_artifacts(self, reason: str) -> Dict[str, Any]:
        """关闭态也必须**明确记账**（绝不静默空转）。

        它是一条**正常结论**（退出码 0），但报告里必须能一眼看出"没有调用模型、
        没有产出任何行"，而不是让人以为"分类跑了、只是没结果"。
        """
        return make_artifacts(
            identity={"enabled": False, "reason": reason, "classified": []},
            observed={
                "enabled": False,
                "reason": reason,
                "raws_in_scope": 0,
                "raws_skipped": [],
                "content_type_conflicts": [],
                "dispatch_content_type": DISPATCH_CONTENT_TYPE,
                "units_seen": 0,
                "units_run": 0,
                "units_classified": 0,
                "units_unclassified": 0,
                "units_skipped_already_run": 0,
                "batches": 0,
                "calls_ok": 0,
                "calls_degraded": 0,
                "rows_written": 0,
                "rows_unchanged": 0,
                "unclassified_rows": 0,
                "unclassified_reasons": {},
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "elapsed_ms": 0,
                "degraded_calls": [],
                "claims_in_store": self._proposed.claim_count(),
                "runs_in_store": self._proposed.run_count(),
                "proposed_db": str(self._proposed.db_path),
                "note": (
                    "模型调用默认关闭（`run --classify` 或 ATLAS_COGNITION=1 才启用）："
                    "本轮**没有**调用模型，也**没有**产出任何 proposed_claims 行"
                ),
            },
        )

    def _classify_scope(
        self, inputs: Snapshot
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        """校验输入快照的范围形状：`raw_ids` 与 `plans` 必须是同一个集合且在归一化产物里。

        三件事都**响亮失败**，绝不用"取交集"蒙混：范围为空会让节点"成功地什么都不做"，
        范围与投影不一致会让幂等键失真，范围超出本轮归一化产物则是接线错误。
        """
        payload = inputs.payload
        records = _upstream_records(inputs, "normalize", what="归一化记录")
        by_raw = {str(item["raw_id"]): dict(item) for item in records}

        scoped_payload = payload.get("raw_ids")
        if not isinstance(scoped_payload, list) or not scoped_payload:
            raise ClassifyStageError(
                "classify 节点的输入快照里 raw_ids 必须是非空列表（组合根按 --raw-id 收窄"
                f"后的范围），收到 {scoped_payload!r}；空范围会让节点'成功'地什么都不做"
            )
        scoped = [str(item) for item in scoped_payload]

        plans = payload.get("plans")
        if not isinstance(plans, list):
            raise ClassifyStageError(
                f"classify 节点的输入快照里 plans 必须是列表，收到 {type(plans).__name__}"
            )
        declared = {str(item["raw_id"]): dict(item) for item in plans}
        if sorted(declared) != sorted(scoped):
            raise ClassifyStageError(
                f"输入快照内部不一致：raw_ids={sorted(scoped)} 与 plans={sorted(declared)} "
                "不是同一个集合（组合根必须为每个范围内的 raw 给出单元投影）"
            )
        unknown = sorted(set(scoped) - set(by_raw))
        if unknown:
            raise ClassifyStageError(
                f"范围内的 raw_id 不在本轮归一化产物里：{unknown}；"
                "分类必须基于本轮真的读过字节的归一化产物（接线错误）"
            )
        return scoped, declared, by_raw

    def _resolve_port(self) -> Any:
        """拿到认知层端口；没有就**响亮失败**（绝不假装分类成功）。"""
        if self._port is not None:
            return self._port
        if self._port_factory is None:
            raise ClassifyStageError(
                "分类被显式启用，但组合根没有给出认知层端口（T-003 `CognitionPort`）："
                "拒绝在没有端口的情况下把'没有调用模型'报成一次成功的分类"
            )
        self._port = self._port_factory()
        return self._port


# --------------------------------------------------------------------------- #
# 便于测试与诊断：由 raw 重算一次归一化并比对
# --------------------------------------------------------------------------- #


def recompute_normalized_text(
    archive: ArchiveStore, raw_id: str, content_type: str = ""
) -> NormalizedText:
    """由归档里的 raw 重新算一遍归一化（用于"可重算"的核对，不是生产路径）。"""
    return normalize(archive.get_content(raw_id), content_type)


def build_evidence_anchor(
    archive: ArchiveStore, raw_id: str, quote: str, *, content_type: str = ""
) -> Tuple[VerificationStatus, Any, Any]:
    """由 quote 确定性推出证据锚点（T-104 ↔ T-002 的接缝，供诊断与测试使用）。"""
    record = archive.get(raw_id)
    normalized_text = recompute_normalized_text(archive, raw_id, content_type)
    return build_anchor(
        raw_id=raw_id,
        raw_sha256=record.content_sha256,
        normalized_text=normalized_text.text,
        quote=quote,
        to_raw_offset=normalized_text.to_raw_offset,
    )


