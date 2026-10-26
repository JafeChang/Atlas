"""T-110 执行器测试：幂等跳过、重试、失败传播、失败不留半成品。"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List

import pytest

from atlas.contracts import (
    AtlasTask,
    ContractError,
    Snapshot,
    TaskOutput,
    TaskVersions,
)
from atlas.runner import (
    ExecutionRecord,
    InMemoryExecutionRecordStore,
    TaskFailedError,
    TaskGraph,
    TaskRunner,
)

VERSIONS = TaskVersions(code_version="code-1", config_version="cfg-1")


class _Recorder:
    """进程内调用记录：证明"真的执行了几次"。

    这是纯内存计数，不是持久化存储——执行记录本身存在
    `InMemoryExecutionRecordStore` 里。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []

    def did_run(self, name: str) -> int:
        return self.calls.count(name)


class _FlakyTask(AtlasTask):
    """可配置的假任务：`fail_times` 次调用抛异常，之后返回产物。

    产物可以随**调用序号**变化（`sequence=True`），这样"下游重跑"与"下游复用了
    上一次的产物"在断言上可区分——否则重复执行与幂等跳过看起来一模一样。
    """

    name: ClassVar[str] = "flaky"

    def __init__(
        self,
        name: str,
        recorder: _Recorder,
        *,
        fail_times: int = 0,
        artifacts: Dict[str, Any] | None = None,
        exc: BaseException | None = None,
        versions: TaskVersions = VERSIONS,
        sequence: bool = False,
    ) -> None:
        super().__init__(versions)
        self.name = name
        self._recorder = recorder
        self._fail_times = fail_times
        self._artifacts = artifacts if artifacts is not None else {"value": f"{name}-ok"}
        self._exc = exc or RuntimeError(f"{name} 故意失败")
        self._sequence = sequence

    def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
        self._recorder.calls.append(self.name)
        if self._recorder.did_run(self.name) <= self._fail_times:
            raise self._exc
        artifacts = dict(self._artifacts)
        if self._sequence:
            artifacts["call"] = self._recorder.did_run(self.name)
        return artifacts


def _runner(
    graph: TaskGraph,
    store: InMemoryExecutionRecordStore,
    *,
    max_retries: int = 1,
    config: Snapshot | None = None,
) -> TaskRunner:
    return TaskRunner(graph, store, max_retries=max_retries, config=config)


# ------------------------------------------------------- 拓扑序 + 成功路径


def test_runs_in_topological_order_and_reports() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    graph.add_task("b", _FlakyTask("b", recorder), depends_on=["a"])
    graph.add_task("c", _FlakyTask("c", recorder), depends_on=["b"])
    store = InMemoryExecutionRecordStore()

    report = _runner(graph, store).run({})

    assert recorder.calls == ["a", "b", "c"]
    assert report.order == ("a", "b", "c")
    assert report.all_succeeded()
    assert [r.task_name for r in report.succeeded] == ["a", "b", "c"]
    assert report.result("b").attempts == 1
    assert report.result("b").output is not None
    assert report.result("b").output.artifacts == {"value": "b-ok"}
    assert len(store) == 3


def test_records_only_written_for_real_executions() -> None:
    """不伪造结果：记录数 == 真实成功执行数，一条不多。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()

    _runner(graph, store).run({})

    assert len(store) == 1
    key = next(iter(store.keys()))
    record = store.get(key)
    assert record is not None
    assert record.task_name == "a"
    assert record.attempt == 1
    assert record.artifacts() == {"value": "a-ok"}
    assert record.output.idempotency_key == key


# ---------------------------------------------------------------- 幂等跳过


def test_second_run_skips_completed_task_with_reason() -> None:
    """同键已完成 → 跳过，且给出可读原因（不得静默跳过）。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    first = runner.run({})
    second = runner.run({})

    assert first.all_succeeded()
    assert recorder.calls == ["a"]  # 第二次真的没执行
    assert len(store) == 1
    assert len(second.skipped) == 1
    skipped = second.skipped[0]
    assert skipped.task_name == "a"
    assert skipped.attempts == 0
    assert "幂等跳过" in skipped.reason
    assert "输入与配置均未变化" in skipped.reason
    assert skipped.succeeded() is False
    # 跳过时回放已有产物，而不是编造或留空
    assert skipped.output is not None
    assert skipped.output.artifacts == {"value": "a-ok"}


