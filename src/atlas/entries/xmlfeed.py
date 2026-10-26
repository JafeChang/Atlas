"""feed 文本骨架：解码 → 判形 → 逐条目字段提取（纯函数，无 I/O；T-130）。

本模块只做"从一段**已解码文本**里取出东西"，不做版本/ID/产物组装——
那些在 `atlas.entries.parser` 与 `atlas.entries.ids`。这样拆分的理由：
字段提取规则是最容易随真实数据变化的代码，让它可以被单独测试与替换。

坐标口径（本任务最关键的设计决定，见 `parser` 模块 docstring）
------------------------------------------------------------

本模块只处理 **`str`**。条目区间是在**解码后的 feed 文本**上的字符偏移 ——
与 `atlas.normalize.NormalizedText.raw_text`、`EvidenceAnchor.char_start/char_end`
同一个参照系。**不是**字节偏移。

解码
----

复用 `atlas.normalize.text.decode_bytes`（T-104）：BOM → `Content-Type` charset →
XML 声明 → `utf-8` → `gb18030` 的严格回退链，不用 `errors="replace"` 编造文本。
T-130 属 T-104 的**下游**（SPEC §4.0 允许沿 DAG 读上游类型/函数），因此这不是
新增依赖、也不是新写的解码器。

命名空间：**注入法**，不用全局注册表
----------------------------------

真实 RSS 里 `<dc:creator>` / `<content:encoded>` 的前缀声明在**根元素**上，
而逐条目解析时只拿到 `<item>…</item>` 切片 —— 直接 `ElementTree.fromstring`
会抛 `unbound prefix`（10 份真实 feed 里 8 份如此）。两种修法：

- `xml.etree.ElementTree.register_namespace`：改的是**进程级全局可变状态**，
  与"纯函数、无全局可变状态"直接冲突，且并发下互相踩（T-206 同一纪律）。
- **注入法**（本模块采用）：先扫全文收集 `prefix → uri`，再把切片里缺失的
  `xmlns:` 声明注入到它自己的起始标签上，使每个切片**自包含**。
  全局状态零改动，重复解析同一输入得到同一结果。
"""

from __future__ import annotations

import html as _html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional, Sequence

from .errors import EntryParseError

__all__ = [
    "ATOM_NS",
    "FeedKind",
    "FeedText",
    "FieldProblem",
    "RawEntry",
    "SPAN_PROBLEM_MALFORMED",
    "SPAN_PROBLEM_UNCLOSED",
    "decode_feed",
    "local_name",
    "text_of",
]

#: RSS 2.0 的条目元素名。
RSS_ITEM_TAG = "item"

#: Atom 的条目元素名。
ATOM_ENTRY_TAG = "entry"

#: Atom 1.0 命名空间。
ATOM_NS = "http://www.w3.org/2005/Atom"

#: 合法 XML 但不是已知 feed 根元素的判定依据。
RSS_ROOT_TAGS = frozenset({"rss", "rdf"})
ATOM_ROOT_TAGS = frozenset({"feed"})

#: 条目切片的结构性问题标识（供 `problems` 字符串用）。
SPAN_PROBLEM_UNCLOSED = "unclosed-element"
SPAN_PROBLEM_MALFORMED = "malformed-element"

#: 一个元素起始标签：`<name attr…>`（属性段可能含 `>`，故贪婪到最后一个 `>`）。
#: 属性段写成 `(\s[^<>]*?)?` 而不是 `([^<>]*?)?`：后者会把 `>` 吃进**元素名**里
#: （`<item>` 被解析成 name=`item>`、attrs=``），于是 `match.end()` 落在
#: `</item>` 之后、切片尾部被吞掉。这是本任务实测踩到的坑，用这条注释钉住。
_START_TAG_RE = re.compile(r"<([A-Za-z_][\w.\-]*)(\s[^<>]*?)?(/?)>", re.DOTALL)

#: 任何标签（用于 `text_of` 的朴素标签剥离）。
_ANY_TAG_RE = re.compile(r"<[^>]*>")

#: XML 声明里的编码名。
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)[\"']", re.DOTALL)

#: 文档里以 `<name` 形式出现的标签名（判形用；`<?xml` / `<!--` / `<!DOCTYPE` 不会命中）。
_TAG_NAME_RE = re.compile(r"<\s*([A-Za-z_][\w.\-]*)")

