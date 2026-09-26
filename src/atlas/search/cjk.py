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

`segment_cjk(text)`：在**两个"词元字符"之间的空白间隙**里插入**一个** ASCII 空格
（`" "`），插入位置是间隙的**末尾**，即紧贴后一个词元字符之前；**其它位置一律不动**。

"需要插入"的判定（唯一出处：`_needs_separator`）——两个相邻（中间只有空白）的
词元字符之间，**至少一侧是汉字**时插入：

| 前 → 后 | 插？ | 例 |
|---|---|---|
| 汉字 → 汉字 | 插 | `中文` → `中 文` |
| 汉字 → 字母/数字 | 插 | `Transformer架构` → `Transformer 架 构`；`第3章` → `第 3 章` |
| 字母/数字 → 汉字 | 插 | `abc中文` → `abc 中 文` |
| 字母/数字 → 字母/数字 | **不插** | `vector database` 原样（纯英文恒等变换） |
| 任一侧是标点/符号/`_` | **不插** | `中-文` 原样（`unicode61` 本来就把它们当分隔符） |

"词元字符"= FTS5 `unicode61` 当作 token 的字符 = `str.isalnum()`（**不含** `_`），
与查询侧的切词正则 `[^\\W_]+` 完全一致（有测试逐码点核对两者等价）。

**为什么"汉字↔字母/数字"也要切**：`unicode61` 把 `Transformer架构` 当成**一个**
token，于是 `架构` 查不到它；同理 `GPT模型` 查不到 `模型`、`向量数据库abc` 查不到
`向量数据库`。中文技术文本里这种混写是常态，是**正确性缺陷**而不是优化项
（实测证据见 `tools/t205cjk_boundary_probe.py`）。

**为什么间隙已有空白时也要插入**（这一步是可逆性的必要条件）：若某个"需要插入"的
边界只在两侧直接相邻时插入，`中文` 与 `中 文` 会切出同一个串，逆映射不再唯一，
原始文本里的真实空格无法还原。插入之后：

- `中 文`（原文，一个真实空格）→ `中  文`（两个空格）→ 逆映射删**一个** → `中 文` ✔
- `中文`（原文）→ `中 文`（一个空格）→ 逆映射删**一个** → `中文` ✔
- `中 abc`（原文）→ `中  abc`（两个空格）→ 逆映射删**一个** → `中 abc` ✔

代价：混合中英文本的 `text_index` 会多出少量空格（**纯英文一个也不多**）。

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
判定"某个空格是不是切分器插入的"只有**一个**出处：`is_inserted_space`
（它调用与 `segment_cjk` 同一个 `_needs_separator` 判定），`desegment` 与摘要还原
（`atlas.search.snippet`）共用它——不会出现两套规则打架。