def test_skip_emits_readable_log(caplog: pytest.LogCaptureFixture) -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    with caplog.at_level(logging.INFO, logger="atlas.runner"):
        runner.run({})
        runner.run({})

    messages = [record.getMessage() for record in caplog.records]
    assert any("开始" in m and "第 1/2 次尝试" in m for m in messages)
    assert any("结束" in m and "成功" in m for m in messages)
    assert any("跳过" in m and "幂等跳过" in m for m in messages)


def test_changed_inputs_rerun_and_produce_new_key() -> None:
    """输入变了 → 幂等键变了 → 必须重跑（幂等不是"永远只跑一次"）。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    runner.run({"a": Snapshot(payload={"doc": 1})})
    second = runner.run({"a": Snapshot(payload={"doc": 2})})

    assert recorder.calls == ["a", "a"]
    assert second.all_succeeded()
    assert len(store) == 2


def test_changed_config_reruns() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()

    runner_a = _runner(graph, store, config=Snapshot(payload={"cfg": "v1"}))
    runner_b = _runner(graph, store, config=Snapshot(payload={"cfg": "v2"}))
    runner_a.run({})
    second = runner_b.run({})

    assert recorder.calls == ["a", "a"]
    assert second.all_succeeded()


def test_changed_code_version_reruns() -> None:
    """代码版本变了也必须重跑（SPEC §3 的版本化语义）。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder, versions=VERSIONS))
    store = InMemoryExecutionRecordStore()
    _runner(graph, store).run({})

    upgraded = TaskGraph()
    upgraded.add_task(
        "a",
        _FlakyTask(
            "a",
            recorder,
            versions=TaskVersions(code_version="code-2", config_version="cfg-1"),
        ),
    )
    second = _runner(upgraded, store).run({})

    assert recorder.calls == ["a", "a"]
    assert second.all_succeeded()


def test_second_run_skips_everything_already_completed() -> None:
    """整图重跑：每个节点都按自己的幂等键跳过，一次真实执行都不发生。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder, sequence=True))
    graph.add_task("b", _FlakyTask("b", recorder, sequence=True), depends_on=["a"])
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    runner.run({})
    second = runner.run({})

    assert recorder.calls == ["a", "b"]
    assert [r.task_name for r in second.skipped] == ["a", "b"]
    assert second.succeeded == ()
    assert not second.blocked
    assert len(store) == 2


def test_skipped_upstream_does_not_block_downstream() -> None:
    """上游是"幂等跳过"（产物已存在）时，下游必须照常执行，不得被误判为阻塞。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder, sequence=True))
    graph.add_task("b", _FlakyTask("b", recorder, sequence=True), depends_on=["a"])
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    first = runner.run({})
    upstream_output = first.result("a").output
    assert upstream_output is not None

    # 只删下游的记录：模拟"下游的产物丢了 / 代码变了要重算，上游仍有效"。
    downstream_key = first.result("b").idempotency_key
    store.drop(downstream_key)  # 模拟"下游的产物丢了，上游仍有效"

    second = runner.run({})

    assert recorder.calls == ["a", "b", "b"]  # 只有下游重跑了一次
    assert [r.task_name for r in second.skipped] == ["a"]
    assert [r.task_name for r in second.succeeded] == ["b"]
    assert not second.blocked
    # 上游跳过时下游拿到的是上游**已存在的真实产物**（同一 output 对象）
    assert second.result("a").output is upstream_output
    assert second.result("b").output is not None
    assert second.result("b").output.artifacts["call"] == 2
    assert first.result("b").output is not None
    assert first.result("b").output.artifacts["call"] == 1


# ---------------------------------------------------------------- 有界重试


def test_retry_succeeds_within_bound() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("flaky", _FlakyTask("flaky", recorder, fail_times=1))
    store = InMemoryExecutionRecordStore()

    report = _runner(graph, store, max_retries=1).run({})

    assert recorder.calls == ["flaky", "flaky"]
    assert report.all_succeeded()
    assert report.result("flaky").attempts == 2
    # 只有成功的那次留下记录：失败尝试不留半成品
    assert len(store) == 1
    record = store.get(report.result("flaky").idempotency_key)
    assert record is not None and record.attempt == 2


