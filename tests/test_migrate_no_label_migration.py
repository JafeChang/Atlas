"""T-131 防线：**不得迁移任何人工标签**（SPEC §2.4 / §2.10）—— 用三层证明钉死。

为什么这条必须是"代码级强制"而不是"约定"
------------------------------------------

§2.3 把三态分得很清：Raw **只增不改** / Proposed **可覆写** / Confirmed **只增不改**。
§2.4 进一步钉住：**去重只做"隐藏/标注"，永不"合并"条目身份**，因为**全部**人工标签
都锚在 `raw_id` 上。导入恰好**改变条目身份**：

| | 旧系统 | 导入后 |
|---|---|---|
| 身份 | 每份 JSON 的 `id`（uuid） | `raw_id = f(channel_id, source_url, content_sha256)` |
| 一篇文章被采集 10 次 | 10 个 uuid、10 条记录 | **1 条 Raw**（内容寻址收敛） |

也就是说：旧 `raw_id` 与 新 `raw_id` 是**两套互不相同的命名空间**。把标签"迁移"过去，
就是把一个锚在 A 对象上的判断挂到 B 对象上 —— 而这两个对象甚至不是一一对应的
（10 → 1）。后果是**标签静默指向另一篇文章**，且因为 Confirmed 只增不改，
**错的东西删不掉**。

本任务的范围本就不含标签（旧库里也没有可用的人工标签），但"范围之外"不等于"不用防"：
归档基线的教训正是"没有路径写进去的字段，最后变成空壳；有路径写错的字段，最后变成脏数据"。
本文件用三层证明把这条防线钉住：

1. **静态**：`atlas.migrate` 的源码里没有 `labels` / `confirmed` / `proposed` 这类符号
   （AST 级，连字符串注释外的引用都查）。
2. **行为（本任务侧）**：迁移运行的**全部** SQL 写操作只碰 `raw_records`
   —— 用 sqlite `authorizer` 同连接拦截（不是事后看结果）。
3. **存储不变量**：`confirmed_labels` 的 append-only 触发器仍在；
   用**活对照**证明"否定的断言真的在验证东西"（合法 `SELECT` / `INSERT` 成功，
   `UPDATE` / `DELETE` 才被拒）。

**注意**：第 3 条在合成临时库上做（本文件），真实库上的同一条断言在
`tests/test_migrate_realdata.py`（那里的行数是导入后**逐位比对**的）。
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path
from typing import List

import pytest

from atlas.archive import open_archive
from atlas.migrate import Migrate, MigrateOptions, build_channel_map
from tests._migrate_fixtures import fake_channels, legacy_record, write_legacy

REGISTRY = fake_channels((("chan-a", "industry-x"),))
CHANNEL_MAP = build_channel_map(REGISTRY)

#: 禁止出现在 `atlas.migrate` 源码里的符号（跨域表名与模块名）。
FORBIDDEN_SYMBOLS = (
    "confirmed_labels",
    "confirmed_label",
    "ConfirmedLabel",
    "proposed_claims",
    "ProposedClaim",
    "evidence_spans",
    "from_claim_id",
    "atlas.labels",
    "atlas_evidence",
)


def test_no_label_symbols_in_migrate_source() -> None:
    """第 1 层（静态）：`atlas.migrate` 里没有标签/提议/证据相关的符号。"""
    package = Path(__file__).resolve().parents[1] / "src" / "atlas" / "migrate"
    hits: List[str] = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        names: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.append(alias.name)
                    if alias.asname:
                        names.append(alias.asname)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.append(node.value)
        for symbol in FORBIDDEN_SYMBOLS:
            if any(symbol in name for name in names):
                hits.append(f"{path.name}: {symbol}")
    assert hits == [], f"atlas.migrate 出现了标签域符号，防线被绕过：{hits}"


def test_migration_touches_only_raw_records_by_authorizer(tmp_path: Path) -> None:
    """第 2 层（行为）：迁移期间**全部** SQL 只碰 raw 域的表（白名单之外的写被拒）。

    做法：在归档连接上装 `authorizer`，**拒绝任何**对 `raw_records` /
    `raw_store_meta` / sqlite 内部表以外的写操作。若迁移试图写 `confirmed_labels`，
    那条 `INSERT` 会在这里失败 —— 这是**在调用点上**拦截，不是事后比对结果。
    """
    legacy = tmp_path / "legacy"
    store = tmp_path / "store"
    write_legacy(legacy, "chan-a", "a.json", legacy_record(source_url="https://x.invalid/1"))

    archive = open_archive(store)
    try:
        # 造一张"标签表"：任何写入都会被下面的 authorizer 拒绝
        archive.records.connection.execute(
            "CREATE TABLE IF NOT EXISTS confirmed_labels ("
            "label_id TEXT PRIMARY KEY, raw_id TEXT NOT NULL, label_key TEXT NOT NULL,"
            "label_value TEXT, actor TEXT NOT NULL)"
        )
        armed = {"denied": 0}

        def guard(action: int, arg1, arg2, _db, _trigger) -> int:
            if action in (
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            ):
                table = arg1 if action == sqlite3.SQLITE_INSERT else arg2
                if table not in ("raw_records", "raw_store_meta"):
                    armed["denied"] += 1
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        archive.records.connection.set_authorizer(guard)
        try:
            # 活对照 1：白名单内的合法 INSERT 必须**成功**（否则"被拒"什么都证明不了）
            archive.records.connection.execute(
                "INSERT INTO raw_store_meta (key, value) VALUES ('t131-control', 'ok')"
            )
            # 活对照 2：对标签表的**读**必须成功
            assert archive.records.connection.execute(
                "SELECT COUNT(*) AS n FROM confirmed_labels"
            ).fetchone()["n"] == 0
            # 否定断言：对标签表的**写**必须被拒
            with pytest.raises(sqlite3.DatabaseError):
                archive.records.connection.execute(
                    "INSERT INTO confirmed_labels (label_id, raw_id, label_key, label_value, actor)"
                    " VALUES ('lbl_x', 'raw_x', 'k', 'v', 'tester')"
                )
            assert armed["denied"] == 1, "authorizer 必须真的拦下了那次写入"
            armed["denied"] = 0

            report = Migrate(
                MigrateOptions(
                    legacy_root=legacy,
                    archive_root=store,
                    archive=archive,
                    channel_map=CHANNEL_MAP,
                )
            ).run()
        finally:
            archive.records.connection.set_authorizer(None)

        assert report.imported == 1, "活对照：迁移本身必须成功"
        assert armed["denied"] == 0, (
            f"迁移期间有 {armed['denied']} 次越界写入被拦截 —— 说明迁移在写标签域"
        )
        assert archive.records.connection.execute(
            "SELECT COUNT(*) AS n FROM confirmed_labels"
        ).fetchone()["n"] == 0
    finally:
        archive.close()


def test_confirmed_labels_append_only_triggers_still_hold(tmp_path: Path) -> None:
    """第 3 层：`confirmed_labels` 的 append-only 触发器仍在（**活对照 + 否定**）。"""
    store = tmp_path / "store"
    archive = open_archive(store)
    try:
        connection = archive.records.connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS confirmed_labels ("
            "label_id TEXT PRIMARY KEY, raw_id TEXT NOT NULL, label_key TEXT NOT NULL,"
            "label_value TEXT, actor TEXT NOT NULL)"
        )
        for verb, name in (
            ("UPDATE", "trg_confirmed_labels_no_update"),
            ("DELETE", "trg_confirmed_labels_no_delete"),
        ):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON confirmed_labels "
                f"BEGIN SELECT RAISE(ABORT, 'confirmed_labels is append-only: {verb} is "
                "forbidden (SPEC 2.10)'); END;"
            )
        connection.execute(
            "INSERT INTO confirmed_labels (label_id, raw_id, label_key, label_value, actor)"
            " VALUES ('lbl_1', 'raw_1', 'k', 'v', 'human')"
        )

        # 活对照：合法 SELECT 必须成功（否则下面的"被拒"可能只是语法写错了）
        rows = connection.execute("SELECT COUNT(*) AS n FROM confirmed_labels").fetchone()
        assert rows["n"] == 1
        # 活对照：合法 INSERT（新增一条判断）必须成功
        connection.execute(
            "INSERT INTO confirmed_labels (label_id, raw_id, label_key, label_value, actor)"
            " VALUES ('lbl_2', 'raw_1', 'k', 'v2', 'human')"
        )
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM confirmed_labels"
        ).fetchone()["n"] == 2

        # 否定断言：改 / 删必须被触发器拒绝，且原因文本可辨认
        with pytest.raises(sqlite3.DatabaseError) as update_error:
            connection.execute("UPDATE confirmed_labels SET label_value = 'x' WHERE label_id = 'lbl_1'")
        assert "append-only" in str(update_error.value)
        with pytest.raises(sqlite3.DatabaseError) as delete_error:
            connection.execute("DELETE FROM confirmed_labels WHERE label_id = 'lbl_1'")
        assert "append-only" in str(delete_error.value)

        # 活对照（收尾）：被拒之后数据仍然完好
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM confirmed_labels"
        ).fetchone()["n"] == 2
    finally:
        archive.close()
