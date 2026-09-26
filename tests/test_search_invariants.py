"""T-205 只读投影与边界验收（判据 A3 / A7，判据全文见 `test_search_query.py`）。

两条互补的证明：

1. **静态**：包内非 docstring 的字符串字面量里
   - 不出现 `LIKE` / `GLOB`（判据 A1：不得静默降级成假全文检索）；
   - 不出现任何上游事实表名（代码不可能写到它从未提过的表）；
   - 写语句（`INSERT` / `DELETE` / `UPDATE` / `DROP` / `CREATE` / `ALTER`）的目标
     只能是 `search_*` / `idx_search_*` / FTS5 探测用的 temp 表；
   - 只 import 标准库白名单与 `atlas.contracts` / `atlas.archive` / `atlas.normalize`
     （后两者只读借用），**不** import 其它任务的实现包，**不**出现分块字样。

2. **行为**：建索引 → 查询 → boost 查询 → `get` → `drop` → 再建，全程结束后
   `raw_records` 行、raw 字节目录、归档 `verify()`、以及同库内其它域的表（哨兵行）
   必须与操作前**逐字节相同**；并用 SQLite authorizer 实测"索引执行的全部 SQL
   只碰 `search_*` 表"。
"""

from __future__ import annotations

import ast
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import atlas.search
from atlas.archive import open_archive
from atlas.contracts import RawRecord
from atlas.contracts.ids import content_sha256, raw_id_for
from atlas.search import ArchiveDocumentSource, SearchQuery, open_index

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

SEARCH_DIR = Path(atlas.search.__file__).resolve().parent
SEARCH_MODULES = sorted(SEARCH_DIR.glob("*.py"))
EXPECTED_MODULES = [
    "__init__.py",
    "documents.py",
    "errors.py",
    "query.py",
    "source.py",
    "sqlite_index.py",
]

#: 允许依赖的标准库根模块（SPEC §2.10：零新依赖）。
ALLOWED_STDLIB_ROOTS = frozenset(
    {
        "__future__",
        "contextlib",
        "dataclasses",
        "datetime",
        "hashlib",
        "pathlib",
        "re",
        "sqlite3",
        "threading",
        "typing",
    }
)

#: 允许依赖的 atlas 模块（SPEC §4.0：跨包只读借用，不 import 实现细节做写入）。
ALLOWED_ATLAS_MODULES = frozenset(
    {
        "atlas.contracts",
        "atlas.contracts.ids",
        "atlas.archive",
        "atlas.normalize",
        "atlas.search",
    }
)

#: 上游事实表：索引层**永远**不得提及它们（提及即意味着可能有写路径）。
#: 刻意不含 `industries` / `channels` 这两个词——它们在查询结果描述里是普通字段名。
UPSTREAM_TABLES = (
    "raw_records",
    "raw_store_meta",
    "confirmed_labels",
    "proposed_claims",
    "evidence_spans",
    "config_versions",
    "label_space",
    "catalog_health",
    "store_meta",
    "registry_label_refs",
    "registry_label_ref_snapshots",
    "collection_tasks",
    "raw_documents",
    "processed_documents",
    "api_keys",
    "audit_logs",
)

#: 出现这些词的字面量就是 SQL（用于把"错误消息里提到 LIKE"与"真的用了 LIKE"分开）。
_SQL_MARKERS = re.compile(
    r"\b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|DROP|MATCH|JOIN|ORDER\s+BY|LIMIT)\b",
    re.IGNORECASE,
)
#: `LIKE` / `GLOB` 作为**操作符**出现（后面跟参数占位符、引号或通配符）。
_LIKE_OPERATOR = re.compile(r"\b(LIKE|GLOB)\b\s*[?'\"%_]", re.IGNORECASE)

_WRITE_KEYWORDS = re.compile(
    r"(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE|DROP\s+TABLE|DROP\s+VIRTUAL\s+TABLE"
    r"|CREATE\s+TABLE|CREATE\s+VIRTUAL\s+TABLE|CREATE\s+INDEX|CREATE\s+UNIQUE\s+INDEX"
    r"|ALTER\s+TABLE)\s+([^\s;,(]+)",
    re.IGNORECASE,
)

#: `IF [NOT] EXISTS` 先剥掉，避免正则回溯把 `IF` 当成表名。
_IF_EXISTS = re.compile(r"\bIF\s+(?:NOT\s+)?EXISTS\b", re.IGNORECASE)

#: 索引私有的表前缀 + FTS5 探测用的 temp 表。
ALLOWED_TABLE_PREFIXES = ("search_", "idx_search_", "sqlite_")
ALLOWED_TABLE_EXACT = ("temp.__atlas_fts5_probe",)


