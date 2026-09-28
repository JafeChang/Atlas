"""T-120 组合根：装配依赖 + 用 `TaskRunner` 按依赖顺序执行。

DAG（边方向：`A depends_on=[B]` ⇒ B → A）::

    collect ──▶ archive ──▶ normalize ──┬──▶ feed
                                        ├──▶ label
                                        └──▶ evidence ◀── proposed_claims（T-105 的行）

**`evidence` 节点的接线（T-107）**

`evidence` 声明依赖 `normalize`（SPEC §4.5 的边 `T-104→T-107`）：它要用**归档字节**
（经 T-104 归一化所依据的同一份 `Content-Type`）把 T-105 的 `classified` 行校验成锚点。
另一条边 `T-105→T-107` 的数据（`proposed_claims` 的行）由**组合根**注入输入快照
（`Pipeline._evidence_claims`），与 `label` 节点注入人工判断是同一种做法。

> ⚠️ **为什么 claims 必须进快照，而不是让阶段自己去查库**：幂等键 = 输入快照 + 配置快照
> 的摘要（`AtlasTask.idempotency_key`）。若快照里不含 claims 投影，T-105 之后再跑出
> 新 claim 时幂等键**不变** ⇒ 节点被幂等跳过 ⇒ 新 claim 永远等不到校验。
> 把投影放进快照，"有新 claim / 新版本 / 新归一化 ⇒ 重跑"就是结构性成立的。

**T-207 的接线位置（调度器决定"采哪些渠道"）**

调度不在图里，它在**图的入口**：`Pipeline.due_decision()` 用
`atlas.schedule` 按 `channel.interval_seconds` 判断哪些渠道到期，
`Pipeline.run(due_only=True)` 只把到期的渠道放进 `collect` 根节点的输入快照。
因此链条是

    interval_seconds →（atlas.schedule）→ 本轮渠道集合 → 输入快照 → 幂等键 → 是否真抓

三条必须说清的边界：

1. **默认关闭**：`due_only=False` 时行为与 T-120 完全一致（全部启用渠道）。
2. **"没有到期的渠道"不是错误**：5 分钟一次的 cron 大多数轮次无事可做。
   `due_decision()` 把"一个可采集渠道都没有"（配置问题 ⇒ `NoSchedulableChannelError`）
   与"有渠道但都还没到期"（正常 ⇒ `DueDecision.is_idle`）**显式分开**；
   `run(due_only=True)` 在后一种情形下抛 `NothingDueError`，由 CLI 翻译成"退出码 0"。
3. **调度 ≠ 真的抓了**：窗口（默认 UTC 整点小时桶）没变，因此同窗口内被调度到的渠道
   仍会被幂等跳过。调度器只保证"该试的会去试"（见 `atlas.schedule` 的模块文档）。

为什么这样接线
--------------

**1. 输入快照来自上游的执行记录（`NodeInputs`）**

`TaskRunner.run(inputs)` 的输入是"执行前一次性给出"的映射，但 DAG 下游阶段的真实输入
（本轮抓到的字节、归档出来的 raw_id）在运行时才产生。`NodeInputs` 用一张**惰性 Mapping**
填上这条缝：某个节点的输入快照 = 它全部先决节点的 `identity` 产物（从执行记录里读）。
拓扑序保证读的时候上游一定已经跑完或已被幂等跳过，因此

- 跳过上游 ≠ 下游拿到空输入（跳过时执行记录里仍有上次的产物，下游照常拿到）；
- 输入快照仍然是**内容寻址**的，`AtlasTask.idempotency_key` 因此可以直接沿用，
  不需要覆写、也不需要任何隐藏状态（"同输入同配置 → 同输出或被跳过"仍然成立）。

上游记录缺失时 `NodeInputs` **响亮失败**，绝不用空快照继续跑（那正是"部分成功却报 success"）。

**2. 幂等键的三个输入**

| 进入键的东西 | 效果 |
|---|---|
| 渠道配置（`Channel.payload()`） | 改配置 → 重跑 |
| 轮询窗口（`window.id`） | 换窗口 → 重新采集；同窗口重跑 → 幂等跳过 |
| 版本三元组（`config_version` 来自注册表） | 改配置链（哪怕只是加了个渠道）→ 全体重跑 |

**3. 失败可见**

本模块**不捕获** `TaskFailedError`：`Pipeline.run()` 让异常向上传播，异常上带
`partial_report`（失败节点 + 被阻塞的下游 + 已成功/跳过的节点）。CLI 只负责打印它并给出非零退出码。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping as MappingABC
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from atlas.archive import ArchiveStore, open_archive
from atlas.cognition.store import CLAIM_STATUS_CLASSIFIED, open_proposed_store
from atlas.contracts import ContractError, Snapshot, TaskVersions, content_sha256
from atlas.evidence import open_evidence_store
from atlas.labels import LabelStore, open_store as open_label_store
from atlas.normalize import normalize
from atlas.registry import RegistryService, open_store as open_registry
from atlas.runner import (
    ExecutionRecord,
    ExecutionRecordStore,
    RunReport,
    TaskGraph,
    TaskRunner,
)
from atlas.schedule import (
    DueDecision,
    LastCollectionSource,
    evaluate_schedule,
    open_last_collection_source,
)

from .tasks import (
    COMPOSE_CODE_VERSION,
    ArchiveStage,
    CollectStage,
    ComposeDependencies,
    EvidenceStage,
    FeedStage,
    LabelStage,
    NormalizeStage,
    PipelineError,
    StageInputError,
    parse_raw_ids,
    parse_window,
)

__all__ = [
    "NODE_ARCHIVE",
    "NODE_COLLECT",
    "NODE_EVIDENCE",
    "NODE_FEED",
    "NODE_LABEL",
    "NODE_NORMALIZE",
    "ComposeDependencies",
    "FileExecutionRecordStore",
    "LabelAssignment",
    "NodeInputs",
    "NothingDueError",
    "Pipeline",
    "PipelineConfig",
    "build_pipeline",
]

NODE_COLLECT = "collect"
NODE_ARCHIVE = "archive"
NODE_NORMALIZE = "normalize"
NODE_FEED = "feed"
NODE_LABEL = "label"
NODE_EVIDENCE = "evidence"


class NothingDueError(PipelineError):
    """due-only 模式下**一个渠道都还没到期**。

    这是**正常状态**（5 分钟一次的 cron 大多数轮次都如此），不是失败：
    CLI 把它渲染成一行说明并返回退出码 0。把它做成异常而不是"返回空报告"，
    是为了让"没有工作"这件事在**类型上**与"跑完了一份工作"区分开 ——
    绝不用一份看起来成功的空报告假装发生过采集。

    它继承 `PipelineError`（"本层配置 / 输入不接受"的家族），但与
    `PipelineError` 的**裸实例**语义相反：裸 `PipelineError` 表示配置问题（退出码 1），
    `NothingDueError` 表示正常空闲（退出码 0）。CLI 因此必须先 `except NothingDueError`，
    再 `except PipelineError`。
    """

    def __init__(self, decision: DueDecision) -> None:
        self.decision = decision
        super().__init__(
            f"没有到期的渠道（可采集 {decision.enabled_channels} 个，全部未到期；"
            f"now={decision.now.isoformat()}）：这是正常状态，不是失败"
        )


#: 节点声明顺序即"同层相对顺序"，因此拓扑序可复现。
DEFAULT_NODES: Tuple[str, ...] = (
    NODE_COLLECT,
    NODE_ARCHIVE,
    NODE_NORMALIZE,
    NODE_FEED,
    NODE_LABEL,
    NODE_EVIDENCE,
)


# --------------------------------------------------------------------------- #
# 执行记录存储：SQLite 不是本包的资产，所以用文件（不新增共享表）
# --------------------------------------------------------------------------- #


class FileExecutionRecordStore:
    """`ExecutionRecordStore` 的 JSON 文件实现：让"幂等"跨进程成立。

    SPEC §2.10 的表归属登记要求新增表必须先登记。T-120 不拥有任何表，
    因此**不去共用 `atlas.db` 加表**，而是把执行记录写在自己的文件里
    （`<store_root>/runs/executions.json`），原子替换、只增不改、拒绝覆盖同键记录。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records: Dict[str, ExecutionRecord] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise PipelineError(
                f"执行记录文件损坏（{self._path}）：{exc}；"
                "拒绝把它当成'没有记录'，那会导致静默重跑"
            ) from exc
        if not isinstance(payload, list):
            raise PipelineError(f"执行记录文件格式错误（{self._path}）：顶层必须是列表")
        for item in payload:
            record = ExecutionRecord.model_validate(item)
            self._records[record.idempotency_key] = record

    def get(self, idempotency_key: str) -> Optional[ExecutionRecord]:
        return self._records.get(idempotency_key)

    def put(self, record: ExecutionRecord) -> None:
        existing = self._records.get(record.idempotency_key)
        if existing is not None:
            raise ContractError(
                f"幂等键 {record.idempotency_key[:12]}… 已有执行记录"
                f"（任务 {existing.task_name}），拒绝覆盖："
                "同键重复提交意味着幂等判定被绕过或键不稳定"
            )
        self._records[record.idempotency_key] = record
        self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            record.model_dump(mode="json") for record in self._records.values()
        ]
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)

    def __len__(self) -> int:
        return len(self._records)

    def keys(self) -> Tuple[str, ...]:
        return tuple(self._records)


