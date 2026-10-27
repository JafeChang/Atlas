"""真实 HTTP / Chromium：卡片阅读、格式、翻译、图片缓存与详情页打标。"""

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from atlas.archive import ArticleContentStore, open_archive
from atlas.labels import open_store
from atlas.reader.images import DiskImageCache, ImageUnavailable
from atlas.webapp import build_webapp
from tests.test_reader import PNG
from tests.test_webui_card_updates import _json_label
from tests.test_webui_entries_render import PAYLOADS, _get, _NoRedirect, _records

HTML = (
    '<html lang="en"><head><title>Example</title></head><body><nav>MENU</nav><article>'
    '<h2 id="chapter">Original heading</h2><p><strong>Original bold text</strong> with meaningful detail.</p>'
    "<p>This paragraph explains the complete method and its results.</p>"
    '<figure><img src="/photo.png" alt="Example figure" onerror="window.bad=true"><figcaption>Original caption</figcaption></figure>'
    '<img src="/missing.png" alt="Unavailable example">'
    '<pre><code class="language-python">print("original code")</code></pre>'
    "<script>window.bad=true</script></article></body></html>"
)
MARKDOWN = '# Markdown heading\n\nA **formatted** paragraph.\n\n| First | Second |\n|---|---|\n| A | B |\n\n```python\nprint("markdown code")\n```\n\n![Markdown picture](/photo.png)'


@contextmanager
def article_world(tmp_path, *, chinese=False):
    root = tmp_path / "store"
    with open_archive(root) as archive:
        for record in _records():
            archive.put(record, PAYLOADS[record.raw_id].encode())
    db_path = root / "atlas.db"
    calls, images = [], []

    class Client:
        def translate(self, segments):
            calls.append(dict(segments))
            return {key: "中文译文：" + value for key, value in segments.items()}

    def download(url):
        images.append(url)
        if url.endswith("missing.png"):
            raise ImageUnavailable("fixture image unavailable")
        return PNG

    cache_root = tmp_path / "cache"
    cache = DiskImageCache(cache_root / "images", downloader=download)
    app = build_webapp(
        root,
        require_login=False,
        db_path=db_path,
        industry_provider=lambda: ("ai", "web", "news"),
        industry_of={"chan-feed": "ai", "chan-article": "web"},
        actor="tester",
        cache_root=cache_root,
        image_cache=cache,
        translation_client_factory=Client,
    )
    with ArticleContentStoreContext(db_path) as store:
        store.record(
            source_url="https://example.test/alpha?a=1&b=2",
            status="ready",
            content_kind="article",
            cleaned_text="这是中文的完整文章正文，包含方法介绍和研究结果。"
            if chinese
            else "Original complete article text.",
            raw_content=(
                "<article><h2>中文标题</h2><p>这是中文的完整文章正文，包含方法介绍和研究结果。</p></article>"
                if chinese
                else HTML
            ).encode(),
            content_type="text/html",
            cleaner_version="fixture",
        )
        store.record(
            source_url="https://example.test/gamma",
            status="ready",
            content_kind="article",
            cleaned_text="Markdown article text.",
            raw_content=MARKDOWN.encode(),
            content_type="text/markdown",
            cleaner_version="fixture",
        )
    try:
        yield app, root, db_path, cache_root, calls, images
    finally:
        app.close()


@contextmanager
def ArticleContentStoreContext(path):
    store = ArticleContentStore(path)
    try:
        yield store
    finally:
        store.close()


def article_link(app, title="Alpha"):
    status, page, _ = _get(app, "/feed?order=asc&limit=50")
    assert status == 200
    soup = BeautifulSoup(page, "html.parser")
    return next(a["href"] for a in soup.select("a.title") if title in a.get_text())


def request(app, path, *, method="GET", headers=None):
    req = urllib.request.Request(
        app.base_url + path, method=method, headers=headers or {}
    )
    try:
        response = urllib.request.build_opener(_NoRedirect).open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read(), response.headers


