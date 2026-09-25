"""T-120 端到端集成测试（SPEC 硬规则 1：完成必须用**跑通的数据流**证明）。

覆盖的验收判据
--------------

1. 完整数据流一次跑通：注册渠道 → 采集（注入假 fetcher，**零网络**）→ 归档 →
   归一化 → feed → 打标，每一阶段都有**可核对的真实产物**。
2. 不变量在系统级成立：`raw_records` 无重复、`confirmed_labels` 只增不改、
   归一化可由 raw 重算且结果一致。
3. 幂等：同输入再跑一次 → 不产生重复 raw / 重复标签，且跳过有明确原因；
   换一个空的执行记录存储再跑一次也不产生重复。
4. 失败可见：fetcher 抛错 → 整条运行失败，报告指出失败节点与被阻塞的下游，
   不留半成品，绝不"部分成功却报 success"。
5. CLI 可运行：`python -m atlas.compose --help` / `plan`（离线）；`run` 默认关闭真实抓取。
6. 只绑 `127.0.0.1` 的 endpoint 标识、一律 `tmp_path`、不打外部网络。

另外守住 SPEC §2.5 的 C8 闭环：feed 的行业维度必须真的从注册表注入
（`industry is not None` 且按行业筛选真的生效）——否则"按行业筛选"会静默失效。
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from atlas.archive import open_archive
from atlas.collect import (
    DomainThrottle,
    FetchResult,
    RobotsCache,
    RobotsFetchResult,
    RetryPolicy,
)
from atlas.compose import (
    ComposeDependencies,
    FileExecutionRecordStore,
    LabelAssignment,
    PipelineError,
    build_pipeline,
)
from atlas.compose.tasks import (
    MAP_SCHEMA,
    CollectionFailedError,
    build_evidence_anchor,
    decode_content,
    recompute_normalized_text,
)
from atlas.contracts import (
    ConfirmedLabel,
    VerificationStatus,
    content_sha256,
    label_id_for,
    raw_id_for,
)
from atlas.feed import ArchiveFeedSource, FeedQuery, run_query
from atlas.labels import open_store as open_labels
from atlas.registry import (
    Channel,
    FetchSpec,
    FetchType,
    Industry,
    RegistryService,
    open_store as open_registry_store,
)
from atlas.runner import InMemoryExecutionRecordStore, TaskFailedError
from atlas.runner.runner import STATUS_BLOCKED, STATUS_FAILED, STATUS_SKIPPED

REPO_ROOT = Path(__file__).resolve().parents[1]

# 窗口固定：同一窗口 = 同一份输入（幂等的前提），且 fetched_at 因此可复现。
WINDOW = "2026-01-01T00:00:00+00:00"
ACTOR = "me"

PLAIN_ENDPOINT = "http://127.0.0.1:9/feed.txt"
HTML_ENDPOINT = "http://127.0.0.1:9/blog.html"
IDLE_ENDPOINT = "http://127.0.0.1:9/idle.txt"

PLAIN_BODY = (
    "Atlas 端到端集成观测 2026-01-01。\n"
    "第一，归档的原始字节必须能按 sha256 核对。\n"
    "第二，归一化文本必须能由 raw 重算。\n"
    "第三，feed 必须能按行业筛选到这一条。"
).encode("utf-8")

HTML_BODY = (
    "<html><head><title>Atlas HTML 渠道</title></head>"
    "<body><h1>端到端观测</h1>"
    "<p>实体测试：A &amp; B &mdash; 结束。</p>"
    "</body></html>"
).encode("utf-8")

EXPECTED_NODES = ("collect", "archive", "normalize", "feed", "label")


# --------------------------------------------------------------------------- #
# 测试替身：只回放内存响应，一条网络都不打
# --------------------------------------------------------------------------- #


class FakeFetcher:
    """按 URL 回放内存里的响应；从不打开 socket。"""

    def __init__(
        self,
        responses: dict[str, bytes] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        bodies = responses if responses is not None else {
            PLAIN_ENDPOINT: PLAIN_BODY,
            HTML_ENDPOINT: HTML_BODY,
        }
        self._responses = {
            url: FetchResult(url=url, status_code=200, content=body)
            for url, body in bodies.items()
        }
        self._error = error
        self.calls: list[str] = []

    def __call__(self, request) -> FetchResult:
        self.calls.append(request.url)
        if self._error is not None:
            raise self._error
        if request.url not in self._responses:
            raise AssertionError(
                f"假 fetcher 没有为 {request.url!r} 准备响应：测试禁止访问真实网络"
            )
        return self._responses[request.url]


def _robots_absent(robots_url: str, *, user_agent: str = "") -> RobotsFetchResult:
    """RFC 9309：404 = 站方明确表示没有 robots.txt → 允许（SPEC §2.12）。"""
    return RobotsFetchResult(url=robots_url, status_code=404, body=b"")


def _dependencies(fetcher) -> ComposeDependencies:
    return ComposeDependencies(
        fetcher=fetcher,
        robots=RobotsCache(_robots_absent, user_agent="Atlas-Test/1.0 (offline)"),
        throttle=DomainThrottle(
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
            global_min_interval=0.0,
        ),
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        retry=RetryPolicy(max_attempts=2, backoff_seconds=0.0),
    )


# --------------------------------------------------------------------------- #
# 前置条件：注册表里配一个行业 + 渠道（走 T-101 的真实 API）
# --------------------------------------------------------------------------- #


def _register_channel(
    root: Path,
    *,
    industry_id: str,
    channel_id: str,
    endpoint: str,
    author: str = "tester",
    enabled: bool = True,
    rate_limit_seconds: int | None = 0,
) -> None:
    store = open_registry_store(root / "atlas.db", author=author)
    try:
        service = RegistryService(store)
        if not any(item.id == industry_id for item in service.list_industries()):
            service.create_industry(
                Industry(id=industry_id, name=industry_id.upper(), enabled=True),
                author=author,
                note="e2e 测试前置",
            )
        service.create_channel(
            Channel(
                id=channel_id,
                industry_id=industry_id,
                type=FetchType.RSS,
                endpoint=endpoint,
                fetch_spec=FetchSpec(type=FetchType.RSS),
                interval_seconds=3600,
                rate_limit_seconds=rate_limit_seconds,
                enabled=enabled,
            ),
            author=author,
            note="e2e 测试前置",
        )
    finally:
        store.close()


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """两个行业、两个渠道：让"按行业筛选 feed"这件事真的有区分度。"""
    root = tmp_path / "store"
    _register_channel(
        root, industry_id="ai", channel_id="chan-plain", endpoint=PLAIN_ENDPOINT
    )
    _register_channel(
        root, industry_id="web", channel_id="chan-html", endpoint=HTML_ENDPOINT
    )
    return root


def _pipeline(store_root: Path, fetcher=None, **kwargs):
    return build_pipeline(
        store_root=store_root,
        actor=ACTOR,
        window=WINDOW,
        dependencies=_dependencies(fetcher or FakeFetcher()),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 判据 1：完整数据流一次跑通，每阶段产物可核对
# --------------------------------------------------------------------------- #


def test_end_to_end_dataflow_produces_verifiable_artifacts(store_root: Path) -> None:
    fetcher = FakeFetcher()
    with _pipeline(
        store_root,
        fetcher,
        label_assignments=(
            LabelAssignment(
                channel_id="chan-plain", label_key="industry", label_value="ai", actor=ACTOR
            ),
        ),
    ) as pipeline:
        report = pipeline.run()

    # 拓扑序真实执行，且没有任何"含糊的成功"
    assert report.all_succeeded(), [(r.task_name, r.status, r.reason) for r in report.results]
    assert set(report.order) == set(EXPECTED_NODES)
    assert report.order.index("collect") < report.order.index("archive")
    assert report.order.index("archive") < report.order.index("normalize")
    assert report.order.index("normalize") < report.order.index("feed")
    assert report.order.index("normalize") < report.order.index("label")
    # 假 fetcher 一共只被打到两次（每个渠道一次），没有重试、更没有真实网络
    assert sorted(fetcher.calls) == sorted([PLAIN_ENDPOINT, HTML_ENDPOINT])

    # ---- 阶段 1：采集（RawRecord 内容寻址）---------------------------------
    collected = report.result("collect").output.artifacts["identity"]["records"]
    by_channel = {item["raw"]["channel_id"]: item for item in collected}
    assert set(by_channel) == {"chan-plain", "chan-html"}

    plain = by_channel["chan-plain"]
    expected_raw_id = raw_id_for("chan-plain", PLAIN_ENDPOINT, content_sha256(PLAIN_BODY))
    assert plain["raw"]["raw_id"] == expected_raw_id
    assert plain["raw"]["content_sha256"] == content_sha256(PLAIN_BODY)
    assert plain["raw"]["byte_length"] == len(PLAIN_BODY)
    assert decode_content(plain["content_b64"]) == PLAIN_BODY

    # ---- 阶段 2：归档（文件字节 + sha256 + 落库三者一致）-------------------
    archive = open_archive(store_root)
    try:
        archived = report.result("archive").output.artifacts
        html_raw_id = raw_id_for("chan-html", HTML_ENDPOINT, content_sha256(HTML_BODY))
        # 渠道顺序由注册表给出（按 id 排序），测试不依赖该顺序，只核对"都归档了"
        assert sorted(item["raw_id"] for item in archived["identity"]["records"]) == sorted(
            [expected_raw_id, html_raw_id]
        )
        # 元数据落库
        stored = archive.get(expected_raw_id)
        assert stored.content_sha256 == content_sha256(PLAIN_BODY)
        assert stored.fetched_at.isoformat() == "2026-01-01T00:00:00+00:00"  # 窗口起点
        # 字节真的在盘上、指纹真的对得上（独立读盘，不信产物自述）
        content_path = archive.blobs.content_path(expected_raw_id)
        on_disk = content_path.read_bytes()
        assert on_disk == PLAIN_BODY
        assert hashlib.sha256(on_disk).hexdigest() == stored.content_sha256
        assert archive.verify() == []

        # ---- 阶段 3：归一化（非空 + 偏移可映射 + 缓存可重建）---------------
        normalized = report.result("normalize").output.artifacts
        plain_facts = next(
            item
            for item in normalized["identity"]["records"]
            if item["raw_id"] == expected_raw_id
        )
        text_file = store_root / "normalized" / f"{expected_raw_id}.txt"
        text = text_file.read_text("utf-8")
        assert text.strip(), "归一化文本必须非空"
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == plain_facts["text_sha256"]
        assert plain_facts["map_schema"] == MAP_SCHEMA
        # 派生块随产物流向下游（DerivedLocator；可重建、会失效，不作人工锚点）
        html_facts = next(
            item for item in normalized["identity"]["records"] if item["raw_id"] == html_raw_id
        )
        assert html_facts["block_count"] >= 1
        assert html_facts["blocks"]
        assert {key for key in html_facts["blocks"][0]} == {
            "block_id",
            "page_number",
            "normalized_start",
            "normalized_end",
        }
        map_payload = json.loads(
            (store_root / "normalized" / f"{expected_raw_id}.map").read_text("utf-8")
        )
        assert map_payload["schema"] == MAP_SCHEMA
        assert map_payload["segments"], "偏移映射必须有段"

        # 由归档字节重算 → 与首次一致（可重建）
        recomputed = recompute_normalized_text(archive, expected_raw_id)
        assert recomputed.text == text
        # 偏移映射真的是原文偏移：普通字符逐字符精确映射（SPEC §2.2）
        raw_text = recomputed.raw_text
        assert raw_text == PLAIN_BODY.decode("utf-8")
        for index in range(len(recomputed.text)):
            assert recomputed.raw_text[recomputed.to_raw_offset(index)] == recomputed.text[index]
        # quote → 确定性锚点（T-104 ↔ T-002 的接缝）
        status, anchor, _derived = build_evidence_anchor(
            archive, expected_raw_id, "归一化文本必须能由 raw 重算"
        )
        assert status is VerificationStatus.VERIFIED
        assert anchor is not None
        assert (
            raw_text[anchor.char_start : anchor.char_end]
            == "归一化文本必须能由 raw 重算"
        )
        assert anchor.raw_id == expected_raw_id
        assert anchor.raw_sha256 == stored.content_sha256
    finally:
        archive.close()

    # ---- 阶段 4：feed（只读投影，查得到 + 行业已接上）----------------------
    feed_identity = report.result("feed").output.artifacts["identity"]
    feed_by_id = {item["raw_id"]: item for item in feed_identity["items"]}
    assert expected_raw_id in feed_by_id
    assert feed_by_id[expected_raw_id]["industry"] == "ai"
    assert feed_by_id[expected_raw_id]["channel_id"] == "chan-plain"
    assert feed_by_id[html_raw_id]["industry"] == "web"

    # ---- 阶段 5：打标（Confirmed 落库，内容寻址）---------------------------
    label_identity = report.result("label").output.artifacts["identity"]
    expected_label_id = label_id_for(expected_raw_id, "industry", "ai", ACTOR)
    assert [item["label_id"] for item in label_identity["labels"]] == [expected_label_id]
    with open_labels(store_root / "atlas.db") as labels:
        assert labels.latest_value(expected_raw_id, "industry") == "ai"
        assert labels.count() == 1
        assert len(labels.all_for(expected_raw_id)) == 1


# --------------------------------------------------------------------------- #
# C8 闭环：feed 的行业维度必须来自注册表，且筛选真的生效
# --------------------------------------------------------------------------- #


def test_feed_industry_filtering_uses_registry_mapping(store_root: Path) -> None:
    with _pipeline(store_root) as pipeline:
        report = pipeline.run()

    channels = {channel.id: channel.industry_id for channel in pipeline.registrations()}
    assert channels == {"chan-plain": "ai", "chan-html": "web"}

    archive = open_archive(store_root)
    try:
        source = ArchiveFeedSource(archive, industry_of=channels)
        everything = run_query(source, FeedQuery())
        assert everything.total == 2
        assert {item.industry for item in everything.items} == {"ai", "web"}

        only_ai = run_query(source, FeedQuery(industries=("ai",)))
        assert [item.channel_id for item in only_ai.items] == ["chan-plain"]
        assert only_ai.items[0].raw_id == raw_id_for(
            "chan-plain", PLAIN_ENDPOINT, content_sha256(PLAIN_BODY)
        )

        only_web = run_query(source, FeedQuery(industries=("web",)))
        assert [item.channel_id for item in only_web.items] == ["chan-html"]

        # 未配置的行业 → 空结果（不是"全都返回"）
        assert run_query(source, FeedQuery(industries=("never-configured",))).total == 0
    finally:
        archive.close()
    assert report.all_succeeded()


# --------------------------------------------------------------------------- #
# 判据 2：不变量在系统级成立
# --------------------------------------------------------------------------- #


def test_system_invariants_raw_unique_confirmed_append_only_normalize_rebuildable(
    store_root: Path,
) -> None:
    with _pipeline(
        store_root,
        label_assignments=(
            LabelAssignment(
                channel_id="chan-plain", label_key="valid", label_value="yes", actor=ACTOR
            ),
        ),
    ) as pipeline:
        pipeline.run()

    raw_id = raw_id_for("chan-plain", PLAIN_ENDPOINT, content_sha256(PLAIN_BODY))
    archive = open_archive(store_root)
    labels = open_labels(store_root / "atlas.db")
    try:
        # Raw：无重复、无残缺
        ids = archive.all_raw_ids()
        assert len(ids) == len(set(ids)) == 2
        duplicates = (
            archive.records.connection.execute(
                "SELECT raw_id, COUNT(*) AS n FROM raw_records GROUP BY raw_id HAVING n > 1"
            ).fetchall()
        )
        assert duplicates == []
        assert archive.verify() == []
        assert archive.records.count() == 2

        # Confirmed：只增不改（重复提交幂等 + 触发器禁止 UPDATE / DELETE）
        before = labels.count()
        assert before == 1
        again = labels.add(
            ConfirmedLabel.human(
                raw_id=raw_id, label_key="valid", label_value="yes", actor=ACTOR
            )
        )
        assert labels.count() == before, "同一判断重复提交不得产生新行"
        assert again.label_id == label_id_for(raw_id, "valid", "yes", ACTOR)

        connection = labels.labels.connection
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE confirmed_labels SET label_value = 'no'")
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM confirmed_labels")
        connection.rollback()
        assert labels.count() == before
        assert labels.latest_value(raw_id, "valid") == "yes"

        # 归一化可重建：重算结果与首次逐字符一致，且不影响 Confirmed
        first = recompute_normalized_text(archive, raw_id)
        second = recompute_normalized_text(archive, raw_id)
        assert first.text == second.text
        assert first.text == (store_root / "normalized" / f"{raw_id}.txt").read_text("utf-8")
        assert [
            (segment.norm_start, segment.raw_start, segment.length, segment.raw_length)
            for segment in first.segments
        ] == [
            (segment.norm_start, segment.raw_start, segment.length, segment.raw_length)
            for segment in second.segments
        ]
        assert labels.count() == before
    finally:
        labels.close()
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 3：幂等
# --------------------------------------------------------------------------- #


def test_second_run_skips_every_node_with_explicit_reasons(store_root: Path) -> None:
    assignments = (
        LabelAssignment(
            channel_id="chan-plain", label_key="industry", label_value="ai", actor=ACTOR
        ),
    )
    with _pipeline(store_root, label_assignments=assignments) as pipeline:
        first = pipeline.run()
        second = pipeline.run()

    assert first.all_succeeded()
    # 整条流水线再跑一次 → 全部幂等跳过，且每条跳过都说明了原因
    assert second.succeeded == ()
    assert second.failed == ()
    assert second.blocked == ()
    assert len(second.skipped) == len(EXPECTED_NODES)
    for result in second.skipped:
        assert result.status == STATUS_SKIPPED
        assert "幂等跳过" in result.reason and "输入与配置均未变化" in result.reason
        assert result.idempotency_key

    # 不产生重复 raw / 重复标签
    raw_id = raw_id_for("chan-plain", PLAIN_ENDPOINT, content_sha256(PLAIN_BODY))
    archive = open_archive(store_root)
    labels = open_labels(store_root / "atlas.db")
    try:
        assert archive.all_raw_ids() == sorted(
            [
                raw_id,
                raw_id_for("chan-html", HTML_ENDPOINT, content_sha256(HTML_BODY)),
            ]
        )
        assert labels.count() == 1
        assert len(labels.all_for(raw_id)) == 1
    finally:
        labels.close()
        archive.close()


def test_rerun_with_fresh_execution_store_does_not_duplicate(store_root: Path) -> None:
    """执行记录被换掉（新进程 / 新存储）时，重跑也不得产生重复。"""
    assignments = (
        LabelAssignment(
            channel_id="chan-plain", label_key="industry", label_value="ai", actor=ACTOR
        ),
    )
    with _pipeline(store_root, label_assignments=assignments) as pipeline:
        first = pipeline.run()
    assert first.all_succeeded()

    with _pipeline(
        store_root,
        label_assignments=assignments,
        execution_store=InMemoryExecutionRecordStore(),
    ) as pipeline:
        # 全新的执行记录存储：上游不再有记录，于是真的重新采集 / 重新归档 / 重新打标
        second = pipeline.run()
    assert second.all_succeeded()
    assert len(second.succeeded) == len(EXPECTED_NODES)

    raw_id = raw_id_for("chan-plain", PLAIN_ENDPOINT, content_sha256(PLAIN_BODY))
    archive = open_archive(store_root)
    labels = open_labels(store_root / "atlas.db")
    try:
        assert archive.all_raw_ids() == sorted(
            [
                raw_id,
                raw_id_for("chan-html", HTML_ENDPOINT, content_sha256(HTML_BODY)),
            ]
        )
        assert archive.verify() == []
        assert labels.count() == 1
        assert len(labels.all_for(raw_id)) == 1
    finally:
        labels.close()
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 4：失败可见（绝不"部分成功却报 success"）
# --------------------------------------------------------------------------- #


def test_channel_failure_fails_the_whole_run_and_names_the_failing_node(
    store_root: Path,
) -> None:
    fetcher = FakeFetcher(error=RuntimeError("fake network down（测试注入的必然失败）"))
    with pytest.raises(TaskFailedError) as caught:
        with _pipeline(store_root, fetcher) as pipeline:
            pipeline.run()

    error = caught.value
    assert error.task_name == "collect", "报告必须指出失败节点"
    assert isinstance(error.last_error, CollectionFailedError)
    assert "chan-plain" in str(error.last_error)
    assert "fake network down" in str(error.last_error)

    partial = error.partial_report
    assert partial is not None
    assert partial.result("collect").status == STATUS_FAILED
    assert partial.succeeded == (), "失败运行里不得有任何'成功'节点"
    assert {item.task_name for item in partial.blocked} == {
        "archive",
        "normalize",
        "feed",
        "label",
    }
    for item in partial.blocked:
        assert item.status == STATUS_BLOCKED
        assert "依赖未满足" in item.reason and "collect" in item.reason

    # 失败不留半成品：没有 raw、没有标签、没有归一化缓存
    archive = open_archive(store_root)
    labels = open_labels(store_root / "atlas.db")
    try:
        assert archive.all_raw_ids() == []
        assert labels.count() == 0
    finally:
        labels.close()
        archive.close()
    assert not (store_root / "normalized").exists() or not list(
        (store_root / "normalized").glob("*.txt")
    )

    # 失败也**不写执行记录**：重试面对的是干净状态
    service_store = FileExecutionRecordStore(store_root / "runs" / "executions.json")
    assert service_store.keys() == ()


def test_label_assignment_for_channel_without_documents_fails_loudly(
    store_root: Path,
) -> None:
    """人工判断指向一个本轮没有文档的渠道：响亮失败，绝不静默跳过。"""
    _register_channel(
        store_root,
        industry_id="ai",
        channel_id="chan-idle",
        endpoint=IDLE_ENDPOINT,
        enabled=False,
    )
    with pytest.raises(PipelineError) as caught:
        with _pipeline(
            store_root,
            label_assignments=(
                LabelAssignment(
                    channel_id="chan-idle",
                    label_key="industry",
                    label_value="ai",
                    actor=ACTOR,
                ),
            ),
        ) as pipeline:
            pipeline.run()
    assert "chan-idle" in str(caught.value)


def test_empty_channel_failure_reports_structured_failure(store_root: Path) -> None:
    """采集失败即使是"单渠道"也必须带结构化原因，而不是一句"失败"。"""
    fetcher = FakeFetcher(error=OSError("connection refused"))
    with pytest.raises(TaskFailedError) as caught:
        with _pipeline(store_root, fetcher) as pipeline:
            pipeline.run()
    assert caught.value.task_name == "collect"
    failures = caught.value.last_error.failures
    assert {item["channel_id"] for item in failures} == {"chan-plain", "chan-html"}
    for item in failures:
        assert item["kind"] == "connection"
        assert item["attempts"] == 2  # max_attempts=2
        assert item["reason"]


def test_run_without_channels_fails_loudly(tmp_path: Path) -> None:
    with _pipeline(tmp_path / "empty-store") as pipeline:
        with pytest.raises(PipelineError):
            pipeline.run()


# --------------------------------------------------------------------------- #
# 默认装配 = 真实实现：只对 127.0.0.1 的本地服务跑，证明合规层没被绕过
# --------------------------------------------------------------------------- #


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    """本地回环测试服务（**只绑 127.0.0.1**，不打任何外部网络）。"""

    routes: dict[str, tuple[int, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        status, body = self.routes.get(self.path, (404, b""))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        return None


@contextlib.contextmanager
def _local_server(routes: dict[str, tuple[int, bytes]]):
    handler = type("_Handler", (_LocalHandler,), {"routes": routes})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_assembly_uses_real_fetcher_and_enforces_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不注入任何替身：`ComposeDependencies.real()` 必须真的抓取并真的限速。

    两个渠道指向**同一个域**（127.0.0.1），SPEC §2.12 的同域最小间隔（默认 1s）因此
    必须体现为约 1 秒的等待 —— 这是"合规层没有被组合根绕过"的行为证据。
    """
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    body_a, body_b = b"Atlas live-mode probe A.", b"Atlas live-mode probe B."
    root = tmp_path / "store"
    allow_all = b"User-agent: *\nAllow: /\n"
    with _local_server(
        {
            "/robots.txt": (200, allow_all),
            "/a.txt": (200, body_a),
            "/b.txt": (200, body_b),
        }
    ) as base:
        _register_channel(
            root,
            industry_id="live",
            channel_id="live-a",
            endpoint=f"{base}/a.txt",
            rate_limit_seconds=None,
        )
        _register_channel(
            root,
            industry_id="live",
            channel_id="live-b",
            endpoint=f"{base}/b.txt",
            rate_limit_seconds=None,
        )
        started = time.monotonic()
        with build_pipeline(store_root=root, actor=ACTOR, window=WINDOW) as pipeline:
            report = pipeline.run()
        elapsed = time.monotonic() - started

    assert report.all_succeeded()
    assert elapsed >= 0.9, f"同域第二个请求必须被限速（实际耗时 {elapsed:.2f}s）"

    archive = open_archive(root)
    try:
        live_a = raw_id_for("live-a", f"{base}/a.txt", content_sha256(body_a))
        live_b = raw_id_for("live-b", f"{base}/b.txt", content_sha256(body_b))
        assert archive.all_raw_ids() == sorted([live_a, live_b])
        # 真实抓到的字节逐字节落盘（UrllibFetcher 真的打了 HTTP）
        assert archive.blobs.content_path(live_a).read_bytes() == body_a
        assert archive.get(live_a).http_status == 200
        assert archive.verify() == []
    finally:
        archive.close()


