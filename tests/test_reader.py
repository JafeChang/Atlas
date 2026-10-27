"""阅读格式、远程图片边界、真实磁盘 LRU 与可恢复全文翻译。"""

import base64
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from atlas.reader.content import make_document
from atlas.reader.images import (
    MAX_IMAGE_CACHE_BYTES,
    DiskImageCache,
    ImageUnavailable,
    fetch_image,
    sanitize_svg,
)
from atlas.reader.translations import TranslationClient, TranslationJobs

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII="
)


def finished(jobs, key):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = jobs.status(key)
        if result["status"] != "running":
            return result
        time.sleep(0.01)
    pytest.fail("翻译任务未结束")


def test_html_and_markdown_keep_readable_structure_without_active_content():
    document = make_document(
        '<html><head><base href="http://localhost/"></head><body><nav>MENU</nav>'
        '<article><h2 id="chapter">Heading</h2><p><strong>Body</strong> text.</p>'
        '<img data-src="../photo.png" alt="Figure" onerror="alert(1)">'
        '<a href="javascript:alert(1)">bad</a><a href="/more">good</a>'
        '<script>alert(1)</script><iframe src="https://example.test"></iframe>'
        '<pre><code class="language-python" onclick="alert(1)">print(1)</code></pre>'
        "</article></body></html>",
        "text/html",
        "https://example.test/posts/one",
    )
    soup = BeautifulSoup(document.html, "html.parser")
    assert soup.select_one("h2").get_text() == "Heading"
    assert soup.select_one("strong").get_text() == "Body"
    assert soup.select_one("img")["src"] == "https://example.test/photo.png"
    assert soup.select_one("pre code").get_text() == "print(1)"
    assert not soup.select("script,iframe,[onclick],[onerror],base,nav")
    assert not soup.find("a", string="bad").has_attr("href")
    assert soup.find("a", string="good")["href"] == "https://example.test/more"
    assert not document.native_chinese

    markdown = make_document(
        "# Heading\n\n**Strong** and [link](/post).\n\n![photo](/p.png)\n\n"
        "```python\nprint('<script>')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "<script>bad()</script>",
        "text/markdown",
        "https://example.test/readme.md",
    )
    soup = BeautifulSoup(markdown.html, "html.parser")
    assert soup.select_one("h1").get_text() == "Heading"
    assert soup.select_one("table td").get_text() == "1"
    assert soup.select_one("pre code").get_text() == "print('<script>')\n"
    assert soup.select_one("img")["src"] == "https://example.test/p.png"
    assert not soup.select("script")


def test_chinese_body_and_pdf_fallback_do_not_treat_text_as_html():
    document = make_document(
        "这是中文正文，包含文章内容及其方法介绍。",
        "text/plain",
        "https://example.test/zh",
    )
    assert document.native_chinese
    japanese = make_document(
        "これは日本語の文章です。研究方法について説明します。",
        "text/plain",
        "https://example.test/ja",
    )
    assert not japanese.native_chinese
    pdf = make_document(
        b"%PDF fake",
        "application/pdf",
        "https://example.test/paper",
        fallback_text="Original paper text.\n\nSecond paragraph <script>unsafe()</script>.",
    )
    assert len(BeautifulSoup(pdf.html, "html.parser").select("p")) == 2
    assert not BeautifulSoup(pdf.html, "html.parser").select("script")
    encoded = make_document(
        "# Café".encode("latin-1"),
        "text/markdown; charset=iso-8859-1",
        "https://example.test/fr",
    )
    assert "Café" in encoded.html
    formula = make_document(
        '<article><p>Formula:</p><math display="block"><mfrac><mi>x</mi><mn>2</mn></mfrac></math></article>',
        "text/html",
        "https://example.test/math",
    )
    assert (
        BeautifulSoup(formula.html, "html.parser")
        .select_one("math mfrac mi")
        .get_text()
        == "x"
    )


def test_article_body_is_preferred_over_repository_and_site_navigation():
    document = make_document(
        "<main><h2>Latest commit</h2><p>"
        + "Repository history and file navigation. " * 30
        + '</p><article class="markdown-body"><h1>Project README</h1><p>Actual project documentation.</p></article></main>',
        "text/html",
        "https://github.com/example/project",
    )
    assert (
        "Project README" in document.html
        and "Actual project documentation" in document.html
    )
    assert (
        "Latest commit" not in document.html
        and "Repository history" not in document.html
    )