#: 判"这不是 feed"用的快速嗅探标记。
_JSON_MARKERS = ('{"', "[{")
_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body")

#: 日期字段的候选顺序（RSS 2.0 与 Atom 各自的实际字段）。
_DATE_FIELDS = ("pubDate", "published", "updated", "date")

#: 正文/内容字段的候选顺序（**不含** title/link：那两个是结构化字段）。
_CONTENT_FIELDS = ("encoded", "content", "description", "summary")


class FeedKind:
    """feed 形态（判形结果）。用普通类而非 Enum：值就是字符串，便于比较与打印。"""

    RSS = "rss"
    ATOM = "atom"
    UNKNOWN = "unknown"

    ALL = (RSS, ATOM, UNKNOWN)


@dataclass(frozen=True, slots=True)
class FieldProblem:
    """一处**已记录**的问题（字段缺失 / 日期无法解析 / 切片结构损坏）。

    硬规则 2 的落地：不允许用 `except` 掩盖接线错误。凡是"能产出但不对"的地方，
    都必须在这里留一条可被报告、可被断言的理由。
    """

    index: int
    field: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - 报告用
        return f"entry[{self.index}].{self.field}: {self.reason}"


@dataclass(frozen=True, slots=True)
class RawEntry:
    """一个条目的**纯文本层**产物：区间 + 字段，还没有 ID / 版本（见 `parser`）。

    `char_start` / `char_end` 是解码后 feed 文本上的字符区间，**左闭右开**。
    """

    index: int
    kind: str
    char_start: int
    char_end: int
    title: str
    link: str
    published_at: Optional[str]
    text: str
    problems: tuple[FieldProblem, ...] = ()


@dataclass(frozen=True, slots=True)
class FeedText:
    """一份 feed 文本的判形结果：形态 + 条目 + **顶层**问题。

    `entries` 为空**不代表失败**，但 `problems` 必须说明原因（空 feed / 未知格式）。
    """

    kind: str
    entries: tuple[RawEntry, ...]
    problems: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 解码与判形
# --------------------------------------------------------------------------- #


def _decode(raw_bytes: bytes, content_type: str) -> tuple[str, str]:
    """字节 → `(解码文本, 实际编码)`；失败抛 `EntryParseError`（不编造文本）。"""
    from atlas.normalize.text import NormalizeError, decode_bytes

    try:
        return decode_bytes(raw_bytes, content_type)
    except NormalizeError as exc:
        raise EntryParseError(f"feed 字节无法解码：{exc}") from exc
    except LookupError as exc:  # 未知字符集名
        raise EntryParseError(f"feed 声称的字符集未知：{exc}") from exc


def _missing_namespaces(text: str) -> dict[str, str]:
    """收集全文声明过的命名空间前缀（`start-ns` 事件）。

    解析中途出错时返回**已经收集到**的部分：真实的 feed 都先声明命名空间，
    因此部分结果对注入来说已经够用。
    """
    import io

    found: dict[str, str] = {}
    try:
        for _event, payload in ET.iterparse(io.StringIO(text), events=("start-ns",)):
            prefix, uri = payload
            if prefix:  # 空前缀（默认命名空间）由元素自身声明，不需要注入
                found.setdefault(prefix, uri)
    except ET.ParseError:
        pass
    return found


def local_name(tag: object) -> str:
    """去掉 `{uri}` 前缀，得到标签的本地名。非字符串标签（注释/PI）返回空串。"""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _make_self_contained(slice_text: str, namespaces: dict[str, str]) -> str:
    """把切片缺失的 `xmlns:` 声明注入它自己的起始标签，使切片可独立解析。"""
    match = _START_TAG_RE.match(slice_text)
    if match is None:
        raise ValueError(f"切片不是以标签开头：{slice_text[:60]!r}")
    name, attrs, self_closing = match.group(1), match.group(2) or "", match.group(3)
    if self_closing:
        return slice_text  # `<item/>`：没有内容，也没有需要解析的子元素
    missing = [item for item in namespaces.items() if f"xmlns:{item[0]}=" not in attrs]
    if not missing:
        return slice_text
    injected = "".join(f' xmlns:{prefix}="{uri}"' for prefix, uri in missing)
    return f"<{name}{injected}{attrs}>" + slice_text[match.end():]


