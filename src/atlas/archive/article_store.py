"""清洗正文与获取快照：正式库 article_contents 表，只增不改。"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ArticleContentRow:
    id: int
    source_url: str
    resolved_url: str
    status: str
    content_kind: str
    cleaned_text: str
    body_chars: int
    cleaner_version: str
    truncated: bool
    failure_code: str
    failure_category: str
    failure_label: str
    failure_detail: str
    retryable: bool
    http_status: int | None
    fetched_at: str
    raw_sha256: str


_COLUMNS = "id,source_url,resolved_url,status,content_kind,{text},body_chars,cleaner_version,truncated,failure_code,failure_category,failure_label,failure_detail,retryable,http_status,fetched_at,raw_sha256"


def read_article_contents(
    path: str | Path, *, include_text: bool = False
) -> dict[str, ArticleContentRow]:
    path = Path(path)
    if not path.is_file():
        return {}
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_contents'"
        ).fetchone():
            return {}
        columns = _COLUMNS.format(text="cleaned_text" if include_text else "''")
        index = ""
        if (
            not include_text
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_article_contents_readview'"
            ).fetchone()
        ):
            index = " INDEXED BY idx_article_contents_readview"
        return {
            row[1]: ArticleContentRow(*row)
            for row in db.execute(
                f"SELECT {columns} FROM article_contents{index} WHERE id IN (SELECT max(id) FROM article_contents GROUP BY source_url)"
            )
        }


def read_article_body(path: str | Path, source_url: str) -> ArticleContentRow | None:
    """按 URL 读取最近一次成功留存的正文；临时失败不抹掉历史正文。"""
    path = Path(path)
    if not path.is_file():
        return None
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    ) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_contents'"
        ).fetchone():
            return None
        row = db.execute(
            f"SELECT {_COLUMNS.format(text='cleaned_text')} FROM article_contents WHERE source_url=? AND status='ready' ORDER BY id DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        return ArticleContentRow(*row) if row else None


def read_article_response(path: str | Path, row_id: int) -> tuple[bytes, str]:
    """只读取得正文快照的原始响应，供阅读页保留 HTML / Markdown 排版。"""
    with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)) as db:
        row = db.execute(
            "SELECT raw_gzip,content_type FROM article_contents WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            raise KeyError(row_id)
        return gzip.decompress(row[0]) if row[0] is not None else b"", row[1]


class ArticleContentStore:
    def __init__(self, path: str | Path):
        self._batch = False
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript("""CREATE TABLE IF NOT EXISTS article_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL,
            resolved_url TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','partial','unavailable')),
            content_kind TEXT NOT NULL CHECK(content_kind IN ('article','paper','abstract','excerpt','unknown')),
            cleaned_text TEXT NOT NULL, body_chars INTEGER NOT NULL,
            cleaned_sha256 TEXT NOT NULL, cleaner_version TEXT NOT NULL,
            truncated INTEGER NOT NULL, failure_code TEXT NOT NULL,
            failure_category TEXT NOT NULL, failure_label TEXT NOT NULL,
            failure_detail TEXT NOT NULL, retryable INTEGER NOT NULL,
            http_status INTEGER, fetched_at TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL, raw_gzip BLOB, content_type TEXT NOT NULL,
            CHECK(body_chars=length(cleaned_text)),
            CHECK(status!='ready' OR length(cleaned_text)>0),
            CHECK(status!='unavailable' OR failure_code!='')
        );
        CREATE INDEX IF NOT EXISTS idx_article_contents_url ON article_contents(source_url,id);
        CREATE INDEX IF NOT EXISTS idx_article_contents_failure ON article_contents(failure_category,id);
        CREATE INDEX IF NOT EXISTS idx_article_contents_readview ON article_contents
            (source_url,id,resolved_url,status,content_kind,body_chars,cleaner_version,truncated,
             failure_code,failure_category,failure_label,failure_detail,retryable,http_status,fetched_at,raw_sha256);
        CREATE TRIGGER IF NOT EXISTS article_contents_no_update BEFORE UPDATE ON article_contents
        BEGIN SELECT RAISE(ABORT,'article_contents is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS article_contents_no_delete BEFORE DELETE ON article_contents
        BEGIN SELECT RAISE(ABORT,'article_contents is append-only'); END;""")

    def record(
        self,
        *,
        source_url: str,
        resolved_url: str = "",
        status: str,
        content_kind: str = "unknown",
        cleaned_text: str = "",
        cleaner_version: str,
        truncated: bool = False,
        failure_code: str = "",
        failure_category: str = "",
        failure_label: str = "",
        failure_detail: str = "",
        retryable: bool = False,
        http_status: int | None = None,
        fetched_at: str | None = None,
        raw_content: bytes = b"",
        content_type: str = "",
    ) -> int:
        if status == "ready" and (
            not cleaned_text.strip() or content_kind not in ("article", "paper")
        ):
            raise ValueError("成功正文必须有文章或论文清洗内容")
        if status == "unavailable" and not failure_code:
            raise ValueError("失败快照必须有明确错误分类")
        if status == "partial" and content_kind not in ("abstract", "excerpt"):
            raise ValueError("部分内容必须明确标记摘要或片段")
        raw_sha = hashlib.sha256(raw_content).hexdigest() if raw_content else ""
        text_sha = hashlib.sha256(cleaned_text.encode()).hexdigest()
        previous = self.db.execute(
            "SELECT id,status,cleaned_sha256,raw_sha256,cleaner_version,failure_code,content_kind FROM article_contents WHERE source_url=? ORDER BY id DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        if previous and previous[1:] == (
            status,
            text_sha,
            raw_sha,
            cleaner_version,
            failure_code,
            content_kind,
        ):
            return previous[0]
        with nullcontext() if self._batch else self.db:
            row = self.db.execute(
                """INSERT INTO article_contents
                (source_url,resolved_url,status,content_kind,cleaned_text,body_chars,cleaned_sha256,
                 cleaner_version,truncated,failure_code,failure_category,failure_label,failure_detail,
                 retryable,http_status,fetched_at,raw_sha256,raw_gzip,content_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_url,
                    resolved_url,
                    status,
                    content_kind,
                    cleaned_text,
                    len(cleaned_text),
                    text_sha,
                    cleaner_version,
                    int(truncated),
                    failure_code,
                    failure_category,
                    failure_label,
                    failure_detail,
                    int(retryable),
                    http_status,
                    fetched_at or datetime.now(UTC).isoformat(),
                    raw_sha,
                    gzip.compress(raw_content, mtime=0) if raw_content else None,
                    content_type,
                ),
            )
        return row.lastrowid

    @contextmanager
    def batch(self):
        """批量导入旧内容一次提交，避免逐行同步拖慢正式库。"""
        if self._batch:
            raise RuntimeError("正文批量事务不可嵌套")
        with self.db:
            self._batch = True
            try:
                yield
            finally:
                self._batch = False

    def raw(self, row_id: int) -> tuple[bytes, str]:
        row = self.db.execute(
            "SELECT raw_gzip,content_type FROM article_contents WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            raise KeyError(row_id)
        return (gzip.decompress(row[0]) if row[0] is not None else b"", row[1])

    def close(self):
        self.db.close()