def test_detail_contains_article_fields_and_rich_body_without_writes_or_translation(
    tmp_path,
):
    with article_world(tmp_path) as (app, root, db_path, cache, calls, images):
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        link = article_link(app)
        assert link.startswith("/article?")
        status, page, _ = _get(app, link)
        soup = BeautifulSoup(page, "html.parser")
        assert status == 200
        assert "Alpha & Beta" in soup.select_one(".article-title").get_text()
        assert "chan-feed" in soup.select_one(".meta").get_text()
        assert soup.select_one("time") and soup.select_one(".tag-row")
        assert soup.select_one(".article-intro") and soup.select_one(".record-details")
        assert soup.select_one(".article-body h2").get_text() == "Original heading"
        assert (
            soup.select_one(".article-body strong").get_text() == "Original bold text"
        )
        assert (
            soup.select_one(".article-body pre code").get_text()
            == 'print("original code")'
        )
        assert not soup.select(
            ".article-body script,.article-body [onerror],.article-body nav"
        )
        assert all(
            img["src"].startswith("/image?") for img in soup.select(".article-body img")
        )
        assert [button.get_text() for button in soup.select('[role="tab"]')] == [
            "原文",
            "中文",
        ]
        assert len(soup.select(".vote-button[aria-pressed=false]")) == 2
        assert calls == images == [] and not cache.exists()
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_markdown_and_native_chinese_body_have_expected_presentation(tmp_path):
    with article_world(tmp_path, chinese=True) as (app, *_):
        status, page, _ = _get(app, article_link(app))
        assert status == 200
        soup = BeautifulSoup(page, "html.parser")
        assert not soup.select('[role="tab"]')
        assert "这是中文的完整文章正文" in soup.select_one(".article-body").get_text()
        _, page, _ = _get(app, article_link(app, "Gamma"))
        soup = BeautifulSoup(page, "html.parser")
        assert soup.select_one(".article-body h1").get_text() == "Markdown heading"
        assert soup.select_one(".article-body table td").get_text() == "A"
        assert soup.select_one(".article-body strong").get_text() == "formatted"


def test_http_images_use_cache_only_and_missing_image_does_not_store_response(tmp_path):
    with article_world(tmp_path) as (app, root, db_path, cache, calls, images):
        _, page, _ = _get(app, article_link(app))
        src = BeautifulSoup(page, "html.parser").select_one(".article-body img")["src"]
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        for _ in range(2):
            status, data, headers = request(app, src)
            assert status == 200 and data == PNG
            assert headers["Content-Type"] == "image/png"
            assert headers["X-Content-Type-Options"] == "nosniff"
        assert len(images) == 1
        assert len(list((cache / "images").glob("*.img"))) == 1
        status, _, _ = request(app, src.replace("photo.png", "other.png"))
        assert status != 200 and len(images) == 1
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
        assert not list(root.rglob("*.img"))


