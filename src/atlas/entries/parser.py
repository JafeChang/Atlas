"""条目化派生层：一份 feed → 条目序列（纯函数、可重建；SPEC §4.2 T-130 / §6.3 裁决 B）。

**为什么需要这一层**（SPEC §6.3）：新结构把**整个 HTTP 响应体**当作一篇文档归档。
实测 `data/store` 里 10 条归档记录内含 **830 篇**文章，没有一篇成为独立文档。
于是 `raw_id` 标识的是一份 **feed**，而 §2.1 规定人工标签锚在 `raw_id` 上
⇒ 只能给"整份 synced-review feed"打标，给不了一篇文章打标。

用户裁决的**方案 B**：feed 仍是 Raw（`raw_id` 不变，Raw 只增不改保住），
另建**条目派生层**——零额外网络请求（正文已在 feed 里）、零 schema 迁移。
本包就是那一层。

坐标口径：**解码后文本的字符偏移**，不是字节偏移（本任务最关键的设计决定）
==========================================================================

条目区间 `[char_start, char_end)` 定义在**解码后的 feed 文本**上：

    text = decode_bytes(raw_bytes, content_type)[0]     # T-104 的严格解码链
    entry_content = text[entry.char_start : entry.char_end]

这正是 SPEC §2.2 真值 `{raw_id, raw_sha256, char_start, char_end}` 的参照系
—— T-107 的 `EvidenceAnchor.char_start/char_end` 落在同一个空间上
（`atlas.evidence.verify` 里的 `normalized.raw_text[anchor.char_start:anchor.char_end]`
就是按解码后原文切片来校验锚点的，见该模块 `_assert_anchor_represents_quote`）。

**因此判定"某条证据属于哪个条目"是纯整数比较**：quote 的锚点落在哪个条目的
字符区间里，它就属于哪个条目。这是 T-107 / T-108 / T-109 集成的基础。

与 T-104 `SegmentTable` 的关系
------------------------------

`SegmentTable` 是**归一化文本 ↔ 解码后原文**之间的映射（`to_raw_offset`）。
条目区间定义在它的**上域**（解码后原文）上，因此：

- 条目化**不需要** `SegmentTable`：拿 `decode_bytes` 的文本就能定区间；
- 要让条目对上 T-106 / T-107 的**归一化**坐标，必须用同一次 `normalize` 的
  `SegmentTable` 把归一化偏移映射到原文偏移（方向是 归一化 → 原文，与条目同向）；
- 本包**刻意不依赖归一化结果**（不读 `NormalizedText.blocks`，也不重跑 html 路径）：
  否则换 HTML 解析器会让条目边界跟着漂移，而条目边界来自 XML 结构，本不该受影响。

条目 ID 与锚点的边界（**与 T-206 的区别必须说清楚**）
====================================================

T-206 的结论是"分块**整体**不得作锚点"：分块连字符区间都没有
（`Chunk` 没有 `raw_sha256` / `char_start` / `char_end` 字段）。

T-130 的结论**更窄**：

| 量 | 性质 | 能不能当锚点 |
|---|---|---|
| `Entry.char_start` / `char_end` | 条目在解码后原文里的字符区间 —— SPEC §2.2 的真值形状 | ✅ **正是锚点**：`Entry.anchor()` 返回合法 `EvidenceAnchor` |
| `Entry.entry_id` | `ent_` + sha256(raw_id, raw_sha256, 解析器版本, 区间, 标题)[:32]，**派生** | ❌ 永远不是：`Entry.as_anchor()` **永远抛** |

三层代码级强制（照 `atlas.chunk` 已定的做法，不另发明）：

1. **结构上**：`Entry` 的字段里没有任何"锚点"字段；`as_anchor()` **永远抛**
   `EntryNotAnchorError`（不返回 `None`、不打 warning）；
   `Entry` 与 `EvidenceAnchor` / `DerivedLocator` 类型不同，塞错地方 pydantic 直接拒。
2. **形状上**：`entry_id` 形如 `ent_…`，而 `EvidenceAnchor.raw_sha256` 要求
   `^[0-9a-f]{64}$` —— 前缀 + 长度都不符，永远过不了字段校验。
3. **机制证明**：换解析器版本 → 同一篇条目得到不同 `entry_id`，而
   `(raw_id, raw_sha256, char_start, char_end)` **逐字段不变**。这就是"ID 不能当锚点、
   字符区间可以"的实证理由，而不是一句声明。见 `tests/test_entries_anchors.py`。

用法
====

```python
from atlas.entries import parse_entries

entrieset = parse_entries(raw_bytes, "application/rss+xml", raw_id="raw_…")
entrieset.text                       # 解码后的 feed 文本（区间即此文本上的偏移）
entrieset.entries[0].title
entrieset.entries[0].link
entrieset.entries[0].raw_slice(entrieset.text)   # 该条目的原文切片（含 XML 标记）
entrieset.entries[0].content_slice(entrieset.text)  # 正文切片
entrieset.entries[0].anchor()        # ✅ 合法 EvidenceAnchor（由字符区间构造）
entrieset.entries[0].as_anchor()     # ❌ 永远抛 EntryNotAnchorError（ID 不是锚点）
entrieset.problems                   # 字段缺失 / 空 feed / 未知格式的理由

# 独立重算（人工核验入口，不依赖本包的任何缓存）
from atlas.entries import verify_offsets, verify_ids
report = verify_offsets(raw_id=..., raw_bytes=raw_bytes, content_type=..., entrieset=entrieset)
report.ok, report.failures
verify_ids(raw_id=..., raw_bytes=raw_bytes, entrieset=entrieset).ok
```

**可重建**：丢弃 `EntrySet` 后，用同样的 `(raw_bytes, content_type, raw_id, parser)`
重跑必然得到**逐字节相同**的产物（含全部 ID）。解析器版本记在产物里
（`EntrySet.parser_version` + `parser_fingerprint`），且**版本参与 ID 计算**——
否则"换了解析器但 ID 没变"会让派生量看起来稳定、实际已经漂移。
"""

