import sqlite3
import threading
from contextlib import closing

import pytest

from atlas.archive import enable_wal
from tests.test_webui_entries_render import _get, running_frontend


def test_wal_is_persistent_preserves_rows_and_does_not_create_missing_db(tmp_path):
    path = tmp_path / "atlas.db"
    with pytest.raises(FileNotFoundError):
        enable_wal(path)
    assert not path.exists()
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE probe(value TEXT)")
        db.execute("INSERT INTO probe VALUES (?)", ("原始内容",))
        db.commit()
    enable_wal(path)
    enable_wal(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT value FROM probe").fetchall() == [("原始内容",)]


def test_rollback_mode_reproduces_blocked_reader_and_wal_removes_the_block(tmp_path):
    path = tmp_path / "atlas.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE probe(value INTEGER)")
        writer.execute("INSERT INTO probe VALUES (1)")
        writer.commit()
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE probe SET value=2")
        with closing(sqlite3.connect(path, timeout=0.05)) as reader:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                reader.execute("SELECT value FROM probe").fetchall()
            writer.rollback()
            assert reader.execute("SELECT value FROM probe").fetchone() == (1,)
    enable_wal(path)
    with (
        closing(sqlite3.connect(path)) as writer,
        closing(sqlite3.connect(path, timeout=0.05)) as reader,
    ):
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("UPDATE probe SET value=2")
        assert reader.execute("SELECT value FROM probe").fetchone() == (1,)
        writer.commit()
        assert reader.execute("SELECT value FROM probe").fetchone() == (2,)


def test_feed_stays_readable_while_background_writer_has_not_committed(tmp_path):
    with running_frontend(tmp_path) as world:
        enable_wal(world.db_path)
        with closing(sqlite3.connect(world.db_path)) as db:
            db.execute("CREATE TABLE probe(value INTEGER)")
            db.execute("INSERT INTO probe VALUES (1)")
            db.commit()
        started, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        failures = []

        def write():
            try:
                with closing(sqlite3.connect(world.db_path)) as db:
                    db.execute("BEGIN EXCLUSIVE")
                    db.execute("UPDATE probe SET value=2")
                    started.set()
                    if not release.wait(8):
                        raise AssertionError(
                            "reader waited for writer instead of reading concurrently"
                        )
                    db.commit()
            except Exception as error:
                failures.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=write)
        worker.start()
        try:
            assert started.wait(3)
            status, page, _ = _get(world.app, "/feed?limit=3")
            assert status == 200 and "Alpha" in page
            assert not finished.is_set(), "读取必须在写事务结束前完成"
        finally:
            release.set()
            worker.join(10)
        assert not failures and not worker.is_alive()
        with closing(sqlite3.connect(world.db_path)) as db:
            assert db.execute("SELECT value FROM probe").fetchone() == (2,)
