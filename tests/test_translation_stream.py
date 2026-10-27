"""Streaming translation exposes completed batches before the job finishes."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from bs4 import BeautifulSoup

from atlas.reader.content import make_document
from atlas.reader.translations import TranslationJobs
from tests.test_webui_article import article_link, article_world, request
from tests.test_webui_entries_render import running_frontend
from tests.test_webui_users import client
from tests.test_webui_users import request as form_request


def document():
    paragraphs = ''.join(f'<p>Part {i}: ' + ('Original text with detail. ' * 95) + '</p>' for i in range(4))
    return make_document(
        '<article>' + paragraphs + '<pre><code>print("untouched")</code></pre>'
        '<figure><img src="/image.png" alt="figure"></figure></article>',
        'text/html', 'https://example.test/article',
    )


class BlockingClient:
    def __init__(self):
        self.calls = 0
        self.waiting = threading.Event()
        self.release = threading.Event()

    def translate(self, segments):
        self.calls += 1
        if self.calls == 2:
            self.waiting.set()
            if not self.release.wait(15):
                raise TimeoutError('test gate not released')
        return {key: '已翻译 ' + text + ' <script>escaped</script>' for key, text in segments.items()}


def finish(jobs, key):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = jobs.status(key)
        if result['status'] != 'running':
            return result
        time.sleep(.01)
    raise AssertionError('translation did not finish')


def read_sse(response):
    while True:
        line = response.readline()
        if not line:
            raise AssertionError('SSE ended before an update')
        if line.startswith(b'data: '):
            return json.loads(line[6:].decode('utf-8'))


def test_partial_batches_preserve_markup_and_resume_cache(tmp_path):
    fake = BlockingClient()
    jobs = TranslationJobs(tmp_path, client_factory=lambda: fake)
    try:
        key = jobs.start(document())['key']
        assert fake.waiting.wait(5)
        partial = jobs.status(key)
        assert partial['status'] == 'running' and 0 < partial['progress'] < 100
        soup = BeautifulSoup(partial['html'], 'html.parser')
        paragraphs = [p.get_text() for p in soup.select('p')]
        assert any('已翻译' in p for p in paragraphs)
        assert any('已翻译' not in p for p in paragraphs)
        assert not soup.select('script')
        assert soup.select_one('pre code').get_text() == 'print("untouched")'
        assert soup.select_one('img')['src'] == 'https://example.test/image.png'
        fake.release.set()
        complete = finish(jobs, key)
        assert complete['status'] == 'ready'
        assert all('已翻译' in p.get_text() for p in BeautifulSoup(complete['html'], 'html.parser').select('p'))
        count = fake.calls
    finally:
        fake.release.set()
        jobs.close()
    cached = TranslationJobs(tmp_path, client_factory=lambda: pytest.fail('cache not reused'))
    try:
        assert cached.start(document())['status'] == 'ready'
        assert fake.calls == count
    finally:
        cached.close()


def test_sse_delivers_partial_updates_and_reconnects(tmp_path):
    fake = BlockingClient()
    with article_world(tmp_path) as (app, *_):
        target = article_link(app).replace('/article?', '/translate?', 1)
        app.server.reader.loader = lambda entry: document()
        app.server.reader.translations.client_factory = lambda: fake
        response = None
        try:
            status, raw, _ = request(app, target, method='POST')
            assert status == 202
            result = json.loads(raw)
            assert fake.waiting.wait(5)
            url = app.base_url + result['stream_url']
            response = urllib.request.urlopen(url, timeout=10)
            assert response.headers.get_content_type() == 'text/event-stream'
            assert response.headers['X-Accel-Buffering'] == 'no'
            partial = read_sse(response)
            assert partial['status'] == 'running' and '已翻译' in partial['html']
            assert 'Original text' in partial['html'] and partial['progress'] < 100
            response.close()
            response = urllib.request.urlopen(url, timeout=10)
            reconnected = read_sse(response)
            assert reconnected['status'] == 'running' and '已翻译' in reconnected['html']
            fake.release.set()
            soup = BeautifulSoup(reconnected['html'], 'html.parser')
            patches_seen = False
            while True:
                result = read_sse(response)
                assert 'html' not in result
                for node_id, text in result.get('patches', {}).items():
                    soup.select_one(f'[data-atlas-translation-node="{node_id}"]').string = text
                    patches_seen = True
                if result['status'] != 'running':
                    break
            assert result['status'] == 'ready' and result['progress'] == 100 and patches_seen
            assert all('已翻译' in p.get_text() for p in soup.select('p'))
            assert soup.select_one('img')['src'].startswith('/image?')
            assert not soup.select('script')
            assert json.loads(request(app, target, method='POST')[1])['status'] == 'ready'
            assert request(app, '/translation-stream?key=../bad')[0] == 400
        finally:
            fake.release.set()
            if response:
                response.close()


def test_stream_requires_logged_in_session(tmp_path):
    with running_frontend(tmp_path, require_login=True) as world:
        opener, _ = client()
        try:
            opener.open(world.app.base_url + '/translation-stream?key=' + '0' * 64, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            pytest.fail('unauthenticated stream was allowed')
        assert form_request(opener, world.app, '/login', {'name': 'alice'})[0] == 303
        with opener.open(world.app.base_url + '/translation-stream?key=' + '0' * 64, timeout=5) as stream:
            assert stream.status == 200 and read_sse(stream)['status'] == 'idle'


@pytest.mark.parametrize('stream_mode', ['live', 'disconnect', 'stalled'])
def test_browser_reads_completed_parts_and_preserves_nodes(tmp_path, stream_mode):
    api = pytest.importorskip('playwright.sync_api')
    fake = BlockingClient()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        link = article_link(app)
        app.server.reader.loader = lambda entry: document()
        app.server.reader.translations.client_factory = lambda: fake
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 900, 'height': 800})
            urls, errors = [], []
            page.on('request', lambda req: urls.append(req.url))
            page.on('pageerror', lambda error: errors.append(str(error)))
            pending = []
            if stream_mode == 'disconnect':
                page.route('**/translation-stream?*', lambda route: route.abort())
            elif stream_mode == 'stalled':
                page.route('**/translation-stream?*', lambda route: pending.append(route))
            page.goto(app.base_url + link)
            page.get_by_role('tab', name='中文').click()
            assert fake.waiting.wait(5)
            page.wait_for_function("""() => {
              const body = document.querySelector('#body-chinese');
              return body && body.textContent.includes('已翻译') && body.textContent.includes('Original text');
            }""", timeout=10000)
            page.evaluate("window.firstTranslated = Array.from(document.querySelectorAll('#body-chinese [data-atlas-translation-node]')).find(n => n.textContent.includes('已翻译'))")
            assert page.locator('#body-chinese script').count() == 0
            assert page.locator('#body-chinese pre code').inner_text() == 'print("untouched")'
            fake.release.set()
            page.locator('#body-chinese .translation-status').filter(has_text='AI 中文译文').wait_for(timeout=10000)
            assert page.evaluate('window.firstTranslated.isConnected')
            assert all('已翻译' in text for text in page.locator('#body-chinese .article-body p').all_inner_texts())
            assert any('/translation-stream?' in url for url in urls)
            if stream_mode != 'live':
                assert any('/translation?' in url for url in urls)
            else:
                assert not any('/translation?' in url for url in urls)
            assert not errors
        finally:
            fake.release.set()
            browser.close()


def test_browser_keeps_partial_text_on_failure_and_retry(tmp_path):
    api = pytest.importorskip('playwright.sync_api')

    class FailingOnce(BlockingClient):
        def __init__(self):
            super().__init__()
            self.batches = []

        def translate(self, segments):
            self.batches.append(set(segments))
            self.calls += 1
            if self.calls == 2:
                self.waiting.set()
                if not self.release.wait(15):
                    raise TimeoutError('test gate not released')
                raise RuntimeError('temporary failure')
            return {key: '已翻译 ' + text for key, text in segments.items()}

    fake = FailingOnce()
    with article_world(tmp_path) as (app, *_), api.sync_playwright() as playwright:
        link = article_link(app)
        app.server.reader.loader = lambda entry: document()
        app.server.reader.translations.client_factory = lambda: fake
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page()
            page.goto(app.base_url + link)
            page.get_by_role('tab', name='中文').click()
            assert fake.waiting.wait(5)
            page.wait_for_function("document.querySelector('#body-chinese').textContent.includes('已翻译')")
            fake.release.set()
            retry = page.get_by_role('button', name='重试翻译')
            retry.wait_for(timeout=10000)
            assert '已翻译' in page.locator('#body-chinese .article-body').inner_text()
            retry.click()
            page.locator('#body-chinese .translation-status').filter(has_text='AI 中文译文').wait_for(timeout=10000)
            assert not fake.batches[0].intersection(set().union(*fake.batches[2:]))
        finally:
            fake.release.set()
            browser.close()


def test_model_stream_parses_complete_json_items_before_response_ends(monkeypatch):
    from types import SimpleNamespace

    from atlas.reader.translations import TranslationClient

    gate = threading.Event()
    first = json.dumps({'id': '0', 'text': '第一段含引号“好”与换行\n文字'}, ensure_ascii=False)
    second = json.dumps({'id': '1', 'text': '第二段中文。'}, ensure_ascii=False)
    frames = ['{"translations":[', first, ',', second, ']}']

    class Response:
        status_code = 200
        closed = False
        def iter_lines(self, decode_unicode=False):
            assert not decode_unicode
            for index, fragment in enumerate(frames):
                if index == 2:
                    assert gate.wait(5)
                packet = {'choices': [{'delta': {'content': fragment}, 'finish_reason': None}]}
                yield ('data: ' + json.dumps(packet, ensure_ascii=False)).encode('utf-8')
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'
        def close(self):
            self.closed = True

    response = Response()
    def post(**kwargs):
        assert kwargs['stream'] and kwargs['json']['stream']
        return response
    monkeypatch.setattr('atlas.reader.translations.requests.post', post)
    config = SimpleNamespace(base_url='https://example.test', api_key='test', model='model', timeout_seconds=5)
    stream = TranslationClient(config).translate_stream({'0': 'English one', '1': 'English two'})
    assert next(stream) == ('0', '第一段含引号“好”与换行\n文字')
    gate.set()
    assert list(stream) == [('1', '第二段中文。')]
    assert response.closed


def test_single_long_node_updates_while_same_model_request_is_running(tmp_path):
    class StreamingClient:
        def __init__(self):
            self.waiting = threading.Event()
            self.release = threading.Event()
            self.batch_count = 0
        def translate_stream(self, segments):
            self.batch_count += 1
            items = list(segments.items())
            for index, (key, _) in enumerate(items):
                if index == 1:
                    self.waiting.set()
                    assert self.release.wait(10)
                yield key, '已译片段 ' + key

    fake = StreamingClient()
    text = 'This is a complete English sentence with important details. ' * 60
    doc = make_document('<article><p>' + text + '</p></article>', 'text/html', 'https://example.test/one')
    jobs = TranslationJobs(tmp_path, client_factory=lambda: fake)
    try:
        key = jobs.start(doc)['key']
        assert fake.waiting.wait(5)
        partial = jobs.status(key)
        assert partial['status'] == 'running' and fake.batch_count == 1
        body = BeautifulSoup(partial['html'], 'html.parser').select_one('p').get_text()
        assert '已译片段' in body and 'English sentence' in body
        fake.release.set()
        result = finish(jobs, key)
        assert result['status'] == 'ready'
        assert 'English sentence' not in BeautifulSoup(result['html'], 'html.parser').select_one('p').get_text()
    finally:
        fake.release.set()
        jobs.close()


def test_model_stream_rejects_duplicate_and_truncated_output(monkeypatch):
    from types import SimpleNamespace

    from atlas.reader.translations import TranslationClient

    class Response:
        status_code = 200
        def __init__(self, content, finish):
            self.content = content
            self.finish = finish
            self.closed = False
        def iter_lines(self, decode_unicode=False):
            packet = {'choices': [{'delta': {'content': self.content}, 'finish_reason': None}]}
            yield ('data: ' + json.dumps(packet, ensure_ascii=False)).encode('utf-8')
            yield ('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': self.finish}]})).encode()
            yield b'data: [DONE]'
        def close(self):
            self.closed = True

    config = SimpleNamespace(base_url='https://example.test', api_key='test', model='model', timeout_seconds=5)
    client = TranslationClient(config)
    duplicate = Response('{"translations":[{"id":"0","text":"中文"},{"id":"0","text":"重复"}]}', 'stop')
    monkeypatch.setattr('atlas.reader.translations.requests.post', lambda **kwargs: duplicate)
    with pytest.raises(ValueError):
        list(client.translate_stream({'0': 'original'}))
    assert duplicate.closed
    truncated = Response('{"translations":[{"id":"0","text":"中文"}]}', 'length')
    monkeypatch.setattr('atlas.reader.translations.requests.post', lambda **kwargs: truncated)
    with pytest.raises(ValueError):
        list(client.translate_stream({'0': 'original'}))
    assert truncated.closed


def test_old_partial_cache_is_not_misinterpreted_as_new_segments(tmp_path):
    class Client:
        def translate(self, segments):
            return dict.fromkeys(segments, '正确译文')

    doc = make_document('<article><p>' + ('Sentence with content. ' * 100) + '</p></article>',
                        'text/html', 'https://example.test/legacy')
    jobs = TranslationJobs(tmp_path, client_factory=Client)
    try:
        key = jobs.key(doc)
        jobs._write(key, {'segments': {'0': '过时的错误片段'}})
        assert jobs.start(doc)['status'] == 'running'
        result = finish(jobs, key)
        assert result['status'] == 'ready'
        assert '正确译文' in result['html'] and '过时的错误片段' not in result['html']
    finally:
        jobs.close()


def test_failed_model_stream_does_not_cache_unverified_segments(tmp_path):
    class Client:
        def __init__(self):
            self.calls = 0
            self.first_batch = None
        def translate_stream(self, segments):
            self.calls += 1
            if self.calls == 1:
                self.first_batch = set(segments)
                yield next(iter(segments)), '暂时译文'
                raise ValueError('truncated model JSON')
            for part_id in segments:
                yield part_id, '经过验证的译文'

    fake = Client()
    doc = make_document('<p>' + ('Original sentence. ' * 100) + '</p>',
                        'text/html', 'https://example.test/failure')
    jobs = TranslationJobs(tmp_path, client_factory=lambda: fake)
    try:
        key = jobs.start(doc)['key']
        failed = finish(jobs, key)
        assert failed['status'] == 'error' and '暂时译文' in failed['html']
        assert not (tmp_path / (key + '.json')).exists()
        assert jobs.start(doc)['status'] == 'running'
        complete = finish(jobs, key)
        assert complete['status'] == 'ready'
        assert '经过验证的译文' in complete['html'] and '暂时译文' not in complete['html']
    finally:
        jobs.close()
