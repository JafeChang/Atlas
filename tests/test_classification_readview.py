import hashlib
import sqlite3
from dataclasses import replace

import pytest

from atlas.cognition import (
    ProposalRunRow,
    ProposedStoreError,
    SqliteProposedStore,
    read_classifications,
)
from tests.test_propose_store import _classified_row, _unclassified_row


def add_classification(path, unit, *, values=("web",), plan="new-plan", status="classified"):
    with SqliteProposedStore(path) as store:
        for value in values if status == "classified" else (None,):
            row = _classified_row(unit_id=unit.unit_id, value=value, quote=unit.title) if value else _unclassified_row("no_claim_extracted")
            row = replace(
                row, claim_key="", raw_id=unit.raw_id, unit_id=unit.unit_id,
                unit_kind=unit.kind, unit_char_start=unit.char_start, unit_char_end=unit.char_end,
                title=unit.title, plan_digest=plan, model_version=plan,
            )
            store.record_claim(row)
        store.record_run(ProposalRunRow.make(
            unit_id=unit.unit_id, raw_id=unit.raw_id, plan_digest=plan, status=status,
            reason=None if status == "classified" else "no_claim_extracted",
            model=plan, code_version=row.code_version, config_version=row.config_version,
            model_version=plan, label_space_version=row.label_space_version,
        ))


def test_reader_is_read_only_and_latest_failure_hides_old_labels(tmp_path):
    path = tmp_path / "store.db"
    unit = _classified_row()
    unit = type("Unit", (), {"unit_id":unit.unit_id, "raw_id":unit.raw_id, "kind":"entry", "char_start":10, "char_end":60, "title":"test title"})()
    add_classification(path, unit, values=("web", "ai"), plan="old")
    before = hashlib.sha256(path.read_bytes()).digest()
    view = read_classifications(path)[unit.unit_id]
    assert set(view.labels) == {"web", "ai"} and view.origin == "ai"
    assert hashlib.sha256(path.read_bytes()).digest() == before
    add_classification(path, unit, status="unclassified", plan="new")
    view = read_classifications(path)[unit.unit_id]
    assert view.status == "unclassified" and view.labels == () and view.model == "new"


def test_reused_outputs_are_explicitly_historical(tmp_path):
    path = tmp_path / "store.db"
    unit = type("Unit", (), {"unit_id":"entry-reused", "raw_id":"raw-reused", "kind":"entry", "char_start":0, "char_end":60, "title":"same title"})()
    add_classification(path, unit, plan="old")
    add_classification(path, unit, plan="new")
    view = read_classifications(path)[unit.unit_id]
    assert view.labels == ("web",) and view.origin == "history" and view.model == "new"


def test_no_database_is_created_and_partial_schema_fails_loudly(tmp_path):
    path = tmp_path / "missing.db"
    assert read_classifications(path) == {} and not path.exists()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE labels_only (value TEXT)")
    assert read_classifications(path) == {}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE proposal_runs (unit_id TEXT)")
    with pytest.raises(ProposedStoreError, match="不完整"):
        read_classifications(path)
