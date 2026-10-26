"""T-110 声明式依赖图（DAG 种子）。

设计要点（SPEC §3、§4.5）：

- **任务 = 节点，依赖 = 边**：`add_task(name, task, depends_on=[...])` 声明
  "要跑 `name`，必须先跑完 `depends_on` 里的每个任务"。
- **引擎无关**：本模块只依赖 stdlib（`graphlib` / `collections`）+ 领域契约，
  不引入 Prefect / Dagster / Celery / Airflow 中的任何一个。将来替换编排引擎时
  "图不变、任务不改"（SPEC §3）。
- **循环依赖必须响亮失败**：检测到环时抛 `CycleError`，并在异常里列出**环上的节点**
  （从 `graphlib.CycleError` 里取出环，保留原始顺序），而不是只说"有环"。
- **缺失依赖必须响亮失败**：引用未声明的任务名时抛 `MissingDependencyError`，
  不静默忽略这条边（CLAUDE.md 硬规则 2）。

这里**不执行**任何任务；执行在 `atlas.runner.runner`。
"""

from __future__ import annotations

import graphlib
from typing import Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

from atlas.contracts import AtlasTask, ContractError

__all__ = [
    "CycleError",
    "DuplicateTaskError",
    "GraphError",
    "MissingDependencyError",
    "TaskGraph",
]


class GraphError(ContractError):
    """依赖图本身的定义有误（重复节点、缺失依赖、成环）。"""


class DuplicateTaskError(GraphError):
    """同一任务名被注册两次。

    拒绝重复注册而不是"后者覆盖前者"：覆盖会让依赖边指向一个被悄悄替换的实现，
    是归档基线里"配置看起来对、实际不是那个东西"的一类失败模式。
    """


class MissingDependencyError(GraphError):
    """依赖边指向一个未注册的任务名。"""

    def __init__(self, task_name: str, missing: Sequence[str]) -> None:
        self.task_name = task_name
        self.missing = list(missing)
        super().__init__(
            f"任务 {task_name!r} 声明了未注册的依赖：{sorted(self.missing)}；"
            "缺失的依赖必须先 add_task，不得忽略这条边"
        )


class CycleError(GraphError):
    """依赖图成环。异常里携带环上的节点序列。"""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        # graphlib 返回的环是闭合的（首尾同一节点），这里给出可读的环路径。
        rendered = " → ".join(self.cycle)
        super().__init__(f"依赖图存在循环依赖（环上节点）：{rendered}")