# --------------------------------------------------------------------------- #
# 静态分析工具
# --------------------------------------------------------------------------- #
def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _code_string_literals(tree: ast.AST) -> list[str]:
    """收集**非 docstring** 的字符串字面量（含 f-string 的源码形态）。"""
    skip = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            found.append(ast.unparse(node))
    return found


def _module_constants(tree: ast.AST, module: object) -> dict[str, object]:
    names: dict[str, object] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names[target.id] = getattr(module, target.id, None)
    return names


def _resolve(text: str, constants: dict[str, object]) -> str:
    def repl(match: re.Match[str]) -> str:
        value = constants.get(match.group(1))
        return str(value) if value is not None else match.group(0)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, text)


# --------------------------------------------------------------------------- #
# A3/A7：静态
# --------------------------------------------------------------------------- #
def test_package_file_list_is_stable() -> None:
    assert [path.name for path in SEARCH_MODULES] == EXPECTED_MODULES


def test_no_like_or_glob_fallback() -> None:
    """判据 A1：不得把全文检索静默降级成 `LIKE` / `GLOB`。

    只针对**看起来像 SQL 的字面量**与**操作符用法**——错误消息里解释
    "拒绝降级成 LIKE"是必要的说明文字，不是降级实现。
    """
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for literal in _code_string_literals(tree):
            if _SQL_MARKERS.search(literal):
                upper = literal.upper()
                assert "LIKE" not in upper, f"{path.name} 的 SQL 里出现 LIKE：{literal!r}"
                assert "GLOB" not in upper, f"{path.name} 的 SQL 里出现 GLOB：{literal!r}"
            assert not _LIKE_OPERATOR.search(literal), (
                f"{path.name} 出现 LIKE/GLOB 操作符用法：{literal!r}"
            )


def test_upstream_fact_tables_are_never_mentioned_in_code() -> None:
    """代码里不出现上游表名 ⇒ 结构上不可能有写上游的语句。"""
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        joined = "\n".join(_code_string_literals(tree))
        for table in UPSTREAM_TABLES:
            assert not re.search(rf"\b{table}\b", joined), (
                f"{path.name} 的代码里出现了上游表 {table!r}（只读投影不得提及）"
            )


def test_write_statements_only_target_search_tables() -> None:
    """把包内所有写语句的目标解析出来，逐个断言是本域的表。"""
    seen: list[str] = []
    for path in SEARCH_MODULES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module = _import_module(path)
        constants = _module_constants(tree, module)
        for literal in _code_string_literals(tree):
            resolved = _IF_EXISTS.sub("", _resolve(literal, constants))
            for match in _WRITE_KEYWORDS.finditer(resolved):
                target = match.group(1).lstrip("'\"")
                seen.append(target)
                assert target.startswith(ALLOWED_TABLE_PREFIXES) or target in (
                    ALLOWED_TABLE_EXACT
                ), f"{path.name} 的写语句目标越界：{target!r}（来自 {literal!r}）"
    # 不能是空转：至少看到了索引自己的建表/写表语句
    assert "search_documents" in seen
    assert "search_documents_fts" in seen
    assert "search_meta" in seen


def test_table_name_constants_are_prefixed() -> None:
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _import_module(path)
        for name, value in _module_constants(tree, module).items():
            if name.endswith("_TABLE") and isinstance(value, str):
                assert value.startswith("search_"), f"{path.name}: {name}={value!r} 未带 search_ 前缀"


def test_no_chunking_in_this_package() -> None:
    """判据边界：分块属于 T-206；本包不得出现 chunk 结构或依赖。"""
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for literal in _code_string_literals(tree):
            assert "chunk" not in literal.lower(), f"{path.name} 出现分块字样：{literal!r}"
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("atlas.chunk")
            if module:
                assert not module.startswith("atlas.chunk")


def test_imports_are_stdlib_whitelist_or_allowed_atlas_modules() -> None:
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue  # 相对导入（同包内）不检查
            for name in names:
                root = name.split(".")[0]
                if root == "atlas":
                    assert name in ALLOWED_ATLAS_MODULES, (
                        f"{path.name} 不得 import {name}（SPEC §4.0）"
                    )
                    continue
                assert root in ALLOWED_STDLIB_ROOTS, (
                    f"{path.name} 引入了白名单外的模块（可能是新依赖）：{name}"
                )


