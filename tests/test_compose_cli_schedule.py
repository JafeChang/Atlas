"""T-207 组合根 / CLI 接线：due-only 真的改变"注册了哪些渠道"，且三态都不含糊。

判据（本模块即判据）
--------------------

| # | 判据 | 用例 |
|---|---|---|
| B1 | `due_only=True` 时**只有到期渠道**被注册（未到期的渠道一次都不被请求） | `test_due_only_registers_and_collects_only_the_due_channel` |
| B2 | 结论由 `interval_seconds` 决定：两个渠道只差 interval 就一个采、一个不采 | 同上（`chan-fresh-7200` 未被请求） |
| B3 | 默认（`due_only=False`）行为与 T-120 **完全一致** | 同上的默认分支、`test_due_only_off_still_registers_every_enabled_channel` |
| B4 | **没有到期的渠道 = 正常**：`run(due_only=True)` 抛 `NothingDueError` 而**不是** `PipelineError`，且不留痕 | `test_no_channel_due_is_not_an_error_and_leaves_no_trace` |
| B5 | **一个可采集渠道都没有 = 配置问题**：仍抛 `PipelineError`（与 B4 显式区分） | 同上（活对照） |
| B6 | `plan` 每个渠道都报 `due` / `last_collected_at`；`plan --due-only` 另给完整 `schedule` | `test_plan_reports_due_and_last_collected_per_channel` |
| B7 | CLI：`run --due-only` 在没有到期渠道时**退出码 0** 且明说"本轮不采集"；`plan --due-only` 纯 JSON 退出码 0 | `test_cli_due_only_run_with_nothing_due_exits_zero`、`test_cli_plan_due_only` |
| B8 | CLI：`run --due-only` 有渠道可采、但**全部未到期**时同样退出码 0（cron 的正常轮次） | `test_cli_due_only_run_with_all_channels_fresh_exits_zero` |
| B9 | **调度 ≠ 真的抓了**：同窗口内到期渠道仍会被幂等跳过（有意保留的交互） | `test_scheduled_channel_can_still_be_skipped_by_window_idempotency` |
| B10 | 空注册表：**只读查询**（`plan --due-only`）如实说"没有可采集渠道"且退出码 0；**要采集的动作**（`run --due-only`）按配置问题给退出码 1 | `test_empty_registry_with_no_database_still_plans`、`test_cli_no_channels_is_still_a_configuration_error` |

B4 / B5 是一条**否定性断言与其活对照**：同一个 `run()`，渠道集合不同导致异常类型不同 ——
`NothingDueError`（正常，退出码 0）vs `PipelineError`（配置问题，退出码 1）。两者都断言，
因此不会把"异常类型不对"误当成"区分成功"（CLAUDE.md 硬规则 4）。

全部用例**离线**：假 fetcher 只回放内存响应，`DomainThrottle` 注入 0 间隔假时钟。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from atlas.archive import open_archive
from atlas.collect import (
    DomainThrottle,
    FetchResult,
    RobotsCache,
    RobotsFetchResult,
    RetryPolicy,
)
from atlas.compose import ComposeDependencies, NothingDueError, PipelineError, build_pipeline
from atlas.contracts import RawRecord, content_sha256, raw_id_for
from atlas.registry import (
    Channel,
    FetchSpec,
    FetchType,
    Industry,
    RegistryService,
    open_store as open_registry_store,
)
from tests._compose_cli import run_cli

UTC = timezone.utc

#: 判定时刻（注入，可复现）。两个渠道的 `last_collected_at` 相对它取。
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
WINDOW = "2026-09-26T12:00:00+00:00"

ACTOR = "t207"

PLAIN_ENDPOINT = "http://127.0.0.1:9/t207-plain.txt"
HTML_ENDPOINT = "http://127.0.0.1:9/t207-html.html"
PLAIN_BODY = b"T-207 due-only probe: plain body."
HTML_BODY = b"<html><body><p>T-207 due-only probe: html body.</p></body></html>"


# --------------------------------------------------------------------------- #
# 离线替身（与 tests/test_compose_pipeline.py 同一手法）
# --------------------------------------------------------------------------- #


class FakeFetcher:
    """只回放内存响应；没有为某个 URL 准备响应就**抛错**（漏抓必须可见）。"""

    def __init__(self, responses: Optional[Dict[str, bytes]] = None) -> None:
        bodies = (
            responses
            if responses is not None
            else {PLAIN_ENDPOINT: PLAIN_BODY, HTML_ENDPOINT: HTML_BODY}
        )
        self._responses = {
            url: FetchResult(url=url, status_code=200, content=body)
            for url, body in bodies.items()
        }
        self.calls: list[str] = []

    def __call__(self, request) -> FetchResult:
        self.calls.append(request.url)
        if request.url not in self._responses:
            raise AssertionError(f"假 fetcher 没有为 {request.url!r} 准备响应（禁止真实网络）")
        return self._responses[request.url]


def _robots_absent(robots_url: str, *, user_agent: str = "") -> RobotsFetchResult:
    return RobotsFetchResult(url=robots_url, status_code=404, body=b"")


def _dependencies(fetcher) -> ComposeDependencies:
    return ComposeDependencies(
        fetcher=fetcher,
        robots=RobotsCache(_robots_absent, user_agent="Atlas-Test/1.0 (offline)"),
        throttle=DomainThrottle(
            clock=lambda: 0.0, sleeper=lambda _seconds: None, global_min_interval=0.0
        ),
        sleeper=lambda _seconds: None,
        clock=lambda: 0.0,
        retry=RetryPolicy(max_attempts=2, backoff_seconds=0.0),
    )


def _register(
    root: Path,
    *,
    channel_id: str,
    endpoint: str,
    interval_seconds: int,
    industry_id: str = "ai",
) -> None:
    store = open_registry_store(root / "atlas.db", author=ACTOR)
    try:
        service = RegistryService(store)
        if not any(item.id == industry_id for item in service.list_industries()):
            service.create_industry(
                Industry(id=industry_id, name=industry_id.upper(), enabled=True),
                author=ACTOR,
                note="t207 前置",
            )
        service.create_channel(
            Channel(
                id=channel_id,
                industry_id=industry_id,
                type=FetchType.RSS,
                endpoint=endpoint,
                fetch_spec=FetchSpec(type=FetchType.RSS),
                interval_seconds=interval_seconds,
                rate_limit_seconds=0,
                enabled=True,
            ),
            author=ACTOR,
            note="t207 前置",
        )
    finally:
        store.close()


def _seed_state(root: Path, entries) -> None:
    """把 `(channel_id, fetched_at)` 写进真实归档（走 T-103 的真实写路径）。"""
    archive = open_archive(root)
    try:
        for index, (channel_id, fetched_at) in enumerate(entries):
            content = f"t207 seed {channel_id} #{index}".encode("utf-8")
            archive.put(
                RawRecord.create(
                    channel_id=channel_id,
                    endpoint=f"http://127.0.0.1:9/seed/{channel_id}/{index}",
                    content=content,
                    fetched_at=fetched_at,
                    http_status=200,
                ),
                content,
            )
    finally:
        archive.close()


def _pipeline(root: Path, fetcher=None, **kwargs):
    return build_pipeline(
        store_root=root,
        actor=ACTOR,
        window=WINDOW,
        dependencies=_dependencies(fetcher or FakeFetcher()),
        **kwargs,
    )


def _two_channels(root: Path) -> None:
    """`chan-due`（1800s，45 分钟前采过 ⇒ 到期）与 `chan-fresh`（7200s，45 分钟前采过 ⇒ 未到期）。"""
    _register(
        root, channel_id="chan-due", endpoint=PLAIN_ENDPOINT, interval_seconds=1800
    )
    _register(
        root, channel_id="chan-fresh", endpoint=HTML_ENDPOINT, interval_seconds=7200
    )
    last = NOW - timedelta(minutes=45)
    _seed_state(root, [("chan-due", last), ("chan-fresh", last)])


# --------------------------------------------------------------------------- #
# B1 / B2 / B3：due-only 只注册到期的渠道；默认行为不变
# --------------------------------------------------------------------------- #


def test_due_only_registers_and_collects_only_the_due_channel(tmp_path: Path) -> None:
    """只看 `interval_seconds` 的差异，就决定"谁被采、谁完全没被碰"。"""
    root = tmp_path / "store"
    _two_channels(root)
    fetcher = FakeFetcher()

    with _pipeline(root, fetcher) as pipeline:
        decision = pipeline.due_decision(now=NOW)
        assert decision.due_ids == ("chan-due",), decision.as_dict()
        assert decision.not_due_ids == ("chan-fresh",)
        report = pipeline.run(due_only=True, now=NOW)

    assert report.all_succeeded()
    observed = report.result("collect").output.artifacts["observed"]
    assert [item["channel_id"] for item in observed["per_channel"]] == ["chan-due"]
    assert observed["failures"] == []
    # 未到期的渠道**一次都没有被请求**（它没有假响应，被请求就会炸成失败）
    assert fetcher.calls == [PLAIN_ENDPOINT], (
        f"只有到期渠道该被请求，实际请求了 {fetcher.calls}"
    )

    # 活对照（B3）：默认 due_only=False 时两个渠道都被注册、都被请求
    fetcher_default = FakeFetcher()
    with _pipeline(root, fetcher_default) as pipeline:
        default_report = pipeline.run()
    assert default_report.all_succeeded()
    assert sorted(fetcher_default.calls) == sorted([PLAIN_ENDPOINT, HTML_ENDPOINT])
    assert {
        item["channel_id"]
        for item in default_report.result("collect").output.artifacts["observed"]["per_channel"]
    } == {"chan-due", "chan-fresh"}


def test_due_only_off_still_registers_every_enabled_channel(tmp_path: Path) -> None:
    """默认模式与 T-120 一致：全部启用渠道都进 `collect` 的输入快照。"""
    root = tmp_path / "store"
    _two_channels(root)
    with _pipeline(root) as pipeline:
        report = pipeline.run()
    assert report.all_succeeded()
    assert len(report.result("collect").output.artifacts["observed"]["per_channel"]) == 2


def test_nothing_due_error_carries_the_full_decision(tmp_path: Path) -> None:
    """`NothingDueError` 带上完整判定，因此调用方能打印"谁未到期、下次何时"。"""
    root = tmp_path / "store"
    _register(root, channel_id="chan-a", endpoint=PLAIN_ENDPOINT, interval_seconds=3600)
    _seed_state(root, [("chan-a", NOW)])

    with _pipeline(root) as pipeline:
        with pytest.raises(NothingDueError) as caught:
            pipeline.run(due_only=True, now=NOW)
        error = caught.value
        assert error.decision.is_idle is True
        assert error.decision.due_ids == ()
        assert error.decision.enabled_channels == 1
        assert error.decision.schedule_of("chan-a").next_due_at == NOW + timedelta(hours=1)
        # 判定与执行是同一条路径：同一个 now 下 due_decision() 也说"空闲"
        assert pipeline.due_decision(now=NOW).is_idle is True


# --------------------------------------------------------------------------- #
# B4 / B5：三态显式区分（含活对照）
# --------------------------------------------------------------------------- #


def test_no_channel_due_is_not_an_error_and_leaves_no_trace(tmp_path: Path) -> None:
    """**没有到期的渠道** ⇒ `NothingDueError`（正常）；**没有渠道** ⇒ `PipelineError`（配置）。"""
    fresh = tmp_path / "store-fresh"
    _register(fresh, channel_id="chan-fresh", endpoint=PLAIN_ENDPOINT, interval_seconds=3600)
    _seed_state(fresh, [("chan-fresh", NOW)])

    with _pipeline(fresh) as pipeline:
        with pytest.raises(NothingDueError) as caught:
            pipeline.run(due_only=True, now=NOW)
        assert type(caught.value) is NothingDueError, (
            "必须是 NothingDueError 本身（CLI 先按它分支成退出码 0）；"
            "裸 PipelineError 表示配置问题（退出码 1），两者不能混"
        )
        assert "正常状态" in str(caught.value)
        # 什么都没做：没有采集、没有归档、没有执行记录
        assert len(pipeline._store) == 0  # type: ignore[attr-defined]
    assert not (fresh / "runs" / "executions.json").is_file()

    # 活对照：**没有可采集渠道**是另一回事 —— 仍然是配置问题
    empty = tmp_path / "store-empty"
    with _pipeline(empty) as pipeline:
        with pytest.raises(PipelineError) as empty_caught:
            pipeline.run(due_only=True, now=NOW)
        assert type(empty_caught.value) is PipelineError, (
            f"空注册表必须抛裸 PipelineError，实际 {type(empty_caught.value).__name__}"
        )
        assert not isinstance(empty_caught.value, NothingDueError)
        assert "没有可采集的渠道" in str(empty_caught.value)
        # 默认模式（due_only=False）对空注册表的语义与 T-120 完全一致
        with pytest.raises(PipelineError):
            pipeline.run()


def test_empty_registry_with_no_database_still_plans(tmp_path: Path) -> None:
    """**活对照**：`due_decision()` 对"一个渠道都没有"返回**空判定**，不假装读到状态。

    空注册表不需要读 `atlas.db`（没有渠道要判定），因此"库不存在 / 库损坏"在这里
    不该被当成错误 —— 但也**不会**伪造出任何到期渠道。SQL 层面对坏数据的响亮失败
    由 `tests/test_schedule_core.py` 直接打在状态源上（那里有正常的活对照）。
    """
    missing = tmp_path / "no-such-store"
    with _pipeline(missing) as pipeline:
        decision = pipeline.due_decision(now=NOW)
        assert decision.enabled_channels == 0
        assert decision.due_ids == ()
        assert decision.is_idle is True
        plan = pipeline.plan(due_only=True, now=NOW)
    assert plan["channels"] == []
    assert plan["schedule"]["enabled_channels"] == 0
    assert plan["schedule"]["idle"] is True


# --------------------------------------------------------------------------- #
# B6：plan 的 per-channel 到期报告
# --------------------------------------------------------------------------- #


def test_plan_reports_due_and_last_collected_per_channel(tmp_path: Path) -> None:
    """`plan` 必须逐渠道给出 `due` 与 `last_collected_at`（不是让人自己算 interval）。"""
    root = tmp_path / "store"
    _two_channels(root)
    with _pipeline(root) as pipeline:
        plan = pipeline.plan(now=NOW)

    by_id = {item["id"]: item for item in plan["channels"]}
    assert set(by_id) == {"chan-due", "chan-fresh"}
    last = (NOW - timedelta(minutes=45)).isoformat()
    assert by_id["chan-due"]["due"] is True
    assert by_id["chan-fresh"]["due"] is False
    assert by_id["chan-due"]["last_collected_at"] == last
    assert by_id["chan-fresh"]["last_collected_at"] == last
    assert by_id["chan-due"]["interval_seconds"] == 1800
    assert by_id["chan-fresh"]["interval_seconds"] == 7200
    assert "schedule" not in plan, "默认 plan 不额外加块（保持既有消费方不变）"

    with _pipeline(root) as pipeline:
        detailed = pipeline.plan(due_only=True, now=NOW)
    assert detailed["schedule"]["due_channels"] == ["chan-due"]
    assert detailed["schedule"]["idle"] is False
    assert detailed["schedule"]["now"] == NOW.isoformat()
    per_channel = {item["id"]: item for item in detailed["schedule"]["per_channel"]}
    assert per_channel["chan-fresh"]["next_due_at"] == (
        (NOW - timedelta(minutes=45)) + timedelta(seconds=7200)
    ).isoformat()


# --------------------------------------------------------------------------- #
# B7 / B8：CLI 的退出码
# --------------------------------------------------------------------------- #


def _register_via_cli(root: Path, *, channel_id: str, interval: int) -> None:
    """用 CLI 注册渠道；行业已存在时忽略"已存在"错误（只跑一次 `register` 的行业）。"""
    proc = run_cli(
        "register",
        "--store-root",
        str(root),
        "--industry-id",
        "ai",
        "--channel-id",
        channel_id,
        "--endpoint",
        f"http://127.0.0.1:9/{channel_id}.txt",
        "--interval-seconds",
        str(interval),
    )
    if proc.returncode != 0:
        assert "行业 id 已存在" in proc.stderr, proc.stderr
    assert (root / "atlas.db").is_file()


def test_cli_plan_due_only(tmp_path: Path) -> None:
    """`plan --due-only`：stdout 是纯 JSON（含 per-channel 到期），退出码 0。"""
    root = tmp_path / "store"
    _register_via_cli(root, channel_id="chan-never", interval=1800)

    proc = run_cli(
        "plan", "--store-root", str(root), "--due-only", "--now", NOW.isoformat()
    )
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    assert [item["id"] for item in plan["channels"]] == ["chan-never"]
    assert plan["channels"][0]["due"] is True
    assert plan["channels"][0]["last_collected_at"] is None
    assert plan["schedule"]["due_channels"] == ["chan-never"]
    assert "调度判定" in proc.stderr, "人读的判定走 stderr，stdout 保持纯 JSON"


def test_cli_due_only_run_with_nothing_due_exits_zero(tmp_path: Path) -> None:
    """**关键边界**：due-only 且没有渠道可采 ⇒ 退出码 0，并明说"本轮不采集"。"""
    root = tmp_path / "store"
    _register_via_cli(root, channel_id="chan-fresh", interval=7200)
    _seed_state(root, [("chan-fresh", NOW - timedelta(minutes=5))])

    proc = run_cli(
        "run",
        "--store-root",
        str(root),
        "--due-only",
        "--now",
        NOW.isoformat(),
        env={"ATLAS_LIVE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    combined = proc.stdout + proc.stderr
    assert "没有到期的渠道" in combined, combined
    assert "正常状态" in combined, combined
    assert "chan-fresh" in combined, "必须说明是哪个渠道未到期、下次何时"
    assert "执行汇总" not in proc.stdout, "不得渲染一份'看起来跑过'的空报告"
    assert not (root / "runs" / "executions.json").is_file()


def test_cli_due_only_run_with_all_channels_fresh_exits_zero(tmp_path: Path) -> None:
    """有渠道可采、但**全部未到期**：同样是退出码 0（cron 的正常轮次）。"""
    root = tmp_path / "store"
    _register_via_cli(root, channel_id="chan-a", interval=3600)
    _register_via_cli(root, channel_id="chan-b", interval=1800)
    _seed_state(root, [("chan-a", NOW), ("chan-b", NOW)])

    proc = run_cli(
        "run",
        "--store-root",
        str(root),
        "--due-only",
        "--now",
        NOW.isoformat(),
        env={"ATLAS_LIVE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "没有到期的渠道" in proc.stdout
    assert "到期渠道（0）" in proc.stdout


def test_cli_due_only_can_not_bypass_live_opt_in(tmp_path: Path) -> None:
    """`--due-only` 不改变合规默认值：没有 `ATLAS_LIVE=1` 仍然退出码 2。"""
    root = tmp_path / "store"
    _register_via_cli(root, channel_id="chan-never", interval=1800)
    proc = run_cli("run", "--store-root", str(root), "--due-only", "--now", NOW.isoformat())
    assert proc.returncode == 2
    assert "ATLAS_LIVE" in proc.stderr
    assert not (root / "runs").exists()


def test_cli_no_channels_is_still_a_configuration_error(tmp_path: Path) -> None:
    """**活对照**：同一个 `run --due-only`，空注册表 ⇒ 退出码 1（配置问题）。

    而 `plan --due-only` 是**只读查询**：空注册表也能打印出来（退出码 0），
    但它**必须**如实说"一个可采集渠道都没有（配置问题）"，而不是说"没有到期的渠道"
    —— 后者会让人以为"配置没问题，只是这一轮不用采"。
    """
    proc = run_cli(
        "run",
        "--store-root",
        str(tmp_path / "empty"),
        "--due-only",
        "--now",
        NOW.isoformat(),
        env={"ATLAS_LIVE": "1"},
    )
    assert proc.returncode == 1
    assert "没有可采集的渠道" in proc.stderr
    assert "没有到期的渠道" not in proc.stdout

    # 只读查询：`plan --due-only` 对空注册表退出码 0，但措辞必须区分两种"空"
    empty = tmp_path / "empty-plan"
    plan_proc = run_cli(
        "plan", "--store-root", str(empty), "--due-only", "--now", NOW.isoformat()
    )
    assert plan_proc.returncode == 0, plan_proc.stderr
    plan = json.loads(plan_proc.stdout)
    assert plan["channels"] == []
    assert plan["schedule"]["enabled_channels"] == 0
    assert "配置问题" in plan_proc.stderr, plan_proc.stderr
    assert "没有到期的渠道" not in plan_proc.stderr, plan_proc.stderr
    assert "没有到期的渠道" not in plan_proc.stdout, plan_proc.stdout

    # 活对照的另一半：不加 `--due-only` 时行为与 T-120 完全一致
    plain = run_cli("plan", "--store-root", str(tmp_path / "empty-plan2"))
    assert plain.returncode == 0, plain.stderr
    assert json.loads(plain.stdout)["channels"] == []


def test_cli_now_must_be_iso8601(tmp_path: Path) -> None:
    """`--now` 不是 ISO-8601 ⇒ 明确拒绝（`SystemExit` 文本进 stderr，非零退出）。"""
    proc = run_cli("plan", "--store-root", str(tmp_path / "store"), "--now", "yesterday")
    assert proc.returncode != 0
    assert "ISO-8601" in proc.stderr


# --------------------------------------------------------------------------- #
# 真默认装配（真 UrllibFetcher + 真 robots + 真限速）只打 127.0.0.1：
# due-only 的整条真实链路（判定 → 注册 → 真抓 → 真归档）
# --------------------------------------------------------------------------- #


class _LocalHandler(http.server.BaseHTTPRequestHandler):
    """本地回环服务（**只绑 127.0.0.1**，不打任何外部网络）。"""

    routes: Dict[str, Tuple[int, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        status, body = self.routes.get(self.path, (404, b""))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return None


@contextlib.contextmanager
def _local_server(routes: Dict[str, Tuple[int, bytes]]):
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


def test_due_only_with_real_assembly_fetches_over_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**不注入任何替身**：due-only 判定 → 注册渠道 → 真 HTTP 抓取 → 真归档。

    这条把 `interval_seconds → atlas.schedule → collect` 整条链路跑成"真的抓到了字节"，
    而不是只有"注册了哪些渠道"（SPEC 硬规则 1：完成必须用跑通的数据流证明）。
    第二个渠道**未到期**：它一次都不该被请求（真实服务器上根本没为它准备路由）。
    """
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)

    root = tmp_path / "store"
    body = "T-207 due-only 真实抓取探针。".encode("utf-8")
    allow_all = b"User-agent: *\nAllow: /\n"
    with _local_server({"/robots.txt": (200, allow_all), "/due.txt": (200, body)}) as base:
        _register(
            root, channel_id="live-due", endpoint=f"{base}/due.txt", interval_seconds=1800
        )
        _register(
            root,
            channel_id="live-fresh",
            endpoint=f"{base}/fresh.txt",
            interval_seconds=7200,
        )
        # 只有 live-fresh 有"刚采过"的记录 ⇒ 它未到期；live-due 从未采集 ⇒ 到期
        _seed_state(root, [("live-fresh", NOW - timedelta(minutes=1))])

        # 不注入任何依赖 ⇒ 走 `ComposeDependencies.real()`（真 UrllibFetcher + 真 robots + 真限速）
        with build_pipeline(store_root=root, actor=ACTOR, window=WINDOW) as pipeline:
            decision = pipeline.due_decision(now=NOW)
            assert decision.due_ids == ("live-due",), decision.as_dict()
            report = pipeline.run(due_only=True, now=NOW)

    assert report.all_succeeded(), report.results
    assert {
        item["channel_id"]
        for item in report.result("collect").output.artifacts["observed"]["per_channel"]
    } == {"live-due"}

    archive = open_archive(root)
    try:
        raw_id = raw_id_for("live-due", f"{base}/due.txt", content_sha256(body))
        # 归档里正好两条：一条是 live-fresh 的"刚采过"种子记录，一条是本轮真抓到的
        assert raw_id in archive.all_raw_ids()
        assert archive.records.count() == 2, archive.all_raw_ids()
        assert archive.blobs.content_path(raw_id).read_bytes() == body, (
            "抓到的字节必须真的落盘（真实 HTTP，不是替身回放）"
        )
        assert archive.get(raw_id).http_status == 200
        # live-fresh 归档里仍**只有**种子那一条 ⇒ 未到期的渠道一次都没被请求
        seeded = [rid for rid in archive.all_raw_ids() if rid != raw_id]
        assert len(seeded) == 1
        assert archive.get(seeded[0]).channel_id == "live-fresh"
        assert b"t207 seed" in archive.get_content(seeded[0])
        assert archive.verify() == []
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# B9：调度 ≠ 真的抓了（有意保留的交互）
# --------------------------------------------------------------------------- #


