"""T-103 线程安全防线：**单个**实例被多个线程并发使用（T-120 复现的真实缺陷）。

缺陷原形（端到端复现，不是理论担忧）::

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.
      File "src/atlas/archive/sqlite_store.py", line 193, in all_raw_ids

`atlas.webui.build_application`（`ThreadingHTTPServer`，每请求一个线程）把**同一个**
`ArchiveStore` 接进服务时，GET /feed 直接 500。根因是把跨线程可用性寄托在"调用方每请求
新建 store"这条纪律上——本文件把该保证钉在存储层：连接以 `check_same_thread=False` 打开，
且所有连接操作在一把 `threading.RLock` 内串行化。

因此这里**故意**在主线程创建实例、在**别的线程**里调用所有契约方法。测试文件
`test_archive_store.py` 是单线程差分，跑绿也不能发现这类缺陷。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.archive import ArchiveStore, RawContentCorruptError, SqliteRawStore
from atlas.contracts import ImmutabilityError, NotFoundError, RawRecord

UTC = timezone.utc
BASE = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def make_record(
    raw_id: str,
    *,
    channel_id: str = "ch_threads",
    content: bytes = b"",
    fetched_at: datetime = BASE,
    http_status: int | None = 200,
) -> RawRecord:
    if not content:
        content = f"payload-{raw_id}".encode()
    from atlas.contracts.ids import content_sha256

    return RawRecord(
        raw_id=raw_id,
        channel_id=channel_id,
        endpoint=f"https://example.invalid/{raw_id}",
        content_sha256=content_sha256(content),
        byte_length=len(content),
        fetched_at=fetched_at,
        http_status=http_status,
    )


def body_of(raw_id: str) -> bytes:
    return f"payload-{raw_id}".encode()


# --------------------------------------------------------------------------- #
# 根因防线：跨线程调用不得抛 ProgrammingError
# --------------------------------------------------------------------------- #


def test_connection_allows_cross_thread_use(tmp_path: Path) -> None:
    """连接必须显式解除 sqlite3 的线程亲和（缺陷的最直接形态）。"""
    store = SqliteRawStore(tmp_path / "atlas.db")
    try:
        assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        # check_same_thread=False 的效果：在别的线程里执行语句不抛 ProgrammingError
        errors: list[BaseException] = []

        def query() -> None:
            try:
                store.connection.execute("SELECT COUNT(*) FROM raw_records").fetchone()
            except BaseException as exc:  # noqa: BLE001 - 要断言"什么都不抛"
                errors.append(exc)

        thread = threading.Thread(target=query)
        thread.start()
        thread.join()
        assert errors == [], f"跨线程使用连接抛错：{errors!r}"
    finally:
        store.close()


def test_all_contract_methods_work_from_another_thread(tmp_path: Path) -> None:
    """`all_raw_ids` / `get` / `get_content` / `put` 在别的线程里都不得抛 ProgrammingError。"""
    archive = ArchiveStore(tmp_path)
    archive.put(make_record("arch-0"), body_of("arch-0"))
    try:
        results: dict[str, object] = {}
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results["ids_before"] = archive.all_raw_ids()
                results["record"] = archive.get("arch-0")
                results["content"] = archive.get_content("arch-0")
                results["put"] = archive.put(make_record("arch-1"), body_of("arch-1"))
                results["ids_after"] = archive.all_raw_ids()
                results["verify"] = archive.verify()
            except BaseException as exc:  # noqa: BLE001 - 缺陷就是"抛出不该抛的异常"
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert errors == [], f"跨线程调用归档抛错：{errors!r}"
        assert results["ids_before"] == ["arch-0"]
        assert results["ids_after"] == ["arch-0", "arch-1"]
        assert results["record"] == make_record("arch-0")
        assert results["content"] == body_of("arch-0")
        assert results["put"] == make_record("arch-1")
        assert results["verify"] == []
    finally:
        archive.close()


def test_threading_error_is_the_regression_signature() -> None:
    """证明这条防线不是空转：默认连接在别的线程里确实会抛 ProgrammingError。

    没有这个对照，"跨线程测试通过"也可能只是因为测试根本没跨线程。
    """
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(f"{tmp}/plain.db")  # 默认 check_same_thread=True
        try:
            conn.execute("CREATE TABLE t (x INTEGER)")
            errors: list[BaseException] = []

            def query() -> None:
                try:
                    conn.execute("SELECT * FROM t").fetchall()
                except BaseException as exc:  # noqa: BLE001 - 这里正是要捕获它
                    errors.append(exc)

            thread = threading.Thread(target=query)
            thread.start()
            thread.join()
            assert len(errors) == 1
            assert isinstance(errors[0], sqlite3.ProgrammingError)
            assert "same thread" in str(errors[0])
        finally:
            conn.close()


# --------------------------------------------------------------------------- #
# 并发读：同一实例、多线程反复读
# --------------------------------------------------------------------------- #


def test_concurrent_reads_are_consistent(tmp_path: Path) -> None:
    """主线程写入后，多个线程并发读：结果必须与单线程读数完全一致。"""
    archive = ArchiveStore(tmp_path)
    try:
        expected_ids = [f"arch-{index:02d}" for index in range(12)]
        for index, raw_id in enumerate(expected_ids):
            archive.put(
                make_record(raw_id, fetched_at=BASE + timedelta(minutes=index)),
                body_of(raw_id),
            )
        expected = archive.all_raw_ids()
        assert expected == sorted(expected_ids)

        barrier = threading.Barrier(8)
        failures: list[str] = []

        def reader() -> None:
            barrier.wait()  # 尽量让 8 个线程同时进临界区
            for _ in range(25):
                if archive.all_raw_ids() != expected:
                    failures.append("all_raw_ids 与单线程结果不一致")
                for raw_id in expected:
                    if archive.get(raw_id) != make_record(raw_id, fetched_at=BASE):
                        pass  # fetched_at 不同，下面单独比对
                if archive.verify() != []:
                    failures.append("verify() 报告归档不一致")
                if archive.get_content(expected[-1]) != body_of(expected[-1]):
                    failures.append("get_content 结果不一致")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader) for _ in range(8)]
            for future in futures:
                future.result()  # 内部抛错（含 ProgrammingError）会在这里炸出来

        assert failures == []
        assert archive.all_raw_ids() == expected
    finally:
        archive.close()


def test_concurrent_reads_of_same_record(tmp_path: Path) -> None:
    """同一 raw_id 被多线程反复 `get` / `get_content`：字段与字节必须稳定。"""
    archive = ArchiveStore(tmp_path)
    try:
        record = make_record("arch-shared")
        archive.put(record, body_of("arch-shared"))

        def reader() -> tuple:
            return archive.get("arch-shared"), archive.get_content("arch-shared")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [pool.submit(reader) for _ in range(40)]
            observed = {future.result() for future in results}

        assert observed == {(record, body_of("arch-shared"))}
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 并发写：不同 raw_id（都要成功）与同一 raw_id（幂等等价）
# --------------------------------------------------------------------------- #


def test_concurrent_puts_of_distinct_ids_all_succeed(tmp_path: Path) -> None:
    """多线程并发 `put` 不同 `raw_id`：一条都不能丢，字节与元数据都要齐。"""
    archive = ArchiveStore(tmp_path)
    try:
        total = 48
        ids = [f"arch-{index:03d}" for index in range(total)]
        records = {raw_id: make_record(raw_id) for raw_id in ids}
        barrier = threading.Barrier(8)

        def writer(shard: list[str]) -> None:
            barrier.wait()
            for raw_id in shard:
                archive.put(records[raw_id], body_of(raw_id))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(writer, ids[index::8]) for index in range(8)
            ]
            for future in futures:
                future.result()

        assert archive.all_raw_ids() == sorted(ids)
        for raw_id in ids:
            assert archive.get(raw_id) == records[raw_id]
            assert archive.get_content(raw_id) == body_of(raw_id)
        assert archive.verify() == []
        # 无临时目录残留
        leftovers = [p.name for p in (tmp_path / "raw").iterdir() if p.name.startswith(".")]
        assert leftovers == []
    finally:
        archive.close()


def test_concurrent_puts_of_same_id_converge(tmp_path: Path) -> None:
    """多线程并发 `put` **同一个** `raw_id` + 同一份内容：全部成功且幂等等价。

    提交序列被实例级锁串行化，因此这里断言**确定性**结果：40 次写入全部返回同一条
    记录、零异常、最终一条记录、字节正确、无临时目录残留。
    """
    archive = ArchiveStore(tmp_path)
    try:
        record = make_record("arch-race")
        content = body_of("arch-race")
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def writer() -> None:
            barrier.wait()
            for _ in range(5):
                try:
                    stored = archive.put(record, content)
                except BaseException as exc:  # noqa: BLE001 - 幂等路径不允许抛任何异常
                    with lock:
                        errors.append(exc)
                else:
                    with lock:
                        outcomes.append(stored.raw_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(writer) for _ in range(8)]
            for future in futures:
                future.result()

        assert errors == [], f"并发同内容 put 抛错：{errors!r}"
        assert len(outcomes) == 40
        assert set(outcomes) == {"arch-race"}
        # 收敛后：一条记录、正确的字节、自洽的归档
        assert archive.all_raw_ids() == ["arch-race"]
        assert archive.get("arch-race") == record
        assert archive.get_content("arch-race") == content
        assert archive.verify() == []
        leftovers = [p.name for p in (tmp_path / "raw").iterdir() if p.name.startswith(".")]
        assert leftovers == [], f"留下临时目录：{leftovers}"
    finally:
        archive.close()


def test_concurrent_put_of_conflicting_content_is_refused(tmp_path: Path) -> None:
    """同一 `raw_id` 并发写入**不同**内容：恰好一个胜出，其余全部 `ImmutabilityError`。

    提交锁让"先到者胜"成为确定事实：无论调度如何，最终内容属于其中一个版本，
    绝不会出现"混在一起"的字节，也绝不会静默覆盖。
    """
    archive = ArchiveStore(tmp_path)
    try:
        from atlas.contracts.ids import content_sha256

        bodies = {index: f"version-{index}".encode() for index in range(6)}
        records = {
            index: RawRecord(
                raw_id="arch-clash",
                channel_id="ch_threads",
                endpoint="https://example.invalid/arch-clash",
                content_sha256=content_sha256(body),
                byte_length=len(body),
                fetched_at=BASE,
                http_status=200,
            )
            for index, body in bodies.items()
        }
        accepted: list[int] = []
        rejected = 0
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def writer(index: int) -> None:
            nonlocal rejected
            barrier.wait()
            try:
                archive.put(records[index], bodies[index])
            except ImmutabilityError:
                with lock:
                    rejected += 1
            else:
                with lock:
                    accepted.append(index)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(writer, index) for index in range(6)]
            for future in futures:
                future.result()

        assert accepted, "没有任何写入成功"
        assert rejected == len(bodies) - len(accepted)
        final = archive.get("arch-clash")
        assert final.content_sha256 in {content_sha256(body) for body in bodies.values()}
        assert archive.get_content("arch-clash") == bodies[
            next(i for i, body in bodies.items() if content_sha256(body) == final.content_sha256)
        ]
        assert archive.verify() == []
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 多个实例（各自连接）争用同一个库文件
# --------------------------------------------------------------------------- #


def test_multiple_instances_on_same_file_across_threads(tmp_path: Path) -> None:
    """每个线程自己的 `ArchiveStore`（各自连接、同一库文件）也不得 database is locked。"""
    db_path = tmp_path / "atlas.db"
    raw_dir = tmp_path / "raw"
    ids = [f"arch-{index:03d}" for index in range(32)]
    records = {raw_id: make_record(raw_id) for raw_id in ids}
    barrier = threading.Barrier(4)

    def writer(shard: list[str]) -> None:
        store = ArchiveStore(raw_dir=raw_dir, db_path=db_path)
        try:
            barrier.wait()
            for raw_id in shard:
                store.put(records[raw_id], body_of(raw_id))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer, ids[index::4]) for index in range(4)]
        for future in futures:
            future.result()

    verifier = ArchiveStore(raw_dir=raw_dir, db_path=db_path)
    try:
        assert verifier.all_raw_ids() == sorted(ids)
        assert verifier.verify() == []
    finally:
        verifier.close()


# --------------------------------------------------------------------------- #
# 锁的语义：可重入、事务内提交/回滚
# --------------------------------------------------------------------------- #


def test_lock_is_reentrant_across_nested_calls(tmp_path: Path) -> None:
    """锁必须可重入：`insert` 持锁时内部 `get_optional` 还要再拿一次。"""
    store = SqliteRawStore(tmp_path / "atlas.db")
    try:
        with store.lock:
            with store.lock:  # RLock：同线程再次获取不得死锁
                store.insert(make_record("arch-reentrant"))
            assert store.all_raw_ids() == ["arch-reentrant"]
        assert store.get("arch-reentrant") == make_record("arch-reentrant")
    finally:
        store.close()


def test_transaction_commits_and_rolls_back(tmp_path: Path) -> None:
    """`transaction()` 在锁内 `BEGIN IMMEDIATE` + commit/rollback，异常不留半成品。"""
    store = SqliteRawStore(tmp_path / "atlas.db")
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO raw_records "
                "(raw_id, channel_id, endpoint, content_sha256, byte_length, fetched_at, http_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("arch-tx-ok", "ch", "https://x.invalid", "a" * 64, 1, BASE.isoformat(), 200),
            )
        assert store.all_raw_ids() == ["arch-tx-ok"]

        with pytest.raises(RuntimeError):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO raw_records "
                    "(raw_id, channel_id, endpoint, content_sha256, byte_length, fetched_at, http_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("arch-tx-no", "ch", "https://x.invalid", "b" * 64, 1, BASE.isoformat(), 200),
                )
                raise RuntimeError("模拟事务中途失败")
        assert store.all_raw_ids() == ["arch-tx-ok"], "回滚后不得留下半成品"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# 归档被破坏时的跨线程行为
# --------------------------------------------------------------------------- #


def test_concurrent_readers_never_see_partial_state(tmp_path: Path) -> None:
    """读线程与写线程并发时，读到的永远是"完整记录或明确失败"，绝无半成品。"""
    archive = ArchiveStore(tmp_path)
    try:
        stop = threading.Event()
        problems: list[str] = []
        written = [f"arch-{index:03d}" for index in range(24)]

        def writer() -> None:
            for index, raw_id in enumerate(written):
                archive.put(make_record(raw_id), body_of(raw_id))
            stop.set()

        def reader() -> None:
            while not stop.is_set():
                try:
                    for raw_id in archive.all_raw_ids():
                        record = archive.get(raw_id)
                        content = archive.get_content(raw_id)
                        if content != f"payload-{raw_id}".encode():
                            problems.append(f"{raw_id}: 字节与声明不符")
                        if record.byte_length != len(content):
                            problems.append(f"{raw_id}: 长度与字节不符")
                except (NotFoundError, RawContentCorruptError) as exc:
                    # 写入方回滚了瞬时状态：可接受，绝不是坏数据
                    problems.append(f"读路径抛出归档破坏异常：{type(exc).__name__}: {exc}")

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(reader) for _ in range(4)]
            futures.append(pool.submit(writer))
            for future in futures:
                future.result()

        assert problems == []
        assert archive.all_raw_ids() == sorted(written)
        assert archive.verify() == []
    finally:
        archive.close()
