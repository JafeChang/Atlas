"""T-110 依赖图测试：拓扑序、循环依赖检测、缺失依赖。"""

from __future__ import annotations

from typing import Any, ClassVar, Dict

import pytest

from atlas.contracts import AtlasTask, Snapshot, TaskVersions
from atlas.runner import (
    CycleError,
    DuplicateTaskError,
    MissingDependencyError,
    TaskGraph,
)


class _NoopTask(AtlasTask):
    """只为占位存在的任务：图测试不执行任何任务，因此 `run()` 必须响亮失败。"""

    name: ClassVar[str] = "noop"

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        raise NotImplementedError("图测试不执行任务")


def _task(name: str) -> AtlasTask:
    return _NoopTask(TaskVersions(code_version="c1", config_version="g1"))


def _graph(edges: Dict[str, list[str]]) -> TaskGraph:
    graph = TaskGraph()
    for name, deps in edges.items():
        graph.add_task(name, _task(name), depends_on=deps)
    return graph


# ---------------------------------------------------------------- 拓扑序


def test_topological_order_respects_edges() -> None:
    """SPEC §4.5 的边：T-001→T-002→T-110 等必须体现在顺序里。"""
    graph = _graph(
        {
            "T-001": [],
            "T-002": ["T-001"],
            "T-110": ["T-002"],
            "T-004": ["T-002"],
            "T-101": ["T-004"],
        }
    )

    order = graph.topological_order()

    assert sorted(order) == ["T-001", "T-002", "T-004", "T-101", "T-110"]
    position = {name: index for index, name in enumerate(order)}
    for prerequisite, dependent in graph.edges():
        assert position[prerequisite] < position[dependent]


def test_topological_order_is_deterministic() -> None:
    """同一张图的排序必须可复现（执行顺序可复现）。"""
    edges = {"a": [], "b": [], "c": ["a", "b"], "d": ["c"], "e": ["a"]}
    first = _graph(edges).topological_order()
    for _ in range(5):
        assert _graph(edges).topological_order() == first


def test_independent_nodes_keep_registration_order() -> None:
    graph = _graph({"first": [], "second": [], "third": []})
    assert graph.topological_order() == ["first", "second", "third"]


def test_layers_group_independent_nodes() -> None:
    graph = _graph(
        {
            "T-001": [],
            "T-002": ["T-001"],
            "T-003": ["T-002"],
            "T-004": ["T-002"],
        }
    )
    assert graph.layers() == [["T-001"], ["T-002"], ["T-003", "T-004"]]


def test_ancestors_transitive_and_excludes_self() -> None:
    graph = _graph({"a": [], "b": ["a"], "c": ["b"], "d": ["a"]})
    assert graph.ancestors("c") == ["a", "b"]
    assert graph.ancestors("a") == []


def test_dependents_and_dependencies_are_explicit() -> None:
    graph = _graph({"a": [], "b": ["a"], "c": ["a"]})
    assert graph.dependencies("b") == ("a",)
    assert graph.dependents("a") == ("b", "c")
    assert graph.dependents("b") == ()
    assert len(graph) == 3
    assert graph.names == ("a", "b", "c")


# ---------------------------------------------------------------- 循环依赖


def _cyclic_graph() -> TaskGraph:
    """构造一张成环的图。

    `add_task` 只接受**已注册**的依赖名，所以声明顺序本身不会产生环；
    环来自"重新接线"：把一个已存在节点的依赖指向它的下游。
    """
    graph = TaskGraph()
    graph.add_task("x", _task("x"))
    graph.add_task("y", _task("y"))
    graph.add_task("z", _task("z"), depends_on=["y"])
    graph.add_task("w", _task("w"), depends_on=["z"])
    graph._deps["y"].append("w")  # noqa: SLF001 - 测试专用：模拟重新接线成环
    return graph


