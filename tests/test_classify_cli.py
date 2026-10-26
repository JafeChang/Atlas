"""T-105 的**用户入口**判据：一条命令真的能跑出 claim（SPEC 硬规则 1）。

判据（用户实际的验收问题："我能不能跑一条命令把这个能力用起来？"）：

1. `python -m atlas.compose classify --store-root <根>` 退出码 **0**，
   报告里含分类的计数，且 `proposed_claims` 里**真的有分类行**
   （由测试另开连接逐行读出 —— 跨连接可见才算数）。
2. 第二次跑同一命令：**每个单元都被幂等跳过**（`units_skipped_already_run` == `units_seen`），
   一行都不新写 —— "不重复调用模型"由 T-105 的运行账保证，报告必须如实报出来。
3. `--raw-id` 收窄只跑指定的原文；写错一个字 ⇒ 退出码非 0 + 响亮报错
   （**不**静默变成"没有这条"）。
4. **模型调用默认关闭**：`run` 没有 `--classify` / `ATLAS_COGNITION=1` 时一个模型都不调；
   `plan` 只读地报出这个开关（`classify_enabled`），因此"这一轮会不会花钱"一眼可见。
5. 没有输入时（归档为空）**响亮失败**：退出码 1 + 一句人话，**没有** traceback。

`classify` 命令本身**会调用模型**（那就是它的全部内容），因此本文件用注入的哑端口
（`QuotingPort`）在**进程内**调用 `main()`：断言的是"CLI 这条路径真的能把 claim 跑出来"，
而不是"函数能被调用"。子进程用例只覆盖**不需要模型**的部分（`--help` / `plan` /
拒绝路径 / 空归档）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.compose import pipeline as pipeline_module
from atlas.compose.cli import main
from tests._compose_classify import (
    ARTICLE_ENDPOINT,
    CHANNEL_ARTICLE,
    FEED_BYTES,
    FEED_ENDPOINT,
    QUOTE_ARTICLE_A,
    RAW_TEXT,
    QuotingPort,
    classify_pipeline,
    make_store_root,
    proposed_rows,
    raw_id_of,
)
from tests._compose_cli import run_cli

REPO_STORE_ROOT = Path("data/store").resolve()

ARTICLE_RAW = raw_id_of(RAW_TEXT, channel_id=CHANNEL_ARTICLE, endpoint=ARTICLE_ENDPOINT)
FEED_RAW = raw_id_of(FEED_BYTES, channel_id="chan-feed", endpoint=FEED_ENDPOINT)

SCRIPT = (
    ("ai", QUOTE_ARTICLE_A),
    ("web", "transformer scaling for language models"),
    ("web", "semantic image segmentation"),
)


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """测试一律用 `tmp_path`，绝不往仓库 `data/` 写（SPEC §2.10 共享存储根）。"""
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


def _prepare_archive(root: Path, port=None):
    """先把归档准备好（离线流水线，不调模型），`classify` 子命令才有输入。"""
    with classify_pipeline(root, port=port, classify=False) as pipeline:
        return pipeline.run()


# --------------------------------------------------------------------------- #
# 判据 1 / 2：一条命令跑出 claim；第二次跑幂等跳过
# --------------------------------------------------------------------------- #


def test_cli_classify_command_produces_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = make_store_root(tmp_path)
    _prepare_archive(root)
    port = QuotingPort(SCRIPT)
    monkeypatch.setattr(pipeline_module, "default_cognition_port", lambda: port)

    exit_code = main(["classify", "--store-root", str(root)])
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "机器分类（T-105，离线）" in captured.out
    assert "观察 classify" in captured.out
    assert "units_seen=3" in captured.out
    assert "units_classified=3" in captured.out
    assert "rows_written=" in captured.out
    assert "Traceback" not in captured.err

    rows = [row for row in proposed_rows(root / "atlas.db") if row["status"] == "classified"]
    assert len(rows) == 3, rows
    assert {row["raw_id"] for row in rows} == {ARTICLE_RAW, FEED_RAW}
    assert {row["quote"] for row in rows} == set(SCRIPT[i][1] for i in range(3))


def test_cli_classify_command_is_idempotent_on_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = make_store_root(tmp_path)
    _prepare_archive(root)
    port = QuotingPort(SCRIPT)
    monkeypatch.setattr(pipeline_module, "default_cognition_port", lambda: port)

    assert main(["classify", "--store-root", str(root)]) == 0
    capsys.readouterr()
    calls_after_first = port.call_count

    assert main(["classify", "--store-root", str(root)]) == 0
    second = capsys.readouterr().out

    # 节点层没有执行记录（离线入口刻意不走 TaskRunner），但 T-105 的运行账在这里生效：
    assert "units_skipped_already_run=3" in second
    assert "units_run=0" in second
    assert "rows_written=0" in second
    assert "全部被幂等跳过" in second
    assert port.call_count == calls_after_first, "第二次命令又调用了模型（烧钱）"
    assert len(proposed_rows(root / "atlas.db")) == 3


def test_cli_classify_command_honours_raw_id_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = make_store_root(tmp_path)
    _prepare_archive(root)
    port = QuotingPort(SCRIPT)
    monkeypatch.setattr(pipeline_module, "default_cognition_port", lambda: port)

    exit_code = main(["classify", "--store-root", str(root), "--raw-id", ARTICLE_RAW])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "raws_in_scope=1" in out
    assert "units_seen=1" in out
    rows = proposed_rows(root / "atlas.db")
    assert [row["raw_id"] for row in rows] == [ARTICLE_RAW]
    assert rows[0]["quote"] == QUOTE_ARTICLE_A


# --------------------------------------------------------------------------- #
# 判据 3：写错的 raw_id 响亮失败
# --------------------------------------------------------------------------- #


def test_cli_classify_rejects_unknown_raw_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = make_store_root(tmp_path)
    _prepare_archive(root)
    monkeypatch.setattr(pipeline_module, "default_cognition_port", lambda: QuotingPort(SCRIPT))
    bogus = "raw_" + "0" * 32

    exit_code = main(["classify", "--store-root", str(root), "--raw-id", bogus])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert bogus in captured.err
    assert "拒绝静默取交集" in captured.err
    assert "Traceback" not in captured.err
    assert proposed_rows(root / "atlas.db") == []


def test_cli_classify_without_archive_fails_loudly(tmp_path: Path) -> None:
    """空归档：退出码 1 + 一句人话，绝不"分类了 0 个单元但一切正常"。"""
    result = run_cli("classify", "--store-root", str(tmp_path / "empty-store"))

    assert result.returncode == 1
    assert "归档" in result.stderr and "没有任何 raw" in result.stderr
    assert "Traceback" not in result.stderr


# --------------------------------------------------------------------------- #
# 判据 4：模型调用默认关闭，且开关一眼可见
# --------------------------------------------------------------------------- #


def test_cli_plan_reports_the_classify_switch(tmp_path: Path) -> None:
    """`plan` 必须**只读地**报出"这一轮会不会调模型"，且真的能接受 `--classify`。"""
    root = make_store_root(tmp_path)

    off = run_cli("plan", "--store-root", str(root))
    on = run_cli("plan", "--store-root", str(root), "--classify")

    assert off.returncode == 0, off.stderr
    assert on.returncode == 0, on.stderr
    assert json.loads(off.stdout)["classify_enabled"] is False
    payload = json.loads(on.stdout)
    assert payload["classify_enabled"] is True
    assert {node["name"] for node in payload["nodes"]} >= {"classify", "evidence"}
    dependencies = {node["name"]: node["depends_on"] for node in payload["nodes"]}
    assert dependencies["classify"] == ["normalize"]
    names = [node["name"] for node in payload["nodes"]]
    assert names.index("classify") < names.index("evidence")


def test_cli_plan_never_constructs_the_cognition_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """**进程内**跑 `plan`（含 `--classify`）：认知层端口一次都不许被构造。

    为什么这条必须是**进程内**调用：`monkeypatch` 只作用于本进程，用
    `run_cli`（子进程）打这个桩的话，桩根本不会生效 —— 那样断言就成了空转
    （"不会失败的测试不是测试"）。子进程侧的"CLI 真的接受 `--classify`"由
    上一个用例覆盖，两者分工明确。
    """
    calls = {"n": 0}

    def exploding_factory():  # pragma: no cover - 被调用即失败
        calls["n"] += 1
        raise AssertionError("plan 不得构造认知层端口（那会读凭据 / 起边车）")

    monkeypatch.setattr(pipeline_module, "default_cognition_port", exploding_factory)
    root = make_store_root(tmp_path)

    assert main(["plan", "--store-root", str(root)]) == 0
    off = json.loads(capsys.readouterr().out)
    assert main(["plan", "--store-root", str(root), "--classify"]) == 0
    on = json.loads(capsys.readouterr().out)
    assert calls["n"] == 0
    assert off["classify_enabled"] is False
    assert on["classify_enabled"] is True


def test_cli_run_still_requires_live_opt_in_and_touches_nothing(
    tmp_path: Path,
) -> None:
    """`run` 的既有纪律不变：没有 `ATLAS_LIVE=1` ⇒ 退出码 2，且不留任何存储痕迹。

    `--classify` **不**能绕过它：抓取与模型调用是两个独立的显式开关
    （`ATLAS_LIVE` 管"是否对真实渠道发请求"，`--classify` / `ATLAS_COGNITION` 管"是否花钱调模型"）。
    """
    proc = run_cli("run", "--store-root", str(tmp_path / "store"), "--classify")

    assert proc.returncode == 2
    assert "ATLAS_LIVE" in proc.stderr
    assert not (tmp_path / "store").exists()


def test_cli_help_lists_the_classify_command() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert "classify" in result.stdout
    assert "evidence" in result.stdout
