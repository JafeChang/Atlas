import multiprocessing

import pytest

from atlas.collect.article import ArticleReader, ArticleReadError
from atlas.collect.executor import Collector, ResultStatus, RetryPolicy
from atlas.collect.fetch import FetchResult
from atlas.collect.network_policy import ROBOTS_TTL, PolicyFetcher, SharedNetworkPolicy
from atlas.collect.robots import FetcherRobotsAdapter, RobotsCache, RobotsFetchResult
from atlas.collect.throttle import SharedDomainThrottle, ThrottleTimeout
from tests.test_collect_executor import make_channel


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def test_article_resumes_after_crawl_delay_across_reader_restarts(
    tmp_path, monkeypatch
):
    clock = Clock()
    requests = []
    monkeypatch.setattr("atlas.collect.article.public_url", lambda url: None)

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield (
                b"User-agent: *\nCrawl-delay: 600\n"
                if self.url.endswith("/robots.txt")
                else b"<article>" + b"Readable article content. " * 15 + b"</article>"
            )

    def get(url, **kwargs):
        requests.append((url, clock.now))
        return Response(url)

    monkeypatch.setattr("atlas.collect.article.requests.get", get)
    url = "https://slow.test/article"

    def reader():
        return ArticleReader(
            shared=SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep)
        )

    with pytest.raises(ArticleReadError) as exc:
        reader().read(url)
    assert exc.value.failure.code == "site_deferred"
    assert requests == [("https://slow.test/robots.txt", 1000)]
    clock.now += 600
    assert "Readable article" in reader().read(url).text
    assert requests == [("https://slow.test/robots.txt", 1000), (url, 1600)]
    with pytest.raises(ArticleReadError):
        reader().read(url)
    clock.now += 600
    assert "Readable article" in reader().read(url).text
    assert requests[-1] == (url, 2200)


def test_feed_collector_shares_robots_delay_and_resumes(tmp_path):
    clock = Clock()
    policy = SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep)
    seen = []
    url = "https://feed.test/feed.xml"

    def fetch(request):
        seen.append((request.url, clock.now))
        body = (
            b"User-agent: *\nCrawl-delay: 600\n"
            if request.url.endswith("robots.txt")
            else b"<rss/>"
        )
        return FetchResult(url=request.url, status_code=200, content=body)

    def collector():
        adapter = FetcherRobotsAdapter(fetch)

        def robots(url, *, user_agent):
            def guarded(target, *, user_agent):
                with policy.slot(target, robots_request=True):
                    return adapter(target, user_agent=user_agent)

            return policy.robots(url, user_agent, guarded)

        return Collector(
            fetcher=fetch,
            robots=RobotsCache(robots),
            throttle=SharedDomainThrottle(policy),
            retry=RetryPolicy(max_attempts=1),
        )

    channel = make_channel("feed", endpoint=url)
    first = collector().collect(channel)
    assert first.status is ResultStatus.FAILED
    assert [u for u, _ in seen] == ["https://feed.test/robots.txt"]
    clock.now += 600
    assert collector().collect(channel).status is ResultStatus.COLLECTED
    assert seen[-1] == (url, 1600)
    # A separate task observes the same last request.
    with pytest.raises(ThrottleTimeout):
        with SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep).slot(url):
            pass


def _hold_request(root, ready, release):
    with SharedNetworkPolicy(root).slot("https://shared.test/a"):
        ready.set()
        release.wait(5)


def test_processes_cannot_request_same_host_concurrently(tmp_path):
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_request, args=(tmp_path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        policy = SharedNetworkPolicy(tmp_path, max_wait=0)
        with pytest.raises(ThrottleTimeout):
            with policy.slot("https://shared.test/b"):
                pytest.fail("同域请求不得同时开始")
        with policy.slot("https://other.test/b"):
            pass
    finally:
        release.set()
        process.join(5)
    assert process.exitcode == 0


def test_channel_interval_applies_to_robots_and_other_tasks(tmp_path):
    clock = Clock()
    policy = SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep)
    url = "https://shared.test/robots.txt"
    policy.set_rate(url, "feed", 600)
    with policy.slot(url, robots_request=True):
        pass
    with pytest.raises(ThrottleTimeout):
        with SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep).slot(
            "https://shared.test/article"
        ):
            pytest.fail("跨任务不得提前请求")
    policy.set_rate(url, "feed", 0)
    with policy.slot("https://shared.test/article", interval=0):
        pass


def test_robots_rate_limit_does_not_grant_permission():
    cache = RobotsCache(lambda url, *, user_agent: RobotsFetchResult(url, 429, b""))
    assert not cache.check("https://example.test/feed").allowed