class TaskGraph:
    """声明式依赖图：节点是任务，边是"先决条件"。

    边方向约定：`A depends_on=[B]` 表示 **B → A**（B 先执行）。
    内部用 `graphlib` 的前驱表表达（`_predecessors[A] = {B, ...}`）。
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, AtlasTask] = {}
        self._deps: Dict[str, List[str]] = {}

    # ---------------------------------------------------------------- 声明

    def add_task(
        self,
        name: str,
        task: AtlasTask,
        depends_on: Iterable[str] = (),
    ) -> None:
        """注册一个任务节点（增量式）。

        - `name` 是图中唯一的节点标识（可与 `task.name` 不同，便于同一实现按
          不同输入/配置挂成多个节点）。
        - `depends_on` 里的名字**必须已经注册**：边只指向已存在的节点，于是
          "缺失依赖"在声明点就被发现，而不是等到执行时才炸。

        需要"任意书写顺序"时用 `declare()`，它会先整体检查再按依赖顺序注册。
        """
        if not name:
            raise GraphError("任务节点必须有非空名字")
        if name in self._tasks:
            raise DuplicateTaskError(
                f"任务 {name!r} 已注册，不允许重复注册（会掩盖依赖边指向的实现）"
            )
        depends = list(dict.fromkeys(depends_on))  # 去重保持顺序
        if name in depends:
            raise CycleError([name, name])
        missing = [dep for dep in depends if dep not in self._tasks]
        if missing:
            raise MissingDependencyError(name, missing)
        self._tasks[name] = task
        self._deps[name] = depends

    def declare(self, spec: Mapping[str, Tuple[AtlasTask, Iterable[str]]]) -> None:
        """批量声明：`{名字: (任务, 依赖名列表)}`，书写顺序任意。

        先整体校验（缺失依赖 / 重复名 / 自环），再注册节点、接边、查环；
        校验不过就**一个节点都不注册**，不留半张图。
        """
        names = list(spec)
        if len(set(names)) != len(names):
            raise DuplicateTaskError("declare() 的键重复")
        for name in names:
            if not name:
                raise GraphError("任务节点必须有非空名字")
            if name in self._tasks:
                raise DuplicateTaskError(f"任务 {name!r} 已注册，不允许重复注册")
            deps = tuple(dict.fromkeys(spec[name][1]))
            missing = [dep for dep in deps if dep not in spec]
            if missing:
                raise MissingDependencyError(name, missing)
            if name in deps:
                raise CycleError([name, name])

        # 全部检查通过后才会走到这里；注册+查环仍可能发现环，
        # 因此失败时把图回滚到调用前的状态——不留半张图。
        before_tasks = dict(self._tasks)
        before_deps = {name: list(deps) for name, deps in self._deps.items()}
        for name in names:
            self._tasks[name] = spec[name][0]
            self._deps[name] = list(dict.fromkeys(spec[name][1]))
        try:
            self.validate()  # 成环时抛 CycleError，并列出环上节点
        except GraphError:
            self._tasks = before_tasks
            self._deps = before_deps
            raise

    # ---------------------------------------------------------------- 查询

    def __contains__(self, name: object) -> bool:
        return name in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Iterator[str]:
        return iter(self._tasks)

    @property
    def names(self) -> Tuple[str, ...]:
        """按注册顺序返回全部节点名。"""
        return tuple(self._tasks)

    def task(self, name: str) -> AtlasTask:
        """取节点对应的任务对象；不存在时抛 `KeyError`（不返回 None）。"""
        if name not in self._tasks:
            raise KeyError(f"图中没有任务 {name!r}")
        return self._tasks[name]

    def dependencies(self, name: str) -> Tuple[str, ...]:
        """直接先决条件（不含传递闭包）。"""
        if name not in self._tasks:
            raise KeyError(f"图中没有任务 {name!r}")
        return tuple(self._deps[name])

    def dependents(self, name: str) -> Tuple[str, ...]:
        """直接后继（谁依赖了 `name`）。"""
        if name not in self._tasks:
            raise KeyError(f"图中没有任务 {name!r}")
        return tuple(other for other, deps in self._deps.items() if name in deps)

    def edges(self) -> Tuple[Tuple[str, str], ...]:
        """全部边，形如 `(先决, 后继)`，按注册顺序稳定输出。"""
        return tuple(
            (dep, name) for name, deps in self._deps.items() for dep in deps
        )

    # ---------------------------------------------------------------- 排序

    def topological_order(self) -> List[str]:
        """拓扑序：每个节点都排在其全部先决条件之后。

        同层节点的相对顺序由 `_sorter()` 按**注册顺序**喂入决定，因此同一张图的
        排序结果可复现，任务执行顺序也因此可复现（不用集合迭代序那种随机顺序）。

        成环时抛 `CycleError` 并列出环上节点。
        """
        try:
            return list(self._sorter().static_order())
        except graphlib.CycleError as exc:
            raise CycleError(list(exc.args[1]) if len(exc.args) > 1 else []) from exc

    def _sorter(self) -> graphlib.TopologicalSorter:
        """构造排序器；节点按注册顺序喂入以保证同层顺序稳定。"""
        sorter: graphlib.TopologicalSorter = graphlib.TopologicalSorter()
        for name in self._tasks:
            sorter.add(name, *self._deps[name])
        return sorter

    def validate(self) -> None:
        """显式校验图是否可拓扑排序；成环时抛 `CycleError` 并给出环上节点。

        在构建完图之后、执行之前调用；也可以在执行前只依赖
        `topological_order()`，它同样会抛 `CycleError`。
        """
        try:
            self._sorter().prepare()
        except graphlib.CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            raise CycleError(list(cycle)) from exc

    def ancestors(self, name: str) -> List[str]:
        """`name` 的全部传递先决条件，按拓扑序返回（不含自身）。"""
        if name not in self._tasks:
            raise KeyError(f"图中没有任务 {name!r}")
        ordered = self.topological_order()
        reachable: set[str] = set()
        frontier = list(self._deps[name])
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(self._deps[current])
        return [node for node in ordered if node in reachable]

    def descendants(self, name: str) -> List[str]:
        """`name` 的全部传递后继，按拓扑序返回（不含自身）。"""
        if name not in self._tasks:
            raise KeyError(f"图中没有任务 {name!r}")
        ordered = self.topological_order()
        reachable: set[str] = set()
        frontier = list(self.dependents(name))
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            frontier.extend(self.dependents(current))
        return [node for node in ordered if node in reachable]

    def layers(self) -> List[List[str]]:
        """按层级分组（同层节点之间无依赖），用于日志/并行化的将来扩展。"""
        remaining = {name: set(deps) for name, deps in self._deps.items()}
        result: List[List[str]] = []
        order = list(self._tasks)
        while remaining:
            layer = [name for name in order if name in remaining and not remaining[name]]
            if not layer:  # pragma: no cover - validate() 已保证无环
                raise CycleError(order)
            result.append(layer)
            for name in layer:
                del remaining[name]
            for deps in remaining.values():
                deps.difference_update(layer)
        return result
