"""`atlas.webapp` 的命令行入口（`python -m atlas.webapp`）验收。

用户的原话是判据的出处："用户照着一条命令就能打开界面、看见真实标题、点一下就完成一次
条目级打标"。因此这个入口是**交付物的一部分**，不是脚手架。

本文件用**不阻塞**的方式覆盖它：

1. **默认值**：`--store-root` 默认 `data/store`（可被 `ATLAS_STORE_ROOT` 覆盖）、
   `--host` 默认 `127.0.0.1`、`--port` 默认 `8765`、`--actor` 有默认值；
2. **默认只绑回环**：`--host` 的默认值就是 `127.0.0.1`，且帮助文本里写明"没有认证"；
3. **store 不存在 ⇒ 响亮失败**：非零退出码 + stderr 里说清"先跑采集"；
4. **store 存在但没有归档 ⇒ 响亮失败**：空 feed 不等于功能坏了；
5. **参数非法 ⇒ 响亮失败**：端口越界、actor 为空；
6. **`store_summary` 是只读的**：真实归档上跑一遍，字节不变；
7. **模块可作为 `__main__` 执行**：文件末尾有 `if __name__ == "__main__":` 守卫
   （`python -m atlas.webapp` 依赖它）。

真正"起服务并打一次标签"的完整闭环在 `tools/t109_entries_e2e.py`（它会真的写真实库，
因此不入 pytest）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256
from atlas.webapp import (
    DEFAULT_PORT,
    DEFAULT_STORE_ROOT,
    STORE_ROOT_ENV_VAR,
    build_parser,
    main,
    store_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBAPP_MODULE = REPO_ROOT / "src" / "atlas" / "webapp.py"

FEED_TEXT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0"><channel><title>demo</title>'
    "<item><title>Hello</title><link>https://example.test/hello</link>"
    "<description><![CDATA[<p>hi</p>]]></description></item>"
    "</channel></rss>"
)


# ---------------------------------------------------------------------- #
# 判据 1 / 2：默认值与"默认只绑回环"
# ---------------------------------------------------------------------- #
def test_parser_defaults_bind_loopback_and_use_a_high_port() -> None:
    args = build_parser().parse_args([])
    assert args.store_root == DEFAULT_STORE_ROOT
    assert args.host == "127.0.0.1", "默认必须只绑回环（服务能写标签且没有认证）"
    assert args.port == DEFAULT_PORT == 8765
    assert args.actor  # 有默认署名
    parser = build_parser()
    help_text = parser.format_help()
    assert "没有认证" in help_text, "帮助文本必须写明本服务没有认证"
    assert "127.0.0.1" in help_text


def test_store_root_env_var_overrides_the_default(monkeypatch) -> None:
    monkeypatch.setenv(STORE_ROOT_ENV_VAR, "/tmp/somewhere-else")
    # `build_parser` 在**构造时**读取环境变量（与 atlas.compose.cli 同一口径）
    args = build_parser().parse_args([])
    assert args.store_root == "/tmp/somewhere-else"


# ---------------------------------------------------------------------- #
# 判据 3 / 4 / 5：响亮失败
# ---------------------------------------------------------------------- #
def test_missing_store_root_fails_loudly(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope"
    code = main(["--store-root", str(missing)])
    captured = capsys.readouterr()
    assert code != 0, "store 不存在时必须非零退出（不得静默起一个空界面）"
    assert "存储根不存在" in captured.err
    assert "atlas.compose run" in captured.err, "错误信息必须给下一步"
    assert not missing.exists(), "失败路径不得创建任何目录"
    assert captured.out == ""


def test_store_without_any_records_fails_loudly(tmp_path: Path, capsys) -> None:
    root = tmp_path / "empty-store"
    root.mkdir()
    archive = open_archive(root)
    archive.close()  # 建出空库（0 条归档记录）
    code = main(["--store-root", str(root)])
    captured = capsys.readouterr()
    assert code != 0
    assert "没有任何归档记录" in captured.err
    assert "还没有采集" in captured.err


def test_illegal_arguments_fail_loudly(tmp_path: Path, capsys) -> None:
    assert main(["--store-root", str(tmp_path), "--port", "70000"]) != 0
    assert "port" in capsys.readouterr().err

    assert main(["--store-root", str(tmp_path), "--actor", "   "]) != 0
    assert "actor" in capsys.readouterr().err


# ---------------------------------------------------------------------- #
# 判据 6：store_summary 只读
# ---------------------------------------------------------------------- #
def test_store_summary_counts_containers_and_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "store"
    archive = open_archive(root)
    try:
        payload = FEED_TEXT.encode("utf-8")
        archive.put(
            RawRecord(
                raw_id="raw-cli-feed",
                channel_id="chan-1",
                endpoint="https://example.test/feed.xml",
                content_sha256=content_sha256(payload),
                byte_length=len(payload),
                fetched_at=_moment(),
                http_status=200,
            ),
            payload,
        )
        article = b"one whole article, no items here"
        archive.put(
            RawRecord(
                raw_id="raw-cli-article",
                channel_id="chan-1",
                endpoint="https://example.test/art",
                content_sha256=content_sha256(article),
                byte_length=len(article),
                fetched_at=_moment(),
                http_status=200,
            ),
            article,
        )
    finally:
        archive.close()

    def snapshot() -> dict:
        return {
            str(path.relative_to(root)): path.stat().st_size
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    summary = store_summary(root)
    assert summary.records == 2
    assert summary.containers == 1
    assert summary.derived_entries == 1
    assert summary.direct_entries == 1
    assert summary.entries == 2
    assert snapshot() == before, "盘点必须是只读的"


def _moment():
    from datetime import datetime, timezone

    return datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- #
# 判据 7：可作为 __main__ 执行
# ---------------------------------------------------------------------- #
def test_module_has_a_main_guard_and_argparse_only() -> None:
    tree = ast.parse(WEBAPP_MODULE.read_text(encoding="utf-8"))
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "__name__ == '__main__'"
    ]
    assert guards, "必须有 `if __name__ == \"__main__\":` 守卫（python -m atlas.webapp 依赖它）"
    body = ast.unparse(guards[0])
    assert "SystemExit(main())" in body

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    # 零新增依赖：CLI 只用 stdlib + 仓库内包
    assert imported <= {
        "__future__",
        "argparse",
        "atlas",
        "dataclasses",
        "os",
        "pathlib",
        "sys",
        "threading",
        "typing",
    }, sorted(imported)
    assert "argparse" in imported
