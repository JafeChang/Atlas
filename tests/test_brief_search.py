"""Index persistence/atomicity tests. Real-model full builds are verified separately."""

import hashlib
import sqlite3
import struct
from pathlib import Path

import pytest

from atlas import brief_search_cli as search
from atlas.cognition.briefs import BRIEF_VERSION


def put(source, key, title, summary, *, status="ready", policy=BRIEF_VERSION):
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS feed_articles("
            "source_key TEXT PRIMARY KEY,source_url TEXT,title_zh TEXT,summary_zh TEXT,"
            "updated_at TEXT,policy TEXT,status TEXT,article_text TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO feed_articles VALUES(?,?,?,?,?,?,?,?)",
            (
                key,
                "https://example.test/" + key,
                title,
                summary,
                "2026-10-30T10:00:00+00:00",
                policy,
                status,
                "UNCHANGED BODY",
            ),
        )


class Encoder:
    """Deterministic unit vectors for schema tests; not a language-model substitute."""

    cpus = [0]
    metadata = {
        "model": "test",
        "model_revision": "1",
        "model_sha256": "test",
        "tokenizer_sha256": "test",
        "dimensions": 384,
        "pooling": "test",
        "normalization": "L2",
    }

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.extend(texts)
        result = []
        for text in texts:
            values = [0.0] * 384
            values[0 if "铝" in text else 1] = 1.0
            result.append(struct.pack("<384f", *values))
        return result


def seeded(tmp_path):
    source = tmp_path / "source.sqlite"
    put(source, "alumina", "氧化铝产量", "氧化铝工厂提高产量。")
    put(source, "model", "模型研究", "模型记忆与检索研究。")
    return source, tmp_path / "index.sqlite"


def test_build_round_trip_preserves_source_and_vector_alignment(tmp_path):
    source, target = seeded(tmp_path)
    put(source, "pending", "待生成", "尚未完成。", status="pending")
    put(source, "old-policy", "旧策略", "旧版本简介。", policy="older")
    put(source, "missing-title", "", "缺少译题。")
    before = source.read_bytes()
    encoder = Encoder()
    report = search.build_index(source, target, encoder=encoder, batch_size=1)
    assert report["document_count"] == 2
    assert report["encoded_count"] == 2
    assert source.read_bytes() == before
    assert report["index_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert encoder.calls == [
        "passage: 氧化铝产量\n氧化铝工厂提高产量。",
        "passage: 模型研究\n模型记忆与检索研究。",
    ]
    (hit,) = search.query_index(target, "氧化铝", mode="keyword")
    assert hit["source_key"] == "alumina" and hit["title_zh"] == "氧化铝产量"
    pytest.importorskip("numpy")
    hits = search.query_index(target, "炼铝", encoder=encoder)
    assert hits[0]["source_key"] == "alumina"
    assert hits[0]["score"] == pytest.approx(1)
    assert (
        search.query_index(target, "炼铝", encoder=encoder, allowed_keys={"model"})[0][
            "source_key"
        ]
        == "model"
    )


def test_publish_failure_keeps_previous_index_queryable(tmp_path, monkeypatch):
    source, target = seeded(tmp_path)
    search.build_index(source, target, encoder=Encoder())
    before = target.read_bytes()

    def broken_write(stage, *args):
        Path(stage).write_bytes(b"partial build")
        raise ValueError("injected write failure")

    with monkeypatch.context() as patch:
        patch.setattr(search, "write_index", broken_write)
        with pytest.raises(ValueError, match="injected write failure"):
            search.build_index(source, target, encoder=Encoder())
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".brief-search-*.sqlite"))
    assert (
        search.query_index(target, "氧化铝", mode="keyword")[0]["source_key"]
        == "alumina"
    )


def test_source_cannot_be_used_as_index_destination(tmp_path):
    source, target = seeded(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="不能覆盖"):
        search.build_index(source, source, encoder=Encoder())
    assert source.read_bytes() == before
    assert search.build_index(source, target, encoder=Encoder())["document_count"] == 2


def test_catches_new_briefs_without_encoding_unchanged_rows_again(tmp_path):
    source, target = seeded(tmp_path)

    class UpdatingEncoder(Encoder):
        def encode(self, texts):
            if not self.calls:
                put(source, "new", "新增新闻", "新增中文简介。")
            return super().encode(texts)

    encoder = UpdatingEncoder()
    result = search.build_index(source, target, encoder=encoder)
    assert result["document_count"] == 3 and result["encoded_count"] == 3
    assert len(encoder.calls) == 3
    assert search.query_index(target, "新增", mode="keyword")[0]["source_key"] == "new"


def test_refreshes_changed_vectors_and_removes_no_longer_ready_rows(tmp_path):
    source, target = seeded(tmp_path)

    class UpdatingEncoder(Encoder):
        def encode(self, texts):
            if not self.calls:
                put(source, "model", "铝业模型", "铝业产量预测。")
                put(source, "alumina", "氧化铝产量", "已撤回。", status="pending")
            return super().encode(texts)

    result = search.build_index(source, target, encoder=UpdatingEncoder())
    assert result["document_count"] == 1 and result["encoded_count"] == 3
    assert (
        search.query_index(target, "铝业", mode="keyword")[0]["source_key"] == "model"
    )
    assert search.query_index(target, "氧化铝", mode="keyword") == []


@pytest.mark.parametrize(
    "vector", [b"x", b"\0" * 1536, struct.pack("<384f", float("nan"), *([0] * 383))]
)
def test_invalid_vectors_do_not_replace_valid_index(tmp_path, vector):
    source, target = seeded(tmp_path)
    search.build_index(source, target, encoder=Encoder())
    before = target.read_bytes()

    class InvalidEncoder(Encoder):
        def encode(self, texts):
            return [vector for _ in texts]

    with pytest.raises(ValueError, match="向量"):
        search.build_index(source, target, encoder=InvalidEncoder())
    assert target.read_bytes() == before
    assert (
        search.query_index(target, "模型", mode="keyword")[0]["source_key"] == "model"
    )


def test_query_rejects_mismatched_model_and_count_corruption(tmp_path):
    source, target = seeded(tmp_path)
    search.build_index(source, target, encoder=Encoder())
    pytest.importorskip("numpy")
    wrong = Encoder()
    wrong.metadata = {**wrong.metadata, "model_revision": "different"}
    with pytest.raises(ValueError, match="模型与索引不符"):
        search.query_index(target, "模型", encoder=wrong)
    assert (
        search.query_index(target, "模型", encoder=Encoder())[0]["source_key"]
        == "model"
    )
    with sqlite3.connect(target) as db:
        db.execute("DELETE FROM brief_search_documents WHERE source_key='model'")
    with pytest.raises(ValueError, match="计数"):
        search.index_metadata(target)


def test_half_cpu_budget_is_idempotent(monkeypatch):
    original = set(search.os.sched_getaffinity(0))
    available = original.copy()

    def set_affinity(pid, values):
        available.clear()
        available.update(values)

    monkeypatch.setattr(search.os, "sched_getaffinity", lambda pid: available.copy())
    monkeypatch.setattr(search.os, "sched_setaffinity", set_affinity)
    first = search.restrict_half_cpu()
    assert first and set(first) <= original
    assert search.restrict_half_cpu() == first