def test_robots_cache_expires_and_refreshes_policy(tmp_path):
    clock = Clock()
    policy = SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep, max_wait=0)
    calls = []

    def fetch(url, *, user_agent):
        calls.append(clock.now)
        return RobotsFetchResult(
            url,
            200,
            f"User-agent: *\nCrawl-delay: {600 if len(calls) == 1 else 60}\n".encode(),
        )

    url = "https://slow.test/robots.txt"
    policy.robots(url, "Atlas", fetch)
    policy.robots(url, "Atlas", fetch)
    assert len(calls) == 1
    clock.now += ROBOTS_TTL + 1
    policy.robots(url, "Atlas", fetch)
    with policy.slot("https://slow.test/article"):
        pass
    assert policy.ready_at(url) == clock.now + 60


def test_feed_redirect_checks_and_obeys_destination_robots(tmp_path):
    clock = Clock()
    policy = SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep)
    seen = []

    def raw(request):
        seen.append((request.url, clock.now))
        if request.url.endswith("robots.txt"):
            body = (
                b"User-agent: *\nCrawl-delay: 600\n"
                if "target.test" in request.url
                else b"User-agent: *\nAllow: /\n"
            )
            return FetchResult(url=request.url, status_code=200, content=body)
        if request.url == "https://origin.test/feed":
            return FetchResult(
                url=request.url,
                status_code=302,
                content=b"",
                headers={"location": "https://target.test/feed"},
            )
        return FetchResult(url=request.url, status_code=200, content=b"<rss/>")

    adapter = FetcherRobotsAdapter(raw)

    def robots_fetch(url, *, user_agent):
        def guarded(target, *, user_agent):
            with policy.slot(target, robots_request=True):
                return adapter(target, user_agent=user_agent)

        return policy.robots(url, user_agent, guarded)

    robots = RobotsCache(robots_fetch)
    collector = Collector(
        fetcher=PolicyFetcher(raw, robots, policy),
        robots=robots,
        throttle=SharedDomainThrottle(policy),
        retry=RetryPolicy(max_attempts=1),
    )
    channel = make_channel("feed", endpoint="https://origin.test/feed")
    first = collector.collect(channel)
    assert first.status is ResultStatus.FAILED
    assert first.failure.kind.value == "rate_limit_wait_timeout"
    assert policy.retry_snapshot(channel.endpoint)["until"] == policy.ready_at(
        "https://target.test/feed"
    )
    assert [url for url, _ in seen] == [
        "https://origin.test/robots.txt",
        "https://origin.test/feed",
        "https://target.test/robots.txt",
    ]
    clock.now = policy.ready_at("https://target.test/feed")
    assert collector.collect(channel).status is ResultStatus.COLLECTED
    assert seen[-1] == ("https://target.test/feed", clock.now)


