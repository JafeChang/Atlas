"""T-106 数据来源适配（注入式，避免与在建任务耦合）。

为什么要注入而不是直接查库
--------------------------

`SPEC.md` §4.0 规定跨包只允许依赖 `atlas.contracts` 与 `atlas.registry.schema`（类型）。
T-106 需要三类数据：

| 数据 | 来源 | 本包的取法 |
|---|---|---|
| 原文元数据 | `atlas.archive`（T-103，已提交） | `ArchiveFeedSource` 适配（只读） |
| 渠道 → 行业 | `atlas.registry` 的配置（T-101） | **注入** `industry_of(channel_id)` 可调用对象 |
| 是否已打标 / 标签 | `atlas.labels`（T-108，**正在写**） | **注入** `LabelLookup`（`raw_id -> 标签键`） |

因此本包**不 import** `atlas.labels`，也不 import `atlas.registry` 的实现；
`tests/test_feed_http.py::test_feed_package_does_not_import_labels` 用 AST 静态守住这条边界。

只读性
------

`FeedSource` Protocol 只声明读方法。`ArchiveFeedSource` 只调用归档层的
`all_raw_ids()` / `get()`，**不调用** `put()`；本包没有任何写入路径
（`tests/test_feed_http.py::test_archive_is_untouched_by_http_reads` 用归档前后
文件快照 + `ArchiveStore.verify()` 做行为性验证）。
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

from atlas.archive import ArchiveStore, open_archive
from atlas.contracts import RawRecord

from .query import LabelLookup

__all__ = [
    "EMPTY_LABEL_LOOKUP",
    "ArchiveFeedSource",
    "FeedSource",
    "StaticFeedSource",
    "archive_source_factory",
    "labels_from_mapping",
]


def EMPTY_LABEL_LOOKUP(raw_id: str) -> Sequence[str]:
    """没有标签信息源时的显式占位：**恒为空**。

    只在查询未要求标签信息时使用；一旦查询带 `labeled` / `labels`，
    `run_query` 会因为在 `label_lookup=None` 上响亮失败而拒绝返回结果。
    """
    return ()


def labels_from_mapping(mapping: Mapping[str, Iterable[str]]) -> LabelLookup:
    """把 `raw_id -> 标签键集合` 的映射包成 `LabelLookup`（供 T-108 / 测试接入）。"""

    def lookup(raw_id: str) -> Sequence[str]:
        return tuple(mapping.get(raw_id, ()))

    return lookup


@runtime_checkable
class FeedSource(Protocol):
    """feed 的数据来源契约（**只读**）。

    - `list_raw(limit, offset)`：按 `raw_id` 升序（或不保证顺序，由查询层排序）返回一页
      `RawRecord`。返回条数**必须 ≤ limit**；返回少于 `limit` 表示已到末尾。
    - `industry_of(channel_id)`：渠道所属行业；无归属时返回 `None`。

    实现方必须保证读操作是幂等的：查询层为了得到精确 `total` 会全量扫描一次。
    """

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]: ...

    def industry_of(self, channel_id: str) -> str | None: ...


class ArchiveFeedSource:
    """基于已提交的 `atlas.archive` 的 `FeedSource` 实现（只读适配）。

    - 记录**以 `all_raw_ids()` 的顺序（`raw_id` 字典序）**分页，保证 `offset` 切片稳定；
    - 逐条 `get(raw_id)` 取元数据，不读原文字节（feed 只展示元数据）；
    - `industry_of` 由注入的映射/可调用对象提供（默认恒为 `None`，表示"未归行业"）。

    线程注意：T-103 的 `SqliteRawStore` 用的是 `sqlite3` 默认线程亲和的连接。
    `ThreadingHTTPServer` 在**各自线程**里处理请求，因此把 `ArchiveStore` 交给
    `atlas.feed.serve()` 时应当传**工厂**（`lambda: ArchiveFeedSource(open_archive(...))`），
    让每个请求线程拿到自己的连接，而不是跨线程共享一个连接。
    """

    def __init__(
        self,
        archive: ArchiveStore,
        *,
        industry_of: Callable[[str], str | None] | Mapping[str, str] | None = None,
    ) -> None:
        self._archive = archive
        self._industry_of = _as_industry_lookup(industry_of)

    @property
    def archive(self) -> ArchiveStore:
        return self._archive

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]:
        if limit < 1:
            raise ValueError(f"limit 必须 ≥ 1（收到 {limit}）")
        if offset < 0:
            raise ValueError(f"offset 必须 ≥ 0（收到 {offset}）")
        raw_ids = self._archive.all_raw_ids()[offset : offset + limit]
        return [self._archive.get(raw_id) for raw_id in raw_ids]

    def industry_of(self, channel_id: str) -> str | None:
        return self._industry_of(channel_id)


class StaticFeedSource:
    """内存 `FeedSource`：给定记录与被注入的行业/标签信息。

    用途：前端预览、本地开发、以及不依赖归档层的**纯单元测试**（测试里也可以直接
    自定义假对象，本类只是省事的实现）。
    """

    def __init__(
        self,
        records: Iterable[RawRecord],
        *,
        industry_of: Callable[[str], str | None] | Mapping[str, str] | None = None,
    ) -> None:
        self._records: Tuple[RawRecord, ...] = tuple(records)
        self._industry_of = _as_industry_lookup(industry_of)

    def list_raw(self, limit: int, offset: int) -> Sequence[RawRecord]:
        if limit < 1:
            raise ValueError(f"limit 必须 ≥ 1（收到 {limit}）")
        if offset < 0:
            raise ValueError(f"offset 必须 ≥ 0（收到 {offset}）")
        ordered = sorted(self._records, key=lambda record: record.raw_id)
        return ordered[offset : offset + limit]

    def industry_of(self, channel_id: str) -> str | None:
        return self._industry_of(channel_id)

    # 便捷只读视图（不是写入路径）
    def by_id(self, raw_id: str) -> RawRecord | None:
        for record in self._records:
            if record.raw_id == raw_id:
                return record
        return None


def archive_source_factory(
    root: Any = None,
    *,
    industry_of: Callable[[str], str | None] | Mapping[str, str] | None = None,
    **archive_kwargs: Any,
) -> Callable[[], ArchiveFeedSource]:
    """构造**每请求新开归档连接**的源工厂（`sqlite3` 线程亲和的正确用法）。

    `atlas.archive.SqliteRawStore` 的连接只能在创建它的线程里使用，而
    `ThreadingHTTPServer` 在各自线程里处理请求。因此给 `atlas.feed.serve()` /
    `FeedServer` 传本工厂，而不是共享一个 `ArchiveFeedSource`::

        server = FeedServer(archive_source_factory("data/store", industry_of=index))
    """

    def factory() -> ArchiveFeedSource:
        return ArchiveFeedSource(
            open_archive(root, **archive_kwargs), industry_of=industry_of
        )

    return factory


def _as_industry_lookup(
    source: Callable[[str], str | None] | Mapping[str, str] | None,
) -> Callable[[str], str | None]:
    if source is None:
        return _no_industry
    if callable(source):
        return source
    mapping: Dict[str, str] = dict(source)
    return lambda channel_id: mapping.get(channel_id)


def _no_industry(channel_id: str) -> None:
    """默认：没有行业信息源 → 一律 `None`（"未归行业"），绝不编造。"""
    return None
