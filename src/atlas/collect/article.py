"""公开文章正文读取。供显式简介批处理使用，页面不会联网。"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from atlas.collect.article_failure import classify_article_failure
from atlas.collect.robots import RobotsCache, RobotsFetchResult
from atlas.collect.throttle import ThrottleTimeout

USER_AGENT = "Atlas/0.1 (Article summaries)"
MAX_BYTES = 32 * 1024 * 1024
CLEANER_VERSION = "article-clean-v3"
MAX_BODY_CHARS = 2_000_000


class ArticleReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        url: str = "",
        raw_content: bytes = b"",
        content_type: str = "",
    ):
        super().__init__(message)
        self.url, self.raw_content, self.content_type = url, raw_content, content_type
        self.failure = classify_article_failure(message, url)


class OriginThrottle:
    """所有工作线程共享；同源串行，遵守默认间隔和 robots Crawl-delay。"""

    def __init__(self, interval=1.0, max_wait=30.0, shared=None):
        self.interval = interval
        self.max_wait = max_wait
        self.shared = shared
        self._guard = threading.Lock()
        self._locks = {}
        self._last = {}
        self._delays = {}

    def set_delay(self, url, seconds):
        host = urlsplit(url).hostname or ""
        with self._guard:
            self._delays[host] = max(seconds, self._delays.get(host, 0))

    @contextmanager
    def slot(self, url, *, robots_request=False):
        if self.shared is not None:
            delay = max(self.interval, 15.0 if (urlsplit(url).hostname or "") in ("arxiv.org", "export.arxiv.org") else 0)
            try:
                with self.shared.slot(url, delay, robots_request=robots_request):
                    yield
            except ThrottleTimeout as error:
                raise ArticleReadError("原文等待站点限速：" + str(error), url=url) from error
            return
        host = urlsplit(url).hostname or ""
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            delay = max(
                self.interval,
                self._delays.get(host, 0),
                15.0 if host in ("arxiv.org", "export.arxiv.org") else 0,
            )
            last = self._last.get(host)
            wait = last + delay - time.monotonic() if last is not None else 0
            if wait > self.max_wait:
                # 不缩短站点要求的间隔；让调用方暂缓此来源，避免阻塞整个批次。
                raise ArticleReadError(
                    f"原文请求等待超时：站点要求间隔 {delay:g} 秒，"
                    f"尚需等待 {wait:.1f} 秒", url=url
                )
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()
            yield


@dataclass(frozen=True)
class ArticleContent:
    text: str
    url: str
    raw_content: bytes = b""
    content_type: str = ""
    content_kind: str = "article"
    cleaner_version: str = CLEANER_VERSION
    truncated: bool = False


@dataclass(frozen=True)
class CleanedDocument:
    text: str
    content_kind: str
    truncated: bool


def clean_paragraphs(text: str) -> str:
    text = (
        unicodedata.normalize("NFC", text)
        .replace("\u200b", "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
    )
    # PDF 字体映射可能产出 NUL 等控制字符；SQLite length 会在 NUL 处停止。
    # 保留代码缩进所需的 TAB 与段落换行，剔除不构成正文的控制符。
    text = text.replace("\f", "\n\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    # 代码块保留缩进和换行；其余文本统一段落空白。
    code_blocks = []

    def protect(match):
        code_blocks.append(match.group(0))
        return f"\nATLAS_CODE_BLOCK_{len(code_blocks) - 1}\n"

    text = re.sub(r"```[\s\S]*?```", protect, text)
    paragraphs = []
    for line in text.splitlines():
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        if line and (not paragraphs or line != paragraphs[-1]):
            paragraphs.append(line)
    text = "\n\n".join(paragraphs)
    for index, code in enumerate(code_blocks):
        text = text.replace(f"ATLAS_CODE_BLOCK_{index}", code)
    return text


def public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ArticleReadError("原文 URL 无效")
    try:
        # 本机代理网络部分域名带不可用的 IPv6 占位记录，优先使用 IPv4。
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = socket.getaddrinfo(
                parsed.hostname, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            addresses = socket.getaddrinfo(
                parsed.hostname, port, family=socket.AF_INET6, type=socket.SOCK_STREAM
            )
    except (OSError, ValueError) as error:
        raise ArticleReadError("原文域名无法解析") from error
    if not addresses or any(
        not ipaddress.ip_address(address[4][0]).is_global for address in addresses
    ):
        raise ArticleReadError("原文 URL 不是公网地址")


def clean_document(content: bytes, content_type: str = "") -> CleanedDocument:
    if content.startswith(b"%PDF") or "application/pdf" in content_type:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages[:500])
        except (PdfReadError, ValueError, KeyError) as error:
            raise ArticleReadError("原文 PDF 无法提取文字") from error
        text = clean_paragraphs(text)
        return CleanedDocument(
            text[:MAX_BODY_CHARS],
            "paper",
            len(reader.pages) > 500 or len(text) > MAX_BODY_CHARS,
        )
    if content_type and not any(
        kind in content_type for kind in ("html", "text/", "xml", "json")
    ):
        raise ArticleReadError("原文不是支持的文本或 PDF")
    soup = BeautifulSoup(content, "lxml")
    page_title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    if any(
        marker in page_title
        for marker in (
            "just a moment",
            "access denied",
            "verify you are human",
            "attention required",
            "captcha",
        )
    ):
        raise ArticleReadError("原文要求访问验证")
    for node in soup.select(
        'script,style,noscript,nav,header,footer,aside,form,svg,iframe,[hidden],[aria-hidden="true"],'
        ".related-posts,.related-articles,.related-content,.recommended-posts,.newsletter,.subscription-prompt"
    ):
        node.decompose()
    for node in soup.select(
        ".cookie-banner,.cookie-consent,#cookie-banner,#onetrust-banner-sdk,.share-buttons,.social-share,.author-bio,.advertisement,.ad-container"
    ):
        node.decompose()
    # JSON-LD / 菜单 / 页脚不是正文；优先站点声明的文章主区。
    candidates = soup.select(
        'article,main,[role="main"],#main-content,#content,.entry-content,.post-content,.post-body,.article-body,.article-content,.story-body,.markdown-body,.readme'
    )

    def score(node):
        text = node.get_text(" ", strip=True)
        links = sum(len(link.get_text(" ", strip=True)) for link in node.select("a"))
        return len(text) if links < len(text) * 0.7 else 0

    main = max(candidates, key=score) if candidates else None
    if main is not None and score(main) == 0:
        main = None
    if main is None:
        paragraphs = soup.select(
            "p, pre, .comment, .readme, .markdown-body, .entry-content, .blob-code"
        )
        text = "\n".join(
            p.get_text(" ", strip=True) for p in paragraphs if score(p) > 0
        )
        if len(text) < 80 and ("text/plain" in content_type or b"<" not in content):
            text = soup.get_text(" ", strip=True)
    else:
        for pre in list(main.select("pre")):
            pre.replace_with("\n\n```\n" + pre.get_text().strip("\n") + "\n```\n\n")
        # 保留段落、标题、列表、代码块的边界；不把整篇压成一行。
        for br in main.select("br"):
            br.replace_with("\n")
        for block in list(
            main.select("p,div,section,li,h1,h2,h3,h4,h5,h6,pre,blockquote,tr")
        ):
            block.insert_before("\n\n")
            block.insert_after("\n\n")
        text = main.get_text(" ", strip=False)
    text = clean_paragraphs(text)
    return CleanedDocument(text[:MAX_BODY_CHARS], "article", len(text) > MAX_BODY_CHARS)


def extract_article(content: bytes, content_type: str = "") -> str:
    """既有简介接口保持单行；正文留存使用 clean_document 的段落文本。"""
    return " ".join(clean_document(content, content_type).text.split())


class ArticleReader:
    """每个工作线程一实例；robots 按 origin 复用，重定向逐跳检查。"""

    def __init__(self, *, throttle: OriginThrottle | None = None, shared=None):
        self.throttle = throttle or OriginThrottle(shared=shared)
        self.shared = shared or self.throttle.shared
        self.browser_routes = {}
        self.browser_fetcher = None
        if self.shared is not None:
            from .chromium import ChromiumFetcher, browser_routes

            self.browser_routes = browser_routes(self.shared.root.parent)
            if self.browser_routes:
                self.browser_fetcher = ChromiumFetcher()
        fetch = self._robots
        if self.shared is not None:
            def fetch(url, *, user_agent):
                return self.shared.robots(url, user_agent, self._robots)
        self.robots = RobotsCache(fetch, user_agent=USER_AGENT, cache_ttl_seconds=86400 if self.shared else None)

    def _get(self, url: str, *, check_robots: bool):
        for _ in range(6):
            public_url(url)
            browser_agent = self.browser_routes.get(urlsplit(url).hostname)
            if check_robots:
                decision = (
                    self.robots.check(url, user_agent=browser_agent)
                    if browser_agent else self.robots.check(url)
                )
                if not decision.allowed:
                    if self.shared is not None and ("ThrottleTimeout" in decision.reason or "等待站点限速" in decision.reason):
                        raise ArticleReadError("原文等待站点限速：" + decision.reason, url=url)
                    raise ArticleReadError(
                        "robots 拒绝或无法确认允许：" + decision.outcome.value
                    )
            try:
                if browser_agent:
                    from .fetch import FetchError, FetchRequest

                    try:
                        with self.throttle.slot(url, robots_request=not check_robots):
                            result = self.browser_fetcher(FetchRequest(
                                url=url, user_agent=browser_agent, timeout_seconds=20
                            ))
                    except FetchError as error:
                        raise ArticleReadError("原文浏览器请求失败：" + str(error), url=url) from error
                    if result.status_code in (301, 302, 303, 307, 308):
                        location = result.headers.get("location")
                        if not location:
                            raise ArticleReadError("原文重定向缺少目标")
                        url = urljoin(url, location)
                        continue
                    return (
                        result.status_code, result.content,
                        result.headers.get("content-type", ""), result.url,
                    )
                with (
                    self.throttle.slot(url, robots_request=not check_robots),
                    requests.get(
                        url,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "text/html,application/pdf,text/plain;q=0.9",
                        },
                        timeout=(8, 15),
                        allow_redirects=False,
                        stream=True,
                    ) as response,
                ):
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise ArticleReadError("原文重定向缺少目标")
                        url = urljoin(url, location)
                        continue
                    chunks, length = [], 0
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > MAX_BYTES:
                            raise ArticleReadError("原文响应超过 32 MiB")
                        chunks.append(chunk)
                    return (
                        response.status_code,
                        b"".join(chunks),
                        response.headers.get("Content-Type", ""),
                        url,
                    )
            except requests.RequestException as error:
                raise ArticleReadError(
                    "原文网络请求失败：" + type(error).__name__
                ) from error
        raise ArticleReadError("原文重定向次数过多")

    def _robots(self, url: str, *, user_agent: str):
        try:
            status, body, _, final = self._get(url, check_robots=False)
            if status == 200:
                from urllib.robotparser import RobotFileParser

                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                delay = parser.crawl_delay(user_agent) or parser.crawl_delay("*")
                if delay:
                    self.throttle.set_delay(url, float(delay))
            return RobotsFetchResult(url=final, status_code=status, body=body)
        except ArticleReadError as error:
            return RobotsFetchResult(
                url=url, status_code=None, body=b"", error=str(error)
            )

    def read(self, url: str) -> ArticleContent:
        try:
            status, content, content_type, final = self._get(url, check_robots=True)
        except ArticleReadError as error:
            if self.shared is not None and error.failure.code == "site_deferred" and error.url:
                self.shared.defer_origin(
                    url, error.url,
                    max(self.shared.ready_at(error.url), self.shared.clock() + 1),
                )
            if not error.url:
                error.url = url
                error.failure = classify_article_failure(str(error), url)
            raise
        if status != 200:
            raise ArticleReadError(
                f"原文 HTTP {status}",
                url=final,
                raw_content=content,
                content_type=content_type,
            )
        try:
            cleaned = clean_document(content, content_type)
        except ArticleReadError as error:
            raise ArticleReadError(
                str(error), url=final, raw_content=content, content_type=content_type
            ) from error
        if len(cleaned.text) < 80:
            raise ArticleReadError(
                "原文缺少足够的可读正文",
                url=final,
                raw_content=content,
                content_type=content_type,
            )
        return ArticleContent(
            cleaned.text,
            final,
            content,
            content_type,
            cleaned.content_kind,
            CLEANER_VERSION,
            cleaned.truncated,
        )


def is_paper(url):
    return (urlsplit(url).hostname or "").lower() in (
        "arxiv.org",
        "www.arxiv.org",
        "export.arxiv.org",
    )


def read_source(reader, url):
    if not is_paper(url):
        return reader.read(url)
    parsed = urlsplit(url)
    identifier = (
        parsed.path.removeprefix("/abs/")
        .removeprefix("/html/")
        .removeprefix("/pdf/")
        .removesuffix(".pdf")
    )
    if not identifier or parsed.path.startswith(("/rss", "/list", "/search")):
        raise ArticleReadError("原文 URL 无效：论文标识缺失", url=url)
    html_url = "https://arxiv.org/html/" + identifier
    try:
        result = reader.read(html_url)
        soup = BeautifulSoup(result.raw_content, "lxml")
        if soup.select_one(".ltx_document, .ltx_section") is not None:
            return replace(result, content_kind="paper")
        raise ArticleReadError(
            "论文 HTML 未包含正文",
            url=html_url,
            raw_content=result.raw_content,
            content_type=result.content_type,
        )
    except ArticleReadError as error:
        if error.failure.category not in ("not_found", "content", "format"):
            raise
    # 官方提供的另一种公开格式；拒绝访问/验证失败时不尝试绕过。
    return replace(
        reader.read("https://arxiv.org/pdf/" + identifier), content_kind="paper"
    )
