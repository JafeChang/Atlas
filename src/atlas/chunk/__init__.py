"""T-206 分块策略：确定性、纯函数、可重建（SPEC §2.3 / §2.7 / §5 延后登记 #7）。

分块是**派生层**，与归一化同一条纪律：

```python
from atlas.chunk import DEFAULT_POLICY, ChunkPolicy, chunk

normalized, chunkset = chunk(raw_bytes, content_type, raw_id="raw_…")
chunkset.policy            # 本次策略快照（含版本）——重建所需，随产物一起记录
chunkset.chunks[0].text    # 分块文本
chunkset.chunks[0].raw_slice(normalized)   # 对应的原文切片
chunkset.chunks[0].locator                 # T-002 的 DerivedLocator（派生定位）

# 只对已归一化的文本分块（跳过 normalize）：
from atlas.chunk import chunk_normalized
chunkset = chunk_normalized(normalized, raw_id="raw_…", policy=DEFAULT_POLICY)
```

**分块 ID 是派生量，永不作人工产物锚点**（SPEC §2.2 / §5 #7）：

- 人工标签锚在 `raw_id` 上（决策 1A）
- 证据锚点是 raw 的字符区间 `{raw_id, raw_sha256, char_start, char_end}`（方案 2C），
  由 quote 确定性重算
- `Chunk.as_anchor()` **永远抛** `ChunkNotAnchorError`；`chunk_id` 形如 `chk_…`，
  既带前缀又不是 64 位十六进制，因此不可能通过 `EvidenceAnchor` 的字段校验

SPEC §4.0 的包布局表里没有 `atlas.chunk`：T-206 原属**延后能力**（§4.3），
本次提前实现故新开一个包（"一个包只由一个任务负责"）。
唯一越出"跨包只允许依赖 `atlas.contracts`"这条一般规则的地方是
`import atlas.normalize`（读 T-104 的 `NormalizedText` / `SegmentTable` 类型）——
分块的输入类型本来就定义在那里，已作为偏离项上报。
"""

from .chunker import Chunk, ChunkSet, chunk, chunk_normalized
from .errors import ChunkError, ChunkNotAnchorError, ChunkPolicyError
from .ids import CHUNK_ID_PREFIX, chunk_id_for, policy_fingerprint
from .policy import (
    DEFAULT_POLICY,
    DEFAULT_SEPARATORS,
    MIN_FILL_RATIO,
    SEPARATOR_KINDS,
    ChunkPolicy,
    SeparatorSpec,
    policy_from,
)

__all__ = [
    "CHUNK_ID_PREFIX",
    "DEFAULT_POLICY",
    "DEFAULT_SEPARATORS",
    "MIN_FILL_RATIO",
    "SEPARATOR_KINDS",
    "Chunk",
    "ChunkError",
    "ChunkNotAnchorError",
    "ChunkPolicy",
    "ChunkPolicyError",
    "ChunkSet",
    "SeparatorSpec",
    "chunk",
    "chunk_id_for",
    "chunk_normalized",
    "policy_fingerprint",
    "policy_from",
]
