"""`archive.BlobStore` 的文件系统语义：目录布局、原子写入、不可覆盖（T-103）。

只测 `BlobStore` 这一层：字节的落盘方式。元数据与契约行为在 `test_archive_store.py`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.archive import BlobStore
from atlas.archive.blobstore import CONTENT_NAME, META_NAME
from atlas.contracts import ImmutabilityError, NotFoundError

RAW_ID = "raw_" + "a" * 32
BODY = b"<html>atlas</html>"
OTHER_BODY = b"<html>atlas v2</html>"


@pytest.fixture()
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "raw")


# --------------------------------------------------------------------------- #
# 目录布局
# --------------------------------------------------------------------------- #


def test_layout_is_raw_id_directory_with_content_bin(blobs: BlobStore) -> None:
    """SPEC §2.10：`<root>/<raw_id>/content.bin`。"""
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    assert blobs.content_path(RAW_ID) == blobs.root / RAW_ID / CONTENT_NAME
    assert blobs.content_path(RAW_ID).read_bytes() == BODY
    assert blobs.meta_path(RAW_ID).read_bytes()


def test_write_new_is_lazy_about_root_directory(tmp_path: Path) -> None:
    """构造 BlobStore 不落盘；只有真写才创建根目录。"""
    root = tmp_path / "not-yet"
    blobs = BlobStore(root)
    assert not root.exists()
    assert blobs.all_raw_ids() == []
    blobs.write_new(RAW_ID, BODY)
    assert root.is_dir()


def test_meta_json_round_trip(blobs: BlobStore) -> None:
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID, "http_status": 200})
    meta = blobs.read_meta(RAW_ID)
    assert meta == {"raw_id": RAW_ID, "http_status": 200}
    assert json.loads(blobs.meta_path(RAW_ID).read_text(encoding="utf-8")) == meta


def test_read_meta_missing_returns_none(blobs: BlobStore) -> None:
    """缺失返回 `None`（不静默造一个空元数据）。"""
    blobs.write_new(RAW_ID, BODY, meta=None)
    assert blobs.read_meta(RAW_ID) is None


def test_read_meta_corrupt_fails_loudly(blobs: BlobStore) -> None:
    """副本损坏必须响亮失败，不得降级成"没有元数据"。"""
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    blobs.meta_path(RAW_ID).write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ImmutabilityError):
        blobs.read_meta(RAW_ID)


# --------------------------------------------------------------------------- #
# 不可覆盖 / 幂等
# --------------------------------------------------------------------------- #


def test_put_is_idempotent_for_identical_bytes(blobs: BlobStore) -> None:
    blobs.put(RAW_ID, BODY)
    before = blobs.content_path(RAW_ID).stat().st_mtime_ns
    directory = blobs.put(RAW_ID, BODY)
    assert directory == blobs.record_dir(RAW_ID)
    assert blobs.content_path(RAW_ID).read_bytes() == BODY
    assert blobs.content_path(RAW_ID).stat().st_mtime_ns == before, "幂等分支不得重写字节"


def test_put_refuses_to_overwrite_with_different_bytes(blobs: BlobStore) -> None:
    blobs.put(RAW_ID, BODY)
    with pytest.raises(ImmutabilityError) as excinfo:
        blobs.put(RAW_ID, OTHER_BODY)
    assert RAW_ID in str(excinfo.value)
    # 原字节必须原封不动
    assert blobs.content_path(RAW_ID).read_bytes() == BODY


def test_write_new_refuses_existing_directory(blobs: BlobStore) -> None:
    blobs.write_new(RAW_ID, BODY)
    with pytest.raises(ImmutabilityError):
        blobs.write_new(RAW_ID, OTHER_BODY)
    assert blobs.content_path(RAW_ID).read_bytes() == BODY


def test_put_rejects_non_bytes(blobs: BlobStore) -> None:
    with pytest.raises(ImmutabilityError):
        blobs.put(RAW_ID, "not bytes")  # type: ignore[arg-type]


def test_get_content_missing_raises_not_found(blobs: BlobStore) -> None:
    with pytest.raises(NotFoundError):
        blobs.get_content("raw_absent")


def test_directory_without_content_bin_fails_loudly(blobs: BlobStore) -> None:
    """人为造出的半成品目录（只有目录、没有 content.bin）不得被当成已有记录。"""
    blobs.record_dir(RAW_ID).mkdir(parents=True)
    assert blobs.exists(RAW_ID) is False
    with pytest.raises(ImmutabilityError):
        blobs.put(RAW_ID, BODY)


@pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", ".hidden"])
def test_invalid_raw_id_is_rejected(blobs: BlobStore, bad_id: str) -> None:
    """`raw_id` 会成为目录名：拒绝空值与路径穿越。"""
    with pytest.raises(NotFoundError):
        blobs.record_dir(bad_id)


# --------------------------------------------------------------------------- #
# 原子性：不留半成品
# --------------------------------------------------------------------------- #


def test_no_leftover_files_after_successful_write(blobs: BlobStore) -> None:
    """成功之后根目录下只有 `<raw_id>/` 一个条目，没有临时残留。"""
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    assert sorted(p.name for p in blobs.root.iterdir()) == [RAW_ID]
    assert sorted(p.name for p in blobs.record_dir(RAW_ID).iterdir()) == sorted(
        [CONTENT_NAME, META_NAME]
    )


def test_failed_write_leaves_no_partial_record(blobs: BlobStore, monkeypatch) -> None:
    """写入中途失败 → 目标目录与临时目录都不留（失败不留半成品）。"""

    def boom(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr("atlas.archive.blobstore._write_file_synced", boom)
    with pytest.raises(OSError):
        blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    assert not blobs.record_dir(RAW_ID).exists()
    assert [p.name for p in blobs.root.iterdir()] == [], "不得残留临时目录"


def test_content_bin_is_never_partially_visible(blobs: BlobStore, monkeypatch) -> None:
    """`os.replace` 之前，目标路径必须不存在——外部看不到半个 content.bin。"""
    import atlas.archive.blobstore as blobstore_module

    real_replace = blobstore_module.os.replace
    seen: list[bool] = []

    def observing_replace(src: str, dst: str) -> None:
        seen.append(Path(dst).exists())
        real_replace(src, dst)

    monkeypatch.setattr(blobstore_module.os, "replace", observing_replace)
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    assert seen == [False], "rename 之前目标目录就已存在，说明不是原子落盘"
    assert blobs.content_path(RAW_ID).read_bytes() == BODY


def test_write_is_observed_as_complete_directory(blobs: BlobStore) -> None:
    """记录目录出现时，两个文件都已就位（目录级原子性）。"""
    blobs.write_new(RAW_ID, BODY, meta={"raw_id": RAW_ID})
    names = sorted(p.name for p in blobs.record_dir(RAW_ID).iterdir())
    assert names == sorted([CONTENT_NAME, META_NAME])


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


def test_all_raw_ids_is_sorted_and_ignores_non_records(blobs: BlobStore) -> None:
    for suffix in ("c", "a", "b"):
        blobs.write_new(f"raw_{suffix * 32}", BODY)
    (blobs.root / "stray_file.txt").write_text("x", encoding="utf-8")
    (blobs.root / "raw_empty_dir").mkdir()
    assert blobs.all_raw_ids() == [f"raw_{s * 32}" for s in ("a", "b", "c")]


def test_all_raw_ids_on_missing_root(tmp_path: Path) -> None:
    assert BlobStore(tmp_path / "nope").all_raw_ids() == []
