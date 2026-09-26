"""T-205 修订（CJK 逐字切分）：汉字逐字切开，纯函数、**可逆**（SPEC §2.15 的缺口修复）。

为什么需要这一层
----------------

FTS5 的 `unicode61` 没有词典分词：连续汉字被当成**一个词元**
（`中文分词测试` 是 1 个 token），于是查 `中文` 命不中 `中文分词测试`——
这是 T-205 交付时如实记录、并用测试钉住的真实缺口。

用户裁决：**保留 `unicode61`，改为"汉字逐字切分后再索引"**（不用 FTS5 `trigram`）——
索引膨胀小、英文 BM25 排序不变、零新依赖。做法是：

- 索引侧：`text_index = segment_cjk(text)`，FTS5 外部内容表指向 `text_index`；
- 查询侧：连续汉字变成 FTS5 **短语**（`中文分词` → `"中 文 分 词"`），
  而不是逐字 `AND`（后者会命中"世界**人**民**工**作**智**慧**能**力"这种假阳性）；
- 摘要侧：FTS5 的 `snippet()` 作用在切分后的列上，返回前必须把插入的分隔符还原掉
  （见 `atlas.search.snippet`）——否则摘要会露出 `中 文`。

切分规则（确切定义）
--------------------

`segment_cjk(text)`：在**两个相邻汉字之间的空白间隙**里插入**一个** ASCII 空格
（`" "`），插入位置是间隙的**末尾**，即紧贴第二个汉字之前；**其它位置一律不动**。

===========  ==================  ================
原文         切分结果             说明
===========  ==================  ================
`中文`        `中 文`             间隙为空
`中 文`       `中  文`            间隙已有空白 ⇒ 真实空格 + 1 个插入空格
`中a文`       `中a文`（不变）      中间是**非空白**字符 ⇒ 不是"相邻汉字间隙"
`abc`         `abc`（不变）        无汉字
===========  ==================  ================

**为什么间隙已有空白时也要插入**（这一步是可逆性的必要条件）：若只在"两个汉字直接
相邻"时插入，`中文` 与 `中 文` 会切出同一个串，逆映射不再唯一，原始文本里的真实空格
无法还原。插入之后：

- `中 文`（原文，一个真实空格）→ `中  文`（两个空格）→ 逆映射删**一个** → `中 文` ✔
- `中文`（原文）→ `中 文`（一个空格）→ 逆映射删**一个** → `中文` ✔

确切 Unicode 范围（硬编码，不依赖 `unicodedata` 的版本，因此跨 Python 版本可复现）
----------------------------------------------------------------------------------

| 区段 | 名称 |
|---|---|
| U+3400–U+4DBF | CJK Unified Ideographs Extension A |
| U+4E00–U+9FFF | CJK Unified Ideographs（**至少**要有的这一段） |
| U+F900–U+FAFF | CJK Compatibility Ideographs |
| U+20000–U+2EBEF | CJK Unified Ideographs Extensions B–F |
| U+2F800–U+2FA1F | CJK Compatibility Ideographs Supplement |
| U+30000–U+323AF | CJK Unified Ideographs Extensions G–H |

**包含扩展 A / 兼容表意 / 扩展 B–H 的理由**：它们的共同性质正是本缺陷的根因——
"`unicode61` 把每个码点当字母数字，但码点之间没有词边界"。排除任何一段都会在那一段上
留下同一个缺陷；而它们对**纯拉丁文本零影响**（判定函数只在遇到这些码点时才有动作），
所以放宽范围不付代价。

**不包含的**：

- 假名（U+3040–U+30FF）、注音符号、谚文：它们是**音节文字**，逐码点切开 + 短语查询
  同样可行，但那属于另一个裁决（本任务是"汉字逐字切分"），且会让 `INDEX_VERSION`
  的语义更含糊；**当前明确不做**。
- CJK 标点与部首（U+3000–U+303F、U+2E80–U+2FDF）：`unicode61` 本来就把它们当分隔符，
  切开对检索没有任何作用。
- 全角拉丁/数字：同上，`unicode61` 按码点归类，切开无意义。

可逆性
------

`desegment(segment_cjk(text)) == text` 对**任意** `str` 成立（有往返性质测试）。
判定"某个空格是不是切分器插入的"只有**一个**出处：`is_inserted_space`，
`desegment` 与摘要还原（`atlas.search.snippet`）共用它——不会出现两套规则打架。

一个**已知的残余边界**（不在本契约内，如实记录）：`中a文` 这种"汉字与拉丁**直接**相邻
且中间没有空白"的连写仍是一个词元，因此 `segment_cjk` 不动它（契约要求"其它位置一律
不动"）。真实语料里这需要"汉字↔拉丁边界也插入空格"的另一个裁决。
"""

