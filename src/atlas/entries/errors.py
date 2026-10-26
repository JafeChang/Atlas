"""T-130 条目化派生层的显式失败（不吞异常、不返回编造结果）。

错误层级挂在 T-002 的 `ContractError` 之下，让"契约违例"只有一套语义
（与 `atlas.chunk.errors` 同一做法）：

- `EntryError`             条目层一切违例的基类
- `EntryParserError`       解析器参数非法（**在构造解析器时**就抛）
- `EntryParseError`        输入不是可解析的 feed（响亮失败，见下）
- `EntryNotAnchorError`    把**派生**的条目 ID 当**真值锚点**用

关键区分（本任务与 T-206 的**不同之处**，必须写清楚）
-------------------------------------------------

T-206 的结论是"分块**整体**不得作锚点"：分块连字符区间都没有。
T-130 的结论是**只有条目 ID 不得作锚点**：

| 量 | 性质 | 能不能当锚点 |
|---|---|---|
| `Entry.char_start` / `char_end` | 条目在**解码后原文**里的字符区间 —— SPEC §2.2 的真值形状 | ✅ **正是锚点** |
| `Entry.entry_id` | `ent_` + sha256(raw_id, raw_sha256, 解析器版本, 区间, 标题)[:32]，**派生** | ❌ 永远不是 |

`Entry.as_anchor()` **永远抛** `EntryNotAnchorError`；`Entry.anchor()` 返回由字符区间
构造的 **`EvidenceAnchor`**（合法，有活对照测试钉死）。把条目 ID 塞进
`EvidenceAnchor.raw_sha256` 会被字段正则 `^[0-9a-f]{64}$` 拒绝。

`EntryParseError` 的边界（"不假装成功"的具体口径）
------------------------------------------------

| 输入 | 行为 |
|---|---|
| 非 XML（JSON API 响应 / HTML 页面） | **抛 `EntryParseError`** —— 拿网页当 feed 是接线错误 |
| XML 但结构损坏（标签不闭合、编码声明与实际字节不符且无法回退解码） | **抛 `EntryParseError`** |
| 合法 XML，但根元素不是 feed（未知格式） | 返回 0 条 + `problems` 说明（XML 是好的，只是不认识） |
| 合法 feed，`<item>`/`<entry>` 数量为 0 | 返回 0 条 + `problems` 说明（**空 feed 是事实，不是错误**） |
| 合法 feed，个别条目字段缺失/日期无法解析 | 该条目仍然产出，**理由记进 `problems`**，绝不静默丢失 |

对照 `atlas.feed.repository`（T-106）的 `_looks_like_feed` 先例：**判据是根元素**，
不是"文本里出现过 `<item`"。
"""

from __future__ import annotations

from atlas.contracts.errors import ContractError

__all__ = [
    "EntryError",
    "EntryNotAnchorError",
    "EntryParseError",
    "EntryParserError",
]


class EntryError(ContractError):
    """条目化派生层的契约违例（解析器非法 / 输入不可解析 / 锚点误用）。"""


class EntryParserError(EntryError):
    """`EntryParser` 参数非法。

    必须**在构造解析器时**抛出（与 `ChunkPolicyError` 同一纪律）：
    解析器版本为空会让"版本变化必须改变条目 ID"这条保证失效，不能拖到运行时。
    """


class EntryParseError(EntryError):
    """输入不是可解析的 feed（非 XML、结构损坏、或 feed XML 声明与实际字节不符）。

    这里刻意**不**返回空结果：把 JSON API 响应或 HTML 页面当 feed 传进来是接线错误，
    静默返回 0 条会让「采集粒度是 feed」这类问题继续潜伏（SPEC §7.3 失败模式 3）。
    """


class EntryNotAnchorError(EntryError):
    """把**派生**量（`entry_id`）当作**真值锚点**使用。

    条目由 `(raw 字节, 解析器版本)` 纯函数决定：换解析器、换版本、换字段提取规则
    都可能让条目 ID 整体漂移。因此 ID **不是**事实，不能承载人工判断。

    **注意与分块的区别**：条目的**字符区间**（`char_start` / `char_end`）
    恰恰**就是** §2.2 的真值形状，可以（并且应该）用来构造 `EvidenceAnchor`。
    本条错误只针对 ID。
    """