def _find_spans(text: str, tag: str) -> Iterator[tuple[str, int, int, str]]:
    """按文档序产出该标签的 `(切片, 字符起始, 独占末尾, 问题标识)`。

    边界规则（确定性，无回溯）：

    - 起始标签 `<tag` 后必须紧跟空白、`>` 或 `/`（否则 `<items>` 会误命中 `<item`）；
    - 切片从起始标签起、**到结束标签末尾为止**（`<item>…</item>`，**含**结束标签）：
      只有这样才能得到结构完整、可独立 `fromstring` 解析的元素。
      `char_start` / `char_end` 就取自这个切片的边界，因此
      `char_end - char_start == len(slice_text)` 恒成立；
    - 没有结束标签 → 切片延伸到文末、`独占末尾 = len(text)`，问题标识
      `unclosed-element`（**不猜**边界）；
    - 起始标签无法匹配（属性里含未转义的 `<`）→ 产出空切片并记 `malformed-element`。
    """
    cursor = 0
    length = len(text)
    open_marker = f"<{tag}"
    close_marker = f"</{tag}>"
    while True:
        start = text.find(open_marker, cursor)
        if start < 0:
            return
        after = text[start + len(open_marker): start + len(open_marker) + 1]
        if after not in (" ", "\t", "\n", "\r", ">", "/"):
            cursor = start + len(open_marker)
            continue
        match = _START_TAG_RE.match(text, start)
        if match is None:
            yield "", start, start, SPAN_PROBLEM_MALFORMED
            cursor = start + len(open_marker)
            continue
        close = text.find(close_marker, match.end())
        if close < 0:
            yield text[start:length], start, length, SPAN_PROBLEM_UNCLOSED
            return
        exclusive_end = close + len(close_marker)
        yield text[start:exclusive_end], start, exclusive_end, ""
        cursor = exclusive_end


def _sniff_kind(text: str) -> str:
    """判形：**第一个**出现的元素名（先例：`atlas.feed.repository._looks_like_feed`）。

    不能简单地"在全文里找 `<rss`" —— 那样一段 HTML 里出现一次 `<rss>` 就会把
    HTML 判成 RSS。判据因此是：文档里**第一个**元素（跳过 `<?xml …?>` 声明、
    `<!-- 注释 -->`、`<!DOCTYPE …>`）是不是已知的 feed 根元素。
    `_TAG_NAME_RE` 要求 `<` 后紧跟字母/下划线，因此声明、注释、doctype 都不会命中。
    """
    match = _TAG_NAME_RE.search(text)
    if match is None:
        return FeedKind.UNKNOWN
    root = match.group(1).lower()
    if root in RSS_ROOT_TAGS:
        return FeedKind.RSS
    if root in ATOM_ROOT_TAGS:
        return FeedKind.ATOM
    return FeedKind.UNKNOWN


def _reject_non_xml(text: str, content_type: str) -> None:
    """"响亮失败"的判据：输入根本不是 XML。**

    判定用两条独立依据（都是结构性的，不看内容里是否出现过 `<item`）：

    1. 文本以 JSON 结构开头（`{"` / `[{`）；
    2. 文本以 HTML 文档标记开头（`<!doctype html` / `<html` / `<head` / `<body`）。

    真实数据里两类都有：`hacker-news-frontpage` 是 Algolia JSON API 响应，
    `ai-news-blog` 是 WordPress HTML 页面（HTTP 200，但根本没有 feed）。
    """
    head = text.lstrip()[:200].lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if head.startswith(_JSON_MARKERS) or mime == "application/json":
        raise EntryParseError(
            "输入不是 feed 而是 JSON（通常是 JSON API 响应），无法条目化："
            f"content_type={content_type!r}，开头 {text[:80]!r}。"
            "JSON API 源需要 `json_api` 适配器，不属于 T-130 的范围。"
        )
    if head.startswith(_HTML_MARKERS) or mime == "text/html":
        raise EntryParseError(
            "输入不是 feed 而是 HTML 文档（端点可能已失效或返回了错误页），无法条目化："
            f"content_type={content_type!r}，开头 {text[:80]!r}"
        )