def test_retry_limit_is_respected() -> None:
    """默认 1 次重试 = 最多 2 次尝试；再多失败也不会无界重试。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("always", _FlakyTask("always", recorder, fail_times=99))
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError):
        _runner(graph, store).run({})

    assert recorder.did_run("always") == 2  # 不是 3、也不是无限


def test_max_retries_zero_means_single_attempt() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("always", _FlakyTask("always", recorder, fail_times=99))
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError):
        _runner(graph, store, max_retries=0).run({})

    assert recorder.did_run("always") == 1


def test_negative_max_retries_is_rejected() -> None:
    graph = TaskGraph()
    with pytest.raises(ValueError):
        TaskRunner(graph, InMemoryExecutionRecordStore(), max_retries=-1)


def test_explicit_retry_count_succeeds() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("flaky", _FlakyTask("flaky", recorder, fail_times=3))
    store = InMemoryExecutionRecordStore()

    report = _runner(graph, store, max_retries=3).run({})

    assert recorder.did_run("flaky") == 4
    assert report.result("flaky").attempts == 4


# ------------------------------------------------------- 失败不留半成品


def test_failure_leaves_no_record_and_retry_starts_clean() -> None:
    """失败尝试不提交记录，因此重试面对的是干净状态。"""
    recorder = _Recorder()

    class _PoisonTask(AtlasTask):
        name: ClassVar[str] = "poison"

        def __init__(self) -> None:
            super().__init__(VERSIONS)
            self.attempts = 0
            self.keys_seen: List[str] = []

        def run(self, inputs: Snapshot, config: Snapshot) -> Dict[str, Any]:
            self.attempts += 1
            self.keys_seen.append(self.idempotency_key(inputs, config))
            if self.attempts == 1:
                raise RuntimeError("第一次尝试失败")
            return {"attempt": self.attempts}

    task = _PoisonTask()
    graph = TaskGraph()
    graph.add_task("poison", task)
    store = InMemoryExecutionRecordStore()

    # 用 max_retries=0 让第一次失败直接传播，此时存储必须为空。
    with pytest.raises(TaskFailedError):
        _runner(graph, store, max_retries=0).run({})
    assert len(store) == 0
    assert task.attempts == 1

    # 同一 runner 再跑一次：因为上次没留记录，这次是干净重跑，并且成功。
    report = _runner(graph, store, max_retries=0).run({})
    assert report.all_succeeded()
    assert len(store) == 1
    assert task.keys_seen[0] == task.keys_seen[1]  # 键稳定 ⇒ 幂等判定可靠


def test_failed_task_is_visible_in_error_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    boom = ValueError("底层数据源 500")
    graph.add_task("boom", _FlakyTask("boom", recorder, fail_times=99, exc=boom))
    store = InMemoryExecutionRecordStore()

    with caplog.at_level(logging.ERROR, logger="atlas.runner"):
        with pytest.raises(TaskFailedError) as excinfo:
            _runner(graph, store).run({})

    error = excinfo.value
    assert error.task_name == "boom"
    assert error.attempts == 2
    assert error.last_error is boom  # 原始异常对象被保留，不是字符串
    assert "boom" in str(error)
    assert len(store) == 0
    assert any("失败" in record.getMessage() for record in caplog.records)
    assert any("向上传播" in record.getMessage() for record in caplog.records)


def test_downstream_of_failure_is_blocked_not_executed() -> None:
    """上游失败时下游标记 blocked 且绝不执行——不允许"失败后继续并报告成功"。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("up", _FlakyTask("up", recorder, fail_times=99))
    graph.add_task("mid", _FlakyTask("mid", recorder), depends_on=["up"])
    graph.add_task("down", _FlakyTask("down", recorder), depends_on=["mid"])
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError) as excinfo:
        _runner(graph, store).run({})

    assert recorder.did_run("mid") == 0  # 下游一次都没跑
    assert recorder.did_run("down") == 0
    assert len(store) == 0

    report = excinfo.value.partial_report
    assert report is not None
    assert [r.task_name for r in report.failed] == ["up"]
    assert [r.task_name for r in report.blocked] == ["mid", "down"]
    assert report.result("up").attempts == 2
    assert "500" not in report.result("up").reason  # 不含无关内容
    assert "2 次尝试均未成功" in report.result("up").reason
    assert "绝不执行" in report.result("mid").reason
    # 失败与阻塞都不是成功
    assert report.succeeded == ()
    assert not report.all_succeeded()


def test_failure_partial_report_keeps_earlier_successes() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("ok", _FlakyTask("ok", recorder))
    graph.add_task("bad", _FlakyTask("bad", recorder, fail_times=99), depends_on=["ok"])
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError) as excinfo:
        _runner(graph, store).run({})

    report = excinfo.value.partial_report
    assert report is not None
    assert [r.task_name for r in report.succeeded] == ["ok"]
    assert [r.task_name for r in report.failed] == ["bad"]