纯拉丁文本上 `segment_cjk` 是**恒等变换**（没有任何一侧是汉字），因此英文索引内容
逐字节不变、BM25 与摘要不变（有对照索引测试钉死）。
"""

from __future__ import annotations

from typing import List, Tuple

__all__ = [
    "CJK_RANGES",
    "desegment",
    "is_cjk",
    "is_inserted_space",
    "is_token_character",
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

#: 版本标识：任一区段或插入规则变化都必须同时升 `INDEX_VERSION`（见 `sqlite_index`）。
#: `/2` = 插入规则从"仅汉字↔汉字"扩到"至少一侧是汉字的任意词元字符边界"。
SEGMENTATION_VERSION = "atlas.search.cjk/2"


def is_cjk(character: str) -> bool:
    """该字符是否是**参与逐字切分的汉字**（判定范围见 `CJK_RANGES`）。"""
    if len(character) != 1:
        raise TypeError(f"is_cjk 只接受单个字符（收到 {character!r}）")
    code = ord(character)
    for low, high in CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def is_token_character(character: str) -> bool:
    """该字符是否是 FTS5 `unicode61` 的**词元字符**（字母或数字，**不含** `_`）。

    这就是查询侧切词正则 `[^\\W_]+` 的逐字符版本：CPython 的 `\\w` =
    `str.isalnum()` ∪ `{_}`，因此 `[^\\W_]` 与 `str.isalnum()` 等价
    （`tests/test_search_cjk.py` 逐码点核对这一点）。

    标点/符号/空白/`_` 都不是词元字符 ⇒ `unicode61` 本来就在它们处断开，
    切分器对它们**不需要**插入任何东西。
    """
    if len(character) != 1:
        raise TypeError(f"is_token_character 只接受单个字符（收到 {character!r}）")
    return character.isalnum()


def _needs_separator(previous: str, current: str) -> bool:
    """两个相邻（中间只有空白）的**非空白**字符之间是否需要插入分隔符。

    **切分规则与逆映射判定的唯一出处**：`segment_cjk` 与 `is_inserted_space` 都调它，
    因此"插了什么"与"删什么"不可能不一致。

    成立条件（两条都满足）：

    1. 两侧都是 `unicode61` 的词元字符（否则它本来就会断开，插了也没用）；
    2. **至少一侧是汉字**——汉字↔汉字（逐字切分的本意）与汉字↔字母/数字
       （`Transformer架构`、`GPT模型`、`向量数据库abc`：不切就查不到 `架构`/`模型`/
       `向量数据库`）。字母↔字母不插 ⇒ 纯英文恒等变换。
    """
    return (
        is_token_character(previous)
        and is_token_character(current)
        and (is_cjk(previous) or is_cjk(current))
    )


def segment_cjk(text: str) -> str:
    """在"至少一侧是汉字"的两个词元字符之间的空白间隙里插入一个空格。

    其余位置逐字符不变（纯拉丁文本因此是恒等变换）。
    纯函数：无 I/O、无状态、同输入同输出。`desegment` 是它的逆。
    """
    if not isinstance(text, str):
        raise TypeError(f"segment_cjk 只接受 str（收到 {type(text).__name__}）")
    pieces: List[str] = []
    previous: str | None = None  # 上一个**非空白**字符（跨空白保持）
    for character in text:
        if character.isspace():
            pieces.append(character)
            continue
        if previous is not None and _needs_separator(previous, character):
            pieces.append(SEPARATOR)
        pieces.append(character)
        previous = character
    return "".join(pieces)


def is_inserted_space(text: str, index: int) -> bool:
    """`text[index]` 是否为 `segment_cjk` 插入的那个分隔符。

    判定规则（`desegment` 与摘要还原共同使用，因此只有这一个出处）：

    1. `text[index]` 是 ASCII 空格；
    2. 紧跟其后的是**词元字符**（插入点永远紧贴后一个词元字符，因此空格后面绝不是空白）；
    3. 从 `index` 往前跨过连续空白，遇到的是**词元字符**（存在"前一个词元字符"）；
    4. `_needs_separator(前一个词元字符, 后一个词元字符)` 成立——
       与 `segment_cjk` 用的是**同一个**判定。

    真实空格永远不满足第 2 条：若某个间隙里本来就有空白、且该边界需要插入，
    `segment_cjk` 会在真实空白**后面**再插一个，于是被判成"插入的"是后一个，
    真实空白的下一个字符仍是空白。若该边界**不需要**插入（例如 `a b`、`中-文`），
    第 4 条不成立。
    """
    if not isinstance(text, str):
        raise TypeError(f"is_inserted_space 只接受 str（收到 {type(text).__name__}）")
    if index < 0 or index >= len(text) or text[index] != SEPARATOR:
        return False
    following = index + 1
    if following >= len(text) or not is_token_character(text[following]):
        return False
    cursor = index
    while cursor - 1 >= 0 and text[cursor - 1].isspace():
        cursor -= 1
    if cursor - 1 < 0:
        return False
    return _needs_separator(text[cursor - 1], text[following])


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
