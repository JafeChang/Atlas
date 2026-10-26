"""HTML → 归一化文本，**带精确偏移**（SPEC §4.2 T-104）。

与 `text.py` 的分工：本模块负责**把 HTML 拆成原子**（`Atom`），
由 `text.assemble` 统一做空白折叠与段表构建。这样 HTML 与纯文本共用同一条段表逻辑。

实现方式：**自写扫描器**而非 DOM 解析器。产物要求"偏移可证明"——
扫描器按构造就知道每段文本在原文中的位置，无需在解析树与源串之间做猜测性对齐。

三类处理：

1. 跳过标签、注释、`<script>` / `<style>` 的内容
2. 块级标签处插入**合成换行原子**（`raw_length == 0`），供派生块定位使用
3. 文本区间的 HTML 实体各成一个原子（`raw_length` = 实体字面量长度）

`raw_length == 0` 的合成原子在 `SegmentTable` 里走退化段路径（恒定映射到插入点），
因此不破坏单调性。
"""

from __future__ import annotations

import html as _html
import re
from typing import List, Optional, Tuple

from .text import Atom, NormalizedText, assemble

#: 需要连同内容一起跳过的标签
SKIP_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template"})

#: 块级标签：边界插入合成换行，作为派生块的边界信号
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)

_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9\-]*)")

Event = Tuple[str, int, int, Optional[str]]
"""`(kind, start, end, name)`，`kind ∈ {"text", "tag", "skip"}`。"""


def _tag_name(raw: str, lt: int, gt: int) -> Optional[str]:
    m = _TAG_NAME_RE.match(raw, lt, gt + 1)
    return m.group(1).lower() if m else None


def scan_html(raw: str) -> List[Event]:
    """把 HTML 切成 text / tag / skip 事件序列。

    不做容错猜测：结构异常时按文本处理（宁可多留文本，也不吞掉内容）。
    """
    events: List[Event] = []
    i, n = 0, len(raw)
    lower = raw.lower()

    while i < n:
        lt = raw.find("<", i)
        if lt < 0:
            events.append(("text", i, n, None))
            break
        if lt > i:
            events.append(("text", i, lt, None))

        if raw.startswith("<!--", lt):
            close = raw.find("-->", lt)
            end = n if close < 0 else close + 3
            events.append(("skip", lt, end, None))
            i = end
            continue

        gt = raw.find(">", lt)
        if gt < 0:
            events.append(("text", lt, n, None))
            break

        name = _tag_name(raw, lt, gt)
        events.append(("tag", lt, gt + 1, name))
        i = gt + 1

        if name in SKIP_CONTENT_TAGS:
            close_at = lower.find(f"</{name}", i)
            if close_at < 0:
                events.append(("skip", i, n, None))
                i = n
            else:
                gt2 = raw.find(">", close_at)
                end = n if gt2 < 0 else gt2 + 1
                events.append(("skip", i, end, None))
                i = end

    return events


def _append_text_atoms(atoms: List[Atom], raw: str, start: int, end: int) -> None:
    """文本区间 → 原子：实体各成一个原子（映射到实体起点），其余原样搬运。"""
    if start >= end:
        return
    cursor = start
    for m in _ENTITY_RE.finditer(raw, start, end):
        if m.start() > cursor:
            atoms.append(Atom(raw[cursor : m.start()], cursor, m.start() - cursor))
        decoded = _html.unescape(m.group(0))
        atoms.append(Atom(decoded, m.start(), m.end() - m.start()))
        cursor = m.end()
    if cursor < end:
        atoms.append(Atom(raw[cursor:end], cursor, end - cursor))


def html_atoms(raw_text: str) -> List[Atom]:
    """HTML 文本 → 原子序列。"""
    atoms: List[Atom] = []
    for kind, start, end, name in scan_html(raw_text):
        if kind == "text":
            _append_text_atoms(atoms, raw_text, start, end)
        elif kind == "tag" and name in BLOCK_TAGS:
            atoms.append(Atom("\n", start, 0))
    return atoms


def normalize_html(
    raw_text: str, *, encoding: str, content_type: str = "text/html"
) -> NormalizedText:
    """已解码的 HTML 文本 → `NormalizedText`（`blocks` 为空，由 `blocks.attach_blocks` 附加）。"""
    return assemble(
        html_atoms(raw_text),
        raw_text=raw_text,
        encoding=encoding,
        content_type=content_type,
        source_kind="html",
    )


def looks_like_html(raw_text: str) -> bool:
    """在缺少 `Content-Type` 时的兜底嗅探（真实抓取里 content_type 常常缺失）。"""
    head = raw_text[:4096]
    return bool(re.search(r"<\s*(html|body|div|p|br|table|a|ul|li)\b", head, re.IGNORECASE))
