"""将已存正文变成可阅读的 HTML；外部 HTML 与 Markdown 使用同一道允许列表。"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from markdown_it import MarkdownIt

from atlas.cognition.briefs import is_chinese
from atlas.normalize.text import decode_bytes

RENDER_VERSION = "article-reader-v1"
_MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
_ALLOWED = frozenset(
    "p div section article span h1 h2 h3 h4 h5 h6 a strong b em i u s del blockquote pre code ul ol li dl dt dd br hr table thead tbody tfoot tr th td caption figure figcaption img sup sub details summary kbd samp mark abbr math mi mn mo mrow mfrac msub msup msubsup msqrt mroot mtext mtable mtr mtd semantics annotation".split()
)
_REMOVE = "script,style,noscript,iframe,object,embed,form,input,button,textarea,select,svg,canvas,video,audio,link,meta,base,template,nav,header,footer,aside,[hidden],[aria-hidden=true],.related-posts,.related-articles,.related-content,.recommended-posts,.newsletter,.cookie-banner,.cookie-consent,.share-buttons,.social-share,.author-bio,.advertisement,.ad-container"
_CONTENT = '.markdown-body,[itemprop="articleBody"],.entry-content,.post-content,.post-body,.article-body,.article-content,.story-body'
_MAIN = 'main,[role="main"],#main-content,#content,.readme'


def external_url(value: str, base_url: str) -> str:
    value = urljoin(base_url, value.strip())
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return value


def sanitize_html(value: str, base_url: str, *, extract_main: bool = False) -> str:
    soup = BeautifulSoup(value, "lxml")
    for node in list(soup.select(_REMOVE)):
        node.decompose()
    if extract_main:

        def score(node):
            text = node.get_text(" ", strip=True)
            links = sum(len(a.get_text(" ", strip=True)) for a in node.select("a"))
            return len(text) - links

        # 显式正文区优先于 article，再回落 main。GitHub 的 main 还包含文件列表和提交记录。
        candidates = (
            soup.select(_CONTENT) or soup.select("article") or soup.select(_MAIN)
        )
        root = max(candidates, key=score) if candidates else (soup.body or soup)
    else:
        root = soup.body or soup
    for comment in root.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for node in list(root.find_all(True)):
        if node.name not in _ALLOWED:
            node.unwrap()
            continue
        attrs = {}
        if node.get("title"):
            attrs["title"] = str(node["title"])[:500]
        if node.name == "a":
            href = str(node.get("href", ""))
            href = (
                "#atlas-body-" + href[1:]
                if href.startswith("#")
                else external_url(href, base_url)
            )
            if href:
                attrs.update(href=href, rel="noreferrer noopener")
        if node.get("id"):
            attrs["id"] = "atlas-body-" + str(node["id"])
        if node.name == "img":
            src = (
                node.get("data-src")
                or node.get("data-original")
                or node.get("src")
                or ""
            )
            if not src and node.get("srcset"):
                src = str(node["srcset"]).split(",")[-1].strip().split()[0]
            src = external_url(str(src), base_url)
            if not src:
                node.decompose()
                continue
            attrs.update(
                src=src,
                alt=str(node.get("alt", "图片"))[:500],
                loading="lazy",
                decoding="async",
                referrerpolicy="no-referrer",
            )
            for dimension in ("width", "height"):
                if (
                    str(node.get(dimension, "")).isdigit()
                    and 0 < int(node[dimension]) <= 10000
                ):
                    attrs[dimension] = str(node[dimension])
        if node.name in ("td", "th"):
            for key in ("colspan", "rowspan"):
                if str(node.get(key, "")).isdigit() and 1 <= int(node[key]) <= 100:
                    attrs[key] = str(node[key])
        if node.name == "ol" and re.fullmatch(r"-?\d{1,6}", str(node.get("start", ""))):
            attrs["start"] = str(node["start"])
        if node.name == "code":
            classes = [
                c
                for c in node.get("class", [])
                if re.fullmatch(r"language-[\w+-]{1,40}", c)
            ]
            if classes:
                attrs["class"] = classes
        if node.name == "math" and node.get("display") in ("inline", "block"):
            attrs["display"] = node["display"]
        if node.name == "annotation" and node.get("encoding") in (
            "application/x-tex",
            "text/plain",
        ):
            attrs["encoding"] = node["encoding"]
        node.attrs = attrs
    return root.decode_contents()


@dataclass(frozen=True)
class ReaderDocument:
    html: str
    source_url: str
    native_chinese: bool
    note: str
    format: str

    @property
    def key(self) -> str:
        return hashlib.sha256(
            (RENDER_VERSION + self.source_url + self.html).encode()
        ).hexdigest()


def feed_excerpt(fragment: str, fallback: str) -> str:
    soup = BeautifulSoup(fragment, "xml")
    body = soup.find(["description", "summary", "content", "encoded"])
    return (
        (str(body.string) if body.string is not None else body.decode_contents())
        if body
        else fallback
    )


def make_document(
    content: bytes | str,
    content_type: str,
    source_url: str,
    *,
    fallback_text: str = "",
    note: str = "",
) -> ReaderDocument:
    if isinstance(content, bytes):
        text = (
            ""
            if content.startswith(b"%PDF") or "application/pdf" in content_type
            else decode_bytes(content, content_type, is_html="html" in content_type)[0]
        )
    else:
        text = content
    if (
        "application/pdf" in content_type
        or (isinstance(content, bytes) and content.startswith(b"%PDF"))
        or text.startswith("%PDF")
    ):
        text, kind = fallback_text, "text"
    elif not text.strip():
        text = fallback_text
        kind = (
            "markdown"
            if re.search(r"(?m)^(?:#{1,6} |```|\s*[-*] |\s*\d+\. )", text)
            else "text"
        )
    elif "markdown" in content_type or urlsplit(source_url).path.lower().endswith(
        (".md", ".markdown")
    ):
        kind = "markdown"
    elif "html" in content_type or re.search(
        r"<(?:html|article|main|p|div|h[1-6]|ul|ol|table|figure)\b", text, re.I
    ):
        kind = "html"
    elif re.search(r"(?m)^(?:#{1,6} |```|\s*[-*] |\s*\d+\. )|!\[[^\]]*\]\(", text):
        kind = "markdown"
    else:
        kind = "text"
    if kind == "markdown":
        rendered = _MARKDOWN.render(text)
    elif kind == "html":
        rendered = text
    else:
        rendered = "".join(
            "<p>" + html.escape(part).replace("\n", "<br>") + "</p>"
            for part in re.split(r"\n\s*\n", text)
            if part.strip()
        )
    rendered = sanitize_html(rendered, source_url, extract_main=kind == "html")
    readable = BeautifulSoup(rendered, "lxml")
    for node in readable.select("pre,code,math"):
        node.decompose()
    plain = readable.get_text(" ", strip=True)
    native_chinese = is_chinese(plain) and not re.search(
        r"[\u3040-\u30ff\uac00-\ud7af]", plain
    )
    return ReaderDocument(rendered, source_url, bool(native_chinese), note, kind)