def test_only_true_descendants_are_marked_blocked() -> None:
    """与失败无依赖关系的并行分支不得被误报为 blocked。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("ok", _FlakyTask("ok", recorder))
    graph.add_task("bad", _FlakyTask("bad", recorder, fail_times=99))
    graph.add_task("child", _FlakyTask("child", recorder), depends_on=["bad"])
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError) as excinfo:
        _runner(graph, store).run({})

    report = excinfo.value.partial_report
    assert report is not None
    blocked = [r.task_name for r in report.blocked]
    # ok 已成功、bad 已失败、child 依赖 bad 所以被阻塞
    assert blocked == ["child"]
    assert [r.task_name for r in report.succeeded] == ["ok"]
    assert [r.task_name for r in report.failed] == ["bad"]
    assert recorder.did_run("child") == 0


def test_partial_report_excludes_nodes_never_reached() -> None:
    """失败即中止：排在失败节点之后、且与失败无依赖的节点不在报告里。

    语义边界：`blocked` 只表示"因依赖失败而未执行"，不表示"排在后面没轮到"。
    """
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("bad", _FlakyTask("bad", recorder, fail_times=99))
    graph.add_task("independent", _FlakyTask("independent", recorder))
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError) as excinfo:
        _runner(graph, store).run({})

    report = excinfo.value.partial_report
    assert report is not None
    assert [r.task_name for r in report.failed] == ["bad"]
    assert report.blocked == ()
    assert "independent" not in [r.task_name for r in report.results]
    assert recorder.did_run("independent") == 0
    assert len(store) == 0


def test_failure_on_second_node_keeps_first_record() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("ok", _FlakyTask("ok", recorder))
    graph.add_task("bad", _FlakyTask("bad", recorder, fail_times=99), depends_on=["ok"])
    store = InMemoryExecutionRecordStore()

    with pytest.raises(TaskFailedError) as excinfo:
        _runner(graph, store).run({})

    assert excinfo.value.task_name == "bad"
    assert recorder.calls == ["ok", "bad", "bad"]
    # 第一个任务的真实成功记录保留，失败任务没有任何记录
    assert len(store) == 1
    record = store.get(next(iter(store.keys())))
    assert record is not None and record.task_name == "ok"


def test_unregistered_target_is_rejected() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    with pytest.raises(KeyError):
        _runner(graph, InMemoryExecutionRecordStore()).run({}, targets=["ghost"])
    assert recorder.calls == []


def test_targets_run_only_ancestors() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    graph.add_task("b", _FlakyTask("b", recorder), depends_on=["a"])
    graph.add_task("unrelated", _FlakyTask("unrelated", recorder))
    store = InMemoryExecutionRecordStore()

    report = _runner(graph, store).run({}, targets=["b"])

    assert recorder.calls == ["a", "b"]
    assert report.order == ("a", "b")
    assert "unrelated" not in [r.task_name for r in report.results]


def test_store_rejects_overwriting_same_key() -> None:
    """同键重复提交必须失败：键不稳或幂等判定被绕过，都要响亮暴露。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()
    _runner(graph, store).run({})
    key = next(iter(store.keys()))
    record = store.get(key)
    assert record is not None

    with pytest.raises(ContractError):
        store.put(record)


def test_run_task_skips_dependency_checks() -> None:
    """`run_task` 只跑单点，不检查依赖；不存在的节点抛 KeyError。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("solo", _FlakyTask("solo", recorder))
    store = InMemoryExecutionRecordStore()
    runner = _runner(graph, store)

    result = runner.run_task("solo")
    assert result.succeeded()
    assert recorder.calls == ["solo"]

    repeated = runner.run_task("solo")
    assert repeated.status == "skipped"

    with pytest.raises(KeyError):
        runner.run_task("ghost")


def test_report_lookup_for_unknown_task_raises() -> None:
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    report = _runner(graph, InMemoryExecutionRecordStore()).run({})
    with pytest.raises(KeyError):
        report.result("nope")


def test_execution_record_is_immutable() -> None:
    """执行记录是不可变契约记录：不得事后篡改。"""
    recorder = _Recorder()
    graph = TaskGraph()
    graph.add_task("a", _FlakyTask("a", recorder))
    store = InMemoryExecutionRecordStore()
    report = _runner(graph, store).run({})
    output = report.result("a").output
    assert output is not None

    record = ExecutionRecord(
        task_name="a",
        idempotency_key=output.idempotency_key,
        output=output,
        attempt=1,
        duration_seconds=0.5,
    )
    with pytest.raises(Exception):
        record.attempt = 3  # type: ignore[misc]
    with pytest.raises(Exception):
        record.model_copy(update={"attempt": 3})


def test_empty_graph_runs_cleanly() -> None:
    report = _runner(TaskGraph(), InMemoryExecutionRecordStore()).run({})
    assert report.results == ()
    assert report.order == ()
    assert report.all_succeeded()
