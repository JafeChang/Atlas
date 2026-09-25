"""派生块定位（SPEC §2.2 / §4.2 T-104）。

`blocks` 是**派生物**：可失效、可重建，**不得**作为人工产物（Confirmed）的锚点。
人工标签锚在 `raw_id` 上（SPEC §2.1 决策 1A）；证据锚点用 `raw` 的字符区间。

块由归一化文本里的换行边界切分。`text.assemble` 保留了两级换行语义
（1 个换行 = 行内换行，2+ 个换行 = 段落分隔），因此这里：

- 以**空行**（`\\n\\n`）为首选段落边界；
- 若全篇没有空行，退化为**单换行**切分（避免整篇变成一个巨型块）。

`block_id` 只由块序号决定 → 同一份 `raw` 重跑必然得到同样的块 ID（可重建）。
"""

from __future__ import annotations

import re
from typing import List, Sequence

from atlas.contracts.anchors import DerivedLocator

from .text import NormalizedText

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_LINE_SPLIT = re.compile(r"\n")


def _blocks_from(spans: Sequence[tuple[int, int]]) -> tuple[DerivedLocator, ...]:
    blocks: List[DerivedLocator] = []
    for index, (start, end) in enumerate(spans):
        blocks.append(
            DerivedLocator(
                block_id=f"blk_{index:04d}",
                normalized_start=start,
                normalized_end=end,
            )
        )
    return tuple(blocks)


def derive_blocks(normalized: NormalizedText) -> tuple[DerivedLocator, ...]:
    """按换行切分块，返回带归一化区间的**派生**定位（只含非空块）。"""
    text = normalized.text
    spans: List[tuple[int, int]] = []

    def collect(separator: re.Pattern[str]) -> List[tuple[int, int]]:
        found: List[tuple[int, int]] = []
        cursor = 0
        for m in separator.finditer(text):
            if text[cursor : m.start()].strip():
                found.append((cursor, m.start()))
            cursor = m.end()
        if text[cursor:].strip():
            found.append((cursor, len(text)))
        return found

    spans = collect(_PARAGRAPH_SPLIT)
    if len(spans) <= 1:
        spans = collect(_LINE_SPLIT)

    return _blocks_from(spans)


def attach_blocks(normalized: NormalizedText) -> NormalizedText:
    """返回附带 `blocks` 的**新**记录（契约记录不可就地修改）。"""
    return normalized.with_blocks(derive_blocks(normalized))


def block_of(normalized: NormalizedText, normalized_offset: int) -> DerivedLocator | None:
    """定位某个归一化偏移落在哪个块内（用于展示与调试；不参与事实判定）。"""
    for block in normalized.blocks:
        if (
            block.normalized_start is not None
            and block.normalized_end is not None
            and block.normalized_start <= normalized_offset < block.normalized_end
        ):
            return block
    return None