from __future__ import annotations

import hashlib
import html as _html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Optional, Tuple

from atlas.contracts import DerivedLocator, EvidenceAnchor
from atlas.contracts.ids import content_sha256

from .errors import EntryError, EntryNotAnchorError, EntryParserError
from .ids import ENTRY_ID_RE, ENTRY_PARSER_VERSION, entry_id_for, fingerprint
from .xmlfeed import (
    FeedKind,
    FieldProblem,
    decode_feed,
    parse_feed_text,
)

__all__ = [
    "CURRENT_PARSER_VERSION",
    "DEFAULT_PARSER",
    "Entry",
    "EntryParser",
    "EntrySet",
    "IdReport",
    "SpanReport",
    "SpanVerification",
    "decode_feed",
    "parse_entries",
    "sanitize_for_quote",
    "verify_ids",
    "verify_offsets",
]

#: 当前解析器版本（模块级常量 = 文档里写的默认值；SPEC §2.12 同一条纪律）。
CURRENT_PARSER_VERSION = ENTRY_PARSER_VERSION

#: 定宽 UTC 串的形状（与 T-205 索引的时间键同构）。
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")

_TAG_RE = re.compile(r"<[^>]*>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def sanitize_for_quote(text: str) -> str:
    """把一段**标记文本**变成可以拿去 `match_quote` 的纯文本。

    为什么必须有这一步：条目正文里混着两种东西 ——

    - 真实 RSS 常见 `<description><![CDATA[<p>正文…</p>]]></description>`：
      CDATA 里是 **HTML 标记 + 转义实体**；
    - Atom 常见 `<content type="html">&lt;p&gt;正文…&lt;/p&gt;</content>`：
      实体解码后是 HTML 标记。

    两者都**不在** T-104 的归一化路径上（feed 是 XML，不是 HTML 文档）。
    若把带标记的原始字节直接当 quote 交给 T-107，匹配会因为标记与空白差异失败并
    被标成"未验证"—— 那不是真的找不到证据，而是**引文形态没对齐**。
    本函数只做两件确定性的事：**解实体 → 去标签**（与
    `atlas.entries.xmlfeed` 里 `type="html"` 字段的处理同一口径）。

    **重要**：quote 的坐标只能由确定性匹配产出（SPEC §2.2）。因此
    `sanitize_for_quote` 的产物**不能**直接映射回原文偏移；正确用法是把它与
    归一化文本一起交给 `atlas.contracts.build_anchor` / `atlas.evidence.verify_quote`
    去算坐标。本函数不做任何坐标运算。
    """
    if not isinstance(text, str):
        raise EntryError(f"text 必须是 str，得到 {type(text).__name__}")
    plain = _CDATA_RE.sub(r"\1", text)
    plain = _html.unescape(plain)
    plain = _TAG_RE.sub(" ", plain)
    return _html.unescape(plain)


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Entry:
    """一个 feed 条目：**派生** ID + **真值**字符区间 + 字段。

    | 字段 | 性质 | 说明 |
    |---|---|---|
    | `entry_id` | **派生** | `ent_…`，参与计算的还有解析器版本；**永不作锚点** |
    | `raw_id` | 溯源 | 条目所属的不可变 Raw（一份 feed） |
    | `raw_sha256` | **真值** | 该 Raw 的字节指纹（`EvidenceAnchor` 的必需字段） |
    | `index` | 文档序 | 0 起，便于报告与调试 |
    | `parser_version` | 重建依据 | 产出该条目的解析器版本 |
    | `kind` | `rss` / `atom` | 条目来自哪种 feed 形态 |
    | `char_start` / `char_end` | **真值** | 解码后 feed 文本上的字符区间，左闭右开 |
    | `title` / `link` / `published_at` | 字段 | 解析结果（缺失/不可解析时为空串 / `None`） |
    | `entry_text` | 字段 | 条目的可读正文（CDATA 已展开，仍是标记文本） |
    | `problems` | 审计 | 本条目字段缺失 / 无法解析的理由，**绝不静默** |

    `char_end - char_start == len(slice_text)`，其中 `slice_text` 是
    `<item>…</item>`（**不含**结束标签）在解码后文本上的切片 ——
    `verify_offsets()` 用这条关系独立重算。
    """

    entry_id: str
    raw_id: str
    raw_sha256: str
    index: int
    parser_version: str
    kind: str
    char_start: int
    char_end: int
    title: str
    link: str
    published_at: Optional[str]
    entry_text: str
    problems: tuple[FieldProblem, ...] = ()

    def __post_init__(self) -> None:
        if not ENTRY_ID_RE.match(self.entry_id):
            raise EntryError(
                f"entry_id 形状非法（应形如 ent_ + 32 位十六进制）：{self.entry_id!r}"
            )
        if not self.raw_id:
            raise EntryError("raw_id 不得为空：条目必须能溯源到不可变原文")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise EntryError("raw_sha256 必须是 64 位小写十六进制内容指纹")
        if not self.parser_version:
            raise EntryError("parser_version 不得为空：ID 必须携带解析器版本")
        if self.index < 0:
            raise EntryError(f"index 不得为负：{self.index}")
        if isinstance(self.char_start, bool) or not isinstance(self.char_start, int):
            raise EntryError("char_start 必须是 int")
        if isinstance(self.char_end, bool) or not isinstance(self.char_end, int):
            raise EntryError("char_end 必须是 int")
        if self.char_start < 0:
            raise EntryError(f"char_start 不得为负：{self.char_start}")
        if self.char_end <= self.char_start:
            raise EntryError(f"非法条目区间：[{self.char_start}, {self.char_end})")
        if not self.title.strip():
            raise EntryError(
                "条目文本不得为空：无标题条目不得进入条目序列（理由记在 problems 里）"
            )
        if self.published_at is not None and not _ISO_UTC_RE.match(self.published_at):
            raise EntryError(
                f"published_at 必须是定宽 UTC 串（%Y-%m-%dT%H:%M:%S.%f+00:00）："
                f"{self.published_at!r}"
            )

    # -- 派生视图 -----------------------------------------------------------------

    @property
    def length(self) -> int:
        return self.char_end - self.char_start

    @property
    def span(self) -> Tuple[int, int]:
        """字符区间（SPEC §2.2 的真值形状之一，**不含** raw_id / raw_sha256）。"""
        return self.char_start, self.char_end

    @property
    def truth_fields(self) -> Tuple[str, str, int, int]:
        """**完整**真值四元组：`(raw_id, raw_sha256, char_start, char_end)`。

        这就是 SPEC §2.2 定义的锚点真值，可以直接构造 `EvidenceAnchor`。
        """
        return (self.raw_id, self.raw_sha256, self.char_start, self.char_end)

    def anchor(self) -> EvidenceAnchor:
        """由**字符区间**构造真值锚点（✅ 合法，有活对照测试钉死）。

        为什么这是对的：条目区间**不是**解析器"猜"出来的语义位置，而是
        `<item>…</item>` 这一步确定性切片在解码后原文上的**实际偏移**。
        区间改变只可能来自：① 原文换了（`raw_sha256` 随之改变）或
        ② 解析器改了 XML 骨架识别规则（那时区间**确实**指向别处，锚点也应该跟着变）。

        反过来说：`entry_id` **不是**这样的量（它还掺了解析器版本与标题提取），
        所以 `as_anchor()` 永远抛 —— 这也正是本任务要划清的边界。
        """
        return EvidenceAnchor.create(
            raw_id=self.raw_id,
            raw_sha256=self.raw_sha256,
            char_start=self.char_start,
            char_end=self.char_end,
        )

    def locator(self) -> DerivedLocator:
        """T-002 的**派生**定位器（明确标注为派生；不得当锚点用）。"""
        return DerivedLocator(block_id=self.entry_id)

    def raw_slice(self, feed_text: str) -> str:
        """本条目的**原文切片**（解码后 feed 文本上的区间；包含 XML 标记）。"""
        return feed_text[self.char_start:self.char_end]

    def content_slice(self, feed_text: str) -> str:
        """本条目的**可读文本切片**（`raw_slice` 去掉标记与 CDATA 外壳）。

        用于"区间真的对应那个条目的内容"这条回环断言。只删标记、只脱 CDATA 外壳，
        **不做实体解码** —— 实体解码会把字符数与偏移解耦（见 SPEC §2.2 的实体澄清），
        这一步的产物因此不能拿去算坐标。
        """
        return _TAG_RE.sub("", _CDATA_RE.sub(r"\1", self.raw_slice(feed_text)))

    def recompute_id(self) -> str:
        """独立重算 `entry_id`（判据自检入口；`verify_ids` 对整集做同一件事）。"""
        return entry_id_for(
            raw_id=self.raw_id,
            raw_sha256=self.raw_sha256,
            parser_version=self.parser_version,
            char_start=self.char_start,
            char_end=self.char_end,
            title=self.title,
        )

    # -- 锚点误用的响亮失败 ---------------------------------------------------------

    def as_anchor(self) -> NoReturn:
        """**永远抛错**：条目 ID 是派生物，不得充当证据/人工产物锚点。

        刻意不返回 `None`、不打 warning —— 静默接受正是 SPEC §7.3 记录的头号
        失败模式。请改用 `Entry.anchor()`（字符区间**才是**真值）。
        """
        raise EntryNotAnchorError(
            f"条目 {self.entry_id}（{self.kind}，字符区间 "
            f"[{self.char_start}, {self.char_end})）的 **ID** 是派生量，"
            "不得作为证据锚点或人工标签锚点：它掺入了解析器版本与标题提取规则，"
            "换解析器后会整体漂移。真值锚点是它的**字符区间** —— 请用 "
            "`Entry.anchor()`（等价于 SPEC §2.2 的 "
            "{raw_id, raw_sha256, char_start, char_end}），或 "
            "`EvidenceAnchor.create(**dict(zip(('raw_id','raw_sha256','char_start','char_end'), e.truth_fields)))`。"
        )


# --------------------------------------------------------------------------- #
# 一次解析的产物
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EntrySet:
    """一次条目化的完整产物：条目 + **解析器快照** + 解码后的 feed 文本。

    `feed_text` 与 `encoding` 一并留在产物里，因此**回环校验不需要重新解码**：
    `verify_offsets()` 只需重新算出 `sha256(feed_text.encode(encoding))` 与
    `raw_sha256` 比对，就能证明"这份文本确实是那个 Raw 的字节解码出来的"，
    随后逐条重算切片。调用方仍然可以只拿 `raw_bytes` 走
    `verify_offsets(raw_id=…, raw_bytes=…)` 完全独立地复核。
    """

    entries: tuple[Entry, ...]
    raw_id: str
    raw_sha256: str
    kind: str
    parser_version: str
    encoding: str
    feed_text: str
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.raw_id:
            raise EntryError("raw_id 不得为空")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise EntryError("raw_sha256 必须是 64 位小写十六进制内容指纹")
        if not self.parser_version:
            raise EntryError("parser_version 不得为空")
        if not self.encoding:
            raise EntryError("encoding 不得为空：区间所在的文本必须说明它是怎么解出来的")
        if not isinstance(self.kind, str) or self.kind not in FeedKind.ALL:
            raise EntryError(f"未知 feed 形态：{self.kind!r}（合法值 {list(FeedKind.ALL)}）")
        previous = -1
        for entry in self.entries:
            if not isinstance(entry, Entry):
                raise EntryError(f"entries 里出现 {type(entry).__name__}，必须是 Entry")
            if entry.raw_id != self.raw_id:
                raise EntryError(
                    f"entry[{entry.index}].raw_id={entry.raw_id!r} 与 EntrySet.raw_id 不一致"
                )
            if entry.raw_sha256 != self.raw_sha256:
                raise EntryError(
                    f"entry[{entry.index}].raw_sha256 与 EntrySet.raw_sha256 不一致"
                )
            if entry.parser_version != self.parser_version:
                raise EntryError(
                    f"entry[{entry.index}].parser_version={entry.parser_version!r} "
                    f"与 EntrySet.parser_version={self.parser_version!r} 不一致"
                )
            if entry.index <= previous:
                raise EntryError(
                    f"条目文档序必须严格递增：entry[{entry.index}] 出现在 index={previous} 之后"
                )
            previous = entry.index
            if entry.char_end > len(self.feed_text):
                raise EntryError(
                    f"entry[{entry.index}] 区间越过 feed 文本末尾："
                    f"char_end={entry.char_end} > len(text)={len(self.feed_text)}"
                )
        for position in range(1, len(self.entries)):
            left = self.entries[position - 1]
            right = self.entries[position]
            if right.char_start < left.char_end:
                raise EntryError(
                    f"条目区间重叠或逆序：entry[{left.index}] 止于 {left.char_end}，"
                    f"entry[{right.index}] 起于 {right.char_start}"
                )

    # -- 派生视图 -----------------------------------------------------------------

    @property
    def parser_fingerprint(self) -> str:
        """解析器快照的完整摘要：重建自检用（与首次不符即说明解析器漂移）。"""
        return fingerprint((self.parser_version, self.kind))

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def links(self) -> tuple[str, ...]:
        return tuple(entry.link for entry in self.entries)

    def by_id(self, entry_id: str) -> Optional[Entry]:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    # -- 自检（不需要原文；靠产物自带的信息） -----------------------------------------

    def verify_ids(self) -> bool:
        """独立重算每个 `entry_id` 并与记录比对。"""
        return all(entry.recompute_id() == entry.entry_id for entry in self.entries)

    def verify_spans(self) -> "SpanReport":
        """本产物内部的区间自检（不重新解码；`sha256(feed_text)` 必须等于 `raw_sha256`）。"""
        digest = hashlib.sha256(self.feed_text.encode(self.encoding)).hexdigest()
        failures: list[str] = []
        if digest != self.raw_sha256:
            failures.append(
                "产物自带的 feed_text 与 raw_sha256 不符："
                f"sha256(text)={digest[:12]}… raw_sha256={self.raw_sha256[:12]}…"
            )
        verifications = tuple(
            _check_entry_span(
                entry,
                feed_text=self.feed_text,
                text_length=len(self.feed_text),
                previous_end=(
                    self.entries[position - 1].char_end if position else 0
                ),
            )
            for position, entry in enumerate(self.entries)
        )
        failures.extend(
            item.reason for item in verifications if not item.ok and item.reason
        )
        return SpanReport(
            raw_id=self.raw_id,
            raw_sha256=self.raw_sha256,
            text_length=len(self.feed_text),
            checked=len(verifications),
            entries=verifications,
            failures=tuple(failures),
        )

    def truth_fields(self) -> tuple[Tuple[str, str, int, int], ...]:
        """全部条目的真值四元组（SPEC §2.2）—— 供锚点/标签层直接消费。"""
        return tuple(entry.truth_fields for entry in self.entries)

    def as_anchors(self) -> tuple[EvidenceAnchor, ...]:
        """全部条目的**合法**真值锚点（活对照：ID 那条路 `Entry.as_anchor()` 永远抛）。"""
        return tuple(entry.anchor() for entry in self.entries)


# --------------------------------------------------------------------------- #
# 回环校验（可独立重算；判据 3）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SpanVerification:
    """单个条目的区间回环校验结果。"""

    index: int
    entry_id: str
    char_start: int
    char_end: int
    ok: bool
    reason: str
    title_represented: bool
    slice_non_empty: bool

    def __str__(self) -> str:
        mark = "OK " if self.ok else "FAIL"
        return (
            f"[{mark}] entry[{self.index}] {self.entry_id} "
            f"[{self.char_start}, {self.char_end}) {self.reason}"
        )


@dataclass(frozen=True, slots=True)
class SpanReport:
    """`verify_offsets` / `verify_spans` 的完整报告。"""

    raw_id: str
    raw_sha256: str
    text_length: int
    checked: int
    entries: tuple[SpanVerification, ...]
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def passed(self) -> int:
        return sum(1 for item in self.entries if item.ok)

    @property
    def failed(self) -> int:
        return self.checked - self.passed


def _normalize_ws(value: str) -> str:
    return " ".join(value.split())


#: 标题里的这五个字符在原文里可能以实体形式出现（RSS / Atom 都允许）。
#: 只反解这五个：真正的"原文切片 ↔ 标题"比对必须两边都试，见 `_title_represented`。
_ESCAPED_FORMS = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("'", "&apos;"),
)


