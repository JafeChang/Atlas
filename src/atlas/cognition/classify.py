"""T-105 分类单元分流：异质 raw → "可分类单元"（SPEC §6.3 裁决 B / §4.2 T-105）。

**为什么必须先分流**（这不是设想，是 SPEC §6.3 已测实的现实）
==========================================================

`data/store` 里的 raw 是**异质**的。主代理在真实 store 上实测（本轮复核一致）：

| raw 种类 | 条数 | 分流结果 |
|---|---|---|
| 是 feed（4× arXiv RSS + google-ai-blog Atom + synced-review / marktechpost / kdnuggets RSS） | **8** | 走 `atlas.entries.parse_entries` → **830 个条目**；**条目是分类单元** |
| 已经是逐篇文章（`endpoint` = 文章 URL，正文已经是纯文本） | **65** | 它**本身就是一个分类单元**；**不得**喂给 feed 解析器 |
| 既不是 feed 也没有可分类内容（HTML 错误页 / JSON API 响应） | **2** | **明确跳过并给理由**，不假装成功 |

`atlas.entries.parse_entries` 对非 feed 输入抛 `EntryParseError` —— 那是**正确行为**
（T-130 的错误模块写得清清楚楚）。本模块**绝不**把它 catch 掉当作"0 个条目"：
"不是 feed"与"这个 feed 是空的"是两件不同的事，静默合并正是 SPEC §7.3 失败模式 3。

**怎么判种类：看内容，不看 `endpoint`**
=====================================

SPEC §2.16 明确警告：`raw_records.endpoint` 有**两种语义**（新采集 = feed 地址，
T-131 导入 = 文章地址），而且文档写着"**不可用于**去重 / 当作 feed 标识"。
因此本模块的判据是：

1. **先试条目化**（判据是根元素，与 `atlas.entries` 同一口径）：
   `parse_entries` 成功 → 该 raw **是 feed**，单元 = 条目；
2. `EntryParseError` → 该 raw **不是 feed**，再看它像不像一篇**可分类的纯文本文档**：
   - 开头是 `{` / `[` → JSON（API 响应）→ **跳过**，理由 `unsupported_content_json`；
   - 开头是 `<!doctype html` / `<html` → HTML（错误页 / 列表页）→ **跳过**，
     理由 `unsupported_content_html`；
     这里刻意**不**做 HTML 正文抽取：那需要 HTML 解析器（§2.6 说的"新增协议类型才写插件"），
     而且当前真实数据里这两条本来就是**没有可分类内容**的错误页 —— 给它们编一篇正文
     等于编造结果（硬规则 2）；
   - 其它（纯文本 / 非良构 XML 的纯文本）→ **整篇文档就是一个分类单元**；
3. feed 解析成功但条目数为 0 → **跳过**，理由 `empty_feed`（空 feed 是事实，不是错误，
   但也没有可分类的东西）。

> 判据 5 的活对照由测试钉死：同一个 raw 走"成功解析出条目"与"走不出条目"两条路时，
> 分流结果必须不同 —— 否则"分流器"只是个恒等函数。

**单元身份是派生的，字符区间才是真值**（与 T-130 / T-206 同一纪律）
==============================================================

| 量 | 性质 | 说明 |
|---|---|---|
| `unit_id` | **派生** | feed 条目 → T-130 的 `entry_id`（`ent_…`）；文章 → `art_…` |
| `char_start` / `char_end` | **真值** | 在**解码后原文文本**上的字符区间（SPEC §2.2 的形状） |

本模块**不做任何坐标运算**，也**不把坐标喂给模型**：区间只随单元一路带下去，
成为 T-105 写进 `proposed_claims` 的溯源列（T-107 的输入）。模型侧永远只看到文字。

**单元文本怎么给模型**（确定性、可复算、不需要额外解析器）
======================================================

单元的 `text` 就是会被喂进 prompt 的那段文字，规则是**纯函数**：

```
text = reduce(title) + "\\n\\n" + reduce(entry_text)
```

`reduce` 只做两件确定性的事：**解实体 → 按空白切词再单空格拼回**
（`atlas.entries.sanitize_for_quote` 的同一口径，只是不做标签删除，因为条目正文里
可能混着 CDATA 展开后的 HTML 标记 —— 那一层由 `sanitize_for_quote` 处理，
本模块**复用**它而不是另写一份）。

为什么这很重要：模型给出的 `quote` 必须能在**同一段单元文本**里逐字找到
（SPEC §2.2），否则它就不是证据。因此"模型看到的文字"与"折回单元的判据所用文字"
必须是**同一份**，而 `unit_digest` 就是这份文字的指纹 —— 换 reduce 规则会让
`unit_digest` 变化，从而幂等键变化，**不会**出现"文本变了但幂等键没变"。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from atlas.entries import (
    EntryParseError,
    EntrySet,
    FeedKind,
    parse_entries,
    sanitize_for_quote,
)

__all__ = [
    "ARTICLE_ID_PREFIX",
    "ARTICLE_ID_RE",
    "BATCH_MAX_CHARS",
    "BATCH_MAX_UNITS",
    "UNIT_TEXT_MAX_CHARS",
    "ClassifiedUnit",
    "ClassificationError",
    "DocumentKind",
    "DocumentPlan",
    "LabelSpace",
    "SkipReason",
    "Unit",
    "UnitKind",
    "batch_key_for",
    "classify_document",
    "plan_batches",
    "reduce_text",
    "unit_id_for_article",
]

#: 文章级单元的 ID 前缀（与 T-130 的 `ent_` 分属两个身份空间，绝不混用）。
ARTICLE_ID_PREFIX = "art_"
ARTICLE_ID_RE = re.compile(r"^art_[0-9a-f]{32}$")

#: 一次批量调用最多塞几个单元 / 多少字符（**实测标定**，见 `tools/t105_real_evidence.py`）。
#:
#: 两条上限来自两个不同的实测约束：
#:
#: 1. **固定开销按调用次数计**：边车进程启动在 drvfs 上约 4.5–6.5 s（SPEC §2.14），
#:    与单元数无关。因此把单元塞进同一次调用能摊薄这笔钱（实测：`deepseek-flash`
#:    一次调用的墙钟约 6–16 s，其中相当一部分是启动）。
#: 2. **输出预算被推理 token 吃掉**：`deepseek-flash` 是推理型，实测一次调用
#:    的 reasoning token 常达 1000–2000，而单次调用的输出预算是有限的。
#:    ⚠️ **本注释原先写错了，2026-09-26 由主代理更正并实测**：原文称
#:    "T-003 的 `max_output_tokens` 字段**没有**被 adapter 送进边车"——**这是假的**。
#:    真实链路（端到端，已实测）：`adapter._build_job()` 放进 `call.maxOutputTokens`
#:    → 边车 `run.mjs` 以 `{...base, maxTokens: call.maxOutputTokens}` 覆盖默认 4096
#:    → OpenAI SDK 以 **`max_completion_tokens`** 发到 HTTP 请求体。
#:    实测证据：配置 1234 → mock 收到的请求体 `max_completion_tokens: 1234`（另试 777/2048 亦跟随）。
#:    **所以生效的预算是配置值**（`CognitionConfig.max_output_tokens` 默认 2048），不是 4096。
#:    误判成因：只读了 `run.mjs` 里 `?? 4096` 那一行默认值，没有往下读覆盖它的那一行。
#:    一批塞得越多，模型越容易把预算全花在推理上、
#:    最终返回空或被截断（实测：一批 5–10 个长单元时 `empty_completion` 明显增多）。
#:
#: 两条合起来给出的结论是"**中等批量 + 有界重试**"，而不是"越大越好"：
#: 上限取 4，超限就由 `ProposalPolicy` 的重试（批次减半直到 1）兜住。
#: 这两个数字**必须与实测一致**；改它们要重跑 `tools/t105_real_evidence.py`。
BATCH_MAX_UNITS = 4
BATCH_MAX_CHARS = 6000
#: 单个单元文本的截断上限（超长条目按字符数截断，**截断位置参与 `unit_digest`**）。
UNIT_TEXT_MAX_CHARS = 2000

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_TITLE_MAX_CHARS = 300


class ClassificationError(Exception):
    """T-105 分流层的契约违例。

    注意：`EntryParseError` **不**被本模块吞掉 —— 它在这里是"这个 raw 不是 feed"的
    **正常信号**，本模块据此分流，并把原始错误文本原样留在 `DocumentPlan.detail` 里。
    """


class UnitKind(str, Enum):
    """单元的来源形态。**只影响溯源，不影响分类语义。**"""

    #: feed 里的一个条目（真值 = 条目在解码后 feed 文本上的字符区间）
    ENTRY = "entry"
    #: 一整篇已经是逐篇归档的文档（真值 = 整篇文本的区间 `[0, len(text))`）
    ARTICLE = "article"


class DocumentKind(str, Enum):
    """一个 raw **分流后**的结论（三选一，没有第四种）。"""

    #: 是 feed，拆出了 ≥1 个条目 → 单元 = 条目
    FEED = "feed"
    #: 不是 feed，但本身是一篇可分类的纯文本文档 → 单元 = 整篇
    ARTICLE = "article"
    #: 既不是 feed 也没有可分类内容 → 0 个单元 + **非空理由**
    SKIPPED = "skipped"


class SkipReason(str, Enum):
    """`DocumentKind.SKIPPED` 的**闭集**理由码。

    刻意做成枚举而不是自由文本：测试断言"每一条被跳过的 raw 都落在这个闭集里"，
    这样"跳过"永远是有据可查的分类，而不是一个没人看得懂的字符串。
    """

    #: 内容是 JSON（通常是 JSON API 响应），没有可分类的正文
    UNSUPPORTED_CONTENT_JSON = "unsupported_content_json"
    #: 内容是 HTML（错误页 / 列表页），本层不做 HTML 正文抽取
    UNSUPPORTED_CONTENT_HTML = "unsupported_content_html"
    #: 内容是 XML，但不是 feed（根元素不认识）→ T-130 返回 0 条
    UNSUPPORTED_CONTENT_XML = "unsupported_content_xml"
    #: 是合法 feed，但里面一条条目都没有
    EMPTY_FEED = "empty_feed"
    #: 文本解出来是空的（零长度 / 全空白）
    EMPTY_CONTENT = "empty_content"


# --------------------------------------------------------------------------- #
# 单元
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Unit:
    """一个**可分类单元**：派生 ID + **真值字符区间** + 会被喂给模型的文本。

    | 字段 | 性质 | 说明 |
    |---|---|---|
    | `unit_id` | **派生** | `ent_…`（feed 条目）或 `art_…`（整篇文档） |
    | `raw_id` | 溯源 | 单元所属的不可变 Raw |
    | `raw_sha256` | **真值** | 该 Raw **解码后文本**的 sha256（与 `EntrySet.raw_sha256` 同一口径） |
    | `char_start` / `char_end` | **真值** | 在**解码后原文文本**上的字符区间（SPEC §2.2 的形状） |
    | `kind` | 形态 | `entry` / `article` |
    | `title` | 字段 | 条目标题 / 文章首行（**可能为空串**，不编造） |
    | `text` | 喂模型的文字 | `reduce(title) + "\\n\\n" + reduce(body)`，见 `reduce_text` |
    | `parser_version` | 重建依据 | feed 条目的解析器版本；文章为 `None` |
    | `problems` | 审计 | 该单元的字段缺失理由（来自 T-130，**原样带出**） |

    `unit_digest` 是 `text` 的指纹，**参与幂等键**：同一单元换了文本（换 reduce 规则 /
    条目正文变了）必然换幂等键，因此"同输入 + 同配置 → 同输出"不会因为静默漂移而失效。
    """

    unit_id: str
    raw_id: str
    raw_sha256: str
    char_start: int
    char_end: int
    kind: str
    title: str
    text: str
    parser_version: Optional[str] = None
    entry_index: Optional[int] = None
    problems: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ClassificationError("unit_id 不得为空")
        if not self.raw_id:
            raise ClassificationError("raw_id 不得为空：单元必须能溯源到不可变原文")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise ClassificationError("raw_sha256 必须是 64 位小写十六进制")
        if self.kind not in (UnitKind.ENTRY.value, UnitKind.ARTICLE.value):
            raise ClassificationError(f"未知单元形态：{self.kind!r}")
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ClassificationError(
                f"非法单元区间：[{self.char_start}, {self.char_end})"
            )
        if not self.text.strip():
            raise ClassificationError(
                "单元文本不得为空：没有文字就没有可抽取的证据（应当被分流为 skipped）"
            )

    @property
    def unit_digest(self) -> str:
        """喂给模型的文本的 sha256（参与幂等键；换文本必然换键）。"""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def is_entry(self) -> bool:
        return self.kind == UnitKind.ENTRY.value

    def truth_fields(self) -> Tuple[str, str, int, int]:
        """SPEC §2.2 的真值四元组（**不含**任何派生量）。"""
        return (self.raw_id, self.raw_sha256, self.char_start, self.char_end)


# --------------------------------------------------------------------------- #
# 文本归约（纯函数）
# --------------------------------------------------------------------------- #


def reduce_text(value: str, *, limit: int = UNIT_TEXT_MAX_CHARS) -> Tuple[str, bool]:
    """把一段文字变成**可稳定引用**的单元文本。返回 `(文本, 是否被截断)`。

    三步，全部确定性（同一输入永远同一输出）：

    1. `sanitize_for_quote`（复用 T-130 的实现，不另写一份）：脱 CDATA 外壳 →
       解实体 → 去标签；
    2. 空白折叠：`\\s+` → 单个空格；
    3. 按 `limit` 截断（**是否截断由返回值显式给出**，不静默）。

    为什么"空白折叠"是必须的：条目正文里混着 CDATA 展开后的 HTML 标记与缩进。
    模型给出 quote 时不可能复现原始空白的每个细节，而 SPEC §2.2 的确定性匹配
    （`match_quote` 的退化路径）本来就把"空白折叠后相同"当作命中。把折叠**前置**
    到这里，是为了让"模型看到的文字"与"折回单元判据使用的文字"是同一份 ——
    否则 quote 的命中率会依赖不可控的空白差异。
    """
    if not isinstance(value, str):
        raise ClassificationError(f"reduce_text 只接受 str，收到 {type(value).__name__}")
    plain = sanitize_for_quote(value) if ("<" in value or "&" in value) else value
    collapsed = _WS_RE.sub(" ", plain).strip()
    if limit > 0 and len(collapsed) > limit:
        return collapsed[:limit].strip(), True
    return collapsed, False


def unit_id_for_article(raw_id: str, raw_sha256: str, text: str) -> str:
    """整篇文档单元的派生 ID：`art_` + sha256(raw_id, raw_sha256, text)[:32]。

    与 T-002 的 `clm_` / `lbl_` / T-130 的 `ent_` **同构**（带字段分隔符的稳定摘要），
    因此：同 raw + 同文本 → 同 ID（可重算）；文本变了（换 reduce 规则）→ 新 ID
    （**不会**出现"文字换了指纹没变"的静默漂移）。
    """
    if not raw_id or not raw_sha256:
        raise ClassificationError("raw_id / raw_sha256 不得为空")
    digest = hashlib.sha256()
    for part in (raw_id, raw_sha256, text):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return ARTICLE_ID_PREFIX + digest.hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 一次分流的产物
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DocumentPlan:
    """一个 raw 的分流结论：`units` + `kind` + 跳过理由（若跳过）。

    不变量（构造期强制）：

    - `kind == FEED` / `ARTICLE` ⇒ `units` 非空、`skip_reason is None`；
    - `kind == SKIPPED` ⇒ `units == ()` **且** `skip_reason is not None`
      **且** `detail` 非空。

    第二组是本任务对"不得假装成功"的代码层落地：**跳过一个 raw 必须带理由**，
    没有"静默 0 个单元"这条路径（与 `CognitionResult.is_unclassified` 必须带
    `reason` 是同一形状）。
    """

    raw_id: str
    raw_sha256: str
    kind: DocumentKind
    units: Tuple[Unit, ...]
    skip_reason: Optional[SkipReason] = None
    detail: str = ""
    channel_id: str = ""
    endpoint: str = ""

    def __post_init__(self) -> None:
        if not self.raw_id:
            raise ClassificationError("raw_id 不得为空")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256):
            raise ClassificationError("raw_sha256 必须是 64 位小写十六进制")
        if self.kind is DocumentKind.SKIPPED:
            if self.units:
                raise ClassificationError("skipped 的 raw 不得携带任何单元")
            if self.skip_reason is None:
                raise ClassificationError("skipped 必须给出理由码：不得静默跳过")
            if not self.detail.strip():
                raise ClassificationError("skipped 必须给出可读理由（detail）")
        else:
            if not self.units:
                raise ClassificationError(
                    f"{self.kind.value} 必须至少产出一个分类单元（否则应当判为 skipped）"
                )
            if self.skip_reason is not None:
                raise ClassificationError("未跳过的 raw 不得携带跳过理由码")
        previous = -1
        for unit in self.units:
            if unit.raw_id != self.raw_id:
                raise ClassificationError(f"单元 {unit.unit_id} 的 raw_id 与计划不符")
            if unit.raw_sha256 != self.raw_sha256:
                raise ClassificationError(f"单元 {unit.unit_id} 的 raw_sha256 与计划不符")
            if unit.char_start < previous:
                raise ClassificationError(f"单元 {unit.unit_id} 的区间逆序或重叠")
            previous = unit.char_end

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def skipped(self) -> bool:
        return self.kind is DocumentKind.SKIPPED

    @property
    def total_chars(self) -> int:
        return sum(len(unit.text) for unit in self.units)


# --------------------------------------------------------------------------- #
# 分流主体
# --------------------------------------------------------------------------- #

_JSON_HEAD = ("{", "[")
_HTML_HEAD = ("<!doctype html", "<html", "<html", "<head")


def _non_xml_shape(text: str) -> Optional[SkipReason]:
    """决定"不是 feed"的文本该**跳过**还是**当整篇文档分类**。

    判据刻意只看开头一段（不猜全文结构）：
    开头是 JSON / HTML 的一律跳过 —— 本层不做 HTML 正文抽取（见模块 docstring），
    而一个 JSON API 响应也没有"正文"可分类。
    """
    head = text.lstrip()[:256]
    lowered = head.lower()
    if head.startswith(_JSON_HEAD):
        return SkipReason.UNSUPPORTED_CONTENT_JSON
    if lowered.startswith(_HTML_HEAD) or "<!doctype html" in lowered:
        return SkipReason.UNSUPPORTED_CONTENT_HTML
    return None


def _title_of_article(text: str) -> str:
    """文章单元的标题 = **正文第一行**（不猜、不解析 HTML）。

    真实数据里 T-131 导入的 65 篇 `content.bin` 已经是纯文本，第一行就是标题
    （实测：`'Winter Release Connects Mechanical and Electrical Design Data to PLM; …'`）。
    取不到就给空串 —— 标题只是提示，**绝不编造**。
    """
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:_TITLE_MAX_CHARS]
    return ""


def _assemble_unit_text(title: str, body: str, link: str = "") -> str:
    """单元文本 = 标题 + 正文 +（可选）原文链接。**纯函数、确定性**。

    链接为什么在**文本里**（而不是一个"模型看不到的提示字段"）：
    真实 feed 条目的 `link` 常常是判断领域的强信号，而 §2.2 要求模型给出逐字引用。
    把链接放进模型看得到的同一段文字，模型就能引用它 —— 引用落在原始 URL 区间上，
    是**真的证据**。反过来，如果链接只存在元数据里，模型引用它就会匹配失败，
    那才是"引文形态没对齐"造成的人为失败。
    """
    parts = [part for part in (title.strip(), body.strip(), link.strip()) if part]
    return "\n\n".join(parts)


def _entry_units(entries: "EntrySet") -> Tuple[Unit, ...]:
    """feed 条目 → 单元（复用 T-130 的 `entry_id` 与字符区间，**不重算**）。"""
    units: List[Unit] = []
    for entry in entries.entries:
        title, _title_truncated = reduce_text(entry.title, limit=_TITLE_MAX_CHARS)
        body, _body_truncated = reduce_text(entry.entry_text, limit=UNIT_TEXT_MAX_CHARS)
        text = _assemble_unit_text(title, body, entry.link)
        if not text:
            # 空正文的条目**不进入单元序列**，但理由跟着计划走（`problems`），
            # 绝不当成"这个 feed 就是 0 个单元"。
            continue
        units.append(
            Unit(
                unit_id=entry.entry_id,
                raw_id=entry.raw_id,
                raw_sha256=entry.raw_sha256,
                char_start=entry.char_start,
                char_end=entry.char_end,
                kind=UnitKind.ENTRY.value,
                title=title,
                text=text,
                parser_version=entry.parser_version,
                entry_index=entry.index,
                problems=tuple(str(problem) for problem in entry.problems),
            )
        )
    return tuple(units)


def classify_document(
    raw_bytes: bytes,
    *,
    raw_id: str,
    content_type: str = "",
    channel_id: str = "",
    endpoint: str = "",
    parser: object = None,
) -> DocumentPlan:
    """一个 raw → 分流结论（**纯函数**：无 I/O、无时钟、无随机）。

    流程见模块 docstring 的"怎么判种类"。三条必须记住的：

    1. `EntryParseError` **原样作为"不是 feed"的证据**，错误文本进 `DocumentPlan.detail`；
    2. 不是 feed 的 **纯文本** 整篇成为一个单元；**HTML / JSON 跳过**并给理由码；
    3. 跳过一个 raw **必须**带 `SkipReason` 与 `detail` —— 构造期强制，没有静默路径。
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise ClassificationError(
            f"raw_bytes 必须是 bytes-like，收到 {type(raw_bytes).__name__}"
        )
    payload = bytes(raw_bytes)
    digest = hashlib.sha256(payload).hexdigest()
    common = {
        "raw_id": raw_id,
        "channel_id": channel_id,
        "endpoint": endpoint,
    }

    feed_error: Optional[EntryParseError] = None
    entries: Optional[EntrySet] = None
    try:
        entries = parse_entries(
            payload,
            content_type,
            raw_id=raw_id,
            parser=parser,  # type: ignore[arg-type]
        )
    except EntryParseError as exc:
        # **正确行为**，不是异常情况：这个 raw 不是 feed。原样记下理由，继续分流。
        feed_error = exc

    if entries is not None:
        if entries.entry_count == 0:
            # 「是 feed 但**空的**」与「是 XML 但不认识」必须分开：
            # 前者是**事实**（源头暂时没有内容），后者是**格式不认识**（可能是接线错误）。
            # 判据用 T-130 已经判定的形态（`FeedKind.RSS` / `ATOM` 才是真 feed），
            # 不看 `problems` 的条数 —— 空 feed 本身就会带一条诊断，那不是"不认识"。
            is_known_feed = entries.kind in (FeedKind.RSS, FeedKind.ATOM)
            detail = (
                f"是合法 feed（形态 {entries.kind}）但一条条目都没有"
                "（空 feed 是事实，不是错误），没有可分类的内容；"
                f"解析器诊断：{'；'.join(entries.problems) or '（无）'}"
            )
            return DocumentPlan(
                raw_sha256=entries.raw_sha256,
                kind=DocumentKind.SKIPPED,
                units=(),
                skip_reason=(
                    SkipReason.EMPTY_FEED
                    if is_known_feed
                    else SkipReason.UNSUPPORTED_CONTENT_XML
                ),
                detail=detail,
                **common,
            )
        units = _entry_units(entries)
        if not units:
            return DocumentPlan(
                raw_sha256=entries.raw_sha256,
                kind=DocumentKind.SKIPPED,
                units=(),
                skip_reason=SkipReason.EMPTY_CONTENT,
                detail=(
                    f"feed 解析出 {entries.entry_count} 个条目，但每个条目的可分类文本"
                    "都为空（正文只有标记 / 空白）—— 没有可分类的内容，不假装成功"
                ),
                **common,
            )
        return DocumentPlan(
            raw_sha256=entries.raw_sha256,
            kind=DocumentKind.FEED,
            units=units,
            **common,
        )

    assert feed_error is not None  # entries is None ⇒ 捕获到了 EntryParseError
    # 不是 feed。看它像不像一篇可分类的纯文本文档。
    from atlas.normalize import decode_bytes  # 仅此一处使用（T-104 的严格解码链）

    text, _encoding = decode_bytes(payload, content_type)
    stripped = text.strip()
    if not stripped:
        return DocumentPlan(
            raw_sha256=digest,
            kind=DocumentKind.SKIPPED,
            units=(),
            skip_reason=SkipReason.EMPTY_CONTENT,
            detail=f"原文解出来是空的（{len(payload)} 字节），没有可分类的内容",
            **common,
        )

    shape = _non_xml_shape(stripped)
    if shape is not None:
        return DocumentPlan(
            raw_sha256=digest,
            kind=DocumentKind.SKIPPED,
            units=(),
            skip_reason=shape,
            detail=(
                f"不是 feed（条目化失败：{feed_error}），且内容是"
                f"「{shape.value}」形态：本层不做 HTML/JSON 正文抽取，"
                "没有可分类的正文 —— 明确跳过，不假装成功"
            ),
            **common,
        )

    title = _title_of_article(stripped)
    body, _truncated = reduce_text(stripped, limit=UNIT_TEXT_MAX_CHARS)
    if not body:
        return DocumentPlan(
            raw_sha256=digest,
            kind=DocumentKind.SKIPPED,
            units=(),
            skip_reason=SkipReason.EMPTY_CONTENT,
            detail="原文经归约后没有任何非空白文字，没有可分类的内容",
            **common,
        )
    # 标题不要**重复**：`body` 是整篇归约的结果，它本来就已经以标题开头
    # （真实数据里 65 篇导入文章的 `content.bin` 就是"第一行标题 + 正文"）。
    # 只有当标题被截断到 `_TITLE_MAX_CHARS` 而正文没有时，标题才需要单独补在前面。
    if title and not body.startswith(title):
        text_for_unit = f"{title}\n\n{body}".strip()
    else:
        text_for_unit = body
    unit = Unit(
        unit_id=unit_id_for_article(raw_id, digest, text_for_unit),
        raw_id=raw_id,
        raw_sha256=digest,
        char_start=0,
        char_end=len(stripped),
        kind=UnitKind.ARTICLE.value,
        title=title,
        text=text_for_unit,
        problems=(),
    )
    return DocumentPlan(
        raw_sha256=digest,
        kind=DocumentKind.ARTICLE,
        units=(unit,),
        detail=(
            f"不是 feed（条目化失败：{feed_error}），是可分类的纯文本文档："
            f"整篇作为一个分类单元（{len(stripped)} 字符）"
        ),
        **common,
    )