def decode_feed(raw_bytes: bytes, content_type: str = "") -> tuple[str, str, str]:
    """字节 → `(feed 文本, 编码, 形态)`。非 feed 输入**响亮失败**。

    Raises:
        EntryParseError: 无法解码、不是 XML、或 XML 结构损坏。
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise EntryParseError(f"raw_bytes 必须是 bytes-like，得到 {type(raw_bytes).__name__}")
    payload = bytes(raw_bytes)
    if not payload:
        raise EntryParseError("feed 字节为空（0 字节）：空响应体按 SPEC §2.12 视为失败，不是空 feed")

    text, encoding = _decode(payload, content_type)
    if not text.strip():
        raise EntryParseError("feed 解码后只有空白：不是可解析的 feed")
    _reject_non_xml(text, content_type)

    # XML **良构性**自检（响亮失败的第二道闸）：
    # 完整解析一遍，结构损坏（截断 / 标签不闭合 / 乱码）在这里就停下。
    # 关键：这一步**先于判形**，否则"根元素名恰好是 rss 的截断文档"会被当成
    # "0 条目的空 feed"而静默通过 —— 那正是"假装成功"。
    # 未知根元素的**合法** XML 会通过，由调用方返回 0 条 + 理由（"不认识的格式"）。
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        raise EntryParseError(
            f"输入不是良构 XML（{exc}），无法条目化：content_type={content_type!r}，"
            f"开头 {text[:80]!r}"
        ) from exc
    return text, encoding, _sniff_kind(text)


# 局部别名：避免在模块级 import io 只为这一处（保持导入面窄）。
from io import StringIO as _StringIO  # noqa: E402  (放在使用点附近，见上方 docstring)


def xml_declared_encoding(text: str) -> Optional[str]:
    """XML 声明里写的编码名（没有声明则 `None`）。"""
    match = _XML_DECL_RE.match(text)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 逐条目字段提取
# --------------------------------------------------------------------------- #


def _element_text(element: ET.Element) -> str:
    """元素的完整文本（含子元素的 text/tail），用于 `itertools.chain` 式拼接。"""
    pieces: list[str] = []
    if element.text:
        pieces.append(element.text)
    for child in element:
        pieces.append(_element_text(child))
        if child.tail:
            pieces.append(child.tail)
    return "".join(pieces)


def _strip_markup(value: str) -> str:
    """去掉 HTML 标签与实体，得到可读文本（Atom `type="html"` 的标题会用到）。"""
    return _html.unescape(_ANY_TAG_RE.sub("", value)).strip()


def text_of(element: ET.Element, tag: str) -> Optional[str]:
    """取第一个本地名为 `tag` 的子元素的文本；没有则 `None`。

    `type="html"` / `type="xhtml"` 的标题会先剥标签再解实体（真实数据里
    google-ai-blog 的 `<content type="html">` 就是转义后的 HTML 字面量）。
    """
    for child in element:
        if local_name(child.tag) != tag:
            continue
        raw = _element_text(child)
        declared = (child.get("type") or "").lower()
        if declared in ("html", "xhtml"):
            stripped = _strip_markup(raw)
            return stripped or raw.strip()
        return raw.strip()
    return None


def _first_text(element: ET.Element, tags: Sequence[str]) -> Optional[str]:
    for tag in tags:
        value = text_of(element, tag)
        if value:
            return value
    return None


def _atom_link(element: ET.Element) -> Optional[str]:
    """Atom 的链接在 `href` 属性上；优先 `rel="alternate"`，再退到第一个 http(s)。"""
    candidates: list[str] = []
    for child in element:
        if local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        if (child.get("rel") or "alternate").lower() == "alternate":
            return href
        candidates.append(href)
    return candidates[0] if candidates else None


def _rss_link(element: ET.Element) -> Optional[str]:
    """RSS 的链接：`<link>` 文本 → `<guid isPermaLink="true">` → Atom 风格 `<link href>`。"""
    text_link = text_of(element, "link")
    if text_link:
        return text_link
    for child in element:
        if local_name(child.tag) == "link":
            href = (child.get("href") or "").strip()
            if href:
                return href
    for child in element:
        if local_name(child.tag) != "guid":
            continue
        permalink = (child.get("isPermaLink") or "true").strip().lower()
        value = _element_text(child).strip()
        if permalink == "true" and value:
            return value
    return None


def _parse_moment(value: str) -> Optional[datetime]:
    """RFC 2822（RSS）或 ISO 8601（Atom）→ 带时区的 `datetime`；失败返回 `None`。"""
    candidate = value.strip()
    if not candidate:
        return None
    try:
        moment = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        moment = None
    if moment is None:
        iso = candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z")) else candidate
        try:
            moment = datetime.fromisoformat(iso)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    """定宽 UTC 串（与 T-205 索引里的时间键同一形状）：字符串比较即时间比较。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _entry_text(element: ET.Element) -> str:
    """条目的"可读正文"：优先 `content:encoded` / `content`，再退 `description` / `summary`。"""
    for field in _CONTENT_FIELDS:
        value = text_of(element, field)
        if value:
            return value
    return ""


