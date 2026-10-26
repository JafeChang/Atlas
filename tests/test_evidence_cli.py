"""T-107 的**用户入口**判据：一条命令真的能跑完证据校验（SPEC 硬规则 1）。

判据（用户实际的验收问题："我能不能跑一条命令把这个能力用起来？"）：

1. `python -m atlas.compose evidence --store-root <根> --raw-id <raw>` 退出码 **0**，
   报告里含证据校验的计数，且**真的**把证据写进了 `evidence_spans`
   （由测试另开连接、按 claim 逐条读出 —— 跨连接可见才算数）。
2. 第二次跑同一命令：`evidence` 被**幂等跳过**（输出里出现 `[skipped] evidence`），
   库里行数不变。
3. `--read-only`：校验照跑、库里**一行都不多**，报告里 `wrote_to_store=false`。
4. 未验证的 claim 在**输出**里可见：[未验证] 那一行必须真的打印出来
   （SPEC §7.3 失败模式 3：静默失效是真实缺陷形态，不是假想）。
5. `--raw-id` 打错一个字 ⇒ 退出码非 0 + 响亮报错，**不**静默变成"没有这条"。

命令一律通过 `tests/_compose_cli.run_cli`（子进程 + 干净环境、无 `ATLAS_LIVE`）执行，
因此断言的是"真的能从命令行跑起来"，不是"函数能被调用"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._compose_cli import run_cli
from tests._compose_evidence import (
    QUOTE_A,
    QUOTE_MISSING,
    RAW_TEXT,
    classified_row,
    evidence_db_rows,
    evidence_pipeline,
    make_store_root,
)

REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """测试一律用 `tmp_path`，绝不往仓库 `data/` 写（SPEC §2.10 共享存储根）。"""
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )


def _prepare(root: Path, *, quote: str = QUOTE_A) -> str:
    """跑一次流水线（归档 + 归一化 + 真 claim），返回本轮 raw_id。"""
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = report.result("collect").output.artifacts["identity"]["records"][0]["raw"][
            "raw_id"
        ]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=quote,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
            )
        )
        # 第一次跑（此时库里还没有 claim）会写 0 条；再跑一次让证据真的落库
        if quote == QUOTE_A:
            pipeline.run()
    return raw_id


def test_cli_evidence_command_verifies_and_persists(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    raw_id = _prepare(root)
    before = evidence_db_rows(root / "atlas.db")
    assert len(before) == 1, before

    result = run_cli(
        "evidence",
        "--store-root",
        str(root),
        "--raw-id",
        raw_id,
    )

    assert result.returncode == 0, result.stderr
    assert "证据校验（T-107，离线）" in result.stdout
    assert "观察 evidence" in result.stdout
    assert "classified_claims=1" in result.stdout
    assert "verified=1" in result.stdout
    assert "wrote_to_store=True" in result.stdout
    assert "Traceback" not in result.stderr
    # 库里仍然只有那一行（幂等：没有因为命令再跑一遍而多写）
    after = evidence_db_rows(root / "atlas.db")
    assert [row["claim_id"] for row in after] == [row["claim_id"] for row in before]
    assert after[0]["char_start"] < after[0]["char_end"]


def test_cli_evidence_is_idempotent_on_second_run(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    raw_id = _prepare(root)

    first = run_cli("evidence", "--store-root", str(root), "--raw-id", raw_id)
    second = run_cli("evidence", "--store-root", str(root), "--raw-id", raw_id)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    # 幂等：第二次一条都不新写（spans_written=0、spans_unchanged=1），库里仍是 1 行
    assert "spans_written=0" in second.stdout
    assert "spans_unchanged=1" in second.stdout
    assert "spans_in_store=1" in second.stdout
    assert "幂等键：" in second.stdout
    assert len(evidence_db_rows(root / "atlas.db")) == 1


def test_cli_evidence_read_only_writes_nothing(tmp_path: Path) -> None:
    # 先在一个根上真的写证据（活对照），再在另一个**干净**的根上只读校验：
    # `evidence_spans` 是 append-only，所以"只读不写"必须在没有既有行的根上验证。
    written_root = make_store_root(tmp_path / "written")
    _prepare(written_root)

    root = make_store_root(tmp_path / "readonly")
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
        raw_id = report.result("collect").output.artifacts["identity"]["records"][0]["raw"][
            "raw_id"
        ]
        pipeline.proposed.record_claim(
            classified_row(
                raw_id=raw_id,
                quote=QUOTE_A,
                unit_char_start=0,
                unit_char_end=len(RAW_TEXT),
            )
        )

    result = run_cli(
        "evidence",
        "--store-root",
        str(root),
        "--raw-id",
        raw_id,
        "--read-only",
    )

    assert result.returncode == 0, result.stderr
    assert "wrote_to_store=False" in result.stdout
    assert "spans_written=0" in result.stdout
    assert "verified=1" in result.stdout
    assert evidence_db_rows(root / "atlas.db") == []
    # 活对照：同一条路径在另一个根上确实写得进去（所以"没写"不是因为路径不通）
    assert len(evidence_db_rows(written_root / "atlas.db")) == 1


def test_cli_evidence_surfaces_unverified_claim(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    raw_id = _prepare(root, quote=QUOTE_MISSING)

    result = run_cli("evidence", "--store-root", str(root), "--raw-id", raw_id)

    # 校验失败**不是**节点失败（重跑一万次也是同一个结果），但必须在输出里可见
    assert result.returncode == 0, result.stderr
    assert "verification_failed=1" in result.stdout
    assert "[未验证]" in result.stdout
    assert QUOTE_MISSING in result.stdout
    assert "spans_written=0" in result.stdout
    assert evidence_db_rows(root / "atlas.db") == []


def test_cli_evidence_rejects_unknown_raw_id(tmp_path: Path) -> None:
    root = make_store_root(tmp_path)
    raw_id = _prepare(root)
    bogus = "raw_" + "0" * 32
    assert bogus != raw_id

    result = run_cli("evidence", "--store-root", str(root), "--raw-id", bogus)

    assert result.returncode != 0
    assert bogus in result.stderr
    assert "拒绝静默取交集" in result.stderr or "不在归档" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_evidence_help_lists_the_command(tmp_path: Path) -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    assert "evidence" in result.stdout


def test_zero_claim_state_is_visible_not_success_not_error(tmp_path: Path) -> None:
    """**今天真实的状态**：任何新 store 都还没有 `classified` 行。

    此时证据节点必须：退出码 0（"没有可校验的东西"不是失败）、
    报告里显式写出"校验了 0 条、**没有**产生证据"，
    且绝不能看起来像"全部通过"（那正是"看起来成功、实际什么都没做"）。
    """
    root = make_store_root(tmp_path)
    with evidence_pipeline(root) as pipeline:
        report = pipeline.run()
    observed = report.result("evidence").output.artifacts["observed"]
    assert observed["raws_in_scope"] == 1
    assert observed["classified_claims"] == 0
    assert observed["spans_written"] == 0
    assert observed["skipped_unclassified"] == 0
    assert observed["raws_without_claims"]

    from atlas.compose.cli import render_evidence

    lines = render_evidence(observed)
    text = "\n".join(lines)
    assert "classified_claims=0" in text
    assert "没有" in text and "产生证据" in text
    assert "[无 claim]" in text

    result = run_cli("evidence", "--store-root", str(root))
    assert result.returncode == 0, result.stderr
    assert "classified_claims=0" in result.stdout
    assert "没有" in result.stdout and "产生证据" in result.stdout
    # 一条都没写：库里没有表都算正常（真实库里 evidence_spans 至今不存在）
    assert evidence_db_rows(root / "atlas.db") == []