# --------------------------------------------------------------------------- #
# 批量计划（调用策略的落地形态）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ClassifiedUnit:
    """批量计划里的一个成员：单元 + **它在批次提示里的序号**（`index`，0 起）。

    `index` 是"模型看到的第几个单元"。它**不要求**模型输出 —— 归属靠 quote 的
    确定性匹配（SPEC §2.2 的分工），`index` 只用于构造提示与人类可读的报告。
    """

    index: int
    unit: Unit

    @property
    def unit_id(self) -> str:
        return self.unit.unit_id


def batch_key_for(raw_id: str, unit_ids: Sequence[str]) -> str:
    """批次的幂等键的一部分：`(raw_id, 有序 unit_id 列表)` 的稳定摘要。

    **有序**是刻意的：批次身份取决于"模型在同一次调用里看到哪些单元、按什么顺序"，
    换顺序就是换输入，必须换键（否则"同输入 → 同输出"这句话就不成立）。
    """
    if not raw_id:
        raise ClassificationError("raw_id 不得为空")
    if not unit_ids:
        raise ClassificationError("批次必须至少包含一个单元")
    digest = hashlib.sha256()
    digest.update(raw_id.encode("utf-8"))
    digest.update(b"\x1f")
    for unit_id in unit_ids:
        digest.update(unit_id.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


def plan_batches(
    units: Sequence[Unit],
    *,
    max_units: int = BATCH_MAX_UNITS,
    max_chars: int = BATCH_MAX_CHARS,
) -> Tuple[Tuple[Unit, ...], ...]:
    """把同属一个 raw 的单元切成交付给**一次调用**的批次（纯函数、确定性）。

    两条上限，**先到者生效**，并且**至少放一个单元**（单个超长单元独占一批，
    否则会死循环，与 T-206 `overlap >= target` 的守卫同一形状）：

    - `max_units`：一笔调用里的单元数上限（归属精度与输出长度的上限）；
    - `max_chars`：一笔调用里的字符数上限（上下文与成本的上限）。

    **输入顺序即输出顺序**：单元按 `(char_start, unit_id)` 排序后切分，因此
    同一份单元集合永远切出同一组批次（可复算），批次键也稳定。
    """
    if max_units <= 0:
        raise ClassificationError(f"max_units 必须为正：{max_units}")
    if max_chars <= 0:
        raise ClassificationError(f"max_chars 必须为正：{max_chars}")

    ordered = sorted(units, key=lambda unit: (unit.char_start, unit.unit_id))
    batches: List[Tuple[Unit, ...]] = []
    current: List[Unit] = []
    current_chars = 0
    for unit in ordered:
        size = len(unit.text)
        too_many = len(current) >= max_units
        too_long = current and (current_chars + size) > max_chars
        if current and (too_many or too_long):
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


# --------------------------------------------------------------------------- #
# 标签空间（C8 闭环的**注入**形状，与 T-106 的 industry_of 同一做法）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LabelSpace:
    """分类的候选标签集合 = **当前启用的行业配置**（SPEC §2.5 / §2.9 闭环）。

    本包**刻意不 import `atlas.registry`**（SPEC §4.0 的跨包规则：registry 不是
    T-105 在 DAG 里的上游）。标签空间因此是**注入参数**，形状照 T-106 的
    `industry_of=`：由**组合根**从注册表读取后传进来。

    ```python
    registry = RegistryService(open_store("data/store/atlas.db"))
    space = LabelSpace.of(registry.label_space(), config_version=registry.config_version)
    ```

    **忘记注入必须响亮可见**：`LabelSpace` 构造期就拒绝空标签集合
    （`labels` 至少一项）。这与 §2.5 警告的"feed 里 industry 会是 None、按行业筛选
    静默返回全空 —— 能跑，但闭环断开"是同一类缝隙，所以这里用**构造期失败**
    把它变成不可能，而不是靠调用方自觉。
    """

    labels: Tuple[str, ...]
    config_version: str
    source: str = "injected"

    def __post_init__(self) -> None:
        if not self.labels:
            raise ClassificationError(
                "标签空间为空：分类的候选标签集合必须来自当前启用的行业配置（SPEC §2.5）。"
                "若组合根忘记注入 label_space，这里**响亮失败**，"
                "绝不静默产出空标签的分类结果。"
            )
        if len(set(self.labels)) != len(self.labels):
            raise ClassificationError(f"标签空间含重复项：{self.labels!r}")
        for label in self.labels:
            if not isinstance(label, str) or not label.strip():
                raise ClassificationError(f"标签必须是非空字符串：{label!r}")
        if not self.config_version:
            raise ClassificationError("config_version 不得为空（SPEC §3 的版本三元组）")

    @classmethod
    def of(
        cls, labels: Sequence[str], *, config_version: str, source: str = "injected"
    ) -> "LabelSpace":
        return cls(
            labels=tuple(sorted(str(label) for label in labels)),
            config_version=config_version,
            source=source,
        )

    @property
    def fingerprint(self) -> str:
        """标签空间的指纹（**有序**，参与幂等键）。"""
        digest = hashlib.sha256()
        for label in self.labels:
            digest.update(label.encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()

    def as_dict(self) -> Dict[str, object]:
        return {
            "labels": list(self.labels),
            "config_version": self.config_version,
            "source": self.source,
            "fingerprint": self.fingerprint,
        }