def test_default_assembly_cannot_bypass_robots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """robots 明确禁止时，组合根**不能**绕过合规层：采集失败且归档为空。"""
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    root = tmp_path / "store"
    rules = b"User-agent: *\nDisallow: /\n"
    with _local_server({"/robots.txt": (200, rules), "/secret.txt": (200, b"nope")}) as base:
        endpoint = f"{base}/secret.txt"
        _register_channel(
            root, industry_id="live", channel_id="live-blocked", endpoint=endpoint
        )
        with pytest.raises(TaskFailedError) as caught:
            with build_pipeline(
                store_root=root, actor=ACTOR, window=WINDOW, max_retries=0
            ) as pipeline:
                pipeline.run()

    error = caught.value
    assert error.task_name == "collect"
    assert isinstance(error.last_error, CollectionFailedError)
    failures = error.last_error.failures
    assert [item["kind"] for item in failures] == ["robots_denied"]
    assert failures[0]["robots_url"].endswith("/robots.txt")

    archive = open_archive(root)
    try:
        assert archive.all_raw_ids() == []
    finally:
        archive.close()


def test_real_robots_404_is_treated_as_absent_per_spec_2_12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §2.12：robots.txt 404 = 站方明确表示没有规则 → 应当允许抓取。"""
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)

    root = tmp_path / "store"
    # 未知路径一律 404（含 /robots.txt）：正是"站点没有 robots.txt"的情形
    with _local_server({"/c.txt": (200, b"Atlas robots-absent probe.")}) as base:
        _register_channel(
            root, industry_id="live", channel_id="live-404", endpoint=f"{base}/c.txt"
        )
        with build_pipeline(
            store_root=root, actor=ACTOR, window=WINDOW, max_retries=0
        ) as pipeline:
            report = pipeline.run()

    assert report.all_succeeded()
    archive = open_archive(root)
    try:
        assert archive.get(
            raw_id_for("live-404", f"{base}/c.txt", content_sha256(b"Atlas robots-absent probe."))
        ).http_status == 200
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 判据 5：CLI 可运行（离线部分）
# --------------------------------------------------------------------------- #


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.pop("ATLAS_LIVE", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "atlas.compose", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=environment,
        timeout=120,
    )


def test_cli_help_runs_offline() -> None:
    proc = _run_cli("--help")
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "usage" in combined.lower()
    for command in ("plan", "run", "register"):
        assert command in combined


def test_cli_plan_prints_dag_offline(tmp_path: Path) -> None:
    proc = _run_cli("plan", "--store-root", str(tmp_path / "store"))
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert [node["name"] for node in plan["nodes"]] == list(EXPECTED_NODES)
    dependencies = {node["name"]: node["depends_on"] for node in plan["nodes"]}
    assert dependencies["collect"] == []
    assert dependencies["archive"] == ["collect"]
    assert dependencies["normalize"] == ["archive"]
    assert dependencies["feed"] == ["normalize"]
    assert dependencies["label"] == ["normalize"]
    assert plan["channels"] == []


def test_cli_register_then_plan_lists_channel(tmp_path: Path) -> None:
    root = tmp_path / "store"
    proc = _run_cli(
        "register",
        "--store-root",
        str(root),
        "--industry-id",
        "ai",
        "--channel-id",
        "chan-cli",
        "--endpoint",
        PLAIN_ENDPOINT,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)[0]["id"] == "chan-cli"

    proc = _run_cli("plan", "--store-root", str(root))
    assert proc.returncode == 0, proc.stderr
    assert [item["id"] for item in json.loads(proc.stdout)["channels"]] == ["chan-cli"]


def test_cli_run_requires_explicit_live_opt_in(tmp_path: Path) -> None:
    """默认关闭真实抓取：没有 ATLAS_LIVE=1 就以退出码 2 明确拒绝。"""
    proc = _run_cli("run", "--store-root", str(tmp_path / "store"))
    assert proc.returncode == 2
    assert "ATLAS_LIVE" in proc.stderr
    assert not (tmp_path / "store").exists(), "被拒绝的运行不得留下任何存储痕迹"