def test_cycle_is_detected_and_names_the_nodes() -> None:
    """成环必须抛异常，且异常里要能读出环上的节点。"""
    cyclic = _cyclic_graph()

    with pytest.raises(CycleError) as excinfo:
        cyclic.topological_order()

    message = str(excinfo.value)
    assert "循环依赖" in message
    cycle = set(excinfo.value.cycle)
    assert {"y", "z", "w"} <= cycle
    assert "y" in message and "w" in message
    # 非环上的节点不应被误报
    assert "x" not in cycle


def test_validate_raises_cycle_error_with_cycle_nodes() -> None:
    with pytest.raises(CycleError) as excinfo:
        _cyclic_graph().validate()

    assert set(excinfo.value.cycle) >= {"y", "z", "w"}


def test_self_dependency_is_rejected() -> None:
    with pytest.raises(CycleError):
        TaskGraph().add_task("solo", _task("solo"), depends_on=["solo"])


def test_acyclic_graph_validates() -> None:
    _graph({"a": [], "b": ["a"], "c": ["b"]}).validate()


# ---------------------------------------------------------------- 缺失依赖


def test_missing_dependency_raises_with_names() -> None:
    graph = TaskGraph()
    graph.add_task("a", _task("a"))

    with pytest.raises(MissingDependencyError) as excinfo:
        graph.add_task("b", _task("b"), depends_on=["a", "nope"])

    assert excinfo.value.missing == ["nope"]
    assert "nope" in str(excinfo.value)
    # 失败的注册不得留下半成品节点
    assert "b" not in graph
    assert graph.names == ("a",)


def test_missing_dependency_must_be_registered_first() -> None:
    graph = TaskGraph()
    with pytest.raises(MissingDependencyError):
        graph.add_task("later", _task("later"), depends_on=["not_yet"])


def test_unknown_task_lookup_raises_keyerror() -> None:
    graph = _graph({"a": []})
    with pytest.raises(KeyError):
        graph.task("ghost")
    with pytest.raises(KeyError):
        graph.dependencies("ghost")
    with pytest.raises(KeyError):
        graph.dependents("ghost")
    with pytest.raises(KeyError):
        graph.ancestors("ghost")


# ---------------------------------------------------------------- 重复注册


def test_duplicate_task_name_is_rejected() -> None:
    graph = _graph({"a": []})
    with pytest.raises(DuplicateTaskError):
        graph.add_task("a", _task("a"))


def test_empty_task_name_is_rejected() -> None:
    graph = TaskGraph()
    with pytest.raises(Exception) as excinfo:
        graph.add_task("", _task("unnamed"))
    assert "非空" in str(excinfo.value)


def test_duplicate_dependencies_are_deduplicated() -> None:
    graph = TaskGraph()
    graph.add_task("a", _task("a"))
    graph.add_task("b", _task("b"), depends_on=["a", "a", "a"])
    assert graph.dependencies("b") == ("a",)
    assert graph.edges() == (("a", "b"),)


# ---------------------------------------------------------------- 整图声明


def test_declare_accepts_any_writing_order() -> None:
    """声明顺序任意、且不要求"先写先决条件"。"""
    graph = TaskGraph()
    graph.declare(
        {
            "T-110": (_task("T-110"), ["T-002"]),
            "T-001": (_task("T-001"), []),
            "T-002": (_task("T-002"), ["T-001"]),
        }
    )
    assert graph.topological_order() == ["T-001", "T-002", "T-110"]


def test_declare_missing_dependency_registers_nothing() -> None:
    graph = TaskGraph()
    graph.declare({"seed": (_task("seed"), [])})
    with pytest.raises(MissingDependencyError):
        graph.declare({"a": (_task("a"), ["ghost"]), "b": (_task("b"), ["a"])})
    # 失败的 declare 不得留下半张图
    assert graph.names == ("seed",)


def test_declare_detects_cycle_and_registers_nothing() -> None:
    graph = TaskGraph()
    with pytest.raises(CycleError) as excinfo:
        graph.declare(
            {
                "a": (_task("a"), ["c"]),
                "b": (_task("b"), ["a"]),
                "c": (_task("c"), ["b"]),
            }
        )
    assert set(excinfo.value.cycle) == {"a", "b", "c"}
    assert len(graph) == 0
