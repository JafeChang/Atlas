"""浏览适配：Algolia/HN JSON 的原文区间，以及明确的验证码跳转响应。

XML 解析器与其既有 ID 保持原样；JSON 的区间由原始文本上的 raw_decode 产生，
不能重序列化 JSON 后再计算坐标。本模块无 I/O，也不参与机器分类分流。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from atlas.contracts import EvidenceAnchor
from atlas.contracts.ids import content_sha256
from atlas.normalize.text import decode_bytes

from .ids import entry_id_for

ALGOLIA_VERSION = "algolia-hn-v1"


@dataclass(frozen=True)
class AlgoliaEntry:
    entry_id: str
    raw_id: str
    raw_sha256: str
    index: int
    char_start: int
    char_end: int
    title: str
    link: str
    published_at: str | None
    kind: str = "algolia-hn"
    parser_version: str = ALGOLIA_VERSION

    @property
    def text_length(self) -> int:
        return self.char_end - self.char_start

    def content_slice(self, text: str) -> str:
        # 展示原始 JSON 对象切片；标记与转义均由页面统一安全处理。
        return text[self.char_start:self.char_end]

    def anchor(self) -> EvidenceAnchor:
        return EvidenceAnchor.create(
            raw_id=self.raw_id, raw_sha256=self.raw_sha256,
            char_start=self.char_start, char_end=self.char_end,
        )


@dataclass(frozen=True)
class AlgoliaFeed:
    entries: tuple[AlgoliaEntry, ...]
    feed_text: str
    problems: tuple[str, ...]


def _skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _hits_start(text: str, decoder: json.JSONDecoder) -> int:
    """跳过完整顶层成员；不会误取 highlight、字符串或嵌套对象里的 hits。"""
    position = _skip_space(text, 0) + 1
    found = -1
    while text[_skip_space(text, position)] != "}":
        key, end = decoder.raw_decode(text, _skip_space(text, position))
        start = _skip_space(text, _skip_space(text, end) + 1)
        _, position = decoder.raw_decode(text, start)
        if key == "hits":
            found = start  # 与 json.loads 对重复键采用最后一个值的语义一致。
        position = _skip_space(text, position)
        if text[position] == ",":
            position += 1
    assert found >= 0  # 调用前已通过 JSON 与 hits 数组结构检查。
    return found + 1


def parse_algolia_entries(raw_bytes: bytes, *, raw_id: str) -> AlgoliaFeed | None:
    """按内容结构识别 Algolia 搜索响应；不匹配时返回 None 交给既有解析器。"""
    text, _ = decode_bytes(raw_bytes)
    if not text.lstrip().startswith("{"):
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not (
        isinstance(document, dict) and isinstance(document.get("hits"), list)
        and type(document.get("nbHits")) is int
        and type(document.get("hitsPerPage")) is int
    ):
        return None

    digest = content_sha256(raw_bytes)
    decoder = json.JSONDecoder()
    position = _hits_start(text, decoder)
    entries: list[AlgoliaEntry] = []
    problems: list[str] = []
    for index in range(len(document["hits"])):
        start = _skip_space(text, position)
        hit, end = decoder.raw_decode(text, start)
        position = _skip_space(text, end)
        if text[position] == ",":
            position += 1
        if not isinstance(hit, dict) or not isinstance(hit.get("title"), str) or not hit["title"].strip():
            problems.append(f"Hacker News 第 {index + 1} 条缺少标题，未作为文章展示。")
            continue
        title = hit["title"].strip()
        link = hit.get("url")
        if not isinstance(link, str) or not link.startswith(("https://", "http://")):
            story_id = str(hit.get("objectID", ""))
            if not story_id.isascii() or not story_id.isdigit():
                problems.append(f"Hacker News 第 {index + 1} 条缺少文章链接与讨论标识。")
                continue
            link = f"https://news.ycombinator.com/item?id={story_id}"
        published_at = None
        value = hit.get("created_at")
        if isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value)
                if moment.tzinfo is not None:
                    published_at = moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
            except ValueError:
                pass  # 日期降级必须在下方留下诊断，页面回退到真实采集时间。
        if published_at is None:
            problems.append(f"Hacker News 第 {index + 1} 条发布时间不可用，显示采集时间。")
        entries.append(AlgoliaEntry(
            entry_id=entry_id_for(
                raw_id=raw_id, raw_sha256=digest, parser_version=ALGOLIA_VERSION,
                char_start=start, char_end=end, title=title,
            ),
            raw_id=raw_id, raw_sha256=digest, index=index, char_start=start, char_end=end,
            title=title, link=link, published_at=published_at,
        ))
    if not entries:
        problems.append("Hacker News 搜索响应中没有可展示的新闻。")
    return AlgoliaFeed(tuple(entries), text, tuple(problems))


class _ChallengeRedirect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag != "meta" or (fields.get("http-equiv") or "").lower() != "refresh":
            return
        target = (fields.get("content") or "").partition(";")[2].strip()
        if target.lower().startswith("url="):
            target = target[4:].strip()
        # 按真实重定向目标识别，不因文章正文提到验证码而屏蔽文章。
        try:
            path = urlsplit(target.strip("\"'")).path
        except ValueError:
            return
        self.found = self.found or path.startswith("/.well-known/sgcaptcha/")


def is_challenge_page(text: str) -> bool:
    parser = _ChallengeRedirect()
    parser.feed(text)
    return parser.found
