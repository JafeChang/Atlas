"""T-108 判据：人工打标的导出 / 导入 / 备份（B7「数据一定要可流转」）。

SPEC §2.3 把 Confirmed 层定为**不可重建**的人工产物：Raw 能重抓、归一化与
Proposed 能重算，人工标签丢了就是丢了。所以这一层的判据不是"有个导出函数"，
而是**导出→回流后逐字段等值**：

- JSON / CSV 两种格式都必须**包含** `label_id` / `raw_id` / `label_key` /
  `label_value` / `actor` / `created_at` / `from_claim_id` / `anchor`（若有）；
- 回流到**全新库**后，每条记录与原对象 `model_dump()` 逐字段相同
  （含 `created_at` 的时区、`from_claim_id`、锚点四元组）；
- 回流**幂等**：同一份导出重复导入不产生重复记录；
- 回流目标是**契约层的 store**：JSON 格式不绑定 SQLite（内存版 `ConfirmedStore` 也能吃）；
- 坏文件**响亮失败**（`atlas.contracts` 的领域异常），绝不跳过坏行、绝不补默认值——
  静默丢数据是不可重建层最不能接受的失败模式。

测试一律用 `tmp_path`。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List

import pytest

from atlas.contracts import (
    ConfirmedLabel,
    ConfirmedStore,
    EvidenceAnchor,
    IdError,
    InvalidContractStateError,
    ProposedClaim,
    TaskVersions,
    VerificationStatus,
    VersionError,
    content_sha256,
    label_id_for,
    raw_id_for,
)
from atlas.labels import (
    CSV_COLUMNS,
    EXPORT_FORMAT,
    EXPORT_FORMAT_VERSION,
    SqliteConfirmedStore,
    backup,
    dumps_json,
    export_csv,
    export_json,
    import_csv,
    import_json,
    label_from_dict,
    label_to_dict,
    load_csv,
    load_json,
    open_store,
    restore,
    verify_label_id,
)
from atlas.labels.sqlite_store import TABLE_NAME

UTC = timezone.utc
T0 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
RAW_A = raw_id_for("ch_a", "https://example.com/a", content_sha256(b"body-a"))
RAW_B = raw_id_for("ch_b", "https://example.com/b", content_sha256(b"body-b"))
RAW_Z = raw_id_for("ch_z", "https://example.com/z", content_sha256(b"body-z"))
SHA_Z = content_sha256(b"body-z")
VERSIONS = TaskVersions(code_version="code-1", config_version="cfg-1", model_version="m-1")

#: 判据要求的字段集（缺一不可）。
REQUIRED_FIELDS = {
    "label_id",
    "raw_id",
    "label_key",
    "label_value",
    "actor",
    "created_at",
    "from_claim_id",
    "anchor",
}

TRICKY_VALUE = 'line1,"quoted"\nline2 — 含中文与逗号'


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def make_label(
    *,
    raw_id: str,
    key: str,
    value: str,
    actor: str,
    at: datetime,
    from_claim_id: str | None = None,
    anchor: EvidenceAnchor | None = None,
) -> ConfirmedLabel:
    return ConfirmedLabel(
        label_id=label_id_for(raw_id, key, value, actor),
        raw_id=raw_id,
        label_key=key,
        label_value=value,
        actor=actor,
        from_claim_id=from_claim_id,
        anchor=anchor,
        created_at=at,
    )


def confirmed_from_claim(at: datetime) -> ConfirmedLabel:
    """一条"确认 AI 提议"的标签：带 `from_claim_id` 与锚点。

    契约的 `from_proposal()` 用 `_utcnow()` 当 `created_at`，这里按字段重建把时间钉死
    （改动只发生在测试里，不动契约）。
    """
    claim = ProposedClaim.propose(
        raw_id=RAW_Z,
        kind="industry",
        value="cs.CV",
        quote="本文提出了一种新的视觉方法",
        confidence=0.87,
        versions=VERSIONS,
    )
    anchor = EvidenceAnchor.create(
        raw_id=RAW_Z, raw_sha256=SHA_Z, char_start=4, char_end=17
    )
    verified = claim.with_verification(VerificationStatus.VERIFIED, anchor)
    payload = ConfirmedLabel.from_proposal(verified, actor="alice").model_dump()
    payload["created_at"] = at
    return ConfirmedLabel(**payload)


@pytest.fixture()
def seeded(tmp_path: Path) -> SqliteConfirmedStore:
    """一份有代表性的库：多文档 / 多维度 / 多作者 / 带锚点 / 难缠字符。"""
    store = SqliteConfirmedStore(db_path=tmp_path / "seed.db")
    store.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
    store.add(
        make_label(
            raw_id=RAW_A,
            key="topic",
            value="机器学习，入门",
            actor="alice",
            at=T0 + timedelta(hours=1),
        )
    )
    store.add(
        make_label(raw_id=RAW_B, key="industry", value="cs.CL", actor="bob", at=T0 + timedelta(hours=2))
    )
    store.add(
        make_label(
            raw_id=RAW_B,
            key="note",
            value=TRICKY_VALUE,
            actor="bob",
            at=T0 + timedelta(hours=3),
        )
    )
    store.add(confirmed_from_claim(at=T0 + timedelta(hours=4)))
    yield store
    store.close()


def _sorted_dumps(labels: List[ConfirmedLabel]) -> list[dict[str, Any]]:
    return [
        label.model_dump(mode="json")
        for label in sorted(labels, key=lambda item: item.label_id)
    ]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. JSON：字段完整 + 可回流 + 幂等
# --------------------------------------------------------------------------- #
def test_json_document_shape(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_json(seeded, tmp_path / "out.json", exported_at=T0)
    document = _read_json(path)

    assert document["format"] == EXPORT_FORMAT
    assert document["format_version"] == EXPORT_FORMAT_VERSION
    assert document["exported_at"] == T0.isoformat()
    assert document["count"] == seeded.count() == 5
    assert len(document["labels"]) == 5

    for entry in document["labels"]:
        assert set(entry) == REQUIRED_FIELDS, "导出字段集必须与判据一致（不多不少）"

    anchored = [entry for entry in document["labels"] if entry["anchor"] is not None]
    assert len(anchored) == 1
    assert anchored[0]["from_claim_id"] is not None
    assert set(anchored[0]["anchor"]) == {"raw_id", "raw_sha256", "char_start", "char_end"}
    assert anchored[0]["anchor"]["raw_id"] == RAW_Z

    human = [entry for entry in document["labels"] if entry["raw_id"] == RAW_A]
    assert all(entry["anchor"] is None for entry in human), "1A：人工直判没有锚点"


def test_json_export_is_deterministic_and_preserves_write_order(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    first = export_json(seeded, tmp_path / "a.json", exported_at=T0).read_text(encoding="utf-8")
    second = export_json(seeded, tmp_path / "b.json", exported_at=T0).read_text(encoding="utf-8")
    assert first == second, "同输入 + 同 exported_at 必须逐字节相同（可 diff、可校验）"

    ids = [entry["label_id"] for entry in _read_json(tmp_path / "a.json")["labels"]]
    assert ids == [label.label_id for label in seeded.all_labels()], (
        "导出必须保持写入顺序：created_at 并列时 latest_value 取先写入者，"
        "排序会改变并列语义，备份就不再等价于原库"
    )


def test_json_roundtrip_into_a_fresh_store_is_field_identical(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    path = export_json(seeded, tmp_path / "export.json", exported_at=T0)
    fresh = SqliteConfirmedStore(db_path=tmp_path / "fresh.db")
    try:
        processed = import_json(fresh, path)
        assert processed == 5
        assert fresh.count() == seeded.count()
        assert _sorted_dumps(fresh.all_labels()) == _sorted_dumps(seeded.all_labels())
        for raw_id in (RAW_A, RAW_B, RAW_Z):
            assert fresh.all_for(raw_id) == seeded.all_for(raw_id)
        assert fresh.latest_value(RAW_A, "industry") == "cs.LG"
        assert fresh.latest_value(RAW_B, "note") == TRICKY_VALUE
        assert fresh.latest_value(RAW_Z, "industry") == "cs.CV"
    finally:
        fresh.close()


def test_json_import_is_idempotent(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_json(seeded, tmp_path / "export.json", exported_at=T0)
    fresh = SqliteConfirmedStore(db_path=tmp_path / "idem.db")
    try:
        assert import_json(fresh, path) == 5
        before = _sorted_dumps(fresh.all_labels())
        assert import_json(fresh, path) == 5
        assert fresh.count() == 5, "重复导入不得产生重复记录（label_id 内容寻址）"
        assert _sorted_dumps(fresh.all_labels()) == before
    finally:
        fresh.close()


def test_load_json_matches_stored_records(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_json(seeded, tmp_path / "export.json", exported_at=T0)
    assert _sorted_dumps(load_json(path)) == _sorted_dumps(seeded.all_labels())


def test_json_flows_into_the_contract_level_store(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    """导出格式不绑定 SQLite：内存契约实现同样能回流（迁移/测试的关键便利）。"""
    path = export_json(seeded, tmp_path / "export.json", exported_at=T0)
    memory = ConfirmedStore()
    assert import_json(memory, path) == 5
    assert memory.count() == 5
    assert memory.latest_value(RAW_B, "note") == TRICKY_VALUE
    assert [lbl.label_value for lbl in memory.all_for(RAW_Z)] == ["cs.CV"]


def test_export_accepts_store_or_iterable_equivalently(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    from_store = export_json(seeded, tmp_path / "store.json", exported_at=T0)
    from_list = export_json(seeded.all_labels(), tmp_path / "list.json", exported_at=T0)
    assert _read_json(from_store) == _read_json(from_list)
    assert dumps_json(seeded, exported_at=T0) == dumps_json(seeded.all_labels(), exported_at=T0)


def test_label_store_wrapper_exports(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    with open_store(tmp_path / "wrapper.db") as store:
        store.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
        path = store.to_json(tmp_path / "wrapper.json", exported_at=T0)
        assert len(_read_json(path)["labels"]) == 1
        assert store.import_from_json(path) == 1
        assert store.count() == 1


# --------------------------------------------------------------------------- #
# 2. CSV：表格可读 + 锚点摊平 + 可回流
# --------------------------------------------------------------------------- #
def test_csv_headers_and_anchor_flattening(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_csv(seeded, tmp_path / "out.csv")
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
    assert list(rows[0].keys()) == list(CSV_COLUMNS)
    assert len(rows) == 5

    anchored = [row for row in rows if row["anchor_raw_id"]]
    assert len(anchored) == 1
    assert anchored[0]["anchor_raw_id"] == RAW_Z
    assert anchored[0]["anchor_raw_sha256"] == SHA_Z
    assert anchored[0]["anchor_char_start"] == "4"
    assert anchored[0]["anchor_char_end"] == "17"
    assert anchored[0]["from_claim_id"]
    assert anchored[0]["label_key"] == "industry"

    human = [row for row in rows if row["raw_id"] == RAW_A]
    assert all(row["anchor_raw_id"] == "" for row in human)
    assert all(row["from_claim_id"] == "" for row in human)


def test_csv_roundtrip_is_field_identical(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_csv(seeded, tmp_path / "export.csv")
    fresh = SqliteConfirmedStore(db_path=tmp_path / "csv-fresh.db")
    try:
        assert import_csv(fresh, path) == 5
        assert _sorted_dumps(fresh.all_labels()) == _sorted_dumps(seeded.all_labels())
        assert fresh.latest_value(RAW_B, "note") == TRICKY_VALUE, "CSV 转义必须无损"
        assert fresh.latest_value(RAW_Z, "industry") == "cs.CV"
    finally:
        fresh.close()


def test_csv_import_is_idempotent(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_csv(seeded, tmp_path / "export.csv")
    fresh = SqliteConfirmedStore(db_path=tmp_path / "csv-idem.db")
    try:
        import_csv(fresh, path)
        import_csv(fresh, path)
        assert fresh.count() == 5
    finally:
        fresh.close()


def test_load_csv_matches_stored_records(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_csv(seeded, tmp_path / "export.csv")
    assert _sorted_dumps(load_csv(path)) == _sorted_dumps(seeded.all_labels())


# --------------------------------------------------------------------------- #
# 3. 备份 / 恢复
# --------------------------------------------------------------------------- #
def test_backup_is_timestamped_and_restorable(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    target = backup(seeded, tmp_path / "backups", now=T0)
    assert target.name == "confirmed_labels-20260201T090000Z.json"
    assert target.exists()

    fresh = SqliteConfirmedStore(db_path=tmp_path / "restored.db")
    try:
        assert restore(fresh, target) == 5
        assert _sorted_dumps(fresh.all_labels()) == _sorted_dumps(seeded.all_labels())
    finally:
        fresh.close()


def test_backup_never_overwrites_a_previous_backup(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    first = backup(seeded, tmp_path / "backups", now=T0)
    second = backup(seeded, tmp_path / "backups", now=T0 + timedelta(hours=1))
    assert first != second
    assert first.exists() and second.exists(), "备份是不可重建层唯一的退路，不得互相覆盖"


def test_backup_restores_tie_break_semantics(tmp_path: Path) -> None:
    """并列时间戳下的 `latest_value` 也要能原样还原——这正是导出必须保序的理由。"""
    original = SqliteConfirmedStore(db_path=tmp_path / "tie.db")
    original.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
    original.add(make_label(raw_id=RAW_A, key="industry", value="cs.CV", actor="bob", at=T0))
    expected = original.latest_value(RAW_A, "industry")
    target = backup(original, tmp_path / "backups", now=T0)
    original.close()

    restored = SqliteConfirmedStore(db_path=tmp_path / "tie-restored.db")
    try:
        restore(restored, target)
        assert expected == "cs.LG"
        assert restored.latest_value(RAW_A, "industry") == expected
        assert [lbl.label_value for lbl in restored.all_for(RAW_A)] == ["cs.LG", "cs.CV"]
    finally:
        restored.close()


def test_restore_dispatches_by_suffix(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    json_path = export_json(seeded, tmp_path / "x.json", exported_at=T0)
    csv_path = export_csv(seeded, tmp_path / "x.csv")
    for path in (json_path, csv_path):
        fresh = SqliteConfirmedStore(db_path=tmp_path / f"d-{path.suffix}.db")
        try:
            assert restore(fresh, path) == 5
        finally:
            fresh.close()


def test_restore_rejects_unknown_suffix(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    target = tmp_path / "labels.txt"
    target.write_text("nope", encoding="utf-8")
    with pytest.raises(InvalidContractStateError):
        restore(seeded, target)


# --------------------------------------------------------------------------- #
# 4. 空库也是合法状态（不得把"没有标签"和"文件坏了"混为一谈）
# --------------------------------------------------------------------------- #
def test_empty_store_exports_a_valid_empty_document(tmp_path: Path) -> None:
    empty = SqliteConfirmedStore(db_path=tmp_path / "empty.db")
    try:
        json_path = export_json(empty, tmp_path / "empty.json", exported_at=T0)
        document = _read_json(json_path)
        assert document["count"] == 0
        assert document["labels"] == []

        csv_path = export_csv(empty, tmp_path / "empty.csv")
        assert csv_path.read_text(encoding="utf-8") == ",".join(CSV_COLUMNS) + "\n"

        fresh = SqliteConfirmedStore(db_path=tmp_path / "empty-import.db")
        try:
            assert import_json(fresh, json_path) == 0
            assert import_csv(fresh, csv_path) == 0
            assert fresh.count() == 0
        finally:
            fresh.close()
    finally:
        empty.close()


# --------------------------------------------------------------------------- #
# 5. 坏文件必须响亮失败（不跳过、不补默认值）
# --------------------------------------------------------------------------- #
def _write(document: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_import_rejects_tampered_label_id(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    document = _read_json(export_json(seeded, tmp_path / "ok.json", exported_at=T0))
    document["labels"][0]["label_id"] = "lbl_hand_edited"
    bad = _write(document, tmp_path / "tampered.json")

    fresh = SqliteConfirmedStore(db_path=tmp_path / "tampered-import.db")
    try:
        with pytest.raises(IdError):
            import_json(fresh, bad)
        assert fresh.count() == 0, "校验失败不得留下已写入的部分记录"
    finally:
        fresh.close()


def test_import_rejects_missing_field(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    document = _read_json(export_json(seeded, tmp_path / "ok.json", exported_at=T0))
    del document["labels"][0]["actor"]
    bad = _write(document, tmp_path / "missing.json")
    with pytest.raises(InvalidContractStateError) as err:
        load_json(bad)
    assert "actor" in str(err.value)


def test_import_rejects_unknown_format_version(
    seeded: SqliteConfirmedStore, tmp_path: Path
) -> None:
    document = _read_json(export_json(seeded, tmp_path / "ok.json", exported_at=T0))
    document["format_version"] = EXPORT_FORMAT_VERSION + 99
    bad = _write(document, tmp_path / "future.json")
    with pytest.raises(VersionError):
        load_json(bad)


def test_import_rejects_foreign_or_unmarked_document(tmp_path: Path) -> None:
    for name, document in (
        ("foreign.json", {"hello": "world"}),
        ("list.json", [1, 2, 3]),
    ):
        bad = _write(document, tmp_path / name)  # type: ignore[arg-type]
        with pytest.raises(InvalidContractStateError):
            load_json(bad)


def test_import_rejects_count_mismatch(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    document = _read_json(export_json(seeded, tmp_path / "ok.json", exported_at=T0))
    document["count"] = 4
    bad = _write(document, tmp_path / "short.json")
    with pytest.raises(InvalidContractStateError):
        load_json(bad)


def test_import_rejects_broken_json(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text('{"format": "atlas.confirmed_labels",', encoding="utf-8")
    with pytest.raises(InvalidContractStateError):
        load_json(bad)


def test_csv_import_rejects_partial_anchor(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    """只有区间一端的锚点必须被拒绝（`export_csv` 自己永远写不出这种行）。"""
    values = {
        "label_id": "lbl_partial",
        "raw_id": RAW_A,
        "label_key": "industry",
        "label_value": "cs.LG",
        "actor": "alice",
        "created_at": T0.isoformat(),
        "from_claim_id": "",
        "anchor_raw_id": RAW_A,
        "anchor_raw_sha256": SHA_Z,
        "anchor_char_start": "4",
        "anchor_char_end": "",  # ← 半截锚点
    }
    bad = tmp_path / "partial.csv"
    bad.write_text(
        ",".join(CSV_COLUMNS) + "\n" + ",".join(values[col] for col in CSV_COLUMNS) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InvalidContractStateError) as err:
        load_csv(bad)
    assert "锚点" in str(err.value)


def test_csv_import_rejects_missing_columns(tmp_path: Path) -> None:
    bad = tmp_path / "narrow.csv"
    bad.write_text("label_id,raw_id\nlbl_x,raw_y\n", encoding="utf-8")
    with pytest.raises(InvalidContractStateError) as err:
        load_csv(bad)
    assert "表头" in str(err.value)


def test_import_target_must_expose_add(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    path = export_json(seeded, tmp_path / "ok.json", exported_at=T0)
    with pytest.raises(InvalidContractStateError):
        import_json(object(), path)
    with pytest.raises(InvalidContractStateError):
        import_csv("not-a-store", path)


# --------------------------------------------------------------------------- #
# 6. 导出侧同样不放过坏记录
# --------------------------------------------------------------------------- #
def test_export_rejects_a_forged_label_in_the_source(tmp_path: Path) -> None:
    forged = ConfirmedLabel(
        label_id="lbl_forged",
        raw_id=RAW_A,
        label_key="industry",
        label_value="cs.LG",
        actor="alice",
        created_at=T0,
    )
    with pytest.raises(IdError):
        export_json([forged], tmp_path / "forged.json")
    assert not (tmp_path / "forged.json").exists(), "拒绝必须发生在落盘之前"


def test_export_rejects_non_label_sources(seeded: SqliteConfirmedStore, tmp_path: Path) -> None:
    with pytest.raises(InvalidContractStateError):
        export_json(seeded.all_labels() + ["not-a-label"], tmp_path / "mixed.json")  # type: ignore[list-item]
    with pytest.raises(InvalidContractStateError):
        export_json(seeded.all_labels()[0], tmp_path / "single.json")
    with pytest.raises(InvalidContractStateError):
        export_json(12345, tmp_path / "number.json")  # type: ignore[arg-type]


def test_verify_label_id_accepts_genuine_records() -> None:
    label = ConfirmedLabel.human(
        raw_id=RAW_A, label_key="industry", label_value="cs.LG", actor="alice"
    )
    verify_label_id(label)  # 不抛即通过


def test_label_to_dict_and_from_dict_roundtrip() -> None:
    label = confirmed_from_claim(at=T0)
    payload = label_to_dict(label)
    assert set(payload) == REQUIRED_FIELDS
    assert label_from_dict(payload) == label, "字典 ↔ 记录必须无损（含锚点与时间）"


# --------------------------------------------------------------------------- #
# 7. 导出不碰别人的表 / 不写仓库 data/
# --------------------------------------------------------------------------- #
def test_export_does_not_add_tables(tmp_path: Path) -> None:
    store = SqliteConfirmedStore(db_path=tmp_path / "keep.db")
    try:
        store.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
        export_json(store, tmp_path / "out.json", exported_at=T0)
        export_csv(store, tmp_path / "out.csv")
        backup(store, tmp_path / "backups", now=T0)
        names = {
            str(row["name"])
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        store.close()
    assert names == {TABLE_NAME}, "导出路径不得顺手建表"


def test_export_and_backup_stay_out_of_the_repo_data_dir(tmp_path: Path) -> None:
    repo_data = Path(__file__).resolve().parents[1] / "data"
    before = sorted(str(p) for p in repo_data.rglob("*")) if repo_data.exists() else None

    store = SqliteConfirmedStore(db_path=tmp_path / "guard.db")
    try:
        store.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
        export_json(store, tmp_path / "o.json", exported_at=T0)
        export_csv(store, tmp_path / "o.csv")
        backup(store, tmp_path / "backups", now=T0)
    finally:
        store.close()

    after = sorted(str(p) for p in repo_data.rglob("*")) if repo_data.exists() else None
    assert after == before


def test_connects_to_a_shared_db_without_touching_foreign_tables(tmp_path: Path) -> None:
    """同一份 `atlas.db` 上与其它域共存：本模块只读写 `confirmed_labels`。"""
    db = tmp_path / "shared.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE raw_records (raw_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO raw_records(raw_id) VALUES (?)", (RAW_A,))
        conn.commit()

    store = SqliteConfirmedStore(db_path=db)
    try:
        store.add(make_label(raw_id=RAW_A, key="industry", value="cs.LG", actor="alice", at=T0))
        export_json(store, tmp_path / "shared.json", exported_at=T0)
        raws = store.connection.execute("SELECT raw_id FROM raw_records").fetchall()
    finally:
        store.close()
    assert [str(row["raw_id"]) for row in raws] == [RAW_A]
