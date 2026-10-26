"""T-205 文档来源适配（注入式，**只读**）：raw（T-103）→ 归一化文本（T-104）。

为什么单独一层
--------------

`atlas.search.sqlite_index` 只认 `DocumentText`（= `RawRecord` + 文本），
**不 import 归档与归一化实现**。归档读取放在这里，与
`atlas.feed.repository.ArchiveFeedSource` 的既有做法同构（§4.0：跨包只依赖
`atlas.contracts` 与类型；只读适配是既成先例）。

只读性
------

本模块只调用归档层的 `all_raw_ids()` / `get()` / `get_content()`，**不调用**
`put()`；`atlas.normalize.normalize` 是**无 I/O 的纯函数**（T-104）。
因此"索引不产生事实"是结构性成立的：

- 归一化文本**永远**由 `archive.get_content(raw_id)` 现算，不接受任何"别人算好的文本"，
  所以"索引可从 raw 全量重建"不依赖任何缓存文件；
- `normalize(content)` 的调用方式与 T-120 `NormalizeStage` 完全一致（都不传
  `content_type`），因此同一个 `raw_id` 在归一化缓存与本索引里得到**同一份文本**、
  同一个文本指纹。

边界：**不做分块**。被索引的单位是一整篇文档（`raw_id`）；分块属于 T-206
（`src/atlas/chunk/`），其触发条件是 embedding / RAG 上下文窗口，与 FTS 无关。
"""

from __future__ import annotations

from typing import Callable, Iterator, Protocol, runtime_checkable

from atlas.archive import ArchiveStore
from atlas.contracts import content_sha256
from atlas.normalize import NormalizedText, normalize

from .documents import DocumentText
from .errors import SearchSourceError

__all__ = ["ArchiveDocumentSource", "DocumentSource", "Normalizer"]

#: 归一化入口的签名（T-104 的 `normalize`；默认值让它可以被单参调用）。
Normalizer = Callable[[bytes], NormalizedText]


@runtime_checkable
class DocumentSource(Protocol):
    """索引的数据来源契约（**只读**）：给出一批文档级输入。

    实现方必须保证读操作幂等：`rebuild()` 会完整走一遍来源。
    """

    def iter_documents(self) -> Iterator[DocumentText]: ...


class ArchiveDocumentSource:
    """基于 `atlas.archive`（T-103）与 `atlas.normalize`（T-104）的只读来源。

    - 遍历顺序是 `all_raw_ids()`（`raw_id` 字典序），保证同一归档给出同一批输入；
    - 每条记录都会重新校验盘上字节指纹与元数据是否一致——归档被破坏时**响亮失败**，
      绝不把坏数据索引进去（也就不会让检索结果建立在损坏的原文上）；
    - 本类**不拥有**传入的 `ArchiveStore`，不负责关闭它。
    """

    def __init__(self, archive: ArchiveStore, *, normalizer: Normalizer = normalize) -> None:
        self._archive = archive
        self._normalize = normalizer

    @property
    def archive(self) -> ArchiveStore:
        return self._archive

    def iter_documents(self) -> Iterator[DocumentText]:
        for raw_id in self._archive.all_raw_ids():
            record = self._archive.get(raw_id)
            content = self._archive.get_content(raw_id)
            digest = content_sha256(content)
            if digest != record.content_sha256:
                raise SearchSourceError(
                    f"raw_id={raw_id} 盘上字节指纹 {digest[:12]}… 与归档元数据 "
                    f"{record.content_sha256[:12]}… 不符；拒绝把它索引进检索层"
                )
            normalized = self._normalize(content)
            yield DocumentText(record=record, text=normalized.text)