def _title_candidates(title: str) -> tuple[str, ...]:
    """标题在原文切片里**可能**出现的形态（全部经空白归一）。

    为什么需要多个候选：真实 feed 的标题在原文里可能是

    - 字面文本（`What I’ve Learned`）—— 大多数；
    - **数字字符引用**（kdnuggets 实测：`What I&#8217;ve Learned`）；
    - 命名实体（`&amp;`）。

    XML 解析器把它们都还原成同一个字符串，因此"切片是否代表这个标题"这条断言
    必须两边都试，否则会**假失败**（kdnuggets 实测 8/10 → 10/10 的差别就在这）。
    这不是放宽判据：字面命中仍然要求逐字符包含；只有确实存在实体形态时，
    才允许用实体的字面量做比对 —— 与 SPEC §2.2 的实体澄清同一口径。
    """
    normalized = _normalize_ws(title)
    if not normalized:
        return ()
    candidates = {normalized}
    escaped = normalized
    for plain, entity in _ESCAPED_FORMS:
        escaped = escaped.replace(plain, entity)
    candidates.add(escaped)
    return tuple(sorted(candidates))


def _unescape_entities(value: str) -> str:
    """把 HTML/XML 实体**与数字字符引用**都还原（`html.unescape` 覆盖两者）。

    注意：这里的产物**只用于字符串包含判定**，绝不用于坐标运算 ——
    解实体会改变字符数，一旦拿它算偏移就会与原文错位（SPEC §2.2 的实体澄清）。
    """
    return _html.unescape(value)