from __future__ import annotations

from typing import List, Tuple

__all__ = [
    "CJK_RANGES",
    "desegment",
    "is_cjk",
    "is_inserted_space",
    "segment_cjk",
]

#: 参与"逐字切分"的汉字码点区段（闭区间）。理由见模块 docstring。
CJK_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x2EBEF),  # CJK Unified Ideographs Extensions B–F
    (0x2F800, 0x2FA1F),  # CJK Compatibility Ideographs Supplement
    (0x30000, 0x323AF),  # CJK Unified Ideographs Extensions G–H
)

#: ASCII 空格：切分器唯一会插入的字符（因此逆映射只需要认识这一个字符）。
SEPARATOR = " "

#: 版本标识：任一区段或插入规则变化都必须同时升 `SCHEMA_VERSION` / `INDEX_VERSION`。
SEGMENTATION_VERSION = "atlas.search.cjk/1"


def is_cjk(character: str) -> bool:
    """该字符是否是**参与逐字切分的汉字**（判定范围见 `CJK_RANGES`）。"""
    if len(character) != 1:
        raise TypeError(f"is_cjk 只接受单个字符（收到 {character!r}）")
    code = ord(character)
    for low, high in CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def segment_cjk(text: str) -> str:
    """在两个相邻汉字之间的空白间隙里插入一个空格（其余位置逐字符不变）。

    纯函数：无 I/O、无状态、同输入同输出。`desegment` 是它的逆。
    """
    if not isinstance(text, str):
        raise TypeError(f"segment_cjk 只接受 str（收到 {type(text).__name__}）")
    pieces: List[str] = []
    preceded_by_han = False  # 上一个"非空白"字符是否是汉字（跨空白保持）
    for character in text:
        if is_cjk(character):
            if preceded_by_han:
                pieces.append(SEPARATOR)
            pieces.append(character)
            preceded_by_han = True
        else:
            pieces.append(character)
            if not character.isspace():
                preceded_by_han = False
    return "".join(pieces)


def is_inserted_space(text: str, index: int) -> bool:
    """`text[index]` 是否为 `segment_cjk` 插入的那个分隔符。

    判定规则（`desegment` 与摘要还原共同使用，因此只有这一个出处）：

    1. `text[index]` 是 ASCII 空格；
    2. 紧跟其后的是汉字（插入点紧贴第二个汉字）；
    3. 从 `index` 往前跨过连续空白，遇到的是**汉字**（即这确实是"两个汉字之间的间隙"、
       且 `index` 是该间隙末尾，也就是插入点）。

    真实空格永远不满足第 3 条：它不可能同时是"间隙末尾"——若间隙里本来就有空白，
    `segment_cjk` 会在它**后面**再插一个，于是被判成插入的是后一个。
    """
    if not isinstance(text, str):
        raise TypeError(f"is_inserted_space 只接受 str（收到 {type(text).__name__}）")
    if index < 0 or index >= len(text) or text[index] != SEPARATOR:
        return False
    if index + 1 >= len(text) or not is_cjk(text[index + 1]):
        return False
    cursor = index
    while cursor - 1 >= 0 and text[cursor - 1].isspace():
        cursor -= 1
    return cursor - 1 >= 0 and is_cjk(text[cursor - 1])


def desegment(text: str) -> str:
    """`segment_cjk` 的左逆：删掉**恰好**那些由 `segment_cjk` 插入的空格。

    对任意 `str` 都有 `desegment(segment_cjk(text)) == text`（往返性质）；
    对切分像还有 `segment_cjk(desegment(x)) == x`（两侧互为逆）。
    """
    if not isinstance(text, str):
        raise TypeError(f"desegment 只接受 str（收到 {type(text).__name__}）")
    pieces: List[str] = []
    for index, character in enumerate(text):
        if character == SEPARATOR and is_inserted_space(text, index):
            continue
        pieces.append(character)
    return "".join(pieces)
