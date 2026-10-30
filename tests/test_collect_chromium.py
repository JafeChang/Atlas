"""真实浏览器读取本地 Mock HTTP；不依赖外部站点或登录状态。"""

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.aluminium.processing import process_archive
from atlas.aluminium.sources import configure_rss_source, probe_sources
from atlas.aluminium.store import AluminiumStore
from atlas.aluminium.ui import SourceManagement
from atlas.archive import open_archive
from atlas.collect.article import ArticleReader
from atlas.collect.chromium import BrowserRoutedFetcher, ChromiumFetcher, browser_routes
from atlas.collect.fetch import FetchRequest, FetchResult, FetchTransportError
from atlas.collect.network_policy import SharedNetworkPolicy
from atlas.compose.pipeline import Pipeline, PipelineConfig
from atlas.compose.tasks import ComposeDependencies
from atlas.entries import parse_entries
from tests.test_aluminium import HTML, RSS, event
from tests.test_aluminium_rss_connections import setup_directory, version


@pytest.fixture(scope="module")
def agent():
    playwright = pytest.importorskip("playwright.sync_api")
    if not shutil.which("Xvfb"):
        pytest.skip("安装 Xvfb 后可运行浏览器传输测试")
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():
            pytest.skip("运行 playwright install chromium 后可运行浏览器传输测试")
    return ChromiumFetcher().user_agent()


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("User-Agent")))
            status, body, mime = 200, HTML, "text/html"
            if self.path == "/robots.txt":
                body, mime = (
                    b"User-agent: *\nDisallow: /forbidden\nAllow: /\n",
                    "text/plain",
                )
            elif self.path == "/feed":
                body, mime = RSS, "application/rss+xml"
            elif self.path == "/redirect":
                status, body = 302, b""
            elif self.path == "/failure":
                status, body = 503, b"Unavailable"
            elif self.path == "/resources":
                body = b'<html><body><script>fetch("/script")</script><img src="/image"></body></html>'
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            if status == 302:
                self.send_header("Location", "/forbidden")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_router_preserves_normal_fetcher_and_requires_registered_identity():
    calls = []

    def normal(req):
        calls.append("http")
        return FetchResult(req.url, 200, RSS)

    def browser(req):
        calls.append("browser")
        return FetchResult(req.url, 200, RSS)

    fetcher = BrowserRoutedFetcher(
        normal, {"browser.test": "actual browser"}, browser=browser
    )
    assert fetcher(FetchRequest("https://normal.test/", "Atlas")).content == RSS
    assert (
        fetcher(FetchRequest("https://browser.test/", "actual browser")).content == RSS
    )
    with pytest.raises(FetchTransportError, match="身份"):
        fetcher(FetchRequest("https://browser.test/", "Atlas"))
    assert calls == ["http", "browser"]


def test_native_browser_feed_and_cleanup(site, agent, monkeypatch):
    import atlas.collect.chromium as chromium

    processes = []
    popen = chromium.subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(chromium.subprocess, "Popen", launch)
    base, seen = site
    response = ChromiumFetcher()(FetchRequest(base + "/feed", agent))
    assert response.status_code == 200 and response.content == RSS
    assert len(parse_entries(response.content, "", raw_id="mock").entries) == 1
    assert seen == [("/feed", agent)]
    assert processes and all(process.poll() is not None for process in processes)


def test_browser_does_not_execute_scripts_or_load_subresources(site, agent):
    base, seen = site
    assert (
        ChromiumFetcher()(FetchRequest(base + "/resources", agent)).status_code == 200
    )
    assert seen == [("/resources", agent)]


def test_browser_returns_redirect_without_following_it(site, agent):
    base, seen = site
    result = ChromiumFetcher()(FetchRequest(base + "/redirect", agent))
    assert result.status_code == 302 and result.headers["location"] == "/forbidden"
    assert result.content == b"" and seen == [("/redirect", agent)]


def test_browser_preserves_http_failure(site, agent):
    base, seen = site
    result = ChromiumFetcher()(FetchRequest(base + "/failure", agent))
    assert result.status_code == 503 and result.content == b"Unavailable"
    assert seen == [("/failure", agent)]


def test_browser_rejects_stale_identity_before_http_request(site, agent):
    base, seen = site
    with pytest.raises(FetchTransportError, match="身份已变化"):
        ChromiumFetcher()(FetchRequest(base + "/feed", "old browser"))
    assert seen == []


def test_missing_browser_is_explicit_failure(site):
    base, seen = site
    with pytest.raises(FetchTransportError, match="不存在"):
        ChromiumFetcher(executable="/missing/chromium")(
            FetchRequest(base + "/feed", "Atlas")
        )
    assert seen == []


def test_browser_feed_and_article_use_existing_pipeline(
    tmp_path, site, agent, monkeypatch
):
    base, seen = site
    db, _, sid = setup_directory(tmp_path)
    monkeypatch.setattr(ChromiumFetcher, "user_agent", lambda self: agent)
    configured = configure_rss_source(db, sid, base + "/feed", browser=True)
    assert not configured["enabled"]
    assert browser_routes(tmp_path) == {"127.0.0.1": agent}
    # 普通客户端的旧成功证据不能启用浏览器配置。
    with AluminiumStore(db) as store:
        store.probe(
            sid, {"collectible": True, "reason": None, "endpoint": base + "/feed"}
        )
    with pytest.raises(ValueError, match="验证"):
        SourceManagement(db, lock_path=tmp_path / "lock").toggle(
            sid, "1", version(db), "tester"
        )
    deps = ComposeDependencies.real(tmp_path)
    probe = probe_sources(db, deps, source_ids=[sid], enable_verified=True)[0]
    assert (
        probe["collectible"]
        and probe["transport"] == "chromium"
        and probe["user_agent"] == agent
    )
    assert configure_rss_source(db, sid, base + "/feed", browser=True)["enabled"]
    with Pipeline(
        PipelineConfig(store_root=tmp_path, actor="tester"), dependencies=deps
    ) as pipeline:
        pipeline.run(targets=("archive",))
    with open_archive(tmp_path) as archive:
        assert len(archive.all_raw_ids()) == 1

    class Model:
        config = SimpleNamespace(model="mock")

        def extract(self, title, body):
            return [event()]

    assert process_archive(tmp_path, extract=True, client=Model())["extracted"] == 1
    # 正文补抓复用相同实际身份、robots 及站点限速。
    monkeypatch.setattr("atlas.collect.article.public_url", lambda url: None)
    article = ArticleReader(shared=SharedNetworkPolicy(tmp_path)).read(base + "/news")
    assert "500 ktpa" in article.text
    assert all(user_agent == agent for _, user_agent in seen)


def test_browser_redirect_still_checks_destination_robots(
    tmp_path, site, agent, monkeypatch
):
    base, seen = site
    db, _, sid = setup_directory(tmp_path)
    monkeypatch.setattr(ChromiumFetcher, "user_agent", lambda self: agent)
    configure_rss_source(db, sid, base + "/redirect", browser=True)
    result = probe_sources(
        db, ComposeDependencies.real(tmp_path), source_ids=[sid], enable_verified=True
    )[0]
    assert not result["collectible"]
    assert result["connection_error"]["kind"] == "robots_denied"
    assert "/forbidden" not in [path for path, _ in seen]
