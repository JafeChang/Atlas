"""归一化文本层 + 偏移映射（SPEC §2.2 方案 2C / §4.2 T-104）。

纯函数，无 I/O：

```python
from atlas.normalize import normalize
nt = normalize(raw_bytes, content_type)
nt.text                       # 归一化文本
nt.raw_text                   # 解码后的原文（to_raw_offset 的参照系）
nt.to_raw_offset(3)           # 归一化偏移 → 原文偏移；满足 OffsetMap 签名
nt.raw_slice(3, 9)            # 归一化区间 → 原文切片
nt.blocks                     # 派生块定位（可重建，不可作为人工锚点）

# 与 T-002 的证据锚点打通
from atlas.contracts import build_anchor
status, anchor, derived = build_anchor(
    raw_id=..., raw_sha256=..., normalized_text=nt.text,
    quote="...", to_raw_offset=nt.to_raw_offset,
)
```

**`normalize` 是完整入口**（等价于 `text.normalize` 之后附加派生块）；
`text.normalize` 是底层入口（`blocks` 为空）。
"""

from .blocks import attach_blocks, block_of, derive_blocks
from .html import (
    BLOCK_TAGS,
    SKIP_CONTENT_TAGS,
    html_atoms,
    looks_like_html,
    normalize_html,
    scan_html,
)
from .text import (
    HTML_MIME_TYPES,
    Atom,
    NormalizedText,
    NormalizeError,
    Segment,
    SegmentTable,
    assemble,
    decode_bytes,
    detect_encoding,
    mime_type,
    normalize as _normalize_without_blocks,
    normalize_plain_text,
    plain_atoms,
)


def normalize(raw_bytes: bytes, content_type: str = "") -> NormalizedText:
    """完整入口：解码 → 归一化 → 附加派生块。"""
    return attach_blocks(_normalize_without_blocks(raw_bytes, content_type))


__all__ = [
    "BLOCK_TAGS",
    "HTML_MIME_TYPES",
    "SKIP_CONTENT_TAGS",
    "Atom",
    "NormalizedText",
    "NormalizeError",
    "Segment",
    "SegmentTable",
    "assemble",
    "attach_blocks",
    "block_of",
    "decode_bytes",
    "derive_blocks",
    "detect_encoding",
    "html_atoms",
    "looks_like_html",
    "mime_type",
    "normalize",
    "normalize_html",
    "normalize_plain_text",
    "plain_atoms",
    "scan_html",
]