def test_scheduled_channel_can_still_be_skipped_by_window_idempotency(
    tmp_path: Path,
) -> None:
    """同一个整点小时内：调度判**到期**，但运行器仍因同窗口幂等**跳过**。

    这是文档里写明的两层分工（`atlas.schedule` 模块文档），**不是 bug**：
    调度器决定"试哪些渠道"，幂等决定"是否真的发请求"。本用例把这条交互钉住，
    免得将来有人误以为 due-only 等于"每个 interval 一定打一次网络"。

    为了把两件事分清楚，这里**显式固定窗口**（`window=WINDOW`，12:00 那个整点桶）：
    调度判定的 `now` 推进到 12:45（到期），而窗口仍是 12:00（同窗口 ⇒ 幂等跳过）。
    """
    root = tmp_path / "store"
    _register(root, channel_id="chan-hourly", endpoint=PLAIN_ENDPOINT, interval_seconds=3600)
    fetcher = FakeFetcher({PLAIN_ENDPOINT: PLAIN_BODY})

    with _pipeline(root, fetcher) as pipeline:  # window=WINDOW（12:00 的整点桶）
        assert pipeline.due_decision(now=NOW).due_ids == ("chan-hourly",)
        first = pipeline.run(due_only=True, now=NOW)
        assert first.all_succeeded()

        # 45 分钟后（13:00 前）：**仍在 12:00 那个整点小时窗口内** ⇒ 调度说到期，
        # 但因为窗口起点没跨过（raw 的 fetched_at = 12:00），距上次只过了 30 分钟，
        # 调度器此时**还**判未到期 —— 判"到期"要用 12:00 之后的下一个整点窗口。
        at_1245 = pipeline.due_decision(now=NOW + timedelta(minutes=45))
        assert at_1245.due_ids == (), "12:45 时距上次采集只 45 分钟，未到期（interval=3600）"

        # 到 13:00：调度判到期，但窗口仍是 12:00 那个桶 ⇒ 幂等跳过（两层分工）
        later = NOW + timedelta(hours=1)
        decision = pipeline.due_decision(now=later)
        assert decision.due_ids == ("chan-hourly",), "调度层：整点后确实到期"
        second = pipeline.run(due_only=True, now=later)

    assert second.result("collect").status == "skipped", second.results
    assert "幂等跳过" in second.result("collect").reason
    # 证据：网络只被打了一次（第一次），第二次的"到期"没有变成网络请求
    assert fetcher.calls == [PLAIN_ENDPOINT]
    # 而且采集记录里只有一条 raw（窗口起点决定了 fetched_at）
    archive = open_archive(root)
    try:
        assert archive.records.count() == 1
    finally:
        archive.close()
