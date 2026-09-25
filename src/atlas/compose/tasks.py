"""T-120 各阶段任务：每一阶段都调用**已提交包的真实 API**。

| 节点 | 调用的已提交 API | 产出（`identity` 部分） |
|---|---|---|
| `collect` | `atlas.collect.collect_channels`（内部走 robots + 限速 + 有界重试） | `RawRecord` + 原始字节 |
| `archive` | `atlas.archive.ArchiveStore.put` / `get_content` | 落库的 `RawRecord`（不可变） |
| `normalize` | `atlas.normalize.normalize` | 归一化文本 + 偏移映射 + 派生块（可重建缓存） |
| `feed` | `atlas.feed.ArchiveFeedSource` + `run_query` | 只读投影（**不产生事实**） |
| `label` | `atlas.labels.LabelStore.add` + `ConfirmedLabel.human` | 人工 `ConfirmedLabel`（只增不改） |

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
    RawRecord,
    Snapshot,
    TaskVersions,
    VerificationStatus,
    build_anchor,
    content_sha256,
)
from atlas.feed import MAX_LIMIT, ArchiveFeedSource, FeedQuery, run_query
from atlas.labels import LabelStore
from atlas.normalize import NormalizedText, normalize
from atlas.registry.schema import Channel

__all__ = [
    "COMPOSE_CODE_VERSION",
    "MAP_SCHEMA",
    "ArchiveStage",
    "CollectStage",
    "CollectionFailedError",
    "ComposeDependencies",
    "FeedStage",
    "LabelStage",
    "NormalizeStage",
    "NormalizeStageError",
    "PipelineError",
    "StageInputError",
    "StageLabelError",
    "atomic_write",
    "build_evidence_anchor",
    "decode_content",
    "encode_content",
    "make_artifacts",
    "parse_window",
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