def test_disk_cache_lru_survives_restart_and_never_exceeds_budget(tmp_path):
    calls = []

    def download(url):
        calls.append(url)
        return PNG

    budget = len(PNG) * 2
    cache = DiskImageCache(tmp_path / "images", max_bytes=budget, downloader=download)
    for key in ("a", "b"):
        assert cache.get("https://example.test/" + key) == (PNG, "image/png")
    files = sorted(cache.root.glob("*.img"))
    for file in files:
        os.utime(file, (1, 1))
    cache.get("https://example.test/a")
    restarted = DiskImageCache(cache.root, max_bytes=budget, downloader=download)
    restarted.get("https://example.test/c")
    restarted.get("https://example.test/a")
    assert calls == [
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    restarted.get("https://example.test/b")
    assert calls[-1] == "https://example.test/b"
    assert sum(p.stat().st_size for p in cache.root.iterdir()) <= budget
    assert not list(cache.root.glob("*.part-*"))
    assert MAX_IMAGE_CACHE_BYTES == 500_000_000
    with pytest.raises(ValueError):
        DiskImageCache(tmp_path, max_bytes=MAX_IMAGE_CACHE_BYTES + 1)


def test_cache_coalesces_concurrent_misses_and_rejects_invalid_or_oversized_images(
    tmp_path,
):
    count, lock = [], threading.Lock()

    def download(url):
        with lock:
            count.append(url)
        time.sleep(0.02)
        return PNG

    cache = DiskImageCache(
        tmp_path / "images", max_bytes=len(PNG) * 2, downloader=download
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(cache.get, ["https://example.test/photo"] * 8))
    assert len(count) == 1 and all(data == PNG for data, _ in results)
    query = urlsplit(cache.url_for("https://example.test/photo")).query
    assert cache.get_signed(query)[0] == PNG
    with pytest.raises(ImageUnavailable):
        cache.get_signed(query.replace("photo", "other"))
    with pytest.raises(ImageUnavailable):
        cache.get_signed("url=http://localhost/&signature=x")
    cache.downloader = lambda url: b"<html>not an image</html>"
    with pytest.raises(ImageUnavailable):
        cache.get("https://example.test/error")
    cache.downloader = lambda url: PNG * 3
    with pytest.raises(ImageUnavailable):
        cache.get("https://example.test/large")
    assert len(list(cache.root.glob("*.img"))) == 1


def test_images_reject_private_targets_including_redirects(monkeypatch):
    requests = []

    def addresses(host, port, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1" if host == "localhost" else "8.8.8.8", port),
            )
        ]

    monkeypatch.setattr("socket.getaddrinfo", addresses)

    class Redirect:
        is_redirect = True
        headers = {"Location": "http://localhost/private"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def request(url, **kwargs):
        requests.append(url)
        return Redirect()

    monkeypatch.setattr("atlas.reader.images.requests.get", request)
    with pytest.raises(Exception, match="公网"):
        fetch_image("http://localhost/private")
    assert requests == []
    with pytest.raises(Exception, match="公网"):
        fetch_image("https://example.test/redirect")
    assert requests == ["https://example.test/redirect"]


def test_svg_images_keep_shapes_and_remove_executable_or_external_content():
    data = sanitize_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="bad()"><script>bad()</script><rect width="10" height="10"/><foreignObject><p>bad</p></foreignObject><image href="http://localhost/private"/><path style="fill:url(http://localhost/x)" d="M0 0"/></svg>'
    )
    assert b"rect" in data and b"path" in data
    assert (
        b"script" not in data
        and b"foreignObject" not in data
        and b"localhost" not in data
        and b"onload" not in data
    )
    with pytest.raises(ImageUnavailable):
        sanitize_svg(b'<!DOCTYPE svg [<!ENTITY x "abc">]><svg>&x;</svg>')


def test_full_translation_preserves_structure_code_images_and_cache(tmp_path):
    calls = []

    class Client:
        def translate(self, segments):
            calls.append(dict(segments))
            return {
                key: "中文译文：" + value + " <script>inert()</script>"
                for key, value in segments.items()
            }

    document = make_document(
        '<article><h2>Heading</h2><p>Actual original article text.</p><pre><code>print("Keep original code")</code></pre><figure><img src="/p.png"><figcaption>Figure caption</figcaption></figure></article>',
        "text/html",
        "https://example.test/post",
    )
    jobs = TranslationJobs(tmp_path, client_factory=Client)
    assert not tmp_path.exists() or not list(tmp_path.iterdir())
    first = jobs.start(document)
    result = finished(jobs, first["key"])
    assert result["status"] == "ready"
    soup = BeautifulSoup(result["html"], "html.parser")
    assert "中文译文" in soup.select_one("h2").get_text()
    assert soup.select_one("pre code").get_text() == 'print("Keep original code")'
    assert soup.select_one("img")["src"] == "https://example.test/p.png"
    assert not soup.select("script")
    jobs.close()
    restarted = TranslationJobs(
        tmp_path, client_factory=lambda: pytest.fail("重复翻译缓存")
    )
    assert restarted.start(document)["status"] == "ready"
    assert len(calls) == 1
    different = make_document("A different body.", "text/plain", document.source_url)
    assert restarted.key(different) != first["key"]
    with pytest.raises(ValueError):
        restarted.status("../escape")
    restarted.close()


def test_translation_failure_retries_only_unfinished_segments(tmp_path):
    calls, failed = [], []

    class Client:
        def translate(self, segments):
            calls.append(set(segments))
            if len(segments) > 100:
                raise RuntimeError("structured output budget exceeded")
            if len(calls) == 2 and not failed:
                failed.append(True)
                raise RuntimeError("temporary failure")
            return {key: "中文译文" for key in segments}

    document = make_document(
        "".join("<p>" + "Original sentence. " * 300 + "</p>" for _ in range(4))
        + "<table><tr>"
        + "".join(f"<td>Cell {index}</td>" for index in range(300))
        + "</tr></table>",
        "text/html",
        "https://example.test/long",
    )
    jobs = TranslationJobs(tmp_path, client_factory=Client)
    key = jobs.start(document)["key"]
    assert finished(jobs, key)["status"] == "error"
    first_batch = calls[0]
    jobs.start(document)
    assert finished(jobs, key)["status"] == "ready"
    assert all(not first_batch.intersection(batch) for batch in calls[1:])
    jobs.close()


def test_translation_client_rejects_missing_or_wrong_paragraphs(monkeypatch):
    config = SimpleNamespace(
        base_url="https://model.test/v1",
        api_key="test",
        model="model",
        timeout_seconds=5,
    )
    items = [{"id": "1", "text": "完整的中文译文。"}]

    def post(*args, **kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": __import__("json").dumps({"translations": items})
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr("atlas.reader.translations.requests.post", post)
    client = TranslationClient(config)
    assert client.translate({"1": "Original text"}) == {"1": "完整的中文译文。"}
    items[0]["id"] = "wrong"
    with pytest.raises(ValueError):
        client.translate({"1": "Original text"})
