"""归档存储：文件系统（字节）+ SQLite（元数据）组合成一个 `RawStore`（SPEC §2.10）。

对外方法签名与语义与 `atlas.contracts.RawStore`（内存版）**逐一相同**::

    put(record, content) -> RawRecord
    get(raw_id)          -> RawRecord
    get_content(raw_id)  -> bytes
    all_raw_ids()        -> list[str]

异常也同一套：`ImmutabilityError` / `IdError` / `NotFoundError`。

提交顺序与补偿策略（失败不留半成品）
------------------------------------

两个事实来源必须一起成立：`data/store/raw/<raw_id>/content.bin`（字节）
与 `raw_records` 行（元数据）。写入顺序是：

1. **先写字节**（`BlobStore.write_new`：临时目录 → fsync → `os.replace` 原子 rename）。
   字节是事实来源；元数据行可以晚一点，反之则不行。
2. **再插元数据**（`SqliteRawStore.insert`，单条 INSERT，自动提交）。
3. **插入失败**：补偿性地 `rmtree` 掉第 1 步刚创建的那个记录目录，回到"两者都没有"。
   只有**本次调用创建**的目录才会被补偿删除——绝不动别人写下的既有记录。

因此调用方观察到的状态只有两种：**两者都有** 或 **两者都没有**。
唯一的例外是进程在第 1 步之后、第 2 步之前被强杀（kill -9 无法补偿），
此时留下"有字节、无元数据行"的孤儿目录；`BlobStore.all_raw_ids` 不会把它当成
已归档记录（`all_raw_ids()` 以 DB 为准），而**任何用同一份内容重跑 `put` 都会自愈**：
第 2 步检测到已有同指纹字节，仅补插元数据行，不改动字节。

异常语义
--------

- `IdError`：`content` 的实际 sha256 与 `record.content_sha256` 不一致（先于任何落盘检查）
- `ImmutabilityError`：同 `raw_id` 已存在且指纹不同（绝不覆盖）
- `NotFoundError`：`get` / `get_content` 查不到
- `RawContentMissingError`（`NotFoundError` 子类）：DB 有行但字节缺失，或字节在而副本损坏。
  这是**归档被破坏**，不是普通的"查不到"；子类化是为了让"照 `RawStore` 契约捕获
  `NotFoundError`"的调用方仍然可用，同时持有具体信息的人能区分二者。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional, Union

from atlas.contracts import IdError, ImmutabilityError, NotFoundError, RawRecord
from atlas.contracts.ids import content_sha256

from .blobstore import (
    DEFAULT_RAW_DIR,
    BlobStore,
    meta_matches,
    record_meta_payload,
)
from .sqlite_store import DEFAULT_DB_PATH, SqliteRawStore

__all__ = [
    "DEFAULT_STORE_ROOT",
    "ArchiveStore",
    "RawContentCorruptError",
    "RawContentMissingError",
    "open_archive",
]

#: SPEC §2.10 的存储根：`<root>/raw/` 与 `<root>/atlas.db`。
DEFAULT_STORE_ROOT = Path("data/store")

_PathLike = Union[str, Path]


class RawContentMissingError(NotFoundError):
    """元数据行存在但原文字节缺失：归档被破坏，不是普通"查不到"。"""


class RawContentCorruptError(ImmutabilityError):
    """原文字节存在但与元数据指纹不符：归档被破坏，任何写入都必须停下。"""


class ArchiveStore:
    """`RawStore` 契约的持久化实现（文件系统 + SQLite）。

    存储根可配置；测试一律指向 `tmp_path`，**绝不**往仓库 `data/` 写东西。

    ``ArchiveStore(root)``                        → ``<root>/raw`` + ``<root>/atlas.db``
    ``ArchiveStore(raw_dir=..., db_path=...)``    → 两个路径分别指定
    """

    def __init__(
        self,
        root: Optional[_PathLike] = None,
        *,
        raw_dir: Optional[_PathLike] = None,
        db_path: Optional[_PathLike] = None,
    ) -> None:
        if root is not None and (raw_dir is not None or db_path is not None):
            raise ValueError("root 与 raw_dir/db_path 不能同时指定")
        if root is not None:
            base = Path(root)
            raw_dir = base / "raw"
            db_path = base / "atlas.db"
        self._blobs = BlobStore(DEFAULT_RAW_DIR if raw_dir is None else raw_dir)
        self._records = SqliteRawStore(DEFAULT_DB_PATH if db_path is None else db_path)

    # ------------------------------------------------------------------
    # 组件访问（只读诊断用；不暴露写入旁路）
    # ------------------------------------------------------------------
    @property
    def blobs(self) -> BlobStore:
        return self._blobs

    @property
    def records(self) -> SqliteRawStore:
        return self._records

    @property
    def db_path(self) -> Path:
        return self._records.db_path

    @property
    def raw_dir(self) -> Path:
        return self._blobs.root

    def close(self) -> None:
        self._records.close()

    def __enter__(self) -> "ArchiveStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # RawStore 契约
    # ------------------------------------------------------------------
    def put(self, record: RawRecord, content: bytes) -> RawRecord:
        """归档一条原文。**永不覆盖**既有记录。

        - 同 `raw_id` + 同内容 → 返回已有记录（幂等，不报错）
        - 同 `raw_id` + 不同内容 → `ImmutabilityError`
        - `content` 指纹与声明不符 → `IdError`
        """
        actual = content_sha256(content)
        if actual != record.content_sha256:
            raise IdError(
                f"内容指纹与标识不一致：声明 {record.content_sha256[:12]}… 实际 {actual[:12]}…"
            )

        # 1) 元数据已存在 → 幂等返回，绝不触碰字节。
        existing = self._records.get_optional(record.raw_id)
        if existing is not None:
            if existing.content_sha256 == record.content_sha256:
                self._verify_blob(record)
                return existing  # 与内存版一致：返回**已有**记录，不是入参
            raise ImmutabilityError(f"raw_id 已存在且内容不同：{record.raw_id}")

        # 2) 字节落盘（原子 rename；已存在的目录不会被覆盖）。
        created = self._write_blob(record, content)

        # 3) 元数据入库；失败则补偿掉本次新建的目录。
        try:
            return self._records.insert(record)
        except BaseException:
            if created:
                shutil.rmtree(self._blobs.record_dir(record.raw_id), ignore_errors=True)
            raise

    def get(self, raw_id: str) -> RawRecord:
        return self._records.get(raw_id)

    def get_content(self, raw_id: str) -> bytes:
        """返回原文字节。元数据行不存在 → `NotFoundError`（与内存版一致）。

        元数据行存在但字节缺失 → `RawContentMissingError`（归档被破坏，响亮失败）。
        """
        self._records.get(raw_id)
        try:
            return self._blobs.get_content(raw_id)
        except NotFoundError as exc:
            raise RawContentMissingError(
                f"raw_id {raw_id} 的元数据存在但原文字节缺失"
                f"（{self._blobs.content_path(raw_id)}）"
            ) from exc

    def all_raw_ids(self) -> List[str]:
        """已归档的全部 `raw_id`（字典序）。以 `raw_records` 为准。

        只认"元数据行与字节都齐全"的记录：单纯的文件系统残留不被当成已归档。
        """
        return self._records.all_raw_ids()

    # ------------------------------------------------------------------
    # 诊断（不在契约内，只读）
    # ------------------------------------------------------------------
    def verify(self) -> List[str]:
        """自检：返回问题描述列表（空列表 = 归档一致）。

        检查项：
        - DB 有行但字节缺失 / 字节数或指纹与行不一致 → 归档被破坏
        - 文件系统有目录但 DB 无行 → 上次写入的孤儿残留（重跑同一份内容可自愈）
        - `meta.json` 与行不一致 → 可读副本漂移（冗余项，不影响事实）
        """
        problems: List[str] = []
        for raw_id in self._records.all_raw_ids():
            record = self._records.get(raw_id)
            path = self._blobs.content_path(raw_id)
            if not path.is_file():
                problems.append(f"{raw_id}: 元数据行存在但缺少原文字节文件（{path}）")
                continue

            payload = path.read_bytes()
            digest = content_sha256(payload)
            if digest != record.content_sha256:
                problems.append(
                    f"{raw_id}: 字节指纹与元数据不符（行 {record.content_sha256[:12]}… "
                    f"实际 {digest[:12]}…）"
                )
            if len(payload) != record.byte_length:
                problems.append(
                    f"{raw_id}: 字节数与元数据不符（行 {record.byte_length} 实际 {len(payload)}）"
                )
            if not meta_matches(self._blobs.read_meta(raw_id), record):
                problems.append(f"{raw_id}: meta.json 与 raw_records 行不一致（可读副本漂移）")

        known = set(self._records.all_raw_ids())
        for raw_id in self._blobs.all_raw_ids():
            if raw_id not in known:
                problems.append(f"{raw_id}: 文件系统有字节但 raw_records 无行（孤儿残留）")
        return problems

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _write_blob(self, record: RawRecord, content: bytes) -> bool:
        """写字节，返回 `True` 表示**本次调用新建**了记录目录。

        目录已存在时不覆盖：同内容 → 走自愈路径（只补/修元数据副本，返回 `False`）。
        """
        target = self._blobs.record_dir(record.raw_id)
        if target.exists():
            stored = self._blobs.get_content(record.raw_id)
            if content_sha256(stored) == record.content_sha256:
                # 自愈：字节已在。仅当 meta.json 缺失/漂移时重写这份冗余副本
                # （字节与元数据行都不动，因此不违反"只增不改"）。
                meta = self._blobs.read_meta(record.raw_id)
                if not meta_matches(meta, record):
                    self._blobs.rewrite_meta(record.raw_id, record_meta_payload(record))
                return False
            raise ImmutabilityError(
                f"raw_id 已存在且内容不同，拒绝覆盖：{record.raw_id}（{target}）"
            )
        self._blobs.write_new(record.raw_id, content, meta=record_meta_payload(record))
        return True

    def _verify_blob(self, record: RawRecord) -> None:
        """幂等分支的完整性检查：DB 说有，字节就必须在、且指纹一致。"""
        target = self._blobs.content_path(record.raw_id)
        if not target.is_file():
            raise RawContentMissingError(
                f"raw_id {record.raw_id} 的元数据存在但原文字节缺失（{target}）"
            )
        actual = content_sha256(target.read_bytes())
        if actual != record.content_sha256:
            raise RawContentCorruptError(
                f"raw_id {record.raw_id} 的原文字节与元数据指纹不符："
                f"行 {record.content_sha256[:12]}… 实际 {actual[:12]}…"
            )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ArchiveStore(raw_dir={str(self.raw_dir)!r}, db_path={str(self.db_path)!r})"


def open_archive(root: Optional[_PathLike] = None, **kwargs: object) -> ArchiveStore:
    """便捷构造（语义同 `ArchiveStore(...)`；省略 root 时用 SPEC §2.10 的默认布局）。"""
    return ArchiveStore(root, **kwargs)  # type: ignore[arg-type]