def test_storage_module_is_source_agnostic() -> None:
    """存储层不得 import 归档/归一化实现——来源适配只在 `source.py`。"""
    path = SEARCH_DIR / "sqlite_index.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("atlas.archive", "atlas.normalize"))
        if module:
            assert not module.startswith(("atlas.archive", "atlas.normalize")), (
                "sqlite_index.py 应当只认 DocumentText，不直接读归档/归一化"
            )


def test_package_has_no_filesystem_write_or_remove_calls() -> None:
    """除"创建库文件的父目录"外，本包不碰文件系统（不写派生缓存、不删文件）。"""
    forbidden_attrs = {
        "unlink",
        "rmdir",
        "write_text",
        "write_bytes",
        "touch",
        "chmod",
        "rmtree",
        "removedirs",
        "mkdtemp",
    }
    for path in SEARCH_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                assert func.attr not in forbidden_attrs, (
                    f"{path.name}:{node.lineno} 调用了文件系统写入语义的 {func.attr}()"
                )
            elif isinstance(func, ast.Name):
                assert func.id != "open", f"{path.name}:{node.lineno} 打开了文件"


def _import_module(path: Path) -> object:
    name = f"atlas.search.{path.stem}" if path.stem != "__init__" else "atlas.search"
    return __import__(name, fromlist=["*"])