def _title_represented(entry: Entry, feed_text: str) -> bool:
    """原文切片（去标记）是否真的代表这个条目的标题。"""
    slice_text = entry.raw_slice(feed_text)
    for form in (_TAG_RE.sub("", slice_text), sanitize_for_quote(slice_text)):
        collapsed = _normalize_ws(form)
        if any(candidate in collapsed for candidate in _title_candidates(entry.title)):
            return True
    return False


def _check_entry_span(
    entry: Entry,
    *,
    feed_text: str,
    text_length: int,
    previous_end: int,
) -> SpanVerification:
    """一个条目的区间自检：不越界、非空、单调、切片确实是那个条目。"""
    reasons: list[str] = []
    title_represented = False
    slice_non_empty = False

    if not 0 <= entry.char_start < entry.char_end <= text_length:
        reasons.append(
            f"区间越界：硬性要求在 [0, {text_length}] 内，实际 "
            f"[{entry.char_start}, {entry.char_end})"
        )
    if entry.char_start < previous_end:
        reasons.append(f"区间与上一个条目重叠或逆序：起于 {entry.char_start} < {previous_end}")

    slice_text = entry.raw_slice(feed_text)
    slice_non_empty = bool(slice_text.strip())
    if not slice_non_empty:
        reasons.append("原文切片为空（只含空白）")
    else:
        if not slice_text.lstrip().startswith("<"):
            reasons.append(f"原文切片不以元素起始标签开头：{slice_text[:60]!r}")
        # 判据表述与 SPEC §2.2 的实体澄清同一形状：切片**经实体解码后**必须包含
        # 条目标题（真实数据里 kdnuggets 的标题在原文里写成 `What I&#8217;ve …`，
        # 逐字符字面比对在实体场景下不可能成立）。
        title_represented = _title_represented(entry, feed_text)
        if not title_represented:
            reasons.append(
                "原文切片（去标记后）不包含条目标题："
                f"title={entry.title[:60]!r}，slice={_normalize_ws(slice_text)[:120]!r}"
            )
    return SpanVerification(
        index=entry.index,
        entry_id=entry.entry_id,
        char_start=entry.char_start,
        char_end=entry.char_end,
        ok=not reasons,
        reason="；".join(reasons),
        title_represented=title_represented,
        slice_non_empty=slice_non_empty,
    )


