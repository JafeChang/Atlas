"""原文字节的**文件系统**存储（SPEC §2.10 目录布局）。

布局::

    <raw_dir>/<raw_id>/content.bin    原始字节（事实来源）
    <raw_dir>/<raw_id>/meta.json      元数据的可读副本（冗余，可重建）

**原子性策略（失败不留半成品）**

1. 先在同一下级目录写临时**目录** `<raw_id>.tmp-<pid>-<n>/`，把 `content.bin`
   与 `meta.json` 都写进去并 `fsync`；
2. 最后用 `os.replace(tmp_dir, <raw_id>/)` 把整个目录**原子 rename** 到位。
   同文件系统内 `rename` 是原子操作，因此外部永远看不到"半个 `content.bin`"——
   要么目录不存在，要么目录内两个文件都完整；
3. 任何异常路径（含写入中途抛错）都会在 `finally` 里 `shutil.rmtree` 掉临时目录；
4. 目录已存在时**绝不覆盖**：先读回已有 `content.bin` 比对 sha256，
   相同则幂等返回，不同则抛 `ImmutabilityError`（SPEC §2.3「Raw 只增不改」）。

`meta.json` 是**冗余的可读副本**，不是事实来源：真值在 SQLite 的 `raw_records`
与 `content.bin` 的字节里。它的存在是为了让 `data/store/raw/` 单独可读、可迁移
（SPEC §2.10 理由 5）。因此本模块不提供任何修改或删除既有记录的入口。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Union

from atlas.contracts import ImmutabilityError, NotFoundError, RawRecord
from atlas.contracts.ids import content_sha256

__all__ = ["CONTENT_NAME", "DEFAULT_RAW_DIR", "META_NAME", "BlobStore"]

#: SPEC §2.10 的目录布局：`data/store/raw/<raw_id>/`。
DEFAULT_RAW_DIR = Path("data/store/raw")

#: 事实来源：原始字节。
CONTENT_NAME = "content.bin"

#: 冗余的可读元数据副本。
META_NAME = "meta.json"

_PathLike = Union[str, Path]


class BlobStore:
    """`<raw_id>` → 原始字节的不可变文件存储。

    只负责字节；元数据真值在 `atlas.archive.sqlite_store.SqliteRawStore`。
    本类**不做** SQLite 交互，因此可独立测试。
    """

    def __init__(self, root: Optional[_PathLike] = None) -> None:
        self._root = DEFAULT_RAW_DIR if root is None else Path(root)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    @property
    def root(self) -> Path:
        """存储根目录（`<root>/<raw_id>/`）。目录**懒创建**，构造时不落盘。"""
        return self._root

    def record_dir(self, raw_id: str) -> Path:
        return self._root / _check_raw_id(raw_id)

    def content_path(self, raw_id: str) -> Path:
        return self.record_dir(raw_id) / CONTENT_NAME

    def meta_path(self, raw_id: str) -> Path:
        return self.record_dir(raw_id) / META_NAME

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def exists(self, raw_id: str) -> bool:
        return self.content_path(raw_id).is_file()

    def get_content(self, raw_id: str) -> bytes:
        path = self.content_path(raw_id)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(f"raw 内容不存在：{raw_id}（{path}）") from exc

    def read_meta(self, raw_id: str) -> Optional[dict]:
        """读回 `meta.json`（可读副本）。缺失时返回 `None`，不静默造一个。"""
        path = self.meta_path(raw_id)
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # 不掩盖：副本损坏是响亮失败，而不是当成"没有元数据"。
            raise ImmutabilityError(f"meta.json 损坏，无法解析：{path}") from exc
        if not isinstance(loaded, dict):
            raise ImmutabilityError(f"meta.json 结构非法（应为对象）：{path}")
        return loaded

    def all_raw_ids(self) -> List[str]:
        """已落盘的全部 `raw_id`（字典序）。目录不存在时返回空列表。"""
        if not self._root.is_dir():
            return []
        found = [
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and (entry / CONTENT_NAME).is_file()
        ]
        return sorted(found)

    def rewrite_meta(self, raw_id: str, meta: dict) -> Path:
        """原子重写**既有**记录的 `meta.json`（可读副本修复）。

        只允许对已存在 `content.bin` 的记录做这件事：`meta.json` 是冗余副本，
        不是事实来源，因此修复它不违反"Raw 只增不改"（字节与元数据行都没变）。
        对不存在的记录调用会抛 `NotFoundError`。
        """
        raw_id = _check_raw_id(raw_id)
        target = self.record_dir(raw_id)
        if not (target / CONTENT_NAME).is_file():
            raise NotFoundError(f"raw 内容不存在，拒绝写 meta.json：{raw_id}（{target}）")
        text = json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        staging = Path(
            tempfile.mkdtemp(prefix=f".{raw_id}.meta-", dir=str(self._root))
        )
        try:
            _write_file_synced(staging / META_NAME, text.encode("utf-8"))
            os.replace(str(staging / META_NAME), str(target / META_NAME))
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return target / META_NAME

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def put(self, raw_id: str, content: bytes) -> Path:
        """写入原文字节，返回记录目录。

        - 内容 sha256 与记录声明的比对由调用方（`ArchiveStore`）负责，
          本方法只负责「原子落盘 + 不可覆盖」这一件事。
        - 已存在且字节相同 → 原子地什么都不做（幂等）。
        - 已存在且字节不同 → `ImmutabilityError`，不触碰已有文件。
        """
        raw_id = _check_raw_id(raw_id)
        if not isinstance(content, (bytes, bytearray)):
            raise ImmutabilityError(f"content 必须是 bytes，收到 {type(content).__name__}")
        payload = bytes(content)

        target = self.record_dir(raw_id)
        if target.exists():
            existing = self._read_existing_bytes(raw_id)
            if existing == payload:
                return target  # 幂等：同内容重复抓取不是错误
            raise ImmutabilityError(
                f"raw_id 已存在且内容不同，拒绝覆盖：{raw_id}（{target}）"
            )
        # 不留半成品：临时目录 + 原子 rename。
        self.write_new(raw_id, payload, meta=None)
        return target

    def write_new(self, raw_id: str, content: bytes, meta: Optional[dict] = None) -> Path:
        """把 `content`（+ 可选 `meta.json`）作为**新**记录原子落盘。

        记录目录已存在时抛 `ImmutabilityError`——本方法是"只增"路径，
        不含覆盖语义（`put` 在同一契约下已把幂等分支处理掉）。
        """
        raw_id = _check_raw_id(raw_id)
        target = self.record_dir(raw_id)
        if target.exists():
            raise ImmutabilityError(f"raw_id 已存在，拒绝写入新记录：{raw_id}（{target}）")

        self._root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{raw_id}.tmp-", dir=str(self._root)))
        try:
            _write_file_synced(staging / CONTENT_NAME, bytes(content))
            if meta is not None:
                text = json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                _write_file_synced(staging / META_NAME, text.encode("utf-8"))
            _fsync_dir(staging)
            if target.exists():  # 并发下的再检查：仍不覆盖
                raise ImmutabilityError(f"raw_id 已被并发写入，拒绝覆盖：{raw_id}")
            os.replace(str(staging), str(target))
        finally:
            # 任一步骤失败（含 os.replace 抛错）都不留临时目录。
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        _fsync_dir(self._root)
        return target

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _read_existing_bytes(self, raw_id: str) -> bytes:
        path = self.content_path(raw_id)
        if not path.is_file():
            # 目录存在但没有 content.bin：半成品（人为或崩溃残留）→ 响亮失败。
            raise ImmutabilityError(
                f"记录目录已存在但缺少 {CONTENT_NAME}，疑似半成品，拒绝继续：{path}"
            )
        return path.read_bytes()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"BlobStore(root={str(self._root)!r})"


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #


def _check_raw_id(raw_id: str) -> str:
    """`raw_id` 会直接成为目录名，因此拒绝任何路径穿越与空值。"""
    if not isinstance(raw_id, str) or not raw_id:
        raise NotFoundError("raw_id 不得为空")
    if raw_id in (".", "..") or "/" in raw_id or "\\" in raw_id or "\x00" in raw_id:
        raise NotFoundError(f"raw_id 含非法路径字符，拒绝用作目录名：{raw_id!r}")
    if raw_id.startswith("."):
        raise NotFoundError(f"raw_id 不得以 '.' 开头（与临时目录命名冲突）：{raw_id!r}")
    return raw_id


def _write_file_synced(path: Path, data: bytes) -> None:
    """写文件并 `fsync`，保证 rename 之前字节已落盘。"""
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    """对目录 `fsync`，让 rename 本身持久化。部分平台不支持，静默跳过。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def record_meta_payload(record: RawRecord) -> dict:
    """展开 `raw_records` 的等价 `meta.json` 内容（元数据可读副本，见模块 docstring）。"""
    return {
        "raw_id": record.raw_id,
        "channel_id": record.channel_id,
        "endpoint": record.endpoint,
        "content_sha256": record.content_sha256,
        "byte_length": record.byte_length,
        "fetched_at": _iso(record.fetched_at),
        "http_status": record.http_status,
    }


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def meta_matches(meta: Optional[dict], record: RawRecord) -> bool:
    """判断 `meta.json` 是否与记录一致（用于漂移检测，不用于判定不可变性）。"""
    if meta is None:
        return False
    expected = record_meta_payload(record)
    return all(meta.get(key) == value for key, value in expected.items())


def sha256_of(content: bytes) -> Tuple[str, int]:
    """`(content_sha256, byte_length)`，供调用方一次算出指纹与长度。"""
    digest = content_sha256(content)
    return digest, len(content)
