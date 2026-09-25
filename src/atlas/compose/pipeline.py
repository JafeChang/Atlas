"""T-120 组合根：装配依赖 + 用 `TaskRunner` 按依赖顺序执行。

DAG（边方向：`A depends_on=[B]` ⇒ B → A）::

    collect ──▶ archive ──▶ normalize ──┬──▶ feed
                                        └──▶ label

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
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import ContractError, Snapshot, TaskVersions
from atlas.labels import LabelStore, open_store as open_label_store
from atlas.registry import RegistryService, open_store as open_registry
from atlas.runner import (
    ExecutionRecord,
    ExecutionRecordStore,
    RunReport,
    TaskGraph,
    TaskRunner,
)

from .tasks import (
    COMPOSE_CODE_VERSION,
    ArchiveStage,
    CollectStage,
    ComposeDependencies,
    FeedStage,
    LabelStage,
    NormalizeStage,
    PipelineError,
    StageInputError,
    parse_window,
)

__all__ = [
    "NODE_ARCHIVE",
    "NODE_COLLECT",
    "NODE_FEED",
    "NODE_LABEL",
    "NODE_NORMALIZE",
    "ComposeDependencies",
    "FileExecutionRecordStore",
    "LabelAssignment",
    "NodeInputs",
    "Pipeline",
    "PipelineConfig",
    "build_pipeline",
]

NODE_COLLECT = "collect"
NODE_ARCHIVE = "archive"
NODE_NORMALIZE = "normalize"
NODE_FEED = "feed"
NODE_LABEL = "label"

#: 节点声明顺序即"同层相对顺序"，因此拓扑序可复现。
DEFAULT_NODES: Tuple[str, ...] = (
    NODE_COLLECT,
    NODE_ARCHIVE,
    NODE_NORMALIZE,
    NODE_FEED,
    NODE_LABEL,
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
    ) -> None:
        self._graph = graph
        self._store = store
        self._config = config
        self._roots = dict(roots or {})
        self._extra = extra
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
        if dependencies:
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

    @property
    def root(self) -> Path:
        return Path(self.store_root)

    @property
    def db_path(self) -> Path:
        """注册表与人工标签共用的库文件（SPEC §2.10：同一份 atlas.db）。"""
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
            }
        )
        return graph

    def registrations(self) -> Tuple[Any, ...]:
        """本轮将要采集的渠道（启用渠道 × 启用行业，SPEC §2.9）。"""
        return tuple(self.registry.fetchable_channels())

    def plan(self) -> Dict[str, Any]:
        """干跑：打印将要执行的 DAG 与本轮渠道，**不采集、不落盘**。"""
        channels = self.registrations()
        industry_of = {channel.id: channel.industry_id for channel in channels}
        graph = self.build_graph(industry_of=industry_of)
        return {
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
                }
                for channel in channels
            ],
        }

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        window: Optional[str] = None,
        targets: Optional[Iterable[str]] = None,
    ) -> RunReport:
        """按拓扑序执行整条流水线。

        Raises:
            TaskFailedError: 某节点重试用尽仍失败（异常带 `partial_report`，**向上传播**）。
        """
        channels = self.registrations()
        if not channels:
            raise PipelineError(
                "注册表里没有可采集的渠道（启用渠道 × 启用行业 为空）："
                "先配置渠道再跑，绝不用空输入产出'成功'的空结果"
            )

        window_id, window_start = parse_window(window or self.config.window)
        versions = self.versions()
        industry_of = {channel.id: channel.industry_id for channel in channels}
        graph = self.build_graph(versions=versions, industry_of=industry_of)

        config_snapshot = Snapshot(
            payload={
                "actor": self.config.actor,
                "on_channel_failure": self.config.on_channel_failure,
                "require_nonempty_text": self.config.require_nonempty_text,
                "code_version": COMPOSE_CODE_VERSION,
                "config_version": versions.config_version,
            }
        )
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
        return runner.run(inputs, targets=targets)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _extra_payload(self, node: str, inputs: NodeInputs) -> Mapping[str, Any]:
        """按节点补充输入快照里"组合根才知道"的部分（当前只有人工打标）。"""
        if node != NODE_LABEL:
            return {}
        return {"assignments": self._resolve_assignments(inputs)}

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
    dependencies: Optional[ComposeDependencies] = None,
    execution_store: Optional[ExecutionRecordStore] = None,
    archive: Optional[ArchiveStore] = None,
    labels: Optional[LabelStore] = None,
    registry: Optional[RegistryService] = None,
) -> Pipeline:
    """便捷装配入口（组合根的唯一公开构造方式）。"""
    config = PipelineConfig(
        store_root=Path(store_root),
        actor=actor,
        window=window,
        label_assignments=tuple(label_assignments),
        on_channel_failure=on_channel_failure,
        max_retries=max_retries,
    )
    return Pipeline(
        config,
        dependencies=dependencies,
        execution_store=execution_store,
        archive=archive,
        labels=labels,
        registry=registry,
    )
