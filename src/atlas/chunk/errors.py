"""T-206 分块层的显式失败（不吞异常、不返回编造结果）。

错误层级挂在 T-002 的 `ContractError` 之下，让"契约违例"只有一套语义：

- `ChunkError`           分块层一切违例的基类
- `ChunkPolicyError`     策略参数非法（**在构造策略时**就抛，不是用到才炸）
- `ChunkNotAnchorError`  把派生量（分块 ID / 分块偏移）当**真值锚点**用

最后一条是本任务的硬要求（SPEC §5 延后登记 #7）：
分块是 raw 的纯函数、可重建，因此**永不作人工产物锚点**。
把它当锚点必须**响亮失败**，而不是被静默接受。
"""

from __future__ import annotations

from atlas.contracts.errors import ContractError

__all__ = [
    "ChunkError",
    "ChunkNotAnchorError",
    "ChunkPolicyError",
]


class ChunkError(ContractError):
    """分块层的契约违例（策略非法 / 重建不一致 / 锚点误用）。"""


class ChunkPolicyError(ChunkError):
    """`ChunkPolicy` 参数非法。

    必须**在构造策略时**抛出：`overlap_chars >= target_chars` 之类的参数会让
    分块循环永不前进（死循环），绝不能等到运行时才发现。
    """


class ChunkNotAnchorError(ChunkError):
    """把**派生**量（`chunk_id` / 归一化区间）当作**真值锚点**使用。

    分块由 `(raw, 策略版本)` 纯函数决定：换解析器、换策略、换参数都会让
    分块 ID 与边界整体漂移。因此它**不是**事实，不能承载人工判断
    （SPEC §2.2 / §2.3 / §5 #7）。人工标签锚在 `raw_id` 上（决策 1A），
    证据锚点锚在 raw 字符区间上（方案 2C）。
    """
