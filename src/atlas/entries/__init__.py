"""T-130 条目化派生层：一份 feed → 条目序列（纯函数、可重建；SPEC §4.2 / §6.3 裁决 B）。

背景（SPEC §6.3）：新结构把**整个 HTTP 响应体**当作一篇文档归档，实测
`data/store` 里 10 条归档记录内含 **830 篇**文章，没有一篇成为独立文档。
于是 `raw_id` 标识的是一份 **feed**，而 §2.1 规定标签锚在 `raw_id` 上
⇒ 只能给"整份 feed"打标。用户裁决的**方案 B**：feed 仍是 Raw（`raw_id` 不变），
另建**条目派生层** —— 零额外网络请求、零 schema 迁移。本包就是那一层。

```python
from atlas.entries import parse_entries, verify_offsets, verify_ids

entrieset = parse_entries(raw_bytes, "application/rss+xml", raw_id="raw_…")
entrieset.kind                        # "rss" / "atom" / "unknown"
entrieset.encoding                    # 实际用于解码的编码（区间所在的参照系）
entrieset.entries[0].title            # 标题
entrieset.entries[0].link             # 原文 URL
entrieset.entries[0].published_at     # 定宽 UTC 串（缺失为 None，理由在 problems）
entrieset.entries[0].span             # (char_start, char_end) —— 解码后 feed 文本上的字符区间
entrieset.entries[0].anchor()         # ✅ 合法 EvidenceAnchor（字符区间 = SPEC §2.2 真值形状）
entrieset.entries[0].as_anchor()      # ❌ 永远抛 EntryNotAnchorError（**ID** 不是锚点）
entrieset.problems                    # 空 feed / 未知格式 / 字段缺失的**理由**

verify_offsets(raw_id=…, raw_bytes=raw_bytes, entries=entrieset).ok   # 区间可回环
verify_ids(raw_id=…, raw_bytes=raw_bytes, entries=entrieset).ok       # ID 可独立重算
```

三条纪律（与 `atlas.chunk` 同一形状，不另发明）

1. **纯函数**：无 I/O、无时钟、无随机、无全局可变状态（命名空间用注入法处理，
   不碰 `ElementTree.register_namespace` 的进程级全局表）。
2. **可重建**：丢结果重跑，逐字节相同；`parser_version` 记在产物里**且参与 ID**。
3. **ID 不作锚点，字符区间正是锚点** —— 见 `Entry.anchor()` / `Entry.as_anchor()`
   与 `EntryNotAnchorError` 的文档；三层代码级强制 + 活对照测试。

依赖方向（SPEC §4.0）

T-130 在 DAG 里的上游是 T-103（不可变原文）/ T-104（归一化文本层 + 偏移映射）。
本包因此**只** import `atlas.contracts`（契约类型）与 `atlas.normalize.text.decode_bytes`
（复用 T-104 已测试的严格解码链），不 import 任何兄弟包
（`feed` / `labels` / `evidence` / `webui` / `search` / `chunk` 都没碰）。

命名空间解析的取舍写在 `atlas.entries.xmlfeed` 的模块 docstring 里；
坐标口径（解码后文本的字符偏移 vs 字节偏移、与 `SegmentTable` 的关系）
写在 `atlas.entries.parser` 的模块 docstring 里。
"""

from .errors import (
    EntryError,
    EntryNotAnchorError,
    EntryParseError,
    EntryParserError,
)
from .ids import (
    ENTRY_ID_PREFIX,
    ENTRY_ID_RE,
    ENTRY_PARSER_VERSION,
    entry_id_for,
    fingerprint,
)
from .parser import (
    CURRENT_PARSER_VERSION,
    DEFAULT_PARSER,
    Entry,
    EntryParser,
    EntrySet,
    IdReport,
    SpanReport,
    SpanVerification,
    parse_entries,
    sanitize_for_quote,
    verify_ids,
    verify_offsets,
)
from .xmlfeed import FeedKind, FieldProblem, decode_feed

__all__ = [
    "CURRENT_PARSER_VERSION",
    "DEFAULT_PARSER",
    "ENTRY_ID_PREFIX",
    "ENTRY_ID_RE",
    "ENTRY_PARSER_VERSION",
    "Entry",
    "EntryError",
    "EntryNotAnchorError",
    "EntryParseError",
    "EntryParser",
    "EntryParserError",
    "EntrySet",
    "FeedKind",
    "FieldProblem",
    "IdReport",
    "SpanReport",
    "SpanVerification",
    "decode_feed",
    "entry_id_for",
    "fingerprint",
    "parse_entries",
    "sanitize_for_quote",
    "verify_ids",
    "verify_offsets",
]
