"""`contracts.RawStore`（内存版）与 `archive.ArchiveStore`（文件系统 + sqlite）的**差分测试**。

这是"实现同一契约"的判据（T-103 硬性要求 4）：**同一组场景**分别喂给两个实现，
断言返回值、异常类型、幂等性与不可变性**逐一相同**。

做法：`SCENARIOS` 是一组 `(名称, 调用序列)`；`run_scenario` 把同一序列在两个实现上跑一遍，
产出**可比较的行为轨迹**（值 / 异常类型 / 内容），两条轨迹必须相等。
内存版是参照实现——它若与 sqlite 版不同，就是 sqlite 版错了。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from atlas.archive import ArchiveStore
from atlas.contracts import (
    ImmutabilityError,
    IdError,
    NotFoundError,
    RawRecord,
    RawStore,
)

UTC = timezone.utc
BASE_TIME = datetime(2026, 9, 25, 12, 0, 0, 123456, tzinfo=UTC)

#: 仓库里的共享存储根（SPEC §2.10）。任何归档测试都不得创建/改动它。
REPO_STORE_ROOT = Path("data/store").resolve()


@pytest.fixture(autouse=True)
def _forbid_repo_store_writes():
    """硬性要求 1：测试一律用 `tmp_path`，绝不往仓库 `data/` 写东西。

    在测试前后各检查一次：任何测试若意外用了默认路径，会在这里响亮失败，
    而不是悄悄把 `data/store/atlas.db` 建进工作区。
    """
    before = REPO_STORE_ROOT.exists()
    yield
    assert REPO_STORE_ROOT.exists() == before, (
        f"归档测试改动了仓库共享存储根 {REPO_STORE_ROOT}；测试必须使用 tmp_path"
    )

BODY_A = b"<html><title>Atlas</title><p>alpha</p></html>"
BODY_B = b"<html><title>Atlas</title><p>beta</p></html>"
BODY_EMPTY = b""


def make_record(
    *,
    channel_id: str = "ch_arxiv",
    endpoint: str = "https://example.invalid/feed",
    content: bytes = BODY_A,
    fetched_at: datetime = BASE_TIME,
    http_status: int | None = 200,
    declared_sha256: str | None = None,
    override_raw_id: str | None = None,
) -> RawRecord:
    """造一条 `RawRecord`。

    - `declared_sha256`：伪造"声明指纹"（触发 `IdError` 的场景）
    - `override_raw_id`：复用另一个 id（触发 `ImmutabilityError` 的场景）
    """
    record = RawRecord.create(
        channel_id=channel_id,
        endpoint=endpoint,
        content=content,
        fetched_at=fetched_at,
        http_status=http_status,
    )
    if declared_sha256 is None and override_raw_id is None:
        return record
    return RawRecord(
        raw_id=override_raw_id or record.raw_id,
        channel_id=record.channel_id,
        endpoint=record.endpoint,
        content_sha256=declared_sha256 or record.content_sha256,
        byte_length=record.byte_length,
        fetched_at=record.fetched_at,
        http_status=record.http_status,
    )


def same_raw_id_with(*, content: bytes, fetched_at: datetime = BASE_TIME) -> RawRecord:
    """`RECENT` 之外的**另一份内容**（因此 raw_id 不同，不会触发 IdError 之前的比照）。"""
    return make_record(content=content, fetched_at=fetched_at)


# --------------------------------------------------------------------------- #
# 行为轨迹
# --------------------------------------------------------------------------- #


def _observe(store: RawStore, call: tuple) -> dict:
    """执行一次调用，把结果压成可比对的字典（值或异常类型）。"""
    op = call[0]
    try:
        if op == "put":
            _, record, content = call
            return {"outcome": "ok", "record": _record_snapshot(store.put(record, content))}
        if op == "put_then_get":
            # 验证 put 的返回值与随后 get 到的是同一条记录
            _, record, content = call
            store.put(record, content)
            return {"outcome": "ok", "record": _record_snapshot(store.get(record.raw_id))}
        if op == "get":
            return {"outcome": "ok", "record": _record_snapshot(store.get(call[1]))}
        if op == "get_content":
            return {"outcome": "ok", "content": store.get_content(call[1])}
        if op == "all_raw_ids":
            return {"outcome": "ok", "ids": list(store.all_raw_ids())}
    except Exception as exc:  # noqa: BLE001 - 差分测试把异常类型本身当作观测值
        return {"outcome": "raise", "error": type(exc).__name__}
    raise AssertionError(f"未知操作：{op!r}")


def _record_snapshot(record: RawRecord) -> dict:
    """摊平成字段字典（不依赖 pydantic 的 `__eq__` 细节，逐字段比较）。"""
    return {
        "raw_id": record.raw_id,
        "channel_id": record.channel_id,
        "endpoint": record.endpoint,
        "content_sha256": record.content_sha256,
        "byte_length": record.byte_length,
        "fetched_at_utc_iso": record.fetched_at.astimezone(UTC).isoformat(),
        "http_status": record.http_status,
    }


def run_scenario(scenario: tuple, store: RawStore) -> list:
    return [_observe(store, call) for call in scenario]


# --------------------------------------------------------------------------- #
# 场景（同一组喂给两个实现）
# --------------------------------------------------------------------------- #

RECENT = make_record()
OTHER = make_record(
    channel_id="ch_hn",
    endpoint="https://news.invalid/rss",
    content=BODY_B,
    fetched_at=BASE_TIME + timedelta(hours=3),
    http_status=404,
)
EMPTY = make_record(
    channel_id="ch_x", endpoint="https://x.invalid", content=BODY_EMPTY, http_status=None
)
FORGED = make_record(declared_sha256="0" * 64)

SCENARIOS: list[tuple[str, tuple]] = [
    (
        "空 store：无记录",
        (("all_raw_ids",), ("get", "raw_missing"), ("put", RECENT, BODY_A), ("all_raw_ids",)),
    ),
    (
        "put 新记录：返回值与后续 get 一致",
        (("put_then_get", RECENT, BODY_A), ("get_content", RECENT.raw_id), ("all_raw_ids",)),
    ),
    (
        "幂等：同 raw_id 同内容重复 put",
        (
            ("put", RECENT, BODY_A),
            ("put", RECENT, BODY_A),
            ("put", RECENT, BODY_A),
            ("all_raw_ids",),
            ("get_content", RECENT.raw_id),
        ),
    ),
    (
        "不可变：同 raw_id 不同内容 → ImmutabilityError，旧内容不变",
        (
            ("put", RECENT, BODY_A),
            # 复用 RECENT 的 raw_id、但携带 BODY_B 的内容与指纹 → 通过 IdError 检查后
            # 撞上不可变分支（这正是"同 id 不同内容"的真实形态）
            (
                "put",
                make_record(content=BODY_B, override_raw_id=RECENT.raw_id),
                BODY_B,
            ),
            ("get", RECENT.raw_id),
            ("get_content", RECENT.raw_id),
            ("all_raw_ids",),
        ),
    ),
    (
        "IdError：声明指纹与实际内容不符 → 不落盘",
        (("put", FORGED, BODY_A), ("all_raw_ids",), ("get", FORGED.raw_id)),
    ),
    (
        "多记录：排序、HTTP 状态、空内容共存",
        (
            ("put", RECENT, BODY_A),
            ("put", OTHER, BODY_B),
            ("put", EMPTY, BODY_EMPTY),
            ("all_raw_ids",),
            ("get_content", OTHER.raw_id),
            ("get_content", EMPTY.raw_id),
            ("get", OTHER.raw_id),
            ("get", EMPTY.raw_id),
        ),
    ),
    (
        "NotFoundError：get / get_content 未知 id",
        (
            ("put", RECENT, BODY_A),
            ("get", "raw_nope"),
            ("get_content", "raw_nope"),
            ("all_raw_ids",),
        ),
    ),
    (
        "同 channel/endpoint 不同内容 → 两个 raw_id（新版本而非覆盖）",
        (
            ("put", RECENT, BODY_A),
            ("put", same_raw_id_with(content=BODY_B, fetched_at=BASE_TIME), BODY_B),
            ("all_raw_ids",),
            ("get_content", same_raw_id_with(content=BODY_B).raw_id),
            ("get_content", RECENT.raw_id),
        ),
    ),
    (
        "put 入参对象与返回值：幂等分支返回**已有**记录",
        (
            ("put", RECENT, BODY_A),
            # 第二次用"同样内容但不同 fetched_at/http_status"的记录：raw_id 相同，
            # 内存版返回首次写入的那条，sqlite 版必须一致。
            (
                "put",
                make_record(fetched_at=BASE_TIME + timedelta(days=1), http_status=503),
                BODY_A,
            ),
            ("get", RECENT.raw_id),
            ("all_raw_ids",),
        ),
    ),
]

SCENARIO_IDS = [name for name, _ in SCENARIOS]


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario", [s for _name, s in SCENARIOS], ids=SCENARIO_IDS)
def test_sqlite_matches_in_memory(scenario: tuple, tmp_path: Path) -> None:
    """同一场景在两个实现上的行为轨迹必须完全相同。"""
    memory = RawStore()
    archive = ArchiveStore(tmp_path)
    try:
        expected = run_scenario(scenario, memory)
        actual = run_scenario(scenario, archive)
    finally:
        archive.close()

    assert expected, "场景没有任何观测点"
    assert actual == expected, (
        "同一场景在两个实现上行为不同：\n"
        f"  内存版   = {expected}\n"
        f"  sqlite版 = {actual}"
    )


def test_differential_covers_all_contract_error_types() -> None:
    """差分场景必须真的命中三类契约异常（否则"相同"是空洞的）。"""
    observed: set[str] = set()
    for _name, scenario in SCENARIOS:
        for line in run_scenario(scenario, RawStore()):
            if line["outcome"] == "raise":
                observed.add(line["error"])
    assert {"IdError", "ImmutabilityError", "NotFoundError"} <= observed, observed


def test_both_implementations_raise_the_same_contract_classes(tmp_path: Path) -> None:
    """两个实现抛出的必须是**同一批契约异常类**，不是各自的自定义异常。"""
    memory = RawStore()
    archive = ArchiveStore(tmp_path)
    try:
        for store in (memory, archive):
            with pytest.raises(IdError):
                store.put(FORGED, BODY_A)

        memory.put(RECENT, BODY_A)
        archive.put(RECENT, BODY_A)
        clash = make_record(content=BODY_B, override_raw_id=RECENT.raw_id)
        for store in (memory, archive):
            with pytest.raises(ImmutabilityError):
                store.put(clash, BODY_B)
            with pytest.raises(NotFoundError):
                store.get("raw_missing")
            with pytest.raises(NotFoundError):
                store.get_content("raw_missing")
    finally:
        archive.close()


def test_differential_only_touches_tmp_path(tmp_path: Path) -> None:
    """sqlite 实现只碰 tmp_path 下的文件（默认路径是相对路径，绝不能被走到）。"""
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        assert Path(archive.db_path).resolve().is_relative_to(tmp_path.resolve())
        assert Path(archive.raw_dir).resolve().is_relative_to(tmp_path.resolve())
        assert (tmp_path / "raw" / RECENT.raw_id / "content.bin").is_file()
        assert (tmp_path / "atlas.db").is_file()
    finally:
        archive.close()
    # 仓库共享存储根由 autouse fixture 前后比对（见 `_forbid_repo_store_writes`）


# --------------------------------------------------------------------------- #
# 只增不改：SQL 触发器
# --------------------------------------------------------------------------- #


def test_raw_records_triggers_forbid_update_and_delete(tmp_path: Path) -> None:
    """SPEC §2.10：`raw_records` 的禁改由 SQL 触发器强制，而不是靠调用方约定。"""
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        conn = archive.records.connection

        with pytest.raises(sqlite3.IntegrityError) as update_exc:
            conn.execute(
                "UPDATE raw_records SET endpoint = 'https://tampered.invalid' WHERE raw_id = ?",
                (RECENT.raw_id,),
            )
        assert "append-only" in str(update_exc.value)
        assert "UPDATE" in str(update_exc.value)

        with pytest.raises(sqlite3.IntegrityError) as delete_exc:
            conn.execute("DELETE FROM raw_records WHERE raw_id = ?", (RECENT.raw_id,))
        assert "append-only" in str(delete_exc.value)
        assert "DELETE" in str(delete_exc.value)

        # 触发器只是拒绝：记录必须原封不动
        assert archive.get(RECENT.raw_id) == archive.get(RECENT.raw_id)
        assert archive.get(RECENT.raw_id).endpoint == RECENT.endpoint
        assert archive.all_raw_ids() == [RECENT.raw_id]
    finally:
        archive.close()


def test_triggers_survive_reopen(tmp_path: Path) -> None:
    """触发器写在库里，重开后依然生效（不是只在构造进程里有效）。"""
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
    finally:
        archive.close()

    reopened = ArchiveStore(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            reopened.records.connection.execute("DELETE FROM raw_records")
    finally:
        reopened.close()


def test_no_write_api_for_mutation(tmp_path: Path) -> None:
    """归档存储不提供任何 update/delete 入口；DB 层的 delete 响亮失败。"""
    archive = ArchiveStore(tmp_path)
    try:
        assert not hasattr(archive, "update")
        assert not hasattr(archive, "delete")
        with pytest.raises(NotImplementedError):
            archive.records.delete(RECENT.raw_id)
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 共用的 DB：只加不改，不碰别的域
# --------------------------------------------------------------------------- #

FOREIGN_DDL = """
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY,
    author  TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def test_shared_db_keeps_other_domains_intact(tmp_path: Path) -> None:
    """`data/store/atlas.db` 由多个域共用：本域只建自己的表，不动别人的数据。"""
    db_path = tmp_path / "atlas.db"
    seeded = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        seeded.executescript(FOREIGN_DDL)
        seeded.execute("INSERT INTO store_meta VALUES ('schema_version', '1')")
        seeded.execute("INSERT INTO config_versions VALUES (1, 'alice', '{\"a\":1}')")
    finally:
        seeded.close()

    before = _dump_foreign_tables(db_path)

    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        archive.put(OTHER, BODY_B)
        assert archive.all_raw_ids() == sorted([RECENT.raw_id, OTHER.raw_id])
    finally:
        archive.close()

    assert _dump_foreign_tables(db_path) == before, "不得改动其它域的表与数据"
    reopened = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0]
            for row in reopened.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        reopened.close()
    assert {"store_meta", "config_versions", "raw_records"} <= names