def parse_span(
    slice_text: str,
    *,
    index: int,
    kind: str,
    char_start: int,
    namespaces: dict[str, str],
) -> RawEntry:
    """一个 `<item>` / `<entry>` 切片 → `RawEntry`（字段缺失**记录理由**，不静默丢）。"""
    problems: list[FieldProblem] = []
    try:
        element = ET.fromstring(_make_self_contained(slice_text, namespaces))
    except (ET.ParseError, ValueError) as exc:
        problems.append(FieldProblem(index, "element", f"XML 无法解析：{exc}"))
        return RawEntry(
            index=index,
            kind=kind,
            char_start=char_start,
            char_end=char_start + len(slice_text),
            title="",
            link="",
            published_at=None,
            text="",
            problems=tuple(problems),
        )

    title = _first_text(element, ("title",)) or ""
    if not title:
        problems.append(FieldProblem(index, "title", "条目缺标题（RSS <title> / Atom <title>）"))

    link = _atom_link(element) if kind is FeedKind.ATOM else _rss_link(element)
    if link is None:
        link = _atom_link(element) or _rss_link(element)
    if not link:
        problems.append(
            FieldProblem(index, "link", "条目缺链接（<link> / <guid isPermaLink=\"true\"> / <id>）")
        )
        link = ""

    published_raw = _first_text(element, _DATE_FIELDS)
    published_at: Optional[str] = None
    if published_raw is None:
        problems.append(FieldProblem(index, "published_at", "条目缺发布时间字段"))
    else:
        moment = _parse_moment(published_raw)
        if moment is None:
            problems.append(
                FieldProblem(index, "published_at", f"发布时间无法解析：{published_raw!r}")
            )
        else:
            published_at = _iso_utc(moment)

    return RawEntry(
        index=index,
        kind=kind,
        char_start=char_start,
        char_end=char_start + len(slice_text),
        title=title,
        link=link,
        published_at=published_at,
        text=_entry_text(element),
        problems=tuple(problems),
    )


def parse_feed_text(text: str, *, kind: str) -> FeedText:
    """已判形的 feed 文本 → 条目序列（按文档序，索引从 0 起）。

    条目数为 0 时 `problems` 必须非空：要么是未知格式，要么是空 feed ——
    "返回空"永远带着理由，绝不静默。
    """
    tag = RSS_ITEM_TAG if kind is FeedKind.RSS else ATOM_ENTRY_TAG
    namespaces = _missing_namespaces(text)
    entries: list[RawEntry] = []
    problems: list[str] = []

    if kind is FeedKind.UNKNOWN:
        return FeedText(
            kind=kind,
            entries=(),
            problems=(
                "无法识别的 feed 格式：根元素既不是 <rss>/<rdf>（RSS 2.0）也不是 <feed>（Atom）。"
                f"开头 {text.lstrip()[:80]!r}。XML 本身是良构的，因此这是「不认识的格式」"
                "而不是「坏输入」；未产出任何条目，也未做任何猜测。",
            ),
        )

    for index, (slice_text, start, _exclusive_end, problem) in enumerate(_find_spans(text, tag)):
        if problem:
            problems.append(
                f"entry[{index}]: 元素切片结构异常（{problem}）：{slice_text[:80]!r}"
            )
            if problem == SPAN_PROBLEM_MALFORMED:
                continue
        entries.append(
            parse_span(
                slice_text,
                index=index,
                kind=kind,
                char_start=start,
                namespaces=namespaces,
            )
        )

    if not entries:
        problems.append(
            f"feed 里没有任何 <{tag}> 条目（形态判定为 {kind}）："
            "HTTP 200 但内容为空/为错误页时按 SPEC §2.12 视为失败，这里如实报 0 条而不编造内容"
        )
    return FeedText(kind=kind, entries=tuple(entries), problems=tuple(problems))
