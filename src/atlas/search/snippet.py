"""T-205 修订：把 FTS5 的摘要从**切分后的文本**还原成**原始文本**（禁止泄漏分隔符）。

问题（实测）
------------

索引列是 `segment_cjk(text)`（例如 `中 文 分 词`），FTS5 的 `snippet()` /
`highlight()` 从**内容表里同名的列**取值并只负责包高亮标记，因此它返回的是：

    '[中 文 分 词] 测 试 与 检…'      ← 插入的分隔符露出来了

摘要里出现 `中 文` 是**纯实现细节外泄**：那是我们为了索引插进去的空格，用户从没打过。

结构性保证
----------

摘要必须从**原始 `text`** 生成，而不是从切分后的列取。本模块的做法是：

1. 在 SQL 里用**哨兵字符**（私用区 U+E000–U+E002）当高亮开/闭标记与省略号，
   于是渲染结果可以无歧义地拆成「原文片段」与「标记」两类片段；
2. 片段内容必然是索引列（`text_index`）的连续子串（FTS5 原样复制内容表的值）；
3. 在索引列里定位该片段，用 `atlas.search.cjk.is_inserted_space` 逐字符判定
   "这个空格是不是切分器插入的"，是则删除、否则保留；
4. 把哨兵替换回对外契约的 `HIGHLIGHT_OPEN` / `HIGHLIGHT_CLOSE` / `SNIPPET_ELLIPSIS`。

于是摘要的正文**逐字符来自原始文本**（去掉标记与省略号后，每一段都是原文的子串），
插入的分隔符**结构上不可能**出现在结果里——有测试直接断言这个子串性质。

英文（无汉字）文本上，索引列 `==` 原始文本，没有"插入的空格"可删，本函数是**恒等变换**，
因此英文摘要与 T-205 逐字节相同（有对照索引测试钉死）。
"""

from __future__ import annotations

import re
from typing import Iterator, List, Optional, Sequence, Tuple

from .cjk import is_inserted_space
from .errors import SearchIndexError
from .query import HIGHLIGHT_CLOSE, HIGHLIGHT_OPEN, SNIPPET_ELLIPSIS

__all__ = [
    "SNIPPET_SENTINEL_CLOSE",
    "SNIPPET_SENTINEL_ELLIPSIS",
    "SNIPPET_SENTINEL_OPEN",
    "restore_snippet",
]

#: 私用区哨兵：只在本层与 SQL 之间使用，**绝不**出现在对外返回的摘要里。
#: 用私用区而不是 `\x00`：SQLite 的文本值允许 NUL，但不少文本 API 会在 NUL 处截断。
SNIPPET_SENTINEL_OPEN = "\ue000"
SNIPPET_SENTINEL_CLOSE = "\ue001"
SNIPPET_SENTINEL_ELLIPSIS = "\ue002"

_SENTINELS = {
    SNIPPET_SENTINEL_OPEN: "open",
    SNIPPET_SENTINEL_CLOSE: "close",
    SNIPPET_SENTINEL_ELLIPSIS: "ellipsis",
}
_SENTINEL_RE = re.compile("[" + SNIPPET_SENTINEL_OPEN + SNIPPET_SENTINEL_CLOSE + SNIPPET_SENTINEL_ELLIPSIS + "]")

_KIND_OPEN = "open"
_KIND_CLOSE = "close"
_KIND_ELLIPSIS = "ellipsis"
_KIND_TEXT = "text"


def _split(rendered: str) -> List[Tuple[str, str]]:
    """把哨兵渲染串拆成有序的 `(kind, text)` 片段（`text` 只对 `KIND_TEXT` 有意义）。"""
    tokens: List[Tuple[str, str]] = []
    buffer: List[str] = []
    for character in rendered:
        kind = _SENTINELS.get(character)
        if kind is None:
            buffer.append(character)
            continue
        if buffer:
            tokens.append((_KIND_TEXT, "".join(buffer)))
            buffer.clear()
        tokens.append((kind, ""))
    if buffer:
        tokens.append((_KIND_TEXT, "".join(buffer)))
    return tokens


def _candidate_offsets(indexed_text: str, body: str) -> Iterator[int]:
    start = 0
    while True:
        found = indexed_text.find(body, start)
        if found < 0:
            return
        yield found
        start = found + 1


def _headline_pieces(
    tokens: Sequence[Tuple[str, str]], dropped: Sequence[bool]
) -> List[str]:
    """按 `dropped` 掩码输出正文片段（标记原样保留为对外契约值）。"""
    pieces: List[str] = []
    cursor = 0
    for kind, text in tokens:
        if kind == _KIND_TEXT:
            kept: List[str] = []
            for character in text:
                if not dropped[cursor]:
                    kept.append(character)
                cursor += 1
            pieces.append("".join(kept))
        elif kind == _KIND_OPEN:
            pieces.append(HIGHLIGHT_OPEN)
        elif kind == _KIND_CLOSE:
            pieces.append(HIGHLIGHT_CLOSE)
        else:
            pieces.append(SNIPPET_ELLIPSIS)
    return pieces


def restore_snippet(
    rendered: str, *, indexed_text: str, original_text: str
) -> str:
    """把哨兵渲染的摘要还原成**原始文本**上的摘要（对外契约的标记）。

    - `rendered`：`snippet(fts, 0, SNIPPET_SENTINEL_OPEN, SNIPPET_SENTINEL_CLOSE,
      SNIPPET_SENTINEL_ELLIPSIS, n)` 的返回值（作用在 `text_index` 列上）；
    - `indexed_text`：该文档的 `text_index`（`segment_cjk(original_text)`）；
    - `original_text`：该文档的 `text`（归一化后的原始文本）。

    还原结果是原文的片段 + 高亮标记 + 省略号，**不含任何插入的分隔符**。
    定位不到一致映射时**响亮失败**（`SearchIndexError`），不静默返回可能泄漏分隔符的串。
    """
    for name, value in (
        ("rendered", rendered),
        ("indexed_text", indexed_text),
        ("original_text", original_text),
    ):
        if not isinstance(value, str):
            raise SearchIndexError(
                f"restore_snippet 的 {name} 必须是 str（收到 {type(value).__name__}）"
            )
    if rendered == "":
        return ""

    tokens = _split(rendered)
    body = "".join(text for kind, text in tokens if kind == _KIND_TEXT)
    if body == "":
        return "".join(_headline_pieces(tokens, []))

    for offset in _candidate_offsets(indexed_text, body):
        dropped = [
            character == " " and is_inserted_space(indexed_text, offset + position)
            for position, character in enumerate(body)
        ]
        mapped = "".join(
            character for character, drop in zip(body, dropped) if not drop
        )
        if mapped in original_text:
            return "".join(_headline_pieces(tokens, dropped))

    raise SearchIndexError(
        "摘要还原失败：FTS5 片段在索引列里找不到与原始文本一致的位置"
        f"（body={body[:60]!r}…）。索引列与原始文本不同步，"
        "这是接线错误，拒绝返回一个可能泄漏切分分隔符的摘要"
    )