# --------------------------------------------------------------------------- #
# 惰性输入快照：下游阶段的输入 = 上游执行记录里的 identity 产物
# --------------------------------------------------------------------------- #


class NodeInputs(MappingABC):
    """`{节点名: 输入快照}` 的惰性 Mapping（见模块文档 §1）。

    根节点（无先决条件）用组合根提供的 `roots`；其余节点的快照 = 先决节点的 `identity`
    外加组合根按节点补充的 `extra`（例如打标节点解析后的人工判断）。
    """

    def __init__(
        self,
        graph: TaskGraph,
        store: ExecutionRecordStore,
        config: Snapshot,
        *,
        roots: Optional[Mapping[str, Mapping[str, Any]]] = None,
        extra: Optional[Callable[[str, "NodeInputs"], Mapping[str, Any]]] = None,
        extras: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._config = config
        self._roots = dict(roots or {})
        self._extra = extra
        self._extras = {name: dict(payload) for name, payload in (extras or {}).items()}
        self._snapshots: Dict[str, Snapshot] = {}
        self._artifacts: Dict[str, Dict[str, Any]] = {}

    # --- Mapping 接口 -----------------------------------------------------
    def __getitem__(self, node: str) -> Snapshot:
        if node not in self._graph:
            raise KeyError(f"图中没有任务 {node!r}")
        return self._snapshot(node)

    def __iter__(self) -> Iterator[str]:
        return iter(self._graph.names)

    def __len__(self) -> int:
        return len(self._graph)

    # --- 供组合根使用 -----------------------------------------------------
    def artifacts_of(self, node: str) -> Dict[str, Any]:
        """上游节点的产物（从执行记录读）。没有记录 = 接线错误，响亮失败。"""
        if node in self._artifacts:
            return self._artifacts[node]
        task = self._graph.task(node)
        key = task.idempotency_key(self._snapshot(node), self._config)
        record = self._store.get(key)
        if record is None:
            raise StageInputError(
                f"上游节点 {node!r}（任务 {task.name}）还没有执行记录"
                f"（幂等键 {key[:12]}…）；依赖边声明了却拿不到产物，"
                "拒绝用空输入继续（那会产出'看起来成功'的空结果）"
            )
        artifacts = record.artifacts()
        self._artifacts[node] = artifacts
        return artifacts

    def identity_of(self, node: str) -> Dict[str, Any]:
        # 组合根显式提供的节点（离线复核路径）：直接给出，**不去查执行记录** ——
        # 那条路径上根本没有上游的执行记录，而"造一条"等于伪造上游产物。
        if node in self._extras:
            return dict(self._extras[node].get("identity") or {})
        artifacts = self.artifacts_of(node)
        identity = artifacts.get("identity")
        if not isinstance(identity, Mapping):
            raise StageInputError(f"上游节点 {node!r} 的产物缺少 identity 块：{artifacts!r}")
        return dict(identity)

    # --- 内部 -------------------------------------------------------------
    def _snapshot(self, node: str) -> Snapshot:
        cached = self._snapshots.get(node)
        if cached is not None:
            return cached

        dependencies = self._graph.dependencies(node)
        payload: Dict[str, Any] = {"node": node, "task": self._graph.task(node).name}
        if node in self._extras:
            # 组合根显式提供的整块快照（T-107 的离线复核路径：证据节点不重跑上游，
            # 输入直接来自存储里的既有事实）。**不是** root（它有依赖边），
            # 因此这条分支必须显式写明 —— 没有它就只能靠"造一条上游执行记录"来绕过，
            # 那等于伪造上游产物。
            payload.update(self._extras[node])
        elif dependencies:
            payload["upstream"] = {
                dep: {"identity": self.identity_of(dep)} for dep in dependencies
            }
        else:
            root = self._roots.get(node)
            if root is None:
                raise PipelineError(
                    f"根节点 {node!r} 没有输入快照；组合根必须为每个无先决条件的节点提供输入"
                )
            payload.update(root)

        if self._extra is not None:
            extra = self._extra(node, self)
            overlap = set(extra) & set(payload)
            if overlap:
                raise PipelineError(f"节点 {node!r} 的补充输入与基础输入冲突：{sorted(overlap)}")
            payload.update(extra)

        snapshot = Snapshot(payload=payload)
        self._snapshots[node] = snapshot
        return snapshot


# --------------------------------------------------------------------------- #
# 配置与人工输入
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LabelAssignment:
    """一条**人工判断**：给某个文档（或某渠道本轮的最新文档）打一个标签。

    人给的是判断，不是坐标（SPEC §2.1 / §2.2）。`channel_id` 与 `raw_id` 二选一：

    - `channel_id`：本轮该渠道采集到的文档（通常是"feed 里点一下"的语义）
    - `raw_id`：明确指定某条已归档文档（改判 / 补标）
    """

    label_key: str
    label_value: str
    actor: str
    channel_id: Optional[str] = None
    raw_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.label_key or not self.label_value:
            raise PipelineError("标签的 key / value 都不得为空")
        if not self.actor or not self.actor.strip():
            raise PipelineError("打标必须给出 actor（谁做的判断）")
        if (self.channel_id is None) == (self.raw_id is None):
            raise PipelineError(
                "LabelAssignment 必须且只能给出 channel_id 或 raw_id 之一"
                f"（收到 channel_id={self.channel_id!r}, raw_id={self.raw_id!r}）"
            )

    def sort_key(self) -> Tuple[str, str, str, str]:
        return (
            self.raw_id or "",
            self.channel_id or "",
            self.label_key,
            self.label_value,
        )


@dataclass(frozen=True)
class PipelineConfig:
    """一次流水线装配的静态配置。"""

    store_root: Path
    actor: str
    window: Optional[str] = None
    label_assignments: Tuple[LabelAssignment, ...] = ()
    on_channel_failure: str = "fail"
    max_retries: int = 1
    require_nonempty_text: bool = True
    evidence_read_only: bool = False
    raw_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.store_root).strip():
            raise PipelineError("store_root 不得为空（测试请用 tmp_path）")
        if not self.actor or not self.actor.strip():
            raise PipelineError("actor 不得为空（审计与 Confirmed 记录都要它）")
        if self.on_channel_failure not in ("fail", "report"):
            raise PipelineError(
                f"on_channel_failure 只接受 'fail' / 'report'，收到 {self.on_channel_failure!r}"
            )
        if self.max_retries < 0:
            raise PipelineError(f"max_retries 必须 >= 0，收到 {self.max_retries}")
        object.__setattr__(self, "raw_ids", parse_raw_ids(self.raw_ids))

    @property
    def root(self) -> Path:
        return Path(self.store_root)

    @property
    def db_path(self) -> Path:
        """注册表与人工标签共用的库文件（SPEC §2.10：同一份 atlas.db）。"""
        return self.root / "atlas.db"

    @property
    def proposed_path(self) -> Path:
        """T-105 的 `proposed_claims` 库文件（同一份共享 `atlas.db`，只读打开）。"""
        return self.root / "atlas.db"

    @property
    def evidence_path(self) -> Path:
        """T-107 的 `evidence_spans` 库文件（同一份共享 `atlas.db`）。"""
        return self.root / "atlas.db"

    @property
    def normalized_dir(self) -> Path:
        """归一化派生物缓存目录（SPEC §2.10 的布局）。"""
        return self.root / "normalized"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def execution_record_path(self) -> Path:
        return self.runs_dir / "executions.json"