def test_real_collector_follows_redirect_through_policy(tmp_path, monkeypatch):
    import http.server
    import threading

    from atlas.compose.tasks import ComposeDependencies

    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/robots.txt":
                self.send_response(404)
                self.end_headers()
            elif self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/feed")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<rss/>")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        deps = ComposeDependencies.real(tmp_path)
        collector = Collector(
            fetcher=deps.fetcher,
            robots=deps.robots,
            throttle=deps.throttle,
            retry=RetryPolicy(max_attempts=1),
        )
        assert (
            collector.collect(make_channel("redirect", endpoint=url)).status
            is ResultStatus.COLLECTED
        )
        assert seen == ["/robots.txt", "/start", "/feed"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_article_redirect_defers_original_url_until_target_ready(tmp_path, monkeypatch):
    clock = Clock()
    policy = SharedNetworkPolicy(tmp_path, clock=clock, sleeper=clock.sleep)
    monkeypatch.setattr("atlas.collect.article.public_url", lambda url: None)
    seen = []

    class Response:
        def __init__(self, url):
            self.status_code = 302 if url == "https://origin.test/article" else 200
            self.headers = (
                {"Location": "https://target.test/article"}
                if self.status_code == 302
                else {"Content-Type": "text/html"}
            )
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            if self.url.endswith("robots.txt"):
                yield (
                    b"User-agent: *\nCrawl-delay: 600\n"
                    if "target.test" in self.url
                    else b"User-agent: *\nAllow: /\n"
                )
            else:
                yield b"<article>" + b"Readable article content. " * 15 + b"</article>"

    def get(url, **kwargs):
        seen.append(url)
        return Response(url)

    monkeypatch.setattr("atlas.collect.article.requests.get", get)
    url = "https://origin.test/article"
    with pytest.raises(ArticleReadError) as error:
        ArticleReader(shared=policy).read(url)
    assert error.value.failure.code == "site_deferred"
    assert policy.ready_at(url) == policy.ready_at("https://target.test/article")
    assert "https://target.test/article" not in seen
    clock.now = policy.ready_at(url)
    assert "Readable article" in ArticleReader(shared=policy).read(url).text


def test_admission_retries_deferred_article_when_site_is_ready(tmp_path, monkeypatch):
    from atlas.admission_cli import main
    from atlas.archive import open_archive
    from atlas.cognition.admission import read_admissions
    from atlas.contracts import RawRecord
    from tests.test_admission import fake_reviews, source_of
    from tests.test_feed_refresh import put

    root = tmp_path / "store"
    payload = b"<rss><channel><item><title>Slow article</title><link>https://slow.test/article</link></item></channel></rss>"
    record = RawRecord.create(
        channel_id="feed",
        endpoint="https://example.test/feed",
        content=payload,
        http_status=200,
    )
    with open_archive(root) as archive:
        archive.put(record, payload)
    source = source_of(root, record)
    clock = Clock()
    monkeypatch.setattr(
        "atlas.admission_cli.SharedNetworkPolicy",
        lambda root: SharedNetworkPolicy(root, clock=clock, sleeper=clock.sleep),
    )
    monkeypatch.setattr("atlas.admission_cli.time.time", clock)
    monkeypatch.setattr("atlas.collect.article.public_url", lambda url: None)
    fetched = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}

        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield (
                b"User-agent: *\nCrawl-delay: 600\n"
                if self.url.endswith("robots.txt")
                else b"<article>" + b"Meaningful article text. " * 20 + b"</article>"
            )

    def get(url, **kwargs):
        fetched.append(url)
        return Response(url)

    monkeypatch.setattr("atlas.collect.article.requests.get", get)
    fake_reviews(monkeypatch, [])
    args = [
        "--store-root",
        str(root),
        "--enable",
        "--report-dir",
        str(tmp_path / "report"),
    ]
    assert main(args) == 0
    assert (
        read_admissions(root / "atlas.db").reviews[source.key].reason_code
        == "source_unavailable"
    )
    assert main(args) == 0 and len(fetched) == 1
    clock.now += 600
    newer = source_of(root, put(root, "New article", "https://new.test/article"))
    assert main([*args, "--limit", "1"]) == 0
    assert read_admissions(root / "atlas.db").allows(source.key)
    assert newer.key not in read_admissions(root / "atlas.db").reviews
    assert fetched == ["https://slow.test/robots.txt", "https://slow.test/article"]


def test_partial_feed_run_retries_deferred_site_in_same_window(tmp_path):
    from atlas.archive import open_archive
    from atlas.compose import ComposeDependencies, build_pipeline
    from tests.test_compose_pipeline import _register_channel

    root = tmp_path / "store"
    fast = "https://fast.test/feed"
    slow = "https://slow.test/feed"
    _register_channel(root, industry_id="ai", channel_id="fast", endpoint=fast)
    _register_channel(root, industry_id="ai", channel_id="slow", endpoint=slow)
    clock = Clock()
    policy = SharedNetworkPolicy(root, clock=clock, sleeper=clock.sleep)
    calls = []

    def fetch(request):
        calls.append(request.url)
        if request.url.endswith("robots.txt"):
            body = (
                b"User-agent: *\nCrawl-delay: 600\n"
                if "slow.test" in request.url
                else b"User-agent: *\nAllow: /\n"
            )
        else:
            body = b"<rss><channel><item><title>Test</title><link>https://example.test/a</link></item></channel></rss>"
        return FetchResult(url=request.url, status_code=200, content=body)

    adapter = FetcherRobotsAdapter(fetch)

    def robots(url, *, user_agent):
        def guarded(target, *, user_agent):
            with policy.slot(target, robots_request=True):
                return adapter(target, user_agent=user_agent)

        return policy.robots(url, user_agent, guarded)

    deps = ComposeDependencies(
        fetcher=fetch,
        robots=RobotsCache(robots),
        throttle=SharedDomainThrottle(policy),
        retry=RetryPolicy(max_attempts=1),
    )
    with build_pipeline(
        store_root=root,
        actor="tester",
        window="2026-01-01T00:00:00+00:00",
        dependencies=deps,
        on_channel_failure="report",
    ) as pipeline:
        assert pipeline.run(targets=["archive"]).all_succeeded()
        assert fast in calls and slow not in calls
        clock.now = policy.ready_at(slow)
        assert pipeline.run(targets=["archive"]).all_succeeded()
        assert slow in calls
        count = len(calls)
        report = pipeline.run(targets=["archive"])
        assert all(result.status == "skipped" for result in report.results)
        assert len(calls) == count
    with open_archive(root) as archive:
        assert {archive.get(rid).channel_id for rid in archive.all_raw_ids()} == {
            "fast",
            "slow",
        }
