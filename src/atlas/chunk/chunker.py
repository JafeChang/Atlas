"""分块内核：`NormalizedText` → 确定性、可重建的分块集合（SPEC §2.3 / §5 #7，T-206）。

**分块是 raw 的纯函数**（与归一化同一层级）：不读文件、不写文件、不看时钟、
不用随机数、没有全局可变状态。丢弃全部结果后，拿同一份 raw 重跑
`normalize → chunk` 必然得到**逐字节相同**的分块集合。

**分块是派生物，永不作人工产物锚点**（SPEC §2.2 / §5 延后登记 #7）
----------------------------------------------------------------
分块边界由"归一化文本 + 策略版本"决定：换解析器、换策略、换参数，边界与 ID
会**整体漂移**。因此 `chunk_id` 不是事实。真值只有一种：

    { raw_id, raw_sha256, char_start, char_end }   ← T-107 由 quote 确定性重算

这一点在代码层用两道锁强制（见 `Chunk.as_anchor` 与 `ChunkNotAnchorError`）：

1. `Chunk` **根本没有** `raw_sha256` 字段，也不接受把 `chunk_id` 当锚点；
   `Chunk` 与 `EvidenceAnchor` / `DerivedLocator` 是不同类型，塞错地方 pydantic
   直接 `ValidationError`；
2. `chunk_id` 形如 `chk_…`，而 `EvidenceAnchor.raw_sha256` 要求 `^[0-9a-f]{64}$`
   —— 前缀 + 长度都不符，**永远**不可能被当成证据真值使用。

**无空隙**：分块按文档顺序切分，`chunk[i+1].normalized_start <= chunk[i].normalized_end`。
相邻分块最多重叠 `overlap_chars`（下界），**绝不出现空隙**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from atlas.contracts import DerivedLocator
from atlas.normalize import NormalizedText

from .errors import ChunkError, ChunkNotAnchorError
from .ids import chunk_id_for, policy_fingerprint
from .policy import DEFAULT_POLICY, ChunkPolicy

__all__ = [
    "Chunk",
    "ChunkSet",
    "chunk",
    "chunk_normalized",
]


def _require_positive_span(normalized_start: int, normalized_end: int) -> None:
    if isinstance(normalized_start, bool) or not isinstance(normalized_start, int):
        raise ChunkError(f"normalized_start 必须是 int，得到 {type(normalized_start).__name__}")
    if isinstance(normalized_end, bool) or not isinstance(normalized_end, int):
        raise ChunkError(f"normalized_end 必须是 int，得到 {type(normalized_end).__name__}")
    if normalized_start < 0:
        raise ChunkError(f"normalized_start 不得为负：{normalized_start}")
    if normalized_end <= normalized_start:
        raise ChunkError(f"非法分块区间：[{normalized_start}, {normalized_end})")


@dataclass(frozen=True, slots=True)
class Chunk:
    """一个分块：**派生**定位 + 原文回环 + 内容寻址 ID。

    字段全部由 `(normalized, raw_id, policy)` 确定性算出：

    | 字段 | 说明 |
    |---|---|
    | `chunk_id` | `chk_` + sha256(raw_id, 策略版本, 区间, 文本)[:32]，**派生** |
    | `raw_id` | 溯源用（ID 命名空间）；**不是**锚点真值 |
    | `index` | 文档序（0 起），便于报告与调试 |
    | `policy_version` | 产出该分块的策略版本（重建依据之一） |
    | `normalized_start` / `normalized_end` | 归一化文本区间 `[start, end)` |
    | `raw_start` / `raw_end` | 经 `SegmentTable` 映射回原文的区间 |
    | `text` | 分块文本；构造期断言 `len(text) == end - start` |

    注意**没有** `raw_sha256`：分块不携带任何证据真值。
    """

    chunk_id: str
    raw_id: str
    index: int
    policy_version: str
    normalized_start: int
    normalized_end: int
    raw_start: int
    raw_end: int
    text: str

    def __post_init__(self) -> None:
        if not self.chunk_id.startswith("chk_"):
            raise ChunkError(f"chunk_id 必须以 'chk_' 开头（派生标识）：{self.chunk_id!r}")
        if not self.raw_id:
            raise ChunkError("raw_id 不得为空：分块必须能溯源到不可变原文")
        if not self.policy_version:
            raise ChunkError("policy_version 不得为空")
        if self.index < 0:
            raise ChunkError(f"index 不得为负：{self.index}")
        _require_positive_span(self.normalized_start, self.normalized_end)
        if self.raw_start < 0 or self.raw_end < self.raw_start:
            raise ChunkError(f"非法原文区间：[{self.raw_start}, {self.raw_end})")
        if not self.text.strip():
            raise ChunkError("分块文本不得为纯空白（分块只覆盖非空白内容）")
        if len(self.text) != self.normalized_end - self.normalized_start:
            raise ChunkError(
                "分块文本长度与归一化区间不一致："
                f"len(text)={len(self.text)} 区间={self.normalized_end - self.normalized_start}"
            )

    # -- 派生视图 -------------------------------------------------------------------

    @property
    def span(self) -> tuple[int, int]:
        return self.normalized_start, self.normalized_end

    @property
    def length(self) -> int:
        return self.normalized_end - self.normalized_start

    @property
    def locator(self) -> DerivedLocator:
        """转成 T-002 的**派生**定位器（可失效、可重建，不可作锚点）。"""
        return DerivedLocator(
            block_id=self.chunk_id,
            normalized_start=self.normalized_start,
            normalized_end=self.normalized_end,
        )

    def raw_slice(self, normalized: NormalizedText) -> str:
        """归一化区间 → 原文切片（判据 4 的回环依据）。"""
        return normalized.raw_slice(self.normalized_start, self.normalized_end)

    # -- 锚点误用的响亮失败 -----------------------------------------------------------

    def as_anchor(self) -> NoReturn:
        """**永远抛错**：分块是派生物，不得充当证据/人工产物锚点（SPEC §5 #7）。

        这里刻意不返回 `None`、不打 warning —— 静默接受正是 SPEC §7.3 记录的头号
        失败模式（"字段存在但没有一条路径真的写进去"）。
        """
        raise ChunkNotAnchorError(
            f"分块 {self.chunk_id}（归一化区间 [{self.normalized_start}, "
            f"{self.normalized_end})）是**派生**量，不得作为证据锚点或人工标签锚点。"
            "真值锚点是 raw 的字符区间 {raw_id, raw_sha256, char_start, char_end}，"
            "由 T-107 从 quote 确定性重算（SPEC §2.2）；人工标签锚在 raw_id 上（§2.1）。"
        )


@dataclass(frozen=True, slots=True)
class ChunkSet:
    """一次分块的完整产物：分块 + **本次使用的策略快照**（SPEC §3 可重算）。

    `policy` 与 `policy_version` 都记在产物里，因此重建不需要回看调用现场。
    """

    chunks: tuple[Chunk, ...]
    raw_id: str
    policy: ChunkPolicy

    def __post_init__(self) -> None:
        if not self.raw_id:
            raise ChunkError("raw_id 不得为空")
        for position, item in enumerate(self.chunks):
            if not isinstance(item, Chunk):
                raise ChunkError(f"chunks[{position}] 必须是 Chunk，得到 {type(item).__name__}")
            if item.index != position:
                raise ChunkError(f"chunks[{position}].index = {item.index}，顺序与 index 不一致")
            if item.raw_id != self.raw_id:
                raise ChunkError(
                    f"chunks[{position}].raw_id = {item.raw_id!r} 与 ChunkSet.raw_id 不一致"
                )
            if item.policy_version != self.policy.version:
                raise ChunkError(
                    f"chunks[{position}].policy_version = {item.policy_version!r} "
                    f"与策略版本 {self.policy.version!r} 不一致"
                )
        for position in range(1, len(self.chunks)):
            previous = self.chunks[position - 1]
            current = self.chunks[position]
            if current.normalized_start <= previous.normalized_start:
                raise ChunkError(
                    f"分块起点未严格递增：chunk[{position}] 起于 "
                    f"{current.normalized_start}，chunk[{position - 1}] 起于 "
                    f"{previous.normalized_start}"
                )
            # 相邻分块之间**不允许空隙**（重叠允许）。空隙是否"全为空白"需要文本，
            # 因此由 `chunk_normalized` 用真实文本做最终校验
            # （`ChunkSet.uncovered_non_whitespace`）；这里只钉死必要条件。
            if current.normalized_end <= current.normalized_start:
                raise ChunkError(f"chunk[{position}] 区间非法：{current.span}")

    # -- 派生视图 -------------------------------------------------------------------

    @property
    def policy_version(self) -> str:
        return self.policy.version

    @property
    def policy_fingerprint(self) -> str:
        """策略快照的完整摘要：重建自检用（与首次不符即说明策略漂移）。"""
        return policy_fingerprint(self.policy.snapshot_fields())

    @property
    def total_chars(self) -> int:
        """分块文本长度之和（**含重叠**，因此 >= 归一化文本长度）。"""
        return sum(item.length for item in self.chunks)

    @property
    def covered_ranges(self) -> tuple[tuple[int, int], ...]:
        """归一化空间的**并集**（重叠已合并，按起点升序）。"""
        merged: list[tuple[int, int]] = []
        for item in self.chunks:
            if merged and item.normalized_start <= merged[-1][1]:
                start, end = merged[-1]
                merged[-1] = (start, max(end, item.normalized_end))
            else:
                merged.append((item.normalized_start, item.normalized_end))
        return tuple(merged)

    def uncovered_positions(self, text: str) -> list[int]:
        """未被任何分块覆盖的位置（**只允许是空白**，判据 7.4 的自检入口）。"""
        covered = bytearray(len(text))
        for start, end in self.covered_ranges:
            covered[start:end] = b"\x01" * (end - start)
        return [index for index, flag in enumerate(covered) if not flag]

    def uncovered_non_whitespace(self, text: str) -> list[int]:
        """落在分块**之外**的非空白位置（必须恒为空列表 —— 无空隙丢失）。"""
        return [index for index in self.uncovered_positions(text) if not text[index].isspace()]

    def verify_ids(self) -> bool:
        """独立重算每个 `chunk_id` 并与记录比对（判据 5.3 自检）。"""
        for item in self.chunks:
            expected = chunk_id_for(
                raw_id=self.raw_id,
                policy_version=item.policy_version,
                normalized_start=item.normalized_start,
                normalized_end=item.normalized_end,
                text=item.text,
            )
            if expected != item.chunk_id:
                return False
        return True

    def verify_offsets(self, normalized: NormalizedText) -> bool:
        """独立重算归一化→原文映射并与记录比对（判据 4.2 自检）。"""
        for item in self.chunks:
            start, end = normalized.to_raw_offset.to_raw_range(
                item.normalized_start, item.normalized_end
            )
            if (start, end) != (item.raw_start, item.raw_end):
                return False
        return True


def chunk_normalized(
    normalized: NormalizedText,
    *,
    raw_id: str,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> ChunkSet:
    """`NormalizedText` → `ChunkSet`（纯函数，无 I/O）。

    Args:
        normalized: T-104 的归一化产物。只使用 `text` / `raw_text` / `to_raw_offset`；
            **刻意不读** `normalized.blocks`（避免与 T-104 的派生块隐式耦合——
            块变了不该让分块跟着变）。
        raw_id: 不可变原文的标识（ID 命名空间与溯源；分块 ID 含它）。
        policy: 分块策略快照；默认 `DEFAULT_POLICY`。

    分块规则（全部确定性）：

    1. 跳过前导空白（分隔符留在前一块尾部；跳过的都是空白，不算丢失字符）；
    2. 目标窗口终点 `target_end = min(start + target_chars, len(text))`，
       硬上限终点 `max_end = min(start + max_chars, len(text))`；
    3. 在 `(start, max_end]` 内找断点（见 `ChunkPolicy.find_break`），规则是
       "目标窗口内按优先级取最高优先级、取最后位置；目标窗口没有命中才放大窗口；
       都没有才硬切"；
    4. 若断点离起点不足 `min_fill_chars`，或剩余内容已能装进一个目标窗口，
       则把断点用 `find_break` 在 `max_end` 处重算一次（取最后一个可读边界）；
    5. 下一块起点 = `snap_start(lower=start + 1, candidate=end - overlap_chars)`，
       因此 **`start < next_start <= end`**：既必然前进，又不可能出现空隙。
    """
    if not isinstance(normalized, NormalizedText):
        raise ChunkError(f"normalized 必须是 NormalizedText，得到 {type(normalized).__name__}")
    if not raw_id:
        raise ChunkError("raw_id 不得为空：分块 ID 需要它做命名空间，且分块必须可溯源")
    if not isinstance(policy, ChunkPolicy):
        raise ChunkError(f"policy 必须是 ChunkPolicy，得到 {type(policy).__name__}")

    text = normalized.text
    total = len(text)
    table = normalized.to_raw_offset

    if not text.strip():
        return ChunkSet(chunks=(), raw_id=raw_id, policy=policy)

    built: list[Chunk] = []
    start = 0
    while start < total:
        # 跳过分块起点前的空白（通常是 `snap_start` 回退后落在分隔符里的位置）。
        # 跳过的字符**全是空白**，因此不产生任何非空白字符的丢失：
        # `ChunkSet.whitespace_gaps()` 必须恒为空列表。
        while start < total and text[start].isspace():
            start += 1
        if start >= total:
            break

        target_end = min(start + policy.target_chars, total)
        max_end = min(start + policy.max_chars, total)
        if max_end >= total:
            # 剩余内容一个硬上限窗口就装得下 → 这是最后一块。
            # 仍然走 `find_break`（保持"绝不切断单词/绝不硬切可读边界"的一致规则），
            # 但把目标窗口设成 `total`，因此它会在 `[start + min_fill, total]` 内
            # 找最高优先级的可读边界；没有边界才退到"最后一个空白之后"。
            end, _hit = policy.find_break(text, start, total, total)
        else:
            # `find_break` 内部已含"目标窗口 → 放大到硬上限 → 硬切"三级回退。
            end, _hit = policy.find_break(text, start, target_end, max_end)

        if end <= start:
            raise ChunkError(f"分块内核未能前进：start={start} end={end}")

        block = text[start:end]
        if not block.strip():  # pragma: no cover - 防御性：起点已跳过空白，不应发生
            raise ChunkError(f"分块内核产出纯空白分块：[{start}, {end})")

        raw_start = table.to_raw_offset(start)
        raw_end = table.to_raw_offset(end)
        if raw_end <= raw_start:
            raise ChunkError(
                f"分块 [{start}, {end}) 映射回原文得到空区间 [{raw_start}, {raw_end})"
            )

        index = len(built)
        built.append(
            Chunk(
                chunk_id=chunk_id_for(
                    raw_id=raw_id,
                    policy_version=policy.version,
                    normalized_start=start,
                    normalized_end=end,
                    text=block,
                ),
                raw_id=raw_id,
                index=index,
                policy_version=policy.version,
                normalized_start=start,
                normalized_end=end,
                raw_start=raw_start,
                raw_end=raw_end,
                text=block,
            )
        )

        if end >= total:
            break
        # lower 用 start + 1（而不是 start）：保证循环必然前进。这是
        # `overlap_chars < target_chars` 之后**第二道**防线，防止"参数合法、
        # 算法却原地打转"的静默活锁。
        next_start = policy.snap_start(
            text, start + 1, end, end - policy.overlap_chars
        )
        if next_start <= start or next_start > end:
            raise ChunkError(
                f"分块内核区间非法：start={start} next_start={next_start} end={end}"
            )
        start = next_start

    result = ChunkSet(chunks=tuple(built), raw_id=raw_id, policy=policy)
    gaps = result.uncovered_non_whitespace(text)
    if gaps:
        # 内核自身的自检：非空白字符绝不允许落在分块之外（判据 7.4）。
        # 这是不变量检查，不是"用 except 掩盖"——失败必须响亮。
        raise ChunkError(
            f"分块未覆盖全部非空白内容：{len(gaps)} 个位置，首个 {gaps[0]}"
            f"（{text[gaps[0]]!r}）"
        )
    return result


def chunk(
    raw_bytes: bytes,
    content_type: str = "",
    *,
    raw_id: str,
    policy: ChunkPolicy = DEFAULT_POLICY,
) -> tuple[NormalizedText, ChunkSet]:
    """便捷入口：`raw 字节 → normalize → chunk`，返回 `(归一化结果, 分块集合)`。

    归一化结果一并返回，因为**分块文本本身不是事实**：要用它必须同时持有
    `NormalizedText`（才能把分块区间映射回原文区间）。刻意不提供"只给分块"的
    入口，避免调用方以为分块可以脱离原文独立存在。

    这不是"可重建"的捷径：丢掉返回值后，用同样的 `(raw_bytes, content_type, raw_id,
    policy)` 重跑必然得到相同的分块集合（判据 2.1）。
    """
    from atlas.normalize import normalize

    normalized = normalize(raw_bytes, content_type)
    return normalized, chunk_normalized(normalized, raw_id=raw_id, policy=policy)
