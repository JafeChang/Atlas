"""实验：FTS5 外部内容表在"索引列 ≠ 展示列"时，snippet/highlight 的行为。

在 :memory: 里跑，不碰任何项目数据。
"""

from __future__ import annotations

import sqlite3

TOK = "unicode61 remove_diacritics 2"


def build(schema_fts: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE docs (
            doc_rowid INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            text_index TEXT NOT NULL
        );
        {schema_fts}
        INSERT INTO docs VALUES (1, '中文分词测试与检索 mixed english beta', '中 文 分 词 测 试 与 检 索 mixed english beta');
        INSERT INTO docs VALUES (2, 'alpha beta gamma', 'alpha beta gamma');
        """
    )
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES ('rebuild')")
    return conn


def show(title: str, conn: sqlite3.Connection, sql: str, args: list) -> None:
    print(f"--- {title}")
    print(f"    SQL: {sql}")
    try:
        rows = conn.execute(sql, args).fetchall()
        for row in rows:
            print(f"    -> {row!r}")
    except sqlite3.Error as exc:
        print(f"    !! {type(exc).__name__}: {exc}")


print("=" * 70)
print("A) 单列 text_index")
conn = build(
    f"""CREATE VIRTUAL TABLE docs_fts USING fts5(
        text_index, content='docs', content_rowid='doc_rowid', tokenize="{TOK}");"""
)
show("A snippet col0", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"中 文 分 词"'])
show("A highlight col0", conn, "SELECT highlight(docs_fts, 0, '[', ']') FROM docs_fts WHERE docs_fts MATCH ?", ['"中 文 分 词"'])
show("A snippet latin", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
print("    token dump:")
for row in conn.execute("SELECT * FROM (SELECT 1)").fetchall():
    pass
conn.close()

print("=" * 70)
print("B) text UNINDEXED + text_index")
conn = build(
    f"""CREATE VIRTUAL TABLE docs_fts USING fts5(
        text UNINDEXED, text_index, content='docs', content_rowid='doc_rowid', tokenize="{TOK}");"""
)
show("B snippet col0(text)", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"中 文 分 词"'])
show("B snippet col1(text_index)", conn, "SELECT snippet(docs_fts, 1, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"中 文 分 词"'])
show("B snippet col0 latin", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
show("B snippet col1 latin", conn, "SELECT snippet(docs_fts, 1, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
show("B highlight col0 latin", conn, "SELECT highlight(docs_fts, 0, '[', ']') FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
show("B bm25", conn, "SELECT -bm25(docs_fts) FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
conn.close()

print("=" * 70)
print("C) 索引列名 text_index 但 FTS 列名 text 指向不同内容表列（用视图）")
try:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE docs (
            doc_rowid INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            text_index TEXT NOT NULL
        );
        CREATE VIEW docs_view AS
            SELECT doc_rowid, text AS text, text_index AS text_index FROM docs;
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            text_index, content='docs_view', content_rowid='doc_rowid', tokenize="{TOK}");
        INSERT INTO docs VALUES (1, '中文分词测试 mixed beta', '中 文 分 词 测 试 mixed beta');
        """
    )
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES ('rebuild')")
    show("C snippet", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"中 文"'])
    conn.close()
except sqlite3.Error as exc:
    print(f"    !! {type(exc).__name__}: {exc}")

print("=" * 70)
print("D) 列名不匹配（FTS 列与内容表列名不同的行为）")
try:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        f"""
        CREATE TABLE docs (doc_rowid INTEGER PRIMARY KEY, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            text_index, content='docs', content_rowid='doc_rowid', tokenize="{TOK}");
        INSERT INTO docs VALUES (1, 'alpha beta');
        """
    )
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES ('rebuild')")
    show("D snippet", conn, "SELECT snippet(docs_fts, 0, '[', ']', '…', 8) FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
    show("D match", conn, "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ?", ['"beta"'])
    conn.close()
except sqlite3.Error as exc:
    print(f"    !! {type(exc).__name__}: {exc}")

print("=" * 70)
print("E) SQLite 版本 + FTS5 编译选项")
conn = sqlite3.connect(":memory:")
print("    sqlite:", conn.execute("SELECT sqlite_version()").fetchone()[0])
try:
    for row in conn.execute("SELECT fts5_source_id()").fetchall():
        print("    fts5_source_id:", row[0])
except sqlite3.Error as exc:
    print(f"    fts5_source_id 不可用: {exc}")
conn.close()