def test_detail_label_round_trip_keeps_article_location_and_stored_anchor(tmp_path):
    with article_world(tmp_path) as (app, root, db_path, *_):
        link = article_link(app)
        _, page, _ = _get(app, link)
        form = BeautifulSoup(page, "html.parser").select_one(".act-valid")
        fields = {node["name"]: node["value"] for node in form.select("input")}
        assert fields["return_to"].startswith("/article?")
        assert _json_label(app, fields)[1]["values"]["valid"] == "valid"
        req = urllib.request.Request(
            app.base_url + "/label",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            response = urllib.request.build_opener(_NoRedirect).open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            assert (
                response.status == 303
                and response.headers["Location"] == fields["return_to"]
            )
        with open_store(db_path) as store:
            (saved,) = store.all_for(fields["raw_id"])
            assert saved.anchor.char_start == int(fields["char_start"])
        _, page, _ = _get(app, link)
        assert BeautifulSoup(page, "html.parser").select_one(
            '.act-valid .vote-button[aria-pressed="true"]'
        )


def test_translation_http_is_explicit_async_and_reuses_full_result(tmp_path):
    with article_world(tmp_path) as (app, root, db_path, cache, calls, images):
        link = article_link(app)
        target = link.replace("/article?", "/translate?", 1)
        assert request(app, target)[0] == 405 and not calls
        assert (
            request(
                app, target, method="POST", headers={"Origin": "https://unrelated.test"}
            )[0]
            == 403
        )
        assert not calls
        status, data, _ = request(app, target, method="POST")
        assert status in (200, 202)
        result = json.loads(data)
        deadline = time.monotonic() + 5
        while result["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
            result = json.loads(request(app, "/translation?key=" + result["key"])[1])
        assert result["status"] == "ready"
        soup = BeautifulSoup(result["html"], "html.parser")
        assert "中文译文" in soup.select_one("h2").get_text()
        assert soup.select_one("pre code").get_text() == 'print("original code")'
        assert soup.select_one("img")["src"].startswith("/image?")
        assert soup.select_one("h2")["id"].startswith("atlas-zh-")
        count = len(calls)
        assert json.loads(request(app, target, method="POST")[1])["status"] == "ready"
        assert len(calls) == count == 1
        assert request(app, "/translation?key=../../escape")[0] == 400
        assert request(app, "/article?raw_id=ghost&char_start=0&char_end=1")[0] == 404


@pytest.mark.parametrize("decision", ["pending", "rejected"])
def test_translation_respects_existing_admission_gate(tmp_path, monkeypatch, decision):
    with article_world(tmp_path) as (app, root, db_path, cache, calls, images):
        target = article_link(app).replace("/article?", "/translate?", 1)
        original = app.server.entry_projection
        with monkeypatch.context() as patch:
            patch.setattr(
                app.server,
                "entry_projection",
                lambda entries, session: tuple(
                    replace(entry, admission_status=decision)
                    for entry in original(entries, session)
                ),
            )
            status, data, _ = request(app, target, method="POST")
            assert status == 409 and "入站审核" in json.loads(data)["message"]
            assert calls == []
        assert request(app, target, method="POST")[0] in (200, 202)


@pytest.mark.parametrize(
    "width,height,scheme",
    [(375, 812, "light"), (812, 375, "dark"), (1280, 900, "dark")],
)
def test_browser_card_detail_tabs_images_and_label_actions(
    tmp_path, width, height, scheme
):
    api = pytest.importorskip("playwright.sync_api")
    with (
        article_world(tmp_path) as (app, root, db_path, cache, calls, images),
        api.sync_playwright() as p,
    ):
        try:
            browser = p.chromium.launch(args=["--no-sandbox"])
        except api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("需要 playwright install chromium")
            raise
        try:
            page = browser.new_page(
                viewport={"width": width, "height": height},
                color_scheme=scheme,
                reduced_motion="reduce",
            )
            page.goto(app.base_url + "/feed?order=asc&limit=50")
            card = page.locator("article.item").filter(has_text="Alpha & Beta")
            card.click(position={"x": 7, "y": 7})
            page.wait_for_url("**/article?**")
            assert page.get_by_role("heading", name="正文", exact=True).count() == 1
            assert (
                page.locator(".article-body strong").inner_text()
                == "Original bold text"
            )
            assert not calls and page.evaluate("window.bad !== true")
            photo = page.locator('.article-body img[alt="Example figure"]')
            photo.scroll_into_view_if_needed()
            page.wait_for_function(
                "document.querySelector('.article-body img').complete"
            )
            assert photo.evaluate("el => el.naturalWidth > 0")
            page.locator(".image-unavailable").wait_for()
            page.get_by_role("tab", name="中文", exact=True).click()
            page.locator("#body-chinese .article-body h2").filter(
                has_text="中文译文"
            ).wait_for()
            assert (
                page.locator("#body-chinese pre code").inner_text()
                == 'print("original code")'
            )
            assert len(calls) == 1
            page.get_by_role("tab", name="中文", exact=True).focus()
            page.keyboard.press("ArrowLeft")
            assert (
                page.get_by_role("tab", name="原文", exact=True).get_attribute(
                    "aria-selected"
                )
                == "true"
            )
            page.keyboard.press("ArrowRight")
            assert (
                page.get_by_role("tab", name="中文", exact=True).get_attribute(
                    "aria-selected"
                )
                == "true"
            )
            assert len(calls) == 1
            navigations = []
            page.on("framenavigated", lambda frame: navigations.append(frame.url))
            page.get_by_role("button", name="标记有效", exact=True).click()
            page.locator('.act-valid .vote-button[aria-pressed="true"]').wait_for()
            assert (
                not navigations
                and "valid=" not in page.locator(".article-detail").inner_text()
            )
            page.reload()
            page.get_by_role("tab", name="中文", exact=True).click()
            page.locator("#body-chinese .article-body h2").wait_for()
            assert len(calls) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert (
                page.locator(".act-valid .vote-button").get_attribute("aria-pressed")
                == "true"
            )
        finally:
            browser.close()
