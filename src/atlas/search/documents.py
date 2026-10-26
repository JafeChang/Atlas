"""T-205 索引的输入单元：**文档级**原文元数据 + 归一化文本。

边界（SPEC §4.2 T-205 / §5 延后登记 #11）
----------------------------------------

被索引的单位是**一整篇文档**（`raw_id`），文本是 T-104 的 `NormalizedText.text`。
本包**不做分块**：分块属于 T-206（`src/atlas/chunk/`），其触发条件是 embedding /
RAG 的上下文窗口，与 FTS 无关。因此这里不存在 chunk 结构，也不 import `atlas.chunk`。

`DocumentText` 只依赖 `atlas.contracts.RawRecord`（类型），不碰归档实现——
归档读取在 `atlas.search.source` 里，索引存储不关心文档从哪来。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from atlas.contracts import RawRecord

from .errors import SearchIndexError

__all__ = ["DocumentText", "text_sha256"]


def text_sha256(text: str) -> str:
    """归一化文本的指纹，算法与 T-120 `atlas.compose.tasks.sha256_text` 一致。

    刻意**不 import** `atlas.compose`（SPEC §4.0：跨包只依赖 contracts）：
    这里只复刻"`sha256(text.encode('utf-8')).hexdigest()`"这一个算式，
    因此同一个 `raw_id` 在组合根的归一化缓存与检索索引里得到同一个文本指纹。
    """
    if not isinstance(text, str):
        raise SearchIndexError(f"待索引文本必须是 str，收到 {type(text).__name__}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentText:
    """一篇待索引文档的**全部**输入。

    - `record`：T-103 的 `RawRecord`（元数据事实，只读借用，索引不改它）
    - `text`：T-104 的文档级归一化文本

    `text_sha256` / `text_length` 是**派生**属性，构造时算好并冻结，
    因此不可能出现"声明的指纹与实际文本不符"这种可漂移状态。
    """

    record: RawRecord
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.record, RawRecord):
            raise SearchIndexError(
                f"record 必须是 atlas.contracts.RawRecord，收到 {type(self.record).__name__}"
            )
        digest = text_sha256(self.text)
        # frozen dataclass：派生值通过 object.__setattr__ 冻结进实例。
        object.__setattr__(self, "_text_sha256", digest)
        object.__setattr__(self, "_text_length", len(self.text))

    @property
    def raw_id(self) -> str:
        return self.record.raw_id

    @property
    def text_sha256(self) -> str:
        return self._text_sha256  # type: ignore[attr-defined]

    @property
    def text_length(self) -> int:
        return self._text_length  # type: ignore[attr-defined]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"DocumentText(raw_id={self.record.raw_id!r}, text_length={self.text_length})"