def test_tables_are_created_with_if_not_exists(tmp_path: Path) -> None:
    """DDL 必须幂等：反复构造不报错，也不重建已有表。"""
    for _ in range(3):
        archive = ArchiveStore(tmp_path)
        try:
            archive.put(RECENT, BODY_A)
            assert archive.records.count() == 1
        finally:
            archive.close()


def _dump_foreign_tables(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        result = {}
        for table in ("store_meta", "config_versions"):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            result[table] = [tuple(row) for row in rows]
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 重开一致
# --------------------------------------------------------------------------- #


def test_reopen_is_consistent(tmp_path: Path) -> None:
    """写入后关闭、重新打开：get / get_content / all_raw_ids 与关闭前一致。"""
    first = ArchiveStore(tmp_path)
    try:
        first.put(RECENT, BODY_A)
        first.put(OTHER, BODY_B)
        first.put(EMPTY, BODY_EMPTY)
        before_ids = first.all_raw_ids()
        before_records = {rid: first.get(rid) for rid in before_ids}
        before_contents = {rid: first.get_content(rid) for rid in before_ids}
    finally:
        first.close()

    second = ArchiveStore(tmp_path)
    try:
        assert second.all_raw_ids() == before_ids
        for raw_id in before_ids:
            assert second.get(raw_id) == before_records[raw_id]
            assert second.get_content(raw_id) == before_contents[raw_id]
        assert second.verify() == []
        # 重开后仍然幂等、仍然不可覆盖
        assert second.put(RECENT, BODY_A) == before_records[RECENT.raw_id]
        clash = make_record(content=BODY_B, override_raw_id=RECENT.raw_id)
        with pytest.raises(ImmutabilityError):
            second.put(clash, BODY_B)
        assert second.get_content(RECENT.raw_id) == BODY_A
    finally:
        second.close()


def test_reopen_reads_meta_json_written_by_previous_session(tmp_path: Path) -> None:
    """`meta.json` 是可读副本：重开后仍与 `raw_records` 行一致。"""
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        meta_path = archive.blobs.meta_path(RECENT.raw_id)
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    finally:
        archive.close()

    assert payload["raw_id"] == RECENT.raw_id
    assert payload["content_sha256"] == RECENT.content_sha256
    assert payload["byte_length"] == len(BODY_A)
    assert payload["http_status"] == 200
    assert payload["endpoint"] == RECENT.endpoint

    reopened = ArchiveStore(tmp_path)
    try:
        assert reopened.verify() == []
    finally:
        reopened.close()


def test_binary_content_round_trip(tmp_path: Path) -> None:
    """字节必须原样往返（含 NUL / 非 UTF-8 序列）。"""
    payload = bytes(range(256)) + b"\x00\xff\xfe" + "汉字".encode("utf-8")
    record = make_record(channel_id="ch_bin", endpoint="https://bin.invalid", content=payload)
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(record, payload)
        assert archive.get_content(record.raw_id) == payload
        assert archive.get(record.raw_id).byte_length == len(payload)
        assert archive.verify() == []
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 原子性：DB 与文件系统要么都在、要么都不在
# --------------------------------------------------------------------------- #


def test_metadata_failure_compensates_the_new_blob(tmp_path: Path, monkeypatch) -> None:
    """元数据入库失败 → 补偿删除本次新建的记录目录，回到"两者都没有"。"""
    import atlas.archive.sqlite_store as sqlite_module

    def boom(self, record):  # noqa: ANN001
        raise sqlite3.OperationalError("模拟入库失败：database is locked")

    monkeypatch.setattr(sqlite_module.SqliteRawStore, "insert", boom)
    archive = ArchiveStore(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            archive.put(RECENT, BODY_A)
    finally:
        archive.close()

    assert not (tmp_path / "raw" / RECENT.raw_id).exists(), "失败的写入不得留下记录目录"
    assert list((tmp_path / "raw").iterdir()) == []

    reopened = ArchiveStore(tmp_path)
    try:
        assert reopened.all_raw_ids() == []
        assert reopened.blobs.all_raw_ids() == []
        with pytest.raises(NotFoundError):
            reopened.get(RECENT.raw_id)
    finally:
        reopened.close()


def test_metadata_failure_does_not_delete_preexisting_blob(tmp_path: Path, monkeypatch) -> None:
    """补偿只删**本次新建**的目录：既有记录绝不因别人的失败而消失。"""
    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)  # 既有记录（自愈路径的前置：字节在，行被别人"弄丢"）
    finally:
        archive.close()

    # 人为制造"字节在、行不在"（模拟上次进程在两步之间被强杀）
    conn = sqlite3.connect(str(tmp_path / "atlas.db"), isolation_level=None)
    try:
        conn.execute("DROP TRIGGER trg_raw_records_no_delete")
        conn.execute("DELETE FROM raw_records")
    finally:
        conn.close()

    import atlas.archive.sqlite_store as sqlite_module

    def boom(self, record):  # noqa: ANN001
        raise sqlite3.OperationalError("模拟入库失败：database is locked")

    monkeypatch.setattr(sqlite_module.SqliteRawStore, "insert", boom)
    reopened = ArchiveStore(tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reopened.put(RECENT, BODY_A)
    finally:
        reopened.close()

    # 字节是上次会话写下的，不属于本次调用 → 不得被补偿删除
    assert (tmp_path / "raw" / RECENT.raw_id / "content.bin").read_bytes() == BODY_A


def test_id_error_writes_nothing(tmp_path: Path) -> None:
    """`IdError` 在校验最前，任何一处落盘都不得发生。"""
    archive = ArchiveStore(tmp_path)
    try:
        with pytest.raises(IdError):
            archive.put(FORGED, BODY_A)
        assert not (tmp_path / "raw").exists()
        assert archive.all_raw_ids() == []
        assert archive.verify() == []
    finally:
        archive.close()


def test_failed_write_then_retry_succeeds(tmp_path: Path, monkeypatch) -> None:
    """失败后重试同一份内容必须成功（失败没留下任何阻碍）。"""
    import atlas.archive.blobstore as blobstore_module

    real_write = blobstore_module._write_file_synced
    calls = {"n": 0}

    def flaky(path, data):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("模拟首次写入失败")
        return real_write(path, data)

    monkeypatch.setattr(blobstore_module, "_write_file_synced", flaky)
    archive = ArchiveStore(tmp_path)
    try:
        with pytest.raises(OSError):
            archive.put(RECENT, BODY_A)
        assert archive.all_raw_ids() == []
        assert list((tmp_path / "raw").iterdir()) == []

        stored = archive.put(RECENT, BODY_A)
        assert stored == RECENT
        assert archive.get_content(RECENT.raw_id) == BODY_A
        assert archive.verify() == []
    finally:
        archive.close()


def test_orphan_blob_is_healed_by_reputting_same_record(tmp_path: Path) -> None:
    """孤儿残留（有字节、无元数据行）在同内容重跑 `put` 时自愈。"""
    archive = ArchiveStore(tmp_path)
    try:
        record_dir = archive.blobs.write_new(RECENT.raw_id, BODY_A)
        assert archive.all_raw_ids() == [], "只有字节时不算已归档"
        assert archive.verify() == [f"{RECENT.raw_id}: 文件系统有字节但 raw_records 无行（孤儿残留）"]

        healed = archive.put(RECENT, BODY_A)
        assert healed == RECENT
        assert archive.all_raw_ids() == [RECENT.raw_id]
        assert archive.get_content(RECENT.raw_id) == BODY_A
        assert archive.verify() == []
        # 字节没有被重写（自愈只补元数据行）
        assert record_dir == archive.blobs.record_dir(RECENT.raw_id)
    finally:
        archive.close()


def test_missing_blob_is_reported_loudly(tmp_path: Path) -> None:
    """DB 有行但字节被删 → 不是普通的"查不到"，必须响亮失败。"""
    from atlas.archive import RawContentMissingError

    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        (tmp_path / "raw" / RECENT.raw_id / "content.bin").unlink()

        with pytest.raises(RawContentMissingError) as excinfo:
            archive.get_content(RECENT.raw_id)
        assert isinstance(excinfo.value, NotFoundError)
        with pytest.raises(RawContentMissingError):
            archive.put(RECENT, BODY_A)  # 幂等分支的自检也要发现
        assert archive.verify() != []
    finally:
        archive.close()


def test_corrupted_blob_is_reported_loudly(tmp_path: Path) -> None:
    """字节被改写但元数据未变 → 幂等分支拒绝（不得把坏数据当成"已有记录"）。"""
    from atlas.archive import RawContentCorruptError

    archive = ArchiveStore(tmp_path)
    try:
        archive.put(RECENT, BODY_A)
        (tmp_path / "raw" / RECENT.raw_id / "content.bin").write_bytes(b"tampered")

        # 用同一份内容重跑：指纹不符 → 不得静默走幂等分支
        with pytest.raises(RawContentCorruptError) as excinfo:
            archive.put(RECENT, BODY_A)
        assert isinstance(excinfo.value, ImmutabilityError)
        assert "指纹不符" in str(excinfo.value)
        # 归档不自洽，`verify()` 必须指出来（而不是假装一切正常）
        problems = archive.verify()
        assert problems, "字节被改写却报告归档一致"
        assert "指纹与元数据不符" in problems[0]
    finally:
        archive.close()


# --------------------------------------------------------------------------- #
# 构造与默认布局
# --------------------------------------------------------------------------- #


def test_default_layout_matches_spec(tmp_path: Path, monkeypatch) -> None:
    """省略路径时使用 SPEC §2.10 的默认布局。

    默认布局是**相对路径**，所以在 `tmp_path` 里验证——否则构造动作会把
    `data/store/atlas.db` 建到仓库里（这正是本测试要防的事）。
    """
    import atlas.archive as archive_module

    monkeypatch.chdir(tmp_path)
    with ArchiveStore() as store:
        assert store.raw_dir == Path("data/store/raw")
        assert store.db_path == Path("data/store/atlas.db")
        assert (tmp_path / "data" / "store" / "atlas.db").is_file()
        assert not (tmp_path / "data" / "store" / "raw").exists(), "没有写入就不该有 raw/"

    with archive_module.open_archive() as opened:
        assert opened.raw_dir == Path("data/store/raw")
        assert opened.db_path == Path("data/store/atlas.db")



def test_root_and_explicit_paths_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ArchiveStore(tmp_path, raw_dir=tmp_path / "raw")
    with pytest.raises(ValueError):
        ArchiveStore(tmp_path, db_path=tmp_path / "atlas.db")


def test_explicit_paths_are_honored(tmp_path: Path) -> None:
    archive = ArchiveStore(raw_dir=tmp_path / "blobs", db_path=tmp_path / "meta.db")
    try:
        archive.put(RECENT, BODY_A)
        assert (tmp_path / "blobs" / RECENT.raw_id / "content.bin").is_file()
        assert (tmp_path / "meta.db").is_file()
        assert archive.get_content(RECENT.raw_id) == BODY_A
    finally:
        archive.close()


def test_open_archive_helper(tmp_path: Path) -> None:
    from atlas.archive import open_archive

    with open_archive(tmp_path) as archive:
        archive.put(RECENT, BODY_A)
        assert archive.get(RECENT.raw_id) == RECENT
    with open_archive(tmp_path) as reopened:
        assert reopened.all_raw_ids() == [RECENT.raw_id]