def verify_offsets(
    *,
    raw_id: str,
    raw_bytes: bytes,
    entries: "EntrySet",
    content_type: str = "",
) -> SpanReport:
    """**独立重算**条目区间：从原始字节重新解码，再逐条比对切片。

    这是判据 3 的入口，刻意只接受 `raw_bytes` —— 不复用 `entries.feed_text`，
    因此它能真的失败（改一个偏移、改一个字节、换一种解码都会被抓到）。

    校验项（逐条）：

    1. `sha256(raw_bytes) == entries.raw_sha256`（这份产物确实属于这份原文）；
    2. `decode_bytes(raw_bytes, content_type)[0] == entries.feed_text`
       （区间所在的文本确实是这份原文解码出来的，且编码一致）；
    3. `[char_start, char_end)` 在文本内、非空、按文档序**严格不重叠**；
    4. 切片以元素起始标签开头、**去标记后包含该条目的标题**（回环到内容）。

    失败不是异常，而是报告里的 `failures` —— 调用方必须自己判定；`ok` 为假即判据失败。
    """
    digest = content_sha256(raw_bytes)
    failures: list[str] = []
    if digest != entries.raw_sha256:
        failures.append(
            f"raw_bytes 指纹 {digest[:12]}… 与产物声明的 raw_sha256 "
            f"{entries.raw_sha256[:12]}… 不符：这份产物不属于这份原文"
        )
    if entries.raw_id != raw_id:
        failures.append(
            f"raw_id 不匹配：入参 {raw_id!r}，产物 {entries.raw_id!r}"
        )

    text, encoding, _kind = decode_feed(raw_bytes, content_type)
    if text != entries.feed_text:
        failures.append(
            "独立解码得到的 feed 文本与产物自带的 feed_text 不一致："
            f"len(独立)={len(text)} len(产物)={len(entries.feed_text)}"
        )
    if encoding != entries.encoding:
        failures.append(
            f"解码编码不一致：独立 {encoding!r}，产物 {entries.encoding!r}"
        )

    checked_text = entries.feed_text
    verifications: list[SpanVerification] = []
    previous_end = 0
    for entry in entries.entries:
        item = _check_entry_span(
            entry,
            feed_text=checked_text,
            text_length=len(checked_text),
            previous_end=previous_end,
        )
        verifications.append(item)
        previous_end = entry.char_end
    failures.extend(item.reason for item in verifications if not item.ok and item.reason)

    return SpanReport(
        raw_id=raw_id,
        raw_sha256=digest,
        text_length=len(checked_text),
        checked=len(verifications),
        entries=tuple(verifications),
        failures=tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class IdReport:
    """`verify_ids` 的报告：独立重算全部 `entry_id`。"""

    checked: int
    mismatches: tuple[str, ...]
    expected_ids: tuple[str, ...]
    actual_ids: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def verify_ids(
    *,
    raw_id: str,
    raw_bytes: bytes,
    entries: "EntrySet",
) -> IdReport:
    """**独立重算**全部条目 ID（不看产物里存的 ID，只按公式重算）。

    `raw_sha256` 由入参字节当场算出并参与 ID，因此"换了原文但 ID 没变"也会被抓到。
    """
    digest = content_sha256(raw_bytes)
    expected: list[str] = []
    mismatches: list[str] = []
    for entry in entries.entries:
        computed = entry_id_for(
            raw_id=raw_id,
            raw_sha256=digest,
            parser_version=entry.parser_version,
            char_start=entry.char_start,
            char_end=entry.char_end,
            title=entry.title,
        )
        expected.append(computed)
        if computed != entry.entry_id:
            mismatches.append(
                f"entry[{entry.index}]：重算 {computed} != 记录 {entry.entry_id}"
            )
    actual = tuple(entry.entry_id for entry in entries.entries)
    return IdReport(
        checked=len(entries.entries),
        mismatches=tuple(mismatches),
        expected_ids=tuple(expected),
        actual_ids=actual,
    )


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EntryParser:
    """条目解析器：**版本化**、无状态、纯函数。

    | 字段 | 默认 | 含义 |
    |---|---|---|
    | `version` | `entry-parser-v1` | **解析器版本**；参与 `entry_id` 计算，也记进产物 |

    **为什么解析器要版本化、还要记进产物**：SPEC §3 要求任务是
    `f(输入快照, 配置快照) → 输出` 且**可重算**。条目化的"配置快照"就是这个版本号：
    只要它被完整记录下来，任何人拿着同一份 raw 就能重建出**逐字节相同**的条目集合。
    因此它不是调用现场的临时参数，而是**产物的一部分**（`EntrySet.parser_version`）。

    **版本变化必须改变条目 ID**：否则"换了解析器但 ID 没变"会让派生量看起来稳定、
    实际已经漂移（这正是 SPEC §5 #7 对分块 ID 的同一担忧）。
    本包把三方（`EntrySet` / `Entry` / `entry_id`）都绑在同一个版本串上。
    """

    version: str = CURRENT_PARSER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise EntryParserError(
                "parser.version 不得为空：ID 必须携带解析器版本，否则换解析器会撞 ID"
            )

    @property
    def fingerprint(self) -> str:
        """解析器快照摘要（重建时用来断言"两次用的解析器完全一致"）。"""
        return fingerprint(self.snapshot_fields())

    def snapshot_fields(self) -> tuple[str, ...]:
        """解析器的**有序**字段（只有一个版本串，但保留与 `ChunkPolicy` 同形的接口）。"""
        return (self.version,)

    def parse(
        self,
        raw_bytes: bytes,
        content_type: str = "",
        *,
        raw_id: str,
    ) -> EntrySet:
        """`raw 字节 → 条目序列`（纯函数：无 I/O、无时钟、无随机、无全局可变状态）。

        Raises:
            EntryParseError: 输入不是可解析的 feed（非 XML / 结构损坏）。
            EntryError: `raw_id` 为空等契约违例。
        """
        if not isinstance(raw_id, str) or not raw_id:
            raise EntryError("raw_id 不得为空：条目必须能溯源到不可变原文")

        digest = content_sha256(raw_bytes)
        text, encoding, kind = decode_feed(raw_bytes, content_type)
        feed = parse_feed_text(text, kind=kind)

        problems: list[str] = list(feed.problems)
        built: list[Entry] = []

        for raw_entry in feed.entries:
            if not raw_entry.title.strip():
                # 无标题条目**不进入条目序列**，但绝不被静默丢弃：理由进 `problems`。
                problems.append(
                    f"entry[{raw_entry.index}]（区间 "
                    f"[{raw_entry.char_start}, {raw_entry.char_end})）被丢弃：无标题；"
                    "条目身份与展示都依赖标题，凭空造一个标题等于编造结果"
                    + (
                        "；细分原因：" + "；".join(str(p) for p in raw_entry.problems)
                        if raw_entry.problems
                        else ""
                    )
                )
                continue
            built.append(
                Entry(
                    entry_id=entry_id_for(
                        raw_id=raw_id,
                        raw_sha256=digest,
                        parser_version=self.version,
                        char_start=raw_entry.char_start,
                        char_end=raw_entry.char_end,
                        title=raw_entry.title,
                    ),
                    raw_id=raw_id,
                    raw_sha256=digest,
                    index=raw_entry.index,
                    parser_version=self.version,
                    kind=raw_entry.kind,
                    char_start=raw_entry.char_start,
                    char_end=raw_entry.char_end,
                    title=raw_entry.title,
                    link=raw_entry.link,
                    published_at=raw_entry.published_at,
                    entry_text=raw_entry.text,
                    problems=raw_entry.problems,
                )
            )

        if len(built) == 0 and feed.entries and not problems:
            # 防御性：feed 有条目却一条都没产出，必须有理由。正常路径不会到这里。
            problems.append("feed 里的条目全部无法产出，但没有记录任何理由（内部不变量被破坏）")

        return EntrySet(
            entries=tuple(built),
            raw_id=raw_id,
            raw_sha256=digest,
            kind=kind,
            parser_version=self.version,
            encoding=encoding,
            feed_text=text,
            problems=tuple(problems),
        )


#: 默认解析器（代码里的默认值就是文档里的默认值；SPEC §2.12 同一条纪律）。
DEFAULT_PARSER = EntryParser()


def parse_entries(
    raw_bytes: bytes,
    content_type: str = "",
    *,
    raw_id: str,
    parser: Optional[EntryParser] = None,
) -> EntrySet:
    """便捷入口：`raw 字节 → 条目序列`（等价于 `EntryParser().parse(...)`）。

    刻意不提供"只给条目、不给 feed 文本"的入口：条目区间只有落在
    `EntrySet.feed_text`（或独立解码出的同一文本）上才有意义，
    就像 T-206 不提供"只给分块"的入口一样。
    """
    return (parser or DEFAULT_PARSER).parse(raw_bytes, content_type, raw_id=raw_id)
