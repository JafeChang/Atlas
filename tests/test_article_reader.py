import pytest

from atlas.collect.article import (
    ArticleReader,
    ArticleReadError,
    extract_article,
    public_url,
)


def test_extract_article_prefers_main_content_and_removes_chrome():
    html = b"<html><nav>Navigation</nav><article><h1>Title</h1><p>Real article body.</p><script>doEvil()</script></article><footer>Subscribe</footer></html>"
    assert extract_article(html, "text/html") == "Title Real article body."
    with pytest.raises(ArticleReadError, match="验证"):
        extract_article(
            b"<title>Just a moment...</title><p>Checking your browser</p>", "text/html"
        )


def test_public_url_rejects_private_destinations_with_public_control(monkeypatch):
    monkeypatch.setattr(
        "atlas.collect.article.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    public_url("https://example.test/article")
    monkeypatch.setattr(
        "atlas.collect.article.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ArticleReadError, match="公网"):
        public_url("https://example.test/article")


def test_original_reader_uses_body_and_does_not_summarize_http_errors(monkeypatch):
    reader = ArticleReader()
    monkeypatch.setattr(
        reader,
        "_get",
        lambda *a, **k: (
            200,
            b"<article>" + b"Actual body text. " * 20 + b"</article>",
            "text/html",
            "https://example.test/final",
        ),
    )
    result = reader.read("https://example.test/article")
    assert "Actual body text" in result.text and result.url.endswith("/final")
    monkeypatch.setattr(
        reader,
        "_get",
        lambda *a, **k: (403, b"Forbidden", "text/html", "https://example.test/"),
    )
    with pytest.raises(ArticleReadError, match="403"):
        reader.read("https://example.test/article")


def test_redirect_destination_is_checked_before_following(monkeypatch):
    from types import SimpleNamespace

    checked, network = [], []
    monkeypatch.setattr(
        "atlas.collect.article.public_url", lambda url: checked.append(url)
    )

    class Response:
        def __init__(self, status, headers, body=b""):
            self.status_code, self.headers, self.body = status, headers, body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield self.body

    def get(url, **kwargs):
        network.append(url)
        assert not kwargs["allow_redirects"]
        if url.endswith("/start"):
            return Response(302, {"Location": "https://other.test/end"})
        return Response(
            200,
            {"Content-Type": "text/html"},
            b"<article>" + b"Actual body text. " * 20 + b"</article>",
        )

    monkeypatch.setattr("atlas.collect.article.requests.get", get)
    reader = ArticleReader()
    monkeypatch.setattr(
        reader.robots, "check", lambda url: SimpleNamespace(allowed=True)
    )
    assert "Actual body text" in reader.read("https://example.test/start").text
    assert network == ["https://example.test/start", "https://other.test/end"]
    network.clear()

    def policy(url):
        return SimpleNamespace(
            allowed=url.endswith("/start"),
            outcome=SimpleNamespace(value="disallowed_by_rules"),
        )

    monkeypatch.setattr(reader.robots, "check", policy)
    with pytest.raises(ArticleReadError, match="robots"):
        reader.read("https://example.test/start")
    assert network == ["https://example.test/start"]
    assert checked[-1] == "https://other.test/end"


def test_pdf_text_is_extracted_without_html_parsing():
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 30 700 Td (Actual text from the original PDF article.) Tj ET"
    )
    page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    assert "Actual text from the original PDF article." in extract_article(
        output.getvalue(), "application/pdf"
    )


def test_dns_ipv4_precedes_unusable_ipv6_and_ipv6_only_hosts_still_work(monkeypatch):
    import socket

    families = []

    def resolve(host, port, **kwargs):
        families.append(kwargs["family"])
        if kwargs["family"] == socket.AF_INET:
            return [(2, 1, 6, "", ("93.184.216.34", 443))]
        return [(10, 1, 6, "", ("2001::1", 443, 0, 0))]

    monkeypatch.setattr("atlas.collect.article.socket.getaddrinfo", resolve)
    public_url("https://example.test/article")
    assert families == [socket.AF_INET]

    def ipv6_only(host, port, **kwargs):
        if kwargs["family"] == socket.AF_INET:
            raise socket.gaierror("No IPv4")
        return [(10, 1, 6, "", ("2606:4700:4700::1111", 443, 0, 0))]

    monkeypatch.setattr("atlas.collect.article.socket.getaddrinfo", ipv6_only)
    public_url("https://example.test/article")


def test_navigation_or_javascript_shell_is_not_original_article_content():
    shell = b"<html><title>Video service</title><div>About Press Copyright Contact us Creators Advertise Developers Terms Privacy Policy and Safety How the service works Test new features</div><script>loadVideo()</script></html>"
    assert extract_article(shell, "text/html") == ""
    assert extract_article(
        b"This is a plain text article. " * 10, "text/plain"
    ).startswith("This is a plain text article.")


def test_related_card_before_real_body_does_not_replace_the_article():
    html = (
        b'<html><article class="related-posts"><p>Unrelated news from September.</p></article><div class="entry-content"><h1>How to grow cacao</h1><p>Keep cacao warm and humid, and protect the roots. '
        + b"Actual growing guidance. " * 10
        + b"</p></div></html>"
    )
    text = extract_article(html, "text/html")
    assert "Actual growing guidance" in text and "Unrelated news" not in text
    linked = b'<article><a href="/month">Postings from September 2026</a> | <a href="/year">Browse yearly postings</a></article>'
    assert extract_article(linked, "text/html") == ""
    wordpress = (
        b'<div id="main-content"><h1>Growing cacao</h1><p>'
        + b"Real growing instructions. " * 30
        + b"</p></div><article><p>Related press release.</p></article>"
    )
    assert "Real growing instructions." in extract_article(wordpress, "text/html")
    assert (
        extract_article(
            b'<p><a href="/month">Monthly postings</a> | <a href="/year">Browse yearly modules</a></p>',
            "text/html",
        )
        == ""
    )