# --------------------------------------------------------------------------- #
# 组合根
# --------------------------------------------------------------------------- #


class Pipeline:
    """把注册表 / 采集 / 归档 / 归一化 / feed / 打标 装配成一条可运行流水线。

    资源归属：由本类**打开**的资源由本类关闭；调用方传进来的由调用方负责
    （便于测试共用同一个 `ArchiveStore` 做独立核对）。
    """

    def __init__(
        self,
        config: PipelineConfig,
        *,
        dependencies: Optional[ComposeDependencies] = None,
        execution_store: Optional[ExecutionRecordStore] = None,
        archive: Optional[ArchiveStore] = None,
        labels: Optional[LabelStore] = None,
        registry: Optional[RegistryService] = None,
        evidence: Optional[Any] = None,
        proposed: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.dependencies = dependencies or ComposeDependencies.real()
        self._closed = False
        self._owned: List[Any] = []

        if execution_store is None:
            self._store: ExecutionRecordStore = FileExecutionRecordStore(
                config.execution_record_path
            )
        else:
            self._store = execution_store

        if archive is None:
            archive = open_archive(config.root)
            self._owned.append(archive)
        self.archive = archive

        if labels is None:
            labels = open_label_store(config.db_path)
            self._owned.append(labels)
        self.labels = labels

        if registry is None:
            registry = RegistryService(
                open_registry(config.db_path, author=config.actor)
            )
            self._owned.append(registry.store)
        self.registry = registry

        if evidence is None:
            evidence = open_evidence_store(config.evidence_path)
            self._owned.append(evidence)
        self.evidence = evidence

        if proposed is None:
            proposed = open_proposed_store(config.proposed_path)
            self._owned.append(proposed)
        self.proposed = proposed

        self._last_inputs: Optional[NodeInputs] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for resource in reversed(self._owned):
            resource.close()

    def __enter__(self) -> "Pipeline":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 装配
    # ------------------------------------------------------------------
    def versions(self) -> TaskVersions:
        """版本三元组：`config_version` 直接取注册表当前版本（SPEC §3）。"""
        return TaskVersions(
            code_version=COMPOSE_CODE_VERSION,
            config_version=self.registry.config_version,
        )

    @property
    def last_inputs(self) -> Optional[NodeInputs]:
        """最近一次 `run()` 用过的输入快照（诊断用；`None` 表示还没跑过）。"""
        return self._last_inputs

    def build_graph(
        self,
        *,
        versions: Optional[TaskVersions] = None,
        industry_of: Optional[Mapping[str, str]] = None,
    ) -> TaskGraph:
        """装配 DAG。阶段的依赖全部构造注入（默认实现见 `ComposeDependencies.real()`）。"""
        versions = versions or self.versions()
        industry_of = dict(industry_of or {})

        graph = TaskGraph()
        graph.declare(
            {
                NODE_COLLECT: (
                    CollectStage(
                        versions,
                        dependencies=self.dependencies,
                        on_channel_failure=self.config.on_channel_failure,
                    ),
                    (),
                ),
                NODE_ARCHIVE: (
                    ArchiveStage(versions, archive=self.archive),
                    (NODE_COLLECT,),
                ),
                NODE_NORMALIZE: (
                    NormalizeStage(
                        versions,
                        archive=self.archive,
                        normalized_dir=self.config.normalized_dir,
                        require_nonempty_text=self.config.require_nonempty_text,
                    ),
                    (NODE_ARCHIVE,),
                ),
                NODE_FEED: (
                    FeedStage(versions, archive=self.archive, industry_of=industry_of),
                    (NODE_NORMALIZE,),
                ),
                NODE_LABEL: (
                    LabelStage(versions, archive=self.archive, labels=self.labels),
                    (NODE_NORMALIZE,),
                ),
                NODE_EVIDENCE: (
                    EvidenceStage(
                        versions,
                        archive=self.archive,
                        evidence=self.evidence,
                        verify_only=self.config.evidence_read_only,
                    ),
                    (NODE_NORMALIZE,),
                ),
            }
        )
        return graph

    def registrations(self) -> Tuple[Any, ...]:
        """本轮将要采集的渠道（启用渠道 × 启用行业，SPEC §2.9）。"""
        return tuple(self.registry.fetchable_channels())

    # ------------------------------------------------------------------
    # 调度（T-207）：哪些渠道到期了
    # ------------------------------------------------------------------
    def due_decision(
        self,
        *,
        now: Optional[datetime] = None,
        last_collected: Optional[Mapping[str, datetime]] = None,
        source: Optional[LastCollectionSource] = None,
    ) -> DueDecision:
        """按 `channel.interval_seconds` 判定本轮该采哪些渠道（**纯判定 + 只读状态**）。

        Args:
            now: 注入的当前时刻（naive 按 UTC 解释）。缺省 = `datetime.now(UTC)`；
                测试与 cron 之外的调用都应当显式注入，判定才可复现。
            last_collected: 显式给出的状态（测试 / 上游已读好时用）；给了它就不再读盘。
            source: 只读状态源；缺省从 `<store_root>/atlas.db` 打开（**只读**）。

        Returns:
            `DueDecision`（见 `atlas.schedule`）。**一个渠道都没有时返回空判定**
            （`enabled_channels == 0`）：`plan` 是对任意状态都成立的只读查询，
            空注册表也该能打印出来。至于"空注册表不许跑"这条纪律，由
            `run(due_only=True)` 显式执行（它把空判定转成本层的 `PipelineError`）
            ——那里才是"真的要做采集"的入口。
        """
        channels = self.registrations()
        moment = now if now is not None else datetime.now(timezone.utc)
        if moment.tzinfo is None:  # 与 `atlas.schedule` 同一条规则：naive 按 UTC 解释
            moment = moment.replace(tzinfo=timezone.utc)
        else:
            moment = moment.astimezone(timezone.utc)
        if not channels:
            # 没有渠道 ⇒ 没有东西可判定 ⇒ 不读 `atlas.db`（库缺失 / 损坏在这里不是错误，
            # 但也绝不伪造出任何"到期渠道"）。`DueDecision` 因此如实是空的。
            return DueDecision(now=moment, enabled_channels=0, schedules=(), due_ids=())
        if last_collected is None:
            reader = source or open_last_collection_source(self.config.root)
            last_collected = reader.last_collected_at()
        return evaluate_schedule(channels, last_collected, moment)

    def plan(self, *, due_only: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
        """干跑：打印将要执行的 DAG 与本轮渠道，**不采集、不落盘**。

        每个渠道额外给出 `interval_seconds` / `due` / `last_collected_at`（T-207）——
        "这个源现在会不会被采"必须能一眼看出来，而不是靠操作者自己算间隔。
        `due_only=True` 时另外给出完整的 `schedule` 块（含 `next_due_at`）。
        """
        channels = self.registrations()
        industry_of = {channel.id: channel.industry_id for channel in channels}
        graph = self.build_graph(industry_of=industry_of)
        decision = self.due_decision(now=now)
        schedules = {item.channel_id: item for item in decision.schedules}
        payload: Dict[str, Any] = {
            "store_root": str(self.config.root),
            "config_version": self.registry.config_version,
            "code_version": COMPOSE_CODE_VERSION,
            "nodes": [
                {"name": name, "depends_on": list(graph.dependencies(name))}
                for name in graph.topological_order()
            ],
            "channels": [
                {
                    "id": channel.id,
                    "industry_id": channel.industry_id,
                    "endpoint": channel.endpoint,
                    "type": channel.type.value,
                    "interval_seconds": channel.interval_seconds,
                    "due": schedules[channel.id].due,
                    "last_collected_at": (
                        schedules[channel.id].last_collected_at.isoformat()
                        if schedules[channel.id].last_collected_at
                        else None
                    ),
                }
                for channel in channels
            ],
        }
        if due_only:
            payload["schedule"] = decision.as_dict()
        return payload

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        window: Optional[str] = None,
        targets: Optional[Iterable[str]] = None,
        due_only: bool = False,
        now: Optional[datetime] = None,
    ) -> RunReport:
        """按拓扑序执行整条流水线。

        Args:
            window: 轮询窗口（见 `parse_window`）。
            targets: 只跑这些节点及其先决条件。
            due_only: T-207。`True` 时只注册**到期**的渠道（默认 `False`：全部启用渠道，
                与 T-120 行为完全一致）。**同一个已有渠道集合不会被改动**，只是入口过滤。
            now: 调度判定用的当前时刻（`due_only=True` 时才有意义；naive 按 UTC 解释）。
                注入它是为了让"哪一轮采了哪些渠道"可复现。

        Raises:
            TaskFailedError: 某节点重试用尽仍失败（异常带 `partial_report`，**向上传播**）。
            PipelineError: 一个可采集渠道都没有 —— **配置问题**，非零退出。
            NothingDueError: `due_only=True` 且没有任何渠道到期 —— **正常状态**，
                由调用方（CLI）翻译成"退出码 0 + 一行说明"。
        """
        if due_only:
            decision = self.due_decision(now=now)
            if decision.enabled_channels == 0:
                # "一个可采集渠道都没有"是**配置问题**，与"没有到期的渠道"完全不同。
                # `due_decision()` 对空注册表返回空判定（`plan` 需要它），
                # 但**真的要做采集**的入口必须把这件事响亮地说出来。
                raise PipelineError(
                    "注册表里没有可采集的渠道（启用渠道 × 启用行业 为空）："
                    "这是**配置问题**，不是'这一轮没有到期的渠道'。"
                    "先配置渠道（`python -m atlas.compose register ...`）再跑。"
                )
            if decision.is_idle:
                raise NothingDueError(decision)
            due_ids = set(decision.due_ids)
            channels = tuple(
                channel for channel in self.registrations() if channel.id in due_ids
            )
        else:
            channels = self.registrations()
            if not channels:
                # 与 T-120 完全一致的语义（唯一变化：这句话现在只在"真的没有渠道"时说，
                # "没有到期的渠道"走上面的 NothingDueError，不再伪装成配置错误）。
                raise PipelineError(
                    "注册表里没有可采集的渠道（启用渠道 × 启用行业 为空）："
                    "先配置渠道再跑，绝不用空输入产出'成功'的空结果"
                )
        if not channels:
            # 不变量：上面两个分支各自保证了"渠道集合非空"，走到这里就是接线错误。
            raise PipelineError(
                "本轮渠道集合为空（due_only 过滤后仍不应为空）——这是接线错误，"
                "绝不用空输入产出'成功'的空结果"
            )

        window_id, window_start = parse_window(window or self.config.window)
        versions = self.versions()
        industry_of = {channel.id: channel.industry_id for channel in channels}
        graph = self.build_graph(versions=versions, industry_of=industry_of)

        config_snapshot = self._config_snapshot()
        roots: Dict[str, Mapping[str, Any]] = {
            NODE_COLLECT: {
                "channels": [channel.payload() for channel in channels],
                "window": {"id": window_id, "started_at": window_start.isoformat()},
            }
        }

        inputs = NodeInputs(
            graph,
            self._store,
            config_snapshot,
            roots=roots,
            extra=self._extra_payload,
        )

        self._last_inputs = inputs
        runner = TaskRunner(
            graph,
            self._store,
            config=config_snapshot,
            max_retries=self.config.max_retries,
        )
        if targets is None and self.config.raw_ids:
            # `raw_ids` 是**证据校验的收窄输入**（T-107 的取证 / 复核路径）：此时只跑
            # 归一化及其上游 + evidence，不跑与本轮取证无关的 feed / label。
            # 无 `raw_ids` 时 targets 保持原样（默认整图 = T-120 的行为）。
            targets = (NODE_NORMALIZE, NODE_EVIDENCE)
        return runner.run(inputs, targets=targets)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _config_snapshot(self) -> Snapshot:
        """进入每个任务幂等键的配置快照（`run()` 与离线复核路径共用同一份）。"""
        versions = self.versions()
        return Snapshot(
            payload={
                "actor": self.config.actor,
                "on_channel_failure": self.config.on_channel_failure,
                "require_nonempty_text": self.config.require_nonempty_text,
                "code_version": COMPOSE_CODE_VERSION,
                "config_version": versions.config_version,
            }
        )

    def collect_evidence_input(self) -> Tuple[Any, Snapshot, Snapshot]:
        """装配 T-107 的**离线复核**输入：`(阶段, 输入快照, 配置快照)`。

        **不采集、不重跑上游**：输入快照的 `records` 直接由归档当前内容构造
        （内容寻址：同一份字节 → 同一 `content_sha256`，可重建），`claims` 仍由
        `_evidence_claims()` 从 `proposed_claims` 投影 —— 与流水线内运行**同一份实现**，
        因此两条入口看到的 claim 集合不会漂移。

        为什么需要它：`evidence` 在 DAG 里依赖 `normalize`，而 `normalize` 依赖
        `archive`、`archive` 依赖 `collect` —— 若"跑一次证据校验"必须重跑采集，
        那么这个能力就**必须联网**才能用一次。而校验本身是纯离线的确定性匹配
        （SPEC §2.2）：字节已经在归档里、claim 已经在库里。离线入口因此是刻意的。

        Raises:
            PipelineError: 归档里没有任何 raw 可校验（"没有输入"不伪装成"校验通过"）。
        """
        versions = self.versions()
        graph = self.build_graph(versions=versions)
        raw_ids = self.archive.all_raw_ids()
        if not raw_ids:
            raise PipelineError(
                f"归档（{self.archive.raw_dir}）里没有任何 raw，证据校验没有输入；"
                "先跑一次采集（ATLAS_LIVE=1 python -m atlas.compose run）"
            )
        scoped = self._scope_raw_ids(raw_ids, covered="归档")
        records = [self._evidence_record(raw_id) for raw_id in scoped]
        config_snapshot = self._config_snapshot()
        inputs = NodeInputs(
            graph,
            self._store,
            config_snapshot,
            extra=self._extra_payload,
            extras={
                # 归一化这一步的快照直接由归档现状构造（**不重跑上游**）：
                # `_evidence_claims()` 经 `identity_of(NODE_NORMALIZE)` 读它，
                # 因此这条 extras 必须挂在 normalize 上，而不只是塞进 evidence 的 payload。
                NODE_NORMALIZE: {"identity": {"records": records}},
            },
        )
        return graph.task(NODE_EVIDENCE), inputs[NODE_EVIDENCE], config_snapshot

    def _evidence_record(self, raw_id: str) -> Dict[str, Any]:
        """归档记录 → 证据校验需要的那几个字段（归一化**元数据**，不含文本）。

        `content_type` 由**真的跑一遍 `atlas.normalize.normalize`** 得到，而不是从
        缓存文件里读：T-104 的归一化路径取决于 Content-Type（HTML / 纯文本），
        而证据锚点的坐标是**归一化区间 → 原文区间**的映射结果。若离线复核用了
        与流水线不同的 Content-Type，同一句 quote 可能落在不同的坐标上 ——
        那就成了"两条入口给出两个答案"。这里的算法与 `NormalizeStage` 完全相同
        （同一份字节、同一个函数），因此**不可能漂移**。
        """
        content = self.archive.get_content(raw_id)
        record = self.archive.get(raw_id)
        digest = content_sha256(content)
        if digest != record.content_sha256:
            raise StageInputError(
                f"raw_id={raw_id} 盘上字节指纹 {digest[:12]}… 与归档元数据 "
                f"{record.content_sha256[:12]}… 不符（归档被改动）"
            )
        normalized = normalize(content)
        return {
            "raw_id": raw_id,
            "channel_id": record.channel_id,
            "endpoint": record.endpoint,
            "content_sha256": record.content_sha256,
            # 与 `NormalizeStage` 同源：没有声明 Content-Type 时用归一化自己推断的那个
            # （`text/plain` / `text/html`），因此校验走的是同一条解码路径。
            "content_type": normalized.content_type,
        }

    def _extra_payload(self, node: str, inputs: NodeInputs) -> Mapping[str, Any]:
        """按节点补充输入快照里"组合根才知道"的部分（人工打标 / T-105 的 claims）。"""
        if node == NODE_LABEL:
            return {"assignments": self._resolve_assignments(inputs)}
        if node == NODE_EVIDENCE:
            return self._evidence_claims(inputs)
        return {}

    def _scope_raw_ids(self, raw_ids: Iterable[str], *, covered: str) -> List[str]:
        """把 `raw_ids` 配置套用到本轮 raws 上（被配置漏掉的 raws 响亮失败）。

        **刻意不用静默取交集**：`--raw-id` 打错一个字就会被解释成"没有这条"，
        于是节点带着 0 条 claim "成功"结束 —— 那正是"看起来成功、实际什么都没做"。
        `covered` 描述"可用范围"是什么（流水线内是"本轮归一化产物"，离线复核是"归档"），
        因为同一条纪律在两条入口上的可用集合不同。
        """
        available = sorted(set(raw_ids))
        if not self.config.raw_ids:
            return available
        missing = sorted(set(self.config.raw_ids) - set(available))
        if missing:
            raise PipelineError(
                f"--raw-id 指定的 raw_id 不在{covered}里：{missing}；"
                f"可用的有 {available}。拒绝静默取交集（那会让'校验过了'变成假象）"
            )
        return sorted(self.config.raw_ids)

    def _evidence_claims(self, inputs: NodeInputs) -> Mapping[str, Any]:
        """把 T-105 `proposed_claims` 的**分类行**投影成 `evidence` 节点的输入。

        SPEC §4.5 的边 `T-105→T-107` 是一条**数据边**：T-105 的行不是本图里某个节点的
        产物（`propose` 由 `python -m atlas.cognition` 按需调用模型产出），因此这条边
        由组合根注入快照补齐，而不是声明一条指向不存在节点的依赖。

        **投影里刻意不含**时刻与调用账（`created_at` / `batch_id` / token / `elapsed_ms`）
        —— 它们变化不代表证据该重算；但 `quote` / `value` / `claim_version` /
        `confidence` / 单元区间**必须**进投影，因为它们**决定锚点**。

        `unclassified_rows` 单独计数并进入快照：未分类行没有 `quote`，
        契约里也不存在"未分类的 claim"，它们**必须被显式跳过并可见**，不能被静默丢掉。

        归一化产物里**没有**任何 claim 的 raw 不报错：那是真实状态（这个 raw 还没被
        分类过）。但一旦库里有分类行而投影漏了它，幂等键就会失真，所以两者都在
        `observed` 里如实呈现（`raws_without_claims`）。
        """
        records = inputs.identity_of(NODE_NORMALIZE).get("records", [])
        if not isinstance(records, list):
            raise PipelineError(
                f"normalize 节点的 identity.records 必须是列表，收到 {type(records).__name__}"
            )
        scoped = self._scope_raw_ids(
            (str(item["raw_id"]) for item in records), covered="本轮归一化产物"
        )

        claims: List[Dict[str, Any]] = []
        unclassified = 0
        for raw_id in scoped:
            for row in self.proposed.current_for_raw(raw_id):
                if row.status != CLAIM_STATUS_CLASSIFIED:
                    # 审计行（unattributed / out_of_space）与降级行都**不是**证据：
                    # 它们的 quote 必须为空（SPEC §2.17 的 CHECK 强制），因此不可能有锚点。
                    unclassified += 1
                    continue
                claims.append(
                    {
                        "claim_id": row.claim_key,
                        "claim_version": row.version,
                        "raw_id": row.raw_id,
                        "unit_id": row.unit_id,
                        "unit_kind": row.unit_kind,
                        "unit_char_start": row.unit_char_start,
                        "unit_char_end": row.unit_char_end,
                        "kind": row.kind,
                        "value": row.value,
                        "quote": row.quote,
                        "confidence": row.confidence,
                        "code_version": row.code_version,
                        "config_version": row.config_version,
                        "model_version": row.model_version,
                        "label_space_version": row.label_space_version,
                    }
                )
        claims.sort(key=lambda item: (item["raw_id"], item["claim_id"], item["claim_version"]))
        return {
            "raw_ids": scoped,
            "claims": claims,
            "unclassified_rows": unclassified,
        }

    def _resolve_assignments(self, inputs: NodeInputs) -> List[Dict[str, Any]]:
        """把人工判断解析成具体 `raw_id`（解析不出即**响亮失败**，不静默跳过）。

        `channel_id` 形式的判断解析成"本轮归一化产物里该渠道的文档"；
        这是人给判断、系统给坐标的分工（SPEC §2.2）。
        """
        if not self.config.label_assignments:
            return []

        identity = inputs.identity_of(NODE_NORMALIZE)
        by_channel: Dict[str, List[str]] = {}
        for record in identity.get("records", []):
            by_channel.setdefault(record["channel_id"], []).append(record["raw_id"])

        resolved: List[Dict[str, Any]] = []
        for assignment in sorted(self.config.label_assignments, key=lambda a: a.sort_key()):
            if assignment.raw_id is not None:
                targets = [assignment.raw_id]
            else:
                targets = sorted(set(by_channel.get(assignment.channel_id or "", ())))
                if not targets:
                    raise PipelineError(
                        f"人工判断指向渠道 {assignment.channel_id!r}，但本轮没有任何该渠道的文档"
                        "；打标绝不静默跳过（那会让'我打过标了'变成假象）"
                    )
            for raw_id in targets:
                resolved.append(
                    {
                        "raw_id": raw_id,
                        "label_key": assignment.label_key,
                        "label_value": assignment.label_value,
                        "actor": assignment.actor,
                    }
                )
        resolved.sort(
            key=lambda item: (
                item["raw_id"],
                item["label_key"],
                item["label_value"],
                item["actor"],
            )
        )
        return resolved


def build_pipeline(
    *,
    store_root: str | Path,
    actor: str,
    window: Optional[str] = None,
    label_assignments: Sequence[LabelAssignment] = (),
    on_channel_failure: str = "fail",
    max_retries: int = 1,
    evidence_read_only: bool = False,
    raw_ids: Sequence[str] = (),
    dependencies: Optional[ComposeDependencies] = None,
    execution_store: Optional[ExecutionRecordStore] = None,
    archive: Optional[ArchiveStore] = None,
    labels: Optional[LabelStore] = None,
    registry: Optional[RegistryService] = None,
    evidence: Optional[Any] = None,
    proposed: Optional[Any] = None,
) -> Pipeline:
    """便捷装配入口（组合根的唯一公开构造方式）。"""
    config = PipelineConfig(
        store_root=Path(store_root),
        actor=actor,
        window=window,
        label_assignments=tuple(label_assignments),
        on_channel_failure=on_channel_failure,
        max_retries=max_retries,
        evidence_read_only=evidence_read_only,
        raw_ids=tuple(raw_ids),
    )
    return Pipeline(
        config,
        dependencies=dependencies,
        execution_store=execution_store,
        archive=archive,
        labels=labels,
        registry=registry,
        evidence=evidence,
        proposed=proposed,
    )