# --------------------------------------------------------------------------- #
# A3：行为——索引不改上游
# --------------------------------------------------------------------------- #
def _snapshot_raw_bytes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted((root / "raw").rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            out[str(path.relative_to(root))] = digest
    return out


def _snapshot_records(archive) -> list[dict]:
    return [archive.get(raw_id).model_dump(mode="json") for raw_id in archive.all_raw_ids()]


def _snapshot_table(db_path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        conn.close()


def _make_archive(root: Path) -> tuple[object, list[RawRecord]]:
    archive = open_archive(root)
    records = []
    bodies = (
        ("ch-a", "https://x.invalid/1", "Vector database retrieval with BM25."),
        ("ch-a", "https://x.invalid/2", "BM25 ranking function."),
        ("ch-b", "https://x.invalid/3", "Gardening in spring."),
        ("ch-b", "https://x.invalid/4", "Another vector database note."),
    )
    for channel_id, endpoint, body in bodies:
        content = body.encode("utf-8")
        record = RawRecord(
            raw_id=raw_id_for(channel_id, endpoint, content_sha256(content)),
            channel_id=channel_id,
            endpoint=endpoint,
            content_sha256=content_sha256(content),
            byte_length=len(content),
            fetched_at=BASE,
            http_status=200,
        )
        records.append(archive.put(record, content))
    return archive, records


def test_indexing_and_querying_never_change_upstream_data(tmp_path: Path) -> None:
    """判据 A3 的核心行为断言：索引不改变上游任何数据。"""
    root = tmp_path / "store"
    archive, _records = _make_archive(root)
    db_path = root / "atlas.db"

    # 同库内其它域的表（模拟 §2.10 的多域共用 DB）
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS confirmed_labels (label_id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS proposed_claims (claim_id TEXT PRIMARY KEY, payload TEXT);
        """
    )
    conn.execute("INSERT OR REPLACE INTO confirmed_labels VALUES ('lbl-1', 'human')")
    conn.execute("INSERT OR REPLACE INTO proposed_claims VALUES ('clm-1', 'machine')")
    conn.commit()
    conn.close()

    before_records = _snapshot_records(archive)
    before_bytes = _snapshot_raw_bytes(root)
    before_labels = _snapshot_table(db_path, "confirmed_labels")
    before_claims = _snapshot_table(db_path, "proposed_claims")
    assert archive.verify() == []
    assert before_bytes and before_records

    index = open_index(root)
    try:
        index.rebuild(
            ArchiveDocumentSource(archive).iter_documents(),
            industry_of={"ch-a": "ai", "ch-b": "garden"},
        )
        assert index.search(SearchQuery(text="vector database")).total == 2
        assert index.search(SearchQuery(text="bm25", industries=("ai",))).total == 2
        assert index.search(
            SearchQuery(text="vector", limit=1), boost=lambda hit: 1.0
        ).total == 2
        assert index.get(before_records[0]["raw_id"]) is not None
        index.drop()
        index.rebuild(ArchiveDocumentSource(archive).iter_documents())
    finally:
        index.close()

    assert _snapshot_records(archive) == before_records
    assert _snapshot_raw_bytes(root) == before_bytes
    assert archive.verify() == []
    assert _snapshot_table(db_path, "confirmed_labels") == before_labels
    assert _snapshot_table(db_path, "proposed_claims") == before_claims
    archive.close()


def test_drop_only_removes_search_tables(tmp_path: Path) -> None:
    root = tmp_path / "store"
    archive, _records = _make_archive(root)
    db_path = root / "atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS confirmed_labels (label_id TEXT PRIMARY KEY)")
    conn.execute("INSERT OR REPLACE INTO confirmed_labels VALUES ('lbl-1')")
    conn.commit()
    conn.close()

    index = open_index(root)
    try:
        index.rebuild(ArchiveDocumentSource(archive).iter_documents())
        index.drop()
    finally:
        index.close()

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "confirmed_labels" in tables
        assert "raw_records" in tables
        assert not any(name == "search_documents" for name in tables)
        assert not any(name.startswith("search_documents_fts") for name in tables)
        remaining = [row[0] for row in conn.execute("SELECT label_id FROM confirmed_labels")]
    finally:
        conn.close()
    assert remaining == ["lbl-1"]
    assert archive.verify() == []
    archive.close()


#: authorizer 需要关注的"触碰表"动作。
_AUTHORIZER_ACTIONS = {
    name: getattr(sqlite3, name)
    for name in (
        "SQLITE_READ",
        "SQLITE_INSERT",
        "SQLITE_UPDATE",
        "SQLITE_DELETE",
        "SQLITE_CREATE_TABLE",
        "SQLITE_DROP_TABLE",
        "SQLITE_CREATE_VIRTUAL_TABLE",
        "SQLITE_DROP_VIRTUAL_TABLE",
        "SQLITE_ALTER_TABLE",
        "SQLITE_CREATE_INDEX",
        "SQLITE_DROP_INDEX",
    )
    if hasattr(sqlite3, name)
}
_ACTION_NAMES = {value: name.removeprefix("SQLITE_") for name, value in _AUTHORIZER_ACTIONS.items()}


def test_authorizer_proves_all_sql_touches_only_search_tables(tmp_path: Path) -> None:
    """用 SQLite authorizer 实测：索引执行的全部 SQL 只碰 `search_*` 表。

    这比静态分析更强——它看的是**真实执行**的语句，包含 FTS5 的 rebuild/optimize
    与影子表访问。
    """
    root = tmp_path / "store"
    archive, _records = _make_archive(root)
    index = open_index(root)
    touched: list[tuple[str, str]] = []

    def authorizer(action: int, arg1, arg2, dbname, source):  # noqa: ANN001
        if action in _ACTION_NAMES:
            touched.append((_ACTION_NAMES[action], str(arg1 or "")))
        return sqlite3.SQLITE_OK

    try:
        index.connection.set_authorizer(authorizer)
        index.rebuild(
            ArchiveDocumentSource(archive).iter_documents(),
            industry_of={"ch-a": "ai", "ch-b": "garden"},
        )
        index.search(SearchQuery(text="vector database"))
        index.search(SearchQuery(text="bm25", industries=("ai",)))
        index.search(SearchQuery(text="vector", limit=1), boost=lambda hit: 1.0)
        index.get(archive.all_raw_ids()[0])
        index.drop()
    finally:
        index.connection.set_authorizer(None)
        index.close()
        archive.close()

    assert touched, "authorizer 没记录到任何语句，这条测试就是空转"

    def allowed(table: str) -> bool:
        if table == "":
            return True
        if table.startswith(ALLOWED_TABLE_PREFIXES):
            return True
        # 外部内容表的影子表：search_documents_fts_data / _idx / _content / _docsize / _config
        return table.startswith("search_documents_fts")

    bad = [(action, table) for action, table in touched if not allowed(table)]
    assert bad == [], f"索引执行了越界表操作：{bad}"
    # 确实写了自己的表（否则上面的断言可能因为"什么都没写"而恒真）
    assert ("INSERT", "search_documents") in touched
    assert ("DELETE", "search_documents") in touched
    assert any(
        action in ("DROP_TABLE", "DROP_VIRTUAL_TABLE") and table.startswith("search_documents")
        for action, table in touched
    ), f"没有看到索引表的 DROP：{sorted(set(touched))}"

def test_authorizer_rejects_nothing_so_the_index_still_works(tmp_path: Path) -> None:
    """对照：authorizer 全放行时索引功能完好（证明上面的记录不是"因为被拒绝"）。"""
    root = tmp_path / "store"
    archive, _records = _make_archive(root)
    index = open_index(root)
    try:
        index.connection.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        index.rebuild(ArchiveDocumentSource(archive).iter_documents())
        assert index.search(SearchQuery(text="bm25")).total == 2
    finally:
        index.connection.set_authorizer(None)
        index.close()
        archive.close()
